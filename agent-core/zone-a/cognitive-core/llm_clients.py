"""LLM client factories for each debate node (ADR-003 Option 2: via the LLM gateway).

`CognitiveSettings.llm_route` picks the transport:

* `gateway` (default; what compose runs): `GatewayChatClient` POSTs to the internal LLM
  gateway's OpenAI-compatible `/v1/chat/completions` (LiteLLM proxy, config in
  `infrastructure/llm-gateway/config.yaml`). The request names a fixed role alias
  (`GATEWAY_MODEL_ALIASES`); the gateway maps it to a Bedrock model id and holds the AWS
  credentials. Zone A holds only the gateway key and has no provider credentials and no
  egress. The HTTP client ignores proxy environment variables, does not follow redirects and
  does not retry: each node already has a hard latency budget and degrades to abstain.
* `bedrock`: the previous direct `langchain_aws.ChatBedrockConverse` path (IAM role via the
  default boto3 chain, no API keys), kept for backward compatibility. Refused in production
  by `config.load_settings`.

No code path reads a provider API key. LangChain chat models return `AIMessage` objects
(whose `content` is a `str` OR a list of content blocks), never `str`. `ChatTextClient`
adapts them to the `LLMClient` protocol so nodes always receive a plain `str`. Provider SDK
imports are lazy, so importing this module (or faking `LLMClient` in tests) needs no
provider package and does no network I/O.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

import httpx
from pydantic import SecretStr

from .config import CognitiveSettings, ConfigError


class LLMClient(Protocol):
    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str: ...


class LLMResponseError(RuntimeError):
    """The provider reply contained no usable text."""


class LLMGatewayError(RuntimeError):
    """The LLM gateway refused the request or answered with a non-2xx status."""


# Role -> model alias defined in infrastructure/llm-gateway/config.yaml (kept in sync by a test).
GATEWAY_MODEL_ALIASES: Final[dict[str, str]] = {
    "blue": "afe-blue",
    "red": "afe-red",
    "judge": "afe-judge",
    "compression": "afe-compression",
    "reflector": "afe-reflector",
}
GATEWAY_CHAT_PATH: Final = "/v1/chat/completions"
_ERROR_TYPE_CHARS: Final = 64


class _AsyncChat(Protocol):
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any: ...


def message_text(message: object) -> str:
    """Extract plain text from a LangChain message (or str).

    Accepts `str`, a bare list of content blocks (OpenAI-style gateway replies), or any
    object with `.content` that is a `str` or a list of content blocks (plain strings and
    `{"type": "text", "text": ...}` dicts). Non-text blocks
    (tool use, reasoning) are ignored. Raises `LLMResponseError` if no text results.
    """
    if isinstance(message, (str, list)):
        content: object = message
    else:
        content = getattr(message, "content", None)
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


def _reply_text(payload: object) -> str:
    """Text of `choices[0].message.content` in an OpenAI-style chat completion."""
    if not isinstance(payload, dict):
        raise LLMResponseError("gateway reply is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMResponseError("gateway reply has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("gateway reply has no message")
    content = message.get("content")
    if not isinstance(content, (str, list)):
        raise LLMResponseError("gateway reply has no text content")
    return message_text(content)


def _error_type(response: httpx.Response) -> str:
    """Only the short `error.type` of a gateway error body: error messages can echo request
    details (including a masked key), so the body itself is never logged."""
    try:
        body = response.json()
    except ValueError:
        return "unknown"
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    return kind[:_ERROR_TYPE_CHARS] if isinstance(kind, str) and kind else "unknown"


class GatewayChatClient:
    """`LLMClient` over the LLM gateway's OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        max_tokens: int,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._model = model
        self._max_tokens = max_tokens
        self._api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            trust_env=False,  # never route the gateway key through an env-configured proxy
            follow_redirects=False,
        )

    @property
    def model(self) -> str:
        return self._model

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        if max_tokens > self._max_tokens:
            raise ValueError(
                f"requested max_tokens={max_tokens} exceeds the client cap {self._max_tokens}"
            )
        response = await self._http.post(
            GATEWAY_CHAT_PATH,
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
        )
        if response.status_code != httpx.codes.OK:
            raise LLMGatewayError(
                f"gateway returned HTTP {response.status_code} for model {self._model} "
                f"(error type: {_error_type(response)})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMResponseError("gateway reply is not valid JSON") from exc
        return _reply_text(payload)

    async def aclose(self) -> None:
        await self._http.aclose()


def _gateway_chat(
    role: str, max_tokens: int, timeout_s: float, settings: CognitiveSettings
) -> GatewayChatClient:
    if settings.llm_gateway_url is None or settings.llm_gateway_key is None:
        raise ConfigError(
            "COGNITIVE_LLM_ROUTE=gateway needs COGNITIVE_LLM_GATEWAY_URL and "
            "COGNITIVE_LLM_GATEWAY_KEY (ADR-003); refusing to start without them"
        )
    return GatewayChatClient(
        base_url=settings.llm_gateway_url,
        api_key=settings.llm_gateway_key,
        model=GATEWAY_MODEL_ALIASES[role],
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


def _client(
    role: str, model_id: str, max_tokens: int, timeout_s: float, settings: CognitiveSettings
) -> LLMClient:
    if settings.llm_route == "gateway":
        return _gateway_chat(role, max_tokens, timeout_s, settings)
    return _bedrock_chat(model_id, max_tokens, settings)


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
    return _client(
        "blue",
        settings.blue_model,
        settings.blue_red_max_tokens,
        settings.blue_latency_budget_s,
        settings,
    )


def get_red_client(settings: CognitiveSettings) -> LLMClient:
    return _client(
        "red",
        settings.red_model,
        settings.blue_red_max_tokens,
        settings.red_latency_budget_s,
        settings,
    )


def get_judge_client(settings: CognitiveSettings) -> LLMClient:
    return _client(
        "judge",
        settings.judge_model,
        settings.judge_max_tokens,
        settings.judge_latency_budget_s,
        settings,
    )


def get_compression_client(settings: CognitiveSettings) -> LLMClient:
    return _client(
        "compression",
        settings.compression_model,
        settings.compression_max_tokens,
        settings.compression_latency_budget_s,
        settings,
    )


def get_reflector_client(settings: CognitiveSettings) -> LLMClient:
    return _client(
        "reflector",
        settings.reflector_model,
        settings.reflector_max_tokens,
        settings.reflector_timeout_s,
        settings,
    )
