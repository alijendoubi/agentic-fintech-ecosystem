"""Contract tests: LangChain returns messages (not str); the adapter must always yield str."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cognitive_core import llm_clients
from cognitive_core.llm_clients import ChatTextClient, LLMResponseError, message_text
from cognitive_core.models import BlueThesis
from cognitive_core.parsing import parse_json_model
from cognitive_core.tests.fakes import BLUE_JSON, make_settings


class _FakeChat:
    def __init__(self, reply: object) -> None:
        self._reply = reply
        self.inputs: list[Any] = []

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.inputs.append(input)
        return self._reply


def test_message_text_plain_string_content() -> None:
    assert message_text(AIMessage(content="hello")) == "hello"


def test_message_text_list_of_text_blocks_and_strings() -> None:
    msg = AIMessage(
        content=[
            {"type": "text", "text": '{"a":'},
            "1}",
            {"type": "tool_use", "id": "x", "name": "n", "input": {}},
            {"type": "reasoning", "reasoning_content": "hidden"},
        ]
    )
    assert message_text(msg) == '{"a":1}'


def test_message_text_accepts_bare_str() -> None:
    assert message_text("raw") == "raw"


@pytest.mark.parametrize(
    "bad",
    [
        AIMessage(content=""),
        AIMessage(content="   "),
        AIMessage(content=[{"type": "tool_use", "id": "x", "name": "n", "input": {}}]),
        AIMessage(content=[]),
        None,
        42,
    ],
)
def test_message_text_rejects_unusable_replies(bad: object) -> None:
    with pytest.raises(LLMResponseError):
        message_text(bad)


@pytest.mark.asyncio
async def test_adapter_returns_str_for_aimessage_and_feeds_the_parser() -> None:
    client = ChatTextClient(_FakeChat(AIMessage(content=BLUE_JSON)), max_tokens=100)
    raw = await client.ainvoke("prompt", max_tokens=50)
    assert isinstance(raw, str)
    assert parse_json_model(raw, BlueThesis).side.value == "BUY"


@pytest.mark.asyncio
async def test_adapter_handles_content_block_replies() -> None:
    blocks = AIMessage(content=[{"type": "text", "text": BLUE_JSON}])
    raw = await ChatTextClient(_FakeChat(blocks), max_tokens=100).ainvoke("p", max_tokens=1)
    assert parse_json_model(raw, BlueThesis).rationale == "strong momentum"


@pytest.mark.asyncio
async def test_adapter_rejects_non_message_reply() -> None:
    with pytest.raises(LLMResponseError):
        await ChatTextClient(_FakeChat(HumanMessage(content=[])), max_tokens=10).ainvoke(
            "p", max_tokens=10
        )


@pytest.mark.asyncio
async def test_adapter_refuses_max_tokens_above_cap() -> None:
    chat = _FakeChat(AIMessage(content="x"))
    with pytest.raises(ValueError, match="exceeds"):
        await ChatTextClient(chat, max_tokens=10).ainvoke("p", max_tokens=11)
    assert chat.inputs == []  # nothing was sent


def test_adapter_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        ChatTextClient(_FakeChat(""), max_tokens=0)


class _CapturingBedrock:
    instances: list[_CapturingBedrock] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _CapturingBedrock.instances.append(self)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return AIMessage(content="ok")


@pytest.fixture
def fake_bedrock(monkeypatch: pytest.MonkeyPatch) -> type[_CapturingBedrock]:
    _CapturingBedrock.instances = []
    module = types.ModuleType("langchain_aws")
    module.ChatBedrockConverse = _CapturingBedrock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_aws", module)
    return _CapturingBedrock


@pytest.mark.parametrize(
    ("factory", "model_field", "token_field"),
    [
        (llm_clients.get_blue_client, "blue_model", "blue_red_max_tokens"),
        (llm_clients.get_red_client, "red_model", "blue_red_max_tokens"),
        (llm_clients.get_judge_client, "judge_model", "judge_max_tokens"),
        (llm_clients.get_compression_client, "compression_model", "compression_max_tokens"),
        (llm_clients.get_reflector_client, "reflector_model", "reflector_max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_factories_use_bedrock_with_settings_and_no_api_keys(
    fake_bedrock: type[_CapturingBedrock], factory: Any, model_field: str, token_field: str
) -> None:
    settings = make_settings(
        COGNITIVE_BEDROCK_REGION="eu-west-1",
        COGNITIVE_BEDROCK_ENDPOINT_URL="https://vpce.example",
    )
    client = factory(settings)
    kwargs = fake_bedrock.instances[-1].kwargs
    assert kwargs["model"] == getattr(settings, model_field)
    assert kwargs["max_tokens"] == getattr(settings, token_field)
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["endpoint_url"] == "https://vpce.example"
    assert not any("key" in k.lower() or "secret" in k.lower() for k in kwargs)
    assert await client.ainvoke("p", max_tokens=1) == "ok"


def test_no_direct_provider_sdk_is_referenced() -> None:
    source = open(llm_clients.__file__, encoding="utf-8").read()
    for forbidden in ("langchain_anthropic", "langchain_mistralai", "ChatAnthropic", "ChatMistralAI"):
        assert forbidden not in source
