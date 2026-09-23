"""任务运行器：在独立临时工作目录里跑一个任务，收集评测指标。

支持三类任务：
- 单阶段任务（task["prompt"] / task["check"]）；
- 跨会话任务（task["phases"]：phase 1 教学、phase 2 使用，共享长期记忆库）；
- 可选 tracing（trace_path 落盘 JSONL）与工具审批（approval_fn）。
"""
import contextlib
import datetime
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from mini_agent.config import CountingLLM, llm_kwargs
from mini_agent.long_term_memory import LongTermMemory
from mini_agent.memory_agent import MemoryAgent
from mini_agent.schema import Message, Role
from mini_agent.tracing import TraceLogger

PROGRESS_PATH = Path(__file__).resolve().parents[1] / "progress.log"


def log_progress(line: str) -> None:
    """把进度追加到 progress.log（立即 flush，便于实时查看）。"""
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")
        f.flush()


def extract_final_answer(messages: List[Message]) -> str:
    """取最后一条有内容的 assistant 消息作为最终答案。"""
    for msg in reversed(messages):
        if msg.role == Role.ASSISTANT and msg.content:
            return msg.content
    return ""


def task_phases(task: Dict) -> List[Dict]:
    """统一单阶段/跨会话任务为 phases 列表。"""
    if task.get("phases"):
        return task["phases"]
    return [{"prompt": task["prompt"], "check": task["check"]}]


def task_units(task: Dict) -> List[Dict]:
    """统一三种任务形态为单元列表：
    - turns：同一会话多轮（Agent 复用、短期记忆累积）；
    - phases：跨会话多阶段（每阶段新 Agent、共享长期记忆）；
    - 单阶段：普通任务。
    """
    if task.get("turns"):
        return task["turns"]
    return task_phases(task)


async def run_agent_task(
    task: Dict,
    policy: str = "all",
    budget_chars: int = 1200,
    impact_priors: Optional[Dict[str, float]] = None,
    max_steps: int = 10,
    use_memory: bool = True,
    trace_path: Optional[str] = None,
    approval_fn: Optional[Callable[[str, dict], bool]] = None,
    memory_injection: str = "system_prompt",
    sandbox: bool = False,
) -> Dict:
    """跑一个任务（含全部阶段），返回聚合指标 + 最后阶段的消息流。"""
    workdir = tempfile.mkdtemp(prefix=f"miniagent_{task['id']}_")
    old_cwd = os.getcwd()
    os.chdir(workdir)

    trace = TraceLogger(trace_path) if trace_path else None
    ltm = (
        LongTermMemory(path=str(Path(workdir) / "ltm.sqlite3")) if use_memory else None
    )
    ltm_before = ltm.count() if ltm is not None else 0

    totals = {
        "steps": 0,
        "llm_calls": 0,
        "prompt_chars": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    answers: List[str] = []
    ltm_reads = 0
    success = True
    error = ""
    last_agent = None

    start = time.time()
    try:
        shared_session = bool(task.get("turns"))
        units = task_units(task)
        agent = None
        llm = None
        for index, unit in enumerate(units):
            if not shared_session or agent is None:
                llm = CountingLLM(**llm_kwargs(), trace=trace)
                agent = MemoryAgent(
                    llm=llm,
                    ltm=ltm,
                    session_id=f"{task['id']}#{'session' if shared_session else index}",
                    task_id=task["id"],
                    policy=policy,
                    budget_chars=budget_chars,
                    impact_priors=impact_priors,
                    max_steps=max_steps,
                    approval_fn=approval_fn,
                    trace=trace,
                    memory_injection=memory_injection,
                    sandbox=sandbox,
                )

            before = (
                llm.calls,
                llm.prompt_chars,
                llm.prompt_tokens,
                llm.completion_tokens,
                agent.current_step,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                await agent.run(unit["prompt"])

            final_answer = extract_final_answer(agent.memory.messages)
            answers.append(final_answer)
            try:
                ok = bool(unit["check"](final_answer, workdir))
            except Exception:
                ok = False
            success = success and ok

            totals["llm_calls"] += llm.calls - before[0]
            totals["prompt_chars"] += llm.prompt_chars - before[1]
            totals["prompt_tokens"] += llm.prompt_tokens - before[2]
            totals["completion_tokens"] += llm.completion_tokens - before[3]
            totals["steps"] += agent.current_step  # 每次 run() 重置，逐单元累加即为总数
            ltm_reads += len(agent.retrieved)
            last_agent = agent
    except Exception as exc:
        error = str(exc)
        success = False
    elapsed = time.time() - start

    ltm_writes = (ltm.count() - ltm_before) if ltm is not None else 0
    if ltm is not None:
        ltm.close()
    os.chdir(old_cwd)

    log_progress(
        f"policy={policy} task={task['id']} success={success} "
        f"steps={totals['steps']} llm_calls={totals['llm_calls']} "
        f"ltm_rw={ltm_reads}/{ltm_writes} seconds={round(elapsed, 2)}"
    )

    return {
        "task_id": task["id"],
        "policy": policy,
        "success": success,
        "steps": totals["steps"],
        "llm_calls": totals["llm_calls"],
        "prompt_chars": totals["prompt_chars"],
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "ltm_reads": ltm_reads,
        "ltm_writes": ltm_writes,
        "seconds": round(elapsed, 2),
        "final_answer": answers[-1] if answers else "",
        "answers": answers,
        "workdir": workdir,
        "error": error,
        "messages": last_agent.memory.messages if last_agent is not None else [],
        "trace_summary": trace.summary() if trace else {},
    }
