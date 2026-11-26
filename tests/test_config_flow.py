import os
from unittest.mock import patch

import pytest
from conftest import DIN, make_fake_pw
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pypowerwall.const import (
    AUTH_CACHE_FILENAMES,
    CONF_AUTHPATH,
    CONF_CONN_TYPE,
    CONF_GW_PWD,
    CONF_RSA_KEY_PATH,
    CONN_TYPE_CLOUD,
    CONN_TYPE_FLEETAPI,
    CONN_TYPE_HYBRID,
    CONN_TYPE_LOCAL,
    CONN_TYPE_TEDAPI,
    CONN_TYPE_TEDAPI_V1R,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_CLOUD,
    DOMAIN,
)

# The flow's own validation call and the coordinator's connection call (triggered by
# the automatic setup that follows a successful create_entry) both resolve through
# the same shared `pypowerwall` module attribute, so one patch target covers both.
CONNECT_TARGET = "custom_components.pypowerwall.config_flow.pypowerwall.Powerwall"


async def _start_menu(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _select_menu(hass: HomeAssistant, result, conn_type: str):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": conn_type}
    )


async def test_menu_shown(hass: HomeAssistant) -> None:
    result = await _start_menu(hass)

    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {
        CONN_TYPE_TEDAPI,
        CONN_TYPE_HYBRID,
        CONN_TYPE_LOCAL,
        "cloud_login",
        CONN_TYPE_CLOUD,
        CONN_TYPE_FLEETAPI,
        CONN_TYPE_TEDAPI_V1R,
    }


async def test_tedapi_flow_success(hass: HomeAssistant) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI)
        assert result["type"] == "form"
        assert result["step_id"] == CONN_TYPE_TEDAPI

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"}
        )

    assert result2["type"] == "create_entry"
    assert result2["title"] == "192.168.91.1"
    assert result2["data"] == {
        CONF_CONN_TYPE: CONN_TYPE_TEDAPI,
        CONF_HOST: "192.168.91.1",
        CONF_GW_PWD: "secret",
    }


async def test_tedapi_flow_cannot_connect(hass: HomeAssistant) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw(connected=False)):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_GW_PWD: "wrong"}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_tedapi_flow_missing_din_is_cannot_connect(hass: HomeAssistant) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw(din=None)):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_tedapi_flow_duplicate_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"},
    ).add_to_hass(hass)

    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "already_configured"


async def test_hybrid_flow_success(hass: HomeAssistant) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_HYBRID)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.91.1",
                CONF_EMAIL: "owner@example.com",
                CONF_PASSWORD: "customerpw",
                CONF_GW_PWD: "secret",
            },
        )

    assert result2["type"] == "create_entry"
    assert result2["data"] == {
        CONF_CONN_TYPE: CONN_TYPE_HYBRID,
        CONF_HOST: "192.168.91.1",
        CONF_EMAIL: "owner@example.com",
        CONF_PASSWORD: "customerpw",
        CONF_GW_PWD: "secret",
    }


async def test_local_flow_success(hass: HomeAssistant) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_LOCAL)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.91.1",
                CONF_EMAIL: "owner@example.com",
                CONF_PASSWORD: "customerpw",
            },
        )

    assert result2["type"] == "create_entry"
    assert result2["data"][CONF_CONN_TYPE] == CONN_TYPE_LOCAL


def _auth_dir(tmp_path, conn_type: str):
    (tmp_path / AUTH_CACHE_FILENAMES[conn_type]).write_text("{}")
    return str(tmp_path)


async def test_cloud_flow_success(hass: HomeAssistant, tmp_path) -> None:
    authpath = _auth_dir(tmp_path, CONN_TYPE_CLOUD)
    with patch(CONNECT_TARGET, return_value=make_fake_pw(site_name="Cloud Site")):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_CLOUD)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTHPATH: authpath}
        )

    assert result2["type"] == "create_entry"
    # No host in cloud-mode data, so the title falls back to the site name pypowerwall reports.
    assert result2["title"] == "Cloud Site"
    assert result2["data"] == {
        CONF_CONN_TYPE: CONN_TYPE_CLOUD,
        CONF_AUTHPATH: authpath,
    }


