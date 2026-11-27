"""Tests for the tariff manager and its export window actions."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from conftest import DIN, make_fake_pw
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pypowerwall.const import (
    CONF_AUTHPATH,
    CONF_CONN_TYPE,
    CONF_GW_PWD,
    CONF_RSA_KEY_PATH,
    CONF_TARIFF_EXPORT_RATES,
    CONF_TARIFF_IMPORT_RATES,
    CONN_TYPE_CLOUD,
    CONN_TYPE_TEDAPI,
    CONN_TYPE_TEDAPI_V1R,
    DOMAIN,
)
from custom_components.pypowerwall.tariff_manager import FIRST_SYNC_SECONDS, RETRY_SECONDS

CONNECT_TARGET = "custom_components.pypowerwall.coordinator.pypowerwall.Powerwall"
LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=LONDON)
IMPORT = "event.octopus_import_current_day_rates"
EXPORT = "event.octopus_export_current_day_rates"
OPTIONS = {CONF_TARIFF_IMPORT_RATES: IMPORT, CONF_TARIFF_EXPORT_RATES: EXPORT}
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
UPDATED = {"Message": "Updated", "Code": 201}


def _rates(day: date, bands):
    slot = datetime.combine(day, datetime.min.time(), LONDON)
    rates = []
    while slot.date() == day:
        hour = slot.hour + slot.minute / 60
        price = next(p for first, last, p in bands if first <= hour < last)
        end = slot + timedelta(minutes=30)
        rates.append({"start": slot.isoformat(), "end": end.isoformat(), "value_inc_vat": price})
        slot = end
    return rates


def _set_rates(hass: HomeAssistant, import_bands=FLUX_IMPORT, export_bands=FLUX_EXPORT) -> None:
    day = NOW.date()
    hass.states.async_set(IMPORT, "2026-11-27T00:00:00", {"rates": _rates(day, import_bands)})
    hass.states.async_set(EXPORT, "2026-11-27T00:00:00", {"rates": _rates(day, export_bands)})


def _site(pw: MagicMock, din: str = DIN) -> MagicMock:
    pw.client = MagicMock(spec=["fleet"])
    pw.client.fleet.get_site_info.return_value = {
        "id": din,
        "installation_time_zone": "Europe/London",
    }
    pw.set_tariff.return_value = UPDATED
    return pw


@pytest.fixture
async def london(hass: HomeAssistant, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(NOW)
    return freezer


def _cloud_data(tmp_path) -> dict:
    (tmp_path / ".pypowerwall.auth").write_text('{"me@example.com": {}}')
    return {CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: str(tmp_path)}


def _v1r_data(authpath) -> dict:
    return {
        CONF_CONN_TYPE: CONN_TYPE_TEDAPI_V1R,
        "host": "192.168.91.1",
        CONF_GW_PWD: "secret",
        CONF_RSA_KEY_PATH: "/key.pem",
        CONF_AUTHPATH: str(authpath),
    }


async def _setup(hass: HomeAssistant, pw, data, options=OPTIONS) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data, options=options)
    entry.add_to_hass(hass)
    with patch(CONNECT_TARGET, return_value=pw):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _advance(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _sent(pw: MagicMock) -> dict:
    return pw.set_tariff.call_args.args[0]["tariff_content_v2"]


async def test_first_sync_writes_all_year_flux_tariff(
    hass: HomeAssistant, tmp_path, london
) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    pw.set_tariff.assert_not_called()

    await _advance(hass, london, FIRST_SYNC_SECONDS + 1)

    pw.set_tariff.assert_called_once()
    assert pw.set_tariff.call_args.args[0]["optimization_strategy"] == "economics"
    tariff = _sent(pw)
    assert list(tariff["seasons"]) == ["All year"]
    assert tariff["energy_charges"]["All year"]["rates"]["hour_16_minute_0"] == 0.39164
    sell_rates = tariff["sell_tariff"]["energy_charges"]["All year"]["rates"]
    assert sell_rates["hour_2_minute_0"] == 0.04943


async def test_unchanged_rates_are_not_rewritten(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    await _advance(hass, london, FIRST_SYNC_SECONDS + 1)

    hass.states.async_set(IMPORT, "2026-11-27T00:00:01", hass.states.get(IMPORT).attributes)
    await _advance(hass, london, 31)
    assert pw.set_tariff.call_count == 1

    _set_rates(hass, import_bands=[(0, 24, 0.3)])
    await _advance(hass, london, 31)
    assert pw.set_tariff.call_count == 2
    assert _sent(pw)["energy_charges"]["All year"]["rates"] == {"hour_0_minute_0": 0.3}


async def test_export_window_gets_dated_season_and_drops_out_next_day(
    hass: HomeAssistant, tmp_path, london
) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))

    await hass.services.async_call(
        DOMAIN,
        "set_export_window",
        {"start": "2026-11-27 13:00:00", "end": "2026-11-27 14:00:00", "price": 3.0},
        blocking=True,
    )
    tariff = _sent(pw)
    assert set(tariff["seasons"]) == {"Export 2026-11-27", "Normal 1"}
    event = tariff["seasons"]["Export 2026-11-27"]
    assert event["tou_periods"]["export_window_1"]["periods"][0]["fromHour"] == 13
    assert tariff["energy_charges"]["Export 2026-11-27"]["rates"]["export_window_1"] == 3.0
    assert tariff["sell_tariff"]["energy_charges"]["Export 2026-11-27"]["rates"][
        "export_window_1"
    ] == 3.0

    london.move_to(datetime(2026, 11, 28, 0, 5, 1, tzinfo=LONDON))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _advance(hass, london, 1)
    assert list(_sent(pw)["seasons"]) == ["All year"]


async def test_clear_export_windows(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    await hass.services.async_call(
        DOMAIN, "set_export_window", {"end": "2026-11-27 14:00:00"}, blocking=True
    )

    await hass.services.async_call(DOMAIN, "clear_export_windows", {}, blocking=True)
    assert list(_sent(pw)["seasons"]) == ["All year"]


@pytest.mark.parametrize(
    ("start", "end", "match"),
    [
        (None, "2026-11-27 11:00:00", "future"),
        ("2026-11-27 13:00:00", "2026-11-28 14:00:00", "24 hours"),
        ("2026-12-10 13:00:00", "2026-12-10 14:00:00", "7 days"),
    ],
)
async def test_window_validation(hass: HomeAssistant, tmp_path, london, start, end, match) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    data = {"end": end, **({"start": start} if start else {})}

    with pytest.raises(HomeAssistantError, match=match):
        await hass.services.async_call(DOMAIN, "set_export_window", data, blocking=True)
    pw.set_tariff.assert_not_called()


async def test_wrong_site_is_refused(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw(), din="9999999-00-E--TG987654321XYZ")
    await _setup(hass, pw, _cloud_data(tmp_path))

    with pytest.raises(HomeAssistantError, match="not this entry"):
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)
    pw.set_tariff.assert_not_called()


async def test_missing_rates_write_nothing(hass: HomeAssistant, tmp_path, london) -> None:
    pw = _site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))

    with pytest.raises(HomeAssistantError, match="no rates"):
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)
    pw.set_tariff.assert_not_called()


async def test_failed_write_is_retried(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    pw = _site(make_fake_pw())
    pw.set_tariff.return_value = None
    await _setup(hass, pw, _cloud_data(tmp_path))
    await _advance(hass, london, FIRST_SYNC_SECONDS + 1)
    assert pw.set_tariff.call_count == 1

    pw.set_tariff.return_value = UPDATED
    await _advance(hass, london, RETRY_SECONDS + 1)
    assert pw.set_tariff.call_count == 2


async def test_v1r_entry_prefers_fleetapi_sign_in(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    (tmp_path / ".pypowerwall.fleetapi").write_text("{}")
    (tmp_path / ".pypowerwall.auth").write_text('{"me@example.com": {}}')
    lan_pw = make_fake_pw()
    await _setup(hass, lan_pw, _v1r_data(tmp_path))
    cloud_pw = _site(make_fake_pw())

    with patch(CONNECT_TARGET, return_value=cloud_pw) as connect:
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)

    assert connect.call_args.kwargs["fleetapi"] is True
    assert connect.call_args.kwargs["authpath"] == str(tmp_path)
    cloud_pw.set_tariff.assert_called_once()
    lan_pw.set_tariff.assert_not_called()


async def test_v1r_entry_without_sign_in_explains(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    await _setup(hass, make_fake_pw(), _v1r_data(tmp_path))

    with pytest.raises(HomeAssistantError, match="Fleet API"):
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)


async def test_failed_sign_in_raises_and_clears_repair(
    hass: HomeAssistant, tmp_path, london
) -> None:
    _set_rates(hass)
    (tmp_path / ".pypowerwall.fleetapi").write_text("{}")
    entry = await _setup(hass, make_fake_pw(), _v1r_data(tmp_path))
    issue_id = f"tariff_sign_in_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    with (
        patch(CONNECT_TARGET, return_value=make_fake_pw(connected=False)),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    with patch(CONNECT_TARGET, return_value=_site(make_fake_pw())):
        await hass.services.async_call(DOMAIN, "update_tariff", {}, blocking=True)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_off_without_rate_options(hass: HomeAssistant, tmp_path, london) -> None:
    pw = _site(make_fake_pw())
    entry = await _setup(hass, pw, _cloud_data(tmp_path), options={})
    assert not hass.services.has_service(DOMAIN, "set_export_window")

    await _advance(hass, london, FIRST_SYNC_SECONDS + 1)
    pw.set_tariff.assert_not_called()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_removed_on_unload(hass: HomeAssistant, tmp_path, london) -> None:
    _set_rates(hass)
    entry = await _setup(hass, _site(make_fake_pw()), _cloud_data(tmp_path))
    assert hass.services.has_service(DOMAIN, "set_export_window")
    assert hass.services.has_service(DOMAIN, "clear_export_windows")
    assert hass.services.has_service(DOMAIN, "update_tariff")

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "set_export_window")


async def test_options_offer_rate_entities_only_where_tariff_writes_work(
    hass: HomeAssistant, tmp_path
) -> None:
    tedapi = MockConfigEntry(
        domain=DOMAIN,
        unique_id="A",
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, "host": "192.168.91.1", CONF_GW_PWD: "pw"},
    )
    tedapi.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(tedapi.entry_id)
    assert CONF_TARIFF_IMPORT_RATES not in result["data_schema"].schema

    v1r = MockConfigEntry(domain=DOMAIN, unique_id="B", data=_v1r_data(tmp_path))
    v1r.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(v1r.entry_id)
    assert CONF_TARIFF_IMPORT_RATES in result["data_schema"].schema
    assert CONF_TARIFF_EXPORT_RATES in result["data_schema"].schema
