"""从预算扫描结果（results/sweep_*.json）推导「预算 / 步数」的取值建议。

用法：python scripts/budget_report.py
输出：各预算档位的成功率、跨会话任务的成功率、各任务首次成功的最小预算、步数分位数。
"""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runs():
    runs = []
    for path in sorted((ROOT / "results").glob("sweep_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data.get("records") or data.get("results") or []
        for row in records:
            runs.append(
                {
                    "task_id": row.get("task_id", "?"),
                    "policy": row.get("policy", "?"),
                    "budget": int(row.get("budget", 0)),
                    "success": bool(row.get("success")),
                    "steps": int(row.get("steps", 0)),
                    "llm_calls": int(row.get("llm_calls", 0)),
                    "prompt_chars": int(row.get("prompt_chars", 0)),
                }
            )
    return runs


def pct(values, q):
    if not values:
        return 0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[idx]


def main():
    runs = load_runs()
    if not runs:
        print("没有找到 results/sweep_*.json")
        return 1

    budgets = sorted({r["budget"] for r in runs})
    print(f"共 {len(runs)} 次运行，预算档位：{budgets}\n")

    print("== 各预算档位：成功率（跨会话 xs_* 单独看）==")
    print(f"{'预算':>6} {'样本':>4} {'总成功率':>8} {'普通任务':>8} {'跨会话任务':>10}")
    for b in budgets:
        rows = [r for r in runs if r["budget"] == b]
        normal = [r for r in rows if not r["task_id"].startswith("xs_")]
        cross = [r for r in rows if r["task_id"].startswith("xs_")]
        rate = lambda xs: (sum(1 for r in xs if r["success"]) / len(xs) * 100) if xs else 0
        print(f"{b:>6} {len(rows):>4} {rate(rows):>7.1f}% {rate(normal):>7.1f}% {rate(cross):>9.1f}%")

    print("\n== 各任务：首次成功的最小预算（按 policy 取最好）==")
    by_task = {}
    for r in runs:
        by_task.setdefault(r["task_id"], []).append(r)
    for task_id in sorted(by_task):
        rows = by_task[task_id]
        ok = sorted({r["budget"] for r in rows if r["success"]})
        print(f"  {task_id:20s} 最小成功预算: {ok[0] if ok else '未成功':>6}")

    print("\n== 步数分布（成功的运行）==")
    ok_runs = [r for r in runs if r["success"]]
    steps = [r["steps"] for r in ok_runs]
    print(
        f"  全部任务: p50={pct(steps, 0.5)} p90={pct(steps, 0.9)} p95={pct(steps, 0.95)} "
        f"max={max(steps) if steps else 0}（样本 {len(steps)}）"
    )
    for prefix, label in (("xs_", "跨会话"),):
        sub = [r["steps"] for r in ok_runs if r["task_id"].startswith(prefix)]
        if sub:
            print(f"  {label}: p50={pct(sub, 0.5)} p90={pct(sub, 0.9)} max={max(sub)}")
    multi = [r["steps"] for r in ok_runs if r["task_id"] in {"scores_total", "backup_copy", "avg_three_files", "sum_two_files"}]
    if multi:
        print(f"  多步文件任务: p50={pct(multi, 0.5)} p90={pct(multi, 0.9)} max={max(multi)}")

    print("\n== 提示词规模（成功的运行，字符）==")
    chars = [r["prompt_chars"] for r in ok_runs]
    if chars:
        print(f"  p50={pct(chars, 0.5)} p90={pct(chars, 0.9)} max={max(chars)}")

    print("\n== 建议（按任务类型）==")
    cross = [r for r in runs if r["task_id"].startswith("xs_")]
    rate_by_budget = {}
    for b in budgets:
        rows = [r for r in cross if r["budget"] == b]
        rate_by_budget[b] = (sum(1 for r in rows if r["success"]) / len(rows)) if rows else 0
    need_budget = next((b for b in budgets if rate_by_budget[b] >= 0.9), budgets[-1])
    steps_p95 = pct([r["steps"] for r in ok_runs], 0.95)
    print(f"  单轮文件/计算：预算 300–500（成功率 ~98%），步数 {steps_p95} + 2 余量 = {steps_p95 + 2}")
    print(f"  跨会话/多轮（需历史约束）：预算 ≥ {need_budget}（跨会话成功率 ≥90%），步数同上")
    print("  开放域联网检索（研究型）：预算 800–1200，步数 20+（实测行程规划类需 19~20 步）")
    print("  说明：预算限制“每轮注入多少历史”（不足→答错），步数限制“能做多少次工具调用”（不足→答不完）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
