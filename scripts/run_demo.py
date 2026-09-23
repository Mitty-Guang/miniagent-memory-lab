"""非交互式部署验证：跑一个真实任务并打印完整消息流（Memory.messages）。

用法：
    .\\.venv\\Scripts\\python.exe run_demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import os

from mini_agent import MiniAgent
from mini_agent.llm import SimpleLLM
from main_mini import load_env_file


async def main():
    load_env_file()
    llm = SimpleLLM(
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    agent = MiniAgent(llm=llm, name="DemoAgent", max_steps=8)

    task = "用 Python 计算 1 到 100 的和，并把结果写入 sum_result.txt"
    await agent.run(task)

    print("\n=== 消息流（Memory.messages）===")
    for i, msg in enumerate(agent.memory.messages):
        preview = (msg.content or "").replace("\n", " ")[:80]
        calls = ""
        if msg.tool_calls:
            calls = " | tool_calls: " + ", ".join(
                c["function"]["name"] for c in msg.tool_calls
            )
        print(f"[{i}] {msg.role.value:9s} {preview}{calls}")

    print(f"\n总消息数: {len(agent.memory.messages)}")


if __name__ == "__main__":
    asyncio.run(main())
