"""交互式可视化 GUI：http://127.0.0.1:8901

功能：
- 输入任务，实时观看 ReAct 轨迹（推理 → 工具调用 → 观察结果）；
- 选择记忆策略（recent / relevance）与上下文预算，实时看“本次选择”统计；
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
import collections
import contextlib
import io
import json
import os
import secrets
import socket
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
STATE = {"run": None, "chat": None, "lg_chat": None}   # chat: 连续对话会话（复用同一 agent，短时记忆延续）
PORT = 8901
_EMBED_RETRIEVER = None   # 惰性单例：EmbeddingRetriever / False（不可用）
LOG_BUFFER: "collections.deque" = collections.deque(maxlen=4000)   # 进程日志环形缓冲
TOKEN = ""   # 局域网模式下启用访问口令（--host 非本机时自动生成，可用 GUI_TOKEN 指定）
RUN_LOG_DIR = HERE.parent / "results" / "logs"


class _Tee:
    """把写到 stdout/stderr 的内容同时复制进环形缓冲（供 GUI"查看日志"入口）。"""

    def __init__(self, stream, buffer):
        self._stream = stream
        self._buffer = buffer

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        for line in str(data).splitlines():
            if line.strip():
                self._buffer.append(line)

    def flush(self):
        with contextlib.suppress(Exception):
            self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def ltm_path() -> Path:
    return HERE.parent / "results" / "gui_ltm.sqlite3"


class _RunLog(io.StringIO):
    """运行期间的 stdout：既收集（面板/文件），也并发进进程日志缓冲。"""

    def write(self, data):
        for line in str(data).splitlines():
            if line.strip():
                LOG_BUFFER.append(line)
        return super().write(data)


def _lan_ips():
    """枚举本机局域网 IPv4（用于打印访问地址）。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def _save_run_log(state, text: str) -> None:
    """保存本轮 stdout 日志：面板可见 + 落盘到 results/logs/。"""
    state.agent_log = text[-20000:]
    try:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_LOG_DIR / f"run_{int(state.started_at)}.log"
        path.write_text(text[-200_000:], encoding="utf-8")
        state.run_log_path = str(path)
    except Exception:
        pass


def ltm_retriever():
    """长期记忆检索器：优先 embedding（跨会话命中更好），不可用回退 TF-IDF（默认）。"""
    global _EMBED_RETRIEVER
    if _EMBED_RETRIEVER is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")   # 国内镜像
        try:
            from mini_agent.retrieval import EmbeddingRetriever

            _EMBED_RETRIEVER = EmbeddingRetriever()
            print("[ltm] 检索器：EmbeddingRetriever（bge-small-zh）", flush=True)
        except Exception as exc:
            print(f"[ltm] EmbeddingRetriever 不可用（{exc}）；回退 TF-IDF", flush=True)
            _EMBED_RETRIEVER = False
    return _EMBED_RETRIEVER or None


