"""多 Agent 协作：Planner → Executor → Reviewer 的监督式流程。

- Planner：单次 LLM 调用，把任务拆成步骤清单（不用工具）；
- Executor：完整 ReAct Agent（工具 + 固定预算记忆 + 可选长期记忆）；
- Reviewer：单次 LLM 调用，检查结果是否满足任务要求（PASS / FAIL + 原因）；
- FAIL 时把审查意见反馈给 Executor 重试（最多 max_retries 次）。

对照脚本（单 Agent vs 多 Agent）见项目根目录 compare_multi_agent.py。
"""
from typing import Dict, Optional

from mini_agent.memory_agent import MemoryAgent
from mini_agent.schema import Role

PLANNER_PROMPT = (
    "你是任务规划者。把用户任务拆解成不超过 5 步的可执行步骤清单，"
    "只输出步骤本身，不要调用任何工具，不要执行任务。"
)
EXECUTOR_PROMPT = (
    "你是任务执行者。按给定的步骤计划使用工具完成任务；"
    "每次只做当前最必要的一步，根据工具结果决定下一步；完成后直接给出结果。"
)
REVIEWER_PROMPT = (
    "你是任务审查者。判断执行结果是否满足任务要求："
    "第一行只输出 PASS 或 FAIL，第二行给出不超过 50 字的原因。不要调用工具。"
)


def final_answer(agent: MemoryAgent) -> str:
    for msg in reversed(agent.memory.messages):
        if msg.role == Role.ASSISTANT and msg.content:
            return msg.content
    return ""


class MultiAgentTeam:
    def __init__(
        self,
        llm,
        policy: str = "relevance",
        budget_chars: int = 1200,
        ltm=None,
        session_id: str = "",
        task_id: str = "",
        max_steps: int = 10,
        max_retries: int = 1,
        approval_fn=None,
        trace=None,
    ):
        self.llm = llm
        self.policy = policy
        self.budget_chars = budget_chars
        self.ltm = ltm
        self.session_id = session_id
        self.task_id = task_id
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.approval_fn = approval_fn
        self.trace = trace
        self.plan = ""
        self.review = ""
        self.rounds = 0
        self.executor: Optional[MemoryAgent] = None

    async def run(self, task: str) -> str:
        # 1) Planner
        plan_reply = await self.llm.chat(
            [{"role": "user", "content": task}], system_prompt=PLANNER_PROMPT
        )
        self.plan = (plan_reply.content or "").strip()
        if self.trace is not None:
            self.trace.log("plan", {"plan": self.plan[:200]})

        # 2) Executor（+ 3) Reviewer，失败可带反馈重试）
        feedback = ""
        result = ""
        for attempt in range(self.max_retries + 1):
            executor = MemoryAgent(
                llm=self.llm,
                ltm=self.ltm,
                session_id=self.session_id,
                task_id=self.task_id,
                policy=self.policy,
                budget_chars=self.budget_chars,
                max_steps=self.max_steps,
                approval_fn=self.approval_fn,
                trace=self.trace,
            )
            executor.system_prompt = EXECUTOR_PROMPT

            prompt = task
            if self.plan:
                prompt += f"\n\n参考步骤计划：\n{self.plan}"
            if feedback:
                prompt += f"\n\n上一轮审查意见（请修正）：\n{feedback}"
            await executor.run(prompt)
            self.executor = executor
            result = final_answer(executor)

            review_reply = await self.llm.chat(
                [{"role": "user", "content": f"任务：{task}\n\n执行结果：{result}"}],
                system_prompt=REVIEWER_PROMPT,
            )
            self.review = (review_reply.content or "").strip()
            self.rounds = attempt + 1
            if self.trace is not None:
                self.trace.log("review", {"round": self.rounds, "review": self.review[:120]})

            if self.review.upper().startswith("PASS") or attempt == self.max_retries:
                break
            feedback = self.review

        return result
