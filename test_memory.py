"""离线单元测试：长期记忆 / tracing / HITL 审批 / MemoryAgent 读写（不调用 API）。

运行：
    .\\.venv\\Scripts\\python.exe test_memory.py
"""
import asyncio
from pathlib import Path

from mini_agent.approval import ApprovalToolCollection
from mini_agent.llm import LLMResponse
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_agent import MemoryAgent
from mini_agent.tracing import TraceLogger


def test_ltm_basic():
    ltm = LongTermMemory(":memory:")
    assert ltm.count() == 0
    ltm.add("项目代号是 ORION", session_id="s1")
    ltm.add("今天天气不错", session_id="s1")

    hits = ltm.search("项目代号是什么", k=2)
    assert hits and "ORION" in hits[0]["text"], hits

    hits_excluded = ltm.search("项目代号是什么", k=2, exclude_session="s1")
    assert hits_excluded == [], hits_excluded
    print("test_ltm_basic passed")


def test_trace_summary():
    trace = TraceLogger()
    trace.log("llm", {"prompt_tokens": 10, "completion_tokens": 5, "latency": 0.5})
    trace.log("tool", {"name": "python_execute"})
    trace.log("approval", {"tool": "bash_execute", "approved": False})
    summary = trace.summary()
    assert summary["llm_calls"] == 1, summary
    assert summary["tool_calls"] == 1, summary
    assert summary["rejected"] == 1, summary
    assert summary["prompt_tokens"] == 10, summary
    print("test_trace_summary passed")


async def test_approval():
    def deny_bash(name, args):
        return name != "bash_execute"

    tools = ApprovalToolCollection(approval_fn=deny_bash)
    allowed = await tools.execute_tool("python_execute", code="print(1)")
    assert allowed.success, allowed

    denied = await tools.execute_tool("bash_execute", command="echo hi")
    assert not denied.success, denied
    assert "审批拒绝" in denied.error, denied.error
    assert len(tools.decisions) == 2 and tools.decisions[1]["approved"] is False
    print("test_approval passed")