def make_ltm(path: Path) -> LongTermMemory:
    """打开（或创建）长期记忆库：与手写 / LangGraph 两条运行时路径共用。"""
    path.parent.mkdir(exist_ok=True)
    return LongTermMemory(path=str(path), retriever=ltm_retriever())


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
    def __init__(
        self, task, policy, budget, approval_mode, max_steps, auto_params=False, chat=False,
        runtime="handwritten",
    ):
        self.task = task
        self.policy = policy
        self.budget = budget
        self.approval_mode = approval_mode
        self.max_steps = max_steps
        self.auto_params = auto_params
        self.auto_plan = {}
        self.runtime = runtime        # handwritten（零依赖主链路）| langgraph（extras 子进程）
        self.langgraph_payload = {}
        self.lh_active = False        # 长周期任务（Harness 模式）
        self.lh_task_id = ""
        self.lh_progress = []
        self.lh_checks = []
        self.lg_messages = []         # LangGraph 运行时：流式消息（同进程/子进程通用）
        self.lg_retrieved = []        # LangGraph 运行时：注入的长期记忆
        self.lg_mode = ""             # in-process / subprocess
        self.stop_requested = False   # 用户点了"中止"
        self.stopped = False          # 实际已中止
        self._proc = None             # LangGraph 子进程句柄（中止时 terminate）
        self.agent_log = ""           # 本轮运行日志（stdout 捕获）
        self.run_log_path = ""        # 落盘的日志文件路径
        self.chat = chat            # 连续对话：复用会话上下文
        self.turns = 0              # 当前会话第几轮
        self.session_id = ""
        self.answer = ""            # 本轮最终回答（无工具调用的最后一条助手消息）
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

    def request_stop(self) -> None:
        """用户点"中止"：置标志（协作式中止），并放行可能正在等待的审批，避免卡住。"""
        self.stop_requested = True
        self._approval_event.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()   # LangGraph 子进程路径：直接终止
            except Exception:
                pass

    def request_approval(self, tool: str, args: dict) -> bool:
        """LangGraph 运行时用：按审批模式决定是否需要人工介入（复用 pending / approve 机制）。

        与手写路径 approval_fn 的门控口径一致：
        - auto：全部自动放行；- bash：仅 bash_execute 需审批；- all：全部需审批。
        """
        gated = self.approval_mode == "all" or (
            self.approval_mode == "bash" and tool == "bash_execute"
        )
        if not gated:
            self.trace.log("approval", {"tool": tool, "approved": True, "text": "自动放行"})
            return True
        return self.approval_fn(tool, args)

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
            "chat": self.chat,
            "turns": self.turns,
            "session_id": self.session_id,
            "answer": self.answer[:4000],
            "approval_mode": self.approval_mode,
            "runtime": self.runtime,
            "langgraph_available": langgraph_python() is not None,
            "lg_mode": self.lg_mode,
            "langgraph_payload": self.langgraph_payload,
            "lh_active": self.lh_active,
            "lh_task_id": self.lh_task_id,
            "lh_progress": self.lh_progress[-40:],
            "lh_checks": self.lh_checks,
            "stopped": self.stopped,
            "stop_requested": self.stop_requested,
            "run_log_path": self.run_log_path,
            "agent_log": self.agent_log[-6000:],
            "lg_retrieved": [
                {
                    "text": (h.get("text") or "")[:160],
                    "score": h.get("score"),
                    "kind": h.get("kind"),
                    "session_id": h.get("session_id"),
                }
                for h in (self.lg_retrieved or [])
            ],
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
                for msg in self.agent.memory.messages[-80:]   # 连续对话下只回传最近 80 条
            ]
        elif self.lg_messages or self.langgraph_payload:
            # LangGraph 运行时的轨迹：优先用流式消息（边跑边显示），结束后用完整版覆盖
            data["messages"] = (self.lg_messages or self.langgraph_payload.get("messages", []))[-80:]
        return data


def last_answer(agent) -> str:
    """取最近一条"无工具调用"的助手消息作为本轮回答（连续对话时用于展示）。"""
    messages = getattr(getattr(agent, "memory", None), "messages", []) or []
    for message in reversed(messages):
        role = getattr(message.role, "value", message.role)
        if role == "assistant" and message.content and not message.tool_calls:
            return str(message.content)
    return ""


def langgraph_python():
    """extras 的 LangGraph 运行时解释器（Python ≥3.11，interrupt 需要）；未安装返回 None。"""
    candidate = HERE.parent / ".venv312" / "Scripts" / "python.exe"
    return candidate if candidate.exists() else None


def _lg_import():
    """惰性导入 extras 的 LangGraph 图模块（同进程运行用；缺依赖返回 None）。"""
    try:
        import importlib

        extras = HERE.parent / "extras" / "langgraph_compare"
        if str(extras) not in sys.path:
            sys.path.insert(0, str(extras))
        return importlib.import_module("multi_agent_graph")
    except Exception as exc:
        print(f"[lg] 同进程导入失败（将回退子进程）: {exc}", flush=True)
        return None


async def _lg_close_llms() -> None:
    """关闭本轮 LangGraph 用到的 LLM 客户端（httpx 连接绑定事件循环，跨轮复用会报 Event loop is closed）。"""
    try:
        import importlib

        extras = HERE.parent / "extras" / "langgraph_compare"
        if str(extras) not in sys.path:
            sys.path.insert(0, str(extras))
        llm_factory = importlib.import_module("llm_factory")
        await llm_factory.close_created_llms()
    except Exception:
        pass


