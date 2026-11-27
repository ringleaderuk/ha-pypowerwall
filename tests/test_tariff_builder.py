"""Tests for building the managed Tesla tariff (no Home Assistant needed)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pypowerwall.tariff_builder import (
    DAY_MINUTES,
    Band,
    ExportWindow,
    TariffBuildError,
    bands_from_rates,
    build_tariff,
    windows_by_date,
)

LONDON = ZoneInfo("Europe/London")
FLUX_IMPORT = [
    (0, 2, 0.2805),
    (2, 5, 0.16936),
    (5, 16, 0.2805),
    (16, 19, 0.39164),
    (19, 24, 0.2805),
]
FLUX_EXPORT = [
    (0, 2, 0.11644),
    (2, 5, 0.04943),
    (5, 16, 0.11644),
    (16, 19, 0.31755),
    (19, 24, 0.11644),
]


def _rates(day: date, bands, zone=LONDON):
    """Half-hourly Octopus Energy integration rates for one local day."""
    start = datetime.combine(day, datetime.min.time(), zone)
    rates = []
    slot = start
    while slot.date() == day:
        hour = slot.hour + slot.minute / 60
        price = next(p for first, last, p in bands if first <= hour < last)
        end = slot + timedelta(minutes=30)
        rates.append({"start": slot, "end": end, "value_inc_vat": price, "is_capped": False})
        slot = end
    return rates


def _flux_bands():
    day = date(2026, 11, 27)
    return bands_from_rates(_rates(day, FLUX_IMPORT), LONDON), bands_from_rates(
        _rates(day, FLUX_EXPORT), LONDON
    )


def _minutes(season):
    covered = []
    for value in season["tou_periods"].values():
        for p in value["periods"]:
            start = p["fromHour"] * 60 + p["fromMinute"]
            end = p["toHour"] * 60 + p["toMinute"] or DAY_MINUTES
            covered += range(start, end)
    return sorted(covered)


LEAP_DAYS = [(date(2028, 1, 1) + timedelta(days=n)).strftime("%m-%d") for n in range(366)]


def _season_days(season):
    """Month/days a season covers, counted round a leap year so 02-29 must be covered too."""
    first = LEAP_DAYS.index(f"{season['fromMonth']:02d}-{season['fromDay']:02d}")
    last = LEAP_DAYS.index(f"{season['toMonth']:02d}-{season['toDay']:02d}")
    if last < first:
        return LEAP_DAYS[first:] + LEAP_DAYS[: last + 1]
    return LEAP_DAYS[first : last + 1]


class TestBandsFromRates:
    def test_flux_day_collapses_to_five_bands(self):
        imports, _ = _flux_bands()
        assert imports == [
            Band(0, 120, 0.2805),
            Band(120, 300, 0.16936),
            Band(300, 960, 0.2805),
            Band(960, 1140, 0.39164),
            Band(1140, 1440, 0.2805),
        ]

    def test_iso_strings_accepted(self):
        rates = [
            {**r, "start": r["start"].isoformat(), "end": r["end"].isoformat()}
            for r in _rates(date(2026, 11, 27), FLUX_IMPORT)
        ]
        assert len(bands_from_rates(rates, LONDON)) == 5

    @pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
    def test_clock_change_days(self, day):
        bands = bands_from_rates(_rates(day, FLUX_IMPORT), LONDON)
        assert bands[0].start == 0
        assert bands[-1].end == DAY_MINUTES

    def test_gap_rejected(self):
        rates = _rates(date(2026, 11, 27), FLUX_IMPORT)
        del rates[10]
        with pytest.raises(TariffBuildError, match="gap"):
            bands_from_rates(rates, LONDON)

    def test_partial_day_rejected(self):
        with pytest.raises(TariffBuildError, match="whole day"):
            bands_from_rates(_rates(date(2026, 11, 27), FLUX_IMPORT)[:-2], LONDON)

    def test_unreadable_rate_rejected(self):
        with pytest.raises(TariffBuildError):
            bands_from_rates([{"start": "x"}], LONDON)


class TestWindowsByDate:
    def test_single_day(self):
        window = ExportWindow(
            datetime(2026, 11, 28, 13, tzinfo=LONDON),
            datetime(2026, 11, 28, 14, tzinfo=LONDON),
            2.0,
        )
        assert windows_by_date([window], LONDON) == {date(2026, 11, 28): [Band(780, 840, 2.0)]}

    def test_split_at_midnight(self):
        window = ExportWindow(
            datetime(2026, 11, 28, 23, tzinfo=LONDON), datetime(2026, 11, 29, 1, tzinfo=LONDON), 2.0
        )
        assert windows_by_date([window], LONDON) == {
            date(2026, 11, 28): [Band(1380, 1440, 2.0)],
            date(2026, 11, 29): [Band(0, 60, 2.0)],
        }

    def test_utc_input_converted_to_site_time(self):
        utc = ZoneInfo("UTC")
        window = ExportWindow(
            datetime(2026, 7, 1, 12, tzinfo=utc), datetime(2026, 7, 1, 13, tzinfo=utc), 2.0
        )
        assert windows_by_date([window], LONDON) == {date(2026, 7, 1): [Band(780, 840, 2.0)]}

    def test_overlap_rejected(self):
        first = ExportWindow(
            datetime(2026, 11, 28, 13, tzinfo=LONDON),
            datetime(2026, 11, 28, 14, tzinfo=LONDON),
            2.0,
        )
        second = ExportWindow(
            datetime(2026, 11, 28, 13, 30, tzinfo=LONDON),
            datetime(2026, 11, 28, 15, tzinfo=LONDON),
            2.0,
        )
        with pytest.raises(TariffBuildError, match="overlap"):
            windows_by_date([first, second], LONDON)


class TestBuildTariff:
    def test_no_windows_is_one_all_year_season(self):
        imports, exports = _flux_bands()
        tariff = build_tariff(imports, exports, {})
        assert list(tariff["seasons"]) == ["All year"]
        season = tariff["seasons"]["All year"]
        assert (season["fromMonth"], season["fromDay"], season["toMonth"], season["toDay"]) == (
            1,
            1,
            12,
            31,
        )
        assert _minutes(season) == list(range(DAY_MINUTES))
        assert tariff["energy_charges"]["All year"]["rates"] == {
            "hour_0_minute_0": 0.2805,
            "hour_2_minute_0": 0.16936,
            "hour_5_minute_0": 0.2805,
            "hour_16_minute_0": 0.39164,
            "hour_19_minute_0": 0.2805,
        }
        assert tariff["sell_tariff"]["energy_charges"]["All year"]["rates"]["hour_16_minute_0"] == (
            0.31755
        )
        assert tariff["currency"] == "GBP"
        assert tariff["name"] == "Managed by Home Assistant"
        assert tariff["sell_tariff"]["name"] == "Managed by Home Assistant (export)"
        assert tariff["version"] == 1

    def test_window_gets_its_own_dated_season(self):
        imports, exports = _flux_bands()
        windows = {date(2026, 11, 28): [Band(780, 840, 2.0)]}
        tariff = build_tariff(imports, exports, windows)

        event = tariff["seasons"]["Export 2026-11-28"]
        assert (event["fromMonth"], event["fromDay"], event["toMonth"], event["toDay"]) == (
            11,
            28,
            11,
            28,
        )
        assert event["tou_periods"]["export_window_1"]["periods"] == [
            {
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 13,
                "fromMinute": 0,
                "toHour": 14,
                "toMinute": 0,
            }
        ]
        assert event["tou_periods"]["hour_5_minute_0"]["periods"] == [
            {
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 5,
                "fromMinute": 0,
                "toHour": 13,
                "toMinute": 0,
            },
            {
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 14,
                "fromMinute": 0,
                "toHour": 16,
                "toMinute": 0,
            },
        ]
        assert tariff["energy_charges"]["Export 2026-11-28"]["rates"]["export_window_1"] == 2.0
        sell = tariff["sell_tariff"]
        assert sell["energy_charges"]["Export 2026-11-28"]["rates"]["export_window_1"] == 2.0
        assert "export_window_1" in sell["seasons"]["Export 2026-11-28"]["tou_periods"]

        for part in (tariff, sell):
            for season in part["seasons"].values():
                assert _minutes(season) == list(range(DAY_MINUTES))

    @pytest.mark.parametrize(
        "days",
        [
            [date(2026, 11, 28)],
            [date(2026, 11, 28), date(2026, 11, 29)],
            [date(2026, 11, 28), date(2026, 12, 3)],
            [date(2026, 12, 31), date(2027, 1, 1)],
            [date(2026, 1, 1)],
            [date(2026, 12, 31)],
            [date(2028, 2, 28)],
        ],
    )
    def test_seasons_cover_every_day_once(self, days):
        imports, exports = _flux_bands()
        tariff = build_tariff(imports, exports, {d: [Band(780, 840, 2.0)] for d in days})
        covered = []
        for season in tariff["seasons"].values():
            covered += _season_days(season)
        assert sorted(covered) == LEAP_DAYS
        assert set(tariff["seasons"]) == set(tariff["sell_tariff"]["seasons"])

    def test_normal_seasons_have_no_window(self):
        imports, exports = _flux_bands()
        tariff = build_tariff(imports, exports, {date(2026, 11, 28): [Band(780, 840, 2.0)]})
        for name, season in tariff["seasons"].items():
            if name.startswith("Normal"):
                assert not any(label.startswith("export_window") for label in season["tou_periods"])

    def test_export_above_import_rejected(self):
        imports, exports = _flux_bands()
        exports = [Band(b.start, b.end, b.price + 1) for b in exports]
        with pytest.raises(TariffBuildError, match="above the import"):
            build_tariff(imports, exports, {})
