"""Config flow for the pypowerwall integration."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, NumberSelectorMode

import pypowerwall

from .cloud_auth import (
    CloudAuthError,
    LoginRequest,
    email_from_tokens,
    exchange_code,
    install_auth_file,
    new_login_request,
    parse_callback,
    remove_staging,
    save_tokens,
    save_tokens_if_absent,
)
from .const import (
    AUTH_CACHE_FILENAMES,
    CLOUD_CONN_TYPES,
    CLOUD_LOGIN_AUTH_DIR,
    CONF_AUTHPATH,
    CONF_CONN_TYPE,
    CONF_DESIGN_CAPACITY,
    CONF_GW_PWD,
    CONF_RSA_KEY_PATH,
    CONF_SITEID,
    CONF_WIFI_HOST,
    CONN_TYPE_CLOUD,
    CONN_TYPE_FLEETAPI,
    CONN_TYPE_HYBRID,
    CONN_TYPE_LOCAL,
    CONN_TYPE_TEDAPI,
    CONN_TYPE_TEDAPI_V1R,
    DEFAULT_DESIGN_CAPACITY_KWH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_CLOUD,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import build_powerwall_kwargs
from .v1r_setup import (
    KEY_TIMED_OUT,
    KEY_VERIFIED,
    EnergySite,
    V1rSetupError,
    key_state,
    list_energy_sites,
    load_or_create_key,
    register_key,
)

_LOGGER = logging.getLogger(__name__)

STEP_CLOUD_LOGIN = "cloud_login"
STEP_RECONFIGURE_CLOUD_LOGIN = "reconfigure_cloud_login"
STEP_RECONFIGURE_SETTINGS = "reconfigure_settings"
STEP_RECONFIGURE_TESLA_LOGIN = "reconfigure_tesla_login"
STEP_RECONFIGURE_CONNECTION = "reconfigure_connection"
STEP_V1R_LOGIN = "v1r_login"
STEP_V1R_SITE = "v1r_site"
STEP_V1R_VERIFY = "v1r_verify"
STEP_V1R_WAIT = "v1r_wait"
STEP_V1R_CHECKED = "v1r_checked"
STEP_V1R_GATEWAY = "v1r_gateway"
CONF_CALLBACK_URL = "callback_url"
CONF_ENERGY_SITE = "energy_site"
# How long to watch for VERIFIED after the switch toggle; pypowerwall's
# documented transition took about a minute.
V1R_VERIFY_TIMEOUT = 120
V1R_VERIFY_POLL_INTERVAL = 10

STEP_CLOUD_LOGIN_SCHEMA = vol.Schema({vol.Required(CONF_CALLBACK_URL): str})

STEP_V1R_GATEWAY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_GW_PWD): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_WIFI_HOST): str,
    }
)

STEP_LOCAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_TEDAPI_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_GW_PWD): str,
    }
)

STEP_HYBRID_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_GW_PWD): str,
    }
)

STEP_TEDAPI_V1R_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_GW_PWD): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Required(CONF_RSA_KEY_PATH): str,
        vol.Optional(CONF_WIFI_HOST): str,
    }
)

STEP_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTHPATH): str,
        vol.Optional(CONF_SITEID): str,
    }
)

STEP_FLEETAPI_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTHPATH): str,
        vol.Optional(CONF_SITEID): str,
    }
)

# Menu order: cheapest-to-set-up modes first, the two file-based advanced modes last.
MENU_OPTIONS = [
    CONN_TYPE_TEDAPI,
    CONN_TYPE_HYBRID,
    CONN_TYPE_LOCAL,
    STEP_CLOUD_LOGIN,
    CONN_TYPE_CLOUD,
    CONN_TYPE_FLEETAPI,
    STEP_V1R_LOGIN,
    CONN_TYPE_TEDAPI_V1R,
]

# Each connection type's settings schema, keyed so the reconfigure step can
# re-show the correct form for an entry created under any of them.
CONN_TYPE_SCHEMAS: dict[str, vol.Schema] = {
    CONN_TYPE_TEDAPI: STEP_TEDAPI_SCHEMA,
    CONN_TYPE_HYBRID: STEP_HYBRID_SCHEMA,
    CONN_TYPE_LOCAL: STEP_LOCAL_SCHEMA,
    CONN_TYPE_CLOUD: STEP_CLOUD_SCHEMA,
    CONN_TYPE_FLEETAPI: STEP_FLEETAPI_SCHEMA,
    CONN_TYPE_TEDAPI_V1R: STEP_TEDAPI_V1R_SCHEMA,
}


class PowerwallConnectionError(Exception):
    """Raised when the Powerwall gateway cannot be reached or authenticated."""


class AuthCacheError(Exception):
    """Raised when a Cloud/FleetAPI auth directory can't be used; error_key is the form error."""

    error_key = "unknown"