class MockLLM:
    """离线假模型：直接给最终答案，不调用工具。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, system_prompt=None, tools=None):
        self.calls += 1
        return LLMResponse(content="好的，已记住。")


class MockTeamLLM:
    """按 system_prompt 区分角色的假模型：规划 / 执行 / 审查。"""

    def __init__(self, fail_first: bool = False):
        self.calls = 0
        self.fail_first = fail_first
        self.reviews = 0

    async def chat(self, messages, system_prompt=None, tools=None):
        self.calls += 1
        prompt = system_prompt or ""
        if "规划" in prompt:
            return LLMResponse(content="1. 创建文件\n2. 写入内容")
        if "审查" in prompt:
            self.reviews += 1
            if self.fail_first and self.reviews == 1:
                return LLMResponse(content="FAIL\n结果不完整")
            return LLMResponse(content="PASS\n结果满足要求")
        return LLMResponse(content="完成了。")


async def test_multi_agent_orchestration():
    from mini_agent.multi_agent import MultiAgentTeam

    team = MultiAgentTeam(llm=MockTeamLLM(), policy="relevance", budget_chars=500)
    result = await team.run("创建 note.txt")
    assert team.plan, "planner 未产出计划"
    assert team.review.upper().startswith("PASS"), team.review
    assert result.strip() == "完成了。", result
    assert team.rounds == 1
    print("test_multi_agent_orchestration passed")


async def test_multi_agent_retry():
    from mini_agent.multi_agent import MultiAgentTeam

    team = MultiAgentTeam(llm=MockTeamLLM(fail_first=True), max_retries=1)
    await team.run("任务")
    assert team.rounds == 2, team.rounds
    print("test_multi_agent_retry passed")


def test_sandbox():
    import tempfile

    from mini_agent.sandbox import PathGuard, build_sandboxed_tools, is_blocked_command

    # 路径白名单：越界路径必须被拦截
    with tempfile.TemporaryDirectory() as root:
        resolved_root = Path(root).resolve()
        guard = PathGuard(str(resolved_root))
        target = guard.resolve("sub/a.txt")
        assert str(resolved_root) in str(target), target
        try:
            guard.resolve("../escape.txt")
            raise AssertionError("越界路径未被拦截")
        except PermissionError:
            pass

    # 命令黑名单
    assert is_blocked_command("rm -rf /") is not None
    assert is_blocked_command("echo hi") is None

    async def scenario():
        tools = {t.name: t for t in build_sandboxed_tools(".", timeout=1)}
        blocked = await tools["bash_execute"].execute(command="rm -rf /tmp/x")
        assert not blocked.success and "拦截" in blocked.error, blocked
        ok = await tools["bash_execute"].execute(command="echo sandbox-ok")
        assert ok.success and "sandbox-ok" in ok.output, ok
        py = await tools["python_execute"].execute(code="print(6 * 7)")
        assert py.success and "42" in py.output, py
        slow = await tools["python_execute"].execute(code="import time; time.sleep(3)")
        assert not slow.success and "超时" in slow.error, slow

    asyncio.run(scenario())
    print("test_sandbox passed")


async def test_memory_agent_read_write():
    ltm = LongTermMemory(":memory:")
    ltm.add("项目代号是 ORION", session_id="old-session")

    # 模式 A（message）：注入为独立 system 消息
    agent = MemoryAgent(
        llm=MockLLM(),
        ltm=ltm,
        session_id="new-session",
        task_id="t1",
        memory_injection="message",
    )
    await agent.run("请记住：项目代号是 ORION。")
    assert agent.retrieved, "未检索到历史记忆"
    assert any(
        "长期记忆" in (m.content or "") for m in agent.memory.messages
    ), "未注入长期记忆（message 模式）"

    # 模式 B（system_prompt，默认）：拼进系统提示词（运行中检查）
    seen = {}

    class SpyLLM(MockLLM):
        async def chat(self, messages, system_prompt=None, tools=None):
            seen["system_prompt"] = system_prompt or ""
            return await super().chat(messages, system_prompt=system_prompt, tools=tools)

    agent2 = MemoryAgent(
        llm=SpyLLM(), ltm=ltm, session_id="new-session-2", task_id="t2"
    )
    await agent2.run("请记住：项目代号是 ORION。")
    assert "长期记忆" in seen.get("system_prompt", ""), seen
    assert "ORION" in seen.get("system_prompt", ""), seen

    # write：任务结束后写入摘要（1 初始 + 2 次运行各 1 条）
    assert ltm.count() == 3, ltm.count()
    assert any("已记住" in item["text"] for item in ltm.all()), ltm.all()
    print("test_memory_agent_read_write passed")


def test_retrievers():
    from mini_agent.retrieval import (
        BigramRetriever,
        TfidfRetriever,
        get_retriever,
        tokenize,
    )

    texts = ["任务：项目代号是 ORION；结果：已记住", "今天天气不错", "报告目录改为 docs"]
    query = "项目代号是什么"

    tfidf_scores = TfidfRetriever().rank(query, texts)
    assert tfidf_scores[0] == max(tfidf_scores), tfidf_scores
    bigram_scores = BigramRetriever().rank(query, texts)
    assert bigram_scores[0] == max(bigram_scores), bigram_scores
    assert tokenize("项目 code 123"), "中英混排分词为空"
    assert isinstance(get_retriever(), TfidfRetriever)
    print("test_retrievers passed")


if __name__ == "__main__":
    test_ltm_basic()
    test_trace_summary()
    asyncio.run(test_approval())
    asyncio.run(test_memory_agent_read_write())
    asyncio.run(test_multi_agent_orchestration())
    asyncio.run(test_multi_agent_retry())
    test_sandbox()
    test_retrievers()
    print("\n✅ 所有离线测试通过（long-term memory / tracing / HITL / multi-agent / sandbox / retrieval）")
