"""长期记忆层：SQLite 持久化 + 可插拔检索（跨会话）。

对应 Agent 记忆的 write / read 两个环节：
- write：任务结束后把「任务 + 结果」压缩成一条记忆写入；
- read：新任务开始时按相关性检索历史记忆，注入上下文（可跨会话）。

检索默认用 TF-IDF（零依赖，见 mini_agent/retrieval.py），
可通过 retriever 参数替换为 BigramRetriever 或 EmbeddingRetriever（fastembed）。
"""
import sqlite3
import time
from typing import Dict, List, Optional

from mini_agent.retrieval import TfidfRetriever


class LongTermMemory:
    """SQLite 长期记忆库。path=":memory:" 时为进程内临时库。"""

    def __init__(self, path: str = ":memory:", retriever=None):
        self.path = path
        self.retriever = retriever or TfidfRetriever()
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                task_id TEXT DEFAULT '',
                kind TEXT DEFAULT 'summary',
                created_at REAL
            )
            """
        )
        self.conn.commit()

    def add(
        self,
        text: str,
        session_id: str = "",
        task_id: str = "",
        kind: str = "summary",
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO memories (text, session_id, task_id, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, session_id, task_id, kind, time.time()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def search(
        self,
        query: str,
        k: int = 3,
        exclude_session: Optional[str] = None,
    ) -> List[Dict]:
        """按与 query 的相关性检索历史记忆（默认排除当前会话）。"""
        rows = self.conn.execute(
            "SELECT id, text, session_id, task_id, kind, created_at FROM memories"
        ).fetchall()
        candidates = [
            (rid, text, sid, tid, kind, ts)
            for rid, text, sid, tid, kind, ts in rows
            if not (exclude_session and sid == exclude_session)
        ]
        if not candidates:
            return []

        scores = self.retriever.rank(query, [c[1] for c in candidates])
        scored = []
        for (rid, text, sid, tid, kind, ts), score in zip(candidates, scores):
            if score > 0:
                scored.append(
                    (
                        score,
                        ts,
                        {
                            "id": rid,
                            "text": text,
                            "session_id": sid,
                            "task_id": tid,
                            "kind": kind,
                            "score": round(float(score), 4),
                        },
                    )
                )
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item for _, _, item in scored[:k]]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def all(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT id, text, session_id, task_id, kind FROM memories ORDER BY id"
        ).fetchall()
        return [
            {"id": r[0], "text": r[1], "session_id": r[2], "task_id": r[3], "kind": r[4]}
            for r in rows
        ]

    def close(self) -> None:
        self.conn.close()
