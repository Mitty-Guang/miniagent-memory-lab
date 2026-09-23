"""长期记忆检索器：可插拔接口，零依赖默认 + 可选向量检索。

- TfidfRetriever（默认）：中英混排轻量分词 + TF-IDF 余弦（零依赖，比字符 Jaccard 更能区分）；
- BigramRetriever：字符二元组 Jaccard（早期实现，保留用于对照）；
- EmbeddingRetriever（可选）：安装 `pip install fastembed` 后可用（首次使用会下载小模型）。

接口：rank(query, texts) -> List[float]（与 texts 等长的相关性分数）。
"""
import math
import re
from collections import Counter
from typing import List, Protocol

from mini_agent.memory_policies import bigrams, jaccard


def tokenize(text: str) -> List[str]:
    """中英混排的轻量分词：英文/数字按词，中文按字符二元组。"""
    lower = (text or "").lower()
    words = re.findall(r"[a-z0-9_]+", lower)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", lower))
    if not cjk:
        return words
    cjk_bigrams = [cjk[i : i + 2] for i in range(len(cjk) - 1)] or [cjk]
    return words + cjk_bigrams


class Retriever(Protocol):
    def rank(self, query: str, texts: List[str]) -> List[float]:
        ...


class BigramRetriever:
    """字符二元组 Jaccard（零依赖，早期默认）。"""

    def rank(self, query: str, texts: List[str]) -> List[float]:
        query_bg = bigrams(query)
        return [jaccard(query_bg, bigrams(text)) for text in texts]


class TfidfRetriever:
    """TF-IDF 余弦（默认）：IDF 在本次候选集合内估计，无需预建索引。"""

    def rank(self, query: str, texts: List[str]) -> List[float]:
        docs = [tokenize(text) for text in texts]
        query_tokens = tokenize(query)

        doc_freq: Counter = Counter()
        for doc in docs:
            for token in set(doc):
                doc_freq[token] += 1
        total_docs = max(len(docs), 1)

        def vector(tokens: List[str]) -> dict:
            counts = Counter(tokens)
            length = sum(counts.values()) or 1
            return {
                token: (count / length)
                * (math.log((total_docs + 1) / (doc_freq.get(token, 0) + 1)) + 1.0)
                for token, count in counts.items()
            }

        query_vec = vector(query_tokens)
        query_norm = math.sqrt(sum(value * value for value in query_vec.values())) or 1e-9

        scores: List[float] = []
        for doc in docs:
            doc_vec = vector(doc)
            dot = sum(value * doc_vec.get(token, 0.0) for token, value in query_vec.items())
            doc_norm = math.sqrt(sum(value * value for value in doc_vec.values())) or 1e-9
            scores.append(dot / (query_norm * doc_norm))
        return scores


class EmbeddingRetriever:
    """可选向量检索：需要 fastembed（未安装时给出清晰提示）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 取决于本地依赖
            raise ImportError(
                "EmbeddingRetriever 需要 fastembed：pip install fastembed。"
                "未安装时请使用默认的 TfidfRetriever。"
            ) from exc
        self.model = TextEmbedding(model_name=model_name)

    def rank(self, query: str, texts: List[str]) -> List[float]:
        import numpy as np

        vectors = list(self.model.embed([query] + list(texts)))
        query_vec, doc_vecs = vectors[0], vectors[1:]
        query_norm = float(np.linalg.norm(query_vec)) or 1e-9
        scores = []
        for doc_vec in doc_vecs:
            denom = query_norm * (float(np.linalg.norm(doc_vec)) or 1e-9)
            scores.append(float(np.dot(query_vec, doc_vec) / denom))
        return scores


def get_retriever(name: str = "tfidf"):
    """按名称取检索器：tfidf（默认）/ bigram / embedding。"""
    name = (name or "tfidf").lower()
    if name == "bigram":
        return BigramRetriever()
    if name == "embedding":
        return EmbeddingRetriever()
    return TfidfRetriever()
