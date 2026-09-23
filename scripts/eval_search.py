"""搜索质量评估：多后端 × 启发式开/关 的对照评测（零成本方案）。

用法：
    python scripts/eval_search.py                          # 默认 Bing RSS，启发式 开/关 对照
    python scripts/eval_search.py --backends bing_rss,duckduckgo
    python scripts/eval_search.py --heuristics both --limit 3

指标：
- hit@k：前 k 条结果中，标题/摘要/URL 命中标注关键词（`expect_any`）即算命中；
- 自动纠正率：工具触发"缩短重搜/平台词兜底"的比例；
- 平均延迟；按类别（单实体/泛词口语/垂直领域/时效性）分层统计。

零成本后端：
- bing_rss：Bing RSS 解析（默认，国内直连可用，无需 key）；
- duckduckgo：DDG HTML 解析（免费无 key，国内需代理：先设 HTTPS_PROXY=http://127.0.0.1:7890）。

结果写入 results/search_eval_<timestamp>.json，便于回归对比。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_agent.web import WebSearchTool  # noqa: E402

QUERIES = ROOT / "benchmarks" / "search_quality" / "queries.jsonl"


def load_queries():
    items = []
    for line in QUERIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def is_hit(results, expect_any) -> bool:
    for item in results:
        text = (item.get("title", "") + " " + item.get("snippet", "") + " " + item.get("url", "")).lower()
        if any(keyword.lower() in text for keyword in expect_any):
            return True
    return False


async def run_backend(backend: str, heuristics: bool, queries, limit: int, delay: float, cache: bool):
    tool = WebSearchTool(backend=backend, heuristics=heuristics, cache=cache)
    rows = []
    for item in queries:
        started = time.time()
        try:
            results, note = await tool.search_results(item["query"], limit=limit)
            error = ""
        except Exception as exc:
            results, note, error = [], "", str(exc)[:120]
        rows.append(
            {
                "id": item["id"],
                "category": item["category"],
                "query": item["query"],
                "hit": is_hit(results, item["expect_any"]) if results else False,
                "auto_corrected": bool(note),
                "results": [r.get("title", "")[:70] for r in results[:limit]],
                "seconds": round(time.time() - started, 2),
                "error": error,
            }
        )
        await asyncio.sleep(delay)
    return rows, tool.cache_hits


def summarize(rows, cache_hits: int = 0):
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    corrected = sum(1 for r in rows if r["auto_corrected"])
    seconds = [r["seconds"] for r in rows] or [0]
    by_category = {}
    for row in rows:
        bucket = by_category.setdefault(row["category"], [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if row["hit"] else 0
    return {
        "n": total,
        "hit@k": round(hits / total * 100, 1) if total else 0,
        "auto_corrected": round(corrected / total * 100, 1) if total else 0,
        "avg_seconds": round(sum(seconds) / len(seconds), 2),
        "cache_hits": cache_hits,
        "paid_calls": max(0, total - cache_hits),   # 实际打到后端的次数（估算额度消耗）
        "by_category": {
            name: f"{hit}/{n} ({round(hit / n * 100)}%)" for name, (n, hit) in sorted(by_category.items())
        },
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", default="bing_rss", help="逗号分隔：bing_rss,duckduckgo")
    parser.add_argument("--heuristics", default="both", choices=["on", "off", "both"])
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--delay", type=float, default=1.2, help="每条查询间隔秒数（防限流）")
    parser.add_argument("--no-cache", action="store_true", help="禁用磁盘缓存（默认启用：重复查询不再消耗额度）")
    parser.add_argument("--category", default="", help="只跑某一类（单实体/泛词口语/垂直领域/时效性）")
    args = parser.parse_args()

    queries = load_queries()
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]
    modes = {"on": [True], "off": [False], "both": [False, True]}[args.heuristics]
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    use_cache = not args.no_cache
    if use_cache:
        print("（已启用磁盘缓存：同一查询重复运行不消耗额度；--no-cache 可强制重跑）")

    report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "limit": args.limit, "cache": use_cache, "runs": []}
    for backend in backends:
        if backend == "tavily" and not use_cache:
            print(f"⚠ Tavily 按次计费（basic=1 credit）：本次将产生约 {len(queries) * len(modes)} 次调用")
        for heuristics in modes:
            label = f"{backend} + 启发式{'开' if heuristics else '关'}"
            print(f"\n▶ 运行：{label}（{len(queries)} 条查询）", flush=True)
            rows, cache_hits = await run_backend(
                backend, heuristics, queries, args.limit, args.delay, use_cache
            )
            summary = summarize(rows, cache_hits)
            report["runs"].append(
                {"backend": backend, "heuristics": heuristics, "summary": summary, "rows": rows}
            )
            print(f"  hit@{args.limit} = {summary['hit@k']}% | 自动纠正 = {summary['auto_corrected']}% "
                  f"| 平均 {summary['avg_seconds']}s | 缓存命中 {cache_hits}/{summary['n']} "
                  f"| 实际调用 {summary['paid_calls']}")
            for name, value in summary["by_category"].items():
                print(f"    {name}: {value}")

    print("\n=== 汇总 ===")
    print(f"{'配置':<30} {'hit@k':>7} {'自动纠正':>9} {'平均延迟':>9} {'实际调用':>9}")
    for run in report["runs"]:
        s = run["summary"]
        label = f"{run['backend']} + 启发式{'开' if run['heuristics'] else '关'}"
        print(f"{label:<30} {s['hit@k']:>6}% {s['auto_corrected']:>8}% {s['avg_seconds']:>8}s {s['paid_calls']:>9}")

    out = ROOT / "results" / f"search_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
