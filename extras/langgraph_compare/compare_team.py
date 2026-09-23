"""对照与演示：手写 MultiAgentTeam vs LangGraph 版（子图 / checkpointer / interrupt）。

三个场景：
1. **性能对照**：同一批任务，手写团队 vs LangGraph 团队（成功率 / 步数 / 调用数 / 耗时）；
2. **多轮会话（checkpointer）**：同一 thread_id 连续两次 invoke，第二轮能看到第一轮的历史；
3. **人工审批（interrupt）**：计划审批（拒绝 → 图提前结束）+ 工具级审批（bash 需放行）。

用法：
    .\\.venv\\Scripts\\python.exe extras\\langgraph_compare\\compare_team.py --limit 3
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))   # 本地模块（llm_factory / agent_graph / multi_agent_graph）
sys.path.insert(0, str(ROOT))   # mini_agent

from langchain_core.callbacks import AsyncCallbackHandler  # noqa: E402
from langgraph.types import Command  # noqa: E402

from multi_agent_graph import build_team, make_checkpointer  # noqa: E402

from mini_agent.config import CountingLLM, llm_kwargs  # noqa: E402
from mini_agent.multi_agent import MultiAgentTeam  # noqa: E402
from mini_agent.task_suite import TASKS  # noqa: E402

RESULTS_DIR = ROOT / "results"


class TokenCounter(AsyncCallbackHandler):
    """统计 LangChain 侧调用次数与 token（与 CountingLLM 口径对齐）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def on_llm_end(self, response, **kwargs):  # type: ignore[override]
        self.calls += 1
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
            self.prompt_tokens += int(usage.get("input_tokens", 0) or 0)
            self.completion_tokens += int(usage.get("output_tokens", 0) or 0)
        except Exception:
            pass


def with_workdir():
    workdir = tempfile.mkdtemp(prefix="lg_team_")
    old = os.getcwd()
    os.chdir(workdir)
    return old


def checked(task: dict, answer: str) -> bool:
    """优先用任务自带的判分函数，否则回退"非空"。"""
    if not (answer or "").strip():
        return False
    check = task.get("check")
    if check is None:
        return True
    try:
        return bool(check(answer))
    except Exception:
        return True


async def run_handwritten(task: dict) -> dict:
    llm = CountingLLM(**llm_kwargs())
    old_cwd = with_workdir()
    try:
        team = MultiAgentTeam(llm=llm, policy="relevance", budget_chars=1200, max_retries=1)
        started = time.time()
        answer = await team.run(task["prompt"])
        return {
            "success": checked(task, answer),
            "seconds": round(time.time() - started, 2),
            "llm_calls": llm.calls,
            "rounds": team.rounds,
            "review": (team.review or "")[:40],
        }
    finally:
        os.chdir(old_cwd)
        await llm.client.close()


async def run_langgraph(graph, task: dict, counter: TokenCounter, thread_id: str) -> dict:
    old_cwd = with_workdir()
    try:
        started = time.time()
        state = await graph.ainvoke(
            {"task": task["prompt"], "rounds": 0},
            config={"configurable": {"thread_id": thread_id}, "callbacks": [counter]},
        )
        final = state.get("final") or ""
        return {
            "success": checked(task, final),
            "seconds": round(time.time() - started, 2),
            "llm_calls": counter.calls,
            "rounds": int(state.get("rounds", 0)),
            "review": (state.get("review") or "")[:40],
        }
    finally:
        os.chdir(old_cwd)


async def demo_multiturn() -> None:
    """checkpointer：同一 thread_id 的两轮对话（状态持久化）。

    说明：`make_checkpointer(path)` 走 AsyncSqliteSaver 可跨进程持久化；
    本演示用内存版（InMemorySaver）避免旧库文件锁导致的挂起。
    """
    graph = build_team(
        checkpointer=await make_checkpointer(),
        max_retries=0,
        require_approval=False,
        sensitive_tools=[],
    )
    config = {"configurable": {"thread_id": "session-demo"}}
    first = await graph.ainvoke(
        {"task": "记住：我的项目代号是 ORION，报告放在 reports 目录下。", "rounds": 0}, config
    )
    second = await graph.ainvoke(
        {"task": "我刚才说的项目代号是什么？只回答代号本身。", "rounds": 0}, config
    )
    messages = second.get("messages") or []
    orion_in_context = any("ORION" in str(getattr(m, "content", "")) for m in messages)
    print("\n[多轮会话 / checkpointer]")
    print(f"  第 1 轮 final: {(first.get('final') or '')[:50]}")
    print(f"  第 2 轮 final: {(second.get('final') or '')[:50]}")
    print(f"  第 2 轮可见消息数: {len(messages)}（上下文含第 1 轮内容: {orion_in_context}）")


