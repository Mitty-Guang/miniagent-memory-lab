"""HITL：工具执行前人工审批。

用法：把 ApprovalToolCollection 注入 Agent（替换默认 ToolCollection），
审批回调返回 True 放行、False 拒绝；被拒绝时把结构化错误回填给模型，
让它改用其他方式或直接作答（而不是让流程中断）。
"""
from typing import Callable, Optional

from mini_agent.tools import ToolCollection, ToolResult


class ApprovalToolCollection(ToolCollection):
    def __init__(
        self,
        approval_fn: Optional[Callable[[str, dict], bool]] = None,
        trace=None,
    ):
        super().__init__()
        self.approval_fn = approval_fn
        self.trace = trace
        self.decisions = []

    async def execute_tool(self, name: str, **kwargs) -> ToolResult:
        approved = True
        if self.approval_fn is not None:
            approved = bool(self.approval_fn(name, kwargs))

        decision = {"tool": name, "args": {k: str(v)[:80] for k, v in kwargs.items()}, "approved": approved}
        self.decisions.append(decision)
        if self.trace is not None:
            self.trace.log("approval", decision)

        if not approved:
            return ToolResult(
                success=False,
                error=f"工具 {name} 被人工审批拒绝：请改用其他方式完成任务，或基于已有信息直接作答。",
            )
        result = await super().execute_tool(name, **kwargs)
        if self.trace is not None:
            self.trace.log("tool", {"name": name, "success": result.success})
        return result


def auto_approve(tool_name: str, args: dict) -> bool:
    """评测默认策略：全部放行（但审批事件仍被记录）。"""
    return True
