"""Notification abstraction. Notifiers must never raise into the trading loop and never
carry secrets (all payloads pass through the log redactor)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from trading_agent.config import NotifierKind, Settings
from trading_agent.logging_config import EventLogger, redact

log = EventLogger("notifications")


class Notifier(ABC):
    def notify(
        self, event_type: str, title: str, message: str, severity: str = "INFO", data: dict[str, Any] | None = None
    ) -> None:
        safe_data = {k: redact(str(v)) for k, v in (data or {}).items()}
        try:
            self._send(event_type, redact(title), redact(message), severity, safe_data)
        except Exception as exc:  # noqa: BLE001 - notifications are best-effort
            log.warning("notification_failed", error=exc.__class__.__name__, event_type=event_type)

    @abstractmethod
    def _send(self, event_type: str, title: str, message: str, severity: str, data: dict[str, str]) -> None: ...


class NullNotifier(Notifier):
    def _send(self, *_args) -> None:
        return None


class LogNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def _send(self, event_type: str, title: str, message: str, severity: str, data: dict[str, str]) -> None:
        self.sent.append({"event_type": event_type, "title": title, "message": message, "severity": severity, "data": data})
        level = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}.get(
            severity, logging.INFO
        )
        log._emit(level, f"notify.{event_type}", f"{title}: {message}", data=data)


class WebhookNotifier(Notifier):
    """Generic JSON webhook (Slack/Discord/Teams-compatible ``text`` field plus structured fields)."""

    def __init__(self, url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> None:
        if not url.startswith("https://"):
            raise ValueError("webhook URL must use https")
        self._url = url
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _send(self, event_type: str, title: str, message: str, severity: str, data: dict[str, str]) -> None:
        payload = {"text": f"[{severity}] {title}\n{message}", "event_type": event_type, "severity": severity, "data": data}
        resp = self._client.post(self._url, json=payload)
        resp.raise_for_status()


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def _send(self, event_type: str, title: str, message: str, severity: str, data: dict[str, str]) -> None:
        for n in self.notifiers:
            n.notify(event_type, title, message, severity, data)


def build_notifier(settings: Settings) -> Notifier:
    if settings.notifier == NotifierKind.NONE:
        return NullNotifier()
    if settings.notifier == NotifierKind.WEBHOOK:
        return MultiNotifier([LogNotifier(), WebhookNotifier(settings.notification_webhook_url)])
    return LogNotifier()
