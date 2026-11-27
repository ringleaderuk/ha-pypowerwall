"""Register a v1r RSA key with the Powerwall, driven from the config flow.

Replaces the `python -m pypowerwall setup -v1r` terminal step. After a Tesla
sign-in (cloud_auth), this generates the RSA-4096 key pair, finds the energy
site and asks Tesla's owner API to add the public key as an authorized client
of the gateway. The gateway only trusts the key once someone toggles the
Powerwall's On/Off switch within about 10 minutes of that request; the config
flow then polls the key state until it reads VERIFIED.

Mirrors pypowerwall's v1r_register (owner API path) without its prompts.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pypowerwall.tesla_auth import _httpx_auth_verify
from pypowerwall.v1r_register import OWNER_API_BASE, _check_key_state

KEY_FILENAME = "tedapi_rsa_private.pem"
KEY_SIZE = 4096

KEY_PENDING = 1
KEY_TIMED_OUT = 2
KEY_VERIFIED = 3

CLIENT_DESCRIPTION = "Home Assistant pypowerwall"


class V1rSetupError(Exception):
    """A registration step failed; error_key is the config flow form error."""

    error_key = "v1r_register_failed"


class NoEnergySiteError(V1rSetupError):
    error_key = "no_energy_site"


@dataclass(frozen=True)
class EnergySite:
    site_id: str
    din: str | None
    name: str | None


def load_or_create_key(directory: str) -> tuple[str, bytes]:
    """Return (private key path, public key DER), generating the pair on first use.

    An existing key is reused: re-registering the same public key is how a
    closed verification window is reopened. Blocking; run in the executor.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, KEY_FILENAME)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(pem)
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )
    return path, public_der


def _request(token: str, method: str, path: str, json: dict | None = None) -> dict:
    try:
        with httpx.Client(http2=True, verify=_httpx_auth_verify()) as client:
            resp = client.request(
                method,
                f"{OWNER_API_BASE}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
    except httpx.HTTPError as err:
        raise V1rSetupError(f"Could not reach Tesla: {err}") from err
    if resp.status_code != 200:
        raise V1rSetupError(f"Tesla returned HTTP {resp.status_code} for {path}")
    try:
        body = resp.json()
    except ValueError as err:
        raise V1rSetupError(f"Tesla returned a non-JSON response for {path}") from err
    if not isinstance(body, dict):
        raise V1rSetupError(f"Unexpected response from Tesla for {path}")
    return body


def list_energy_sites(token: str) -> list[EnergySite]:
    """Energy sites on the Tesla account. Blocking; run in the executor."""
    products = _request(token, "GET", "/api/1/products").get("response") or []
    sites = [
        EnergySite(
            site_id=str(product["energy_site_id"]),
            din=product.get("gateway_id"),
            name=product.get("site_name"),
        )
        for product in products
        if isinstance(product, dict) and product.get("energy_site_id") is not None
    ]
    if not sites:
        raise NoEnergySiteError("No energy sites on this Tesla account")
    return sites


def _command(token: str, site_id: str, message: dict) -> dict:
    payload = {
        "command_properties": {
            "message": {"authorization": message},
            "identifier_type": 1,
        },
        "command_type": "grpc_command",
    }
    return _request(token, "POST", f"/api/1/energy_sites/{site_id}/command", json=payload)


def register_key(token: str, site_id: str, public_der: bytes) -> int | None:
    """Ask the gateway to add the public key; returns its key state. Blocking."""
    resp = _command(
        token,
        site_id,
        {
            "add_authorized_client_request": {
                "key_type": 1,
                "public_key": base64.b64encode(public_der).decode(),
                "authorized_client_type": 1,
                "description": CLIENT_DESCRIPTION,
            }
        },
    )
    return _check_key_state(resp, pubkey_der=public_der)


def key_state(token: str, site_id: str, public_der: bytes) -> int | None:
    """Current state of this public key on the gateway, or None if not listed. Blocking."""
    resp = _command(token, site_id, {"list_authorized_clients_request": {}})
    return _check_key_state(resp, pubkey_der=public_der)
