"""Diagnostics for pypowerwall: what the gateway or Tesla's cloud actually returns."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import PypowerwallConfigEntry
from .const import CONF_GW_PWD, CONF_SITEID

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_GW_PWD,
    CONF_SITEID,
    "din",
    "id",
    "gateway_id",
    "energy_site_id",
    "site_name",
    "PackageSerialNumber",
    "serial_number",
    "access_token",
    "refresh_token",
    "id_token",
}


def _raw_payloads(pw: Any) -> dict[str, Any]:
    """Unprocessed responses behind the battery capacity sensors. Blocking."""
    payloads: dict[str, Any] = {}
    try:
        payloads["system_status"] = pw.system_status()
    except Exception as err:  # noqa: BLE001 - diagnostics must not fail
        payloads["system_status"] = f"error: {err!r}"
    client = getattr(pw, "client", None)
    for name in ("get_battery", "get_site_power"):
        method = getattr(client, name, None)
        if method is None:
            continue
        try:
            payloads[f"cloud_{name}"] = method(force=True)
        except Exception as err:  # noqa: BLE001 - diagnostics must not fail
            payloads[f"cloud_{name}"] = f"error: {err!r}"
    return payloads


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PypowerwallConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    raw = await hass.async_add_executor_job(_raw_payloads, coordinator.pw)
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "coordinator_data": dataclasses.asdict(coordinator.data)
            if coordinator.data
            else None,
            "raw": raw,
        },
        TO_REDACT,
    )
