"""LangGraph 版多 Agent 团队（supervisor 单图模式）+ checkpointer + interrupt。

与主项目手写 `MultiAgentTeam`（Planner → Executor → Reviewer）做成对照，覆盖 LangGraph 核心原语：

1. **supervisor 单图**：planner / approval / executor / reviewer / finish 都是同一张 `StateGraph`
   上的节点，条件边决定流转（生产上比"子图嵌套"更常用；子图示例见 `subgraph_demo.py`）；
2. **checkpointer**：`thread_id` 维度的状态持久化 → 多轮会话（同一线程连续 invoke 可延续历史）；
   `AsyncSqliteSaver`（跨进程）优先，缺依赖时回退 `InMemorySaver`；
3. **interrupt**：工具级人工审批——执行敏感工具（bash）前 `interrupt()` 暂停图，
   外部用 `Command(resume={"approved": ...})` 恢复；拒绝时把"审批拒绝"回灌给模型，
   与主项目 `approval_fn` 语义一致（拒绝后模型自动改道）；
4. **条件边 + 循环**：reviewer FAIL → 回到 executor 重试（带审查意见），并有步数上限兜底。

> 环境要求：`interrupt()` 在异步上下文里需要 **Python ≥3.11**（本仓库用 `.venv312` 跑 extras）。
"""
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agent_graph import TOOLS  # 复用同一套工具（与主项目 mini_agent/tools.py 相同实现）
from llm_factory import get_llm

PLANNER_PROMPT = (
    "你是任务规划者。把用户任务拆解成不超过 5 步的可执行步骤清单，"
    "只输出步骤本身，不要调用任何工具，不要执行任务。"
)
EXECUTOR_PROMPT = (
    "你是任务执行者。按给定的步骤计划使用工具完成任务；"
    "每次只做当前最必要的一步，根据工具结果决定下一步；完成后直接给出结果。"
)
REVIEWER_PROMPT = (
    "你是任务审查者。判断执行结果是否满足任务要求："
    "第一行只输出 PASS 或 FAIL，第二行给出不超过 50 字的原因。不要调用工具。"
)


class TeamState(TypedDict, total=False):
    task: str
    plan: str
    messages: Annotated[List[AnyMessage], add_messages]
    review: str
    rounds: int          # 已完成审查的轮次
    steps: int           # executor 的 ReAct 步数
    approved: bool
    final: str
    log: Annotated[List[str], lambda a, b: (a or []) + (b or [])]

    sensitive_tools: List[str]
    memory_block: str    # planner 检索到的长期记忆（executor/reviewer 也可见）


def _repair_dangling_tool_calls(messages: List[AnyMessage]) -> List[AnyMessage]:
    """OpenAI 契约：每个 assistant(tool_calls) 都要有对应 tool 响应。

    步数上限 / 审批中断 / 异常都可能留下"悬空调用"，直接发下一轮会 400
    （insufficient tool messages following tool_calls message）。
    这里按顺序扫描全部消息，为未响应的 tool_call_id 追加占位 tool 消息
    （与主项目 memory_policies.repair_orphans 同源思路）。
    """
    repaired: List[AnyMessage] = []
    open_calls: Dict[str, bool] = {}

    def flush_open() -> None:
        for call_id, answered in list(open_calls.items()):
            if not answered and call_id:
                repaired.append(
                    ToolMessage(content="未执行（达到步数上限或已中断）。", tool_call_id=call_id)
                )
        open_calls.clear()

    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            flush_open()   # 先补齐"上一组"悬空调用（插在该组之后、这条消息之前）
            repaired.append(msg)
            open_calls.update({call.get("id"): False for call in msg.tool_calls})
            continue
        if isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", None)
            if call_id in open_calls:
                open_calls[call_id] = True
        repaired.append(msg)
    flush_open()
    return repaired


def make_checkpointer_sync_fallback():
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


