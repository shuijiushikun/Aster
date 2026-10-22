"""轻量词法稀疏召回：中文二元片段 + 英文/型号完整词，非学习型 BM25。"""

import math
import re
from collections import Counter
from hashlib import blake2s

from llama_index.core.vector_stores.types import VectorStoreQueryResult

SPARSE_VERSION = "zh-bigram-latin-logtf-v1"


def sparse_vectors(texts: list[str]) -> tuple[list[list[int]], list[list[float]]]:
    batch_indices, batch_values = [], []
    for text in texts:
        tokens = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text.lower())
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.extend(run[i:i+2] for i in range(max(1, len(run)-1)))
        counts = Counter(int.from_bytes(blake2s(t.encode(), digest_size=4).digest(), "big") for t in tokens)
        indices = sorted(counts)
        values = [1 + math.log(counts[i]) for i in indices]
        norm = math.sqrt(sum(v*v for v in values)) or 1
        batch_indices.append(indices)
        batch_values.append([v / norm for v in values])
    return batch_indices, batch_values


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
