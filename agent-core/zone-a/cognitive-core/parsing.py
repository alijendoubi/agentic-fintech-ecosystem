"""Strict parsing of model replies into pydantic models (fail closed).

The only leniency is stripping one surrounding Markdown code fence. Anything else
that does not validate against the model raises `ModelOutputError`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ValidationError

MAX_RAW_CHARS = 100_000
_FENCE = re.compile(r"\A```(?:json|JSON)?[ \t]*\r?\n(?P<body>.*?)\r?\n?```\Z", re.DOTALL)


class ModelOutputError(ValueError):
    """The model reply was not valid JSON for the expected schema."""


def strip_json_fence(raw: str) -> str:
    """Remove a single wrapping ```json fence if (and only if) it wraps the whole reply."""
    text = raw.strip()
    match = _FENCE.match(text)
    return match.group("body").strip() if match else text


def parse_json_model[ModelT: BaseModel](raw: str, model: type[ModelT]) -> ModelT:
    """Validate `raw` as JSON for `model`. Raises `ModelOutputError` on any deviation."""
    if not isinstance(raw, str):
        raise ModelOutputError(f"expected str reply, got {type(raw).__name__}")
    if len(raw) > MAX_RAW_CHARS:
        raise ModelOutputError("model reply exceeds the maximum accepted size")
    try:
        return model.model_validate_json(strip_json_fence(raw))
    except ValidationError as exc:
        raise ModelOutputError(
            f"{model.__name__} validation failed: {exc.error_count()} error(s)"
        ) from exc
