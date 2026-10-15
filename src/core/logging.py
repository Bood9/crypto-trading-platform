"""Structured logs with a blanket secrets filter.

Spec §15: no secrets in logs. The filter sits at the end of the processor
chain so that it fires on everything that reached the output, regardless
of which module put it there.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Keys whose values are cut out entirely.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "apikey",
        "apisecret",
        "bybit_api_key",
        "bybit_api_secret",
        "master_key",
        "password",
        "postgres_password",
        "secret",
        "token",
        "authorization",
        "x-bapi-sign",
        "sign",
        "signature",
        "private_key",
        "telegram_token",
        "bot_token",
    }
)

REDACTED = "***"

# A secret leaking inside a string: the whole string cannot be thrown away,
# so only the value is cut out. The groups are explicit — computing the
# replacement by parsing the match is too fragile to trust with the only
# barrier between a secret and the log.
_INLINE_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(api[_-]?(?:key|secret)|token|password|passwd|secret|sign(?:ature)?)"
            r"\b(\s*[=:]\s*)(\S+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})"),
        rf"\1{REDACTED}",
    ),
)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _INLINE_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


def _redact_mapping(data: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _SECRET_KEYS:
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: cuts secrets out of the whole event."""
    return _redact_mapping(event_dict)


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configures structlog. Called once at process start."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # httpx logs every request at INFO. During a backfill that is thousands
    # of lines in which our own events drown.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Last before rendering: sees everything that reached the output.
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
