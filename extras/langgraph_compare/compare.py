"""对照实验：同一套记忆策略与工具，手写循环（主项目）vs LangGraph 图。

运行（在仓库根目录下，需先安装 requirements-extras.txt）：
    .\\.venv\\Scripts\\python.exe extras\\langgraph_compare\\compare.py --limit 8 --budget 500 --policy relevance

说明：
- 任务集 / 工具 / 模型 / 策略 / 预算 完全一致，唯一变量是「运行时实现」；
- 指标：成功率（同一套确定性判分）、LLM 调用数、prompt/completion tokens、耗时。
"""
import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

for path in (str(HERE), str(REPO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from langchain_core.messages import HumanMessage  # noqa: E402

from agent_graph import build_memory_agent  # noqa: E402
from config import warmup_async  # noqa: E402
from runner import run_agent_task  # noqa: E402
from task_suite import TASKS  # noqa: E402

RESULTS_DIR = REPO / "results"


def load_priors() -> Dict[str, float]:
    path = RESULTS_DIR / "impact_priors.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


async def run_langgraph_task(task: Dict, policy: str, budget: int, priors: Dict) -> Dict:
    workdir = tempfile.mkdtemp(prefix=f"lg_{task['id']}_")
    old_cwd = os.getcwd()
    os.chdir(workdir)
    graph, stats = build_memory_agent(
        policy=policy, budget_chars=budget, priors=priors
    )

    answers: List[str] = []
    success = True
    error = ""
    start = time.time()
    try:
        for phase in task.get("phases") or [
            {"prompt": task["prompt"], "check": task["check"]}
        ]:
            state = {
                "messages": [HumanMessage(phase["prompt"])],
                "policy": policy,
                "budget": budget,
                "priors": priors,
            }
            with contextlib.redirect_stdout(io.StringIO()):
                result = await graph.ainvoke(state, config={"recursion_limit": 44})
            final = ""
            for msg in reversed(result["messages"]):
                if msg.type == "ai" and msg.content:
                    final = str(msg.content)
                    break
            answers.append(final)
            try:
                ok = bool(phase["check"](final, workdir))
            except Exception:
                ok = False
            success = success and ok
    except Exception as exc:
        error = str(exc)
        success = False
    elapsed = time.time() - start
    os.chdir(old_cwd)

    return {
        "impl": "langgraph",
        "task_id": task["id"],
        "success": success,
        "llm_calls": stats["llm_calls"],
        "prompt_tokens": stats["prompt_tokens"],
        "completion_tokens": stats["completion_tokens"],
        "seconds": round(elapsed, 2),
        "final_answer": answers[-1] if answers else "",
        "error": error,
    }


def summarize(rows: List[Dict], impl: str) -> Dict:
    subset = [r for r in rows if r["impl"] == impl]
    n = len(subset) or 1
    return {
        "impl": impl,
        "success_rate": round(sum(r["success"] for r in subset) / n, 3),
        "avg_calls": round(sum(r["llm_calls"] for r in subset) / n, 2),
        "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in subset) / n, 0),
        "avg_seconds": round(sum(r["seconds"] for r in subset) / n, 2),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=6, help="任务数（取单阶段任务前 N 个）")
    parser.add_argument("--budget", type=int, default=500)
    parser.add_argument("--policy", type=str, default="relevance")
    args = parser.parse_args()

    await warmup_async()

    tasks = [t for t in TASKS if not t.get("phases")][: args.limit]
    priors = load_priors() if args.policy == "impact" else {}

    rows: List[Dict] = []
    for task in tasks:
        print(f"[compare] {task['id']} ...", flush=True)
        rows.append(await run_langgraph_task(task, args.policy, args.budget, priors))
        mini = await run_agent_task(
            task,
            policy=args.policy,
            budget_chars=args.budget,
            impact_priors=priors or None,
        )
        rows.append(
            {
                "impl": "miniagent",
                "task_id": mini["task_id"],
                "success": mini["success"],
                "llm_calls": mini["llm_calls"],
                "prompt_tokens": mini["prompt_tokens"],
                "completion_tokens": mini["completion_tokens"],
                "seconds": mini["seconds"],
                "final_answer": mini["final_answer"],
                "error": mini["error"],
            }
        )

    print(f"\n=== 对照实验（{len(tasks)} 任务 × 策略 {args.policy} × 预算 {args.budget} 字符）===")
    print("| 任务 | 实现 | 成功 | LLM调用 | Prompt tokens | 耗时(s) |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        print(
            f"| {row['task_id']} | {row['impl']} | {'✅' if row['success'] else '❌'} | "
            f"{row['llm_calls']} | {row['prompt_tokens']} | {row['seconds']} |"
        )

    print("\n=== 汇总 ===")
    print("| 实现 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |")
    print("| --- | --- | --- | --- | --- |")
    for impl in ("miniagent", "langgraph"):
        s = summarize(rows, impl)
        print(
            f"| {s['impl']} | {s['success_rate']:.1%} | {s['avg_calls']} | "
            f"{s['avg_prompt_tokens']:.0f} | {s['avg_seconds']} |"
        )

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"compare_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {"rows": rows, "summary": [summarize(rows, i) for i in ("miniagent", "langgraph")]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    asyncio.run(main())
