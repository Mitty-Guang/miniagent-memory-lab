"""二次开发模块：固定预算下的记忆选择（BudgetedMemory）。

上游 MiniAgent 的 Memory 只是一个无上限的消息列表，think() 时把全部历史塞给模型。
本模块提供 BudgetedMemory：在预算内按策略选择要放进上下文的消息，并保持与上游
接口完全兼容（get_messages 仍返回 OpenAI 消息格式），MiniAgent 无需改动。

三种策略（用于对比实验）：
- all       上游行为：不选择，全量上下文（无预算）
- recent    近因策略：保留最近的消息
- relevance 相关性策略：按与任务文本的字符二元组重叠度选择

> 曾实现 impact（决策影响）策略，实测无增益（跨会话 81.2% = 相关性 81.2%），
> 已移除；负结果与机制分析见 docs/SECONDARY_DEV.md「负结果记录」。

安全约束：任何 tool 消息必须保留其对应的 assistant tool_calls 消息，
否则 OpenAI 接口会报错（孤儿修复）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mini_agent.agent import MiniAgent
from mini_agent.schema import Memory, Message, Role

DEFAULT_BUDGET_CHARS = 1200


def bigrams(text: str) -> set:
    """字符二元组集合，用于轻量中英文相关性计算（不依赖分词库）。"""
    compact = "".join(ch.lower() for ch in text if not ch.isspace())
    if not compact:
        return set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)} or {compact}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def message_chars(msg: Message) -> int:
    """粗粒度 token 预算的代理指标：消息字符数。"""
    size = len(msg.content or "")
    if msg.tool_calls:
        size += len(str(msg.tool_calls))
    return size


def contains_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def repair_orphans(messages: List[Message]) -> List[Message]:
    """保证 API 合法性：assistant 的 tool_calls 必须紧跟其【全部】tool 响应。

    规则（比“丢弃孤儿 tool 消息”更严格，DeepSeek 官方 API 会校验）：
    - assistant(tool_calls) + 紧随其后的连续 tool 消息视为一个原子组；
    - 组内缺少任一响应 → 整组丢弃（否则报 400 insufficient tool messages）；
    - 没有前置 assistant 的 tool 消息 → 丢弃。
    """
    kept: List[Message] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.role == Role.ASSISTANT and msg.tool_calls:
            call_ids = [call.get("id") for call in msg.tool_calls]
            responses = {}
            j = i + 1
            while j < n and messages[j].role == Role.TOOL:
                responses[messages[j].tool_call_id] = messages[j]
                j += 1
            if all(cid in responses for cid in call_ids):
                kept.append(msg)
                kept.extend(responses[cid] for cid in call_ids)
            i = j
        elif msg.role == Role.TOOL:
            i += 1  # 孤儿 tool 消息
        else:
            kept.append(msg)
            i += 1
    return kept


def message_groups(messages: List[Message]) -> List[List[int]]:
    """按“工具调用组”切分消息：assistant(tool_calls) + 其 tool 响应为一组。"""
    groups: List[List[int]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.role == Role.ASSISTANT and msg.tool_calls:
            j = i + 1
            while j < n and messages[j].role == Role.TOOL:
                j += 1
            groups.append(list(range(i, j)))
            i = j
        else:
            groups.append([i])
            i += 1
    return groups


def to_openai_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    """Message 列表 -> OpenAI API 消息格式。

    注意：`content` 必须**始终存在**（可为空字符串）。曾因空内容时省略该字段，
    导致 assistant(tool_calls) 消息缺 `content` → 接口 422 `missing field content`。
    """
    result = []
    for msg in messages:
        item: Dict[str, Any] = {"role": msg.role.value, "content": msg.content or ""}
        if msg.tool_calls:
            item["tool_calls"] = msg.tool_calls
        if msg.tool_call_id:
            item["tool_call_id"] = msg.tool_call_id
        if msg.reasoning_content:
            item["reasoning_content"] = msg.reasoning_content   # 思考模式必须回传
        result.append(item)
    return result


class BudgetedMemory(Memory):
    """带预算与选择策略的记忆。"""

    policy: str = "all"
    budget_chars: int = DEFAULT_BUDGET_CHARS
    last_selection: Dict[str, Any] = {}

    def get_messages(self) -> List[Dict[str, Any]]:
        messages = list(self.messages)
        if self.policy == "all" or self._total_chars(messages) <= self.budget_chars:
            selected = messages
        else:
            selected = self._select(messages)
        # 供 GUI / tracing 观察本次选择结果
        self.last_selection = {
            "policy": self.policy,
            "budget_chars": self.budget_chars,
            "total_messages": len(messages),
            "selected_messages": len(selected),
            "total_chars": self._total_chars(messages),
            "selected_chars": self._total_chars(selected),
        }
        return self._to_openai(selected)

    # ---------- 内部实现 ----------
    def _total_chars(self, messages: List[Message]) -> int:
        return sum(message_chars(m) for m in messages)

    def _to_openai(self, messages: List[Message]) -> List[Dict[str, Any]]:
        return to_openai_messages(messages)

    def _query(self, messages: List[Message]) -> str:
        for msg in messages:
            if msg.role == Role.USER and msg.content:
                return msg.content
        return ""

    def _score(self, msg: Message, index: int, total: int, query_bg: set) -> float:
        recency = index / max(1, total - 1)
        relevance = jaccard(bigrams(msg.content or ""), query_bg)
        if self.policy == "recent":
            return recency
        return relevance

    def _must_keep(self, messages: List[Message]) -> set:
        """任务定义（首条用户消息）+ 最新状态（含其 assistant 调用）。"""
        n = len(messages)
        keep = {0, n - 1}
        if messages[n - 1].role == Role.TOOL:
            last_id = messages[n - 1].tool_call_id
            for j in range(n - 2, -1, -1):
                msg = messages[j]
                if (
                    msg.role == Role.ASSISTANT
                    and msg.tool_calls
                    and any(call.get("id") == last_id for call in msg.tool_calls)
                ):
                    keep.add(j)
                    break
        return keep

    def _select(self, messages: List[Message]) -> List[Message]:
        n = len(messages)
        query_bg = bigrams(self._query(messages))
        groups = message_groups(messages)
        must_keep = self._must_keep(messages)

        # 必保组：任务定义 / 最新状态所在的组，按整组保留
        keep_groups: set = set()
        used = 0
        for gi, group in enumerate(groups):
            if any(i in must_keep for i in group):
                keep_groups.add(gi)
                used += sum(message_chars(messages[i]) for i in group)

        # 组得分 = 组内最高分；整组进/整组出，避免拆散工具调用
        scored: List[Tuple[float, int]] = []
        for gi, group in enumerate(groups):
            score = max(self._score(messages[i], i, n, query_bg) for i in group)
            scored.append((score, gi))
        scored.sort(key=lambda item: item[0], reverse=True)

        for _, gi in scored:
            if gi in keep_groups:
                continue
            size = sum(message_chars(messages[i]) for i in groups[gi])
            if used + size > self.budget_chars:
                continue
            keep_groups.add(gi)
            used += size

        selected = [messages[i] for gi in sorted(keep_groups) for i in groups[gi]]
        return repair_orphans(selected)


class BudgetedMiniAgent(MiniAgent):
    """把 BudgetedMemory 注入上游 MiniAgent（不改动上游代码）。"""

    def __init__(
        self,
        llm,
        name: str = "BudgetedAgent",
        max_steps: int = 10,
        policy: str = "all",
        budget_chars: int = DEFAULT_BUDGET_CHARS,
        stop_check=None,
    ):
        super().__init__(llm=llm, name=name, max_steps=max_steps, stop_check=stop_check)
        self.memory = BudgetedMemory(
            policy=policy,
            budget_chars=budget_chars,
        )
