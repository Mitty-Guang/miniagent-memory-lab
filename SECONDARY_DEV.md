# 二次开发说明（基于 miniagent）

> 上游项目：Jacob-liu1996/miniagent（190 stars, MIT License）
> 本文档说明本仓库相对上游做了什么、为什么做、如何复现实验。

## 1. 上游是什么

一个约 600 行的教学向 ReAct Agent 框架：

- `mini_agent/agent.py` — ReAct 主循环（think → act），最多 max_steps 步
- `mini_agent/llm.py` — OpenAI 兼容的 LLM 客户端（支持 base_url）
- `mini_agent/tools.py` — 三个工具：python_execute / file_editor / bash_execute
- `mini_agent/schema.py` — Message / Memory / AgentState 数据结构

上游的 `Memory` 是一个**无上限的消息列表**，每步都把全部历史塞进上下文。

## 2. 本次二次开发做了什么

### 2.1 部署适配（`main_mini.py`）

- 上游硬编码了 `api_key`，且未暴露 `base_url`，无法接非 OpenAI 官方服务；
- 改为从环境变量 / `.env` 读取 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME`；
- 已在本机接 OpenAI 兼容网关跑通全部示例（当前配置：DeepSeek 官方 API，模型 `deepseek-flash`）。

### 2.2 固定预算记忆选择（`mini_agent/memory_policies.py`）

核心新增模块，与上游接口完全兼容（`get_messages()` 仍返回 OpenAI 消息格式），
不改动上游 `agent.py`。四种策略：

| 策略 | 选择依据 | 说明 |
| --- | --- | --- |
| `all` | 不选择 | 上游行为，作为无预算上界参照 |
| `recent` | 近因 | 只保留最近的消息 |
| `relevance` | 相关性 | 与任务文本的字符二元组 Jaccard 重叠度 |
| `impact` | 决策影响 | 0.4×相关性 + 0.2×近因 + 0.4×离线干预测得的决策影响先验 |

实现要点：

1. **预算**：用消息字符数作为 token 预算的代理指标（避免引入 tokenizer 依赖）；
2. **必保消息**：首条用户消息（任务定义）+ 最新状态（含其 assistant 调用消息）；
3. **孤儿修复**：任何 tool 消息必须保留其对应的 assistant `tool_calls` 消息，
   否则 OpenAI 接口报错——这是实现过程中实际踩到的坑（见 STUDY_NOTES.md）。

### 2.3 离线决策影响分析（`impact_analysis.py`）

对应我的研究项目 memcausal 的方法论：**不按“检索相关性”，按“对决策的实际影响”给记忆打分**。

- 用 `policy="all"` 跑任务，记录完整轨迹与最终答案；
- 对每条记忆做 leave-one-out 干预：删掉它，让模型仅基于剩余历史重新作答；
- 反事实答案与原答案的相似度（文本二元组 + 数字一致性）→ `impact = 1 - similarity`；
- 按 `impact_key`（角色 + 是否含数字）聚合为**决策影响先验表** `results/impact_priors.json`，
  供在线 `impact` 策略使用。

### 2.4 评测框架（`task_suite.py` / `runner.py` / `evaluate.py` / `run_experiments.py`）

- 6 个可确定性判分的小任务（计算 / 文件写入 / 多步读写 / JSON 处理）；
- 每个任务在独立临时目录中执行（工具用相对路径，隔离副作用）；
- 指标：成功率、平均步数、平均 LLM 调用数、平均上下文字符数、平均耗时；
- 结果落盘 `results/eval_*.json` 并打印 Markdown 对比表。

### 2.5 工程健壮性

- `CountingLLM` 对失败调用做指数退避重试并打印可见日志（上游会把 API 异常吞成
  一条错误文本，导致 Agent 静默提前结束——实测冷启动首调用与网关超时时都出现过）；
- 启动时做一次预热调用（warmup），规避冷启动首个请求失败；
- 工具调用组原子性：`message_groups()` + `repair_orphans()` 两层防线，
  解决“孤儿 tool 消息”和“不完整工具调用组”两类 API 400 错误；
- 失败样本不污染统计：干预失败打标并剔除，全量失败的任务跳过分析；
- 任务间加 1s 间隔，降低限流风险；
- 离线单测 `test_policies.py`：二元组相似度、孤儿修复、不完整组丢弃、
  预算约束、组原子性、全量不裁剪。

### 2.6 长期记忆层（2026-09-23 新增）

`mini_agent/long_term_memory.py` + `mini_agent/memory_agent.py`：

- **write**：任务结束后把「任务 + 最终结果」压缩成一条记忆写入 SQLite（可跨进程持久化）；
- **read**：新任务开始时用任务文本检索历史记忆，按分数取 Top-k 注入上下文；
  检索器**可插拔**（`mini_agent/retrieval.py`）：默认 `TfidfRetriever`（中英混排分词 + TF-IDF，零依赖），
  另有 `BigramRetriever`（早期对照）与 `EmbeddingRetriever`（装 fastembed 即启用向量检索）；
  默认排除当前会话（保证测的是跨会话记忆）；
- 分层设计：**短期** = 固定预算的消息选择（已有），**长期** = SQLite 记忆库（新增）；
- 记忆注入方式可切换：拼进系统提示词（默认，消融更优）或独立 system 消息（见 6.6）；
- 离线测试覆盖 read（检索并注入，两种注入模式都验证）与 write（摘要落库）。

### 2.7 任务集（21 个任务）

`task_suite.py` 从 6 个扩展到 **18 个**：

| 类型 | 数量 | 说明 |
| --- | --- | --- |
| 单步任务 | 6 | 原有（计算 / 文件 / JSON） |
| 多步任务 | 8 | 4-6 次工具调用（多文件求和、重命名、均值、反转字符串等） |
| **跨会话任务** | 4 | 2 个阶段：phase 1 教学（如"项目代号是 ORION"）、phase 2 使用；两阶段各自新会话、共享长期记忆库 |

跨会话任务用于验证"长期记忆是否真的被写入、并在新会话中被检索使用"。

### 2.8 HITL 工具审批（2026-09-23 新增）

`mini_agent/approval.py`：`ApprovalToolCollection` 在工具执行前调用审批回调，
拒绝时把结构化原因回填给模型（模型会改用其他工具，而不是流程中断）。
`demo_hitl.py` 演示：bash 被拒 → 模型自动改用 file_editor 完成同一任务。

### 2.9 Tracing 与 token/成本统计（2026-09-23 新增）

- `mini_agent/tracing.py`：LLM 调用 / 工具执行 / 审批事件写入 JSONL（含 token、延迟）；
- `CountingLLM` 直接调用兼容客户端以捕获 `usage`（prompt / completion tokens）与单次延迟；
- `trace.summary()` 输出：调用数、工具数、审批数 / 拒绝数、token、平均 / 最大延迟。

### 2.10 预算扫描实验（2026-09-23 新增）

`run_sweep.py`：预算 × 策略网格（默认 300/500/800/1200 × recent/relevance/impact），
输出各格的成功率、平均步数、累计 token/字符，落盘 `results/sweep_*.json`。

### 2.11 执行沙箱与权限（2026-09-23 新增）

`mini_agent/sandbox.py`：

- `SandboxedPythonExecutor`：子进程执行（超时 + cwd 限制 + 输出截断），替代进程内 exec；
- `SandboxedFileEditor`：`PathGuard` 把文件操作限制在沙箱根目录（防 `../` 逃逸）；
- `SandboxedBashExecutor`：危险命令黑名单（`rm -rf` / `del /f` / `format` / 递归删除等）+ 超时；
- 通过 `MemoryAgent(sandbox=True)` 或 `ApprovalToolCollection(sandbox_root=...)` 启用。

> 诚实边界：这是"进程级"沙箱，不是容器级隔离；生产环境需叠加容器/低权限用户/网络隔离。

### 2.12 同一会话多轮任务（2026-09-23 新增）

`task_suite.py` 新增 3 个 `turns` 型任务（同一 Agent 连续多轮、短期记忆累积）：
文件链式操作、增量追加与计数、**带更正的指令跟随**（reports → docs）。
`runner.py` 统一支持三种形态：单阶段 / phases（跨会话）/ turns（同一会话多轮）。

### 2.13 跨模型对照（2026-09-23 新增）

`compare_models.py`：同一批任务、同一策略下顺序切换 `MODEL_NAME`
（如 `deepseek-flash` vs `deepseek-v4-pro`），比较成功率与成本。

## 3. 如何运行

```powershell
# 离线单测（不消耗 API；两套：短期选择 + 长期记忆/HITL/tracing）
.\.venv\Scripts\python.exe test_policies.py
.\.venv\Scripts\python.exe test_memory.py

