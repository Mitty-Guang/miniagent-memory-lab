# Mem2ActBench 适配器（公开基准验证）

用本项目的**长期记忆管线**（写入 → 检索 → 注入）驱动工具调用生成，在公开基准
**Mem2ActBench（ACL 2026）** 上验证"记忆是否真的被用上"。

## 基准是什么

Mem2ActBench 用 ToolACE / BFCL / OASST1 合成 2,029 段长对话与 400 个任务：
用户跨会话提到过的偏好与任务状态（记忆）需要被**主动检索并用于**
「选对工具 + 填对参数」——正是真实业务助手的形态（天气、日程、社交趋势等）。
论文：*Mem2ActBench: A Benchmark for Evaluating Long-Term Memory Utilization in
Task-Oriented Autonomous Agents*（[GitHub](https://github.com/Cantaloupe-M/Mem2ActBench)）。

## 数据准备（仓库不提交数据）

```powershell
git clone --depth 1 https://github.com/Cantaloupe-M/Mem2ActBench.git benchmarks/mem2actbench/data/repo
```

## 运行

```powershell
.\.venv\Scripts\python.exe benchmarks\mem2actbench\run_benchmark.py --limit 40 --top-k 3
```

## 简化协议（与论文完整协议的区别）

| 环节 | 论文 | 本适配器 |
| --- | --- | --- |
| 记忆库 | 完整会话历史（数千轮） | 该题的证据片段 + 40 条随机干扰片段（"大海捞针"近似） |
| 检索 | 各记忆系统自带（BGE-M3 等） | 本项目 `LongTermMemory`（默认 TF-IDF，可换向量） |
| 生成 | 多模型 × 七种记忆框架 | `deepseek-flash` + 原生 Function Calling |
| 判分 | 参数 F1 / BLEU-1 / Tool Accuracy | **参数精确匹配（TA）+ 参数 F1**（工具名与参数统一归一化） |
| 对照 | 不同记忆框架 | **有记忆 vs 无记忆**（同一模型同一 prompt，仅上下文不同） |

> 由于工具名含 `/`、空格等非法字符，适配器会做统一清洗（`sanitize_name`）；
> 判分时工具名按"字母数字核心"归一化比较（`core_name`，忽略大小写与分隔符差异），
> 参数按归一化后的键值对精确匹配 + F1。

## 结果

见仓库根目录 `SECONDARY_DEV.md` 的「Mem2ActBench」一节，原始数据在 `results/mem2actbench_*.json`。
