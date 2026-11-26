import base64
import hashlib
import json
import os
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from custom_components.pypowerwall.cloud_auth import (
    REDIRECT_URI_APP,
    REDIRECT_URI_WEB,
    InvalidCallbackError,
    StateMismatchError,
    TokenExchangeError,
    email_from_tokens,
    exchange_code,
    new_login_request,
    parse_callback,
    save_tokens,
)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class TestNewLoginRequest:
    def test_links_share_pkce_and_state_but_not_redirect(self):
        login = new_login_request()
        web, app = _query(login.url), _query(login.app_url)
        assert web["redirect_uri"] == REDIRECT_URI_WEB
        assert app["redirect_uri"] == REDIRECT_URI_APP
        assert web["state"] == app["state"] == login.state
        assert web["code_challenge"] == app["code_challenge"]
        assert web["client_id"] == "ownerapi"
        assert web["code_challenge_method"] == "S256"

    def test_challenge_is_s256_of_verifier(self):
        login = new_login_request()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(login.code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert _query(login.url)["code_challenge"] == expected

    def test_each_request_is_fresh(self):
        assert new_login_request().state != new_login_request().state


class TestParseCallback:
    def test_web_callback(self):
        url = "  https://auth.tesla.com/void/callback?code=abc&state=s1&issuer=x \n"
        assert parse_callback(url, "s1") == ("abc", REDIRECT_URI_WEB)

    def test_app_callback(self):
        assert parse_callback("tesla://auth/callback?code=abc&state=s1", "s1") == (
            "abc",
            REDIRECT_URI_APP,
        )

    @pytest.mark.parametrize(
        "url", ["", "hello", "https://auth.tesla.com/void/callback?state=s1"]
    )
    def test_missing_code(self, url):
        with pytest.raises(InvalidCallbackError):
            parse_callback(url, "s1")

    def test_wrong_state(self):
        with pytest.raises(StateMismatchError):
            parse_callback("https://auth.tesla.com/void/callback?code=abc&state=old", "s1")


def _client_returning(response=None, error=None):
    client = MagicMock()
    if error:
        client.post.side_effect = error
    else:
        client.post.return_value = response
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    return factory, client


class TestExchangeCode:
    def test_success_posts_code_exchange(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"access_token": "at", "refresh_token": "rt"}
        factory, client = _client_returning(resp)
        with patch("custom_components.pypowerwall.cloud_auth.httpx.Client", factory):
            assert exchange_code("abc", "ver", REDIRECT_URI_WEB)["refresh_token"] == "rt"

        assert factory.call_args.kwargs["http2"] is True
        url = client.post.call_args.args[0]
        payload = client.post.call_args.kwargs["json"]
        assert url == "https://auth.tesla.com/oauth2/v3/token"
        assert payload == {
            "grant_type": "authorization_code",
            "client_id": "ownerapi",
            "code": "abc",
            "code_verifier": "ver",
            "redirect_uri": REDIRECT_URI_WEB,
        }

    def test_rejected(self):
        factory, _ = _client_returning(MagicMock(status_code=401))
        with patch("custom_components.pypowerwall.cloud_auth.httpx.Client", factory):
            with pytest.raises(TokenExchangeError):
                exchange_code("abc", "ver", REDIRECT_URI_WEB)

    def test_missing_tokens(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"refresh_token": "rt"}
        factory, _ = _client_returning(resp)
        with patch("custom_components.pypowerwall.cloud_auth.httpx.Client", factory):
            with pytest.raises(TokenExchangeError):
                exchange_code("abc", "ver", REDIRECT_URI_WEB)

    def test_network_error(self):
        factory, _ = _client_returning(error=httpx.ConnectError("down"))
        with patch("custom_components.pypowerwall.cloud_auth.httpx.Client", factory):
            with pytest.raises(TokenExchangeError):
                exchange_code("abc", "ver", REDIRECT_URI_WEB)


def _id_token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


class TestEmailFromTokens:
    def test_top_level_email(self):
        assert email_from_tokens({"id_token": _id_token({"email": "a@b.c"})}) == "a@b.c"

    def test_nested_email(self):
        assert email_from_tokens({"id_token": _id_token({"data": {"email": "a@b.c"}})}) == "a@b.c"

    @pytest.mark.parametrize("tokens", [{}, {"id_token": "junk"}, {"id_token": "a.!!!.c"}])
    def test_missing(self, tokens):
        assert email_from_tokens(tokens) is None


class TestSaveTokens:
    def test_writes_pypowerwall_auth_file(self, tmp_path):
        authpath = tmp_path / "new" / "dir"
        save_tokens(
            str(authpath),
            "a@b.c",
            {"access_token": "at", "refresh_token": "rt", "expires_in": 100, "id_token": "id"},
        )
        path = authpath / ".pypowerwall.auth"
        saved = json.loads(path.read_text())
        assert list(saved) == ["a@b.c"]
        assert saved["a@b.c"]["url"] == "https://auth.tesla.com/"
        sso = saved["a@b.c"]["sso"]
        assert sso["access_token"] == "at"
        assert sso["refresh_token"] == "rt"
        assert sso["id_token"] == "id"
        assert sso["expires_in"] == 100
        assert sso["expires_at"] > 0

    def test_replaces_other_accounts(self, tmp_path):
        (tmp_path / ".pypowerwall.auth").write_text(json.dumps({"old@b.c": {}}))
        save_tokens(str(tmp_path), "a@b.c", {"access_token": "at", "refresh_token": "rt"})
        assert list(json.loads((tmp_path / ".pypowerwall.auth").read_text())) == ["a@b.c"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_owner_only_permissions(self, tmp_path):
        save_tokens(str(tmp_path), "a@b.c", {"access_token": "at", "refresh_token": "rt"})
        assert (tmp_path / ".pypowerwall.auth").stat().st_mode & 0o777 == 0o600
