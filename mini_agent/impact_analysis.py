"""离线干预分析：leave-one-out 测量每条记忆对最终决策的因果影响。

流程（对应 memcausal 的思路：不按“检索相关性”，按“对决策的实际影响”打分）：
1. 用 policy="all"（完整记忆）跑任务，得到最终答案；
2. 依次删除第 i 条消息（首条用户任务不可删），让模型仅基于剩余历史重新作答；
3. 比较反事实答案与原答案的相似度：impact_i = 1 - similarity；
4. 按 impact_key（角色 + 是否含数字）聚合，产出 results/impact_priors.json，
   供 BudgetedMemory(policy="impact") 在线选择使用。

说明：这是“干预分布内”的因果证据，不是结构因果模型；结论只对同协议有效。
"""
import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from mini_agent.config import CountingLLM, llm_kwargs
from mini_agent.memory_policies import (
    bigrams,
    impact_key,
    jaccard,
    repair_orphans,
    to_openai_messages,
)
from mini_agent.runner import log_progress, run_agent_task
from mini_agent.task_suite import TASKS

FINAL_SYSTEM_PROMPT = (
    "你是任务复盘助手：请基于给定的对话历史，直接给出该任务的最终答案，"
    "不要调用任何工具。"
)

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def answer_similarity(a: str, b: str) -> float:
    """反事实答案与原答案的相似度（文本二元组 + 数字一致性）。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a and not b:
        return 1.0
    text_score = jaccard(bigrams(a), bigrams(b))
    nums_a = set(re.findall(r"\d+(?:\.\d+)?", a))
    nums_b = set(re.findall(r"\d+(?:\.\d+)?", b))
    if nums_a or nums_b:
        return round(0.6 * text_score + 0.4 * jaccard(nums_a, nums_b), 4)
    return round(text_score, 4)


async def analyze_task(task: Dict, budget_chars: int = 1200) -> Dict:
    """对单个任务做 leave-one-out 干预分析。"""
    result = await run_agent_task(task, policy="all", budget_chars=budget_chars)
    messages = result["messages"]
    original = result["final_answer"]

    if not original or "LLM调用失败" in original:
        print(f"[impact] 跳过 {task['id']}：全量运行失败", flush=True)
        log_progress(f"impact task={task['id']} SKIPPED full-run-failed")
        return {
            "task_id": task["id"],
            "original_answer": original,
            "success_full_context": False,
            "n_messages": len(messages),
            "impacts": [],
        }

    llm = CountingLLM(**llm_kwargs())
    impacts: List[Dict] = []

    for i, msg in enumerate(messages):
        if i == 0:  # 任务定义不可删
            continue
        ablated = [m for j, m in enumerate(messages) if j != i]
        ablated = repair_orphans(ablated)
        if not ablated:
            continue
        response = await llm.chat(
            to_openai_messages(ablated),
            system_prompt=FINAL_SYSTEM_PROMPT,
            tools=None,
        )
        counterfactual = response.content or ""
        if "LLM调用失败" in counterfactual:
            impacts.append(
                {
                    "index": i,
                    "role": msg.role.value,
                    "key": impact_key(msg),
                    "similarity": None,
                    "impact": None,
                    "failed": True,
                    "content_preview": (msg.content or "").replace("\n", " ")[:60],
                }
            )
            continue
        similarity = answer_similarity(counterfactual, original)
        impacts.append(
            {
                "index": i,
                "role": msg.role.value,
                "key": impact_key(msg),
                "similarity": similarity,
                "impact": round(1 - similarity, 4),
                "content_preview": (msg.content or "").replace("\n", " ")[:60],
            }
        )

    impacts_list = impacts
    avg = sum(item["impact"] for item in impacts_list) / max(1, len(impacts_list))
    log_progress(
        f"impact task={task['id']} messages={len(messages)} "
        f"avg_impact={avg:.3f} n_interventions={len(impacts_list)}"
    )

    return {
        "task_id": task["id"],
        "original_answer": original,
        "success_full_context": result["success"],
        "n_messages": len(messages),
        "impacts": impacts,
    }


def aggregate_priors(details: List[Dict]) -> Dict[str, float]:
    """按 impact_key 聚合平均决策影响（跳过失败的干预）。"""
    groups: Dict[str, List[float]] = defaultdict(list)
    for record in details:
        for item in record["impacts"]:
            if item.get("impact") is None:
                continue
            groups[item["key"]].append(item["impact"])
    return {key: round(sum(vals) / len(vals), 4) for key, vals in groups.items()}


def summarize(details: List[Dict]) -> None:
    """打印每个任务的影响分布。"""
    print("\n=== 离线干预分析（决策影响） ===")
    for record in details:
        values = [
            item["impact"]
            for item in record["impacts"]
            if item.get("impact") is not None
        ]
        if not values:
            print(f"- {record['task_id']}: 无有效干预结果")
            continue
        avg = sum(values) / len(values)
        print(
            f"- {record['task_id']}: 消息 {record['n_messages']} 条, "
            f"平均影响 {avg:.3f}, 最大影响 {max(values):.3f}, "
            f"零影响条数 {sum(1 for v in values if v < 0.05)}"
        )


async def run(limit: int = 0, budget_chars: int = 1200) -> Dict[str, float]:
    # 跨会话任务（phases）的干预语义不同，先跳过；只分析单阶段任务
    candidates = TASKS[:limit] if limit else TASKS
    tasks = [t for t in candidates if not t.get("phases")]
    skipped = len(candidates) - len(tasks)
    if skipped:
        print(f"[impact] 跳过 {skipped} 个跨会话任务（多阶段干预暂不分析）", flush=True)
    details = []
    for task in tasks:
        print(f"[impact] 分析任务: {task['id']} ...", flush=True)
        details.append(await analyze_task(task, budget_chars=budget_chars))
        await asyncio.sleep(1.0)  # 缓解限流

    priors = aggregate_priors(details)
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "impact_details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / "impact_priors.json").write_text(
        json.dumps(priors, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summarize(details)
    print(f"\n先验表已保存: {RESULTS_DIR / 'impact_priors.json'}")
    print(json.dumps(priors, ensure_ascii=False))
    return priors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务（0=全部）")
    parser.add_argument("--budget", type=int, default=1200)
    args = parser.parse_args()
    from mini_agent.config import warmup

    warmup()
    asyncio.run(run(limit=args.limit, budget_chars=args.budget))
