"""预算扫描实验：不同预算下三种记忆策略的成功率/成本曲线。

用法：
    .\\.venv\\Scripts\\python.exe run_sweep.py
    .\\.venv\\Scripts\\python.exe run_sweep.py --budgets 300,600 --policies recent,relevance --limit 6
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from mini_agent.evaluate import load_priors
from mini_agent.runner import run_agent_task
from mini_agent.task_suite import TASKS

POLICIES = ["recent", "relevance"]
BUDGETS = [300, 500, 800, 1200]
RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def aggregate(records: List[Dict]) -> Dict:
    n = len(records)
    if not n:
        return {}
    return {
        "n_tasks": n,
        "success_rate": round(sum(r["success"] for r in records) / n, 3),
        "avg_steps": round(sum(r["steps"] for r in records) / n, 2),
        "avg_prompt_tokens": round(sum(r.get("prompt_tokens", 0) for r in records) / n, 1),
        "avg_prompt_chars": round(sum(r["prompt_chars"] for r in records) / n, 0),
    }


async def run(
    budgets: List[int],
    policies: List[str],
    limit: int = 0,
    sleep: float = 0.3,
) -> Dict:
    from mini_agent.config import warmup_async

    await warmup_async()
    tasks = TASKS[:limit] if limit else TASKS

    records: List[Dict] = []
    table: Dict[str, Dict[str, Dict]] = {}

    for budget in budgets:
        for policy in policies:
            rows = []
            for task in tasks:
                print(
                    f"[sweep] budget={budget} policy={policy} task={task['id']} ...",
                    flush=True,
                )
                result = await run_agent_task(
                    task,
                    policy=policy,
                    budget_chars=budget,
                )
                result.pop("messages", None)
                result.pop("answers", None)
                result["budget"] = budget
                rows.append(result)
                records.append(result)
                await asyncio.sleep(sleep)
            table.setdefault(str(budget), {})[policy] = aggregate(rows)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(exist_ok=True)
    output = {
        "timestamp": timestamp,
        "budgets": budgets,
        "policies": policies,
        "n_tasks": len(tasks),
        "table": table,
        "records": records,
    }
    path = RESULTS_DIR / f"sweep_{timestamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 预算扫描（成功率，{len(tasks)} 个任务） ===")
    header = "| 策略 | " + " | ".join(f"预算 {b}" for b in budgets) + " |"
    print(header)
    print("| --- | " + " | ".join("---" for _ in budgets) + " |")
    for policy in policies:
        cells = []
        for budget in budgets:
            s = table[str(budget)][policy]
            cells.append(f"{s['success_rate']:.1%}")
        print(f"| {policy} | " + " | ".join(cells) + " |")

    print(f"\n=== 平均累计 Prompt Token ===")
    print(header)
    print("| --- | " + " | ".join("---" for _ in budgets) + " |")
    for policy in policies:
        cells = []
        for budget in budgets:
            s = table[str(budget)][policy]
            cells.append(f"{s['avg_prompt_tokens']:.0f}")
        print(f"| {policy} | " + " | ".join(cells) + " |")

    print(f"\n结果已保存: {path}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--budgets", type=str, default=",".join(str(b) for b in BUDGETS))
    parser.add_argument("--policies", type=str, default=",".join(POLICIES))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.3)
    args = parser.parse_args()
    asyncio.run(
        run(
            budgets=[int(b) for b in args.budgets.split(",") if b.strip()],
            policies=[p.strip() for p in args.policies.split(",") if p.strip()],
            limit=args.limit,
            sleep=args.sleep,
        )
    )
