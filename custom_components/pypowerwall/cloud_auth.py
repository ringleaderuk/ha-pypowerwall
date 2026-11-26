"""Tesla account sign-in for Cloud mode, driven from the config flow.

Replaces the `python -m pypowerwall setup` terminal step: the flow shows the
Tesla login link, the user pastes back the URL Tesla redirects to, and this
module exchanges the code for tokens and writes the same `.pypowerwall.auth`
file pypowerwall reads in Cloud mode.

Tesla's owner API only accepts an access token that came from a PKCE code
exchange to start a session (pypowerwall's cloud/AUTH.md, RCA-8), so the
exchanged access token is stored alongside the refresh token rather than
refreshed straight away.

The ownerapi client only accepts the tesla://auth/callback redirect (the older
https://auth.tesla.com/void/callback one now fails with "redirect_uri not
registered"). Browsers can't open tesla:// links, but Chrome and Edge log the
full callback URL in the developer tools console ("Failed to launch
'tesla://auth/callback?code=...'"), so parse_callback accepts that whole line
as well as the bare URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from pypowerwall.tesla_auth import _httpx_auth_verify

AUTH_HOST = "https://auth.tesla.com"
CLIENT_ID = "ownerapi"
SCOPES = "openid email offline_access"
REDIRECT_URI = "tesla://auth/callback"
AUTH_FILENAME = ".pypowerwall.auth"


class CloudAuthError(Exception):
    """A sign-in step failed; error_key is the config flow form error."""

    error_key = "login_failed"


class InvalidCallbackError(CloudAuthError):
    error_key = "invalid_callback"


class StateMismatchError(CloudAuthError):
    error_key = "state_mismatch"


class TokenExchangeError(CloudAuthError):
    error_key = "login_failed"


@dataclass(frozen=True)
class LoginRequest:
    """One sign-in attempt: the PKCE secret and the login link built from it."""

    code_verifier: str
    state: str
    url: str


def _authorize_url(code_challenge: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_HOST}/oauth2/v3/authorize?{urllib.parse.urlencode(params)}"


def new_login_request() -> LoginRequest:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    return LoginRequest(
        code_verifier=code_verifier,
        state=state,
        url=_authorize_url(code_challenge, state),
    )


def _query_value(text: str, name: str) -> str | None:
    match = re.search(rf"[?&]{name}=([^&\s'\"]+)", text)
    return urllib.parse.unquote(match.group(1)) if match else None


def parse_callback(pasted: str, expected_state: str) -> str:
    """Return the authorization code from the callback URL, or a console line containing it."""
    code = _query_value(pasted, "code")
    if not code:
        raise InvalidCallbackError
    if _query_value(pasted, "state") != expected_state:
        raise StateMismatchError
    return code


def exchange_code(code: str, code_verifier: str) -> dict:
    """Swap the authorization code for tokens. Blocking; run in the executor.

    HTTP/2 with pypowerwall's TLS settings, as Tesla's token endpoint rejects
    other client fingerprints (pypowerwall's cloud/AUTH.md, RCA-1/RCA-2).
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": REDIRECT_URI,
    }
    try:
        with httpx.Client(http2=True, verify=_httpx_auth_verify()) as client:
            resp = client.post(f"{AUTH_HOST}/oauth2/v3/token", json=payload, timeout=30)
    except httpx.HTTPError as err:
        raise TokenExchangeError(f"Could not reach Tesla: {err}") from err
    if resp.status_code != 200:
        raise TokenExchangeError(f"Tesla rejected the login (HTTP {resp.status_code})")
    data = resp.json()
    if not data.get("refresh_token") or not data.get("access_token"):
        raise TokenExchangeError("Tesla's response had no tokens")
    return data


def email_from_tokens(tokens: dict) -> str | None:
    """The account email, read from the id_token's claims."""
    id_token = tokens.get("id_token") or ""
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict):
        return None
    email = claims.get("email") or (claims.get("data") or {}).get("email")
    return email or None


def save_tokens(authpath: str, email: str, tokens: dict) -> None:
    """Write the auth file pypowerwall's Cloud mode reads. Blocking; run in the executor.

    The file holds only this account: the integration passes pypowerwall the
    first email in the file (coordinator._cloud_auth_email).
    """
    expires_in = int(tokens.get("expires_in") or 28800)
    sso = {
        "token_type": tokens.get("token_type", "Bearer"),
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": int(time.time()) + expires_in,
        "expires_in": expires_in,
    }
    if tokens.get("id_token"):
        sso["id_token"] = tokens["id_token"]
    directory = os.path.expanduser(authpath)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, AUTH_FILENAME)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({email: {"url": f"{AUTH_HOST}/", "sso": sso}}, f, indent=2)
