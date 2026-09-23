"""交互式可视化 GUI：http://127.0.0.1:8901

功能：
- 输入任务，实时观看 ReAct 轨迹（推理 → 工具调用 → 观察结果）；
- 选择记忆策略（recent / relevance / impact）与上下文预算，实时看“本次选择”统计；
- 工具审批（HITL）：自动放行 / 仅 bash 需审批 / 全部需审批，页面上点“批准/拒绝”
  （30 秒不操作自动放行）；
- 实时面板：上下文选择、token 统计、工具调用、长期记忆读写、最终结果；
- 记忆查看：本轮注入的历史记忆（含相关度分数）+ 记忆库浏览器（搜索 / 逐条删除 / 清空）。

零依赖（Python 标准库 + 原生 JS），运行：
    .\\.venv\\Scripts\\python.exe gui.py --port 8901
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mini_agent.config import CountingLLM, llm_kwargs, warmup_async
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_agent import MemoryAgent
from mini_agent.tracing import TraceLogger

HERE = Path(__file__).resolve().parent
APPROVAL_TIMEOUT = 30.0
STATE_LOCK = threading.Lock()
STATE = {"run": None}
PORT = 8901


def ltm_path() -> Path:
    return HERE.parent / "results" / "gui_ltm.sqlite3"


def _read_memory(query: str = "", limit: int = 300) -> dict:
    """读取长期记忆库（记忆浏览器用）：按写入时间倒序，可选子串过滤。"""
    path = ltm_path()
    if not path.exists():
        return {"total": 0, "items": []}
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT id, text, session_id, task_id, kind, created_at "
            "FROM memories ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    items = [
        {
            "id": r[0],
            "text": r[1],
            "session_id": r[2],
            "task_id": r[3],
            "kind": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]
    if query:
        needle = query.lower()
        items = [
            it
            for it in items
            if needle in (it["text"] or "").lower()
            or needle in (it["session_id"] or "").lower()
            or needle in (it["task_id"] or "").lower()
        ]
    return {"total": len(items), "items": items[:limit]}


class RunState:
    def __init__(self, task, policy, budget, approval_mode, max_steps):
        self.task = task
        self.policy = policy
        self.budget = budget
        self.approval_mode = approval_mode
        self.max_steps = max_steps
        self.status = "running"
        self.error = ""
        self.output = ""
        self.started_at = time.time()
        self.finished_at = None
        self.trace = TraceLogger()
        self.agent = None
        self.llm = None
        self.ltm_rows = []
        self.pending = None
        self._approval_event = threading.Event()
        self._approval_result = True

    def approval_fn(self, tool_name, args):
        gated = self.approval_mode == "all" or (
            self.approval_mode == "bash" and tool_name == "bash_execute"
        )
        if not gated:
            return True
        self.pending = {
            "tool": tool_name,
            "args": {k: str(v)[:200] for k, v in args.items()},
            "ts": time.time(),
        }
        self._approval_event.clear()
        got = self._approval_event.wait(timeout=APPROVAL_TIMEOUT)
        decision = self._approval_result if got else True
        self.pending = None
        return decision

    def resolve_approval(self, approved: bool):
        self._approval_result = bool(approved)
        self._approval_event.set()

    def to_dict(self):
        data = {
            "status": self.status,
            "error": self.error,
            "output": self.output[:4000],
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "policy": self.policy,
            "budget": self.budget,
            "approval_mode": self.approval_mode,
            "task": self.task,
            "pending": self.pending,
            "llm": {},
            "selection": {},
            "retrieved": [],
            "ltm": self.ltm_rows,
            "messages": [],
            "trace": [
                {
                    k: v
                    for k, v in event.items()
                    if k
                    in (
                        "ts",
                        "event",
                        "tool",
                        "approved",
                        "name",
                        "success",
                        "plan",
                        "review",
                        "latency",
                        "prompt_tokens",
                        "completion_tokens",
                        "id",
                        "text",
                        "hits",
                    )
                }
                for event in self.trace.events[-40:]
            ],
        }
        if self.llm is not None:
            data["llm"] = {
                "calls": self.llm.calls,
                "prompt_tokens": self.llm.prompt_tokens,
                "completion_tokens": self.llm.completion_tokens,
                "latency_avg": round(sum(self.llm.latencies) / len(self.llm.latencies), 2)
                if self.llm.latencies
                else 0,
            }
        if self.agent is not None:
            data["selection"] = dict(self.agent.memory.last_selection)
            data["retrieved"] = [
                {
                    "text": (h.get("text") or "")[:160],
                    "score": h.get("score"),
                    "kind": h.get("kind"),
                    "session_id": h.get("session_id"),
                }
                for h in (self.agent.retrieved or [])
            ]
            data["messages"] = [
                {
                    "role": msg.role.value,
                    "content": str(msg.content or "")[:1500],
                    "tool_calls": [
                        call.get("function", {}).get("name") for call in (msg.tool_calls or [])
                    ],
                    "tool_call_id": msg.tool_call_id,
                }
                for msg in self.agent.memory.messages
            ]
        return data


def run_worker(state: RunState):
    async def worker():
        workdir = tempfile.mkdtemp(prefix="gui_run_")
        old_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            state.llm = CountingLLM(
                **llm_kwargs(), trace=state.trace, max_retries=8, base_delay=3.0
            )
            # 长期记忆跨运行持久化（放在 results/ 下），演示“记住 → 换会话使用”
            path = ltm_path()
            path.parent.mkdir(exist_ok=True)
            ltm = LongTermMemory(path=str(path))
            state.agent = MemoryAgent(
                llm=state.llm,
                ltm=ltm,
                session_id=f"gui-{int(state.started_at)}",
                policy=state.policy,
                budget_chars=state.budget,
                approval_fn=state.approval_fn,
                trace=state.trace,
                max_steps=state.max_steps,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                state.output = await state.agent.run(state.task)
            state.ltm_rows = ltm.all()
            ltm.close()
        except Exception as exc:
            state.error = str(exc)
        finally:
            try:
                if state.llm is not None:
                    await state.llm.client.close()
            except Exception:
                pass
            os.chdir(old_cwd)
            state.status = "error" if state.error else "done"
            state.finished_at = time.time()

    asyncio.run(worker())


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>MiniAgent 可视化 Demo</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", sans-serif; margin: 0; background:#f6f7f9; color:#1f2937; }
  header { background:#1F4E79; color:#fff; padding:14px 22px; display:flex; align-items:center; gap:14px; }
  header h1 { font-size:17px; margin:0; }
  .pill { font-size:12px; padding:3px 10px; border-radius:10px; background:#ffffff33; }
  .pill.done { background:#10b981; } .pill.error { background:#ef4444; }
  .wrap { display:grid; grid-template-columns: 360px 1fr; gap:16px; padding:16px 22px; align-items:start; }
  .card { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:14px 16px; margin-bottom:14px; }
  .card h3 { margin:0 0 10px; font-size:14px; color:#374151; }
  label { display:block; font-size:12px; color:#6b7280; margin:8px 0 4px; }
  textarea, input, select { width:100%; padding:8px 10px; border:1px solid #d1d5db; border-radius:8px; font-size:13px; font-family:inherit; }
  textarea { height:84px; resize:vertical; }
  .row { display:flex; gap:10px; } .row > div { flex:1; }
  button { border:0; border-radius:8px; padding:9px 14px; font-size:13px; cursor:pointer; }
  button.primary { background:#2563eb; color:#fff; width:100%; margin-top:12px; font-weight:700; }
  button.primary:disabled { background:#9ca3af; cursor:not-allowed; }
  button.ok { background:#10b981; color:#fff; } button.no { background:#ef4444; color:#fff; }
  .kv { font-size:12.5px; color:#374151; display:flex; justify-content:space-between; padding:2px 0; }
  .kv b { color:#111827; }
  .steps { max-height:520px; overflow:auto; }
  .msg { border:1px solid #eef0f3; border-radius:10px; padding:8px 12px; margin-bottom:8px; font-size:13px; }
  .msg .who { font-size:11px; font-weight:700; margin-bottom:3px; }
  .msg.user { background:#eff6ff; } .msg.user .who { color:#1d4ed8; }
  .msg.assistant { background:#f9fafb; } .msg.assistant .who { color:#374151; }
  .msg.tool { background:#f0fdf4; } .msg.tool .who { color:#047857; }
  .msg.system { background:#fefce8; } .msg.system .who { color:#a16207; }
  .pre { white-space:pre-wrap; word-break:break-word; }
  .banner { display:none; background:#fff7ed; border:1px solid #fdba74; border-radius:10px; padding:12px 14px; margin-bottom:12px; }
  .banner b { color:#c2410c; }
  .trace div { font-size:12px; border-bottom:1px dashed #f0f0f0; padding:3px 0; color:#4b5563; }
  .muted { color:#9ca3af; font-size:12px; }
  #output { white-space:pre-wrap; font-size:13px; }
</style></head>
<body>
<header>
  <h1>MiniAgent 可视化 Demo</h1>
  <span class="pill" id="status">空闲</span>
  <span class="muted" id="elapsed" style="color:#dbeafe"></span>
</header>

<div class="wrap">
  <div>
    <div class="card">
      <h3>任务与配置</h3>
      <label>示例任务</label>
      <select id="example" onchange="pickExample()">
        <option value="">（选择后自动填入下方）</option>
        <option value="创建 hello.txt，内容为 Hello GUI。完成后告诉我。">创建 hello.txt</option>
        <option value="用 Python 计算 1 到 100 的和，直接告诉我结果。">计算 1~100 的和</option>
        <option value="用 http_get 查询 https://wttr.in/Beijing?format=3 ，告诉我北京现在的天气。">北京现在的天气（联网）</option>
        <option value="搜索「Python 3.12 新特性」，给我三条摘要。">联网搜索：Python 3.12 新特性</option>
        <option value="用 bash 命令创建一个文件 note.txt，内容为 HITL-OK；如果命令被拒绝，请改用其他工具完成。">HITL：bash 被拒改用其他工具</option>
        <option value="创建 reports/project.txt，内容写我的项目代号。完成后告诉我。">跨会话记忆：项目代号</option>
      </select>
      <label>任务内容</label>
      <textarea id="task">创建 hello.txt，内容为 Hello GUI。完成后告诉我。</textarea>
      <div class="row">
        <div><label>记忆策略</label>
          <select id="policy">
            <option value="recent">recent（近因）</option>
            <option value="relevance" selected>relevance（相关性）</option>
            <option value="impact">impact（决策影响）</option>
            <option value="all">all（不裁剪）</option>
          </select></div>
        <div><label>预算（字符，跨会话建议 1200）</label><input id="budget" type="number" value="500" min="100" step="50"></div>
      </div>
      <div class="row">
        <div><label>工具审批</label>
          <select id="approval">
            <option value="auto" selected>自动放行</option>
            <option value="bash">仅 bash 需审批</option>
            <option value="all">全部需审批</option>
          </select></div>
        <div><label>最大步数（联网研究 20+）</label><input id="maxSteps" type="number" value="20" min="1" max="50"></div>
      </div>
      <button class="primary" id="runBtn" onclick="runTask()">▶ 运行任务</button>
      <button class="primary" id="retryBtn" style="display:none;background:#f59e0b" onclick="retryLast()">↻ 重试上次任务</button>
    </div>

    <div class="card"><h3>本次上下文选择</h3><div id="selection" class="muted">暂无</div></div>
    <div class="card"><h3>Token / 延迟</h3><div id="llm" class="muted">暂无</div></div>
    <div class="card"><h3>长期记忆（本轮）<button style="float:right;font-size:11px;padding:2px 8px;background:#f3f4f6" onclick="clearLtm()">清空记忆库</button></h3><div id="ltm" class="muted">暂无</div></div>
  </div>

  <div>
    <div class="banner" id="approvalBanner">
      <b>⚠ 工具审批请求</b>：<span id="approvalText"></span>
      <div style="margin-top:8px"><button class="ok" onclick="decide(true)">批准</button>
      &nbsp;<button class="no" onclick="decide(false)">拒绝</button>
      &nbsp;<span class="muted">30 秒不操作自动放行</span></div>
    </div>
    <div class="card"><h3>ReAct 轨迹（实时）</h3><div class="steps" id="steps"><div class="muted">点“运行任务”开始</div></div></div>
    <div class="card"><h3>事件流（tracing）</h3><div class="trace" id="trace"><div class="muted">暂无</div></div></div>
    <div class="card"><h3>最终结果</h3><div id="output" class="muted">暂无</div></div>
    <div class="card">
      <h3>记忆库浏览器（跨会话持久化）
        <span style="float:right;font-weight:400">
          <input id="memQuery" placeholder="搜索记忆内容…" style="width:190px;display:inline-block;padding:4px 8px;font-size:12px" onkeydown="if(event.key==='Enter')loadMemory()">
          <button style="font-size:11px;padding:4px 10px;background:#e5e7eb" onclick="loadMemory()">搜索</button>
          <button style="font-size:11px;padding:4px 10px;background:#e5e7eb" onclick="$('memQuery').value='';loadMemory()">重置</button>
        </span>
      </h3>
      <div id="memList" class="steps" style="max-height:340px"><div class="muted">暂无</div></div>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let polling = null;
const esc = (s) => (s || "").replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function pickExample() {
  const v = $('example').value;
  if (v) $('task').value = v;
}

async function runTask() {
  const body = {
    task: $('task').value.trim(),
    policy: $('policy').value,
    budget: parseInt($('budget').value || '500', 10),
    approval_mode: $('approval').value,
    max_steps: parseInt($('maxSteps').value || '20', 10),
  };
  await submit(body);
}

async function retryLast() {
  if (!lastBody) { return; }
  await submit(lastBody);
}

let lastBody = null;
async function submit(body) {
  if (!body.task) { alert('请填写任务'); return; }
  lastBody = body;
  $('runBtn').disabled = true;
  $('retryBtn').style.display = 'none';
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!j.ok) { alert('启动失败: ' + (j.error || r.status)); $('runBtn').disabled = false; return; }
  startPolling();
}

async function decide(approved) {
  await fetch('/api/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approved})});
}

async function clearLtm() {
  await fetch('/api/clear_ltm', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
  refresh(); loadMemory();
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadMemory() {
  const q = $('memQuery').value.trim();
  let data;
  try { data = await (await fetch('/api/memory?q=' + encodeURIComponent(q))).json(); } catch (e) { return; }
  const items = data.items || [];
  $('memList').innerHTML = items.length ? items.map(it => `
    <div class="msg" style="background:#fafafa">
      <div class="who">#${it.id} · ${esc(it.kind || '')} · 会话 ${esc(it.session_id || '-')} · ${fmtTime(it.created_at)}
        <button style="float:right;font-size:11px;padding:1px 8px;background:#fee2e2;color:#991b1b" onclick="deleteMemory(${it.id})">删除</button>
      </div>
      <div class="pre">${esc(it.text)}</div>
    </div>`).join('') + `<div class="muted" style="margin-top:6px">共 ${data.total} 条${q ? `（匹配「${esc(q)}」）` : ''}</div>`
    : '<div class="muted">没有匹配的记忆</div>';
}

async function deleteMemory(id) {
  await fetch('/api/memory_delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
  loadMemory();
}

function startPolling() {
  if (polling) return;
  polling = setInterval(refresh, 800);
  refresh();
}

let lastStatus = '';
async function refresh() {
  let s;
  try { s = await (await fetch('/api/state')).json(); } catch (e) { return; }
  render(s);
  if (s.status === 'done' || s.status === 'error' || s.status === 'idle') {
    clearInterval(polling); polling = null; $('runBtn').disabled = false;
    if (lastStatus === 'running') loadMemory();  // 运行结束：刷新记忆库（本轮新写入）
  }
  lastStatus = s.status;
}

function render(s) {
  const statusMap = { idle: '空闲', running: '运行中', done: '完成', error: '出错' };
  const pill = $('status');
  pill.textContent = statusMap[s.status] || s.status;
  pill.className = 'pill ' + (s.status === 'done' ? 'done' : (s.status === 'error' ? 'error' : ''));
  $('elapsed').textContent = s.elapsed ? (s.elapsed + ' s') : '';

  const sel = s.selection || {};
  $('selection').innerHTML = sel.total_messages
    ? `<div class="kv"><span>策略 / 预算</span><b>${esc(sel.policy)} / ${sel.budget_chars} 字符</b></div>
       <div class="kv"><span>消息数（选中/总）</span><b>${sel.selected_messages} / ${sel.total_messages}</b></div>
       <div class="kv"><span>字符（选中/总）</span><b>${sel.selected_chars} / ${sel.total_chars}</b></div>`
    : '<span class="muted">暂无</span>';

  const llm = s.llm || {};
  $('llm').innerHTML = llm.calls
    ? `<div class="kv"><span>LLM 调用</span><b>${llm.calls}</b></div>
       <div class="kv"><span>Prompt / Completion tokens</span><b>${llm.prompt_tokens} / ${llm.completion_tokens}</b></div>
       <div class="kv"><span>平均延迟</span><b>${llm.latency_avg} s</b></div>`
    : '<span class="muted">暂无</span>';

  const ltm = s.ltm || [];
  const writes = (s.trace || []).filter(e => e.event === 'memory_write');
  let ltmHtml = '';
  if (writes.length) ltmHtml += writes.map(e => `<div class="pre" style="font-size:12px">✍ ${esc(e.text || '')}</div>`).join('');
  const retrieved = s.retrieved || [];
  if (retrieved.length) {
    ltmHtml += `<div class="muted" style="margin-top:8px">📖 本轮注入 ${retrieved.length} 条历史记忆：</div>`;
    ltmHtml += retrieved.map(h => `<div class="pre" style="font-size:11.5px;background:#f8fafc;border-radius:6px;padding:6px;margin-top:4px">`
      + `<b>相关度 ${h.score == null ? '-' : h.score}</b> · ${esc(h.kind || '')} · ${esc(h.session_id || '-')}<br>${esc(h.text)}</div>`).join('');
  }
  if (ltm.length) ltmHtml += `<div class="muted" style="margin-top:6px">记忆库共 ${ltm.length} 条（见右下方浏览器）</div>`;
  $('ltm').innerHTML = ltmHtml || '<span class="muted">暂无</span>';

  const banner = $('approvalBanner');
  if (s.pending) {
    banner.style.display = 'block';
    $('approvalText').textContent = `${s.pending.tool}(${JSON.stringify(s.pending.args)})`;
  } else { banner.style.display = 'none'; }

  const steps = s.messages || [];
  $('steps').innerHTML = steps.length ? steps.map(m => {
    const who = {user:'🧑 用户', assistant:'🤖 模型', tool:'🔧 工具结果', system:'📌 系统(长期记忆)'}[m.role] || m.role;
    const calls = (m.tool_calls && m.tool_calls.length) ? `<div class="muted">→ 调用工具: ${esc(m.tool_calls.join(', '))}</div>` : '';
    const content = m.content ? esc(m.content) : '<span class="muted">(空)</span>';
    return `<div class="msg ${m.role}"><div class="who">${who}</div>${calls}<div class="pre">${content}</div></div>`;
  }).join('') : '<div class="muted">暂无</div>';

  const trace = s.trace || [];
  $('trace').innerHTML = trace.length ? trace.slice().reverse().map(e => {
    const meta = { llm: 'LLM', tool: '工具', approval: '审批', memory_read: '记忆读', memory_write: '记忆写' }[e.event] || e.event;
    let detail = '';
    if (e.event === 'llm') detail = `tokens ${e.prompt_tokens}/${e.completion_tokens}, ${e.latency}s`;
    else if (e.event === 'tool') detail = `${e.name} ${e.success ? '✓' : '✗'}`;
    else if (e.event === 'approval') detail = `${e.tool} ${e.approved ? '放行' : '拒绝'}`;
    else if (e.event === 'memory_read') detail = `命中 ${ (e.hits || []).length } 条`;
    else if (e.event === 'memory_write') detail = String(e.text || '').slice(0, 60);
    return `<div>${meta} · ${esc(detail)}</div>`;
  }).join('') : '<div class="muted">暂无</div>';

  $('output').innerHTML = s.error
    ? `<span style="color:#b91c1c">错误: ${esc(s.error)}</span>`
    : (s.output
        ? (s.output.includes('LLM调用失败')
            ? `<span style="color:#b91c1c">⚠ 接口异常：${esc(s.output)}<br>可点上方橙色「重试上次任务」</span>`
            : esc(s.output))
        : '<span class="muted">暂无</span>');
  $('retryBtn').style.display = (s.status === 'done' && s.output && s.output.includes('LLM调用失败')) ? 'block' : 'none';
}
refresh();
loadMemory();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with STATE_LOCK:
                run = STATE["run"]
                data = run.to_dict() if run else {"status": "idle"}
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return
        if self.path.startswith("/api/memory"):
            query = ""
            if "?" in self.path:
                parsed = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                query = (parsed.get("q") or [""])[0].strip()
            data = _read_memory(query)
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")

        if self.path.startswith("/api/run"):
            task = (payload.get("task") or "").strip()
            if not task:
                self._send(400, b'{"ok":false,"error":"task empty"}')
                return
            run = RunState(
                task=task,
                policy=payload.get("policy", "relevance"),
                budget=int(payload.get("budget", 500)),
                approval_mode=payload.get("approval_mode", "auto"),
                max_steps=int(payload.get("max_steps", 20)),
            )
            with STATE_LOCK:
                STATE["run"] = run
            threading.Thread(target=run_worker, args=(run,), daemon=True).start()
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/approve"):
            with STATE_LOCK:
                run = STATE["run"]
            if run is not None:
                run.resolve_approval(bool(payload.get("approved", True)))
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/clear_ltm"):
            path = ltm_path()
            if path.exists():
                conn = sqlite3.connect(str(path))
                conn.execute("DELETE FROM memories")
                conn.commit()
                conn.close()
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/memory_delete"):
            mem_id = int(payload.get("id") or 0)
            path = ltm_path()
            deleted = 0
            if mem_id and path.exists():
                conn = sqlite3.connect(str(path))
                deleted = conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,)).rowcount
                conn.commit()
                conn.close()
            self._send(200, json.dumps({"ok": True, "deleted": deleted}).encode("utf-8"))
            return

        self._send(404, b'{"ok":false}')

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()
    PORT = args.port

    # 启动时后台预热一次（规避冷启动/瞬时网络问题导致的首次调用失败）
    threading.Thread(
        target=lambda: asyncio.run(warmup_async(attempts=3, delay=2.0)), daemon=True
    ).start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[gui] http://127.0.0.1:{PORT} （Ctrl+C 停止）", flush=True)
    server.serve_forever()
