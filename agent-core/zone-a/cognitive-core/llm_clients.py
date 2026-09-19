"""LLM client factories for each debate node.

Per README ("Zone A holds zero API keys") and ADR-002, all models are reached
through the AWS Bedrock VPC endpoint or Mistral's EU endpoint — no raw keys
live in this module. Imports of the heavy provider SDKs are deferred into the
factory functions so this module (and anything that only needs `LLMClient`
for typing/mocking, e.g. tests) can be imported without those packages
installed.
"""

from __future__ import annotations

from typing import Protocol

import config


class LLMClient(Protocol):
    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str: ...


def get_blue_client() -> LLMClient:
    from langchain_mistralai import ChatMistralAI

    return ChatMistralAI(model=config.BLUE_MODEL, max_tokens=config.BLUE_RED_MAX_TOKENS)


def get_red_client() -> LLMClient:
    from langchain_mistralai import ChatMistralAI

    return ChatMistralAI(model=config.RED_MODEL, max_tokens=config.BLUE_RED_MAX_TOKENS)


def get_judge_client() -> LLMClient:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=config.JUDGE_MODEL, max_tokens=config.JUDGE_MAX_TOKENS)


def get_compression_client() -> LLMClient:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=config.COMPRESSION_MODEL, max_tokens=config.COMPRESSION_MAX_TOKENS)


def get_reflector_client() -> LLMClient:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=config.REFLECTOR_MODEL, max_tokens=config.COMPRESSION_MAX_TOKENS)
