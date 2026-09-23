"""HITL 演示：危险工具（bash_execute）先被人工拒绝、模型改用其他工具完成。

运行：
    .\\.venv\\Scripts\\python.exe demo_hitl.py

演示点：
1. 工具执行前走审批回调（approve / deny），被拒绝时把结构化原因回填给模型；
2. 模型据此改用 file_editor 完成同一任务（自适应而不是崩溃）；
3. 全过程写入 results/hitl_trace.jsonl（可用 trace.summary() 汇总）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from pathlib import Path

from mini_agent.config import CountingLLM, llm_kwargs
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_agent import MemoryAgent
from mini_agent.tracing import TraceLogger


async def main():
    trace_path = Path("results") / "hitl_trace.jsonl"
    trace = TraceLogger(str(trace_path))

    decisions = []

    def approval(name, args):
        """第一次 bash 调用拒绝，之后全部放行（模拟人工审批）。"""
        if name == "bash_execute" and not any(
            d["tool"] == "bash_execute" for d in decisions
        ):
            decisions.append({"tool": name, "approved": False})
            return False
        decisions.append({"tool": name, "approved": True})
        return True

    llm = CountingLLM(**llm_kwargs(), trace=trace)
    agent = MemoryAgent(
        llm=llm,
        ltm=LongTermMemory(),
        session_id="hitl-demo",
        approval_fn=approval,
        trace=trace,
        max_steps=8,
    )

    task = (
        "用 bash 命令创建一个文件 note.txt，内容为 HITL-OK；"
        "如果命令被拒绝，请改用其他工具完成。"
    )
    await agent.run(task)

    print("\n=== 审批记录 ===")
    for index, decision in enumerate(decisions, 1):
        print(f"{index}. {decision['tool']} -> {'放行' if decision['approved'] else '拒绝'}")
    print("\n=== Trace 汇总 ===")
    print(trace.summary())
    print(f"\n完整 trace: {trace_path}")


if __name__ == "__main__":
    asyncio.run(main())
