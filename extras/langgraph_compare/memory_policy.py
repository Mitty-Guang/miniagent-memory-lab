"""固定预算记忆选择（LangGraph 版）：与 miniagent 的 memory_policies 同一套策略。

- all / recent / relevance / impact 四种策略；
- 按“工具调用组”原子化选择（AIMessage(tool_calls) + 其全部 ToolMessage 整组进出），
  保证消息序列对 OpenAI 兼容接口合法；
- 必保：首条 HumanMessage（任务）与最新状态所在的组。
"""
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

DEFAULT_BUDGET_CHARS = 1200

_ROLE_MAP = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}


def bigrams(text: str) -> set:
    compact = "".join(ch.lower() for ch in text if not ch.isspace())
    if not compact:
        return set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)} or {compact}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def message_chars(msg) -> int:
    size = len(str(msg.content or ""))
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        size += len(str(tool_calls))
    return size


def impact_key(msg) -> str:
    role = _ROLE_MAP.get(getattr(msg, "type", ""), getattr(msg, "type", "unknown"))
    kind = "num" if any(ch.isdigit() for ch in str(msg.content or "")) else "txt"
    return f"{role}:{kind}"


def message_groups(messages: List[Any]) -> List[List[int]]:
    """按“工具调用组”切分：AIMessage(tool_calls) + 其后的 ToolMessage 为一组。"""
    groups: List[List[int]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                j += 1
            groups.append(list(range(i, j)))
            i = j
        else:
            groups.append([i])
            i += 1
    return groups


def repair_orphans(messages: List[Any]) -> List[Any]:
    """保证 API 合法：AIMessage 的 tool_calls 必须紧跟其全部 ToolMessage。"""
    kept: List[Any] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            call_ids = [call.get("id") for call in msg.tool_calls]
            responses = {}
            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                responses[messages[j].tool_call_id] = messages[j]
                j += 1
            if all(cid in responses for cid in call_ids):
                kept.append(msg)
                kept.extend(responses[cid] for cid in call_ids)
            i = j
        elif isinstance(msg, ToolMessage):
            i += 1  # 孤儿 tool 消息
        else:
            kept.append(msg)
            i += 1
    return kept


def _query_text(messages: List[Any]) -> str:
    for msg in messages:
        if isinstance(msg, HumanMessage):
            return str(msg.content or "")
    return ""


def _must_keep(messages: List[Any]) -> set:
    n = len(messages)
    keep = {0, n - 1}
    if isinstance(messages[n - 1], ToolMessage):
        last_id = getattr(messages[n - 1], "tool_call_id", None)
        for j in range(n - 2, -1, -1):
            msg = messages[j]
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                if any(call.get("id") == last_id for call in msg.tool_calls):
                    keep.add(j)
                    break
    return keep


def select_messages(
    messages: List[Any],
    policy: str = "relevance",
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    priors: Optional[Dict[str, float]] = None,
) -> List[Any]:
    priors = priors or {}
    n = len(messages)
    if n == 0:
        return []
    if policy == "all" or sum(message_chars(m) for m in messages) <= budget_chars:
        return list(messages)

    groups = message_groups(messages)
    must_keep = _must_keep(messages)
    query_bg = bigrams(_query_text(messages))

    def score(index: int) -> float:
        msg = messages[index]
        recency = index / max(1, n - 1)
        relevance = jaccard(bigrams(str(msg.content or "")), query_bg)
        if policy == "recent":
            return recency
        if policy == "relevance":
            return relevance
        prior = priors.get(impact_key(msg), 0.5)
        return 0.4 * relevance + 0.2 * recency + 0.4 * prior

    keep_groups = set()
    used = 0
    for gi, group in enumerate(groups):
        if any(i in must_keep for i in group):
            keep_groups.add(gi)
            used += sum(message_chars(messages[i]) for i in group)

    scored = sorted(
        ((max(score(i) for i in group), gi) for gi, group in enumerate(groups)),
        key=lambda item: item[0],
        reverse=True,
    )
    for _, gi in scored:
        if gi in keep_groups:
            continue
        size = sum(message_chars(messages[i]) for i in groups[gi])
        if used + size > budget_chars:
            continue
        keep_groups.add(gi)
        used += size

    selected = [messages[i] for gi in sorted(keep_groups) for i in groups[gi]]
    return repair_orphans(selected)
