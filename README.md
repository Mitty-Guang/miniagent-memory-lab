# MiniAgent Memory Lab · ReAct Agent 的记忆管理与评测平台

> 基于 [Jacob-liu1996/miniagent](https://github.com/Jacob-liu1996/miniagent)（MIT License）二次开发，
> 面向 LLM Agent 的**分层记忆 / 上下文管理 / 评测**实验平台。

手写 ReAct 循环 + 固定预算记忆选择 + SQLite 长期记忆 + 人工审批（HITL）+ tracing + 零依赖可视化 GUI，
并附带 18 个确定性任务的多预算评测、LangGraph 对照与多 Agent 对照实验。

## 功能亮点

| 模块 | 说明 |
| --- | --- |
| 基础 Agent | ReAct 循环（Thought → Action → Observation）、Function Calling（Python / 文件 / bash / 联网工具）、异常重试、max_steps 保险丝 |
| 联网工具 | `web_search`（Bing 搜索）+ `http_get`（公网抓取）：默认允许公网，内置 **SSRF 防护**（拒绝内网/本机/保留地址），可切换白名单模式（`WEB_ACCESS`） |
| 短期记忆 | 固定预算消息选择：`all / recent / relevance / impact` 四策略；**工具调用组原子化**选择；必保任务与最新状态 |
| 长期记忆 | SQLite 记忆库：任务摘要写入 + 跨会话检索注入；检索器**可插拔**（TF-IDF 默认 / 字符 Jaccard / fastembed 向量检索） |
| 决策影响分析 | leave-one-out 反事实重放 → 每条消息的决策影响先验（驱动 impact 策略） |
| 评测框架 | 21 个确定性任务（6 单步 + 8 多步 + **4 跨会话** + **3 同一会话多轮**）；4 档预算 × 3 策略网格；成功率/步数/调用数/token/耗时 |
| HITL 审批 | 工具执行前人工审批；拒绝时回填原因、模型自动改道（`scripts/demo_hitl.py`） |
| 执行沙箱 | 子进程 Python 执行 + 路径白名单 + 命令黑名单 + 超时（`mini_agent/sandbox.py`） |
| 可观测性 | `TraceLogger`：LLM / 工具 / 审批 / 记忆读写事件 + token / 延迟统计 |
| 可视化 | 交互式 GUI（实时轨迹 / 上下文选择 / 审批按钮 / token 面板）+ 实验监控面板 |
| 多 Agent | Planner → Executor → Reviewer 监督式协作（含失败反馈重试）与单 Agent 对照 |
| MCP | `extras/mcp/`：把沙箱工具包成标准 MCP Server（stdio），附客户端演示与接入配置 |
| 公开基准 | `benchmarks/mem2actbench/`：Mem2ActBench（ACL 2026）适配器——记忆驱动的工具调用，对比有/无记忆 |
| 对照实验 | 手写循环 vs LangGraph、单 Agent vs 多 Agent、跨模型（flash vs pro）、记忆注入方式消融 |

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # 填入任意 OpenAI 兼容服务的 key（不填则自动跳过真实调用）
```

```powershell
# 离线单测（不消耗 API）
.\.venv\Scripts\python.exe tests\test_policies.py
.\.venv\Scripts\python.exe tests\test_memory.py

# 冒烟测试：单阶段 + 跨会话任务（验证长期记忆/token 链路）
.\.venv\Scripts\python.exe scripts\smoke_test.py

# HITL 演示：危险工具先拒后放，模型自适应改用其他工具
.\.venv\Scripts\python.exe scripts\demo_hitl.py

# 一键实验：离线影响分析 + 四策略评测
.\.venv\Scripts\python.exe scripts\run_experiments.py

# 预算扫描：多预算 × 多策略的成功率/成本曲线
.\.venv\Scripts\python.exe scripts\run_sweep.py

# 单 Agent vs 多 Agent 对照
.\.venv\Scripts\python.exe scripts\compare_multi_agent.py --limit 8 --budget 800

# 跨模型对照（flash vs pro）
.\.venv\Scripts\python.exe scripts\compare_models.py --models deepseek-flash,deepseek-v4-pro --limit 6

# 记忆注入方式消融（system_prompt vs 独立 system 消息）
.\.venv\Scripts\python.exe scripts\ablation_injection.py --repeat 2 --budget 800 --policy impact

# MCP：把沙箱工具暴露为标准 MCP 工具（需 extras/mcp/requirements-extras.txt）
.\.venv\Scripts\python.exe extras\mcp\client_demo.py

# 公开基准：Mem2ActBench（记忆驱动的工具调用；需先下载数据，见 benchmarks/mem2actbench/README.md）
.\.venv\Scripts\python.exe benchmarks\mem2actbench\run_benchmark.py --limit 40 --top-k 3

# 可视化
.\.venv\Scripts\python.exe scripts\gui.py            # 交互式 Demo: http://127.0.0.1:8901
.\.venv\Scripts\python.exe scripts\monitor.py        # 实验监控: http://127.0.0.1:8899
```

> 可选向量检索：`pip install fastembed` 后把 `LongTermMemory(retriever=EmbeddingRetriever())`
> 或 `get_retriever("embedding")` 接入即可（首次使用会下载小模型）。

## 实验结论（摘要）

**预算扫描**（18 任务 × 4 预算 × 3 策略 = 216 次运行，成功率 **93.1%**）：

| 策略 | 预算 300 | 预算 500 | 预算 800 | 预算 1200 |
| --- | --- | --- | --- | --- |
| recent | 94.4% | 88.9% | 88.9% | 94.4% |
| relevance | 88.9% | 94.4% | **100%** | 94.4% |
| impact | 88.9% | 88.9% | 94.4% | **100%** |

三个发现：
1. **整体打平**：这批确定性任务上三种策略差异在噪声内；
2. **预算太紧反而更贵**：预算 300 时累计 token 最高（重试步数变多），1200 时最低；
3. **跨会话任务才是区分度所在**：长期记忆任务 recent 62.5% vs relevance / impact 81.3%。

**多 Agent 对照**（8 任务）：成功率同为 100%，但多 Agent 成本约 2×（调用 3.5→7.1、token 2628→4508、
耗时 3.35s→8.41s）——"多 Agent 不是免费的"。

**LangGraph 对照**：同一套记忆策略移植到 StateGraph（`extras/langgraph_compare/`），
成功率 100% vs 100%、成本同量级；增益在 checkpointer / interrupt / 可视化等框架原语。

**记忆注入方式消融**（4 个跨会话任务 × 2 次重复）：拼进系统提示词 **8/8**、
平均耗时 5.36s；独立 system 消息 7/8、13.75s——默认已切换为系统提示词注入。

**跨模型对照**（6 任务）：`deepseek-flash` 6/6、3994 tokens、5.76s；
`deepseek-v4-pro` 5/6、**1583 tokens**、27.87s——更强模型调用更少、token 仅 40%，
但延迟高且本批成功率未提升。

**向量检索**：`EmbeddingRetriever`（fastembed + bge-small-zh）冒烟通过；
国内可用 `HF_ENDPOINT=https://hf-mirror.com` 下载模型，默认仍是零依赖 TF-IDF。

**公开基准（Mem2ActBench，ACL 2026，40 题子集）**：记忆驱动的工具调用任务上，
本项目记忆管线把 Tool Accuracy 从 10% 提升到 **37.5%**、参数 F1 从 0.145 到 **0.421**
（同模型同 prompt，仅上下文不同；简化协议，详见 `benchmarks/mem2actbench/README.md`）。

## 目录结构

```
.
├── mini_agent/              # 核心包：ReAct 循环 / 工具 / 分层记忆 / HITL / 沙箱 / tracing / 多 Agent
│   └── config.py runner.py task_suite.py evaluate.py impact_analysis.py   # 评测管线模块
├── scripts/                 # 入口脚本：评测 / 对照实验 / GUI / 监控 / 演示
├── tests/                   # 离线测试（不消耗 API）
├── benchmarks/mem2actbench/ # 公开基准适配器（Mem2ActBench，ACL 2026）
├── extras/                  # langgraph_compare（LangGraph 对照）、mcp（MCP 接入）
├── docs/                    # 改动说明 + 源码深读 + 上游文档
└── results/                 # 实验数据（JSON）
```

## 文档

- **[SECONDARY_DEV.md](docs/SECONDARY_DEV.md)**：相对上游做了什么、如何复现、全部实验数据（含"预算与步数怎么定"）；
- **[STUDY_NOTES.md](docs/STUDY_NOTES.md)**：架构讲解、踩过的坑（工具调用组原子性 / 冷启动 / 失败样本污染 / ToolNode config）、面试问答；
- **[ISSUES.md](docs/ISSUES.md)**：问题记录（用户提出的问题 + 工程问题 + 环境问题 → 定位/修复/验证）；
- **[DEMOS.md](docs/DEMOS.md)**：7 个能体现本项目独特方法的演示（跨会话记忆 / 预算裁剪 / 影响度策略 / 联网鲁棒性 / 记忆干预 / HITL / 自动参数）；
- **[SEARCH_BACKENDS.md](docs/SEARCH_BACKENDS.md)**：联网能力的问题分层与业界方案（搜索 API 对比、tool retrieval、MCP）与本项目取舍；
- **[SEARCH_EVAL.md](docs/SEARCH_EVAL.md)**：搜索质量对照评估（30 条分层评估集；Bing RSS / DuckDuckGo / Tavily × 启发式开关）。

## 许可与致谢

本项目基于 [Jacob-liu1996/miniagent](https://github.com/Jacob-liu1996/miniagent) 开发，
保留其原始 [MIT License](LICENSE)；上游 README 见 [UPSTREAM_README.md](docs/UPSTREAM_README.md)。