async def lg_checkpointer():
    """新建一个 LangGraph 检查点（同文件、跨轮/跨进程持久化）。

    注意：不能做成模块级单例——GUI 每次 run 都新建事件循环，而 aiosqlite/锁会绑定循环，
    复用单例会报 "is bound to a different event loop"。每次新建、指向同一文件即可：
    多轮状态由 thread_id + SQLite 文件保证。
    """
    lag = _lg_import()
    if lag is None:
        raise RuntimeError("当前环境缺少 LangGraph/LangChain")
    path = HERE.parent / "results" / "gui_lg_checkpoints.sqlite"
    path.parent.mkdir(exist_ok=True)
    return await lag.make_checkpointer(str(path))


def _lg_serialize(message) -> dict:
    """把 LangChain 消息转成 GUI 轨迹格式（与环境无关）。"""
    kind = type(message).__name__
    role = {"HumanMessage": "user", "ToolMessage": "tool", "SystemMessage": "system"}.get(kind, "assistant")
    return {
        "role": role,
        "content": str(getattr(message, "content", "") or "")[:1500],
        "tool_calls": [call.get("name") for call in (getattr(message, "tool_calls", None) or [])],
    }


async def run_langgraph_inprocess(state: RunState, ltm, thread_id: str = "") -> dict:
    """同进程跑 LangGraph 团队图：真·实时流式 + 共享记忆库 + **interrupt 人工审批**。

    interrupt 桥接：图在 approval / 敏感工具处暂停时，chunk 里会出现 `__interrupt__`；
    这里把它转成 GUI 的审批请求（按 approval_mode 门控），拿到决定后用
    `Command(resume={"approved": ...})` 恢复图（LangGraph 官方审批语义）。
    """
    lag = _lg_import()
    if lag is None:
        raise RuntimeError("当前环境缺少 LangGraph/LangChain")
    from langgraph.types import Command   # 仅同进程路径需要（Python ≥3.11）

    graph = lag.build_team(
        checkpointer=await lg_checkpointer(),
        max_retries=1,
        max_steps=state.max_steps,
        require_approval=True,                  # 计划审批（按 mode 决定是否需要人工点）
        sensitive_tools=["bash_execute"],       # 工具级审批（仅 bash 需审批时生效）
        ltm=ltm,
        session_id=state.session_id,
        retrieved_sink=state.lg_retrieved,
    )
    config = {"configurable": {"thread_id": thread_id or f"gui-{int(state.started_at)}"}}
    started = time.time()
    next_input: object = {"task": state.task, "rounds": 0}

    while True:
        if state.stop_requested:
            state.stopped = True
            break
        resume_payload = None
        async for chunk in graph.astream(next_input, config=config, stream_mode="updates"):
            if state.stop_requested:
                state.stopped = True
                break
            interrupts = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
            if interrupts:
                value = getattr(interrupts[0], "value", {}) or {}
                label = str(value.get("tool") or "计划审批")
                raw_args = value.get("args")
                args = (
                    raw_args
                    if isinstance(raw_args, dict)
                    else {k: str(v)[:120] for k, v in value.items() if k not in {"ask"}}
                )
                approved = await asyncio.to_thread(state.request_approval, label, args or {})
                state.trace.log("approval", {"tool": label, "approved": approved})
                resume_payload = {"approved": approved}
                continue
            for node, update in (chunk or {}).items():
                detail = str(node)
                if isinstance(update, dict):
                    for message in update.get("messages") or []:
                        state.lg_messages.append(_lg_serialize(message))
                    if update.get("rounds") is not None:
                        detail += f" · rounds={update['rounds']}"
                    if update.get("steps") is not None:
                        detail += f" · steps={update['steps']}"
                state.trace.log(
                    "lg_node",
                    {"name": str(node), "text": detail, "latency": round(time.time() - started, 1)},
                )
        snapshot = await graph.aget_state(config)
        if not snapshot.next:      # 图已结束
            break
        next_input = Command(resume=resume_payload or {"approved": True})

    # 关闭本次的 aiosqlite 连接（否则下次 run 的新事件循环会撞上旧连接 → "Event loop is closed"）
    with contextlib.suppress(Exception):
        conn = getattr(checkpointer, "conn", None)
        if conn is not None:
            await conn.close()

    values = dict(snapshot.values) if snapshot else {}
    return {
        "final": str(values.get("final") or ""),
        "plan": str(values.get("plan") or ""),
        "review": str(values.get("review") or ""),
        "rounds": int(values.get("rounds") or 0),
        "seconds": round(time.time() - started, 2),
        "log": list(values.get("log") or []),
        "messages": [_lg_serialize(m) for m in (values.get("messages") or [])],
    }


