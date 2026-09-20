"""LLM client factories for each debate node (ADR-002: everything via AWS Bedrock).

ADR-002 (Accepted) says all LLM calls, Mistral Large 2 included, go through the
Bedrock VPC endpoint with IAM-role auth, and Zone A holds zero API keys. This module
implements exactly that with `langchain_aws.ChatBedrockConverse`; there is no code path
that reads a provider API key. ASSUMPTION (ALI-41): Mistral Large 2 is served from
Bedrock in the target region; if the owner instead wants Mistral's own EU endpoint,
ADR-002 and the "no API keys in Zone A" rule must be amended first.

LangChain chat models return `AIMessage` objects (whose `content` is a `str` OR a list of
content blocks), never `str`. `ChatTextClient` adapts them to the `LLMClient` protocol
so nodes always receive a plain `str`. Provider SDK imports are lazy, so importing this
module (or faking `LLMClient` in tests) needs no provider package and does no network I/O.
"""

from __future__ import annotations

from typing import Any, Protocol

from .config import CognitiveSettings


class LLMClient(Protocol):
    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str: ...


class LLMResponseError(RuntimeError):
    """The provider reply contained no usable text."""


class _AsyncChat(Protocol):
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any: ...


def message_text(message: object) -> str:
    """Extract plain text from a LangChain message (or str).

    Accepts `str`, or any object with `.content` that is a `str` or a list of content
    blocks (plain strings and `{"type": "text", "text": ...}` dicts). Non-text blocks
    (tool use, reasoning) are ignored. Raises `LLMResponseError` if no text results.
    """
    content = message if isinstance(message, str) else getattr(message, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                block_text = block.get("text")
                if isinstance(block_text, str):
                    parts.append(block_text)
        text = "".join(parts)
    else:
        raise LLMResponseError(f"unsupported reply type: {type(message).__name__}")
    if not text.strip():
        raise LLMResponseError("provider reply contained no text")
    return text


class ChatTextClient:
    """Adapts a LangChain chat model (already bound to `max_tokens`) to `LLMClient`."""

    def __init__(self, chat: _AsyncChat, *, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self._chat = chat
        self._max_tokens = max_tokens

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        if max_tokens > self._max_tokens:
            raise ValueError(
                f"requested max_tokens={max_tokens} exceeds the client cap {self._max_tokens}"
            )
        return message_text(await self._chat.ainvoke(prompt))


def _bedrock_chat(model_id: str, max_tokens: int, settings: CognitiveSettings) -> ChatTextClient:
    from langchain_aws import ChatBedrockConverse

    # No credentials are passed: boto3's default chain resolves the IAM role.
    chat = ChatBedrockConverse(
        model_id=model_id,
        max_tokens=max_tokens,
        region_name=settings.bedrock_region,
        endpoint_url=settings.bedrock_endpoint_url,
    )
    return ChatTextClient(chat, max_tokens=max_tokens)


def get_blue_client(settings: CognitiveSettings) -> LLMClient:
    return _bedrock_chat(settings.blue_model, settings.blue_red_max_tokens, settings)


def get_red_client(settings: CognitiveSettings) -> LLMClient:
    return _bedrock_chat(settings.red_model, settings.blue_red_max_tokens, settings)


def get_judge_client(settings: CognitiveSettings) -> LLMClient:
    return _bedrock_chat(settings.judge_model, settings.judge_max_tokens, settings)


def get_compression_client(settings: CognitiveSettings) -> LLMClient:
    return _bedrock_chat(settings.compression_model, settings.compression_max_tokens, settings)


def get_reflector_client(settings: CognitiveSettings) -> LLMClient:
    return _bedrock_chat(settings.reflector_model, settings.reflector_max_tokens, settings)
