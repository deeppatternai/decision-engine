"""Shared bounded parsing for explicit-audit prompt hooks."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, BinaryIO


_MAX_INPUT_BYTES = 64 * 1024
_AUDIT_PREFIX = re.compile(
    r"^(?:(?:please|kindly)\s+)?(?:(?:deeply|thoroughly)\s+)?audit\b"
    r"|^(?:请)?(?:深度|深入)?(?:审计|审核)",
    re.IGNORECASE,
)


def read_payload(stream: BinaryIO) -> dict[str, Any] | None:
    raw = stream.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def is_explicit_audit_prompt(prompt: object) -> bool:
    if not isinstance(prompt, str):
        return False
    normalized = unicodedata.normalize("NFKC", prompt).strip()
    return bool(_AUDIT_PREFIX.match(normalized))
