from typing import Literal

from pydantic import BaseModel, Field, model_validator


class KnowledgeMetadata(BaseModel):
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    category: Literal["general", "digital", "apparel"]
    model_code: str | None
    effective_status: Literal["active", "inactive"]
    references: list[str] = Field(min_length=1)
    source_path: str = ""
    content_hash: str = ""

    @model_validator(mode="after")
    def scope(self):
        if (self.category == "general") != (self.model_code is None):
            raise ValueError("general documents require model_code=null; product documents require a model")
        return self


class Evidence(BaseModel):
    document_id: str
    title: str
    revision: str
    chunk_index: int
    category: str
    model_code: str | None
    score: float = Field(allow_inf_nan=False)
    score_kind: Literal["cosine", "rrf", "rerank"]
    excerpt: str
    source_path: str
    references: list[str]


class RetrievalResult(BaseModel):
    query: str
    model_code: str | None
    status: Literal["ok", "insufficient_evidence", "unavailable"]
    sources: list[Evidence] = Field(default_factory=list, max_length=4)
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "ok") != bool(self.sources):
            raise ValueError("only ok results contain evidence")
        return self
