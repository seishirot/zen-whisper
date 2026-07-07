"""JSON Lines protocol helpers for the native host/backend boundary."""

from __future__ import annotations

import json
from typing import Any


class ProtocolError(ValueError):
    """Raised when a protocol message cannot be decoded or validated."""


JsonDict = dict[str, Any]
MAX_LINE_BYTES = 1_048_576


def decode_line(line: bytes | str) -> JsonDict:
    if isinstance(line, bytes) and len(line) > MAX_LINE_BYTES:
        raise ProtocolError("Message is too large")
    if isinstance(line, str) and len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ProtocolError("Message is too large")
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object")
    if not isinstance(message.get("request_id"), str) or not message["request_id"]:
        raise ProtocolError("Message requires string request_id")
    if not isinstance(message.get("type"), str) or not message["type"]:
        raise ProtocolError("Message requires string type")
    return message


def encode_message(message: JsonDict) -> bytes:
    if not isinstance(message.get("request_id"), str) or not message["request_id"]:
        raise ProtocolError("Response requires string request_id")
    if not isinstance(message.get("type"), str) or not message["type"]:
        raise ProtocolError("Response requires string type")
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def error_response(
    request_id: str,
    code: str,
    message: str,
    *,
    recoverable: bool = True,
) -> JsonDict:
    return {
        "type": "error",
        "request_id": request_id,
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
