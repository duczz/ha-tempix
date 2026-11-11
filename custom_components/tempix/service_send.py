"""Neutral service-call primitive shared by V1 and the V2 dispatcher.

``safe_service_call`` used to live in ``coordinator_appliers``. The V2
cutover needs it in the HardwareDispatcher too, so it moved to
this decision-free module — both sides import it here, and the V1 appliers
can be removed later without taking the send primitive with them.
``coordinator_appliers`` re-exports the name for backwards compatibility.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

_LOGGER = logging.getLogger(__name__)


async def safe_service_call(
    hass: HomeAssistant,
    name: str,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    *,
    timeout: int = 30,
    return_response: bool = False,
    max_retries: int = 2,
    raise_on_failure: bool = False,
    ha_error_log_level: int = logging.WARNING,
    context: Context | None = None,
) -> Any:
    """Wrap hass.services.async_call with timeout and exponential-backoff retry.

    Retries up to *max_retries* times (2 s, 4 s back-off).
    Essential for FRITZ!DECT thermostats where the DECT radio bridge
    can temporarily be unable to reach devices.

    If *raise_on_failure* is True, raises ``RuntimeError`` after all retries
    are exhausted so callers (e.g. circuit breaker) can detect persistent failures.

    *ha_error_log_level* controls the log level for HomeAssistantError/ServiceNotFound
    failures (default WARNING). Pass ``logging.DEBUG`` for expected transient failures
    (e.g. calendar sync not yet complete on startup).
    """
    entity_hint = service_data.get("entity_id", "?")
    total_attempts = max_retries + 1
    last_exc: Exception | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            async with asyncio.timeout(timeout):
                return await hass.services.async_call(
                    domain, service, service_data,
                    blocking=True,
                    return_response=return_response,
                    # nur wenn gesetzt: der Sendepfad der Geraete bleibt Byte-fuer-Byte gleich
                    **({"context": context} if context is not None else {}),
                )
        except TimeoutError as exc:
            last_exc = exc
            if attempt < total_attempts:
                backoff = 2 ** attempt  # 2 s, 4 s
                _LOGGER.warning(
                    "%s: %s.%s timed out (attempt %d/%d) for %s – retry in %ds",
                    name, domain, service, attempt, total_attempts, entity_hint, backoff,
                )
                await asyncio.sleep(backoff)
            else:
                _LOGGER.error(
                    "%s: %s.%s timed out after %d attempts for %s – giving up",
                    name, domain, service, total_attempts, entity_hint,
                )
        except (HomeAssistantError, ServiceNotFound) as exc:
            last_exc = exc
            _LOGGER.log(
                ha_error_log_level,
                "%s: %s.%s failed for %s: %s",
                name, domain, service, entity_hint, exc,
            )
            break  # no retry for HA errors

    if raise_on_failure and last_exc is not None:
        raise RuntimeError(f"{domain}.{service} failed for {entity_hint}") from last_exc
    return None
