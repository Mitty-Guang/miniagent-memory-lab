"""离线单元测试：验证记忆选择逻辑（不调用 API）。

运行：
    .\\.venv\\Scripts\\python.exe test_policies.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_agent.memory_policies import (
    BudgetedMemory,
    bigrams,
    jaccard,
    message_chars,
    repair_orphans,
)
from mini_agent.schema import Message


def make_messages():
    """构造一条典型的 ReAct 轨迹：user / assistant(tool_call) / tool / assistant。"""
    call = {"id": "c1", "type": "function", "function": {"name": "python_execute", "arguments": "{}"}}
    return [
        Message.user_message("计算 1 到 100 的和并写入结果文件，完成后告诉我。" + "任务" * 60),
        Message.assistant_message(content=None, tool_calls=[call]),
        Message.tool_message(content="5050" + "结果" * 80, tool_call_id="c1"),
        Message.assistant_message("已完成，结果是 5050。" + "总结" * 60),
    ]


def test_bigrams():
    assert jaccard(bigrams("abc"), bigrams("abc")) == 1.0
    assert jaccard(bigrams("abc"), bigrams("xyz")) == 0.0
    print("test_bigrams passed")


def test_repair_orphans():
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    msgs = [
        Message.user_message("hi"),
        Message.assistant_message(content=None, tool_calls=[call]),
        Message.tool_message(content="ok", tool_call_id="c1"),
        Message.tool_message(content="orphan", tool_call_id="c999"),
    ]
    kept = repair_orphans(msgs)
    assert len(kept) == 3, kept
    assert all(m.tool_call_id != "c999" for m in kept if m.role.value == "tool")
    print("test_repair_orphans passed")


def test_repair_incomplete_group_dropped():
    """assistant 有两个 tool_calls 但只保住一个响应 → 整组丢弃（防 400）。"""
    c1 = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    c2 = {"id": "c2", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    msgs = [
        Message.user_message("hi"),
        Message.assistant_message(content=None, tool_calls=[c1, c2]),
        Message.tool_message(content="r1", tool_call_id="c1"),
        Message.assistant_message(content="done"),
    ]
    kept = repair_orphans(msgs)
    assert [m.role.value for m in kept] == ["user", "assistant"], kept
    print("test_repair_incomplete_group_dropped passed")


def test_groups_atomic():
    """选择时工具调用组必须整组保留或整组丢弃（不同预算下都成立）。"""
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    msgs = [
        Message.user_message("任务" * 100),
        Message.assistant_message(content=None, tool_calls=[call]),
        Message.tool_message(content="结果" * 100, tool_call_id="c1"),
        Message.assistant_message("最终答案" * 100),
    ]

    def assert_valid(selected):
        for i, m in enumerate(selected):
            if m.role.value == "assistant" and m.tool_calls:
                ids = [c["id"] for c in m.tool_calls]
                following = selected[i + 1 : i + 1 + len(ids)]
                assert len(following) == len(ids), selected
                assert all(f.role.value == "tool" for f in following), selected
                assert {f.tool_call_id for f in following} == set(ids), selected

    for budget in (300, 900, 1400):
        memory = BudgetedMemory(policy="relevance", budget_chars=budget)
        memory.messages = msgs
        selected = memory._select(msgs)
        assert_valid(selected)
        print(f"  budget={budget}: roles={[m.role.value for m in selected]}")
    print("test_groups_atomic passed")


def test_budget_selection():
    messages = make_messages()
    memory = BudgetedMemory(policy="relevance", budget_chars=200)
    memory.messages = messages
    selected = memory._select(messages)
    total = sum(message_chars(m) for m in selected)
    # 允许超出预算的唯一来源是 must-keep 消息
    floor = max(
        message_chars(messages[0]) + message_chars(messages[-1]),
        sum(message_chars(m) for m in selected),
    )
    assert total <= max(200, floor), f"selected {total} chars > budget"
    assert selected[0].role.value == "user", "首条任务必须保留"
    assert selected[-1].role.value == "assistant", "最新状态必须保留"
    print(f"test_budget_selection passed (selected {total} chars)")


def test_tool_message_keeps_its_call():
    messages = make_messages()
    memory = BudgetedMemory(policy="recent", budget_chars=150)
    memory.messages = messages
    selected = memory._select(messages)
    ids = {m.tool_call_id for m in selected if m.role.value == "tool"}
    for msg in selected:
        if msg.role.value == "assistant" and msg.tool_calls:
            ids -= {c["id"] for c in msg.tool_calls}
    assert not ids, f"存在孤儿 tool 消息: {ids}"
    print("test_tool_message_keeps_its_call passed")


def test_full_context_untouched():
    messages = make_messages()
    memory = BudgetedMemory(policy="all", budget_chars=10)
    memory.messages = messages
    out = memory.get_messages()
    assert len(out) == len(messages), "policy=all 不应裁剪任何消息"
    print("test_full_context_untouched passed")


if __name__ == "__main__":
    test_bigrams()
    test_repair_orphans()
    test_repair_incomplete_group_dropped()
    test_budget_selection()
    test_tool_message_keeps_its_call()
    test_groups_atomic()
    test_full_context_untouched()
    print("\n✅ 所有离线测试通过")
