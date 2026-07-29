from __future__ import annotations

from typing import Awaitable, Callable, Optional

import structlog

from src.core.config import AlertsConfig

SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"

WEBHOOK_TIMEOUT_SECONDS = 10.0

Transport = Callable[[str, dict], Awaitable[bool]]


async def _post_httpx(url: str, payload: dict) -> bool:
    import httpx

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload)
        return response.status_code < 300


class AlertService:

    def __init__(
        self,
        config: AlertsConfig,
        transport: Optional[Transport] = None,
    ) -> None:
        self._config = config
        self._transport = transport or _post_httpx
        self._logger = structlog.get_logger("AlertService")

    async def send(self, severity: str, title: str, message: str) -> bool:
        log = {
            SEVERITY_INFO: self._logger.info,
            SEVERITY_WARNING: self._logger.warning,
            SEVERITY_CRITICAL: self._logger.error,
        }.get(severity, self._logger.warning)
        log("alert", title=title, message=message)

        if not self._config.enabled or not self._config.webhook_url:
            return False

        text = f"[{severity}] Auriferous — {title}\n{message}"
        try:
            return await self._transport(
                self._config.webhook_url,
                {"content": text, "text": text},
            )
        except Exception as e:
            self._logger.error("alert_delivery_failed", title=title, error=str(e))
            return False
