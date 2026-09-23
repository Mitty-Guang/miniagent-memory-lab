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

## 2. 结果：11/11 通过（100%）

| 任务 | 结果 | 步数 | 调用 | 耗时 |
| --- | --- | --- | --- | --- |
| lh_sales_report | ✅ | 13 | 13 | 18.6s |
| lh_data_clean | ✅ | 13 | 13 | 39.2s |
| lh_json_merge_recover | ✅ | 13 | 13 | 18.0s |
| lh_wordfreq_code | ✅ | 15 | 15 | 21.5s |
| lh_conditional_pipeline | ✅ | 6 | 6 | 6.5s |
| lh_project_3turns（3 轮） | ✅ | 15 | 15 | 33.7s |
| lh_bugfix_selftest（2 轮） | ✅ | 26 | 26 | 108.7s |
| lh_weather_pipeline | ✅ | 12 | 12 | 33.1s |
| lh_web_research_uncertainty（改造后） | ✅ | **6** | 6 | 22.5s |
| lh_xs_pref_report（跨会话 3 阶段） | ✅ | 22 | 22 | 27.3s |
| lh_xs_spec_to_code（跨会话 2 阶段） | ✅ | 22 | 22 | 38.4s |

原始数据：`results/long_horizon_*.json`。

**结论**：结构化长链（多文件合并/清洗/容错/条件分支/代码自测/多轮规范变更/跨会话）
**全部通过**；缺陷修复 26 步、跨会话 22 步是"步数消耗最大"的两类。

## 3. L3 开放式研究的修复（从失败到 6 步通过）

初版 L3 任务连续 3 次失败（20 步 × 3），轨迹显示**同一个模式**：

1. 把 20 步几乎全部花在检索上；2. 最后一步在**回答里**写出了完整报告（还说"你可直接保存为文件"）；
3. **从未调用 `file_editor` 落盘** → 判分（要求文件存在）失败。

**瓶颈不是内容质量，而是"何时落盘"**（与早期"诺坎普规划"的收口问题同源）。

按三个方向改造后（`max_steps` 20 → **30**；拆成**两轮**：先写骨架+待核实清单，再检索补充；
系统提示加**产出纪律**；50% 步数处提醒"先落盘初稿"）：

| 版本 | 结果 | 步数 | 说明 |
| --- | --- | --- | --- |
| 初版（单轮 + 20 步） | ❌ ×3 | 20 触顶 | 检索循环，内容只在回答里 |
| 改造后（骨架先行 + 2 轮 + 30 步上限） | ✅ | **6** | 先落盘骨架 → 少量检索 → 补全即判分通过 |

**意外收获**：骨架先行不仅让任务通过，还把步数从 20+ 降到 **6**——"先产出再完善"本身也是
**省预算**的策略（与 `docs/SECONDARY_DEV.md` 的"收口时机"结论一致）。

## 4. 跨会话长周期族（新增）

| 任务 | 形态 | 判分 |
| --- | --- | --- |
| lh_xs_pref_report | 3 阶段、每阶段**新会话**、共享长期记忆：定偏好(NOVA/reports/两位小数) → 按偏好出报告 → 需求变更(+5%) | 每阶段各自判分（5400.00 / 5670.00 + 代号） |
| lh_xs_spec_to_code | 2 阶段：先立 `docs/spec.md`（3 条验收标准）→ 新会话按规范实现并**运行自测** | `python test_main.py` 输出 OK |

这族任务验证的是「**写 → 读 → 用**」链路在**长周期**下的可靠性：阶段之间不共享短期记忆，
只能靠 LTM 检索注入把规范带过去（run 脚本对 `phases` 用新 agent、共享 LTM 库实现）。

## 5. GUI 一键演示（Harness 模式）

`scripts/gui.py` 新增 `/api/lh_tasks` 与 `/api/lh_run`；页面左栏「长周期任务（Harness 模式）」
下拉选择任务 → 一键运行，面板实时显示：

- 逐轮/逐阶段进度（`轮 1 开始 → 判分 ✅`）
- 逐轮判分标记（`✅ / ✅ / ❌`）+ 步数/调用/耗时 + 工作目录 + 最终回答

GUI 本体仍零框架依赖：Harness 在同一个进程里被复用（`run_long_horizon.run_task`），
进度通过回调写进运行状态。

