"""GUI 页面（HTML/CSS/JS，零依赖）。

视觉参考成熟 Agent 工具（Langfuse / LangSmith 的 trace 视图、OpenHands、Chainlit）：
- 顶部栏：品牌 + 状态/耗时/token 指标 chips + 明暗主题切换；
- 三栏响应式：左=运行控制与统计，中=ReAct 轨迹与结果，右=事件流与记忆库浏览器；
- 卡片化 + 统一设计变量（圆角/阴影/配色），轨迹按角色着色，事件流为时间线；
- 悬浮审批条（HITL 请求时出现在顶部，不遮挡内容）。

功能钩子（元素 id）与 scripts/gui.py 的 JS 逻辑保持一致：
example / task / policy / budget / approval / maxSteps / autoParams / runBtn / retryBtn /
status / elapsed / chipLlm / chipTok / progress / autoPlan / selection / llm / ltm /
approvalBanner / approvalText / steps / trace / output / memQuery / memList
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MiniAgent Memory Lab · 可视化 Demo</title>
<style>
  :root {
    --bg:#f4f6fb; --panel:#ffffff; --panel-2:#f8fafc; --border:#e6e9f2;
    --text:#0f172a; --muted:#64748b; --faint:#94a3b8;
    --accent:#4f46e5; --accent-2:#6366f1; --accent-soft:#eef2ff;
    --ok:#10b981; --warn:#f59e0b; --err:#ef4444; --info:#0ea5e9;
    --radius:14px; --radius-sm:9px;
    --shadow:0 1px 2px rgba(15,23,42,.04), 0 10px 28px -18px rgba(15,23,42,.35);
    --mono:"JetBrains Mono","Cascadia Mono",Consolas,"Courier New",monospace;
  }
  html[data-theme="dark"] {
    --bg:#0b1020; --panel:#121a2e; --panel-2:#0f1729; --border:#1f2a44;
    --text:#e8ecf6; --muted:#93a1bd; --faint:#6b7a99;
    --accent:#7c83ff; --accent-2:#8b8ffb; --accent-soft:#1b2244;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 12px 30px -20px rgba(0,0,0,.9);
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font-family:"Inter","Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
    font-size:13px; line-height:1.55;
  }
  ::-webkit-scrollbar { width:9px; height:9px; }
  ::-webkit-scrollbar-thumb { background:var(--border); border-radius:9px; }
  ::-webkit-scrollbar-thumb:hover { background:var(--faint); }

  /* 顶部栏 */
  .topbar {
    position:sticky; top:0; z-index:20; display:flex; align-items:center; gap:14px;
    padding:12px 22px; background:var(--panel); border-bottom:1px solid var(--border);
    box-shadow:0 1px 0 rgba(15,23,42,.02);
  }
  .brand { display:flex; align-items:center; gap:11px; margin-right:auto; }
  .logo {
    width:34px; height:34px; border-radius:10px; display:grid; place-items:center;
    background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff;
    font-weight:800; font-size:15px; letter-spacing:.5px;
  }
  .brand h1 { font-size:15px; margin:0; font-weight:700; letter-spacing:.2px; }
  .brand .sub { font-size:11.5px; color:var(--muted); margin-top:1px; }
  .chips { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .chip {
    font-size:11.5px; padding:4px 11px; border-radius:999px; border:1px solid var(--border);
    background:var(--panel-2); color:var(--muted); white-space:nowrap;
  }
  .chip b { color:var(--text); font-weight:600; }
  .chip.state { border-color:transparent; background:var(--accent-soft); color:var(--accent); font-weight:600; }
  .chip.state.done { background:#e7f8f1; color:#047857; }
  .chip.state.error { background:#fdecec; color:#b91c1c; }
  .chip.state.running { animation:pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.55} }
  .icon-btn {
    width:32px; height:32px; border-radius:10px; border:1px solid var(--border);
    background:var(--panel-2); color:var(--muted); cursor:pointer; font-size:14px;
  }
  .icon-btn:hover { color:var(--text); border-color:var(--faint); }

  /* 进度条 */
  #progress { height:3px; background:transparent; overflow:hidden; }
  #progress.on { background:linear-gradient(90deg,var(--accent) 0%,var(--accent-2) 40%,var(--accent) 80%); background-size:200% 100%; animation:slide 1.1s linear infinite; }
  @keyframes slide { 0%{background-position:0 0} 100%{background-position:200% 0} }

  /* 布局 */
  .layout { display:grid; grid-template-columns:340px minmax(0,1fr) 380px; gap:16px; padding:16px 22px 40px; align-items:start; }
  .col { display:flex; flex-direction:column; gap:14px; min-width:0; }
  @media (max-width:1280px) { .layout { grid-template-columns:330px minmax(0,1fr); } .col.right { grid-column:1 / -1; } }
  @media (max-width:860px) { .layout { grid-template-columns:1fr; } }

  /* 卡片 */
  .card { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden; }
  .card > h3 {
    margin:0; padding:11px 14px; font-size:12.5px; font-weight:600; letter-spacing:.2px;
    border-bottom:1px solid var(--border); background:var(--panel-2);
    display:flex; align-items:center; gap:8px;
  }
  .card > h3 .spacer { margin-left:auto; }
  .card .body { padding:13px 14px; }
  .muted { color:var(--muted); }
  .faint { color:var(--faint); }
  .kv { display:flex; justify-content:space-between; gap:10px; padding:3px 0; font-size:12.5px; }
  .kv b { font-weight:600; }
  .pre { white-space:pre-wrap; word-break:break-word; font-size:12.5px; }

  /* 表单 */
  label { display:block; font-size:11.5px; color:var(--muted); margin:9px 0 4px; }
  textarea, input, select {
    width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:var(--radius-sm);
    font-size:12.5px; font-family:inherit; background:var(--panel); color:var(--text); outline:none;
  }
  textarea { height:82px; resize:vertical; }
  textarea:focus, input:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(99,102,241,.16); }
  input[disabled] { background:var(--panel-2); color:var(--faint); }
  .row { display:flex; gap:10px; } .row > div { flex:1; min-width:0; }
  .switch { display:flex; align-items:center; gap:8px; margin:11px 0 2px; font-size:12px; color:var(--text); }
  .switch input { width:auto; }

  /* 按钮 */
  button { border:0; border-radius:var(--radius-sm); padding:8px 13px; font-size:12.5px; cursor:pointer; font-family:inherit; }
  .btn-primary {
    width:100%; margin-top:12px; font-weight:600; color:#fff;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    box-shadow:0 8px 18px -10px rgba(79,70,229,.9);
  }
  .btn-primary:hover { filter:brightness(1.06); }
  .btn-primary:disabled { background:var(--faint); box-shadow:none; cursor:not-allowed; }
  .btn-warn { background:linear-gradient(135deg,#f59e0b,#f97316); color:#fff; }
  .btn-ghost { background:var(--panel-2); border:1px solid var(--border); color:var(--muted); }
  .btn-ghost:hover { color:var(--text); }
  .btn-danger { background:#fdecec; color:#b91c1c; }
  .btn-xs { padding:3px 9px; font-size:11px; }

  /* 轨迹 */
  .steps { max-height:560px; overflow:auto; padding:12px 14px; display:flex; flex-direction:column; gap:9px; }
  .msg { border:1px solid var(--border); border-radius:var(--radius-sm); padding:9px 12px; background:var(--panel); }
  .msg .who { font-size:10.5px; font-weight:700; letter-spacing:.4px; color:var(--muted); margin-bottom:4px; text-transform:uppercase; }
  .msg.user { background:var(--accent-soft); border-color:transparent; }
  .msg.user .who { color:var(--accent); }
  .msg.assistant { border-left:3px solid var(--accent); }
  .msg.tool { background:var(--panel-2); border-left:3px solid var(--info); }
  .msg.tool .pre { font-family:var(--mono); font-size:11.5px; }
  .msg.system { border-style:dashed; border-left:3px solid var(--warn); }
  .toolchip {
    display:inline-block; font-family:var(--mono); font-size:11px; padding:1px 8px; margin:3px 4px 0 0;
    border-radius:999px; background:var(--accent-soft); color:var(--accent); border:1px solid transparent;
  }

  /* 事件流时间线 */
  .trace { max-height:340px; overflow:auto; padding:10px 14px; }
  .tl { display:flex; align-items:baseline; gap:9px; padding:4px 0; font-size:12px; border-bottom:1px dashed var(--border); }
  .tl:last-child { border-bottom:0; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--faint); flex:0 0 auto; margin-top:5px; }
  .dot.llm { background:var(--accent); } .dot.tool { background:var(--info); }
  .dot.approval { background:var(--warn); } .dot.memory { background:var(--ok); }
  .tl .meta { margin-left:auto; color:var(--faint); font-family:var(--mono); font-size:11px; text-align:right; }

  /* 结果 */
  #output { white-space:pre-wrap; word-break:break-word; }

  /* 悬浮审批条 */
  .banner {
    display:none; position:fixed; top:66px; left:50%; transform:translateX(-50%); z-index:30;
    width:min(680px,92vw); padding:13px 16px; border-radius:var(--radius);
    background:var(--panel); border:1px solid var(--warn); box-shadow:0 18px 44px -18px rgba(15,23,42,.5);
  }
  .banner b { color:#b45309; }

  /* 记忆库 */
  .mem { border:1px solid var(--border); border-radius:var(--radius-sm); padding:9px 11px; margin-bottom:8px; background:var(--panel-2); }
  .mem .head { display:flex; align-items:center; gap:7px; font-size:11px; color:var(--muted); margin-bottom:5px; }
  .mem .head .spacer { margin-left:auto; }
  .mem .text { font-size:12px; white-space:pre-wrap; word-break:break-word; }
  .score { font-family:var(--mono); font-size:11px; color:var(--accent); background:var(--accent-soft); padding:1px 7px; border-radius:999px; }
  .empty { color:var(--faint); font-size:12px; padding:6px 0; }
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <div class="logo">M</div>
    <div>
      <h1>MiniAgent Memory Lab</h1>
      <div class="sub">记忆策略 · 预算扫描 · HITL 审批 · 联网工具</div>
    </div>
  </div>
  <div class="chips">
    <span class="chip state" id="status">空闲</span>
    <span class="chip" id="elapsed"></span>
    <span class="chip" id="chipLlm"></span>
    <span class="chip" id="chipTok"></span>
    <button class="icon-btn" onclick="toggleTheme()" title="明暗主题切换">◐</button>
  </div>
</header>
<div id="progress"></div>

<div class="layout">
  <!-- 左栏：运行控制 + 统计 + 本轮记忆 -->
  <div class="col left">
    <div class="card">
      <h3>运行任务</h3>
      <div class="body">
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
          <div><label>预算（字符）</label><input id="budget" type="number" value="500" min="100" step="50" disabled></div>
        </div>
        <div class="row">
          <div><label>工具审批</label>
            <select id="approval">
              <option value="auto" selected>自动放行</option>
              <option value="bash">仅 bash 需审批</option>
              <option value="all">全部需审批</option>
            </select></div>
          <div><label>最大步数</label><input id="maxSteps" type="number" value="20" min="1" max="50" disabled></div>
        </div>
        <div class="switch">
          <input type="checkbox" id="autoParams" checked onchange="toggleAuto()">
          <span>让模型自动选择预算 / 步数（推荐）</span>
        </div>
        <button class="btn-primary" id="runBtn" onclick="runTask()">▶ 运行任务</button>
        <button class="btn-primary btn-warn" id="retryBtn" style="display:none" onclick="retryLast()">↻ 重试上次任务</button>
      </div>
    </div>

    <div class="card">
      <h3>运行参数 <span class="spacer faint" style="font-weight:400">预算 / 步数</span></h3>
      <div class="body" id="autoPlan"><span class="muted">暂无</span></div>
    </div>

    <div class="card">
      <h3>本次上下文选择</h3>
      <div class="body" id="selection"><span class="muted">暂无</span></div>
    </div>

    <div class="card">
      <h3>Token / 延迟</h3>
      <div class="body" id="llm"><span class="muted">暂无</span></div>
    </div>

    <div class="card">
      <h3>长期记忆（本轮）<span class="spacer"></span>
        <button class="btn-ghost btn-xs" onclick="clearLtm()">清空记忆库</button>
      </h3>
      <div class="body" id="ltm"><span class="muted">暂无</span></div>
    </div>
  </div>

  <!-- 中栏：轨迹 + 结果 -->
  <div class="col mid">
    <div class="card">
      <h3>ReAct 轨迹（实时）<span class="spacer faint" style="font-weight:400">推理 → 工具 → 观察</span></h3>
      <div class="steps" id="steps"><div class="empty">点「运行任务」开始</div></div>
    </div>
    <div class="card">
      <h3>最终结果</h3>
      <div class="body" id="output"><span class="muted">暂无</span></div>
    </div>
  </div>

  <!-- 右栏：事件流 + 记忆库浏览器 -->
  <div class="col right">
    <div class="card">
      <h3>事件流（tracing）</h3>
      <div class="trace" id="trace"><div class="empty">暂无</div></div>
    </div>
    <div class="card">
      <h3>记忆库浏览器<span class="spacer"></span>
        <input id="memQuery" placeholder="搜索记忆…" style="width:120px;padding:4px 8px;font-size:11.5px"
               onkeydown="if(event.key==='Enter')loadMemory()">
        <button class="btn-ghost btn-xs" onclick="loadMemory()">搜索</button>
        <button class="btn-ghost btn-xs" onclick="$('memQuery').value='';loadMemory()">重置</button>
      </h3>
      <div class="steps" id="memList" style="max-height:420px"><div class="empty">暂无</div></div>
    </div>
  </div>
</div>

<div class="banner" id="approvalBanner">
  <b>⚠ 工具审批请求</b>：<span id="approvalText"></span>
  <div style="margin-top:9px;display:flex;gap:8px;align-items:center">
    <button class="btn-primary" style="width:auto;margin:0;padding:7px 16px" onclick="decide(true)">批准</button>
    <button class="btn-danger" style="padding:7px 16px" onclick="decide(false)">拒绝</button>
    <span class="muted" style="font-size:11.5px">30 秒不操作自动放行</span>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let polling = null;
let lastBody = null;
let lastStatus = '';
const esc = (s) => (s || "").replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ---------- 主题 ---------- */
function toggleTheme() {
  const root = document.documentElement;
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  root.dataset.theme = next;
  localStorage.setItem('miniagent-theme', next);
}
(function initTheme() {
  const saved = localStorage.getItem('miniagent-theme');
  if (saved) document.documentElement.dataset.theme = saved;
})();

/* ---------- 表单 ---------- */
function pickExample() {
  const v = $('example').value;
  if (v) $('task').value = v;
}

function toggleAuto() {
  const auto = $('autoParams').checked;
  $('budget').disabled = auto;
  $('maxSteps').disabled = auto;
}

async function runTask() {
  await submit({
    task: $('task').value.trim(),
    policy: $('policy').value,
    budget: parseInt($('budget').value || '500', 10),
    approval_mode: $('approval').value,
    max_steps: parseInt($('maxSteps').value || '20', 10),
    auto_params: $('autoParams').checked,
  });
}

async function retryLast() {
  if (!lastBody) { return; }
  await submit(lastBody);
}

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

/* ---------- 记忆库 ---------- */
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
    <div class="mem">
      <div class="head">#${it.id} · ${esc(it.kind || '')} · 会话 ${esc(it.session_id || '-')} · ${fmtTime(it.created_at)}
        <span class="spacer"></span>
        <button class="btn-danger btn-xs" onclick="deleteMemory(${it.id})">删除</button>
      </div>
      <div class="text">${esc(it.text)}</div>
    </div>`).join('') + `<div class="faint" style="margin-top:6px">共 ${data.total} 条${q ? `（匹配「${esc(q)}」）` : ''}</div>`
    : '<div class="empty">没有匹配的记忆</div>';
}

async function deleteMemory(id) {
  await fetch('/api/memory_delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
  loadMemory();
}

/* ---------- 轮询与渲染 ---------- */
function startPolling() {
  if (polling) return;
  polling = setInterval(refresh, 800);
  refresh();
}

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
  pill.className = 'chip state' + (s.status === 'done' ? ' done' : (s.status === 'error' ? ' error' : (s.status === 'running' ? ' running' : '')));
  $('elapsed').innerHTML = s.elapsed ? `耗时 <b>${s.elapsed}s</b>` : '';
  $('progress').className = s.status === 'running' ? 'on' : '';

  const llm = s.llm || {};
  $('chipLlm').innerHTML = llm.calls ? `LLM <b>${llm.calls}</b> 次` : '';
  $('chipTok').innerHTML = llm.prompt_tokens ? `tokens <b>${llm.prompt_tokens}</b>/<b>${llm.completion_tokens}</b>` : '';

  // 运行参数（模型自动判断 / 手动）
  const plan = s.auto_plan || {};
  if (s.auto_params) {
    $('autoPlan').innerHTML = plan.category
      ? `<div class="kv"><span>类别（模型判断）</span><b>${esc(plan.category)}</b></div>
         <div class="kv"><span>预算 / 最大步数</span><b>${plan.budget} 字符 / ${plan.max_steps} 步</b></div>
         <div class="muted" style="margin-top:6px;font-size:12px">理由：${esc(plan.reason || '-')}</div>
         <div class="faint" style="margin-top:3px;font-size:11px">来源：${plan.source === 'llm' ? '模型估计（1 次额外调用）' : '规则兜底'}</div>`
      : '<span class="muted">模型估计中…</span>';
  } else {
    $('autoPlan').innerHTML = `<div class="kv"><span>预算 / 最大步数（手动）</span><b>${s.budget || '-'} 字符 / ${s.max_steps || '-'} 步</b></div>`;
  }

  const sel = s.selection || {};
  $('selection').innerHTML = sel.total_messages
    ? `<div class="kv"><span>策略 / 预算</span><b>${esc(sel.policy)} / ${sel.budget_chars} 字符</b></div>
       <div class="kv"><span>消息数（选中/总）</span><b>${sel.selected_messages} / ${sel.total_messages}</b></div>
       <div class="kv"><span>字符（选中/总）</span><b>${sel.selected_chars} / ${sel.total_chars}</b></div>`
    : '<span class="muted">暂无</span>';

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
    ltmHtml += `<div class="muted" style="margin-top:9px;font-size:11.5px">📖 本轮注入 ${retrieved.length} 条历史记忆</div>`;
    ltmHtml += retrieved.map(h => `<div class="mem" style="margin-top:6px">
        <div class="head"><span class="score">相关度 ${h.score == null ? '-' : h.score}</span>
          <span>${esc(h.kind || '')}</span><span class="spacer"></span><span>${esc(h.session_id || '-')}</span></div>
        <div class="text">${esc(h.text)}</div></div>`).join('');
  }
  if (ltm.length) ltmHtml += `<div class="faint" style="margin-top:7px;font-size:11px">记忆库共 ${ltm.length} 条（右下可浏览/删除）</div>`;
  $('ltm').innerHTML = ltmHtml || '<span class="muted">暂无</span>';

  const banner = $('approvalBanner');
  if (s.pending) {
    banner.style.display = 'block';
    $('approvalText').textContent = `${s.pending.tool}(${JSON.stringify(s.pending.args)})`;
  } else { banner.style.display = 'none'; }

  const steps = s.messages || [];
  $('steps').innerHTML = steps.length ? steps.map(m => {
    const who = {user:'🧑 用户', assistant:'🤖 模型', tool:'🔧 工具结果', system:'📌 系统（长期记忆）'}[m.role] || m.role;
    const calls = (m.tool_calls && m.tool_calls.length)
      ? m.tool_calls.map(n => `<span class="toolchip">→ ${esc(n)}</span>`).join('') : '';
    const content = m.content ? esc(m.content) : '<span class="faint">(空)</span>';
    return `<div class="msg ${m.role}"><div class="who">${who}</div>${calls}<div class="pre">${content}</div></div>`;
  }).join('') : '<div class="empty">暂无</div>';

  const trace = s.trace || [];
  $('trace').innerHTML = trace.length ? trace.slice().reverse().map(e => {
    const meta = { llm: 'LLM', tool: '工具', approval: '审批', memory_read: '记忆读', memory_write: '记忆写', auto_plan: '参数估计' }[e.event] || e.event;
    const cls = e.event === 'llm' ? 'llm' : (e.event === 'tool' ? 'tool' : (e.event === 'approval' ? 'approval' : (e.event.startsWith('memory') ? 'memory' : '')));
    let detail = '';
    if (e.event === 'llm') detail = `tokens ${e.prompt_tokens}/${e.completion_tokens} · ${e.latency}s`;
    else if (e.event === 'tool') detail = `${e.name} ${e.success ? '✓' : '✗'}`;
    else if (e.event === 'approval') detail = `${e.tool} ${e.approved ? '放行' : '拒绝'}`;
    else if (e.event === 'memory_read') detail = `命中 ${(e.hits || []).length} 条`;
    else if (e.event === 'memory_write') detail = String(e.text || '').slice(0, 48);
    else if (e.event === 'auto_plan') detail = `${e.category} · 预算 ${e.budget} · 步数 ${e.max_steps}`;
    return `<div class="tl"><span class="dot ${cls}"></span><span>${meta}</span><span class="meta">${esc(detail)}</span></div>`;
  }).join('') : '<div class="empty">暂无</div>';

  $('output').innerHTML = s.error
    ? `<span style="color:var(--err)">错误: ${esc(s.error)}</span>`
    : (s.output
        ? (s.output.includes('LLM调用失败')
            ? `<span style="color:var(--err)">⚠ 接口异常：${esc(s.output)}<br>可点上方橙色「重试上次任务」</span>`
            : esc(s.output))
        : '<span class="muted">暂无</span>');
  $('retryBtn').style.display = (s.status === 'done' && s.output && s.output.includes('LLM调用失败')) ? 'block' : 'none';
}

toggleAuto();
refresh();
loadMemory();
</script>
</body></html>"""
