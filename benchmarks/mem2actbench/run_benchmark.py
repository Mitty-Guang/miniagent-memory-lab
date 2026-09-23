"""Mem2ActBench 适配器：在公开基准上验证本项目的长期记忆管线。

基准：Mem2ActBench（ACL 2026）——记忆驱动的工具调用：
用户跨会话提到过的偏好/事实（记忆）需要被正确检索并用来
「选对工具 + 填对参数」。数据来自 ToolACE / BFCL / OASST1 合成的真实业务会话。

简化协议（与论文完整协议的区别写在本目录 README）：
1. 对每个 QA，取其 source_conversation_ids 对应的**证据片段**，
   再随机混入若干**干扰片段**（来自其他会话），一起写入长期记忆库（LTM）；
2. 用 QA 的 query 走本项目检索（默认 TF-IDF）取 Top-k，注入上下文；
3. 模型通过 Function Calling 直接输出目标工具的调用；
4. 判分：工具名准确率 + 参数精确匹配（TA）+ 参数 F1；
5. 对照：无记忆（只给 query）——用于展示"记忆是否真的被用上"。

运行：
    .\\.venv\\Scripts\\python.exe benchmarks\\mem2actbench\\run_benchmark.py --limit 40 --top-k 3

数据准备（本仓库不提交数据）：
    git clone --depth 1 https://github.com/Cantaloupe-M/Mem2ActBench.git benchmarks/mem2actbench/data/repo
"""
import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (str(REPO),):
    if path not in sys.path:
        sys.path.insert(0, path)

from config import CountingLLM, llm_kwargs, warmup_async  # noqa: E402
from mini_agent.long_term_memory import LongTermMemory  # noqa: E402

DATA_DIR = HERE / "data" / "repo" / "Mem2ActBench"
QA_FILE = DATA_DIR / "qa_dataset.jsonl"
CONV_FILE = DATA_DIR / "toolmem_conversation.jsonl"
RESULTS_DIR = REPO / "results"

TYPE_MAP = {
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "str": "string",
    "string": "string",
    "list": "array",
    "dict": "object",
    "object": "object",
    "integer": "integer",
    "number": "number",
    "array": "array",
}

# OpenAI 兼容接口要求工具名匹配 ^[a-zA-Z0-9_-]+$；基准里存在 "/"、空格等字符
INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_name(name: str) -> str:
    """把工具名清洗成 API 合法形式（判分时两侧用同一函数归一）。"""
    cleaned = INVALID_NAME_CHARS.sub("_", (name or "").strip())
    return cleaned or "unknown_tool"


# ---------- 数据 ----------

