"""GUI 的 LangGraph 运行时入口：跑一个任务并输出单行 JSON（供 scripts/gui.py 以子进程调用）。

设计说明：
- GUI 本体（3.10 venv）保持零框架依赖；需要 LangGraph 时由本脚本在 **Python ≥3.11 的专用环境**
  （`.venv312`）里执行，避免把框架依赖混进核心链路；
- 只把结果 JSON 打到 stdout（其余日志走 stderr），便于父进程解析；
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


async def run(task: str, max_steps: int) -> dict:
    workdir = tempfile.mkdtemp(prefix="lg_gui_")
    os.chdir(workdir)
    started = time.time()
    graph = build_team(
        checkpointer=await make_checkpointer(),
        max_retries=1,
        max_steps=max_steps,
        require_approval=False,
        sensitive_tools=[],
    )
    state = await graph.ainvoke(
        {"task": task, "rounds": 0},
        config={"configurable": {"thread_id": f"gui-{int(started)}"}},
    )
    messages = []
    for message in state.get("messages") or []:
        messages.append(
            {
                "role": role_of(message),
                "content": str(getattr(message, "content", "") or "")[:1500],
                "tool_calls": [call.get("name") for call in (getattr(message, "tool_calls", None) or [])],
            }
        )
    return {
        "final": str(state.get("final") or ""),
        "plan": str(state.get("plan") or ""),
        "review": str(state.get("review") or ""),
        "rounds": int(state.get("rounds") or 0),
        "seconds": round(time.time() - started, 2),
        "log": list(state.get("log") or []),
        "messages": messages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-steps", type=int, default=6)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(args.task, args.max_steps))
    except Exception as exc:  # 把错误也作为 JSON 返回，便于 GUI 展示
        payload = {"final": "", "error": f"{type(exc).__name__}: {exc}"[:300], "messages": []}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
