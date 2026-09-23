# 搜索质量评估：免费方案 vs Tavily（对照实验）

起因：「今年的北京实习推荐」检索不到有用信息（见 `docs/ISSUES.md` U15）。
本文是**可复现的对照评估**：同一评估集、同一判分口径，对比不同搜索后端与启发式开关。

复现：
```powershell
python scripts/eval_search.py --backends bing_rss --heuristics both      # 免费，直连
python scripts/eval_search.py --backends tavily --heuristics both        # 免费额度（1000 credits/月）
$env:HTTPS_PROXY="http://127.0.0.1:7890"; python scripts/eval_search.py --backends duckduckgo
```

## 1. 评估设置

- **评估集**：`benchmarks/search_quality/queries.jsonl`，30 条真实查询，按类型分层：
  单实体 8 / 泛词口语 8 / 垂直领域 7 / 时效性 7（含踩坑样本「今年的北京实习推荐」）；
- **判分**：hit@3 —— 前 3 条结果的 标题/摘要/URL 命中该条标注关键词即算命中
  （标注的是"主题相关"关键词，如 实习/招聘/ncss/牛客，不是精确答案）；
- **对照维度**：后端（Bing RSS / DuckDuckGo / Tavily）× 启发式（关 = 原始检索；开 = 相关性过滤 + 自动缩短 + 平台词兜底）；
- **约束**：全部用免费额度/免费后端（Tavily 免费层 1000 credits/月，basic=1 credit；本次共消耗约 60 credits）。

## 2. 结果（2026-09-23）

| 后端 | 配置 | hit@3 | 自动纠正率 | 平均延迟 | 成本 | 可用性 |
| --- | --- | --- | --- | --- | --- | --- |
| Bing RSS（爬取） | 启发式**关** | 53.3% | 0.0% | 0.2s | ¥0 | ✅ 国内直连 |
| Bing RSS（爬取） | 启发式**开** | **66.7%** | 23.3% | 0.76s | ¥0 | ✅ |
| DuckDuckGo（爬取） | 启发式关 | 6.7%* | 0.0% | 1.57s | ¥0 | ❌ 28/30 被风控（空结果） |
| DuckDuckGo（爬取） | 启发式开 | 0.0%* | 0.0% | 1.47s | ¥0 | ❌ |
| Tavily（agent-native） | 启发式**关** | **100%** | 0.0% | 2.84s | 免费额度内 | ✅ 国内直连 |
| Tavily（agent-native） | 启发式开 | 96.7% | 0.0% | 3.71s | 同上 | ✅ |

\* DDG 只有前 2 条查询返回了结果，其余 28 条被限流返回空页——数字不代表检索质量，代表**稳定性**。

分层（启发式关 / 开）：
- Bing RSS：单实体 62%/88%、泛词口语 50%/62%、垂直 43%/43%、时效 57%/71%
- Tavily：全部类别 **100%**（启发式开后单实体 88%）

## 3. 三条结论

**① 启发式是"给劣质后端打的补丁"，应按后端自动开关（已实现）**
- 在 Bing 爬取后端上：**+13.4pt**（53.3% → 66.7%），自动纠正触发率 23.3%——确实救回了泛词/长短语类查询；
- 在 Tavily 上：**-3.3pt**（100% → 96.7%）。差异条目是 E06「上海中心大厦 高度」：Tavily 返回了
  英文页（Shanghai Tower），被我们的**中文 bigram 过滤**判为 0 分剔除 → 命中丢失；
- 结论：`WebSearchTool._use_heuristics()` 默认**只对爬取类后端（bing_rss）启用**，API 类后端默认关闭。

**② "换 agent-native API"比"继续优化爬虫"更有效**
- Tavily 零兜底即 30/30，包含踩坑样本「今年的北京实习推荐」（返回 Indeed/环球影城/Apple 实习岗位）；
- 代价：延迟 2.84s vs Bing raw 0.2s（约 10 倍）、依赖外部服务与免费额度；
- 诚实说明：本评估只测"检索层命中"，未测端到端任务成功率（Tavily 的摘要更长，可减少 `http_get` 抓正文，端到端差距可能更小）。

**③ 免费方案的真实成本不是钱，是稳定性**
- DDG 免费但 28/30 被风控；Bing 爬取虽然零成本，但会分词退化（U15），需要启发式补丁；
- 这正是 `open-websearch` / `web-mcp` 这类项目的设计要点：**多引擎并行 + 引擎冷却 + 兜底链**；
- 本仓库已实现 `openwebsearch` 后端（本地 daemon，免 key 多引擎），但该 daemon 依赖 **Node ≥20.18.1**
  （本机 18.16.1，`undici@7` 报 `File is not defined`），装好 Node 后即可用：
  ```powershell
  npx --yes open-websearch@latest serve      # 默认 http://127.0.0.1:3000
  python scripts/eval_search.py --backends openwebsearch --heuristics both
  ```

## 4. 局限（不要过度解读）

- 样本 30 条、单时间点：用于**方向性决策**，不宣称统计显著；关键词标注是"主题相关"，不判答案正确性；
- 未覆盖：英文长尾、需要登录/JS 渲染的站点、多跳研究型任务；
- Tavily 只用了 `basic` 深度（1 credit）；`advanced` 质量更高但 2 credits、延迟更大。

## 5. 下一步（按收益排序）

1. **Node 升级到 20+** → 跑 `openwebsearch` 对照（免 key 多引擎，含百度/搜狗，国内更稳）；
2. 用 Tavily 的 `include_raw_content` 替代 `http_get` 抓 JS 页面（解决官网 403/渲染问题，`docs/ISSUES.md` T9）；
3. 端到端评估：把本评估集的查询变成任务，比较"任务成功率 / 步数 / token 成本"，而不只是检索命中；
4. 回归机制：把 `scripts/eval_search.py` 接进改动后的例行检查（当前命中率作为基线）。

原始数据：`results/search_eval_*.json`
