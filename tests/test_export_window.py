"""Tests for the set_export_window / restore_tariff services."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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
    CONN_TYPE_CLOUD,
    CONN_TYPE_TEDAPI,
    CONN_TYPE_TEDAPI_V1R,
    DOMAIN,
)
from custom_components.pypowerwall.tariff import EXPORT_WINDOW_PERIOD, to_v2
from custom_components.pypowerwall.tariff_control import (
    RESTORE_RETRY_SECONDS,
    SIGN_IN_CHECK_INTERVAL,
    SIGN_IN_FIRST_CHECK_SECONDS,
)

CONNECT_TARGET = "custom_components.pypowerwall.coordinator.pypowerwall.Powerwall"
NOW = datetime.fromisoformat("2026-11-28T12:00:00+00:00")
START = NOW + timedelta(hours=1)
END = NOW + timedelta(hours=2)
UPDATED = {"Message": "Updated", "Code": 201}


def _tariff():
    periods = {
        "OFF_PEAK": [
            {
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 0,
                "fromMinute": 0,
                "toHour": 16,
                "toMinute": 0,
            }
        ],
        "ON_PEAK": [
            {
                "fromDayOfWeek": 0,
                "toDayOfWeek": 6,
                "fromHour": 16,
                "fromMinute": 0,
                "toHour": 0,
                "toMinute": 0,
            }
        ],
    }
    season = {"fromMonth": 1, "toMonth": 12, "fromDay": 1, "toDay": 31, "tou_periods": periods}
    return {
        "name": "Octopus",
        "currency": "GBP",
        "energy_charges": {"ALL": {"ALL": 0}, "All Year": {"OFF_PEAK": 0.25, "ON_PEAK": 0.35}},
        "seasons": {"All Year": season},
        "sell_tariff": {
            "energy_charges": {"ALL": {"ALL": 0}, "All Year": {"OFF_PEAK": 0.15, "ON_PEAK": 0.15}},
        },
    }


def _with_site(pw: MagicMock, din: str = DIN) -> MagicMock:
    pw.client = MagicMock(spec=["get_site_config"])
    pw.client.get_site_config.return_value = {
        "response": {
            "id": din,
            "installation_time_zone": "Europe/London",
            "tariff_content": _tariff(),
            "tou_settings": {"optimization_strategy": "economics"},
        }
    }
    pw.set_tariff.return_value = UPDATED
    return pw


async def _setup(hass: HomeAssistant, pw, data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data)
    entry.add_to_hass(hass)
    with patch(CONNECT_TARGET, return_value=pw):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _cloud_data(tmp_path) -> dict:
    (tmp_path / ".pypowerwall.auth").write_text('{"me@example.com": {}}')
    return {CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: str(tmp_path)}


def _v1r_data(authpath=None) -> dict:
    data = {
        CONF_CONN_TYPE: CONN_TYPE_TEDAPI_V1R,
        "host": "192.168.91.1",
        CONF_GW_PWD: "secret",
        CONF_RSA_KEY_PATH: "/key.pem",
    }
    if authpath is not None:
        data[CONF_AUTHPATH] = str(authpath)
    return data


async def _set_window(hass: HomeAssistant, **extra) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_export_window",
        {"start": START.isoformat(), "end": END.isoformat(), **extra},
        blocking=True,
    )
    await hass.async_block_till_done()


def _sent(pw: MagicMock, call: int = -1) -> dict:
    return pw.set_tariff.call_args_list[call].args[0]


@pytest.fixture
def frozen(freezer):
    freezer.move_to(NOW)
    return freezer


async def test_cloud_window_written_then_restored_at_end(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    pw = _with_site(make_fake_pw())
    entry = await _setup(hass, pw, _cloud_data(tmp_path))

    await _set_window(hass, price=3.5)

    sent = _sent(pw)
    assert sent["optimization_strategy"] == "economics"
    season = sent["tariff_content_v2"]["seasons"]["All Year"]
    assert season["tou_periods"][EXPORT_WINDOW_PERIOD]["periods"] == [
        {
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
            "fromHour": 13,
            "fromMinute": 0,
            "toHour": 14,
            "toMinute": 0,
        }
    ]
    assert sent["tariff_content_v2"]["energy_charges"]["All Year"]["rates"][
        EXPORT_WINDOW_PERIOD
    ] == 3.5
    controller = hass.data[DOMAIN]["tariff_controllers"][entry.entry_id]
    assert controller.backup["tariff"] == to_v2(_tariff())

    frozen.move_to(END + timedelta(seconds=1))
    async_fire_time_changed(hass, END + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert pw.set_tariff.call_count == 2
    assert _sent(pw)["tariff_content_v2"] == to_v2(_tariff())
    assert controller.backup is None


async def test_restore_tariff_service_ends_window_early(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    pw = _with_site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    await _set_window(hass)

    await hass.services.async_call(DOMAIN, "restore_tariff", {}, blocking=True)
    await hass.async_block_till_done()
    assert _sent(pw)["tariff_content_v2"] == to_v2(_tariff())

    frozen.move_to(END + timedelta(seconds=1))
    async_fire_time_changed(hass, END + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert pw.set_tariff.call_count == 2


async def test_second_window_builds_on_the_saved_original(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    pw = _with_site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    await _set_window(hass)
    pw.client.get_site_config.return_value["response"]["tariff_content"] = {"name": "changed"}

    await hass.services.async_call(
        DOMAIN,
        "set_export_window",
        {"start": START.isoformat(), "end": (END + timedelta(hours=1)).isoformat()},
        blocking=True,
    )
    await hass.async_block_till_done()

    periods = _sent(pw)["tariff_content_v2"]["seasons"]["All Year"]["tou_periods"]
    assert periods[EXPORT_WINDOW_PERIOD]["periods"][0]["toHour"] == 15


async def test_saved_tariff_restored_after_restart(
    hass: HomeAssistant, tmp_path, frozen, hass_storage
) -> None:
    pw = _with_site(make_fake_pw())
    data = _cloud_data(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DIN, data=data)
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.tariff_backup.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.tariff_backup.{entry.entry_id}",
        "data": {
            "tariff": to_v2(_tariff()),
            "optimization_strategy": "economics",
            "timezone": "Europe/London",
            "end": (NOW - timedelta(minutes=5)).isoformat(),
        },
    }
    with patch(CONNECT_TARGET, return_value=pw):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    async_fire_time_changed(hass, NOW + timedelta(seconds=1))
    await hass.async_block_till_done()

    pw.set_tariff.assert_called_once()
    assert _sent(pw)["tariff_content_v2"] == to_v2(_tariff())


async def test_failed_restore_is_retried(hass: HomeAssistant, tmp_path, frozen) -> None:
    pw = _with_site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))
    await _set_window(hass)

    pw.set_tariff.return_value = None
    frozen.move_to(END + timedelta(seconds=1))
    async_fire_time_changed(hass, END + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert pw.set_tariff.call_count == 2

    pw.set_tariff.return_value = UPDATED
    later = END + timedelta(seconds=RESTORE_RETRY_SECONDS + 2)
    frozen.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert pw.set_tariff.call_count == 3
    assert _sent(pw)["tariff_content_v2"] == to_v2(_tariff())


async def test_window_must_end_in_the_future(hass: HomeAssistant, tmp_path, frozen) -> None:
    pw = _with_site(make_fake_pw())
    await _setup(hass, pw, _cloud_data(tmp_path))

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "set_export_window",
            {"end": (NOW - timedelta(minutes=1)).isoformat()},
            blocking=True,
        )
    pw.set_tariff.assert_not_called()


async def test_wrong_site_is_refused(hass: HomeAssistant, tmp_path, frozen) -> None:
    pw = _with_site(make_fake_pw(), din="9999999-00-E--TG987654321XYZ")
    await _setup(hass, pw, _cloud_data(tmp_path))

    with pytest.raises(HomeAssistantError, match="not this entry"):
        await _set_window(hass)
    pw.set_tariff.assert_not_called()


async def test_v1r_entry_uses_cloud_sign_in(hass: HomeAssistant, tmp_path, frozen) -> None:
    lan_pw = make_fake_pw()
    await _setup(hass, lan_pw, _v1r_data(_cloud_data(tmp_path)[CONF_AUTHPATH]))
    cloud_pw = _with_site(make_fake_pw())

    with patch(CONNECT_TARGET, return_value=cloud_pw) as connect:
        await _set_window(hass)

    assert connect.call_args.kwargs["cloudmode"] is True
    assert connect.call_args.kwargs["authpath"] == str(tmp_path)
    assert connect.call_args.kwargs["email"] == "me@example.com"
    cloud_pw.set_tariff.assert_called_once()
    lan_pw.set_tariff.assert_not_called()


async def test_v1r_entry_without_sign_in_explains(hass: HomeAssistant, tmp_path, frozen) -> None:
    await _setup(hass, make_fake_pw(), _v1r_data(tmp_path))

    with pytest.raises(HomeAssistantError, match="Tesla sign-in"):
        await _set_window(hass)


async def test_services_only_for_tariff_capable_entries(hass: HomeAssistant, tmp_path) -> None:
    entry = await _setup(
        hass,
        make_fake_pw(),
        {CONF_CONN_TYPE: CONN_TYPE_TEDAPI, "host": "192.168.91.1", CONF_GW_PWD: "secret"},
    )
    assert not hass.services.has_service(DOMAIN, "set_export_window")
    await hass.config_entries.async_unload(entry.entry_id)

    entry = await _setup(hass, _with_site(make_fake_pw()), _cloud_data(tmp_path))
    assert hass.services.has_service(DOMAIN, "set_export_window")
    assert hass.services.has_service(DOMAIN, "restore_tariff")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "set_export_window")


async def test_sign_in_check_raises_and_clears_repair(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    entry = await _setup(hass, make_fake_pw(), _v1r_data(_cloud_data(tmp_path)[CONF_AUTHPATH]))
    issue_id = f"tesla_sign_in_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    with patch(CONNECT_TARGET, return_value=make_fake_pw(connected=False)) as connect:
        frozen.tick(SIGN_IN_FIRST_CHECK_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert connect.call_args.kwargs["cloudmode"] is True
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    with patch(CONNECT_TARGET, return_value=_with_site(make_fake_pw())):
        frozen.tick(SIGN_IN_CHECK_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_sign_in_check_skipped_without_sign_in(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    entry = await _setup(hass, make_fake_pw(), _v1r_data(tmp_path))

    with patch(CONNECT_TARGET) as connect:
        frozen.tick(SIGN_IN_FIRST_CHECK_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    connect.assert_not_called()
    issue_id = f"tesla_sign_in_failed_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_cloud_entry_has_no_separate_sign_in_check(
    hass: HomeAssistant, tmp_path, frozen
) -> None:
    await _setup(hass, _with_site(make_fake_pw()), _cloud_data(tmp_path))

    with patch(CONNECT_TARGET) as connect:
        frozen.tick(SIGN_IN_FIRST_CHECK_SECONDS + 1)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    connect.assert_not_called()