class AuthCacheNotFoundError(AuthCacheError):
    """Raised when the auth directory doesn't contain the file pypowerwall reads."""

    error_key = "auth_cache_not_found"


class AuthCacheNotWritableError(AuthCacheError):
    """Raised when the auth directory is read-only, so refreshed tokens couldn't be saved."""

    error_key = "auth_cache_not_writable"


class MissingPasswordError(Exception):
    """Raised when a v1r entry supplies neither the gateway nor the customer password."""


class RsaKeyNotFoundError(Exception):
    """Raised when a v1r entry's RSA private key path isn't a file Home Assistant can see."""


def _connect_and_get_info(conn_type: str, data: dict[str, Any]) -> tuple[str, str | None]:
    """Connect to the gateway and return (din, site_name), raising on any failure."""
    pw = pypowerwall.Powerwall(**build_powerwall_kwargs(conn_type, data))
    if not pw.is_connected():
        raise PowerwallConnectionError(f"Unable to connect to Powerwall ({conn_type} mode)")
    din = pw.din()
    if not din:
        raise PowerwallConnectionError(f"Connected ({conn_type} mode) but could not read a DIN")
    return din, pw.site_name()


def _check_auth_cache(conn_type: str, data: dict[str, Any]) -> None:
    """Fail with a specific error when a Cloud/FleetAPI auth directory is unusable."""
    filename = AUTH_CACHE_FILENAMES.get(conn_type)
    if filename is None:
        return
    authpath = os.path.expanduser(data[CONF_AUTHPATH])
    if not os.path.isfile(os.path.join(authpath, filename)):
        raise AuthCacheNotFoundError
    if not os.access(authpath, os.W_OK):
        raise AuthCacheNotWritableError


def _check_rsa_key(conn_type: str, data: dict[str, Any]) -> None:
    """Fail with a specific error when the v1r RSA private key file doesn't exist."""
    if conn_type != CONN_TYPE_TEDAPI_V1R:
        return
    if not os.path.isfile(os.path.expanduser(data[CONF_RSA_KEY_PATH])):
        raise RsaKeyNotFoundError


async def _validate_input(
    hass: HomeAssistant, conn_type: str, data: dict[str, Any]
) -> tuple[str, str | None]:
    """Validate the user input, returning (din, site_name).

    v1r login takes either the gateway password or the customer password
    (pypowerwall derives the latter from the last 5 characters of the former), so
    both are optional in the schema -- voluptuous can't express "one of" -- and
    at least one is enforced here.
    """
    if conn_type == CONN_TYPE_TEDAPI_V1R and not (
        data.get(CONF_GW_PWD) or data.get(CONF_PASSWORD)
    ):
        raise MissingPasswordError
    await hass.async_add_executor_job(_check_auth_cache, conn_type, data)
    await hass.async_add_executor_job(_check_rsa_key, conn_type, data)
    return await hass.async_add_executor_job(_connect_and_get_info, conn_type, data)


class PypowerwallConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for pypowerwall."""

    VERSION = 1

    def __init__(self) -> None:
        self._login: LoginRequest | None = None
        self._v1r_token: str | None = None
        self._v1r_key_path: str | None = None
        self._v1r_public_der: bytes | None = None
        self._v1r_sites: list[EnergySite] = []
        self._v1r_site: EnergySite | None = None
        self._v1r_state: int | None = None
        self._v1r_registered = False
        self._v1r_wait_task: asyncio.Task[int | None] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user pick which pypowerwall connection mode to set up."""
        return self.async_show_menu(step_id="user", menu_options=MENU_OPTIONS)

    async def _async_step_connection(
        self,
        conn_type: str,
        schema: vol.Schema,
        user_input: dict[str, Any] | None,
        step_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Shared validate/create logic for every connection-type step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {CONF_CONN_TYPE: conn_type, **(extra_data or {}), **user_input}
            try:
                din, site_name = await _validate_input(self.hass, conn_type, data)
            except MissingPasswordError:
                errors["base"] = "missing_password"
            except RsaKeyNotFoundError:
                errors["base"] = "rsa_key_not_found"
            except AuthCacheError as err:
                errors["base"] = err.error_key
            except PowerwallConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface unexpected errors as a generic failure
                _LOGGER.exception("Unexpected error validating Powerwall connection")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(din)
                self._abort_if_unique_id_configured()
                title = data.get(CONF_HOST) or site_name or f"Powerwall ({conn_type})"
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id=step_id or conn_type,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user correct an existing entry's connection settings.

        Without this, everything collected at setup is write-once: a changed
        gateway password, a Cloud/FleetAPI config cache that moved, or a
        gateway that picked up a new IP all leave the entry permanently
        failing to connect, and the only recourse is deleting and re-adding
        it -- which loses the entity IDs and all their recorded history.

        The connection type itself is deliberately not editable here.
        Switching mode changes which credentials are even meaningful (and
        which entities exist -- see GRID_CONTROL_CONN_TYPES and
        GRID_ISLANDING_CONN_TYPES), so that stays a remove-and-re-add
        operation rather than a reconfigure.

        The gateway identity is pinned: settings that connect successfully
        but report a different DIN are rejected via _abort_if_unique_id_mismatch
        rather than silently repointing this entry -- and the history attached
        to its entities -- at a different physical gateway.
        """
        entry = self._get_reconfigure_entry()
        conn_type = entry.data[CONF_CONN_TYPE]
        if conn_type == CONN_TYPE_CLOUD and user_input is None:
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=[STEP_RECONFIGURE_CLOUD_LOGIN, STEP_RECONFIGURE_SETTINGS],
                description_placeholders={"conn_type": conn_type},
            )
        if conn_type == CONN_TYPE_TEDAPI_V1R and user_input is None:
            return self.async_show_menu(
                step_id="reconfigure",
                menu_options=[STEP_RECONFIGURE_CONNECTION, STEP_RECONFIGURE_TESLA_LOGIN],
                description_placeholders={"conn_type": conn_type},
            )
        return await self._async_reconfigure_form(user_input)

    async def async_step_reconfigure_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v1r entries: edit the gateway address, passwords and key path."""
        return await self._async_reconfigure_form(user_input)

    async def async_step_reconfigure_tesla_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v1r entries: sign in to Tesla for the export window actions' tariff writes."""
        return await self._async_cloud_login(
            STEP_RECONFIGURE_TESLA_LOGIN, user_input, self._get_reconfigure_entry()
        )

    async def async_step_reconfigure_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud entries: edit the auth directory / site ID by hand."""
        return await self._async_reconfigure_form(user_input)

    async def async_step_reconfigure_cloud_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud entries: sign in to Tesla again, rewriting the entry's auth file."""
        return await self._async_cloud_login(
            STEP_RECONFIGURE_CLOUD_LOGIN, user_input, self._get_reconfigure_entry()
        )

    async def _async_reconfigure_form(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        conn_type = entry.data[CONF_CONN_TYPE]
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {CONF_CONN_TYPE: conn_type, **user_input}
            try:
                din, _site_name = await _validate_input(self.hass, conn_type, data)
            except MissingPasswordError:
                errors["base"] = "missing_password"
            except RsaKeyNotFoundError:
                errors["base"] = "rsa_key_not_found"
            except AuthCacheError as err:
                errors["base"] = err.error_key
            except PowerwallConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface unexpected errors as a generic failure
                _LOGGER.exception("Unexpected error validating Powerwall connection")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(din)
                self._abort_if_unique_id_mismatch(reason="wrong_gateway")
                return self.async_update_reload_and_abort(entry, data=data)

        # Pre-fill with the entry's current values (overlaid with whatever the
        # user just submitted, so a failed attempt comes back edited rather
        # than reset), and name the mode in the description -- the form fields
        # alone don't say which connection type is being reconfigured.
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONN_TYPE_SCHEMAS[conn_type], {**entry.data, **(user_input or {})}
            ),
            errors=errors,
            description_placeholders={"conn_type": conn_type},
        )

    async def async_step_tedapi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """TEDAPI full mode: host + gateway QR password only."""
        return await self._async_step_connection(CONN_TYPE_TEDAPI, STEP_TEDAPI_SCHEMA, user_input)

    async def async_step_hybrid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Hybrid mode: customer local login plus TEDAPI for supplemental vitals."""
        return await self._async_step_connection(CONN_TYPE_HYBRID, STEP_HYBRID_SCHEMA, user_input)

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Local customer login mode (Customer Login must be enabled on the gateway)."""
        return await self._async_step_connection(CONN_TYPE_LOCAL, STEP_LOCAL_SCHEMA, user_input)

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Cloud mode: reuses a token cache produced by `python -m pypowerwall setup`."""
        return await self._async_step_connection(CONN_TYPE_CLOUD, STEP_CLOUD_SCHEMA, user_input)

    async def async_step_cloud_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud mode, signing in to Tesla from here instead of `pypowerwall setup`."""
        return await self._async_cloud_login(STEP_CLOUD_LOGIN, user_input, None)

    async def _async_cloud_login(
        self, step_id: str, user_input: dict[str, Any] | None, entry: ConfigEntry | None
    ) -> ConfigFlowResult:
        """Show the Tesla login link, then exchange the pasted callback URL for tokens.

        With an entry (reconfigure) the tokens replace that entry's auth file and
        the DIN must still match; otherwise they go to CLOUD_LOGIN_AUTH_DIR and a
        new entry is created. A v1r entry keeps its LAN settings and only gains
        the auth file, which the export window actions use for tariff writes.

        The tokens are validated from a staging directory and only copied into
        place once the gateway checks pass, so signing in to the wrong account
        can't overwrite a working entry's auth file. That validation also uses
        the new access token straight away, which Tesla requires before it will
        ever refresh it (pypowerwall's cloud/AUTH.md, RCA-8). An authorization
        code works once, so any failure issues a fresh login link.
        """
        errors: dict[str, str] = {}
        if self._login is None:
            self._login = new_login_request()

        if user_input is not None:
            login = self._login
            authpath = (
                entry.data.get(CONF_AUTHPATH) if entry is not None else None
            ) or self.hass.config.path(CLOUD_LOGIN_AUTH_DIR)
            staging = self.hass.config.path(
                CLOUD_LOGIN_AUTH_DIR, f".signin-{secrets.token_hex(4)}"
            )
            try:
                code = parse_callback(user_input[CONF_CALLBACK_URL], login.state)
                tokens = await self.hass.async_add_executor_job(
                    exchange_code, code, login.code_verifier
                )
                email = email_from_tokens(tokens)
                if not email:
                    raise CloudAuthError("Tesla's tokens did not include the account email")
                await self.hass.async_add_executor_job(save_tokens, staging, email, tokens)
                data = {CONF_CONN_TYPE: CONN_TYPE_CLOUD, CONF_AUTHPATH: staging}
                if entry is not None and entry.data.get(CONF_SITEID):
                    data[CONF_SITEID] = entry.data[CONF_SITEID]
                din, site_name = await _validate_input(self.hass, CONN_TYPE_CLOUD, data)
            except CloudAuthError as err:
                _LOGGER.warning("Tesla sign-in failed: %s", err)
                errors["base"] = err.error_key
            except PowerwallConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface unexpected errors as a generic failure
                _LOGGER.exception("Unexpected error during Tesla sign-in")
                errors["base"] = "unknown"
            else:
                try:
                    await self.async_set_unique_id(din)
                    if entry is not None:
                        self._abort_if_unique_id_mismatch(reason="wrong_gateway")
                    else:
                        self._abort_if_unique_id_configured()
                    await self.hass.async_add_executor_job(install_auth_file, staging, authpath)
                finally:
                    await self.hass.async_add_executor_job(remove_staging, staging)
                data[CONF_AUTHPATH] = authpath
                if entry is not None and entry.data[CONF_CONN_TYPE] == CONN_TYPE_TEDAPI_V1R:
                    return self.async_update_reload_and_abort(
                        entry,
                        data={**entry.data, CONF_AUTHPATH: authpath},
                        reason="tesla_sign_in_saved",
                    )
                if entry is not None:
                    return self.async_update_reload_and_abort(entry, data=data)
                return self.async_create_entry(
                    title=site_name or f"Powerwall ({CONN_TYPE_CLOUD})", data=data
                )
            await self.hass.async_add_executor_job(remove_staging, staging)
            self._login = new_login_request()

        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_CLOUD_LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={"login_url": self._login.url},
        )

    async def async_step_fleetapi(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """FleetAPI mode: reuses a config cache from `python -m pypowerwall.fleetapi setup`."""
        return await self._async_step_connection(
            CONN_TYPE_FLEETAPI, STEP_FLEETAPI_SCHEMA, user_input
        )

    async def async_step_tedapi_v1r(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """TEDAPI v1r LAN mode: requires an RSA key from `python -m pypowerwall register`."""
        return await self._async_step_connection(
            CONN_TYPE_TEDAPI_V1R, STEP_TEDAPI_V1R_SCHEMA, user_input
        )

    async def async_step_v1r_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """v1r set up from here: sign in to Tesla, then create and register the RSA key."""
        errors: dict[str, str] = {}
        if self._login is None:
            self._login = new_login_request()

        if user_input is not None:
            login = self._login
            try:
                code = parse_callback(user_input[CONF_CALLBACK_URL], login.state)
                tokens = await self.hass.async_add_executor_job(
                    exchange_code, code, login.code_verifier
                )
                self._v1r_token = tokens["access_token"]
                (
                    self._v1r_key_path,
                    self._v1r_public_der,
                ) = await self.hass.async_add_executor_job(
                    load_or_create_key, self.hass.config.path(CLOUD_LOGIN_AUTH_DIR)
                )
                self._v1r_sites = await self.hass.async_add_executor_job(
                    list_energy_sites, self._v1r_token
                )
                if email := email_from_tokens(tokens):
                    await self.hass.async_add_executor_job(
                        save_tokens_if_absent,
                        self.hass.config.path(CLOUD_LOGIN_AUTH_DIR),
                        email,
                        tokens,
                    )
            except (CloudAuthError, V1rSetupError) as err:
                _LOGGER.warning("v1r setup failed: %s", err)
                errors["base"] = err.error_key
            except Exception:  # noqa: BLE001 - surface unexpected errors as a generic failure
                _LOGGER.exception("Unexpected error during v1r setup")
                errors["base"] = "unknown"
            else:
                if len(self._v1r_sites) == 1:
                    self._v1r_site = self._v1r_sites[0]
                    return await self._async_v1r_register()
                return await self.async_step_v1r_site()
            self._login = new_login_request()

        return self.async_show_form(
            step_id=STEP_V1R_LOGIN,
            data_schema=STEP_CLOUD_LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={"login_url": self._login.url},
        )

    async def async_step_v1r_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the energy site when the Tesla account has more than one."""
        if user_input is not None:
            self._v1r_site = next(
                site for site in self._v1r_sites if site.site_id == user_input[CONF_ENERGY_SITE]
            )
            return await self._async_v1r_register()
        choices = {
            site.site_id: f"{site.name or site.site_id} ({site.din or 'unknown gateway'})"
            for site in self._v1r_sites
        }
        return self.async_show_form(
            step_id=STEP_V1R_SITE,
            data_schema=vol.Schema({vol.Required(CONF_ENERGY_SITE): vol.In(choices)}),
        )

    async def _async_v1r_register(self, error: str | None = None) -> ConfigFlowResult:
        """Send the key to the gateway, which opens its ~10 minute verification window.

        Checks for an existing entry for this gateway first: finding it only at
        the gateway form, after the switch toggle, would waste the whole setup.
        """
        if self._v1r_site.din:
            await self.async_set_unique_id(self._v1r_site.din)
            self._abort_if_unique_id_configured(error="v1r_already_configured")
        try:
            self._v1r_state = await self.hass.async_add_executor_job(
                register_key, self._v1r_token, self._v1r_site.site_id, self._v1r_public_der
            )
        except V1rSetupError as err:
            _LOGGER.warning("v1r key registration failed: %s", err)
            self._v1r_registered = False
            return self._async_show_v1r_verify(err.error_key)
        self._v1r_registered = True
        if self._v1r_state == KEY_VERIFIED:
            return await self.async_step_v1r_gateway()
        return self._async_show_v1r_verify(error)

    @callback
    def _async_show_v1r_verify(self, error: str | None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=STEP_V1R_VERIFY,
            data_schema=vol.Schema({}),
            errors={"base": error} if error else None,
        )

    async def async_step_v1r_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The user has toggled the Powerwall's switch; go and watch for VERIFIED."""
        if user_input is None:
            return self._async_show_v1r_verify(None)
        if not self._v1r_registered:
            return await self._async_v1r_register()
        return await self.async_step_v1r_wait()

    async def async_step_v1r_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._v1r_wait_task is None:
            self._v1r_wait_task = self.hass.async_create_task(self._async_wait_for_verified())
        if not self._v1r_wait_task.done():
            return self.async_show_progress(
                step_id=STEP_V1R_WAIT,
                progress_action="wait_for_verified",
                progress_task=self._v1r_wait_task,
            )
        self._v1r_state = self._v1r_wait_task.result()
        self._v1r_wait_task = None
        return self.async_show_progress_done(next_step_id=STEP_V1R_CHECKED)

    async def _async_wait_for_verified(self) -> int | None:
        state = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + V1R_VERIFY_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(V1R_VERIFY_POLL_INTERVAL)
            try:
                state = await self.hass.async_add_executor_job(
                    key_state, self._v1r_token, self._v1r_site.site_id, self._v1r_public_der
                )
            except V1rSetupError as err:
                _LOGGER.debug("v1r key state poll failed: %s", err)
                continue
            if state in (KEY_VERIFIED, KEY_TIMED_OUT):
                break
        return state

    async def async_step_v1r_checked(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._v1r_state == KEY_VERIFIED:
            return await self.async_step_v1r_gateway()
        if self._v1r_state == KEY_TIMED_OUT:
            return await self._async_v1r_register("v1r_window_closed")
        return self._async_show_v1r_verify("v1r_not_verified")

    async def async_step_v1r_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The key is verified: collect the gateway's LAN address and password."""
        return await self._async_step_connection(
            CONN_TYPE_TEDAPI_V1R,
            STEP_V1R_GATEWAY_SCHEMA,
            user_input,
            step_id=STEP_V1R_GATEWAY,
            extra_data={CONF_RSA_KEY_PATH: self._v1r_key_path},
            description_placeholders={"key_path": self._v1r_key_path},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return PypowerwallOptionsFlow()


class PypowerwallOptionsFlow(OptionsFlow):
    """Handle options for pypowerwall (scan interval, battery design capacity)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        default_scan_interval = (
            DEFAULT_SCAN_INTERVAL_CLOUD
            if self.config_entry.data[CONF_CONN_TYPE] in CLOUD_CONN_TYPES
            else DEFAULT_SCAN_INTERVAL
        )
        current = self.config_entry.options.get("scan_interval", default_scan_interval)
        current_capacity = self.config_entry.options.get(
            CONF_DESIGN_CAPACITY, DEFAULT_DESIGN_CAPACITY_KWH
        )
        schema = vol.Schema(
            {
                vol.Required("scan_interval", default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL, max=300, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_DESIGN_CAPACITY, default=current_capacity): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=100,
                        step=0.1,
                        unit_of_measurement="kWh",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
