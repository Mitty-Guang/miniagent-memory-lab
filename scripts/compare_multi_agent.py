"""对照实验：单 Agent vs 多 Agent（Planner → Executor → Reviewer）。

运行：
    .\\.venv\\Scripts\\python.exe compare_multi_agent.py --limit 8 --budget 800

指标：成功率（同一套确定性判分）、LLM 调用数、Prompt tokens、耗时、审查轮数。
两个实现使用同一模型、同一记忆策略与预算、同一工具集，唯一变量是「单 Agent vs 多 Agent」。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import contextlib
import datetime
import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List

from mini_agent.config import CountingLLM, llm_kwargs, warmup_async
from mini_agent.evaluate import load_priors
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.multi_agent import MultiAgentTeam
from mini_agent.runner import run_agent_task
from mini_agent.task_suite import TASKS

RESULTS_DIR = Path("results")


async def run_multi_agent_task(task: Dict, policy: str, budget: int, priors: Dict) -> Dict:
    workdir = tempfile.mkdtemp(prefix=f"ma_{task['id']}_")
    old_cwd = os.getcwd()
    os.chdir(workdir)

    llm = CountingLLM(**llm_kwargs())
    ltm = LongTermMemory(path=str(Path(workdir) / "ltm.sqlite3"))

    answers: List[str] = []
    rounds = 0
    success = True
    error = ""
    start = time.time()
    try:
        for index, phase in enumerate(task.get("phases") or [
            {"prompt": task["prompt"], "check": task["check"]}
        ]):
            team = MultiAgentTeam(
                llm=llm,
                policy=policy,
                budget_chars=budget,
                ltm=ltm,
                session_id=f"{task['id']}#{index}",
                task_id=task["id"],
            )
            with contextlib.redirect_stdout(io.StringIO()):
                await team.run(phase["prompt"])
            rounds += team.rounds
            final = team.executor  # 供取最终答案
            answer = ""
            if final is not None:
                from mini_agent.multi_agent import final_answer

                answer = final_answer(final)
            answers.append(answer)
            try:
                ok = bool(phase["check"](answer, workdir))
            except Exception:
                ok = False
            success = success and ok
    except Exception as exc:
        error = str(exc)
        success = False
    elapsed = time.time() - start

    ltm.close()
    os.chdir(old_cwd)

    return {
        "impl": "multi-agent",
        "task_id": task["id"],
        "success": success,
        "llm_calls": llm.calls,
        "prompt_tokens": llm.prompt_tokens,
        "completion_tokens": llm.completion_tokens,
        "rounds": rounds,
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
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--budget", type=int, default=800)
    parser.add_argument("--policy", type=str, default="relevance")
    args = parser.parse_args()

    await warmup_async()

    tasks = TASKS[: args.limit]

    rows: List[Dict] = []
    for task in tasks:
        print(f"[compare-ma] {task['id']} ...", flush=True)
        single = await run_agent_task(
            task,
            policy=args.policy,
            budget_chars=args.budget,
        )
        rows.append(
            {
                "impl": "single-agent",
                "task_id": single["task_id"],
                "success": single["success"],
                "llm_calls": single["llm_calls"],
                "prompt_tokens": single["prompt_tokens"],
                "completion_tokens": single["completion_tokens"],
                "rounds": 0,
                "seconds": single["seconds"],
                "final_answer": single["final_answer"],
                "error": single["error"],
            }
        )
        rows.append(await run_multi_agent_task(task, args.policy, args.budget, priors))

    print(f"\n=== 单 Agent vs 多 Agent（{len(tasks)} 任务 × 预算 {args.budget}）===")
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
    for impl in ("single-agent", "multi-agent"):
        s = summarize(rows, impl)
        print(
            f"| {s['impl']} | {s['success_rate']:.1%} | {s['avg_calls']} | "
            f"{s['avg_prompt_tokens']:.0f} | {s['avg_seconds']} |"
        )

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"compare_multi_agent_{stamp}.json"
    out.write_text(
        json.dumps(
            {"rows": rows, "summary": [summarize(rows, i) for i in ("single-agent", "multi-agent")]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    asyncio.run(main())
