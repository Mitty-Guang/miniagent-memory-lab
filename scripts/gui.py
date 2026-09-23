"""交互式可视化 GUI：http://127.0.0.1:8901

功能：
- 输入任务，实时观看 ReAct 轨迹（推理 → 工具调用 → 观察结果）；
- 选择记忆策略（recent / relevance / impact）与上下文预算，实时看“本次选择”统计；
- **运行参数自动选择**：勾选后由模型（1 次轻量调用）按任务复杂度给出预算 / 最大步数，
  失败自动回退关键词规则（见 mini_agent/auto_budget.py）；
- 工具审批（HITL）：自动放行 / 仅 bash 需审批 / 全部需审批，页面上点“批准/拒绝”
  （30 秒不操作自动放行）；
- 实时面板：上下文选择、token 统计、工具调用、长期记忆读写、最终结果；
- 记忆查看：本轮注入的历史记忆（含相关度分数）+ 记忆库浏览器（搜索 / 逐条删除 / 清空）。

页面（HTML/CSS/JS）在 scripts/gui_page.py，便于单独美化；本文件只负责服务与运行逻辑。
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

from mini_agent.auto_budget import estimate
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
    def __init__(self, task, policy, budget, approval_mode, max_steps, auto_params=False):
        self.task = task
        self.policy = policy
        self.budget = budget
        self.approval_mode = approval_mode
        self.max_steps = max_steps
        self.auto_params = auto_params
        self.auto_plan = {}
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
            "max_steps": self.max_steps,
            "auto_params": self.auto_params,
            "auto_plan": self.auto_plan,
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
                        "category",
                        "budget",
                        "max_steps",
                        "reason",
                        "source",
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
            # 模型自动选择运行参数（预算 / 最大步数）：1 次轻量调用，失败自动回退规则
            if state.auto_params:
                plan = await estimate(state.task, state.llm)
                state.auto_plan = plan
                state.budget = int(plan["budget"])
                state.max_steps = int(plan["max_steps"])
                state.trace.log(
                    "auto_plan",
                    {
                        "category": plan["category"],
                        "budget": plan["budget"],
                        "max_steps": plan["max_steps"],
                        "reason": plan["reason"],
                        "source": plan["source"],
                    },
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


from gui_page import PAGE  # noqa: E402  （页面 HTML/CSS/JS 在 scripts/gui_page.py）


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
                auto_params=bool(payload.get("auto_params")),
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
