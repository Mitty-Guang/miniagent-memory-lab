"""离线单元测试：长期记忆 / tracing / HITL 审批 / MemoryAgent 读写（不调用 API）。

运行：
    .\\.venv\\Scripts\\python.exe test_memory.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import sqlite3
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


def test_web_tools():
    from mini_agent.web import HttpGetTool, WebSearchTool, is_allowed, is_private_host

    # SSRF 防护：内网/回环/元数据地址必须被拦截
    for host in ("127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.1.1", "::1"):
        assert is_private_host(host), host
    assert not is_private_host("8.8.8.8"), "8.8.8.8 应为公网"

    # 白名单模式：未列出的域名被拒；内网地址被拒（两种模式都拒绝）
    tool = HttpGetTool(access_mode="allowlist", allowed_domains=["wttr.in"])
    blocked = asyncio.run(tool.execute(url="https://example.com/x"))
    assert not blocked.success and "白名单" in blocked.error, blocked
    private = asyncio.run(tool.execute(url="http://127.0.0.1:8080/"))
    assert not private.success and "SSRF" in private.error, private
    bad_scheme = asyncio.run(tool.execute(url="file:///etc/passwd"))
    assert not bad_scheme.success and "http" in bad_scheme.error, bad_scheme

    # 搜索结果解析（离线：构造 Bing 风格 HTML）
    sample = (
        '<li class="b_algo"><h2><a href="https://a.com">标题A</a></h2><p>摘要 A</p></li>'
        '<li class="b_algo"><h2><a href="https://b.com">标题B</a></h2><p>摘要 B</p></li>'
    )
    parsed = WebSearchTool().parse_results(sample)
    assert len(parsed) == 2 and parsed[0]["url"] == "https://a.com", parsed
    assert "标题A" in parsed[0]["title"], parsed

    # RSS 解析（Bing format=rss 的结构）
    rss = (
        "<rss><channel><item><title>标题R</title>"
        "<link>https://r.example/a</link><description>摘要R</description></item></channel></rss>"
    )
    parsed_rss = WebSearchTool().parse_rss(rss)
    assert parsed_rss and parsed_rss[0]["url"] == "https://r.example/a", parsed_rss
    assert "摘要R" in parsed_rss[0]["snippet"], parsed_rss

    # 相关性过滤 + 自动缩短查询
    from mini_agent.web import (
        _query_tokens,
        _relevance_score,
        _shorten_query,
        _sort_by_relevance,
    )

    assert _shorten_query("诺坎普球场 参观") == "诺坎普球场", _shorten_query("诺坎普球场 参观")
    assert _shorten_query("诺坎普球场参观") == "诺坎普球场", _shorten_query("诺坎普球场参观")
    assert _shorten_query("Camp Nou visit") == "Camp Nou", _shorten_query("Camp Nou visit")
    tokens = _query_tokens("诺坎普球场 参观")
    junk = {"title": "诺（汉字）_百度百科", "url": "https://x", "snippet": "诺，应也"}
    good = {"title": "诺坎普 - 百度百科", "url": "https://y", "snippet": "Spotify Camp Nou 球场"}
    assert _relevance_score(junk, tokens) == 0, _relevance_score(junk, tokens)
    ranked = _sort_by_relevance([junk, good], tokens)
    assert ranked and ranked[0]["title"].startswith("诺坎普"), ranked
    assert len(ranked) == 1, ranked  # 0 分噪声被剔除
    assert is_allowed("https://sub.wttr.in/x") and not is_allowed("https://example.com")
    print("test_web_tools passed")


def test_memory_browser():
    """GUI 记忆浏览器的数据层：读取 / 过滤 / 删除（用临时库，不动真实记忆）。"""
    import tempfile

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    import gui  # noqa: E402  （gui.py 仅定义，不起服务）

    tmp = Path(tempfile.mkdtemp(prefix="gui_mem_")) / "ltm.sqlite3"
    original = gui.ltm_path
    gui.ltm_path = lambda: tmp
    try:
        ltm = LongTermMemory(path=str(tmp))
        ltm.add("用户的项目代号是 ORION", session_id="s1", kind="fact")
        ltm.add("所有报告放在 reports 目录", session_id="s1", kind="fact")
        ltm.close()

        data = gui._read_memory()
        assert data["total"] == 2, data
        assert data["items"][0]["id"] == 2, data  # 按写入时间倒序
        filtered = gui._read_memory("ORION")
        assert filtered["total"] == 1 and "ORION" in filtered["items"][0]["text"], filtered
        assert gui._read_memory("不存在的关键词")["total"] == 0

        # 删除（模拟 /api/memory_delete 的 SQL 行为）
        conn = sqlite3.connect(str(tmp))
        deleted = conn.execute("DELETE FROM memories WHERE id = ?", (2,)).rowcount
        conn.commit()
        conn.close()
        assert deleted == 1 and gui._read_memory()["total"] == 1
    finally:
        gui.ltm_path = original
    print("test_memory_browser passed")


def test_auto_budget():
    """自动参数估计：规则兜底 / JSON 解析 / 安全夹逼 / 模型失败回退。"""
    from mini_agent.auto_budget import estimate, parse_plan, rule_based

    assert rule_based("创建三个文件并复制到 backup 目录")["category"] == "多步文件"
    assert rule_based("创建 reports/project.txt，内容写我的项目代号")["category"] == "跨会话"
    assert rule_based("上次说的偏好是什么")["category"] == "跨会话"
    assert rule_based("搜索 Python 3.12 新特性")["category"] == "联网研究"
    assert rule_based("计算 1+1")["budget"] == 300

    plan = parse_plan(
        '```json\n{"category":"联网研究","budget":1000,"max_steps":20,"reason":"需检索"}\n```'
    )
    assert plan and plan["budget"] == 1000 and plan["source"] == "llm", plan
    assert parse_plan("这不是 JSON") is None
    assert parse_plan('{"budget": 50, "max_steps": 3}') is None   # 超出安全区间 → 拒绝
    assert parse_plan('{"budget": "abc", "max_steps": 8}') is None

    class FakeLLM:
        def __init__(self, content=None, boom=False):
            self.content = content
            self.boom = boom

        async def chat(self, messages, system_prompt=None, tools=None):
            if self.boom:
                raise RuntimeError("network down")
            return type("R", (), {"content": self.content})()

    ok = asyncio.run(
        estimate(
            "规划一次看球路线",
            FakeLLM('{"category":"联网研究","budget":900,"max_steps":18,"reason":"需要实时信息"}'),
        )
    )
    assert ok["source"] == "llm" and ok["max_steps"] == 18, ok
    fallback = asyncio.run(estimate("规划一次看球路线", FakeLLM(boom=True)))
    assert fallback["source"] == "rule" and "回退" in fallback["reason"], fallback
    print("test_auto_budget passed")


def test_chat_continuation():
    """连续对话：同一 agent 连跑两轮，第二轮应看到第一轮的消息（短时记忆延续）。"""
    from mini_agent.memory_agent import MemoryAgent

    agent = MemoryAgent(
        llm=MockLLM(),
        ltm=LongTermMemory(path=":memory:"),
        session_id="chat-test",
        policy="relevance",
        budget_chars=1200,
        max_steps=3,
    )

    async def scenario():
        await agent.run("记住：我的项目代号是 ORION")
        first_turn = len(agent.memory.messages)
        await agent.run("按我的偏好把 10÷3 写成报告")
        return first_turn

    first_turn = asyncio.run(scenario())
    messages = agent.memory.messages
    assert len(messages) > first_turn, (len(messages), first_turn)  # 消息累积（上下文延续）
    contents = [str(m.content or "") for m in messages]
    assert any("ORION" in c for c in contents), contents          # 第一轮内容仍在上下文里
    assert any("10÷3" in c for c in contents), contents           # 第二轮任务已追加

    # 单次运行（新会话）对照：新 agent 不携带上一轮消息
    fresh = MemoryAgent(
        llm=MockLLM(), ltm=LongTermMemory(path=":memory:"), session_id="single", max_steps=3
    )
    asyncio.run(fresh.run("另一个任务"))
    assert len(fresh.memory.messages) == 3, len(fresh.memory.messages)  # user + assistant + ...
    print("test_chat_continuation passed")


def test_openai_message_shape():
    """回归：所有消息必须带 content 字段（空内容也要有），否则接口 422 missing field content。"""
    from mini_agent.memory_policies import to_openai_messages
    from mini_agent.schema import Memory, Message

    messages = [
        Message.user_message("任务"),
        Message.assistant_message(
            content="",
            tool_calls=[{"id": "c1", "function": {"name": "bash_execute", "arguments": "{}"}}],
            reasoning_content="我先看看有哪些文件",
        ),
        Message.tool_message(content="ok", tool_call_id="c1"),
        Message.assistant_message(content="完成"),
    ]
    for item in to_openai_messages(messages):
        assert "content" in item, item
    assert to_openai_messages(messages)[1]["content"] == ""
    # 思考模式的思维链必须回传
    assert to_openai_messages(messages)[1]["reasoning_content"] == "我先看看有哪些文件"

    memory = Memory()
    for msg in messages:
        memory.add_message(msg)
    for item in memory.get_messages():
        assert "content" in item, item

    # as_chat_message：外部传入的 dict 缺 content 时自动补空串
    from mini_agent.config import as_chat_message

    fixed = as_chat_message({"role": "assistant", "tool_calls": [{"id": "x"}]})
    assert fixed["content"] == "" and "tool_calls" in fixed, fixed
    print("test_openai_message_shape passed")


def test_langgraph_message_repair():
    """回归：LangGraph 版必须补齐"所有"悬空 tool_calls（≥2 组），否则接口 400。（缺依赖则跳过）"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extras" / "langgraph_compare"))
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        import multi_agent_graph as lag
    except Exception as exc:
        print(f"test_langgraph_message_repair skipped（{type(exc).__name__}: {exc}）")
        return

    messages = [
        HumanMessage(content="任务"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "bash_execute", "args": {}}]),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "bash_execute", "args": {}}]),
        HumanMessage(content="[审查意见] FAIL"),
    ]
    fixed = lag._repair_dangling_tool_calls(messages)
    answered = {getattr(m, "tool_call_id", None) for m in fixed if isinstance(m, ToolMessage)}
    assert answered == {"c1", "c2"}, answered
    # 两组的占位应分别插在各自 assistant 消息之后（顺序合法）
    order = [type(m).__name__ for m in fixed]
    ai_indices = [i for i, name in enumerate(order) if name == "AIMessage"]
    tool_index = order.index("ToolMessage")
    assert tool_index < ai_indices[1], order
    print("test_langgraph_message_repair passed")


if __name__ == "__main__":
    test_ltm_basic()
    test_trace_summary()
    asyncio.run(test_approval())
    asyncio.run(test_memory_agent_read_write())
    asyncio.run(test_multi_agent_orchestration())
    asyncio.run(test_multi_agent_retry())
    test_sandbox()
    test_retrievers()
    test_web_tools()
    test_memory_browser()
    test_auto_budget()
    test_chat_continuation()
    test_openai_message_shape()
    test_langgraph_message_repair()
    print("\n✅ 所有离线测试通过（记忆 / tracing / HITL / multi-agent / sandbox / 检索 / 联网 / 记忆浏览器 / 自动参数 / 连续对话 / 消息格式 / LangGraph 消息修复）")
