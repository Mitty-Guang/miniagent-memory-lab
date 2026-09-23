"""GUI 的 LangGraph 运行时入口：流式输出节点进展（供 scripts/gui.py 以子进程调用）。

输出协议（stdout，每行一个 JSON，方便父进程边读边渲染）：
- {"type": "node", "node": "executor", "seconds": 3.1, "messages": [...], "rounds": 1, ...}
  —— 每个图节点执行完立刻输出一次（含该节点新增的消息、plan/review/rounds 等摘要）；
- {"type": "final", "final": "...", "plan": "...", "review": "...", "rounds": 2,
   "seconds": 86.4, "log": [...], "messages": [...]}  —— 最后一次输出，含完整状态。

设计说明：
- GUI 本体（3.10 venv）保持零框架依赖；本脚本在 **Python ≥3.11 的专用环境**（`.venv312`）执行；
- 除 JSON 行外的日志都走 stderr，避免污染协议；
- 审批中断关闭（require_approval=False、sensitive_tools=[]），与 GUI 默认"自动放行"一致。

用法：
    .venv312\\Scripts\\python.exe extras\\langgraph_compare\\gui_run.py --task "创建一个文件" [--max-steps 6]
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402

from multi_agent_graph import build_team, make_checkpointer  # noqa: E402


def role_of(message) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, ToolMessage):
        return "tool"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, SystemMessage):
        return "system"
    return "assistant"


def serialize(message) -> dict:
    return {
        "role": role_of(message),
        "content": str(getattr(message, "content", "") or "")[:1500],
        "tool_calls": [call.get("name") for call in (getattr(message, "tool_calls", None) or [])],
    }


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def run(task: str, max_steps: int, ltm_path: str = "") -> None:
    workdir = tempfile.mkdtemp(prefix="lg_gui_")
    os.chdir(workdir)
    started = time.time()
    ltm = None
    session_id = f"gui-lg-{int(started)}"
    if ltm_path:
        sys.path.insert(0, str(HERE.parents[1]))
        from mini_agent.long_term_memory import LongTermMemory

        Path(ltm_path).parent.mkdir(parents=True, exist_ok=True)
        ltm = LongTermMemory(path=ltm_path)
    retrieved: list = []
    graph = build_team(
        checkpointer=await make_checkpointer(),
        max_retries=1,
        max_steps=max_steps,
        require_approval=False,
        sensitive_tools=[],
        ltm=ltm,
        session_id=session_id,
        retrieved_sink=retrieved,
    )
    config = {"configurable": {"thread_id": f"gui-{int(started)}"}}

    # —— 流式：每个节点执行完输出一次（GUI 据此实时渲染轨迹）——
    async for chunk in graph.astream(
        {"task": task, "rounds": 0}, config=config, stream_mode="updates"
    ):
        for node, update in (chunk or {}).items():
            event: dict = {
                "type": "node",
                "node": node,
                "seconds": round(time.time() - started, 1),
            }
            if isinstance(update, dict):
                messages = update.get("messages")
                if messages:
                    event["messages"] = [serialize(m) for m in messages]
                if update.get("plan"):
                    event["plan"] = str(update["plan"])[:200]
                if update.get("review"):
                    event["review"] = str(update["review"])[:120]
                if update.get("rounds") is not None:
                    event["rounds"] = update["rounds"]
                if update.get("steps") is not None:
                    event["steps"] = update["steps"]
            emit(event)

    snapshot = await graph.aget_state(config)
    values = dict(snapshot.values) if snapshot else {}
    if ltm is not None:
        ltm.close()
    emit(
        {
            "type": "final",
            "final": str(values.get("final") or ""),
            "plan": str(values.get("plan") or ""),
            "review": str(values.get("review") or ""),
            "rounds": int(values.get("rounds") or 0),
            "seconds": round(time.time() - started, 2),
            "log": list(values.get("log") or []),
            "messages": [serialize(m) for m in (values.get("messages") or [])],
            "retrieved": [
                {"text": (h.get("text") or "")[:160], "score": h.get("score"), "kind": h.get("kind"),
                 "session_id": h.get("session_id")}
                for h in retrieved
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--ltm-path", default="", help="共享长期记忆库路径（与 GUI 同一个 SQLite）")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.task, args.max_steps, args.ltm_path))
    except Exception as exc:  # 错误也按协议输出，便于 GUI 展示
        emit({"type": "final", "final": "", "error": f"{type(exc).__name__}: {exc}"[:300], "messages": []})


if __name__ == "__main__":
    main()
