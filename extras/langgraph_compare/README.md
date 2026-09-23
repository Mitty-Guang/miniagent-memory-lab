# LangGraph 对照实验

同一套记忆策略与工具（直接复用主项目的 `mini_agent/tools.py` 与 `task_suite.py`），
用 **LangGraph StateGraph** 重写运行时，与主项目的**手写 ReAct 循环**做对照。

```
START → memory → agent ──(有 tool_calls)──→ tools ──→ memory
                  │
                  └──(无 tool_calls)→ END
```

- `memory_policy.py`：固定预算消息选择（与主项目同一套打分与工具调用组原子化逻辑）；
- `agent_graph.py`：StateGraph（memory / agent / tools 节点 + 条件边 + 可选审批）；
- `compare.py`：对照实验（成功率 / 调用数 / token / 耗时）。

## 运行

```powershell
# 需要额外依赖（LangChain / LangGraph）
.\.venv\Scripts\python.exe -m pip install -r extras\langgraph_compare\requirements-extras.txt

.\.venv\Scripts\python.exe extras\langgraph_compare\compare.py --limit 8 --budget 500 --policy relevance
```

## 实测结果（8 任务 × relevance × 预算 500）

| 实现 | 成功率 | 平均调用 | 平均 Prompt tokens | 平均耗时(s) |
| --- | --- | --- | --- | --- |
| 主项目（手写循环） | 100% | 4.38 | 3225 | 5.03 |
| LangGraph（StateGraph） | 100% | 4.25 | 2661 | 4.15 |

结论：同一套策略两套运行时**行为一致、成本同量级**；LangGraph 的增益在框架原语
（checkpointer 多轮记忆、interrupt 工具审批、图可视化）。

> 实现坑：把 `ToolNode` 包进自定义节点时必须透传 config，否则报
> `ValueError: Missing required config key 'N/A'`；直接作为节点使用即可。
