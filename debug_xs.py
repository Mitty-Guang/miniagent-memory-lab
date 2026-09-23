"""调试脚本：复现跨会话任务 xs_project_code，打印每次 think() 实际发出的消息角色序列。

运行：
    .\\.venv\\Scripts\\python.exe debug_xs.py
"""
import asyncio
import contextlib
import io
import os
import tempfile
from pathlib import Path

from config import CountingLLM, llm_kwargs
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_agent import MemoryAgent
from mini_agent.schema import Role
from task_suite import TASKS


async def main():
    task = next(t for t in TASKS if t["id"] == "xs_project_code")
    for run_index in range(3):
        workdir = tempfile.mkdtemp(prefix="dbg_xs_")
        old_cwd = os.getcwd()
        os.chdir(workdir)
        ltm = LongTermMemory(path=str(Path(workdir) / "ltm.sqlite3"))

        print(f"\n{'=' * 70}\n[run {run_index + 1}] workdir={workdir}")

        for phase_index, phase in enumerate(task["phases"]):
            llm = CountingLLM(**llm_kwargs())
            sent = []
            original_chat = llm.chat

            async def spy(messages, system_prompt=None, tools=None, _sent=sent, _orig=original_chat):
                _sent.append([m.get("role") for m in messages])
                return await _orig(messages, system_prompt=system_prompt, tools=tools)

            llm.chat = spy
            agent = MemoryAgent(
                llm=llm,
                ltm=ltm,
                session_id=f"{task['id']}#{phase_index}",
                task_id=task["id"],
                policy="relevance",
                budget_chars=500,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                await agent.run(phase["prompt"])

            print(f"  phase{phase_index + 1} 用户: {phase['prompt']}")
            print(f"  phase{phase_index + 1} 记忆注入: {len(agent.retrieved)} 条")
            if phase_index == 1:
                for i, roles in enumerate(sent):
                    print(f"    第{i + 1}次 think() 消息角色: {roles}")
            final = ""
            for msg in reversed(agent.memory.messages):
                if msg.role == Role.ASSISTANT and msg.content:
                    final = msg.content
                    break
            print(f"  phase{phase_index + 1} 最终回答: {final[:120]}")
            try:
                ok = phase["check"](final, workdir)
            except Exception as exc:
                ok = False
            print(f"  phase{phase_index + 1} 判分: {'PASS' if ok else 'FAIL'}")

        ltm.close()
        os.chdir(old_cwd)


if __name__ == "__main__":
    asyncio.run(main())
