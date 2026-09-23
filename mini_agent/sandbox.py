"""执行沙箱与权限控制：子进程隔离、超时、输出截断、路径白名单、命令黑名单。

说明（诚实边界）：这是"进程级"沙箱（独立子进程 + cwd 限制 + 超时 + 黑名单），
不是容器级隔离；生产环境应叠加容器/低权限用户/网络隔离。

用法：
    from mini_agent.sandbox import build_sandboxed_tools
    tools = build_sandboxed_tools(root=os.getcwd())
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from mini_agent.tools import BaseTool, ToolResult

DEFAULT_TIMEOUT = 15
MAX_OUTPUT = 4000

BLOCKED_PATTERNS: List[str] = [
    r"\brm\s+-[a-z]*r[a-z]*f\b",           # rm -rf
    r"\brm\s+-[a-z]*f[a-z]*r\b",
    r"\bdel\s+/[fsq]\b",                    # del /f /s /q
    r"\brd\s+/s\b",
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\bmkfs\b",
    r"\bRemove-Item\b.*-Recurse",           # PowerShell 递归删除
    r">\s*/dev/sd[a-z]",
]


class PathGuard:
    """限制文件操作在 root 目录内（防 ../ 逃逸）。"""

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PermissionError(f"路径越界（沙箱根目录：{self.root}）：{path}")
        return candidate


def is_blocked_command(command: str) -> Optional[str]:
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


class SandboxedPythonExecutor(BaseTool):
    """子进程执行 Python（超时 + cwd 限制 + 输出截断），替代进程内 exec。"""

    name: str = "python_execute"
    description: str = "在子进程沙箱中执行 Python 代码并返回输出（默认 15 秒超时）"
    parameters: Dict = {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "要执行的 Python 代码"}},
        "required": ["code"],
    }
    root: str = "."
    timeout: int = DEFAULT_TIMEOUT

    async def execute(self, code: str, **kwargs) -> ToolResult:
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f"执行超时（>{self.timeout}s），已终止")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if result.returncode != 0:
            return ToolResult(success=False, error=(result.stderr or "")[:MAX_OUTPUT])
        return ToolResult(success=True, output=(result.stdout or "")[:MAX_OUTPUT] or "(无输出)")


class SandboxedFileEditor(BaseTool):
    """文件读写（PathGuard 限制在 root 内）。"""

    name: str = "file_editor"
    description: str = "查看、创建和编辑文件（限沙箱根目录内）"
    parameters: Dict = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list"]},
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["action", "path"],
    }
    root: str = "."

    async def execute(self, action: str, path: str, content: str = "", **kwargs) -> ToolResult:
        try:
            target = PathGuard(self.root).resolve(path)
            if action == "read":
                return ToolResult(success=True, output=target.read_text(encoding="utf-8")[:MAX_OUTPUT])
            if action == "write":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return ToolResult(success=True, output=f"文件已写入: {path}")
            if action == "list":
                if not target.is_dir():
                    return ToolResult(success=False, error="路径不是目录")
                return ToolResult(success=True, output="\n".join(os.listdir(target)))
            return ToolResult(success=False, error=f"不支持的操作: {action}")
        except PermissionError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class SandboxedBashExecutor(BaseTool):
    """命令执行（黑名单 + 超时 + 输出截断）。"""

    name: str = "bash_execute"
    description: str = "执行命令行命令（危险命令被拦截；默认 30 秒超时）"
    parameters: Dict = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    root: str = "."
    timeout: int = 30

    async def execute(self, command: str, **kwargs) -> ToolResult:
        blocked = is_blocked_command(command)
        if blocked:
            return ToolResult(
                success=False,
                error=f"命令被沙箱策略拦截（匹配规则 {blocked}）：请改用其他方式。",
            )
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f"命令执行超时（>{self.timeout}s）")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if result.returncode != 0:
            return ToolResult(success=False, error=(result.stderr or "")[:MAX_OUTPUT])
        return ToolResult(success=True, output=(result.stdout or "")[:MAX_OUTPUT])


def build_sandboxed_tools(root: str, timeout: int = DEFAULT_TIMEOUT) -> List[BaseTool]:
    return [
        SandboxedPythonExecutor(root=root, timeout=timeout),
        SandboxedFileEditor(root=root),
        SandboxedBashExecutor(root=root),
    ]
