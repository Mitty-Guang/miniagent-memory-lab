# LangGraph 对照实验

同一套记忆策略与工具（直接复用主项目的 `mini_agent/tools.py` 与 `task_suite.py`），
用 **LangGraph StateGraph** 重写运行时，与主项目的**手写 ReAct 循环**做对照。

> **环境要求**：`interrupt()`（动态人工审批）在异步上下文里需要 **Python ≥3.11**；
> 本仓库用专用 venv 跑 extras：`py -3.12 -m venv .venv312` +
> `.\.venv312\Scripts\python.exe -m pip install -r requirements-extras.txt langgraph-checkpoint-sqlite`。
> 核心 `mini_agent/` 不受影响（零框架依赖）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `agent_graph.py` / `compare.py` | **单 Agent**：LangGraph ReAct（memory/agent/tools 节点）vs 手写循环；实测行为一致、token 同量级 |
| `multi_agent_graph.py` | **多 Agent**：supervisor 单图（planner → approval → executor ⇄ tools → reviewer → finish），含 `interrupt` 工具级审批、`AsyncSqliteSaver`/`InMemorySaver` checkpointer、悬空 tool_calls 修复 |
| `compare_team.py` | 多 Agent 对照 + **多轮会话（checkpointer）** + **人工审批（interrupt）** 三个场景 |
| `langchain_retriever.py` | **LangChain 集成**：自研 LTM → `BaseRetriever`，LCEL 组 RAG 链（检索 → 提示词 → 模型） |
| `memory_policy.py` / `llm_factory.py` | 共用的记忆裁剪与 LLM 工厂（读仓库根 `.env`） |

## 运行

```powershell
# 多 Agent 对照 + 两个原语演示（推荐）
.\.venv312\Scripts\python.exe extras\langgraph_compare\compare_team.py --limit 2

# 单 Agent 对照
.\.venv312\Scripts\python.exe extras\langgraph_compare\compare.py --limit 8 --budget 500 --policy relevance

# LangChain Retriever + LCEL
.\.venv312\Scripts\python.exe extras\langgraph_compare\langchain_retriever.py
```

## 实测结果

**多 Agent（2026-09-24，同一批任务、同一套工具与策略）**

| 实现 | 成功率 | 平均 LLM 调用 | 平均轮次 | 平均耗时 |
| --- | --- | --- | --- | --- |
| 手写 MultiAgentTeam | 100% | 4.0 | 1.0 | 3.43s |
| LangGraph supervisor 图 | 100% | 4.0 | 1.0 | 4.38s |

**单 Agent（8 任务 × relevance × 预算 500）**

| 实现 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| 主项目（手写循环） | 100% | 4.38 | 3225 | 5.03 |
| LangGraph（StateGraph） | 100% | 4.25 | 2661 | 4.15 |

**原语演示**：计划审批被拒 → 图返回"已取消（人工拒绝）"；放行后每次 `bash_execute` 触发工具级
`interrupt`（实测连续 4 次暂停/放行）；同一 `thread_id` 连续两次 invoke，第二轮状态保留第一轮消息。

结论：同一套策略两套运行时**行为一致、成本同量级**；LangGraph 的增益在框架原语
（checkpointer 多轮状态、interrupt 人工审批、图可视化），代价是依赖与 Python 版本要求。
更多坑与对照表见 [`docs/LANGGRAPH_NOTES.md`](../../docs/LANGGRAPH_NOTES.md)。

> 实现坑：把 `ToolNode` 包进自定义节点时必须透传 config，否则报
> `ValueError: Missing required config key 'N/A'`；直接作为节点使用即可。
