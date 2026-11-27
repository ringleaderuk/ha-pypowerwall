"""Tests for the export window tariff rewrite (no Home Assistant needed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.pypowerwall.tariff import (
    DEFAULT_OPTIMIZATION_STRATEGY,
    EXPORT_WINDOW_PERIOD,
    TariffError,
    read_site_tariff,
    to_v2,
    window_intervals,
    with_export_window,
    write_site_tariff,
)


def _p(from_hour, to_hour, from_minute=0, to_minute=0, days=(0, 6)):
    return {
        "fromDayOfWeek": days[0],
        "toDayOfWeek": days[1],
        "fromHour": from_hour,
        "fromMinute": from_minute,
        "toHour": to_hour,
        "toMinute": to_minute,
    }


def _v1_tariff():
    """An Octopus-style day: cheap overnight, peak 16:00-19:00, standard otherwise."""
    seasons = {
        "All Year": {
            "fromMonth": 1,
            "toMonth": 12,
            "fromDay": 1,
            "toDay": 31,
            "tou_periods": {
                "OFF_PEAK": [_p(23, 5, 30, 30)],
                "ON_PEAK": [_p(16, 19)],
                "PARTIAL_PEAK": [_p(5, 16, 30), _p(19, 23, 0, 30)],
            },
        }
    }
    return {
        "name": "Octopus",
        "currency": "GBP",
        "energy_charges": {
            "ALL": {"ALL": 0},
            "All Year": {"OFF_PEAK": 0.07, "ON_PEAK": 0.35, "PARTIAL_PEAK": 0.25},
        },
        "demand_charges": {"ALL": {"ALL": 0}, "All Year": {}},
        "seasons": seasons,
        "sell_tariff": {
            "energy_charges": {
                "ALL": {"ALL": 0},
                "All Year": {"OFF_PEAK": 0.15, "ON_PEAK": 0.15, "PARTIAL_PEAK": 0.15},
            },
            "seasons": seasons,
        },
    }


def _minutes(periods):
    """Every minute of the day covered by these periods, with repeats kept."""
    covered = []
    for period in periods:
        start = period["fromHour"] * 60 + period["fromMinute"]
        end = period["toHour"] * 60 + period["toMinute"] or 1440
        if end <= start:
            covered += list(range(start, 1440)) + list(range(0, end))
        else:
            covered += list(range(start, end))
    return covered


def _all_periods(season):
    return [p for value in season["tou_periods"].values() for p in value["periods"]]


class TestToV2:
    def test_nests_rates_and_periods(self):
        v2 = to_v2(_v1_tariff())
        assert v2["energy_charges"]["All Year"] == {
            "rates": {"OFF_PEAK": 0.07, "ON_PEAK": 0.35, "PARTIAL_PEAK": 0.25}
        }
        assert v2["energy_charges"]["ALL"] == {"rates": {"ALL": 0}}
        assert v2["demand_charges"]["All Year"] == {"rates": {}}
        assert v2["seasons"]["All Year"]["tou_periods"]["ON_PEAK"] == {"periods": [_p(16, 19)]}
        assert v2["sell_tariff"]["energy_charges"]["All Year"]["rates"]["ON_PEAK"] == 0.15
        assert v2["sell_tariff"]["seasons"]["All Year"]["tou_periods"]["ON_PEAK"] == {
            "periods": [_p(16, 19)]
        }

    def test_buy_and_sell_seasons_copied_apart(self):
        v2 = to_v2(_v1_tariff())
        assert v2["seasons"] is not v2["sell_tariff"]["seasons"]

    def test_v2_is_left_alone(self):
        v2 = to_v2(_v1_tariff())
        assert to_v2(v2) == v2

    def test_does_not_change_its_input(self):
        v1 = _v1_tariff()
        to_v2(v1)
        assert v1 == _v1_tariff()


class TestWindowIntervals:
    def test_same_day(self):
        assert window_intervals(13 * 60, 14 * 60) == [(780, 840)]

    def test_across_midnight(self):
        assert window_intervals(23 * 60, 60) == [(1380, 1440), (0, 60)]

    def test_to_midnight(self):
        assert window_intervals(23 * 60, 0) == [(1380, 1440)]

    def test_zero_length_rejected(self):
        with pytest.raises(TariffError):
            window_intervals(600, 600)


class TestWithExportWindow:
    def test_window_carved_out_and_priced(self):
        tariff = with_export_window(to_v2(_v1_tariff()), 13 * 60, 14 * 60, 2.0)
        season = tariff["seasons"]["All Year"]
        assert season["tou_periods"][EXPORT_WINDOW_PERIOD] == {"periods": [_p(13, 14)]}
        assert season["tou_periods"]["PARTIAL_PEAK"] == {
            "periods": [_p(5, 13, 30), _p(14, 16), _p(19, 23, 0, 30)]
        }
        assert tariff["energy_charges"]["All Year"]["rates"][EXPORT_WINDOW_PERIOD] == 2.0
        sell_rates = tariff["sell_tariff"]["energy_charges"]["All Year"]["rates"]
        assert sell_rates[EXPORT_WINDOW_PERIOD] == 2.0
        sell_season = tariff["sell_tariff"]["seasons"]["All Year"]
        assert sell_season["tou_periods"][EXPORT_WINDOW_PERIOD] == {"periods": [_p(13, 14)]}

    @pytest.mark.parametrize(
        ("start", "end"),
        [(13 * 60, 14 * 60), (2 * 60, 3 * 60 + 30), (22 * 60 + 30, 60), (16 * 60, 19 * 60)],
    )
    def test_day_still_covered_once(self, start, end):
        tariff = with_export_window(to_v2(_v1_tariff()), start, end, 2.0)
        for part in (tariff, tariff["sell_tariff"]):
            assert sorted(_minutes(_all_periods(part["seasons"]["All Year"]))) == list(range(1440))

    def test_swallowed_period_loses_its_rates(self):
        tariff = with_export_window(to_v2(_v1_tariff()), 16 * 60, 19 * 60, 2.0)
        assert "ON_PEAK" not in tariff["seasons"]["All Year"]["tou_periods"]
        assert "ON_PEAK" not in tariff["energy_charges"]["All Year"]["rates"]
        assert "ON_PEAK" not in tariff["sell_tariff"]["energy_charges"]["All Year"]["rates"]

    def test_sell_tariff_without_seasons_follows_buy_periods(self):
        v2 = to_v2(_v1_tariff())
        del v2["sell_tariff"]["seasons"]
        tariff = with_export_window(v2, 16 * 60, 19 * 60, 2.0)
        assert tariff["sell_tariff"]["energy_charges"]["All Year"]["rates"] == {
            "OFF_PEAK": 0.15,
            "PARTIAL_PEAK": 0.15,
            EXPORT_WINDOW_PERIOD: 2.0,
        }

    def test_weekday_periods_kept_per_day_range(self):
        v2 = to_v2(_v1_tariff())
        v2["seasons"]["All Year"]["tou_periods"]["ON_PEAK"] = {
            "periods": [_p(16, 19, days=(0, 4)), _p(16, 19, days=(5, 6))]
        }
        tariff = with_export_window(v2, 17 * 60, 18 * 60, 2.0)
        assert tariff["seasons"]["All Year"]["tou_periods"]["ON_PEAK"] == {
            "periods": [
                _p(16, 17, days=(0, 4)),
                _p(18, 19, days=(0, 4)),
                _p(16, 17, days=(5, 6)),
                _p(18, 19, days=(5, 6)),
            ]
        }

    def test_original_untouched(self):
        v2 = to_v2(_v1_tariff())
        with_export_window(v2, 13 * 60, 14 * 60, 2.0)
        assert v2 == to_v2(_v1_tariff())

    def test_needs_seasons(self):
        v2 = to_v2(_v1_tariff())
        v2["seasons"] = {}
        with pytest.raises(TariffError):
            with_export_window(v2, 600, 660, 2.0)

    def test_needs_sell_tariff(self):
        v2 = to_v2(_v1_tariff())
        del v2["sell_tariff"]
        with pytest.raises(TariffError):
            with_export_window(v2, 600, 660, 2.0)

    def test_needs_periods_in_every_season(self):
        v2 = to_v2(_v1_tariff())
        v2["seasons"]["All Year"]["tou_periods"] = {}
        with pytest.raises(TariffError):
            with_export_window(v2, 600, 660, 2.0)


class TestReadWrite:
    def _cloud_pw(self, site_info):
        pw = MagicMock()
        pw.client = MagicMock(spec=["get_site_config"])
        pw.client.get_site_config.return_value = {"response": site_info}
        return pw

    def test_reads_cloud_site_info(self):
        pw = self._cloud_pw(
            {
                "id": "1232100-00-E--TG1",
                "installation_time_zone": "Europe/London",
                "tariff_content": _v1_tariff(),
                "tou_settings": {"optimization_strategy": "balanced"},
            }
        )
        site = read_site_tariff(pw)
        assert site.tariff == to_v2(_v1_tariff())
        assert site.optimization_strategy == "balanced"
        assert site.timezone == "Europe/London"
        assert site.din == "1232100-00-E--TG1"
        pw.client.get_site_config.assert_called_once_with(force=True)

    def test_prefers_v2(self):
        v2 = to_v2(_v1_tariff())
        v2["name"] = "v2"
        pw = self._cloud_pw({"tariff_content": _v1_tariff(), "tariff_content_v2": v2})
        site = read_site_tariff(pw)
        assert site.tariff["name"] == "v2"
        assert site.optimization_strategy == DEFAULT_OPTIMIZATION_STRATEGY

    def test_reads_fleetapi_site_info(self):
        pw = MagicMock()
        pw.client = MagicMock(spec=["fleet"])
        pw.client.fleet.get_site_info.return_value = {"tariff_content_v2": to_v2(_v1_tariff())}
        assert read_site_tariff(pw).tariff == to_v2(_v1_tariff())

    def test_falls_back_to_get_tariff(self):
        pw = self._cloud_pw({})
        pw.get_tariff.return_value = _v1_tariff()
        assert read_site_tariff(pw).tariff == to_v2(_v1_tariff())
        pw.get_tariff.assert_called_once_with(force=True)

    def test_no_tariff(self):
        pw = self._cloud_pw({})
        pw.get_tariff.return_value = None
        with pytest.raises(TariffError):
            read_site_tariff(pw)

    def test_write_sends_v2(self):
        pw = MagicMock()
        pw.set_tariff.return_value = {"Message": "Updated", "Code": 201}
        write_site_tariff(pw, {"name": "x"}, "economics")
        pw.set_tariff.assert_called_once_with(
            {"optimization_strategy": "economics", "tariff_content_v2": {"name": "x"}}
        )

    @pytest.mark.parametrize("result", [None, {}, {"ERROR": "Unknown API"}])
    def test_write_failure(self, result):
        pw = MagicMock()
        pw.set_tariff.return_value = result
        with pytest.raises(TariffError):
            write_site_tariff(pw, {"name": "x"}, "economics")
