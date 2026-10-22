"""显式的百炼 OpenAI 兼容 Embedding 适配，不依赖 LlamaIndex 全局默认值。"""

import math

import httpx
from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import Field, SecretStr


class BailianEmbedding(BaseEmbedding):
    api_key: SecretStr = Field(exclude=True)
    base_url: str
    dimension: int
    timeout_s: float = 20

    def _body(self, texts):
        return dict(model=self.model_name, input=texts, dimensions=self.dimension, encoding_format="float")

    def _parse(self, response: httpx.Response, count: int) -> list[list[float]]:
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda r: r["index"])
        if [r["index"] for r in rows] != list(range(count)):
            raise ValueError("embedding response indices mismatch")
        vectors = [r["embedding"] for r in rows]
        if any(len(v) != self.dimension or not all(math.isfinite(x) for x in v) for v in vectors):
            raise ValueError("embedding dimension or value mismatch")
        return vectors

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(self.base_url.rstrip("/") + "/embeddings", json=self._body(texts),
                                   headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"})
        return self._parse(response, len(texts))

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(self.base_url.rstrip("/") + "/embeddings", json=self._body(texts),
                                        headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"})
        return self._parse(response, len(texts))

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embeddings([query])[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embeddings([text])[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return (await self._aget_text_embeddings([query]))[0]

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return await self._aget_query_embedding(text)


def make_embedding(settings):
    if not settings.embed_api_key:
        raise ValueError("EMBED_API_KEY is required; no default embedding provider")
    return BailianEmbedding(model_name=settings.embed_model_name, api_key=SecretStr(settings.embed_api_key),
                            base_url=settings.embed_base_url, dimension=settings.embed_dimension,
                            timeout_s=settings.rag_timeout_s, embed_batch_size=10, num_workers=1)
