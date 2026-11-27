"""Keep the site's Tesla tariff in step with Octopus rates and export windows.

Enabled by choosing the Octopus Energy integration's import and export
day-rate event entities in the integration options. From then on Home
Assistant owns the tariff Tesla holds: it is rebuilt from those rates plus any
export windows (tariff_builder.py) and written when the rates change, when a
window is added or cleared, and once a day, which also drops windows whose
date has passed.

Tariff writes go through Tesla's cloud. Cloud and FleetAPI entries use their
own client; a v1r entry, whose own connection is LAN only, uses the Fleet API
config file in /config/pypowerwall (or, failing that, a Cloud sign-in file
there). Fleet API tokens refresh normally; the Cloud sign-in only refreshes
while it keeps being used (pypowerwall's cloud/AUTH.md, RCA-8).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_CACHE_FILENAMES,
    CLOUD_LOGIN_AUTH_DIR,
    CONF_AUTHPATH,
    CONF_CONN_TYPE,
    CONF_TARIFF_EXPORT_RATES,
    CONF_TARIFF_IMPORT_RATES,
    CONN_TYPE_CLOUD,
    CONN_TYPE_FLEETAPI,
    CONN_TYPE_TEDAPI_V1R,
    DOMAIN,
    TARIFF_CONTROL_CONN_TYPES,
)
from .coordinator import async_connect_powerwall
from .tariff_builder import (
    ExportWindow,
    TariffBuildError,
    bands_from_rates,
    build_tariff,
    windows_by_date,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
FIRST_SYNC_SECONDS = 60
RATES_CHANGED_DELAY_SECONDS = 30
RETRY_SECONDS = 15 * 60
MAX_WINDOW_LENGTH = timedelta(hours=24)
MAX_WINDOW_AHEAD = timedelta(days=7)
OPTIMIZATION_STRATEGY = "economics"
ISSUE_TARIFF_SIGN_IN = "tariff_sign_in_failed"


def tariff_manager_enabled(entry: ConfigEntry) -> bool:
    return (
        entry.data[CONF_CONN_TYPE] in TARIFF_CONTROL_CONN_TYPES
        and bool(entry.options.get(CONF_TARIFF_IMPORT_RATES))
        and bool(entry.options.get(CONF_TARIFF_EXPORT_RATES))
    )


def _aware(value: datetime) -> datetime:
    """Times without a zone are Home Assistant local time, as the UI's date pickers send."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(value)


def _zone(name: str | None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, KeyError):
            _LOGGER.debug("Unknown site time zone %r, using Home Assistant's", name)
    return dt_util.get_default_time_zone()


def _site_info(pw: Any) -> dict[str, Any]:
    client = getattr(pw, "client", None)
    info: Any = None
    if hasattr(client, "get_site_config"):
        response = client.get_site_config(force=True)
        info = response.get("response") if isinstance(response, dict) else None
    elif hasattr(client, "fleet"):
        info = client.fleet.get_site_info(force=True)
    return info if isinstance(info, dict) else {}


def _write(pw: Any, tariff: dict[str, Any]) -> None:
    result = pw.set_tariff(
        {"optimization_strategy": OPTIMIZATION_STRATEGY, "tariff_content_v2": tariff}
    )
    if not result or (isinstance(result, dict) and result.get("ERROR")):
        raise HomeAssistantError(f"Tesla did not accept the tariff: {result!r}")


def _digest(tariff: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(tariff, sort_keys=True).encode()).hexdigest()


