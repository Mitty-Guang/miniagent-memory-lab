"""长周期 / 真实复杂任务评测：多轮、多文件、容错、联网研究（确定性判分）。

与 `mini_agent/task_suite.py`（单步小任务）互补，本套任务的特征：
- **多轮**：同一会话连续 2~3 轮（规范 → 产出 → 变更），验证短期记忆与指令遵循；
- **多文件 + 长链**：预置输入文件（CSV/JSON/损坏文件），要求合并/清洗/容错/条件分支；
- **代码实现**：要求写出可运行脚本并自测（run 出来验判分）；
- **联网研究**：要求区分「已核实事实」与「不确定项」，链接可追溯。

用法：
    python scripts/run_long_horizon.py --limit 4
    python scripts/run_long_horizon.py --ids lh_sales_report,lh_web_research_uncertainty
    python scripts/run_long_horizon.py --families 数据管道,多轮项目 --policy relevance

判分：任务文件里的 `check` 表达式在受限命名空间求值（helpers 见 build_namespace），
全部确定性、不依赖 LLM judge。结果写入 results/long_horizon_<ts>.json。
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_agent.config import CountingLLM, llm_kwargs, load_env_file  # noqa: E402
from mini_agent.long_term_memory import LongTermMemory  # noqa: E402
from mini_agent.memory_agent import MemoryAgent  # noqa: E402
from mini_agent.schema import Role  # noqa: E402
from mini_agent.tracing import TraceLogger  # noqa: E402

load_env_file(str(ROOT / ".env"))   # 任务在临时目录里执行，必须显式加载仓库根的 .env

TASKS_FILE = ROOT / "benchmarks" / "long_horizon" / "tasks.jsonl"
RESULTS_DIR = ROOT / "results"


# ---------------- 判分 helpers ----------------
def build_namespace(workdir: str, answer: str) -> dict:
    base = Path(workdir)

    def file_text(name: str) -> str:
        path = base / name
        return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""

    def file_has(name: str, text: str) -> bool:
        return text in file_text(name)

    def json_has(name: str, expected: dict) -> bool:
        try:
            data = json.loads(file_text(name))
        except Exception:
            return False
        return all(str(data.get(k)) == str(v) for k, v in expected.items()) and len(data) == len(expected)

    def count_rows(name: str) -> int:
        text = file_text(name).strip()
        return max(0, len(text.splitlines()) - 1) if text else 0

    def link_count(name: str) -> int:
        return len(set(re.findall(r"https?://[^\s)\]\"'>]+", file_text(name))))

    def answer_has(text: str) -> bool:
        return text in (answer or "")

    def py_ok(script: str, expect_output: str = "") -> bool:
        path = base / script
        if not path.exists():
            return False
        try:
            proc = subprocess.run(
                [sys.executable, script],
                cwd=str(base),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        return (expect_output in proc.stdout) if expect_output else True

    return {
        "file_text": file_text,
        "file_has": file_has,
        "json_has": json_has,
        "count_rows": count_rows,
        "link_count": link_count,
        "answer_has": answer_has,
        "py_ok": py_ok,
        "workdir": workdir,
        "answer": answer,
    }


def evaluate(expr: str, workdir: str, answer: str) -> bool:
    safe_builtins = {
        "len": len, "str": str, "int": int, "float": float, "bool": bool, "min": min,
        "max": max, "any": any, "all": all, "sorted": sorted, "set": set, "list": list,
        "dict": dict, "sum": sum, "abs": abs, "round": round,
    }
    try:
        return bool(eval(expr, {"__builtins__": safe_builtins}, build_namespace(workdir, answer)))
    except Exception as exc:
        print(f"    [check-error] {type(exc).__name__}: {exc}")
        return False


def load_tasks() -> list:
    """兼容多行 JSON 与 JSONL：用 raw_decode 逐个解析连续的 JSON 对象。"""
    text = TASKS_FILE.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    tasks = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \n\r\t,":
            index += 1
        if index >= len(text):
            break
        obj, end = decoder.raw_decode(text, index)
        tasks.append(obj)
        index = end
    return tasks


def final_answer_of(agent: MemoryAgent) -> str:
    for msg in reversed(agent.memory.messages):
        if msg.role == Role.ASSISTANT and msg.content:
            return str(msg.content)
    return ""


async def run_task(task: dict, policy: str, budget: int, max_steps: int) -> dict:
    workdir = tempfile.mkdtemp(prefix=f"lh_{task['id']}_")
    for item in task.get("setup") or []:
        path = Path(workdir) / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(workdir)
    llm = CountingLLM(**llm_kwargs(), trace=TraceLogger(), max_retries=6, base_delay=2.0)
    ltm = LongTermMemory(path=str(Path(workdir) / "ltm.sqlite3"))
    started = time.time()
    units = task["turns"]
    steps = 0
    checks = []
    answers = []
    error = ""
    agent = None
    try:
        agent = MemoryAgent(
            llm=llm,
            ltm=ltm,
            session_id=f"{task['id']}#session",
            policy=policy,
            budget_chars=budget,
            max_steps=task.get("max_steps") or max_steps,
        )
        for index, unit in enumerate(units):
            print(f"  · 轮 {index + 1}/{len(units)}: {unit['prompt'][:46]}...", flush=True)
            await agent.run(unit["prompt"])
            steps += agent.current_step
            answer = final_answer_of(agent)
            answers.append(answer)
            ok = evaluate(unit["check"], workdir, answer)
            checks.append(ok)
            print(f"    判分: {'✅' if ok else '❌'}", flush=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
        checks.append(False)
    finally:
        os.chdir(old_cwd)
        try:
            await llm.client.close()
        except Exception:
            pass
        ltm.close()

    return {
        "task_id": task["id"],
        "family": task.get("family"),
        "level": task.get("level"),
        "needs_web": bool(task.get("needs_web")),
        "turns": len(units),
        "success": bool(checks) and all(checks),
        "checks": checks,
        "steps": steps,
        "llm_calls": llm.calls,
        "prompt_tokens": llm.prompt_tokens,
        "completion_tokens": llm.completion_tokens,
        "seconds": round(time.time() - started, 1),
        "error": error,
        "final_answer": (answers[-1] if answers else "")[:300],
        "workdir": workdir,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="", help="逗号分隔的任务 id")
    parser.add_argument("--families", default="", help="逗号分隔的任务族")
    parser.add_argument("--policy", default="relevance")
    parser.add_argument("--budget", type=int, default=1200)
    parser.add_argument("--max-steps", type=int, default=14)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    tasks = load_tasks()
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        tasks = [t for t in tasks if t["id"] in wanted]
    if args.families:
        wanted = {x.strip() for x in args.families.split(",") if x.strip()}
        tasks = [t for t in tasks if t.get("family") in wanted]
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"长周期任务评测：{len(tasks)} 个任务（策略 {args.policy} / 预算 {args.budget}）\n")
    rows = []
    for task in tasks:
        print(f"▶ {task['id']}（{task.get('family')} / {task.get('level')} / 联网={bool(task.get('needs_web'))}）")
        row = await run_task(task, args.policy, args.budget, args.max_steps)
        rows.append(row)
        print(f"  结果: {'✅ 通过' if row['success'] else '❌ 未通过'} | 步数 {row['steps']} | "
              f"调用 {row['llm_calls']} | {row['seconds']}s | tokens {row['prompt_tokens']}/{row['completion_tokens']}"
              f"{' | 错误: ' + row['error'] if row['error'] else ''}\n")
        await asyncio.sleep(args.sleep)

    total = len(rows)
    ok = sum(1 for r in rows if r["success"])
    print("=== 汇总 ===")
    print(f"{'任务':<28} {'族':<10} {'等级':<4} {'结果':<6} {'步数':>4} {'调用':>4} {'耗时':>7}")
    for row in rows:
        print(f"{row['task_id']:<28} {str(row['family']):<10} {str(row['level']):<4} "
              f"{'通过' if row['success'] else '失败':<6} {row['steps']:>4} {row['llm_calls']:>4} {row['seconds']:>6}s")
    print(f"\n通过率: {ok}/{total} = {round(ok / total * 100, 1)}%（按轮次判分全过才算通过）")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"long_horizon_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "policy": args.policy,
                    "budget": args.budget, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已保存：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