def build_source_index(needed_ids: set, max_chars: int = 800) -> Dict[str, str]:
    """一次扫描对话库，建立 source_id -> 片段文本（user+assistant 拼接）。"""
    index: Dict[str, str] = {}
    buffer: Dict[str, List[str]] = {}

    with open(CONV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            session = json.loads(line)
            for turn in session.get("turns", []):
                source_id = turn.get("source_id")
                if not source_id or source_id not in needed_ids:
                    continue
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                buffer.setdefault(source_id, []).append(f"{turn.get('role')}: {content}")

    for source_id, parts in buffer.items():
        index[source_id] = "\n".join(parts)[:max_chars]
    return index


def build_distractors(exclude_ids: set, count: int, seed: int = 42, max_chars: int = 400) -> List[str]:
    """从其他会话里抽干扰片段（用于制造检索难度）。"""
    rng = random.Random(seed)
    pool: List[str] = []
    with open(CONV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            session = json.loads(line)
            for turn in session.get("turns", []):
                source_id = turn.get("source_id")
                if not source_id or source_id in exclude_ids or turn.get("role") != "user":
                    continue
                content = (turn.get("content") or "").strip()
                if 20 <= len(content) <= max_chars:
                    pool.append(f"{source_id}: {content}")
    rng.shuffle(pool)
    return pool[:count]


def load_qas(limit: int) -> List[Dict]:
    qas = []
    with open(QA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                qas.append(json.loads(line))
    return qas[:limit] if limit else qas


def to_openai_tool(schema: Dict) -> Dict:
    params = schema.get("parameters") or {}
    properties = {}
    for key, spec in (params.get("properties") or {}).items():
        item = dict(spec or {})
        item["type"] = TYPE_MAP.get(str(item.get("type", "string")).lower(), "string")
        properties[key] = item
    required = params.get("required") or list(properties.keys())
    return {
        "type": "function",
        "function": {
            "name": sanitize_name(schema.get("name", "unknown_tool")),
            "description": schema.get("description", ""),
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# ---------- 判分 ----------

def _norm(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def grade(call: Optional[Dict], gold: Dict) -> Tuple[bool, float]:
    """返回 (TA 是否通过, 参数 F1)。工具名与两侧都做同一套清洗后再比较。"""
    if not call:
        return False, 0.0
    name_ok = sanitize_name(call.get("name", "")) == sanitize_name(gold.get("name", ""))
    gold_args = {k: _norm(v) for k, v in (gold.get("arguments") or {}).items()}
    pred_args = {k: _norm(v) for k, v in (call.get("arguments") or {}).items()}
    if not gold_args:
        f1 = 1.0 if not pred_args else 0.0
        return name_ok and f1 == 1.0, f1
    hits = sum(1 for k, v in gold_args.items() if pred_args.get(k) == v)
    precision = hits / max(len(pred_args), 1)
    recall = hits / len(gold_args)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return name_ok and hits == len(gold_args), f1


# ---------- 运行 ----------

async def run_one(
    llm: CountingLLM,
    qa: Dict,
    index: Dict[str, str],
    distractors: List[str],
    top_k: int,
    mode: str,
) -> Dict:
    ltm = LongTermMemory(":memory:")
    evidence_ids = qa.get("source_conversation_ids") or []
    evidence = [index[sid] for sid in evidence_ids if sid in index]
    for text in evidence + distractors:
        ltm.add(text, session_id="history")

    hits = ltm.search(qa["query"], k=top_k)
    ltm.close()

    context = ""
    if mode == "memory" and hits:
        context = "[长期记忆] 与该请求相关的历史记录：\n" + "\n".join(
            f"- {h['text'][:400]}" for h in hits
        )

    tool = to_openai_tool(qa.get("target_tool_schema") or {})
    user = qa["query"]
    if context:
        user = f"{context}\n\n[当前请求] {user}"

    response = await llm.chat(
        [{"role": "user", "content": user}],
        system_prompt="你是助手。请根据历史记忆与当前请求，调用正确的工具并填写参数（只调用工具）。",
        tools=[tool],
    )

    call = None
    if response.tool_calls:
        first = response.tool_calls[0]
        try:
            args = json.loads(first["function"].get("arguments") or "{}")
        except Exception:
            args = {}
        call = {"name": first["function"].get("name"), "arguments": args}

    ta, f1 = grade(call, qa.get("tool_call") or {})
    return {
        "qa_id": qa.get("qa_id"),
        "mode": mode,
        "ta": ta,
        "param_f1": round(f1, 3),
        "pred": call,
        "gold": qa.get("tool_call"),
        "evidence_found": len(evidence),
        "retrieved": len(hits),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--distractors", type=int, default=40)
    parser.add_argument("--modes", type=str, default="memory,nomemory")
    args = parser.parse_args()

    if not QA_FILE.exists():
        print(f"未找到数据：{QA_FILE}\n请先按 README 下载 Mem2ActBench 到 benchmarks/mem2actbench/data/repo")
        return

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    await warmup_async()

    qas = load_qas(args.limit)
    needed = {sid for qa in qas for sid in (qa.get("source_conversation_ids") or [])}
    print(f"[data] {len(qas)} 个 QA，需要 {len(needed)} 个来源片段；建立索引中...", flush=True)
    index = build_source_index(needed)
    print(f"[data] 索引命中 {len(index)}/{len(needed)} 个来源片段", flush=True)
    distractors = build_distractors(exclude_ids=needed, count=args.distractors)
    print(f"[data] 干扰片段 {len(distractors)} 条/题", flush=True)

    llm = CountingLLM(**llm_kwargs())
    rows: List[Dict] = []
    for qa in qas:
        for mode in modes:
            row = await run_one(llm, qa, index, distractors, args.top_k, mode)
            rows.append(row)
            mark = "✅" if row["ta"] else ("🟡" if row["param_f1"] > 0 else "❌")
            print(
                f"[{qa['qa_id']}] {mode:8s} {mark} TA={row['ta']} F1={row['param_f1']} "
                f"evidence={row['evidence_found']}",
                flush=True,
            )
            await asyncio.sleep(0.2)

    print(f"\n=== Mem2ActBench 子集结果（{len(qas)} 题 × Top-{args.top_k}，"
          f"每題 {args.distractors} 条干扰） ===")
    print("| 模式 | Tool Accuracy | 平均参数 F1 |")
    print("| --- | --- | --- |")
    summary = {}
    for mode in modes:
        sub = [r for r in rows if r["mode"] == mode]
        n = len(sub) or 1
        ta = sum(1 for r in sub if r["ta"]) / n
        f1 = sum(r["param_f1"] for r in sub) / n
        summary[mode] = {"n": len(sub), "tool_accuracy": round(ta, 3), "avg_param_f1": round(f1, 3)}
        print(f"| {mode} | {ta:.1%} | {f1:.3f} |")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"mem2actbench_{stamp}.json"
    path.write_text(
        json.dumps({"summary": summary, "rows": rows, "config": vars(args)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {path}")


if __name__ == "__main__":
    asyncio.run(main())