async def run_langgraph_runtime(state: RunState, ltm_path_value: str = "", thread_id: str = "") -> dict:
    """以子进程方式跑 LangGraph 团队图，并**流式**读取节点进展（GUI 实时渲染）。

    gui_run.py 每完成一个节点就输出一行 JSON；这里逐行读取：
    - type=node：把新增消息追加到 state.lg_messages（对话面板实时显示）、
      并写一条 trace 事件（事件流实时显示节点名/轮次）；
    - type=final：完整载荷（含 final/plan/review/log/全部消息）。
    """
    python = langgraph_python()
    if python is None:
        raise RuntimeError("未找到 .venv312：请先创建 Python≥3.11 环境并安装 extras 依赖（见 docs/LANGGRAPH_NOTES.md）")
    script = HERE.parent / "extras" / "langgraph_compare" / "gui_run.py"
    proc = await asyncio.create_subprocess_exec(
        str(python),
        str(script),
        "--task",
        state.task,
        "--max-steps",
        str(state.max_steps),
        *(["--ltm-path", ltm_path_value] if ltm_path_value else []),
        *(["--thread-id", thread_id] if thread_id else []),
        *(["--checkpoint-path", str(HERE.parent / "results" / "gui_lg_checkpoints.sqlite")] if thread_id else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(HERE.parent),
    )

    payload: dict = {}
    stderr_chunks: list = []
    state._proc = proc            # 供"中止"按钮 terminate
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "node":
            node = str(event.get("node") or "?")
            for message in event.get("messages") or []:
                state.lg_messages.append(message)
            detail = node
            if event.get("rounds") is not None:
                detail += f" · rounds={event['rounds']}"
            if event.get("steps") is not None:
                detail += f" · steps={event['steps']}"
            state.trace.log("lg_node", {"name": node, "text": detail, "latency": event.get("seconds")})
        else:
            payload = event
            for message in event.get("messages") or []:
                state.lg_messages.append(message)

    if proc.stderr is not None:
        stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace")
        stderr_chunks.append(stderr_text)
        for line in stderr_text.splitlines():
            if line.strip():
                LOG_BUFFER.append(f"[lg-subprocess] {line}")
    await proc.wait()

    if not payload:
        if state.stop_requested:      # 用户点了中止：子进程被 terminate，按"已中止"收尾
            state.stopped = True
            state.agent_log = ("".join(stderr_chunks))[-20000:]
            return {"final": "⏹ 已被用户中止（LangGraph 子进程已终止）。", "messages": state.lg_messages,
                    "rounds": 0, "seconds": 0}
        detail = ("".join(stderr_chunks) or "无输出").strip()[-200:]
        raise RuntimeError(f"LangGraph 子进程无有效输出：{detail}")
    _save_run_log(state, "".join(stderr_chunks))    # 子进程 stderr（含 [ltm]/报错）作为本轮日志
    if payload.get("messages"):
        state.lg_messages = list(payload["messages"])   # 用完整版覆盖流式截断版
    if payload.get("retrieved"):
        state.lg_retrieved = list(payload["retrieved"])
    return payload


def _lh_module():
    """惰性导入长周期 runner（scripts/run_long_horizon.py）。"""
    sys.path.insert(0, str(HERE))
    import run_long_horizon as lh

    return lh


def load_lh_tasks() -> list:
    try:
        tasks = _lh_module().load_tasks()
    except Exception as exc:
        print(f"[lh] 任务加载失败: {exc}", flush=True)
        return []
    return [
        {
            "id": t["id"],
            "family": t.get("family", ""),
            "level": t.get("level", ""),
            "needs_web": bool(t.get("needs_web")),
            "units": len(t.get("turns") or t.get("phases") or []),
        }
        for t in tasks
    ]


def run_lh_worker(state: RunState, task: dict) -> None:
    """长周期任务：直接复用 harness（预置文件 + 多轮/跨会话 + 确定性判分），进度回填面板。"""

    async def worker():
        try:
            lh = _lh_module()
            state.trace.log("runtime", {"name": "long-horizon", "text": f"task={task['id']}"})

            def progress(line: str) -> None:
                state.lh_progress.append(line)
                if len(state.lh_progress) > 200:
                    state.lh_progress = state.lh_progress[-200:]

            row = await lh.run_task(
                task, state.policy, state.budget, state.max_steps,
                progress=progress, stop_check=lambda: state.stop_requested,
            )
            state.stopped = bool(row.get("stopped"))
            state.lh_checks = list(row.get("checks") or [])
            marks = " / ".join("✅" if c else "❌" for c in state.lh_checks)
            state.output = (
                f"长周期任务：{row['task_id']}（{row['family']} / {row['level']}）\n"
                f"结果：{'✅ 通过' if row['success'] else '❌ 未通过'}（逐轮判分：{marks}）\n"
                f"步数 {row['steps']} ｜ LLM 调用 {row['llm_calls']} ｜ {row['seconds']}s\n"
                f"工作目录：{row['workdir']}\n\n最终回答：\n{row['final_answer']}"
            )
            state.answer = state.output
            state.trace.log(
                "tool",
                {"name": f"lh:{row['task_id']}", "success": bool(row["success"])},
            )
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.status = "error" if state.error else "done"
            state.finished_at = time.time()

    asyncio.run(worker())


def run_worker(state: RunState):
    async def worker():
        workdir = tempfile.mkdtemp(prefix="gui_run_")
        old_cwd = os.getcwd()
        os.chdir(workdir)
        try:
            with STATE_LOCK:
                session = STATE.get("chat")
            reuse = bool(state.chat and session and session.get("agent"))

            # —— LangGraph 运行时：优先同进程（实时流式 + 共享记忆库），缺依赖回退 .venv312 子进程 ——
            if state.runtime == "langgraph":
                with STATE_LOCK:
                    lg_session = STATE.get("lg_chat")
                if state.chat and lg_session:
                    thread_id = lg_session["thread_id"]
                    state.turns = int(lg_session.get("turns", 0)) + 1
                else:
                    thread_id = f"gui-lg-{int(state.started_at)}"
                    state.turns = 1
                state.session_id = thread_id
                ltm = make_ltm(ltm_path())
                workdir = tempfile.mkdtemp(prefix="gui_lg_")   # 独立工作目录：避免产物写进仓库
                old_cwd = os.getcwd()
                os.chdir(workdir)
                try:
                    # 默认子进程（每轮全新进程，无跨事件循环的资源复用问题）；
                    # 需要"interrupt 审批桥接到 GUI 按钮"时设 GUI_LG_INPROCESS=1 走同进程。
                    use_inprocess = os.getenv("GUI_LG_INPROCESS", "").strip() == "1" and _lg_import() is not None
                    if use_inprocess:
                        state.lg_mode = "in-process"
                        state.trace.log("lg_node", {"name": "runtime", "text": "同进程（共享记忆库）"})
                        log_io = _RunLog()
                        with contextlib.redirect_stdout(log_io):
                            payload = await run_langgraph_inprocess(state, ltm, thread_id)
                        _save_run_log(state, log_io.getvalue())
                    else:
                        state.lg_mode = "subprocess"
                        state.trace.log("lg_node", {"name": "runtime", "text": "子进程 .venv312"})
                        payload = await run_langgraph_runtime(state, str(ltm_path()), thread_id)
                    if state.chat:
                        with STATE_LOCK:
                            STATE["lg_chat"] = {"thread_id": thread_id, "turns": state.turns}
                    else:
                        with STATE_LOCK:
                            STATE["lg_chat"] = None
                    state.ltm_rows = ltm.all()
                finally:
                    with contextlib.suppress(Exception):
                        ltm.close()
                    with contextlib.suppress(Exception):
                        os.chdir(old_cwd)
                    await _lg_close_llms()      # 关闭本轮 LangGraph 用的 LLM 客户端（防跨循环泄漏）
                state.langgraph_payload = payload
                state.lg_retrieved = list(state.lg_retrieved)
                state.output = payload.get("final") or payload.get("error") or "(无输出)"
                state.answer = state.output
                state.trace.log(
                    "runtime",
                    {
                        "name": f"langgraph/{state.lg_mode}",
                        "text": f"rounds={payload.get('rounds')} review={str(payload.get('review'))[:24]}",
                        "latency": payload.get("seconds"),
                    },
                )
                return

            if reuse:
                # 连续对话：复用 agent 的短时记忆（上下文延续）。
                # 注意：LLM 客户端与 SQLite 连接都绑定线程/事件循环，不能跨轮复用，
                # 因此每轮在当前线程重建，轮末同线程关闭（见 finally）。
                state.agent = session["agent"]
                state.session_id = session["session_id"]
                state.turns = int(session.get("turns", 0)) + 1
            else:
                state.session_id = f"gui-{int(state.started_at)}"
                state.turns = 1

            state.llm = CountingLLM(
                **llm_kwargs(), trace=state.trace, max_retries=8, base_delay=3.0
            )
            ltm = make_ltm(ltm_path())

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

            if reuse:
                # 每轮可改策略/预算/步数（策略与预算作用在增长的对话历史上）
                state.agent.llm = state.llm
                state.agent.ltm = ltm
                state.agent.memory.policy = state.policy
                state.agent.memory.budget_chars = state.budget
                state.agent.max_steps = state.max_steps
            else:
                state.agent = MemoryAgent(
                    llm=state.llm,
                    ltm=ltm,
                    session_id=state.session_id,
                    policy=state.policy,
                    budget_chars=state.budget,
                    approval_fn=state.approval_fn,
                    trace=state.trace,
                    max_steps=state.max_steps,
                    stop_check=lambda: state.stop_requested,
                )

            log_io = _RunLog()
            with contextlib.redirect_stdout(log_io):
                state.output = await state.agent.run(state.task)
            _save_run_log(state, log_io.getvalue())
            state.answer = last_answer(state.agent) or state.output
            if getattr(state.agent, "stopped", False):
                state.stopped = True
                state.output = f"⏹ 已被用户中止（完成 {state.agent.current_step} 步）。\n\n" + state.output
            state.ltm_rows = ltm.all()

            if state.chat:
                base = session if reuse else {}
                with STATE_LOCK:
                    STATE["chat"] = {
                        "agent": state.agent,
                        "session_id": state.session_id,
                        "turns": state.turns,
                        "created_at": base.get("created_at", time.time()),
                        "calls": int(base.get("calls", 0)) + state.llm.calls,
                        "prompt_tokens": int(base.get("prompt_tokens", 0))
                        + state.llm.prompt_tokens,
                        "completion_tokens": int(base.get("completion_tokens", 0))
                        + state.llm.completion_tokens,
                    }
        except Exception as exc:
            state.error = str(exc)
        finally:
            # LLM 客户端与 SQLite 连接都在本线程创建，必须在本线程关闭
            with contextlib.suppress(Exception):
                if state.llm is not None:
                    await state.llm.client.close()
            with contextlib.suppress(Exception):
                if state.agent is not None and state.agent.ltm is not None:
                    state.agent.ltm.close()
            if not state.chat:
                with STATE_LOCK:
                    STATE["chat"] = None
            os.chdir(old_cwd)
            state.status = "error" if state.error else "done"
            state.finished_at = time.time()

    asyncio.run(worker())


from gui_page import PAGE  # noqa: E402  （页面 HTML/CSS/JS 在 scripts/gui_page.py）


class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        """局域网模式下校验访问口令（header X-GUI-Token 或 ?token=）。"""
        if not TOKEN:
            return True
        if (self.headers.get("X-GUI-Token") or "") == TOKEN:
            return True
        if "?" in self.path:
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            return (query.get("token") or [""])[0] == TOKEN
        return False

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/") and not self._authorized():
            self._send(401, b'{"ok":false,"error":"unauthorized: need ?token= or X-GUI-Token header"}')
            return
        if self.path.startswith("/api/state"):
            with STATE_LOCK:
                run = STATE["run"]
                session = STATE.get("chat") or {}
                data = run.to_dict() if run else {
                    "status": "idle",
                    "chat": bool(session),
                    "turns": session.get("turns", 0),
                    "session_id": session.get("session_id", ""),
                    "langgraph_available": langgraph_python() is not None,
                }
                if isinstance(data, dict) and data.get("chat"):
                    data["session_totals"] = {
                        "turns": session.get("turns", 0),
                        "calls": session.get("calls", 0),
                        "prompt_tokens": session.get("prompt_tokens", 0),
                        "completion_tokens": session.get("completion_tokens", 0),
                    }
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
        if self.path.startswith("/api/logs"):
            with STATE_LOCK:
                run = STATE["run"]
            payload = {
                "run": (run.agent_log[-8000:] if run else ""),
                "run_log_path": (run.run_log_path if run else ""),
                "process": list(LOG_BUFFER)[-500:],
            }
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return
        if self.path.startswith("/api/lh_tasks"):
            self._send(200, json.dumps({"tasks": load_lh_tasks()}, ensure_ascii=False).encode("utf-8"))
            return
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._authorized():
            self._send(401, b'{"ok":false,"error":"unauthorized: need ?token= or X-GUI-Token header"}')
            return
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
                chat=bool(payload.get("chat")),
                runtime=(payload.get("runtime") or "handwritten"),
            )
            with STATE_LOCK:
                STATE["run"] = run
            threading.Thread(target=run_worker, args=(run,), daemon=True).start()
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/new_session"):
            with STATE_LOCK:
                session = STATE.get("chat")
                STATE["chat"] = None
                STATE["lg_chat"] = None
                STATE["run"] = None
            if session:
                with contextlib.suppress(Exception):
                    asyncio.run(session["llm"].client.close())
                with contextlib.suppress(Exception):
                    session["agent"].ltm.close()
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/lh_run"):
            task_id = (payload.get("task_id") or "").strip()
            try:
                tasks = _lh_module().load_tasks()
            except Exception as exc:
                self._send(400, json.dumps({"ok": False, "error": f"任务加载失败: {exc}"}).encode("utf-8"))
                return
            task = next((t for t in tasks if t["id"] == task_id), None)
            if task is None:
                self._send(400, b'{"ok":false,"error":"unknown task_id"}')
                return
            run = RunState(
                task=task_id,
                policy=payload.get("policy", "relevance"),
                budget=int(payload.get("budget", task.get("budget", 1200))),
                approval_mode="auto",
                max_steps=int(payload.get("max_steps") or task.get("max_steps") or 14),
                auto_params=False,
                chat=False,
                runtime="long_horizon",
            )
            run.lh_active = True
            run.lh_task_id = task_id
            run.lh_progress = [f"任务已启动：{task_id}（{task.get('family')} / {task.get('level')}）"]
            with STATE_LOCK:
                STATE["run"] = run
            threading.Thread(target=run_lh_worker, args=(run, task), daemon=True).start()
            self._send(200, b'{"ok":true}')
            return

        if self.path.startswith("/api/stop"):
            with STATE_LOCK:
                run = STATE["run"]
            if run is not None:
                run.request_stop()
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
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址：127.0.0.1（默认，仅本机）/ 0.0.0.0（开放局域网，需口令）")
    args = parser.parse_args()
    PORT = args.port
    LAN_MODE = args.host not in {"127.0.0.1", "localhost"}
    TOKEN = (os.getenv("GUI_TOKEN") or "").strip()   # 可选：设置后才启用访问口令（默认无口令）

    # 启动时后台预热一次（规避冷启动/瞬时网络问题导致的首次调用失败）
    threading.Thread(
        target=lambda: asyncio.run(warmup_async(attempts=3, delay=2.0)), daemon=True
    ).start()

    server = ThreadingHTTPServer((args.host, PORT), Handler)
    # 把 stdout/stderr 复制进环形缓冲（"运行日志"面板可看进程日志/重试/traceback）
    sys.stdout = _Tee(sys.stdout, LOG_BUFFER)
    sys.stderr = _Tee(sys.stderr, LOG_BUFFER)
    print(f"[gui] http://127.0.0.1:{PORT} （Ctrl+C 停止）", flush=True)
    if LAN_MODE:
        for ip in _lan_ips():
            print(f"[gui] 局域网访问: http://{ip}:{PORT}/" + (f"?token={TOKEN}" if TOKEN else ""), flush=True)
        if TOKEN:
            print("[gui] 已启用访问口令（GUI_TOKEN）", flush=True)
        else:
            print("[gui] 已开放到局域网（无口令，适用于可信内网；如需口令请设 GUI_TOKEN=xxx 再启动）。"
                  "注意 /api/run 会驱动工具执行（bash/python）。", flush=True)
    server.serve_forever()
