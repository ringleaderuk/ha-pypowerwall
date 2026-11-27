"""Temporarily rewrite the Tesla tariff so the Powerwall exports in a chosen window.

Powerwalls have no "force export" command. In Time-Based Control the optimiser
exports when the tariff it holds says the current price beats later ones, so an
export window is a copy of the site's own tariff with that time of day carved
out of every period and given its own, high price. Buy and sell get the same
price because Tesla rejects a sell price above the buy price.

Writes take Tesla's tariff_content_v2 form. Cloud mode only reads the older
form, which nests rates and periods one level less (pypowerwall's API.md,
"Utility tariff"), so to_v2 converts it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

EXPORT_WINDOW_PERIOD = "EXPORT_WINDOW"
DEFAULT_OPTIMIZATION_STRATEGY = "economics"
DAY_MINUTES = 24 * 60


class TariffError(Exception):
    """The tariff could not be read, changed or written."""


@dataclass(frozen=True)
class SiteTariff:
    tariff: dict[str, Any]
    optimization_strategy: str
    timezone: str | None
    din: str | None


def _copy(tariff: dict[str, Any]) -> dict[str, Any]:
    # Copied apart so buy and sell never share a seasons object, even if the input does.
    out = copy.deepcopy(tariff)
    if isinstance(out.get("sell_tariff"), dict):
        out["sell_tariff"] = copy.deepcopy(tariff["sell_tariff"])
    return out


def to_v2(tariff: dict[str, Any]) -> dict[str, Any]:
    """Return tariff in tariff_content_v2 form; already-v2 parts are left alone."""
    out = _copy(tariff)
    for part in (out, out.get("sell_tariff")):
        if not isinstance(part, dict):
            continue
        for key in ("energy_charges", "demand_charges"):
            charges = part.get(key)
            if isinstance(charges, dict):
                for season, rates in charges.items():
                    if isinstance(rates, dict) and not isinstance(rates.get("rates"), dict):
                        charges[season] = {"rates": rates}
        for season in (part.get("seasons") or {}).values():
            tou_periods = season.get("tou_periods") if isinstance(season, dict) else None
            if isinstance(tou_periods, dict):
                for label, periods in tou_periods.items():
                    if isinstance(periods, list):
                        tou_periods[label] = {"periods": periods}
    return out


def _period_intervals(period: dict[str, Any]) -> list[tuple[int, int]]:
    start = period.get("fromHour", 0) * 60 + period.get("fromMinute", 0)
    end = period.get("toHour", 0) * 60 + period.get("toMinute", 0)
    if end > start:
        return [(start, end)]
    # A "to" at or before "from" runs past midnight; to 00:00 is the end of the day.
    return [(start, DAY_MINUTES)] + ([(0, end)] if end else [])


def window_intervals(start_minute: int, end_minute: int) -> list[tuple[int, int]]:
    """Minute-of-day intervals for a window, split in two if it crosses midnight."""
    if start_minute == end_minute:
        raise TariffError("The export window must be shorter than 24 hours")
    if end_minute > start_minute:
        return [(start_minute, end_minute)]
    return [(start_minute, DAY_MINUTES)] + ([(0, end_minute)] if end_minute else [])


def _subtract(interval: tuple[int, int], cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pieces = [interval]
    for cut_start, cut_end in cuts:
        pieces = [
            piece
            for start, end in pieces
            for piece in ((start, min(end, cut_start)), (max(start, cut_end), end))
            if piece[1] > piece[0]
        ]
    return pieces


def _period(template: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    end = end % DAY_MINUTES
    return {
        **template,
        "fromHour": start // 60,
        "fromMinute": start % 60,
        "toHour": end // 60,
        "toMinute": end % 60,
    }


def _drop_rate(part: dict[str, Any], season: str, label: str) -> None:
    for key in ("energy_charges", "demand_charges"):
        rates = ((part.get(key) or {}).get(season) or {}).get("rates")
        if isinstance(rates, dict):
            rates.pop(label, None)


def _cut_seasons(part: dict[str, Any], cuts: list[tuple[int, int]]) -> None:
    for name, season in part["seasons"].items():
        tou_periods = season.get("tou_periods")
        if not tou_periods:
            raise TariffError(
                f"Tariff season {name!r} has no time-of-use periods; set up a "
                "time-of-use tariff in the Tesla app first"
            )
        for label in list(tou_periods):
            kept = [
                _period(period, start, end)
                for period in tou_periods[label].get("periods") or []
                for interval in _period_intervals(period)
                for start, end in _subtract(interval, cuts)
            ]
            if kept:
                tou_periods[label] = {"periods": kept}
            else:
                del tou_periods[label]
                _drop_rate(part, name, label)
        tou_periods[EXPORT_WINDOW_PERIOD] = {
            "periods": [
                _period({"fromDayOfWeek": 0, "toDayOfWeek": 6}, start, end) for start, end in cuts
            ]
        }


def with_export_window(
    tariff: dict[str, Any], start_minute: int, end_minute: int, price: float
) -> dict[str, Any]:
    """Copy of a v2 tariff where start..end buys and sells at price.

    start/end are minutes of the day, applied to every day: the tariff is put
    back when the window ends, so later days never see it.
    """
    out = _copy(tariff)
    if not out.get("seasons"):
        raise TariffError(
            "The site's tariff has no seasons; set up a time-of-use tariff in the Tesla app first"
        )
    sell = out.get("sell_tariff")
    if not isinstance(sell, dict):
        raise TariffError(
            "The site's tariff has no export (sell) rates; add them in the Tesla app first"
        )
    cuts = window_intervals(start_minute, end_minute)
    for part in (out, sell):
        if part.get("seasons"):
            _cut_seasons(part, cuts)
        charges = part.setdefault("energy_charges", {})
        for season in part.get("seasons") or out["seasons"]:
            charges.setdefault(season, {}).setdefault("rates", {})[EXPORT_WINDOW_PERIOD] = price
    if not sell.get("seasons"):
        # Sell rates without seasons of their own follow the buy periods, so drop
        # rates for any period the window swallowed whole.
        for name, season in out["seasons"].items():
            for label in list(sell["energy_charges"][name]["rates"]):
                if label != "ALL" and label not in season["tou_periods"]:
                    _drop_rate(sell, name, label)
    return out


def _site_info(pw: Any) -> dict[str, Any]:
    client = getattr(pw, "client", None)
    info: Any = None
    if hasattr(client, "get_site_config"):
        response = client.get_site_config(force=True)
        info = response.get("response") if isinstance(response, dict) else None
    elif hasattr(client, "fleet"):
        info = client.fleet.get_site_info(force=True)
    return info if isinstance(info, dict) else {}


def read_site_tariff(pw: Any) -> SiteTariff:
    """The site's current tariff in v2 form. Blocking; pw must be a Cloud or FleetAPI client."""
    info = _site_info(pw)
    tariff = (
        info.get("tariff_content_v2")
        or info.get("tariff_content")
        or pw.get_tariff(force=True)
    )
    if not isinstance(tariff, dict) or not tariff:
        raise TariffError("Tesla returned no tariff for this site")
    strategy = (info.get("tou_settings") or {}).get("optimization_strategy")
    return SiteTariff(
        tariff=to_v2(tariff),
        optimization_strategy=strategy or DEFAULT_OPTIMIZATION_STRATEGY,
        timezone=info.get("installation_time_zone"),
        din=info.get("id"),
    )


def write_site_tariff(pw: Any, tariff: dict[str, Any], optimization_strategy: str) -> None:
    """Send a v2 tariff to Tesla. Blocking; pw must be a Cloud or FleetAPI client."""
    result = pw.set_tariff(
        {"optimization_strategy": optimization_strategy, "tariff_content_v2": tariff}
    )
    if not result or (isinstance(result, dict) and result.get("ERROR")):
        raise TariffError(f"Tesla did not accept the tariff: {result!r}")
