"""ADR-003 Option 2: cognitive-core reaches LLMs only through the key-holding gateway.

No real network: the client is exercised against `httpx.MockTransport`, plus one loopback
HTTP server to prove the real socket path (and that proxy environment variables are ignored).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from cognitive_core import __main__ as entrypoint
from cognitive_core import llm_clients
from cognitive_core.config import ConfigError, load_settings
from cognitive_core.llm_clients import (
    GATEWAY_MODEL_ALIASES,
    GatewayChatClient,
    LLMGatewayError,
    LLMResponseError,
)
from cognitive_core.tests.fakes import BLUE_JSON, make_settings
from pydantic import SecretStr

# Test-only key: random-looking, long enough, not a placeholder fragment.
KEY = "sk-afe-test-9f3c1b7e2d8a4c6f0b5e9d1a7c3f8e2b"
URL = "http://llm-gateway:4000"
GATEWAY_ENV = {"COGNITIVE_LLM_GATEWAY_URL": URL, "COGNITIVE_LLM_GATEWAY_KEY": KEY}
GATEWAY_CONFIG = Path(__file__).resolve().parents[3] / "infrastructure/llm-gateway/config.yaml"


def _completion(content: object) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


class _Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def _client(recorder: _Recorder, *, max_tokens: int = 100) -> GatewayChatClient:
    return GatewayChatClient(
        base_url=URL + "/",
        api_key=SecretStr(KEY),
        model="afe-blue",
        max_tokens=max_tokens,
        timeout_s=1.0,
        transport=httpx.MockTransport(recorder),
    )


# -- settings -----------------------------------------------------------------------------


def test_gateway_is_the_default_route_and_the_key_is_secret() -> None:
    s = load_settings(GATEWAY_ENV)
    assert s.llm_route == "gateway"
    assert s.llm_gateway_url == URL
    assert s.llm_gateway_key is not None and s.llm_gateway_key.get_secret_value() == KEY
    assert KEY not in repr(s) and KEY not in str(s.model_dump())


def test_route_is_parsed_and_unknown_routes_are_refused() -> None:
    assert load_settings({"COGNITIVE_LLM_ROUTE": " Bedrock "}).llm_route == "bedrock"
    with pytest.raises(ConfigError, match="COGNITIVE_LLM_ROUTE"):
        load_settings({"COGNITIVE_LLM_ROUTE": "direct"})


@pytest.mark.parametrize(
    "url",
    [
        "ftp://llm-gateway:4000",
        "llm-gateway:4000",
        "http://",
        "http://user:pw@llm-gateway:4000",
        "http://llm-gateway:4000/v1",
        "http://llm-gateway:4000/?x=1",
    ],
)
def test_bad_gateway_urls_are_refused(url: str) -> None:
    with pytest.raises(ConfigError, match="llm_gateway_url"):
        load_settings({"COGNITIVE_LLM_GATEWAY_URL": url})


@pytest.mark.parametrize(
    "key",
    [
        "sk-short",
        "sk-change-me-0000000000000000000000000000",
        "PLACEHOLDER-9f3c1b7e2d8a4c6f0b5e9d1a7c3f8e2b",
        "sk-example-9f3c1b7e2d8a4c6f0b5e9d1a7c3f8e2b",
    ],
)
def test_short_or_placeholder_keys_are_refused_without_echoing_them(key: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({"COGNITIVE_LLM_GATEWAY_KEY": key})
    assert "llm_gateway_key" in str(info.value)
    assert key not in str(info.value)


def test_production_requires_the_gateway_route_over_https() -> None:
    prod = {"ENVIRONMENT": "production"}
    with pytest.raises(ConfigError, match="COGNITIVE_LLM_ROUTE=gateway"):
        load_settings({**prod, **GATEWAY_ENV, "COGNITIVE_LLM_ROUTE": "bedrock"})
    with pytest.raises(ConfigError, match="COGNITIVE_LLM_GATEWAY_URL and"):
        load_settings(prod)
    with pytest.raises(ConfigError, match="https"):
        load_settings({**prod, **GATEWAY_ENV})
    ok = load_settings({**prod, **GATEWAY_ENV, "COGNITIVE_LLM_GATEWAY_URL": "https://gw:4000"})
    assert ok.llm_route == "gateway"


# -- factories ----------------------------------------------------------------------------

_FACTORIES = [
    (llm_clients.get_blue_client, "blue", "blue_red_max_tokens", "blue_latency_budget_s"),
    (llm_clients.get_red_client, "red", "blue_red_max_tokens", "red_latency_budget_s"),
    (llm_clients.get_judge_client, "judge", "judge_max_tokens", "judge_latency_budget_s"),
    (
        llm_clients.get_compression_client,
        "compression",
        "compression_max_tokens",
        "compression_latency_budget_s",
    ),
    (llm_clients.get_reflector_client, "reflector", "reflector_max_tokens", "reflector_timeout_s"),
]


@pytest.mark.parametrize(("factory", "role", "token_field", "timeout_field"), _FACTORIES)
def test_gateway_factories_use_role_aliases_caps_and_budgets(
    factory: Any, role: str, token_field: str, timeout_field: str
) -> None:
    settings = make_settings(**GATEWAY_ENV)
    client = factory(settings)
    assert isinstance(client, GatewayChatClient)
    assert client.model == GATEWAY_MODEL_ALIASES[role]
    assert client._max_tokens == getattr(settings, token_field)
    http = client._http
    assert str(http.base_url).rstrip("/") == URL
    assert http.timeout.read == getattr(settings, timeout_field)
    assert http.follow_redirects is False
    assert http._trust_env is False


@pytest.mark.parametrize(
    "env", [{}, {"COGNITIVE_LLM_GATEWAY_URL": URL}, {"COGNITIVE_LLM_GATEWAY_KEY": KEY}]
)
@pytest.mark.parametrize(("factory", "role", "token_field", "timeout_field"), _FACTORIES)
def test_gateway_factories_fail_closed_without_url_and_key(
    env: dict[str, str], factory: Any, role: str, token_field: str, timeout_field: str
) -> None:
    with pytest.raises(ConfigError, match="COGNITIVE_LLM_GATEWAY_URL"):
        factory(make_settings(**env))


@pytest.mark.asyncio
async def test_runner_refuses_to_start_on_the_gateway_route_without_a_key() -> None:
    env = {"COGNITIVE_SINK": "log", "COGNITIVE_HEALTH_PORT": "0", "COGNITIVE_LLM_GATEWAY_URL": URL}
    assert await entrypoint.amain(env) == entrypoint.EXIT_CONFIG


# -- client -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_sends_an_openai_style_request_with_the_bearer_key() -> None:
    recorder = _Recorder(httpx.Response(200, json=_completion(BLUE_JSON)))
    assert await _client(recorder).ainvoke("the prompt", max_tokens=42) == BLUE_JSON
    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == URL + "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "afe-blue",
        "messages": [{"role": "user", "content": "the prompt"}],
        "max_tokens": 42,
    }


@pytest.mark.asyncio
async def test_client_accepts_content_block_lists() -> None:
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    recorder = _Recorder(httpx.Response(200, json=_completion(blocks)))
    assert await _client(recorder).ainvoke("p", max_tokens=1) == "ab"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _completion(""),
        _completion(None),
        {"choices": []},
        {"choices": [{"message": "x"}]},
        [],
    ],
)
async def test_client_rejects_unusable_replies(body: object) -> None:
    recorder = _Recorder(httpx.Response(200, json=body))
    with pytest.raises(LLMResponseError):
        await _client(recorder).ainvoke("p", max_tokens=1)


@pytest.mark.asyncio
async def test_client_rejects_non_json_replies() -> None:
    recorder = _Recorder(httpx.Response(200, text="<html>"))
    with pytest.raises(LLMResponseError, match="JSON"):
        await _client(recorder).ainvoke("p", max_tokens=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 400, 429, 500, 302])
async def test_client_raises_on_gateway_errors_without_logging_the_body(status: int) -> None:
    body = {"error": {"message": f"Received API Key = {KEY}", "type": "auth_error"}}
    headers = {"location": "http://elsewhere.invalid/"} if status == 302 else None
    recorder = _Recorder(httpx.Response(status, json=body, headers=headers))
    with pytest.raises(LLMGatewayError) as info:
        await _client(recorder).ainvoke("p", max_tokens=1)
    message = str(info.value)
    assert f"HTTP {status}" in message and "auth_error" in message
    assert KEY not in message
    assert len(recorder.requests) == 1  # no retry, no redirect followed


@pytest.mark.asyncio
async def test_client_refuses_max_tokens_above_its_cap_before_sending() -> None:
    recorder = _Recorder(httpx.Response(200, json=_completion("x")))
    with pytest.raises(ValueError, match="exceeds"):
        await _client(recorder, max_tokens=10).ainvoke("p", max_tokens=11)
    assert recorder.requests == []


def test_client_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError):
        GatewayChatClient(
            base_url=URL, api_key=SecretStr(KEY), model="m", max_tokens=0, timeout_s=1.0
        )
    with pytest.raises(ValueError):
        GatewayChatClient(
            base_url=URL, api_key=SecretStr(KEY), model="m", max_tokens=1, timeout_s=0
        )


# -- real loopback socket -----------------------------------------------------------------


class _FakeGateway(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None, dict[str, Any]]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        _FakeGateway.seen.append((self.path, self.headers.get("authorization"), body))
        if self.headers.get("authorization") != f"Bearer {KEY}":
            payload, status = {"error": {"type": "auth_error"}}, 401
        else:
            payload, status = _completion("pong"), 200
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence stderr
        return


@pytest.fixture
def fake_gateway() -> Iterator[str]:
    _FakeGateway.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_factory_client_talks_to_a_loopback_gateway_and_ignores_proxy_env(
    fake_gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the client honoured these, the request would go to a dead proxy and fail.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    settings = make_settings(COGNITIVE_LLM_GATEWAY_URL=fake_gateway, COGNITIVE_LLM_GATEWAY_KEY=KEY)
    client = llm_clients.get_judge_client(settings)
    assert await client.ainvoke("ping", max_tokens=5) == "pong"
    path, auth, body = _FakeGateway.seen[-1]
    assert path == "/v1/chat/completions" and auth == f"Bearer {KEY}"
    assert body["model"] == "afe-judge" and body["max_tokens"] == 5


@pytest.mark.asyncio
async def test_loopback_gateway_rejects_a_wrong_key(fake_gateway: str) -> None:
    client = GatewayChatClient(
        base_url=fake_gateway,
        api_key=SecretStr("x" * 40),
        model="afe-blue",
        max_tokens=5,
        timeout_s=2.0,
    )
    with pytest.raises(LLMGatewayError, match="HTTP 401"):
        await client.ainvoke("ping", max_tokens=5)
    await client.aclose()


# -- gateway config.yaml stays in sync with this package ------------------------------------


def test_gateway_config_matches_aliases_and_default_model_ids() -> None:
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load(GATEWAY_CONFIG.read_text(encoding="utf-8"))
    by_alias = {entry["model_name"]: entry["litellm_params"] for entry in cfg["model_list"]}
    assert set(by_alias) == set(GATEWAY_MODEL_ALIASES.values())
    defaults = load_settings({})
    expected = {
        "blue": defaults.blue_model,
        "red": defaults.red_model,
        "judge": defaults.judge_model,
        "compression": defaults.compression_model,
        "reflector": defaults.reflector_model,
    }
    for role, alias in GATEWAY_MODEL_ALIASES.items():
        params = by_alias[alias]
        assert params["model"] == f"bedrock/converse/{expected[role]}"
        # Owner-set values only: every limit and the region come from the gateway's env.
        for knob in ("aws_region_name", "rpm", "tpm"):
            assert str(params[knob]).startswith("os.environ/"), (alias, knob)
        assert not any("key" in k.lower() or "secret" in k.lower() for k in params)
    assert cfg["general_settings"]["master_key"] == "os.environ/LLM_GATEWAY_MASTER_KEY"
    assert cfg["router_settings"]["num_retries"] == 0