async def test_fleetapi_flow_success(hass: HomeAssistant, tmp_path) -> None:
    authpath = _auth_dir(tmp_path, CONN_TYPE_FLEETAPI)
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_FLEETAPI)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTHPATH: authpath}
        )

    assert result2["type"] == "create_entry"
    assert result2["data"][CONF_CONN_TYPE] == CONN_TYPE_FLEETAPI


@pytest.mark.parametrize("conn_type", [CONN_TYPE_CLOUD, CONN_TYPE_FLEETAPI])
@pytest.mark.parametrize("exists", [False, True])
async def test_file_based_flow_rejects_a_missing_auth_file(
    hass: HomeAssistant, tmp_path, conn_type: str, exists: bool
) -> None:
    """A missing directory or a directory without the file never reaches pypowerwall."""
    authpath = str(tmp_path if exists else tmp_path / "nope")
    with patch(CONNECT_TARGET) as powerwall:
        result = await _select_menu(hass, await _start_menu(hass), conn_type)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTHPATH: authpath}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "auth_cache_not_found"}
    powerwall.assert_not_called()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_cloud_flow_rejects_a_read_only_auth_directory(
    hass: HomeAssistant, tmp_path
) -> None:
    authpath = _auth_dir(tmp_path, CONN_TYPE_CLOUD)
    tmp_path.chmod(0o500)
    try:
        with patch(CONNECT_TARGET) as powerwall:
            result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_CLOUD)
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_AUTHPATH: authpath}
            )
    finally:
        tmp_path.chmod(0o700)

    assert result2["errors"] == {"base": "auth_cache_not_writable"}
    powerwall.assert_not_called()


async def test_reconfigure_cloud_rejects_a_missing_auth_file(
    hass: HomeAssistant, tmp_path
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: "/old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == "menu"
    # The menu reuses the reconfigure description, which needs {conn_type}.
    assert result["description_placeholders"] == {"conn_type": CONN_TYPE_CLOUD}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_settings"}
    )
    assert result["type"] == "form"
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AUTHPATH: str(tmp_path)}
    )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "auth_cache_not_found"}


EXCHANGE_TARGET = "custom_components.pypowerwall.config_flow.exchange_code"


def _fake_tokens(email: str | None = "owner@example.com") -> dict:
    import base64
    import json

    claims = {"email": email} if email else {}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": f"h.{payload}.s",
        "expires_in": 28800,
    }


def _callback_for(result, scheme_prefix="https://auth.tesla.com/void/callback") -> str:
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(result["description_placeholders"]["login_url"]).query)["state"][0]
    return f"{scheme_prefix}?code=abc123&state={state}&issuer=x"


