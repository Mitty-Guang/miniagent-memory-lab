"""跨模型对照：同一批任务、同一策略，比较两个模型的成功率与成本。

运行：
    .\\.venv\\Scripts\\python.exe compare_models.py --models deepseek-flash,deepseek-v4-pro --limit 6 --budget 500

说明：模型通过环境变量 MODEL_NAME 切换（config.llm_kwargs 每次调用时读取），
因此同一个进程内可以顺序跑多个模型。
"""
import argparse
import asyncio
import datetime
import json
import os
from pathlib import Path
from typing import Dict, List

from config import warmup_async
from runner import run_agent_task
from task_suite import TASKS

RESULTS_DIR = Path("results")


async def run(models: List[str], limit: int, budget: int, policy: str) -> Dict:
    baseline_model = os.environ.get("MODEL_NAME", "")
    await warmup_async()

    tasks = TASKS[:limit]
    rows: List[Dict] = []
    for model in models:
        os.environ["MODEL_NAME"] = model
        for task in tasks:
            print(f"[compare-models] model={model} task={task['id']} ...", flush=True)
            result = await run_agent_task(task, policy=policy, budget_chars=budget)
            result.pop("messages", None)
            result.pop("answers", None)
            result["model"] = model
            rows.append(result)
            await asyncio.sleep(0.3)
    if baseline_model:
        os.environ["MODEL_NAME"] = baseline_model

    print(f"\n=== 跨模型对照（{len(tasks)} 任务 × 策略 {policy} × 预算 {budget}）===")
    print("| 模型 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |")
    print("| --- | --- | --- | --- | --- |")
    summary = []
    for model in models:
        sub = [r for r in rows if r["model"] == model]
        n = len(sub) or 1
        ok = sum(r["success"] for r in sub)
        item = {
            "model": model,
            "runs": len(sub),
            "success": ok,
            "success_rate": round(ok / n, 3),
            "avg_calls": round(sum(r["llm_calls"] for r in sub) / n, 2),
            "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in sub) / n, 0),
            "avg_seconds": round(sum(r["seconds"] for r in sub) / n, 2),
        }
        summary.append(item)
        print(
            f"| {model} | {ok}/{len(sub)} | {item['avg_calls']} | "
            f"{item['avg_prompt_tokens']:.0f} | {item['avg_seconds']} |"
        )

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"compare_models_{stamp}.json"
    path.write_text(
        json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {path}")
    return {"rows": rows, "summary": summary}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=str, default="deepseek-flash,deepseek-v4-pro")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--budget", type=int, default=500)
    parser.add_argument("--policy", type=str, default="impact")
    args = parser.parse_args()
    asyncio.run(
        run(
            models=[m.strip() for m in args.models.split(",") if m.strip()],
            limit=args.limit,
            budget=args.budget,
            policy=args.policy,
        )
    )
