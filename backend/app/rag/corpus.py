"""JSON front matter（YAML 的子集）+ Markdown；按小节切分，固定点 ID。"""

import hashlib
import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode

from app.config import BACKEND_ROOT
from app.rag.schemas import KnowledgeMetadata

KNOWLEDGE_DIR = BACKEND_ROOT / "data" / "knowledge"
INDEX_VERSION = "after-sales-v1"
OWNER = "customer-support-agent"
GENERAL_MODEL = "__general__"


def load_corpus(directory: Path = KNOWLEDGE_DIR) -> tuple[list[KnowledgeMetadata], list[TextNode], str]:
    documents, sections = [], []
    splitter = SentenceSplitter(chunk_size=384, chunk_overlap=40)
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
        if not match:
            raise ValueError(f"missing JSON metadata: {path.name}")
        meta = KnowledgeMetadata.model_validate(json.loads(match[1]))
        meta.source_path = f"data/knowledge/{path.name}"
        meta.content_hash = hashlib.sha256(raw.encode()).hexdigest()
        if meta.effective_status != "active":
            continue
        if any(d.document_id == meta.document_id for d in documents):
            raise ValueError(f"multiple active revisions: {meta.document_id}")
        documents.append(meta)
        chunks = []
        for section in re.split(r"(?m)(?=^## )", match[2]):
            if section.strip():
                chunks.extend(splitter.split_text(section.strip()))
        if not chunks:
            raise ValueError(f"empty document: {path.name}")
        sections.append(chunks)
    if not documents:
        raise ValueError("no active knowledge documents")
    generation = hashlib.sha256((INDEX_VERSION + "".join(d.content_hash for d in documents)).encode()).hexdigest()
    nodes = []
    for meta, chunks in zip(documents, sections, strict=True):
        for i, text in enumerate(chunks):
            metadata = meta.model_dump()
            metadata.update(owner=OWNER, record_type="chunk", index_generation=generation,
                            model_code=meta.model_code or GENERAL_MODEL, chunk_index=i)
            nodes.append(TextNode(
                id_=str(uuid5(NAMESPACE_URL, f"{OWNER}/{meta.document_id}/{meta.revision}/{i}")),
                text=f"{meta.title}\n{text}", metadata=metadata,
                # Qdrant 适配器的 document_id 来自 SOURCE，普通 metadata 同名项会被覆盖。
                relationships={NodeRelationship.SOURCE: RelatedNodeInfo(node_id=meta.document_id)},
                excluded_embed_metadata_keys=list(metadata), excluded_llm_metadata_keys=list(metadata),
            ))
    return documents, nodes, generation
