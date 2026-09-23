# LangGraph / LangChain 实践笔记（面试用）

本目录是**同一套运行时的两套实现对照**：核心 `mini_agent/`（手写 ReAct 循环，零框架依赖）
↔ `extras/langgraph_compare/`（LangGraph 图 + LangChain 组件）。下面记录原语对照、实测数据与坑。

## 1. 原语对照表（手写 vs LangGraph）

| 能力 | 手写实现（`mini_agent/`） | LangGraph 实现（`extras/`） |
| --- | --- | --- |
| 循环/控制流 | `while` + 状态机（`AgentState`） | `StateGraph` + 条件边（`add_conditional_edges`）+ 循环 |
| 多 Agent | `MultiAgentTeam`（Planner→Executor→Reviewer，普通函数调用） | supervisor 单图：planner / approval / executor / reviewer / finish 节点 |
| 上下文裁剪 | `BudgetedMemory`（预算 + 相关性/近因） | `memory_policy.select_messages`（同一套打分，供图节点调用） |
| 人工审批（HITL） | `approval_fn` 回调（阻塞等待） | **`interrupt()`** 暂停图 + `Command(resume=...)` 恢复（可跨进程/跨请求） |
| 多轮会话状态 | 复用同一 agent 实例（进程内） | **checkpointer**（`thread_id`）+ `add_messages` reducer；SQLite 版可跨进程 |
| 消息契约修复 | `repair_orphans`（tool_calls 必须跟齐 tool 响应） | `_repair_dangling_tool_calls`（同源思路，防 400） |
| 观测 | 自研 `TraceLogger` + token/成本统计 | LangChain 回调（`AsyncCallbackHandler.on_llm_end`）+ 事件流 |

**结论**：手写版把机制显式化（能讲清每一步），LangGraph 版把状态/中断/持久化交给框架（少写胶水、天然支持跨进程恢复）。
两者行为一致（下表），选型取决于"要不要跨进程恢复 / 是否需要图可视化 / 团队熟悉度"。

## 2. 实测数据（2026-09-24）

同一批任务、同一套工具与记忆策略：

| 实现 | 成功率 | 平均 LLM 调用 | 平均轮次 | 平均耗时 |
| --- | --- | --- | --- | --- |
| 手写 MultiAgentTeam | 100% | 4.0 | 1.0 | 3.43s |
| LangGraph supervisor 图 | 100% | 4.0 | 1.0 | 4.38s |

（另有更早的**单 Agent** ReAct 对照：`compare.py`，两者成功率一致、token 同量级。原始数据见 `results/compare_lg_*.json`。）

**HITL（interrupt）实测**：计划审批被拒 → 图提前结束并返回"已取消（人工拒绝）"；
计划放行后，每次 `bash_execute` 都触发一次工具级审批（实测连续 4 次暂停/放行）→ 最终给出结果。
**多轮（checkpointer）实测**：同一 `thread_id` 连续两次 invoke，第二轮状态里可见第一轮消息。

## 3. 踩过的坑（面试可讲）

1. **`interrupt()` 需要 Python ≥3.11**：在 3.10 的异步上下文里会报
   `Called get_config outside of a runnable context`（源码里那句提示被 try/except 吞掉了）。
   → 本仓库用专用 venv（`.venv312`）跑 extras；核心链路仍在 3.10 主 venv 上跑（零框架依赖）。
2. **异步图必须用 `AsyncSqliteSaver`**：同步 `SqliteSaver` 在 `ainvoke` 下直接
   `NotImplementedError: does not support async methods`。
3. **悬空 tool_calls 会 400**：`insufficient tool messages following tool_calls message`——
   步数上限/审批中断/异常都可能留下"assistant 带 tool_calls 但没有对应 tool 响应"的历史，
   下一轮请求即失败。修复：发请求前补齐占位 tool 消息（与手写版 `repair_orphans` 同源）。
4. **子图 + checkpointer 要谨慎**：把"编译后的子图"直接作为父图节点、同时挂 checkpointer 时，
   我们在实践中观察到节点被重复执行/长时间不收敛；改为**supervisor 单图 + 条件边**（生产上更常见）后稳定。
5. **`ToolNode` 包进自定义节点要透传 config**（早期单 Agent 对照踩到）：
   `ValueError: Missing required config key 'N/A'`；直接作为节点用即可。
6. **消息字段契约**：`content` 必须始终存在（空串），思考模式模型还要求回传 `reasoning_content`
   （详见 `docs/ISSUES.md` E16/E17）。

## 4. 运行方式

```powershell
# ① extras 专用环境（Python ≥3.11，interrupt 需要）
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r extras\langgraph_compare\requirements-extras.txt langgraph-checkpoint-sqlite

# ② 多 Agent 对照 + checkpointer + interrupt 演示
.\.venv312\Scripts\python.exe extras\langgraph_compare\compare_team.py --limit 2

# ③ 单 Agent ReAct 对照（手写循环 vs LangGraph 图）
.\.venv312\Scripts\python.exe extras\langgraph_compare\compare.py --limit 8 --budget 500 --policy relevance

# ④ LangChain 集成：自研 LTM → BaseRetriever，LCEL 组 RAG 链
.\.venv312\Scripts\python.exe extras\langgraph_compare\langchain_retriever.py

# ⑤ GUI（推荐用 .venv312 启动：LangGraph 同进程运行，实时流式 + 共享记忆库）
.\.venv312\Scripts\python.exe scripts\gui.py --port 8901
#    - 运行时下拉可选「手写循环」或「LangGraph 图」；
#    - LangGraph 图同进程运行（v3.12 支持 interrupt，后续可开工具级审批），
#      轨迹逐节点实时显示、与手写路径共享同一个长期记忆库（读+写）；
#    - 若在 3.10 环境启动 GUI，则自动回退到 .venv312 子进程（流式 JSON 行），功能等价但多一层进程。
```

## 5. GUI 运行时切换（同一任务、两套运行时）

左侧「运行时」下拉：`手写循环（零依赖）` / `LangGraph 图（extras）`。

**运行方式（2026-09-24 起）**：GUI 建议直接用 `.venv312` 启动 —— LangGraph 走**同进程**：

| 能力 | 手写循环 | LangGraph 图（同进程） |
| --- | --- | --- |
| 轨迹实时显示 | ✅ | ✅（逐节点 `astream` 流式） |
| 长期记忆 | ✅ 读+写 | ✅ **同一个库**（planner 注入、finish 回写） |
| 人工审批（interrupt） | approval_fn 回调 | 需要时可开（Python 3.12 已支持） |
| 依赖 | 零框架依赖 | 需 langchain/langgraph（.venv312） |

实测同一任务「用 Python 计算 1 到 50 的和」：手写 3.6s / LangGraph 5.4s，均通过；
LangGraph 运行中消息数采样 `[1,5,12,15,16,17,21]`（流式可见），记忆库新增条目 ✓。

> 兼容说明：在 3.10 的 `.venv` 里启动 GUI 时，LangGraph 自动回退到 `.venv312` 子进程
> （`gui_run.py` 逐行输出 JSON 事件），功能等价，只是多一层进程。
