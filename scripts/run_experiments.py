"""一键实验：先做离线决策影响分析，再做四策略对比评测。

用法：
    .\\.venv\\Scripts\\python.exe run_experiments.py            # 全部任务
    .\\.venv\\Scripts\\python.exe run_experiments.py --limit 3  # 快速验证
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import asyncio

from mini_agent import evaluate
from mini_agent.config import warmup_async


async def main(limit: int = 0, budget: int = 1200):
    await warmup_async()
    await evaluate.run(budget_chars=budget, limit=limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务（0=全部）")
    parser.add_argument("--budget", type=int, default=1200)
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, budget=args.budget))
