"""冒烟测试：跑一个单阶段 + 一个跨会话任务，验证长期记忆/token/审批链路。

运行：
    .\\.venv\\Scripts\\python.exe smoke_test.py
"""
import asyncio

from config import warmup_async
from runner import run_agent_task
from task_suite import TASKS


async def main():
    await warmup_async()

    single = next(t for t in TASKS if t["id"] == "sum_1_100")
    phased = next(t for t in TASKS if t["id"] == "xs_project_code")

    for task in (single, phased):
        result = await run_agent_task(task, policy="relevance", budget_chars=500)
        print(
            f"\n[{task['id']}] success={result['success']} steps={result['steps']} "
            f"calls={result['llm_calls']} ltm_rw={result['ltm_reads']}/{result['ltm_writes']} "
            f"tokens={result['prompt_tokens']}/{result['completion_tokens']} "
            f"seconds={result['seconds']}"
        )
        for index, answer in enumerate(result["answers"]):
            print(f"  phase{index + 1} answer: {(answer or '')[:80]}")
        if result["error"]:
            print(f"  error: {result['error']}")


if __name__ == "__main__":
    asyncio.run(main())
