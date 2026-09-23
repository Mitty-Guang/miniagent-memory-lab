"""评测：同一任务集、同一预算下对比四种记忆策略。

指标：成功率 / 平均步数 / 平均 LLM 调用数 / 平均上下文字符数 / 平均耗时。
结果保存到 results/eval_*.json，并打印 Markdown 表格。
"""
import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from runner import run_agent_task
from task_suite import TASKS

POLICIES = ["all", "recent", "relevance", "impact"]
RESULTS_DIR = Path("results")


def load_priors(path: str = "results/impact_priors.json") -> Dict[str, float]:
    file = Path(path)
    if not file.exists():
        print(f"[warn] 未找到先验表 {path}，impact 策略将使用默认先验 0.5")
        return {}
    return json.loads(file.read_text(encoding="utf-8"))


def aggregate(records: List[Dict]) -> Dict:
    if not records:
        return {}
    n = len(records)
    return {
        "n_tasks": n,
        "success_rate": round(sum(r["success"] for r in records) / n, 3),
        "avg_steps": round(sum(r["steps"] for r in records) / n, 2),
        "avg_llm_calls": round(sum(r["llm_calls"] for r in records) / n, 2),
        "avg_prompt_chars": round(sum(r["prompt_chars"] for r in records) / n, 0),
        "avg_prompt_tokens": round(sum(r.get("prompt_tokens", 0) for r in records) / n, 1),
        "avg_seconds": round(sum(r["seconds"] for r in records) / n, 2),
    }


async def run(
    budget_chars: int = 1200,
    policies: Optional[List[str]] = None,
    limit: int = 0,
) -> Dict:
    policies = policies or POLICIES
    tasks = TASKS[:limit] if limit else TASKS
    priors = load_priors()

    all_records: List[Dict] = []
    summary: Dict[str, Dict] = {}

    for policy in policies:
        records = []
        for task in tasks:
            print(f"[eval] policy={policy} task={task['id']} ...", flush=True)
            result = await run_agent_task(
                task,
                policy=policy,
                budget_chars=budget_chars,
                impact_priors=priors if policy == "impact" else None,
            )
            result.pop("messages", None)  # 消息流不进评测摘要
            records.append(result)
            await asyncio.sleep(1.0)  # 缓解限流
        all_records.extend(records)
        summary[policy] = aggregate(records)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(exist_ok=True)
    output = {
        "budget_chars": budget_chars,
        "policies": policies,
        "timestamp": timestamp,
        "summary": summary,
        "records": all_records,
    }
    (RESULTS_DIR / f"eval_{timestamp}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n=== 评测结果（预算 {budget_chars} 字符） ===")
    header = "| 策略 | 成功率 | 平均步数 | 平均LLM调用 | 平均累计上下文 | 平均Prompt Token | 平均耗时(s) |"
    print(header)
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for policy in policies:
        s = summary[policy]
        print(
            f"| {policy} | {s['success_rate']:.1%} | {s['avg_steps']} | "
            f"{s['avg_llm_calls']} | {s['avg_prompt_chars']:.0f} | "
            f"{s['avg_prompt_tokens']:.0f} | {s['avg_seconds']} |"
        )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=1200)
    parser.add_argument("--policies", type=str, default=",".join(POLICIES))
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(
        run(
            budget_chars=args.budget,
            policies=[p.strip() for p in args.policies.split(",") if p.strip()],
            limit=args.limit,
        )
    )
