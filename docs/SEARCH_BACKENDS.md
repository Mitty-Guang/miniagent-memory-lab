# 联网能力：问题分层与业界方案（2026-09 调研）

起因：任务「今年的北京实习推荐」工具调用拿不到有用信息。本文记录**问题本质的拆层**、
**业界成熟方案**与**本项目的取舍**（面试可用来讲"我们知道成熟做法是什么、为什么这样选"）。

---

## 1. 问题分两层（不要混为一谈）

| 层 | 问题 | 我们的现状 | 业界对应方案 |
| --- | --- | --- | --- |
| **A. 工具/API 选择** | 面对很多工具/接口，选哪个、怎么组合 | 工具只有 5 个，原生 function calling 够用；`auto_budget` 已做"任务分类"的迷你路由 | Tool Retrieval（工具检索）、MCP、Agent 路由 |
| **B. 查询构造与结果质量** | 调对了工具，但查询词/结果不好 | **本次踩坑在这一层**：Bing 中文泛词分词退化 + 弱词误导相关性判断 + 平台词位置敏感 | 换 Agent-native 搜索 API、query rewriting、rerank |

本次故障复盘：`今年的北京实习推荐` → ① Bing 返回生肖黄历页（泛词退化）② 相关性判据被弱词
"今年"骗过（不触发重试）③ 兜底时平台词位置错误（`北京实习 实习僧` 无效，`实习僧 北京` 有效）。
修复见 `docs/ISSUES.md` U15。

---

## 2. 搜索 API：业界方案（2026 现状）

**关键背景**：Microsoft 已于 **2025-08-11 退役 Bing Search API**（公共端点返回 HTTP 410），
推荐迁移到 Azure AI Foundry 的 Grounding with Bing Search——这是"平台承诺"而非可直接替换的 API。
这也是本项目不得不走 HTML/RSS 抓取的根因，不是实现偷懒。

| 方案 | 定位 | 返回内容 | 价格（2026 公开价） | 备注 |
| --- | --- | --- | --- | --- |
| **Tavily** | Agent-native 检索 | 排名结果 + 摘要 + 可选答案 + `include_raw_content` 全文 | 免费 1000 credits/月；$8/1k（basic）、$16/1k（advanced） | 官方 MCP server；LangChain/LlamaIndex 官方集成；2026-02 被 Nebius 收购 |
| **Exa** | 神经/语义检索 | URL + 正文 + highlights | ~$7/1k（含 contents） | 概念型/探索型检索强 |
| **Brave Search API** | 独立索引（非 Google/Bing 镜像） | SERP JSON + `extra_snippets`，2026-02 新增 LLM Context 端点 | ~$5/1k；免费层 2025 年底取消（改 $5/月 credits） | 最快（~600ms）、隐私合规好；索引比 Google 小 |
| **Serper** | Google SERP JSON | 原始 SERP（organic/knowledgeGraph/answerBox） | ~$0.3–1/1k | 最便宜，但要自己做抽取/清洗 |
| **Perplexity Sonar** | 答案引擎 | 带引用的成稿答案 | ~$5–14/1k + tokens | 延迟数秒；适合"要答案"而不是"要原文" |
| **SearXNG（自建）** | 元搜索（可聚合 Bing/Google/…） | HTML/JSON | 自托管免费 | 需自己维护实例与解析；国内可自建 |

评测参考（第三方口径，注意厂商倾向）：Brave 在事实型查询准确率领先（94% vs Tavily 93%），
Tavily 在**难检索/多跳**任务上更强（任务完成 51% vs 38%；多跳 F1 41.1% vs 28%）——即
"要精准事实用 Brave、要 agent 研究循环用 Tavily"。

---

## 3. 工具/API 自动选择：业界方案

1. **原生 Function Calling**（`tool_choice=auto`）：工具少（≤20）时的默认解，本项目当前做法；
2. **Tool Retrieval（工具检索，RAG over tools）**：把工具名称/描述向量化 + 混合检索
   （BM25 + dense）+ cross-encoder 重排，每步只把 top-k 工具给模型。论文量化收益：
   - ToolScope（ACL 2026）：工具选择准确率 **+8.8%~38.6%**，上下文从 32.5k → 469 tokens（**-98.6%**）；
   - ITR（Instruction-Tool Retrieval）：每步 token **-95%**、工具路由准确率 **+32%**、整体成本 **-70%**；
   - DTDR（Findings of ACL 2026）：动态依赖感知检索，函数调用成功率 **+23%~104%**。
3. **MCP（Model Context Protocol）**：工具/API 的标准接入层（本项目已有 MCP server + client demo），
   生态里有 registry/gateway 做工具发现；MCP server 很多时用 **Tool-to-Agent Retrieval**
   这类方法做"工具级/Agent 级"联合路由（LiveMCPBench 上 Recall@5 +19.4%）。
4. **路由器/规划器**：小模型先做任务分类再选工具族（本项目 `auto_budget` 是这个模式的迷你版，
   可自然扩展成 tool router）；多智能体编排（本项目 `multi_agent.py` 的 Planner/Executor/Reviewer）。

---

## 4. 本项目的取舍（务实路线）

| 阶段 | 做法 | 理由 |
| --- | --- | --- |
| 现状（零成本、国内直连） | Bing RSS 解析 + 相关性过滤 + 自动缩短 + **平台词前置**兜底 | 无 API key、无代理也能跑；启发式覆盖已知退化模式 |
| 下一步（推荐） | 抽象 `SearchBackend` 接口：`bing_rss`（默认）/ `tavily` / `brave` / `serper`，key 走环境变量 | 保留零依赖默认值；有 key 时一键切到 agent-native 检索 |
| 再下一步 | 接 Tavily `include_raw_content` 替代 `http_get` 抓 JS 页面 | 直接解决官网 403 / JS 渲染抓不到正文的痛点 |
| 工具变多时 | 复用现有 `retrieval.py`（TF-IDF / bigram / embedding）做 **tool retrieval** | 组件已存在，索引工具描述即可；这也是"抽象复用"的加分点 |
| 网络注意 | Brave/Serper(Google)/Exa 在国内需代理，Tavily 需实测 | 国内演示场景下"可插拔 + 默认 Bing"更稳 |

---

## 5. 面试话术（30 秒版）

> 我们没把它当"换个更强模型"的问题，而是先做故障分层：**工具选择** vs **查询构造** vs **结果验收**。
> 工具选择层，业界的成熟做法是 tool retrieval（工具描述向量化 + 混合检索 + 重排，能把上下文降 95%+、
> 路由准确率提 30%+）和 MCP 标准化接入；查询与结果层，是换 agent-native 搜索 API（Tavily/Brave/Exa）
> 或做 query rewriting + rerank。我们的实现已经预留了可插拔检索接口（TF-IDF/bigram/embedding 三种
> retriever），把这套抽象平移到搜索后端上就是插件化改造——**先量化问题、再选方案**，而不是盲目换组件。
