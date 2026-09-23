"""MCP Server：把本项目的沙箱工具集暴露为标准 MCP 工具。

任何 MCP 兼容客户端（Claude Desktop / Cursor / 其他 Agent 框架）都能通过
stdio 发现并调用这些工具——这就是"工具接入标准化"的最小可运行形态。

依赖 mcp 2.x（`MCPServer`，即原 FastMCP）。

运行（通常由 MCP 客户端拉起，也可手动测试）：
    .\\.venv\\Scripts\\python.exe extras\\mcp\\mcp_server.py

工具：
- run_python(code)：子进程沙箱执行 Python；
- read_file(path) / write_file(path, content) / list_dir(path)：受限文件操作；
- run_command(command)：危险命令黑名单 + 超时。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from mini_agent.sandbox import (  # noqa: E402
    SandboxedBashExecutor,
    SandboxedFileEditor,
    SandboxedPythonExecutor,
)

SANDBOX_ROOT = os.environ.get("MCP_SANDBOX_ROOT", os.getcwd())

mcp = MCPServer("miniagent-tools")

_python = SandboxedPythonExecutor(root=SANDBOX_ROOT)
_files = SandboxedFileEditor(root=SANDBOX_ROOT)
_bash = SandboxedBashExecutor(root=SANDBOX_ROOT)


@mcp.tool()
def run_python(code: str) -> str:
    """在子进程沙箱中执行 Python 代码，返回标准输出（默认 15 秒超时）。"""
    import asyncio

    result = asyncio.run(_python.execute(code=code))
    return result.output if result.success else f"错误: {result.error}"


@mcp.tool()
def read_file(path: str) -> str:
    """读取沙箱根目录内的文本文件。"""
    import asyncio

    result = asyncio.run(_files.execute(action="read", path=path))
    return result.output if result.success else f"错误: {result.error}"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """写入沙箱根目录内的文本文件（越界路径会被拒绝）。"""
    import asyncio

    result = asyncio.run(_files.execute(action="write", path=path, content=content))
    return result.output if result.success else f"错误: {result.error}"


@mcp.tool()
def list_dir(path: str = ".") -> str:
    """列出沙箱根目录内的目录内容。"""
    import asyncio

    result = asyncio.run(_files.execute(action="list", path=path))
    return result.output if result.success else f"错误: {result.error}"


@mcp.tool()
def run_command(command: str) -> str:
    """执行命令行命令（危险命令黑名单 + 30 秒超时）。"""
    import asyncio

    result = asyncio.run(_bash.execute(command=command))
    return result.output if result.success else f"错误: {result.error}"


if __name__ == "__main__":
    mcp.run()
