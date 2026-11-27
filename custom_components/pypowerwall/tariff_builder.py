"""Build the Tesla tariff that Home Assistant manages for the site.

Tesla's own supplier-linked plans can't be written back once changed (the API
only takes a complete tariff, which unlinks the plan), so a managed tariff is
built from scratch every time: the day's import and export rates from the
Octopus Energy integration, plus any export windows.

Layout follows the dynamic tariffs Tesla itself writes for Octopus plans:
each date with an export window gets a season of its own (fromMonth/fromDay
equal to toMonth/toDay) and the rest of the year is covered by seasons that
run between those dates, wrapping round the year end. A window therefore only
ever applies to its own date, so if a later write fails the stale window can
only touch a day that has already gone.

Every window is bought and sold at its price because Tesla rejects a sell
price above the buy price at any time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

DAY_MINUTES = 24 * 60
TARIFF_CODE = "HOME_ASSISTANT"
TARIFF_NAME = "Managed by Home Assistant"
TARIFF_UTILITY = "Home Assistant"
TARIFF_CURRENCY = "GBP"
WINDOW_LABEL = "export_window"


class TariffBuildError(Exception):
    """The rates or windows can't be turned into a valid tariff."""


@dataclass(frozen=True)
class Band:
    """A price from start to end, in minutes of the local day (end 1440 = midnight)."""

    start: int
    end: int
    price: float


@dataclass(frozen=True)
class ExportWindow:
    start: datetime
    end: datetime
    price: float


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TariffBuildError(f"Rate time {value!r} is not a date and time")


def bands_from_rates(rates: Iterable[Mapping[str, Any]], zone: tzinfo) -> list[Band]:
    """Collapse a day of Octopus rates into bands covering the local day exactly once."""
    slots: list[Band] = []
    for rate in rates:
        try:
            start = _as_datetime(rate["start"]).astimezone(zone)
            end = _as_datetime(rate["end"]).astimezone(zone)
            price = round(float(rate["value_inc_vat"]), 5)
        except (KeyError, TypeError, ValueError) as err:
            raise TariffBuildError(f"Unreadable rate {rate!r}") from err
        start_minute = start.hour * 60 + start.minute
        end_minute = end.hour * 60 + end.minute
        if end.date() > start.date() and end_minute == 0:
            end_minute = DAY_MINUTES
        if end_minute <= start_minute:
            raise TariffBuildError(f"Rate {rate!r} does not fit within one day")
        slots.append(Band(start_minute, end_minute, price))
    slots.sort(key=lambda band: band.start)

    bands: list[Band] = []
    position = 0
    for slot in slots:
        if slot.start != position:
            raise TariffBuildError(
                f"Rates leave a gap or overlap at {position // 60:02d}:{position % 60:02d}"
            )
        if bands and bands[-1].price == slot.price:
            bands[-1] = Band(bands[-1].start, slot.end, slot.price)
        else:
            bands.append(slot)
        position = slot.end
    if position != DAY_MINUTES:
        raise TariffBuildError("Rates do not cover the whole day")
    return bands


def windows_by_date(
    windows: Iterable[ExportWindow], zone: tzinfo
) -> dict[date, list[Band]]:
    """Split windows at local midnight into per-date bands."""
    by_date: dict[date, list[Band]] = {}
    for window in windows:
        start = window.start.astimezone(zone)
        end = window.end.astimezone(zone)
        while start < end:
            next_midnight = datetime.combine(
                start.date() + timedelta(days=1), datetime.min.time(), zone
            )
            piece_end = min(end, next_midnight)
            end_minute = (
                DAY_MINUTES
                if piece_end == next_midnight
                else piece_end.hour * 60 + piece_end.minute
            )
            start_minute = start.hour * 60 + start.minute
            if end_minute > start_minute:
                by_date.setdefault(start.date(), []).append(
                    Band(start_minute, end_minute, window.price)
                )
            start = piece_end
    for day, bands in by_date.items():
        bands.sort(key=lambda band: band.start)
        for earlier, later in zip(bands, bands[1:], strict=False):
            if later.start < earlier.end:
                raise TariffBuildError(f"Export windows overlap on {day.isoformat()}")
    return by_date


def _label(band: Band) -> str:
    return f"hour_{band.start // 60}_minute_{band.start % 60}"


def _period(start: int, end: int) -> dict[str, int]:
    end %= DAY_MINUTES
    return {
        "fromDayOfWeek": 0,
        "toDayOfWeek": 6,
        "fromHour": start // 60,
        "fromMinute": start % 60,
        "toHour": end // 60,
        "toMinute": end % 60,
    }


