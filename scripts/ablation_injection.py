"""消融实验：长期记忆的注入方式（独立 system 消息 vs 系统提示词）对跨会话任务的影响。

运行：
    .\\.venv\\Scripts\\python.exe ablation_injection.py --repeat 2 --budget 800 --policy relevance

设计：
- 任务：4 个跨会话任务（phase 1 教 → phase 2 用；每阶段独立会话、共享记忆库）；
- 两种注入方式：
  - message（当前实现）：把检索到的记忆作为独立 system 消息放进对话；
  - system_prompt：把记忆块拼进本次运行的系统提示词；
- 每格重复 --repeat 次，降低单次波动。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio
import datetime
import json
from pathlib import Path
from typing import Dict, List

from mini_agent.config import warmup_async
from mini_agent.evaluate import load_priors
from mini_agent.runner import run_agent_task
from mini_agent.task_suite import TASKS

MODES = ["message", "system_prompt"]
RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def summarize(rows: List[Dict], mode: str) -> Dict:
    sub = [r for r in rows if r["mode"] == mode]
    n = len(sub) or 1
    return {
        "mode": mode,
        "runs": len(sub),
        "success": sum(r["success"] for r in sub),
        "success_rate": round(sum(r["success"] for r in sub) / n, 3),
        "avg_calls": round(sum(r["llm_calls"] for r in sub) / n, 2),
        "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in sub) / n, 0),
        "avg_seconds": round(sum(r["seconds"] for r in sub) / n, 2),
    }


async def run(repeat: int, budget: int, policy: str) -> Dict:
    await warmup_async()
    tasks = [t for t in TASKS if t.get("phases")]

    rows: List[Dict] = []
    for mode in MODES:
        for round_index in range(repeat):
            for task in tasks:
                print(
                    f"[ablation] mode={mode} repeat={round_index + 1} task={task['id']} ...",
                    flush=True,
                )
                result = await run_agent_task(
                    task,
                    policy=policy,
                    budget_chars=budget,
                    memory_injection=mode,
                )
                result.pop("messages", None)
                result.pop("answers", None)
                result["mode"] = mode
                result["repeat"] = round_index + 1
                rows.append(result)
                await asyncio.sleep(0.3)

    print(f"\n=== 注入方式消融（{len(tasks)} 个跨会话任务 × {repeat} 次 × 预算 {budget}）===")
    print("| 注入方式 | 成功 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |")
    print("| --- | --- | --- | --- | --- |")
    summaries = []
    for mode in MODES:
        s = summarize(rows, mode)
        summaries.append(s)
        print(
            f"| {mode} | {s['success']}/{s['runs']} | {s['avg_calls']} | "
            f"{s['avg_prompt_tokens']:.0f} | {s['avg_seconds']} |"
        )

    print("\n=== 任务级明细 ===")
    print("| 任务 | " + " | ".join(MODES) + " |")
    print("| --- | " + " | ".join("---" for _ in MODES) + " |")
    for task in tasks:
        cells = []
        for mode in MODES:
            sub = [r for r in rows if r["mode"] == mode and r["task_id"] == task["id"]]
            ok = sum(r["success"] for r in sub)
            cells.append(f"{ok}/{len(sub)}")
        print(f"| {task['id']} | " + " | ".join(cells) + " |")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"ablation_injection_{stamp}.json"
    path.write_text(
        json.dumps({"rows": rows, "summary": summaries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {path}")
    return {"rows": rows, "summary": summaries}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--budget", type=int, default=800)
    parser.add_argument("--policy", type=str, default="relevance")
    args = parser.parse_args()
    asyncio.run(run(repeat=args.repeat, budget=args.budget, policy=args.policy))