async def make_checkpointer(path: str = "", sensitive_tools: Optional[List[str]] = None):
    """优先 SQLite（跨进程持久化），不可用回退内存；异步图必须用 AsyncSqliteSaver。"""
    if path:
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            conn = await aiosqlite.connect(path)
            return AsyncSqliteSaver(conn)
        except Exception as exc:  # pragma: no cover
            print(f"[warn] AsyncSqliteSaver 不可用（{exc}），回退 InMemorySaver")
    return make_checkpointer_sync_fallback()


def build_team(
    checkpointer=None,
    max_retries: int = 1,
    max_steps: int = 6,
    require_approval: bool = True,
    sensitive_tools: Optional[List[str]] = None,
    ltm=None,
    session_id: str = "",
    retrieved_sink: Optional[list] = None,
):
    """构建 supervisor 图。

    require_approval=False 时跳过计划审批（纯性能对照用）；
    sensitive_tools 默认 {"bash_execute"}（工具级 interrupt 审批）；
    ltm 传入 LongTermMemory 时：planner 检索历史记忆注入提示词、finish 写回本轮摘要
    （与主项目 MemoryAgent 的"写 → 读 → 用"链路对齐）。
    """
    sensitive = set(sensitive_tools if sensitive_tools is not None else {"bash_execute"})
    retrieved: List[dict] = retrieved_sink if retrieved_sink is not None else []
    llm = get_llm()
    llm_with_tools = llm.bind_tools(TOOLS)
    tool_map = {t.name: t for t in TOOLS}

    # —— intake：把本轮用户输入写入消息流（多轮会话时历史里才有"上一轮用户原话"）——
    async def intake_node(state: TeamState) -> Dict[str, Any]:
        return {
            "messages": [HumanMessage(content=state["task"])],
            "log": [f"[intake] 接收：{state['task'][:24]}"],
        }

    # —— planner ——
    async def planner_node(state: TeamState) -> Dict[str, Any]:
        memory_block = ""
        if ltm is not None:
            hits = ltm.search(state["task"], k=3, exclude_session=session_id or None)
            retrieved.clear()
            retrieved.extend(hits)
            if hits:
                memory_block = "\n\n[长期记忆] 与此任务相关的历史记忆：\n" + "\n".join(
                    f"- {h['text']}" for h in hits
                )
        resp = await llm.ainvoke(
            [
                SystemMessage(content=PLANNER_PROMPT + memory_block),
                HumanMessage(content=state["task"]),
            ]
        )
        plan = (resp.content or "").strip()
        log_line = f"[planner] {len(plan.splitlines())} 步计划"
        if memory_block:
            log_line += f"（注入 {len(retrieved)} 条长期记忆）"
        return {"plan": plan, "memory_block": memory_block, "log": [log_line]}

    # —— 计划审批（interrupt）——
    async def approval_node(state: TeamState) -> Dict[str, Any]:
        decision = interrupt({"plan": state.get("plan", ""), "ask": "是否批准该计划并开始执行？"})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        return {"approved": approved, "log": [f"[approval] {'批准' if approved else '拒绝'}"]}

    async def auto_approval_node(state: TeamState) -> Dict[str, Any]:
        return {"approved": True, "log": ["[approval] 自动放行"]}

    # —— executor：ReAct（agent → tools → agent…）——
    async def executor_agent_node(state: TeamState) -> Dict[str, Any]:
        history = _repair_dangling_tool_calls(list(state.get("messages", []))[-max_steps * 3 :])
        prompt = [
            SystemMessage(content=EXECUTOR_PROMPT + (state.get("memory_block") or "")),
            HumanMessage(content=f"任务：{state['task']}\n\n计划：\n{state.get('plan', '')}"),
        ]
        resp = await llm_with_tools.ainvoke(prompt + history)
        return {"messages": [resp], "steps": int(state.get("steps", 0)) + 1}

    async def executor_tools_node(state: TeamState) -> Dict[str, Any]:
        last = state["messages"][-1]
        outputs: List[AnyMessage] = []
        for call in getattr(last, "tool_calls", []) or []:
            name = call["name"]
            args = call.get("args") or {}
            if name in sensitive:
                # —— LangGraph 原语：interrupt 暂停，等待外部 Command(resume=...) ——
                decision = interrupt({"tool": name, "args": args, "ask": "是否批准该工具调用？"})
                approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
                if not approved:
                    outputs.append(
                        ToolMessage(
                            content=f"审批拒绝：{name} 未获批准。请改用其他工具或调整方案后继续。",
                            tool_call_id=call["id"],
                        )
                    )
                    continue
            try:
                content = await tool_map[name].ainvoke(args)
            except Exception as exc:
                content = f"错误: {exc}"
            outputs.append(ToolMessage(content=str(content), tool_call_id=call["id"]))
        return {"messages": outputs}

    def after_executor(state: TeamState) -> str:
        last = state["messages"][-1] if state.get("messages") else None
        has_calls = bool(getattr(last, "tool_calls", None))
        if has_calls and int(state.get("steps", 0)) < max_steps:
            return "tools"
        if has_calls:
            return "flush"      # 步数上限：把未执行的调用占位，保持消息契约合法
        return "reviewer"

    async def flush_pending_node(state: TeamState) -> Dict[str, Any]:
        last = state["messages"][-1] if state.get("messages") else None
        outputs: List[AnyMessage] = []
        for call in getattr(last, "tool_calls", []) or []:
            outputs.append(
                ToolMessage(content="未执行（达到步数上限）。", tool_call_id=call["id"])
            )
        return {"messages": outputs, "log": ["[flush] 步数上限：未执行的工具调用已占位"]}

    # —— reviewer ——
    async def reviewer_node(state: TeamState) -> Dict[str, Any]:
        last_ai = ""
        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
                last_ai = msg.content
                break
        resp = await llm.ainvoke(
            [
                SystemMessage(content=REVIEWER_PROMPT),
                HumanMessage(content=f"任务：{state['task']}\n\n执行结果：\n{last_ai}"),
            ]
        )
        review = (resp.content or "").strip()
        rounds = int(state.get("rounds", 0)) + 1
        return {
            "review": review,
            "rounds": rounds,
            "steps": 0,                       # 下一轮重新计数
            "messages": [HumanMessage(content=f"[审查意见] {review}")],
            "log": [f"[reviewer] 第 {rounds} 轮：{review.splitlines()[0][:20]}"],
        }

    def after_review(state: TeamState) -> str:
        review = (state.get("review") or "").upper()
        need_retry = review.startswith("FAIL") and int(state.get("rounds", 0)) <= max_retries
        return "executor" if need_retry else "finish"

    # —— finish ——
    async def finish_node(state: TeamState) -> Dict[str, Any]:
        final = ""
        for msg in reversed(state.get("messages", [])):
            if isinstance(msg, AIMessage) and msg.content and not getattr(msg, "tool_calls", None):
                final = msg.content
                break
        if not state.get("approved"):
            final = f"已取消执行（人工拒绝）。计划为：\n{state.get('plan', '')}"
        final = final or state.get("plan", "")
        log_line = "[finish] 结束"
        if ltm is not None and final and "LLM调用失败" not in final:
            memory_id = ltm.add(
                f"任务：{state['task'][:120]}；结果：{final.strip()[:160]}",
                session_id=session_id,
                kind="summary",
            )
            log_line += f"（已写入长期记忆 #{memory_id}）"
        return {"final": final, "log": [log_line]}

    builder = StateGraph(TeamState)
    builder.add_node("intake", intake_node)
    builder.add_node("planner", planner_node)
    builder.add_node("approval", approval_node if require_approval else auto_approval_node)
    builder.add_node("executor", executor_agent_node)
    builder.add_node("tools", executor_tools_node)
    builder.add_node("flush", flush_pending_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("finish", finish_node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "planner")
    builder.add_edge("planner", "approval")
    builder.add_conditional_edges(
        "approval",
        lambda s: "executor" if s.get("approved") else "finish",
        {"executor": "executor", "finish": "finish"},
    )
    builder.add_conditional_edges(
        "executor", after_executor, {"tools": "tools", "flush": "flush", "reviewer": "reviewer"}
    )
    builder.add_edge("tools", "executor")
    builder.add_edge("flush", "reviewer")
    builder.add_conditional_edges(
        "reviewer", after_review, {"executor": "executor", "finish": "finish"}
    )
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)
