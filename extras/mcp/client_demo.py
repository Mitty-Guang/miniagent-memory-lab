"""MCP 客户端演示：以 stdio 方式连接 mcp_server.py，列出并调用工具。

运行：
    .\\.venv\\Scripts\\python.exe extras\\mcp\\client_demo.py
"""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SERVER = HERE / "mcp_server.py"


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=str(REPO),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("== 发现的 MCP 工具 ==")
            for tool in tools.tools:
                print(f"- {tool.name}: {(tool.description or '').splitlines()[0]}")

            print("\n== 调用 run_python ==")
            result = await session.call_tool("run_python", {"code": "print(6 * 7)"})
            print(result.content[0].text if result.content else result)

            print("\n== 调用 run_command（应被黑名单拦截）==")
            result = await session.call_tool("run_command", {"command": "rm -rf /tmp/x"})
            print(result.content[0].text if result.content else result)

            print("\n== 调用 write_file + read_file ==")
            await session.call_tool("write_file", {"path": "mcp_demo.txt", "content": "hello-mcp"})
            result = await session.call_tool("read_file", {"path": "mcp_demo.txt"})
            print(result.content[0].text if result.content else result)


if __name__ == "__main__":
    asyncio.run(main())
