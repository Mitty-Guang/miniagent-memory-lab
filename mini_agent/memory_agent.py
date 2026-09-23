"""带长期记忆的 Agent：read（检索注入）+ write（任务后写入）。

- read：任务开始前用任务文本检索历史记忆（排除当前会话），默认拼进系统提示词
  （memory_injection="system_prompt"；消融实验显示比独立 system 消息更快、更稳）；
- write：任务结束后把「任务 + 最终结果」压缩为一条记忆写回长期库；
- sandbox=True 时使用沙箱工具（子进程 / 路径白名单 / 命令黑名单）。

其余行为（ReAct 循环、固定预算短期记忆选择）与 BudgetedMiniAgent 完全一致。
"""
import os
from typing import Optional

from mini_agent.approval import ApprovalToolCollection, auto_approve
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_policies import BudgetedMiniAgent
from mini_agent.schema import Message, Role


class MemoryAgent(BudgetedMiniAgent):
    def __init__(
        self,
        llm,
        ltm: Optional[LongTermMemory] = None,
        session_id: str = "",
        task_id: str = "",
        retrieve_k: int = 3,
        approval_fn=None,
        trace=None,
        memory_injection: str = "system_prompt",
        sandbox: bool = False,
        **kwargs,
    ):
        super().__init__(llm=llm, **kwargs)
        self.ltm = ltm
        self.session_id = session_id
        self.task_id = task_id
        self.retrieve_k = retrieve_k
        self.retrieved = []
        self.trace = trace
        # message：注入为独立 system 消息；system_prompt：拼进系统提示词（默认，消融更优）
        self.memory_injection = memory_injection
        self.tools = ApprovalToolCollection(
            approval_fn=approval_fn or auto_approve,
            trace=trace,
            sandbox_root=os.getcwd() if sandbox else None,
        )

    async def run(self, user_input: str) -> str:
        base_prompt = None
        if self.ltm is not None:
            hits = self.ltm.search(
                user_input, k=self.retrieve_k, exclude_session=self.session_id
            )
            self.retrieved = hits
            if self.trace is not None:
                self.trace.log(
                    "memory_read",
                    {"query": user_input[:80], "hits": [h["id"] for h in hits]},
                )
            if hits:
                block = "[长期记忆] 与此任务相关的历史记忆：\n" + "\n".join(
                    f"- {h['text']}" for h in hits
                )
                if self.memory_injection == "system_prompt":
                    base_prompt = self.system_prompt
                    self.system_prompt = f"{base_prompt}\n\n{block}"
                else:
                    self.memory.add_message(Message(role=Role.SYSTEM, content=block))

        try:
            result = await super().run(user_input)
        finally:
            if base_prompt is not None:
                self.system_prompt = base_prompt

        if self.ltm is not None:
            summary = self._summarize(user_input)
            if summary:
                memory_id = self.ltm.add(
                    summary, session_id=self.session_id, task_id=self.task_id
                )
                if self.trace is not None:
                    self.trace.log("memory_write", {"id": memory_id, "text": summary[:100]})
        return result

    def _summarize(self, user_input: str) -> str:
        finals = [
            m.content
            for m in self.memory.messages
            if m.role == Role.ASSISTANT and m.content
        ]
        if not finals:
            return ""
        answer = finals[-1].strip().replace("\n", " ")[:160]
        return f"任务：{user_input.strip()[:120]}；结果：{answer}"