def _cut(bands: list[Band], windows: list[Band]) -> list[tuple[str, int, int, float]]:
    """(label, start, end, price) for each band piece outside the windows, then the windows."""
    pieces: list[tuple[str, int, int, float]] = []
    for band in bands:
        spans = [(band.start, band.end)]
        for window in windows:
            spans = [
                piece
                for start, end in spans
                for piece in ((start, min(end, window.start)), (max(start, window.end), end))
                if piece[1] > piece[0]
            ]
        pieces += [(_label(band), start, end, band.price) for start, end in spans]
    for index, window in enumerate(windows, start=1):
        pieces.append((f"{WINDOW_LABEL}_{index}", window.start, window.end, window.price))
    return pieces


def _season_content(
    bands: list[Band], windows: list[Band]
) -> tuple[dict[str, Any], dict[str, float]]:
    tou_periods: dict[str, dict[str, list[dict[str, int]]]] = {}
    rates: dict[str, float] = {}
    for label, start, end, price in _cut(bands, windows):
        tou_periods.setdefault(label, {"periods": []})["periods"].append(_period(start, end))
        rates[label] = price
    return tou_periods, rates


def _date_ranges(event_dates: list[date]) -> list[tuple[str, date | None, date | None]]:
    """(season name, from, to) covering every day of the year once; None means all year."""
    if not event_dates:
        return [("All year", None, None)]
    ranges: list[tuple[str, date | None, date | None]] = [
        (f"Export {day.isoformat()}", day, day) for day in event_dates
    ]
    for index, (day, next_day) in enumerate(zip(event_dates, event_dates[1:], strict=False)):
        first, last = day + timedelta(days=1), next_day - timedelta(days=1)
        if first <= last:
            ranges.append((f"Normal {index + 1}", first, last))
    # Seasons are month/day only, so the last one wraps past the year end back to
    # the day before the first window; working it out by month/day rather than
    # adding 365 days keeps leap years from leaving a day uncovered.
    wrap_first = event_dates[-1] + timedelta(days=1)
    wrap_last = event_dates[0] - timedelta(days=1)
    if (wrap_first.month, wrap_first.day) != (event_dates[0].month, event_dates[0].day):
        ranges.append((f"Normal {len(event_dates)}", wrap_first, wrap_last))
    return ranges


def build_tariff(
    import_bands: list[Band],
    export_bands: list[Band],
    windows: Mapping[date, list[Band]],
) -> dict[str, Any]:
    """A complete tariff_content_v2 for the given day's bands and dated export windows."""
    for band in export_bands:
        overlaps = [b for b in import_bands if b.start < band.end and band.start < b.end]
        if any(band.price > b.price for b in overlaps):
            raise TariffBuildError("An export rate is above the import rate at the same time")
    event_dates = sorted(windows)
    if len(event_dates) > 1 and event_dates[-1] - event_dates[0] >= timedelta(days=365):
        raise TariffBuildError("Export windows must fall within one year")

    seasons: dict[str, dict[str, Any]] = {}
    sell_seasons: dict[str, dict[str, Any]] = {}
    energy_charges: dict[str, dict[str, Any]] = {}
    sell_energy_charges: dict[str, dict[str, Any]] = {}
    for name, first, last in _date_ranges(event_dates):
        dates = (
            {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31}
            if first is None or last is None
            else {
                "fromMonth": first.month,
                "fromDay": first.day,
                "toMonth": last.month,
                "toDay": last.day,
            }
        )
        day_windows = windows.get(first, []) if first is not None and first == last else []
        tou_periods, rates = _season_content(import_bands, day_windows)
        seasons[name] = {**dates, "tou_periods": tou_periods}
        energy_charges[name] = {"rates": rates}
        sell_tou_periods, sell_rates = _season_content(export_bands, day_windows)
        sell_seasons[name] = {**dates, "tou_periods": sell_tou_periods}
        sell_energy_charges[name] = {"rates": sell_rates}

    demand_charges = {"ALL": {"rates": {"ALL": 0}}}
    return {
        "version": 1,
        "code": TARIFF_CODE,
        "name": TARIFF_NAME,
        "utility": TARIFF_UTILITY,
        "currency": TARIFF_CURRENCY,
        "demand_charges": demand_charges,
        "energy_charges": energy_charges,
        "seasons": seasons,
        "sell_tariff": {
            "code": TARIFF_CODE,
            "name": TARIFF_NAME,
            "utility": TARIFF_UTILITY,
            "currency": TARIFF_CURRENCY,
            "demand_charges": demand_charges,
            "energy_charges": sell_energy_charges,
            "seasons": sell_seasons,
        },
    }