class TariffManager:
    """Owns the Tesla tariff for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.tariff_manager.{entry.entry_id}"
        )
        self._windows: list[ExportWindow] = []
        self._written: str | None = None
        self._timezone: str | None = None
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None

    @property
    def windows(self) -> list[ExportWindow]:
        return list(self._windows)

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_TARIFF_SIGN_IN}_{self.entry.entry_id}"

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self._windows = [
            ExportWindow(
                start=datetime.fromisoformat(window["start"]),
                end=datetime.fromisoformat(window["end"]),
                price=float(window["price"]),
            )
            for window in stored.get("windows", [])
        ]
        self._written = stored.get("written")
        self._timezone = stored.get("timezone")
        self._unsubs = [
            async_track_time_change(self.hass, self._async_daily, hour=0, minute=5, second=0),
            async_track_state_change_event(
                self.hass,
                [
                    self.entry.options[CONF_TARIFF_IMPORT_RATES],
                    self.entry.options[CONF_TARIFF_EXPORT_RATES],
                ],
                self._async_rates_changed,
            ),
        ]
        self._schedule(FIRST_SYNC_SECONDS, force=False)

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._pending is not None:
            self._pending()
            self._pending = None

    @callback
    def _schedule(self, delay: float, force: bool) -> None:
        if self._pending is not None:
            self._pending()

        async def _run(_now: datetime) -> None:
            self._pending = None
            try:
                await self.async_sync(force=force)
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Could not update the Tesla tariff, retrying in %s minutes: %s",
                    RETRY_SECONDS // 60,
                    err,
                )
                self._schedule(RETRY_SECONDS, force=force)

        self._pending = async_call_later(self.hass, delay, _run)

    async def _async_daily(self, _now: datetime) -> None:
        self._schedule(0, force=True)

    @callback
    def _async_rates_changed(self, _event: Event) -> None:
        self._schedule(RATES_CHANGED_DELAY_SECONDS, force=False)

    def _rates(self, option: str) -> list[dict[str, Any]]:
        entity_id = self.entry.options[option]
        state = self.hass.states.get(entity_id)
        rates = state.attributes.get("rates") if state is not None else None
        if not rates:
            raise HomeAssistantError(f"{entity_id} has no rates yet")
        return list(rates)

    def _build(self, zone: tzinfo) -> dict[str, Any]:
        try:
            return build_tariff(
                bands_from_rates(self._rates(CONF_TARIFF_IMPORT_RATES), zone),
                bands_from_rates(self._rates(CONF_TARIFF_EXPORT_RATES), zone),
                windows_by_date(self._windows, zone),
            )
        except TariffBuildError as err:
            raise HomeAssistantError(f"Could not build the tariff: {err}") from err

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "windows": [
                    {"start": w.start.isoformat(), "end": w.end.isoformat(), "price": w.price}
                    for w in self._windows
                ],
                "written": self._written,
                "timezone": self._timezone,
            }
        )

    async def _async_client(self) -> Any:
        conn_type = self.entry.data[CONF_CONN_TYPE]
        if conn_type != CONN_TYPE_TEDAPI_V1R:
            return self.entry.runtime_data.pw
        authpath = self.entry.data.get(CONF_AUTHPATH) or self.hass.config.path(
            CLOUD_LOGIN_AUTH_DIR
        )
        mode: str | None = None
        for candidate in (CONN_TYPE_FLEETAPI, CONN_TYPE_CLOUD):
            if await self.hass.async_add_executor_job(
                os.path.isfile, os.path.join(authpath, AUTH_CACHE_FILENAMES[candidate])
            ):
                mode = candidate
                break
        if mode is None:
            raise HomeAssistantError(
                "Tariff writes go through Tesla's cloud, but there is no "
                f"{AUTH_CACHE_FILENAMES[CONN_TYPE_FLEETAPI]} (Fleet API) or "
                f"{AUTH_CACHE_FILENAMES[CONN_TYPE_CLOUD]} (Cloud) in {authpath}"
            )
        try:
            pw = await async_connect_powerwall(self.hass, mode, {CONF_AUTHPATH: authpath})
        except ConfigEntryNotReady as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TARIFF_SIGN_IN,
                translation_placeholders={"title": self.entry.title, "path": authpath},
            )
            raise HomeAssistantError(f"Could not connect to Tesla's cloud: {err}") from err
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return pw

    async def async_sync(self, force: bool = False) -> None:
        """Write the tariff if it differs from the last one written, or always when forced."""
        async with self._lock:
            now = dt_util.utcnow()
            pruned = [w for w in self._windows if w.end > now]
            changed = len(pruned) != len(self._windows)
            self._windows = pruned

            # Rates are split into bands by the site's local day, so until the site's
            # time zone is known (first run) nothing can be built before asking Tesla.
            if self._timezone is not None and not force:
                tariff = self._build(_zone(self._timezone))
                if _digest(tariff) == self._written:
                    if changed:
                        await self._async_save()
                    return

            pw = await self._async_client()
            try:
                info = await self.hass.async_add_executor_job(_site_info, pw)
            except Exception as err:  # noqa: BLE001 - pypowerwall raises assorted network errors
                raise HomeAssistantError(f"Could not read the site from Tesla: {err}") from err
            din = self.entry.unique_id
            if info.get("id") and din and info["id"] != din:
                raise HomeAssistantError(
                    f"The Tesla account's site is gateway {info['id']}, not this entry's {din}"
                )
            if info.get("installation_time_zone"):
                self._timezone = info["installation_time_zone"]
            tariff = self._build(_zone(self._timezone))
            if not force and _digest(tariff) == self._written:
                await self._async_save()
                return

            try:
                await self.hass.async_add_executor_job(_write, pw, tariff)
            except HomeAssistantError:
                raise
            except Exception as err:  # noqa: BLE001 - pypowerwall raises assorted network errors
                raise HomeAssistantError(f"Could not write the tariff to Tesla: {err}") from err
            # Only written once Tesla has accepted it, so a failed write is retried
            # rather than mistaken for the tariff already being in place.
            self._written = _digest(tariff)
            await self._async_save()
            _LOGGER.info("Wrote the Tesla tariff (%d export windows)", len(self._windows))

    async def async_add_window(self, start: datetime | None, end: datetime, price: float) -> None:
        now = dt_util.utcnow()
        start = _aware(start) if start else now
        end = _aware(end)
        if end <= start or end <= now:
            raise HomeAssistantError("The export window must end after it starts and in the future")
        if end - start > MAX_WINDOW_LENGTH:
            raise HomeAssistantError("The export window can be at most 24 hours long")
        if end - now > MAX_WINDOW_AHEAD:
            raise HomeAssistantError("The export window must end within the next 7 days")
        if any(w.start < end and start < w.end for w in self._windows):
            raise HomeAssistantError("The export window overlaps one already set")
        self._windows.append(ExportWindow(start, end, price))
        self._windows.sort(key=lambda w: w.start)
        await self._async_save()
        try:
            await self.async_sync(force=True)
        except HomeAssistantError:
            self._schedule(RETRY_SECONDS, force=True)
            raise

    async def async_clear_windows(self) -> None:
        self._windows = []
        await self._async_save()
        try:
            await self.async_sync(force=True)
        except HomeAssistantError:
            self._schedule(RETRY_SECONDS, force=True)
            raise
