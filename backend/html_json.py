"""Helpers for safely embedding JSON inside inline HTML script blocks."""

from __future__ import annotations

import json
from typing import Any


def escape_script_json(data_json: str) -> str:
    """Escape characters that can break out of an inline <script> block."""
    return (
        data_json
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def dump_script_safe_json(value: Any) -> str:
    """Serialize a value as JSON that is safe to embed in inline scripts."""
    return escape_script_json(json.dumps(value, ensure_ascii=False))
