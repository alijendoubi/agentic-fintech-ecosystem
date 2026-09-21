"""Canonical, schema-driven encoding of protobuf messages and sha256 digests (encoding v1).

Not protobuf wire bytes: ``SerializeToString(deterministic=True)`` is not guaranteed stable across
versions/languages. Instead every message is walked via its descriptor into a JSON document:

* all declared fields are emitted (defaults included); unset sub-messages -> ``null``
* ``double``/``float`` -> the shortest round-trip decimal string (``repr``), ``-0.0`` -> ``"0.0"``;
  NaN/Infinity are rejected. (Strings, not JSON numbers, so no reader can re-round them.)
* 32/64-bit integers -> JSON integers; ``bool`` -> boolean; ``string`` -> string; ``bytes`` ->
lowercase hex
* enums -> the enum value NAME (unknown numbers are rejected)
* repeated -> arrays in order; map fields are not supported (rejected)
* JSON text: sorted keys, separators ``,`` ``:``, ``ensure_ascii`` (pure ASCII), no NaN, UTF-8
hashed.
The same JSON rules apply to the manifest envelope. digest = sha256(utf8(text)) lowercase hex.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from afe_manifest.errors import ManifestBuildError

_INT_TYPES = {
    FieldDescriptor.TYPE_INT32,
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_SFIXED64,
}


def float_text(value: float) -> str:
    if not math.isfinite(value):
        raise ManifestBuildError("non-finite floating point value in message")
    return "0.0" if value == 0 else repr(float(value))


def _scalar(field: FieldDescriptor, value: Any) -> Any:
    if field.type in (FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT):
        return float_text(value)
    if field.type == FieldDescriptor.TYPE_ENUM:
        if field.enum_type is None:
            raise ManifestBuildError(f"enum field {field.full_name} has no enum type")
        by_number = field.enum_type.values_by_number
        if value not in by_number:
            raise ManifestBuildError(f"unknown enum number {value} for {field.full_name}")
        return by_number[value].name
    if field.type == FieldDescriptor.TYPE_BYTES:
        return bytes(value).hex()
    if field.type in _INT_TYPES:
        return int(value)
    if field.type in (FieldDescriptor.TYPE_BOOL, FieldDescriptor.TYPE_STRING):
        return value
    raise ManifestBuildError(f"unsupported field type {field.type} for {field.full_name}")


def _value(field: FieldDescriptor, value: Any) -> Any:
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return message_to_canonical(value)
    return _scalar(field, value)


def _is_repeated(field: FieldDescriptor) -> bool:
    # `is_repeated` exists in current protobuf; `label` was removed from the upb descriptors.
    flag = getattr(field, "is_repeated", None)
    if flag is not None:
        return bool(flag)
    return bool(getattr(field, "label", None) == FieldDescriptor.LABEL_REPEATED)


def message_to_canonical(message: Any) -> dict[str, Any]:
    """Schema-driven dict form of a protobuf message (see module docstring)."""
    out: dict[str, Any] = {}
    for field in message.DESCRIPTOR.fields:
        if field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.GetOptions().map_entry:
            raise ManifestBuildError(f"map field {field.full_name} is not supported")
        raw = getattr(message, field.name)
        if _is_repeated(field):
            out[field.name] = [_value(field, item) for item in raw]
        elif field.type == FieldDescriptor.TYPE_MESSAGE and not message.HasField(field.name):
            out[field.name] = None
        else:
            out[field.name] = _value(field, raw)
    return out


def canonical_text(document: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ManifestBuildError(f"document is not canonically serialisable: {exc}") from exc


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(value: Any) -> str:
    """sha256 of the canonical JSON of any JSON-compatible value (dict/list/None/...)."""
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return digest_text(text)
