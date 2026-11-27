"""Export window actions: rewrite the Tesla tariff for a while, then put it back.

The original tariff is saved to Home Assistant storage before anything is
written, so a restart in the middle of a window still restores it: on setup a
saved tariff is put back at its window's end, or straight away if that has
passed. A failed restore is retried every RESTORE_RETRY_SECONDS.

v1r entries reach Tesla's cloud through their own sign-in file, which is
checked every SIGN_IN_CHECK_INTERVAL to keep it refreshed; a failed check
raises a repair issue asking for a new sign-in.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_CACHE_FILENAMES,
    CLOUD_LOGIN_AUTH_DIR,
    CONF_AUTHPATH,
    CONF_CONN_TYPE,
    CONN_TYPE_CLOUD,
    CONN_TYPE_TEDAPI_V1R,
    DOMAIN,
)
from .coordinator import async_connect_powerwall
from .tariff import SiteTariff, TariffError, read_site_tariff, with_export_window, write_site_tariff

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
RESTORE_RETRY_SECONDS = 300
MAX_WINDOW = timedelta(hours=24)
SIGN_IN_FIRST_CHECK_SECONDS = 60
SIGN_IN_CHECK_INTERVAL = timedelta(hours=6)
ISSUE_TESLA_SIGN_IN = "tesla_sign_in_failed"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


def _site_zone(name: str | None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, KeyError):
            _LOGGER.debug("Unknown site time zone %r, using Home Assistant's", name)
    return dt_util.get_default_time_zone()


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


class TariffController:
    """Export windows and tariff restore for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.tariff_backup.{entry.entry_id}"
        )
        self._backup: dict[str, Any] | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._unsub_checks: list[CALLBACK_TYPE] = []
        self._lock = asyncio.Lock()

    @property
    def backup(self) -> dict[str, Any] | None:
        """The saved original tariff while an export window is in place."""
        return self._backup

    async def async_start(self) -> None:
        self._backup = await self._store.async_load()
        if self._backup:
            self._schedule_restore(dt_util.parse_datetime(self._backup["end"]))
        if self._uses_own_sign_in:
            self._unsub_checks = [
                async_call_later(self.hass, SIGN_IN_FIRST_CHECK_SECONDS, self._async_check_sign_in),
                async_track_time_interval(
                    self.hass,
                    self._async_check_sign_in,
                    SIGN_IN_CHECK_INTERVAL,
                    cancel_on_shutdown=True,
                ),
            ]

    @callback
    def async_stop(self) -> None:
        self._cancel()
        for unsub in self._unsub_checks:
            unsub()
        self._unsub_checks = []

    @callback
    def _cancel(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _schedule_restore(self, when: datetime | None) -> None:
        self._cancel()
        if when is None or when <= dt_util.utcnow():
            self._unsub = async_call_later(self.hass, 0, self._async_timed_restore)
        else:
            self._unsub = async_track_point_in_time(self.hass, self._async_timed_restore, when)

    async def _async_timed_restore(self, _now: datetime) -> None:
        self._unsub = None
        try:
            await self.async_restore()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not restore the Tesla tariff, retrying in %s seconds: %s",
                RESTORE_RETRY_SECONDS,
                err,
            )

    @property
    def _uses_own_sign_in(self) -> bool:
        """v1r entries reach Tesla's cloud through a separate sign-in file."""
        return self.entry.data[CONF_CONN_TYPE] == CONN_TYPE_TEDAPI_V1R

    @property
    def _authpath(self) -> str:
        return self.entry.data.get(CONF_AUTHPATH) or self.hass.config.path(CLOUD_LOGIN_AUTH_DIR)

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_TESLA_SIGN_IN}_{self.entry.entry_id}"

    async def _async_has_sign_in(self) -> bool:
        return await self.hass.async_add_executor_job(
            os.path.isfile, os.path.join(self._authpath, AUTH_CACHE_FILENAMES[CONN_TYPE_CLOUD])
        )

    async def _async_cloud_client(self) -> Any:
        if not self._uses_own_sign_in:
            return self.entry.runtime_data.pw
        if not await self._async_has_sign_in():
            raise HomeAssistantError(
                "Tariff changes go through Tesla's cloud and need a Tesla sign-in, but there is "
                f"no {AUTH_CACHE_FILENAMES[CONN_TYPE_CLOUD]} in {self._authpath}. Use "
                "Reconfigure → \"Sign in to Tesla\" on this Powerwall's integration entry."
            )
        try:
            pw = await async_connect_powerwall(
                self.hass, CONN_TYPE_CLOUD, {CONF_AUTHPATH: self._authpath}
            )
        except ConfigEntryNotReady as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TESLA_SIGN_IN,
                translation_placeholders={"title": self.entry.title},
            )
            raise HomeAssistantError(f"Could not connect to Tesla's cloud: {err}") from err
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return pw

    async def _async_check_sign_in(self, _now: datetime | None = None) -> None:
        """Use the sign-in so Tesla keeps refreshing it, and flag it once it stops working.

        Tesla only refreshes a sign-in whose token has been used (pypowerwall's
        cloud/AUTH.md, RCA-8), so one left idle past its ~8 hour life can't be
        revived; regular use, and a repair notice when that fails, avoid finding
        out in the middle of an export window.
        """
        if not await self._async_has_sign_in():
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return
        try:
            await self._async_cloud_client()
        except HomeAssistantError as err:
            _LOGGER.warning("Tesla sign-in check for export windows failed: %s", err)

    async def _async_read(self, pw: Any) -> SiteTariff:
        try:
            site = await self.hass.async_add_executor_job(read_site_tariff, pw)
        except TariffError as err:
            raise HomeAssistantError(str(err)) from err
        except Exception as err:  # noqa: BLE001 - pypowerwall raises assorted network errors
            raise HomeAssistantError(f"Could not read the tariff from Tesla: {err}") from err
        din = self.entry.unique_id
        if site.din and din and site.din != din:
            raise HomeAssistantError(
                f"The Tesla account's site is gateway {site.din}, not this entry's {din}"
            )
        return site

    async def _async_write(self, pw: Any, tariff: dict[str, Any], strategy: str) -> None:
        try:
            await self.hass.async_add_executor_job(write_site_tariff, pw, tariff, strategy)
        except Exception as err:  # noqa: BLE001 - pypowerwall raises assorted network errors
            raise HomeAssistantError(f"Could not write the tariff to Tesla: {err}") from err

    async def async_set_export_window(
        self, start: datetime | None, end: datetime, price: float
    ) -> None:
        """Make start..end the most valuable time to export, restoring the tariff at end."""
        now = dt_util.utcnow()
        start = _aware(start) if start else now
        end = _aware(end)
        if end <= start or end <= now:
            raise HomeAssistantError("The export window must end after it starts and in the future")
        if end - start >= MAX_WINDOW:
            raise HomeAssistantError("The export window must be shorter than 24 hours")

        async with self._lock:
            pw = await self._async_cloud_client()
            if self._backup is not None:
                site = SiteTariff(
                    tariff=self._backup["tariff"],
                    optimization_strategy=self._backup["optimization_strategy"],
                    timezone=self._backup.get("timezone"),
                    din=None,
                )
            else:
                site = await self._async_read(pw)
            zone = _site_zone(site.timezone)
            try:
                tariff = with_export_window(
                    site.tariff,
                    _minute_of_day(start.astimezone(zone)),
                    _minute_of_day(end.astimezone(zone)),
                    price,
                )
            except TariffError as err:
                raise HomeAssistantError(str(err)) from err

            self._backup = {
                "tariff": site.tariff,
                "optimization_strategy": site.optimization_strategy,
                "timezone": site.timezone,
                "end": end.isoformat(),
            }
            await self._store.async_save(self._backup)
            # Scheduled before the write: a write that fails after Tesla applied it
            # still gets put back, and re-sending an unchanged tariff is harmless.
            self._schedule_restore(end)
            await self._async_write(pw, tariff, site.optimization_strategy)

    async def async_restore(self) -> None:
        """Put the saved tariff back now, retrying later if Tesla can't be reached."""
        async with self._lock:
            if self._backup is None:
                return
            self._cancel()
            backup = self._backup
            try:
                pw = await self._async_cloud_client()
                await self._async_write(pw, backup["tariff"], backup["optimization_strategy"])
            except HomeAssistantError:
                self._unsub = async_call_later(
                    self.hass, RESTORE_RETRY_SECONDS, self._async_timed_restore
                )
                raise
            self._backup = None
            await self._store.async_remove()
            _LOGGER.info("Restored the Tesla tariff after the export window")