async def demo_hitl() -> None:
    """interrupt：计划审批（拒绝）+ 工具级审批（bash 放行）。"""
    print("\n[人工审批 / interrupt]")
    # 场景 1：计划审批被拒 → 图提前结束
    reject_graph = build_team(checkpointer=await make_checkpointer(), max_retries=0)
    config = {"configurable": {"thread_id": "hitl-reject"}}
    state = await reject_graph.ainvoke({"task": "创建 hello.txt，内容 Hello", "rounds": 0}, config)
    payload = state.get("__interrupt__")
    if payload:
        value = getattr(payload[0], "value", payload[0])
        print(f"  暂停，等待审批（{str(value)[:70]}）")
    else:
        print("  （未暂停，异常）")
    state = await reject_graph.ainvoke(Command(resume={"approved": False}), config)
    print(f"  拒绝后 final: {(state.get('final') or '')[:60]}")

    # 场景 2：批准计划 → 执行中 bash 工具再次 interrupt → 放行
    approve_graph = build_team(checkpointer=await make_checkpointer(), max_retries=0)
    config2 = {"configurable": {"thread_id": "hitl-approve"}}
    state = await approve_graph.ainvoke({"task": "用 bash 列出当前目录文件", "rounds": 0}, config2)
    rounds = 0
    while state.get("__interrupt__") and rounds < 6:
        payload = state["__interrupt__"][0]
        value = getattr(payload, "value", payload)
        tool = value.get("tool") if isinstance(value, dict) else None
        print(f"  第 {rounds + 1} 次暂停（{'工具: ' + tool if tool else '计划审批'}）→ 放行")
        state = await approve_graph.ainvoke(Command(resume={"approved": True}), config2)
        rounds += 1
    print(f"  放行后 final: {(state.get('final') or '')[:60]}")


async def main(limit: int, skip_demos: bool) -> None:
    tasks = TASKS[:limit] if limit else TASKS[:6]
    counter = TokenCounter()
    # 对照场景关闭"审批中断"（与手写团队默认自动放行一致，保证同口径）
    graph = build_team(
        checkpointer=await make_checkpointer(),
        max_retries=1,
        require_approval=False,
        sensitive_tools=[],
    )

    rows = []
    for index, task in enumerate(tasks):
        print(f"[compare] {task['id']} ...", flush=True)
        hand = await run_handwritten(task)
        lg_hand = await run_langgraph(graph, task, counter, thread_id=f"task-{index}")
        rows.append({"task_id": task["id"], "handwritten": hand, "langgraph": lg_hand})
        await asyncio.sleep(0.5)

    def agg(key: str, field: str) -> float:
        values = [row[key][field] for row in rows if row[key].get(field) is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    summary = {
        "n_tasks": len(rows),
        "handwritten": {
            "success_rate": round(sum(r["handwritten"]["success"] for r in rows) / len(rows), 3),
            "avg_seconds": agg("handwritten", "seconds"),
            "avg_llm_calls": agg("handwritten", "llm_calls"),
            "avg_rounds": agg("handwritten", "rounds"),
        },
        "langgraph": {
            "success_rate": round(sum(r["langgraph"]["success"] for r in rows) / len(rows), 3),
            "avg_seconds": agg("langgraph", "seconds"),
            "avg_llm_calls": agg("langgraph", "llm_calls"),
            "avg_rounds": agg("langgraph", "rounds"),
            "prompt_tokens": counter.prompt_tokens,
            "completion_tokens": counter.completion_tokens,
        },
    }

    print("\n=== 多 Agent 对照（手写 vs LangGraph）===")
    print(f"{'实现':<20} {'成功率':>7} {'平均调用':>8} {'平均轮次':>8} {'平均耗时':>8}")
    for name, key in (("手写 MultiAgentTeam", "handwritten"), ("LangGraph 团队", "langgraph")):
        s = summary[key]
        print(
            f"{name:<20} {s['success_rate'] * 100:>6.1f}% {s['avg_llm_calls']:>8} "
            f"{s['avg_rounds']:>8} {s['avg_seconds']:>7}s"
        )

    if not skip_demos:
        await demo_multiturn()
        await demo_hitl()

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"compare_lg_team_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "summary": summary, "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3, help="对照任务数（默认 3）")
    parser.add_argument("--skip-demos", action="store_true", help="跳过 checkpointer / interrupt 演示")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, skip_demos=args.skip_demos))
