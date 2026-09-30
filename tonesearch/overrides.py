"""Per-request settings a visitor brings: their own AI provider and TONE3000 key.

Values arrive as request headers, live only for that request and are never stored or logged.
"""
from __future__ import annotations

from contextvars import ContextVar

HEADERS = {
    "provider": "X-AI-Provider",
    "account_id": "X-AI-Account-Id",
    "base_url": "X-AI-Base-Url",
    "model": "X-AI-Model",
    "api_key": "X-AI-Key",
    "tone3000_api_key": "X-TONE3000-Key",
    # AI tuning, honoured only with the visitor's own AI provider (see ai._visitor_config).
    "max_tokens": "X-AI-Max-Tokens",
    "temperature": "X-AI-Temperature",
    "timeout_seconds": "X-AI-Timeout-Seconds",
    "history_messages": "X-AI-History-Messages",
    "history_message_chars": "X-AI-History-Message-Chars",
    "research_chars": "X-AI-Research-Chars",
    "max_reply_chars": "X-AI-Max-Reply-Chars",
    "max_explanation_chars": "X-AI-Max-Explanation-Chars",
}
MAX_LENGTH = 500

_current: ContextVar[dict] = ContextVar("tonesearch_overrides", default={})


def from_headers(headers) -> dict:
    values = {}
    for name, header in HEADERS.items():
        value = (headers.get(header) or "").strip()
        if value:
            values[name] = value[:MAX_LENGTH]
    return values


def activate(values: dict) -> None:
    _current.set(dict(values))


def get(name: str) -> str:
    return _current.get().get(name, "")