async def _start_cloud_login(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    result = await _select_menu(hass, await _start_menu(hass), "cloud_login")
    assert result["type"] == "form"
    assert result["step_id"] == "cloud_login"
    assert result["description_placeholders"]["login_url"].startswith(
        "https://auth.tesla.com/oauth2/v3/authorize?"
    )
    return result


async def test_cloud_login_flow_success(hass: HomeAssistant, tmp_path) -> None:
    import json

    result = await _start_cloud_login(hass, tmp_path)
    with (
        patch(EXCHANGE_TARGET, return_value=_fake_tokens()) as exchange,
        patch(CONNECT_TARGET, return_value=make_fake_pw(site_name="Cloud Site")) as powerwall,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    authpath = str(tmp_path / "pypowerwall")
    assert result2["type"] == "create_entry"
    assert result2["title"] == "Cloud Site"
    assert result2["data"] == {CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: authpath}
    assert exchange.call_args.args[0] == "abc123"
    assert exchange.call_args.args[2] == "https://auth.tesla.com/void/callback"
    saved = json.loads((tmp_path / "pypowerwall" / ".pypowerwall.auth").read_text())
    assert list(saved) == ["owner@example.com"]
    assert saved["owner@example.com"]["sso"]["access_token"] == "at"
    assert saved["owner@example.com"]["sso"]["refresh_token"] == "rt"
    assert powerwall.call_args.kwargs["email"] == "owner@example.com"
    assert powerwall.call_args.kwargs["cloudmode"] is True


async def test_cloud_login_app_redirect_uses_app_redirect_uri(
    hass: HomeAssistant, tmp_path
) -> None:
    result = await _start_cloud_login(hass, tmp_path)
    with (
        patch(EXCHANGE_TARGET, return_value=_fake_tokens()) as exchange,
        patch(CONNECT_TARGET, return_value=make_fake_pw()),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"callback_url": _callback_for(result, "tesla://auth/callback")},
        )

    assert result2["type"] == "create_entry"
    assert exchange.call_args.args[2] == "tesla://auth/callback"


@pytest.mark.parametrize(
    ("callback", "error"),
    [
        ("not a url", "invalid_callback"),
        ("https://auth.tesla.com/void/callback?code=abc&state=wrong", "state_mismatch"),
    ],
)
async def test_cloud_login_rejects_bad_callback(
    hass: HomeAssistant, tmp_path, callback: str, error: str
) -> None:
    result = await _start_cloud_login(hass, tmp_path)
    with patch(EXCHANGE_TARGET) as exchange:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": callback}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": error}
    exchange.assert_not_called()
    assert (
        result2["description_placeholders"]["login_url"]
        != result["description_placeholders"]["login_url"]
    )


async def test_cloud_login_token_exchange_failure(hass: HomeAssistant, tmp_path) -> None:
    from custom_components.pypowerwall.cloud_auth import TokenExchangeError

    result = await _start_cloud_login(hass, tmp_path)
    with patch(EXCHANGE_TARGET, side_effect=TokenExchangeError("HTTP 401")):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    assert result2["errors"] == {"base": "login_failed"}
    assert not (tmp_path / "pypowerwall").exists()


async def test_cloud_login_tokens_without_email(hass: HomeAssistant, tmp_path) -> None:
    result = await _start_cloud_login(hass, tmp_path)
    with patch(EXCHANGE_TARGET, return_value=_fake_tokens(email=None)):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    assert result2["errors"] == {"base": "login_failed"}


async def test_cloud_login_cannot_connect(hass: HomeAssistant, tmp_path) -> None:
    result = await _start_cloud_login(hass, tmp_path)
    with (
        patch(EXCHANGE_TARGET, return_value=_fake_tokens()),
        patch(CONNECT_TARGET, return_value=make_fake_pw(connected=False)),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    assert result2["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_cloud_sign_in_again(hass: HomeAssistant, tmp_path) -> None:
    import json

    authpath = tmp_path / "existing"
    authpath.mkdir()
    (authpath / ".pypowerwall.auth").write_text(json.dumps({"old@example.com": {}}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: str(authpath), "siteid": "123"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_cloud_login"}
    )
    assert result["step_id"] == "reconfigure_cloud_login"
    with (
        patch(EXCHANGE_TARGET, return_value=_fake_tokens()),
        patch(CONNECT_TARGET, return_value=make_fake_pw()) as powerwall,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_CONN_TYPE: CONN_TYPE_CLOUD,
        CONF_AUTHPATH: str(authpath),
        "siteid": "123",
    }
    saved = json.loads((authpath / ".pypowerwall.auth").read_text())
    assert list(saved) == ["owner@example.com"]
    assert powerwall.call_args.kwargs["siteid"] == "123"


async def test_reconfigure_cloud_sign_in_wrong_gateway(hass: HomeAssistant, tmp_path) -> None:
    authpath = tmp_path / "existing"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: str(authpath)},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_cloud_login"}
    )
    with (
        patch(EXCHANGE_TARGET, return_value=_fake_tokens()),
        patch(CONNECT_TARGET, return_value=make_fake_pw(din="OTHER-DIN")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": _callback_for(result)}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "wrong_gateway"


def _rsa_key(tmp_path) -> str:
    key = tmp_path / "tedapi_rsa_private.pem"
    key.write_text("key")
    return str(key)


async def test_tedapi_v1r_flow_success(hass: HomeAssistant, tmp_path) -> None:
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI_V1R)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.91.1",
                CONF_GW_PWD: "secret",
                CONF_RSA_KEY_PATH: _rsa_key(tmp_path),
            },
        )

    assert result2["type"] == "create_entry"
    assert result2["data"][CONF_CONN_TYPE] == CONN_TYPE_TEDAPI_V1R


