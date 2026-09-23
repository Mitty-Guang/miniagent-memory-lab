"""运行参数自动估计：让模型按任务复杂度选择「上下文预算」与「最大步数」。

动机（见 docs/SECONDARY_DEV.md 第 7 节）：预算与步数是两种不同资源——
预算不足会丢历史约束（答错），步数不足会做不完（答不完）。手工设置对使用者是负担，
因此这里用**一次轻量 LLM 调用**给出建议值，并用规则兜底：
- LLM 不可用 / 输出不合法 → 关键词规则（保证任何情况下都有合理默认）；
- 模型给出的值会被夹逼到安全区间（budget ≥ 300、max_steps ≥ 8），避免"省 token"式低估。
"""
import json
import re
from typing import Dict, Optional

# 与 docs/SECONDARY_DEV.md 第 7 节的扫描结论一致
BUDGET_RANGE = (300, 1200)
STEPS_RANGE = (8, 24)

# (关键词, 类别, 预算, 步数)：顺序即优先级
RULES = (
    (("上次", "之前", "继续", "偏好", "记得", "代号", "约定", "历史"), "跨会话", 1200, 12),
    (
        (
            "搜索", "联网", "查一下", "查查", "最新", "新闻", "天气", "路线", "规划",
            "机票", "酒店", "赛程", "门票", "股价", "汇率",
        ),
        "联网研究",
        1000,
        20,
    ),
    (("创建", "读取", "写入", "文件", "目录", "重命名", "复制", "备份", "统计", "批量"), "多步文件", 500, 12),
)

PROMPT = """你是任务复杂度评估器。请判断用户任务属于哪一类，并给出运行参数。
类别与参数对照：
- 单轮计算/问答：budget=300, max_steps=8
- 多步文件操作：budget=500, max_steps=12
- 跨会话/多轮（依赖"上次/之前/偏好/约定"等历史信息）：budget=1200, max_steps=12
- 联网研究（需要搜索或实时信息）：budget=1000, max_steps=20
只输出 JSON，不要任何解释：{"category": "类别", "budget": 300, "max_steps": 8, "reason": "一句话理由"}"""


def rule_based(task: str) -> Dict:
    """关键词规则兜底：任何任务都能给出一个合理默认。"""
    for keywords, category, budget, steps in RULES:
        hit = next((k for k in keywords if k in task), "")
        if hit:
            return {
                "category": category,
                "budget": budget,
                "max_steps": steps,
                "reason": f"命中关键词「{hit}」，按{category}任务估计",
                "source": "rule",
            }
    return {
        "category": "单轮",
        "budget": 300,
        "max_steps": 8,
        "reason": "未命中特殊关键词，按单轮任务估计",
        "source": "rule",
    }


def parse_plan(text: str) -> Optional[Dict]:
    """解析模型输出（容忍 ```json 代码块与前后杂文本），非法则返回 None。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        budget = int(data.get("budget") or 0)
        steps = int(data.get("max_steps") or 0)
    except (TypeError, ValueError):
        return None
    lo_b, hi_b = BUDGET_RANGE
    lo_s, hi_s = STEPS_RANGE
    if not (lo_b <= budget <= hi_b) or not (lo_s <= steps <= hi_s):
        return None
    return {
        "category": str(data.get("category") or "未知")[:20],
        "budget": budget,
        "max_steps": steps,
        "reason": str(data.get("reason") or "")[:120],
        "source": "llm",
    }


def clamp(plan: Dict) -> Dict:
    """夹逼到安全区间，避免模型低估导致答错/答不完。"""
    lo_b, hi_b = BUDGET_RANGE
    lo_s, hi_s = STEPS_RANGE
    plan["budget"] = min(hi_b, max(lo_b, int(plan["budget"])))
    plan["max_steps"] = min(hi_s, max(lo_s, int(plan["max_steps"])))
    return plan


async def estimate(task: str, llm) -> Dict:
    """优先用模型估计；失败或不合法则回退规则。返回 dict（含 category/budget/max_steps/reason/source）。"""
    fallback = rule_based(task)
    try:
        response = await llm.chat(
            messages=[{"role": "user", "content": f"用户任务：{task}"}],
            system_prompt=PROMPT,
        )
        plan = parse_plan(getattr(response, "content", "") or "")
    except Exception as exc:
        print(f"[auto_plan] 模型估计失败，回退规则: {exc}", flush=True)
        plan = None
    if not plan:
        fallback["reason"] += "（模型估计不可用，已回退规则）"
        return fallback
    return clamp(plan)
