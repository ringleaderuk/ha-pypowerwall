from unittest.mock import MagicMock, patch

from conftest import DIN, make_fake_pw
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pypowerwall.const import (
    CONF_CONN_TYPE,
    CONF_GW_PWD,
    CONN_TYPE_TEDAPI,
    DOMAIN,
)
from custom_components.pypowerwall.diagnostics import async_get_config_entry_diagnostics

CONNECT_TARGET = "custom_components.pypowerwall.coordinator.pypowerwall.Powerwall"


async def test_diagnostics_redacts_and_includes_raw_payloads(hass: HomeAssistant) -> None:
    pw = make_fake_pw(
        system_status={
            "nominal_full_pack_energy": 13000,
            "battery_blocks": [{"PackageSerialNumber": "SN1"}],
        }
    )
    pw.client = MagicMock()
    pw.client.get_battery.return_value = {
        "response": {"total_pack_energy": 13000, "gateway_id": "GW1", "site_name": "Home"}
    }
    pw.client.get_site_power.return_value = {"response": {"energy_left": 6500}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"},
    )
    entry.add_to_hass(hass)
    with patch(CONNECT_TARGET, return_value=pw):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_GW_PWD] == "**REDACTED**"
    assert diag["coordinator_data"]["battery_full_pack_energy"] == 13.0
    assert diag["coordinator_data"]["din"] == "**REDACTED**"
    raw = diag["raw"]
    assert raw["system_status"]["nominal_full_pack_energy"] == 13000
    assert raw["system_status"]["battery_blocks"][0]["PackageSerialNumber"] == "**REDACTED**"
    assert raw["cloud_get_battery"]["response"]["total_pack_energy"] == 13000
    assert raw["cloud_get_battery"]["response"]["gateway_id"] == "**REDACTED**"
    assert raw["cloud_get_site_power"]["response"]["energy_left"] == 6500
    pw.client.get_battery.assert_called_with(force=True)


async def test_diagnostics_survives_errors(hass: HomeAssistant) -> None:
    pw = make_fake_pw()
    pw.client = MagicMock(spec=["get_battery"])
    pw.client.get_battery.side_effect = RuntimeError("boom")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"},
    )
    entry.add_to_hass(hass)
    with patch(CONNECT_TARGET, return_value=pw):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        pw.system_status.side_effect = RuntimeError("nope")

        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["raw"]["system_status"].startswith("error:")
    assert diag["raw"]["cloud_get_battery"].startswith("error:")
    assert "cloud_get_site_power" not in diag["raw"]
