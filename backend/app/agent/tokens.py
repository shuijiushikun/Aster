"""Local BPE estimates for prompt budgets; provider usage remains authoritative."""

import json
from functools import lru_cache

import tiktoken
from langchain_core.messages import convert_to_openai_messages
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.config import get_settings


@lru_cache(maxsize=1)
def _encoding():
    # LlamaIndex ships the cl100k_base vocabulary, so no runtime download is needed.
    # Reuse its initialization instead of creating a second tokenizer cache.
    from llama_index.core.utils import get_tokenizer

    get_tokenizer()
    return tiktoken.get_encoding("cl100k_base")


def text_tokens(text: str) -> int:
    # User-provided special-token strings are ordinary text, not control tokens.
    return len(_encoding().encode_ordinary(text))


def truncate_tokens(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoding = _encoding()
    ids = encoding.encode_ordinary(text)
    if len(ids) <= limit:
        return text
    ids = ids[:limit]
    # A token boundary may fall inside a multi-byte Chinese character or emoji.
    shortened = encoding.decode_bytes(ids).decode("utf-8", errors="ignore")
    while text_tokens(shortened) > limit:
        ids.pop()
        shortened = encoding.decode_bytes(ids).decode("utf-8", errors="ignore")
    return shortened


def context_tokens(messages, *, tools=None, response_format=None) -> int:
    # Count roles, tool-call IDs/arguments/results and schemas, without internal
    # context tags or already-reported usage. JSON framing is a local estimate;
    # DeepSeek and fallback providers apply their own server-side chat templates.
    payload = {"messages": convert_to_openai_messages(messages)}
    if tools:
        payload["tools"] = [tool if isinstance(tool, dict) else convert_to_openai_tool(tool)
                            for tool in tools]
    if response_format:
        payload["response_format"] = response_format
    return text_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))


def input_token_limit(max_tokens: int | None = None) -> int:
    settings = get_settings()
    total = settings.context_max_tokens if max_tokens is None else max_tokens
    return total - settings.max_output_tokens - settings.context_token_margin
