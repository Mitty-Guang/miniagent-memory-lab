"""LangChain 集成：把本项目的长期记忆库（LTM）适配成 LangChain Retriever，并用 LCEL 组一条 RAG 链。

覆盖的 LangChain 概念（面试常问）：
- **BaseRetriever**：自定义检索器的标准接口（`_get_relevant_documents` / 异步版），
  把 `mini_agent/long_term_memory.py`（SQLite + TF-IDF/bigram/embedding 三种检索器）接进 LangChain 生态；
- **Document / metadata**：检索结果带 score / session_id / kind，可直接做引用与过滤；
- **LCEL（`|` 管道）**：`ChatPromptTemplate | LLM | StrOutputParser()`，
  以及 `{"memory": itemgetter("question") | retriever | format_docs, ...}` 的并行分发写法；
- **与手写主链路的关系**：核心 `mini_agent/` 仍不依赖 LangChain；这里是"同一份记忆库、
  两种消费方式"的适配层（证明抽象没锁死在框架里）。

运行：
    .\\.venv312\\Scripts\\python.exe extras\\langgraph_compare\\langchain_retriever.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from langchain_core.documents import Document  # noqa: E402
from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langchain_core.retrievers import BaseRetriever  # noqa: E402
from operator import itemgetter  # noqa: E402

from llm_factory import get_llm  # noqa: E402

from mini_agent.long_term_memory import LongTermMemory  # noqa: E402


class LTMRetriever(BaseRetriever):
    """把项目自研的长期记忆库包成 LangChain Retriever。"""

    ltm: Any
    k: int = 3

    def _to_documents(self, query: str) -> List[Document]:
        hits = self.ltm.search(query, k=self.k) if self.ltm is not None else []
        return [
            Document(
                page_content=hit.get("text", ""),
                metadata={
                    "id": hit.get("id"),
                    "score": hit.get("score"),
                    "session_id": hit.get("session_id"),
                    "kind": hit.get("kind"),
                },
            )
            for hit in hits
        ]

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:  # type: ignore[override]
        return self._to_documents(query)

    async def _aget_relevant_documents(self, query: str, *, run_manager=None) -> List[Document]:  # type: ignore[override]
        # 说明：LTM 的 SQLite 连接绑定创建线程，故直接调用（本地查询毫秒级，不阻塞风险可忽略）；
        # 如需丢线程池，应让 LongTermMemory 以 check_same_thread=False 建连接。
        return self._to_documents(query)


def format_docs(docs: List[Document]) -> str:
    if not docs:
        return "（无相关历史记忆）"
    return "\n".join(
        f"- [{d.metadata.get('kind', 'memory')} score={d.metadata.get('score')}] {d.page_content}"
        for d in docs
    )


def build_memory_chain(retriever: LTMRetriever):
    """LCEL：检索（并行分发）→ 提示词 → 模型 → 纯文本输出。"""
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是带长期记忆的助手。以下是从长期记忆库检索到的历史记忆（可能为空）：\n{memory}\n"
                "若记忆与问题相关就据此回答，并在答案里注明依据；否则直接回答。",
            ),
            ("user", "{question}"),
        ]
    )
    return (
        {
            "memory": itemgetter("question") | retriever | format_docs,
            "question": itemgetter("question"),
        }
        | prompt
        | get_llm()
        | StrOutputParser()
    )


def seed_memory() -> LongTermMemory:
    ltm = LongTermMemory(path=":memory:")
    ltm.add("用户的项目代号是 ORION，报告统一放在 reports 目录下，数字保留两位小数。",
            session_id="demo-1", kind="preference")
    ltm.add("用户之前问过诺坎普球场参观信息，关注交通方式与门票。",
            session_id="demo-2", kind="summary")
    return ltm


async def main() -> None:
    ltm = seed_memory()
    retriever = LTMRetriever(ltm=ltm, k=3)

    # ① 直接用 Retriever 接口检索（不经过模型）
    docs = await retriever.ainvoke("我的报告应该放在哪里？")
    print("[Retriever] 命中", len(docs), "条：")
    for doc in docs:
        print(f"  - score={doc.metadata['score']} | {doc.page_content[:48]}")

    # ② LCEL 链：检索 + 生成
    chain = build_memory_chain(retriever)
    for question in ("我的项目代号是什么？", "报告里的数字要怎么保留？"):
        answer = await chain.ainvoke({"question": question})
        print(f"\n[LCEL] Q: {question}\n        A: {answer.strip()[:160]}")


if __name__ == "__main__":
    asyncio.run(main())
