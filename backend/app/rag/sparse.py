"""BM25：中文二元片段与完整英文/型号词，Qdrant 在查询时计算 IDF。"""

import math
import re
from collections import Counter
from hashlib import blake2s

from llama_index.core.vector_stores.types import VectorStoreQueryResult

SPARSE_VERSION = "zh-bigram-latin-bm25-v1"
BM25_K1 = 1.2
BM25_B = 0.75


def token_counts(text: str) -> Counter[int]:
    tokens = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i:i+2] for i in range(max(1, len(run)-1)))
    return Counter(int.from_bytes(blake2s(t.encode(), digest_size=4).digest(), "big") for t in tokens)


def average_document_length(texts: list[str]) -> float:
    if not texts:
        raise ValueError("BM25 requires a nonempty corpus")
    return max(1.0, sum(sum(token_counts(text).values()) for text in texts) / len(texts))


class BM25Encoder:
    def __init__(self, average_length: float | None = None):
        if average_length is not None and (not math.isfinite(average_length) or average_length <= 0):
            raise ValueError("average_length must be finite and positive")
        self.average_length = average_length

    def documents(self, texts: list[str]) -> tuple[list[list[int]], list[list[float]]]:
        if self.average_length is None:
            raise ValueError("average_length must be computed over the whole corpus before indexing")
        batch_indices, batch_values = [], []
        for text in texts:
            counts = token_counts(text)
            length = sum(counts.values())
            normalization = BM25_K1 * (1 - BM25_B + BM25_B * length / self.average_length)
            indices = sorted(counts)
            batch_indices.append(indices)
            batch_values.append([counts[i] * (BM25_K1 + 1) / (counts[i] + normalization) for i in indices])
        return batch_indices, batch_values

    def queries(self, texts: list[str]) -> tuple[list[list[int]], list[list[float]]]:
        # 查询只标记不同词项；IDF 由集合的 Modifier.IDF 计算，避免重复加权。
        indices = [sorted(token_counts(text)) for text in texts]
        return indices, [[1.0] * len(row) for row in indices]


def reciprocal_rank_fusion(dense: VectorStoreQueryResult, sparse: VectorStoreQueryResult,
                           alpha: float = 0.5, top_k: int = 12) -> VectorStoreQueryResult:
    # 只融合排名，不把余弦相似度与稀疏内积分数直接相加。
    scores, nodes = {}, {}
    for result in (dense, sparse):
        for rank, node in enumerate(result.nodes or []):
            nodes[node.node_id] = node
            scores[node.node_id] = scores.get(node.node_id, 0) + 1 / (60 + rank + 1)
    ids = sorted(scores, key=lambda i: (-scores[i], i))[:top_k]
    return VectorStoreQueryResult(nodes=[nodes[i] for i in ids], ids=ids,
                                  similarities=[scores[i] for i in ids])
