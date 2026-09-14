"""Structured (JSON) logging with correlation IDs.

Every log record carries ``timestamp``, ``component``, ``event``, ``correlation_id``,
``severity`` and optional ``symbol`` / ``order_id`` fields. Secrets are redacted by
:class:`SecretRedactingFilter` so credentials never reach the logs.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_SECRET_PATTERNS = [
    re.compile(r"(APCA-API-SECRET-KEY\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(APCA-API-KEY-ID\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(secret[_-]?key\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(api[_-]?key\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(password\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(token\s*[:=]\s*)(\S+)", re.I),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1***", text)
    return text


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:16]
    _correlation_id.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str:
    return _correlation_id.get()


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            try:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            except Exception:  # pragma: no cover
                pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "component": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None) or get_correlation_id(),
        }
        for key in ("symbol", "order_id", "client_order_id", "strategy", "latency_ms"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = _jsonable(extra)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.now(UTC).strftime('%H:%M:%S')} {record.levelname:<7} "
            f"[{record.name}] cid={getattr(record, 'correlation_id', None) or get_correlation_id()} "
            f"{record.getMessage()}"
        )
        for key in ("symbol", "order_id", "client_order_id"):
            val = getattr(record, key, None)
            if val is not None:
                base += f" {key}={val}"
        data = getattr(record, "data", None)
        if data:
            base += " " + json.dumps(_jsonable(data), default=str)
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, int | float | bool) or obj is None:
        return obj
    return str(obj)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class EventLogger:
    """Thin wrapper producing structured events for a component."""

    def __init__(self, component: str) -> None:
        self._log = logging.getLogger(component)

    def _emit(self, level: int, event: str, message: str | None = None, **fields: Any) -> None:
        data = fields.pop("data", None)
        extra: dict[str, Any] = {"event": event, "correlation_id": get_correlation_id()}
        for key in ("symbol", "order_id", "client_order_id", "strategy", "latency_ms"):
            if key in fields:
                extra[key] = fields.pop(key)
        if fields:
            data = {**(data or {}), **fields}
        if data:
            extra["data"] = data
        self._log.log(level, message or event, extra=extra)

    def debug(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, message, **fields)

    def info(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._emit(logging.INFO, event, message, **fields)

    def warning(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._emit(logging.WARNING, event, message, **fields)

    def error(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._emit(logging.ERROR, event, message, **fields)

    def exception(self, event: str, message: str | None = None, **fields: Any) -> None:
        data = fields.pop("data", None)
        extra: dict[str, Any] = {"event": event, "correlation_id": get_correlation_id()}
        if fields:
            data = {**(data or {}), **fields}
        if data:
            extra["data"] = data
        self._log.exception(message or event, extra=extra)
