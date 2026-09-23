"""快速检查当前 .env 配置的 API：连通性 + 工具调用能力。

用法：
    .\\.venv\\Scripts\\python.exe check_api.py
"""
import asyncio

from config import CountingLLM, llm_kwargs


async def main():
    kwargs = llm_kwargs()
    print(f"model={kwargs['model']} base_url={kwargs['base_url']}")
    llm = CountingLLM(**kwargs)

    ping = await llm.chat([{"role": "user", "content": "ping: reply with exactly pong"}])
    print("ping ->", (ping.content or "").strip()[:50])

    tools = [
        {
            "type": "function",
            "function": {
                "name": "python_execute",
                "description": "执行 Python 代码并返回结果",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        }
    ]
    resp = await llm.chat(
        [{"role": "user", "content": "用 python_execute 工具计算 2+3。必须先调用工具。"}],
        tools=tools,
    )
    if resp.tool_calls:
        print("tool_calls ->", [c["function"]["name"] for c in resp.tool_calls])
        print("args ->", resp.tool_calls[0]["function"]["arguments"])
    else:
        print("tool_calls -> NONE; content:", (resp.content or "")[:150])


if __name__ == "__main__":
    asyncio.run(main())
