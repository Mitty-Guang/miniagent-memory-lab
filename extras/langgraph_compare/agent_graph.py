"""LangGraph 版记忆管理 Agent：与 miniagent 的手写循环同一套策略与工具。

图结构：
    START → memory → agent ──(有 tool_calls)──→ tools ──→ memory
                      │
                      └──(无 tool_calls)→ END

- memory 节点：固定预算消息选择（all / recent / relevance / impact）；
- agent 节点：绑定工具的 LLM 调用（记录 token 与调用数）；
- tools 节点：ToolNode 执行工具并把结果写回 state；可选 approval_fn 人工审批；
- checkpointer：可选，多轮会话记忆（thread_id 隔离）。

注意：本模块复用 miniagent 的工具实现（sys.path 需先加入 miniagent 目录），
以保证两套实现使用完全相同的工具与执行语义。
"""
from typing import Annotated, Dict, List, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from llm_factory import get_llm
from memory_policy import select_messages

from mini_agent.tools import BashExecutor, FileEditor, PythonExecutor

SYSTEM_PROMPT = (
    "你是一个可以使用工具完成任务的助手。需要执行操作时调用工具；"
    "每次只做当前最必要的一步，根据工具结果决定下一步；任务完成后直接给出结果。"
)

_python = PythonExecutor()
_file = FileEditor()
_bash = BashExecutor()


@tool
async def python_execute(code: str) -> str:
    """执行 Python 代码并返回输出。"""
    result = await _python.execute(code=code)
    return result.output if result.success else f"错误: {result.error}"


@tool
async def file_editor(action: str, path: str, content: str = "") -> str:
    """读写文件：action 取 read / write / list。"""
    result = await _file.execute(action=action, path=path, content=content)
    return result.output if result.success else f"错误: {result.error}"


@tool
async def bash_execute(command: str) -> str:
    """执行命令行命令并返回输出。"""
    result = await _bash.execute(command=command)
    return result.output if result.success else f"错误: {result.error}"


TOOLS = [python_execute, file_editor, bash_execute]


class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]  # 完整历史
    prompt: List[AnyMessage]                             # 本轮送进模型的（预算内）
    policy: str
    budget: int
    priors: Dict[str, float]


def build_memory_agent(
    policy: str = "relevance",
    budget_chars: int = 1200,
    priors: Optional[Dict[str, float]] = None,
    checkpointer=None,
    approval_fn=None,
    stats: Optional[Dict[str, int]] = None,
):
    """构建 LangGraph 记忆 Agent；返回 (graph, stats)。"""
    llm = get_llm().bind_tools(TOOLS)
    stats = stats if stats is not None else {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }

    def memory_node(state: AgentState):
        selected = select_messages(
            state["messages"],
            policy=state.get("policy") or policy,
            budget_chars=int(state.get("budget") or budget_chars),
            priors=state.get("priors") or priors,
        )
        return {"prompt": selected}

    async def agent_node(state: AgentState):
        reply = await llm.ainvoke([SystemMessage(SYSTEM_PROMPT)] + state["prompt"])
        stats["llm_calls"] += 1
        usage = getattr(reply, "usage_metadata", None) or {}
        stats["prompt_tokens"] += int(usage.get("input_tokens") or 0)
        stats["completion_tokens"] += int(usage.get("output_tokens") or 0)
        return {"messages": [reply]}

    tools_node = ToolNode(TOOLS)

    if approval_fn is None:
        tools_callable = tools_node  # 直接作为节点使用（LangGraph 标准用法）
    else:
        async def tools_with_approval(state: AgentState, config: RunnableConfig):
            last = state["messages"][-1]
            pending = [f"{call['name']}({call['args']})" for call in last.tool_calls]
            if not approval_fn(pending):
                return {
                    "messages": [
                        ToolMessage(
                            content="工具调用被人工审批拒绝：请改用其他方式完成任务。",
                            tool_call_id=call["id"],
                        )
                        for call in last.tool_calls
                    ]
                }
            return await tools_node.ainvoke(state, config)

        tools_callable = tools_with_approval

    builder = StateGraph(AgentState)
    builder.add_node("memory", memory_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_callable)
    builder.add_edge(START, "memory")
    builder.add_edge("memory", "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "memory")

    return builder.compile(checkpointer=checkpointer), stats
