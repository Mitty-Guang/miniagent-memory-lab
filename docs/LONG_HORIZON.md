# 长周期 / 真实复杂任务评测（2026-09-24）

现有 `mini_agent/task_suite.py`（21 个小任务）验证的是单步/短链能力；本套
`benchmarks/long_horizon/tasks.jsonl` 专门测**多轮、多文件、容错、联网研究**这类
更接近真实工作的长周期任务，全部**确定性判分**（不依赖 LLM judge）。

复现：
```powershell
$env:WEB_SEARCH_CACHE="1"                                    # 检索走缓存，省额度
python scripts/run_long_horizon.py --limit 4                 # 跑前 4 个
python scripts/run_long_horizon.py --ids lh_web_research_uncertainty
python scripts/run_long_horizon.py --families 数据管道,多轮项目 --policy relevance
```

## 1. 任务设计（9 个，4 族，L1–L3）

| 任务 | 族 / 等级 | 形态 | 判分要点 |
| --- | --- | --- | --- |
| lh_sales_report | 数据管道 / L1 | 预置 3 个 CSV，合并→按产品汇总→写 md + total.txt | total.txt 精确等于 `3200.00`；md 含三个产品 |
| lh_data_clean | 数据清洗 / L1 | 脏 CSV（重复/缺失/混合日期）→ clean.csv + issues.md | 去重后 5 行、N/A 填充、日期 ISO、三类问题计数 |
| lh_json_merge_recover | 容错处理 / L1 | 两个合法 JSON + 一个损坏 JSON | merged.json 键值完整；errors.md 记录坏文件且不中断 |
| lh_wordfreq_code | 代码实现 / L1 | 写 `wordfreq.py` 并**实际运行**产出 out.json | out.json 与期望词频完全一致 |
| lh_conditional_pipeline | 条件分支 / L1 | 读 n=137 → 分支建 big.txt/small.txt + 平方 | 文件内容精确匹配 |
| lh_project_3turns | 多轮项目 / L2 | **3 轮**：定规范(NEBULA/reports/两位小数) → 生成报告 → 需求变更(+10%) | 每轮各自判分，全过才算通过 |
| lh_bugfix_selftest | 缺陷修复 / L2 | **2 轮**：修 `calc.py` 通过自测 → 新增 multiply 并补测试 | 运行 `test_calc.py` 输出 OK |
| lh_weather_pipeline | 联网+数据 / L2 | 抓 wttr.in JSON → 算当日最高/最低温 → 写 weather.md | 含最高/最低/°C/来源 URL |
| lh_web_research_uncertainty | 联网研究 / L3 | 开放式调研 → research.md（≥2 链接 + 明确「不确定」节） | 文件存在 + 链接数 + 不确定性标注 + 长度 |

## 2. 结果：8/9 通过（88.9%）

| 任务 | 结果 | 步数 | 调用 | 耗时 | tokens(p/c) |
| --- | --- | --- | --- | --- | --- |
| lh_sales_report | ✅ | 13 | 13 | 18.6s | — |
| lh_data_clean | ✅ | 13 | 13 | 39.2s | — |
| lh_json_merge_recover | ✅ | 13 | 13 | 18.0s | — |
| lh_wordfreq_code | ✅ | 15 | 15 | 21.5s | — |
| lh_conditional_pipeline | ✅ | 6 | 6 | 6.5s | 7530/908 |
| lh_project_3turns（3 轮） | ✅ | 15 | 15 | 33.7s | — |
| lh_bugfix_selftest（2 轮） | ✅ | 26 | 26 | 108.7s | — |
| lh_weather_pipeline | ✅ | 12 | 12 | 33.1s | 20872/2780 |
| lh_web_research_uncertainty | ❌ | 20（触顶） | 20 | 379s | — |

原始数据：`results/long_horizon_*.json`。

**结论**：结构化长链（多文件合并/清洗/容错/条件分支/代码自测/多轮规范变更）**稳定通过**；
缺陷修复用了 26 步（两轮 + 自测迭代），是"步数消耗最大"的通过案例。

## 3. 失败分析：L3 开放式研究的"产出纪律"问题

`lh_web_research_uncertainty` 连续 3 次失败（20 步 × 3），轨迹显示**同一个模式**：

1. 把 20 步几乎全部花在检索上（多次 `web_search` + `http_get`）；
2. 最后一步的**回答里**写出了完整的 research.md 正文（还贴心地说"你可直接保存为文件"）；
3. 但**从未调用 `file_editor` 落盘** → 判分（要求文件存在）失败。

也就是说：**内容质量不是瓶颈，"何时落盘"才是**。这与早期"诺坎普规划"案例同源（检索循环 +
收口太晚），但在 L3 任务上更严重——因为交付物是文件而不是答案。

**已实施的两项缓解**（`mini_agent/agent.py`）：
- 50% 步数处的系统提醒："先落盘初稿（write-then-update），不要一直检索到最后"；
- 系统提示新增**产出纪律**："把内容写在回答里不算交付"。

**复测结果**：仍然失败（20 步触顶）。说明仅在提示层加约束不够，L3 任务需要结构性改动：

| 方向 | 具体做法 |
| --- | --- |
| 提高步数上限 | L3 任务 max_steps 提到 30+（当前 20），给"检索 → 落盘 → 修订"留空间 |
| 任务拆分 | 把"调研"与"写报告"拆成两轮（第二轮明确要求写文件并复核），失败率会显著下降 |
| 强制首动作 | 任务描述里直接要求"先在 research.md 写骨架（含待核实清单），再逐条补充" |
| 检索侧优化 | 复用 `docs/SEARCH_EVAL.md` 的结论：泛词走平台词/多引擎兜底，减少无效检索步 |

面试表述：*"我们的长周期评测显示，结构化任务 8/9 通过；唯一失败的是开放式研究——失败点不是
内容质量，而是模型把成果留在回答里、没有落盘。我们据此加了'先写初稿再迭代'的产出纪律，
并把它归类为需要任务拆分/提高步数上限的结构性问题。"*