# 冒烟测试：单阶段 + 跨会话任务（验证长期记忆与 token 链路）
.\.venv\Scripts\python.exe smoke_test.py

# HITL 演示：危险工具先拒后放，模型自适应改用其他工具
.\.venv\Scripts\python.exe demo_hitl.py

# 一键实验：离线影响分析 + 四策略评测
.\.venv\Scripts\python.exe run_experiments.py

# 预算扫描：多预算 × 多策略的成功率/成本曲线
.\.venv\Scripts\python.exe run_sweep.py
```

配置：复制 `.env` 并填入任意 OpenAI 兼容服务（本机已配置 DeepSeek 官方 API + `deepseek-flash`）。

## 4. 实验协议

- 同一任务集（21 个：6 单步 + 8 多步 + 4 跨会话 + 3 同一会话多轮）、
  同一模型（`deepseek-flash`, temperature 0.7）、同一最大步数（10）；
- 同预算对比：`recent` / `relevance` / `impact` 共享同一字符预算，
  `all` 不裁剪作为上界参照；扫描 300/500/800/1200 四档预算；
- 所有策略共用同一套长期记忆配置（MemoryAgent），差异只来自短期消息选择策略；
- 任务判分不依赖 LLM Judge，全部为确定性检查（文件内容 / 答案关键值），可复现；
- 指标：成功率、平均步数、平均 LLM 调用、累计 token/字符、耗时；
- 每次评测结果保存原始 JSON（含各任务明细），支持审计。

## 5. 早期结果（2026-09-22，6 任务，预算 500 字符）

> 以下为 6 任务版本的早期结果，保留作为对照；18 任务 + 预算扫描的最新结果见第 6 节。

### 5.1 离线决策影响先验（6 个任务，28 次 leave-one-out 干预）

| 记忆类型 | 平均决策影响 |
| --- | --- |
| tool:num（含数字的工具结果） | 0.68 |
| assistant:txt | 0.54 |
| tool:txt | 0.53 |
| assistant:num | 0.42 |

观察：含数字的工具结果（计算结果/文件内容）对最终决策的平均影响最高——
真正被下游使用的是事实性结果，而不是推理过程文本。

### 5.2 四策略对比（同预算 500 字符，6 任务）

| 策略 | 成功率 | 平均步数 | 平均LLM调用 | 平均累计上下文字符 | 平均耗时(s) |
| --- | --- | --- | --- | --- | --- |
| all（无预算上界） | 100% | 2.83 | 2.83 | 1455 | 2.28 |
| recent | 100% | 3.50 | 3.50 | 1663 | 3.96 |
| relevance | 83.3% | 7.00 | 7.00 | 4021 | 7.73 |
| impact | 100% | 4.33 | 4.33 | 2209 | 4.52 |

### 5.3 结论（诚实版）

1. 固定预算下，**纯相关性选择明显劣化**：1/6 任务失败（10 步触顶），平均步数与
   累计上下文是无预算基线的 2.5x / 2.8x——相关性高的消息不一定是决策需要的消息；
2. **加入决策影响先验后恢复 100% 成功率**，相比纯相关性步数 -38%、累计上下文 -45%；
3. `recent` 在这批短任务上表现很强（关键信息通常就在最近），与 `impact` 的差距
   不显著——任务太短、记忆压力不足，无法区分两者；
4. 局限：6 个任务、单一模型、单一预算，结论是方向性的；下一步应延长任务轨迹
   （更多工具调用/多会话）并跑多预算曲线，让"决策影响"策略的优势有区分度。

> 原始数据：`results/eval_20260922_171157.json`、`results/impact_details.json`、
> `results/impact_priors.json`。所有判分为确定性检查，可复现。

## 6. 最新结果（2026-09-23，18 任务 + 预算扫描）

### 6.1 预算扫描（4 预算 × 3 策略 × 18 任务 = 216 次运行）

**成功率**：

| 策略 | 预算 300 | 预算 500 | 预算 800 | 预算 1200 |
| --- | --- | --- | --- | --- |
| recent | 94.4% | 88.9% | 88.9% | 94.4% |
| relevance | 88.9% | 94.4% | **100.0%** | 94.4% |
| impact | 88.9% | 88.9% | 94.4% | **100.0%** |

**平均累计 Prompt Token**：

| 策略 | 预算 300 | 预算 500 | 预算 800 | 预算 1200 |
| --- | --- | --- | --- | --- |
| recent | 3499 | 3777 | 3469 | 2951 |
| relevance | 4025 | 3328 | 3074 | 3009 |
| impact | 3810 | 3905 | 3871 | 3075 |

### 6.2 三个关键发现

1. **整体打平（诚实结论）**：18 个任务上三种策略在 88.9%~100% 波动，差异 1-2 个任务量级，在噪声内——
   这批任务太确定，策略差异被模型自身能力盖过；
2. **跨会话任务是区分度所在**：长期记忆任务（记住 → 换会话使用）recent **62.5%** vs relevance / impact **81.3%**——
   跨会话场景更依赖检索质量与选择口径；`ltm_reads=1 / ltm_writes=2` 显示记忆读写链路全程生效；
3. **预算太紧反而更贵**：预算 300 时累计 token 最高（3499~4025），预算 1200 时最低（2951~3075）——
   预算过紧会让 Agent 多走重试步数，总成本反升。

失败分布：15 / 216（**93.1% 成功**），集中在跨会话任务 `xs_project_code`（7 次）与 `xs_reports_dir`（4 次）。

### 6.3 单 Agent vs 多 Agent（8 任务，预算 800）

| 实现 | 成功率 | 平均 LLM 调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| 单 Agent | **100%** | 3.5 | 2628 | 3.35 |
| 多 Agent（Planner / Executor / Reviewer） | **100%** | 7.12 | 4508 | 8.41 |

结论：这批任务上多 Agent 把成本抬到约 2 倍（调用 2.0×、token 1.7×、耗时 2.5×）而成功率没有提升——
"多 Agent 不是免费的"；简单确定性任务上单 Agent 更优，多 Agent 的价值需要更复杂/开放的任务来验证。

### 6.4 LangGraph 对照（手写循环 vs StateGraph）

| 实现 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| miniagent（手写循环） | **100%** | 4.38 | 3225 | 5.03 |
| LangGraph（memory → agent ⇄ tools） | **100%** | 4.25 | 2661 | 4.15 |

- 同一套记忆策略与工具移植到 StateGraph：8 任务全部通过，成本同量级（差异在噪声内）；
- LangGraph 的增益在框架原语：checkpointer（多轮记忆）、interrupt（工具审批）、图可视化；
- 实现坑：把 `ToolNode` 包进自定义节点时忘记透传 config →
  `ValueError: Missing required config key 'N/A'`，第一次工具调用即崩（修复：直接作为节点使用或显式传入 config）。

### 6.5 跨会话任务失败分析（诚实口径）

- 失败构成：网络重试耗尽（`LLM调用失败: 重试 5 次后仍失败`）与模型在"长期记忆 + 新任务"下偶发误读
  （把历史记忆当任务、或反过来向用户索要指令）各占一部分；
- 记忆链路本身正常：复现显示注入顺序为 `[system(长期记忆), user(任务)]`，`ltm_reads=1 / writes=2` 全程生效；
- 后续改进已落地：长期记忆改为注入 **system prompt**（见 6.6 消融）、失败样本判分收紧；
- 另注：教学阶段（phase 1）的 `_answer_ok` 检查过于宽松（错误信息也算"有回答"），已修正为排除错误内容。

### 6.6 记忆注入方式消融（2026-09-23）

4 个跨会话任务 × 2 次重复 × 预算 800（impact 策略）：

| 注入方式 | 成功 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| system_prompt（拼进系统提示词） | **8/8** | 4.88 | 3804 | **5.36** |
| message（独立 system 消息） | 7/8 | 5.62 | 3837 | 13.75 |

- 任务级：`xs_project_code` 1/2 → **2/2**，其余任务两者都 2/2；
- 方向性证据（样本较小）：把长期记忆拼进系统提示词**更快更稳**——默认已切换为 `system_prompt`；
- 说明：6.1 的预算扫描使用旧默认（message 模式），复现时若用新默认可能有小幅差异。

### 6.7 执行沙箱（2026-09-23）

- 子进程 Python 执行（超时 / cwd 限制 / 输出截断）+ 路径白名单（防 `../` 逃逸）+
  命令黑名单（`rm -rf` / `del /f` / `format` 等）；
- 离线单测覆盖：越界路径拦截、黑名单命令拦截、正常命令执行、超时终止；
- 边界：进程级隔离，非容器级。

### 6.8 同一会话多轮任务（2026-09-23）

`turns` 型任务 3 个（文件链式操作 / 增量追加与计数 / 带更正的指令跟随）；
冒烟测试 3 轮全部通过（短期记忆在同一会话内累积）。任务集总数 18 → **21**。

### 6.9 跨模型对照（2026-09-23）

6 个任务 × 预算 500（impact 策略）：

| 模型 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| deepseek-flash | **6/6** | 5.33 | 3994 | **5.76** |
| deepseek-v4-pro | 5/6 | 3.67 | **1583** | 27.87 |

- pro 的调用数 −31%、**累计 token 仅为 flash 的 40%**（一次想得更准，更少返工），
  但单次延迟高（平均耗时约 5×），本批任务成功率未提升（1/6 失败）；
- 结论（诚实）：**更强模型≠更高成功率**，但可能显著省 token——选型要按"任务难度 × 延迟/成本约束"权衡。

### 6.10 向量检索（EmbeddingRetriever，2026-09-23）

- 安装 `fastembed` 后可用（模型 `BAAI/bge-small-zh-v1.5`，首次使用自动下载）；
- 冒烟验证：查询"项目代号是什么"对三条记忆打分 `0.726 / 0.181 / 0.401`，目标记忆排第一；
- 国内网络可用镜像：`HF_ENDPOINT=https://hf-mirror.com`；
- 接口已接入 `LongTermMemory`（`get_retriever("embedding")` 或 `retriever=` 参数），默认仍是零依赖 TF-IDF。

原始数据：`results/sweep_*.json`、`results/compare_*.json`、`results/compare_multi_agent_*.json`、
`results/compare_models_*.json`、`results/ablation_injection_*.json`。