async def _v1r_flow(hass: HomeAssistant, extra: dict, rsa_key_path: str):
    with patch(CONNECT_TARGET, return_value=make_fake_pw()) as powerwall:
        result = await _select_menu(hass, await _start_menu(hass), CONN_TYPE_TEDAPI_V1R)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "192.168.91.1",
                CONF_RSA_KEY_PATH: rsa_key_path,
                **extra,
            },
        )
    result2["powerwall"] = powerwall
    return result2


async def test_tedapi_v1r_flow_password_only_success(hass: HomeAssistant, tmp_path) -> None:
    result = await _v1r_flow(hass, {CONF_PASSWORD: "customer-pw"}, _rsa_key(tmp_path))

    assert result["type"] == "create_entry"
    assert CONF_GW_PWD not in result["data"]
    assert result["data"][CONF_PASSWORD] == "customer-pw"


async def test_tedapi_v1r_flow_both_passwords_success(hass: HomeAssistant, tmp_path) -> None:
    result = await _v1r_flow(
        hass, {CONF_GW_PWD: "secret", CONF_PASSWORD: "customer-pw"}, _rsa_key(tmp_path)
    )

    assert result["type"] == "create_entry"


async def test_tedapi_v1r_flow_requires_a_password(hass: HomeAssistant, tmp_path) -> None:
    result = await _v1r_flow(hass, {}, _rsa_key(tmp_path))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "missing_password"}


async def test_tedapi_v1r_flow_rejects_a_missing_rsa_key(hass: HomeAssistant, tmp_path) -> None:
    """A key path Home Assistant can't see never reaches pypowerwall."""
    result = await _v1r_flow(
        hass, {CONF_GW_PWD: "secret"}, str(tmp_path / "tedapi_rsa_private.pem")
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "rsa_key_not_found"}
    result["powerwall"].assert_not_called()


async def test_reconfigure_v1r_rejects_a_missing_rsa_key(hass: HomeAssistant, tmp_path) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={
            CONF_CONN_TYPE: CONN_TYPE_TEDAPI_V1R,
            CONF_HOST: "192.168.91.1",
            CONF_GW_PWD: "pw",
            CONF_RSA_KEY_PATH: "/key.pem",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "192.168.91.1",
            CONF_GW_PWD: "pw",
            CONF_RSA_KEY_PATH: str(tmp_path / "missing.pem"),
        },
    )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "rsa_key_not_found"}


async def test_reconfigure_v1r_requires_a_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={
            CONF_CONN_TYPE: CONN_TYPE_TEDAPI_V1R,
            CONF_HOST: "192.168.91.1",
            CONF_GW_PWD: "old-pw",
            CONF_RSA_KEY_PATH: "/key.pem",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_RSA_KEY_PATH: "/key.pem"}
    )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "missing_password"}


def _scan_interval_default(result) -> int:
    schema = result["data_schema"].schema
    (marker,) = (key for key in schema if key == "scan_interval")
    return marker.default()


async def test_options_flow_defaults_to_cloud_interval_for_cloud_entry(
    hass: HomeAssistant,
) -> None:
    """Cloud/FleetAPI entries should default the options form to the slower cloud interval."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-entry-din",
        data={CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: "/config/pypowerwall"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert _scan_interval_default(result) == DEFAULT_SCAN_INTERVAL_CLOUD


async def test_options_flow_defaults_to_lan_interval_for_tedapi_entry(
    hass: HomeAssistant,
) -> None:
    """Local/LAN entries should keep defaulting the options form to the fast interval."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert _scan_interval_default(result) == DEFAULT_SCAN_INTERVAL


