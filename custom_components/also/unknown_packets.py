"""Redacted summaries of stream packets and fields that pyalso does not know.

The point is to learn what the cloud sends beyond what is decoded today (new packet types, extra
fields) without leaking private data. Payload bytes are therefore re-encoded with every value that
could identify the owner or the bike masked:

* text (printable ASCII) is replaced by ``x`` of the same length (serial numbers, names);
* varints of 100 000 000 or more, and every fixed32 and fixed64 value, are zeroed (coordinates are
  sint64 nanodegrees on the wire, but a 32-bit float can carry one too, so fixed32 is masked as
  well; timestamps and ids are large);
* other length-delimited fields that parse as messages, and the bodies of group fields, are
  redacted recursively; the rest is zeroed.

The field NUMBERS, wire types and small values are kept on purpose: that is what makes the summary
useful for learning the protocol. Floats are therefore not recoverable from a summary.

A payload that cannot be parsed keeps only feature, command, status and length. The summaries are
only ever shown in diagnostics; the log gets feature, command, status and length.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from pyalso import (
    WIRE_FIXED32,
    WIRE_FIXED64,
    WIRE_LEN,
    WIRE_START_GROUP,
    WIRE_VARINT,
    StreamEvent,
    UnknownPacket,
    WireError,
    WireField,
    parse_fields,
)

MAX_VARINT_KEPT = 100_000_000
MAX_HEX_BYTES = 512
_MAX_DEPTH = 4


def _printable(data: bytes) -> bool:
    return bool(data) and all(0x20 <= byte <= 0x7E for byte in data)


def _redact_field(item: WireField, depth: int) -> WireField:
    value = item.value
    if item.wire_type == WIRE_VARINT:
        return (
            item if isinstance(value, int) and value < MAX_VARINT_KEPT else replace(item, value=0)
        )
    if item.wire_type in (WIRE_FIXED64, WIRE_FIXED32):
        return replace(item, value=0)  # 64-bit and 32-bit (float) values may be coordinates
    if item.wire_type in (WIRE_LEN, WIRE_START_GROUP) and isinstance(value, bytes):
        if not value:
            return item
        if item.wire_type == WIRE_LEN and _printable(value):
            return replace(item, value=b"x" * len(value))
        if depth < _MAX_DEPTH:
            try:
                nested = parse_fields(value)
            except WireError:
                return replace(item, value=bytes(len(value)))
            return replace(item, value=_encode(_redact(nested, depth + 1)))
        return replace(item, value=bytes(len(value)))
    return item


def _redact(fields: Iterable[WireField], depth: int = 0) -> list[WireField]:
    return [_redact_field(item, depth) for item in fields]


def _encode(fields: Iterable[WireField]) -> bytes:
    return b"".join(item.encode() for item in fields)


def redact_payload_hex(payload: bytes) -> str | None:
    """Hex of ``payload`` with private values masked; ``None`` if it is not parseable/too big."""
    if len(payload) > MAX_HEX_BYTES:
        return None
    try:
        fields = parse_fields(payload)
    except WireError:
        return None
    return _encode(_redact(fields)).hex()


def _head(event: StreamEvent) -> dict[str, Any]:
    return {
        "name": event.name,
        "feature": f"{event.feature:#04x}",
        "command": f"{event.command:#06x}",
        "status": f"{event.status:#04x}",
        "length": len(event.payload),
        "timestamp": event.timestamp,
    }


def _unknown_fields_of(event: StreamEvent) -> list[WireField]:
    """Fields the decoders kept as unknown, from the event and its nested messages."""
    found: list[WireField] = list(getattr(event, "unknown_fields", ()) or ())
    for batt in getattr(event, "batteries", ()) or ():
        found.extend(batt.unknown_fields)
    location = getattr(event, "location", None)
    if location is not None:
        found.extend(location.unknown_fields)
    return found


def summarize_event(event: StreamEvent) -> dict[str, Any] | None:
    """A redacted, JSON-safe summary if ``event`` is unknown or carries unknown fields."""
    if isinstance(event, UnknownPacket):
        summary = _head(event)
        summary["kind"] = "unknown_packet"
        summary["reason"] = event.reason
        summary["payload_hex_redacted"] = redact_payload_hex(event.payload)
        return summary
    extra = _unknown_fields_of(event)
    if not extra:
        return None
    summary = _head(event)
    summary["kind"] = "unknown_fields"
    summary["fields_hex_redacted"] = _encode(_redact(extra)).hex()
    summary["field_numbers"] = sorted({item.number for item in extra})
    return summary
