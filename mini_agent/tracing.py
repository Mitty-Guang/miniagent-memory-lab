"""结构化 tracing：把 LLM / 工具 / 审批事件写入 JSONL，便于回放与统计。"""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional


class TraceLogger:
    """事件流记录器。path 为空时只保留在内存中（评测批跑默认关闭落盘）。"""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else None
        self.events: List[Dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, data: Dict) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **data}
        self.events.append(record)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summary(self) -> Dict:
        llm_events = [e for e in self.events if e["event"] == "llm"]
        tool_events = [e for e in self.events if e["event"] == "tool"]
        approvals = [e for e in self.events if e["event"] == "approval"]
        latencies = [e.get("latency", 0) for e in llm_events if e.get("latency")]
        return {
            "llm_calls": len(llm_events),
            "tool_calls": len(tool_events),
            "approvals": len(approvals),
            "rejected": sum(1 for e in approvals if not e.get("approved", True)),
            "prompt_tokens": sum(e.get("prompt_tokens", 0) for e in llm_events),
            "completion_tokens": sum(e.get("completion_tokens", 0) for e in llm_events),
            "latency_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0,
            "latency_max": round(max(latencies), 3) if latencies else 0,
        }