async def test_options_flow_respects_explicit_scan_interval_override(
    hass: HomeAssistant,
) -> None:
    """An explicitly-set scan_interval option should win over the conn-type default."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="cloud-entry-din-2",
        data={CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: "/config/pypowerwall"},
        options={"scan_interval": 15},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _scan_interval_default(result) == 15


def _design_capacity_default(result) -> float:
    schema = result["data_schema"].schema
    (marker,) = (key for key in schema if key == "design_capacity_kwh")
    return marker.default()


async def test_options_flow_design_capacity_default_and_override(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "secret"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _design_capacity_default(result) == 13.5

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 5, "design_capacity_kwh": 14.0}
    )
    assert result["type"] == "create_entry"
    assert entry.options["design_capacity_kwh"] == 14.0


def _suggested(result, field: str):
    """The value pre-filled into a form field (HA stores it on the marker's description)."""
    schema = result["data_schema"].schema
    (marker,) = (key for key in schema if key == field)
    return (marker.description or {}).get("suggested_value")


async def _tedapi_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_TEDAPI, CONF_HOST: "192.168.91.1", CONF_GW_PWD: "old-pw"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_shows_current_values_for_its_conn_type(hass: HomeAssistant) -> None:
    """The form should be the entry's own conn-type schema, pre-filled with its values."""
    entry = await _tedapi_entry(hass)

    result = await entry.start_reconfigure_flow(hass)

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert set(result["data_schema"].schema) == {CONF_HOST, CONF_GW_PWD}
    assert _suggested(result, CONF_HOST) == "192.168.91.1"
    assert _suggested(result, CONF_GW_PWD) == "old-pw"


async def test_reconfigure_updates_entry_data(hass: HomeAssistant) -> None:
    """A corrected gateway password should be written back to the existing entry."""
    entry = await _tedapi_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT_TARGET, return_value=make_fake_pw()):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.1", CONF_GW_PWD: "correct-pw"}
        )
        await hass.async_block_till_done()

    assert result2["type"] == "abort"
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_CONN_TYPE: CONN_TYPE_TEDAPI,
        CONF_HOST: "192.168.91.1",
        CONF_GW_PWD: "correct-pw",
    }


async def test_reconfigure_rejects_a_different_gateway(hass: HomeAssistant) -> None:
    """Settings that reach a different DIN must not silently repoint the entry."""
    entry = await _tedapi_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT_TARGET, return_value=make_fake_pw(din="9999999-00-F--TGOTHERGATEWAY")):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.50", CONF_GW_PWD: "other-pw"}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "wrong_gateway"
    # The original settings must survive a rejected reconfigure.
    assert entry.data[CONF_HOST] == "192.168.91.1"
    assert entry.data[CONF_GW_PWD] == "old-pw"


async def test_reconfigure_reports_connection_failure_and_keeps_edits(
    hass: HomeAssistant,
) -> None:
    """A failed attempt should re-show the form with the error and the user's input."""
    entry = await _tedapi_entry(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT_TARGET, return_value=make_fake_pw(connected=False)):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.91.9", CONF_GW_PWD: "still-wrong"}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}
    assert _suggested(result2, CONF_HOST) == "192.168.91.9"
    assert entry.data[CONF_GW_PWD] == "old-pw"


async def test_reconfigure_uses_the_schema_of_a_file_based_conn_type(
    hass: HomeAssistant,
) -> None:
    """A FleetAPI entry should get the authpath form, not a TEDAPI one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DIN,
        data={CONF_CONN_TYPE: CONN_TYPE_FLEETAPI, CONF_AUTHPATH: "/config/wrong-dir"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    assert result["step_id"] == "reconfigure"
    assert CONF_AUTHPATH in result["data_schema"].schema
    assert _suggested(result, CONF_AUTHPATH) == "/config/wrong-dir"
