import base64
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from custom_components.pypowerwall import v1r_setup
from custom_components.pypowerwall.v1r_setup import (
    KEY_FILENAME,
    KEY_PENDING,
    KEY_VERIFIED,
    NoEnergySiteError,
    V1rSetupError,
    key_state,
    list_energy_sites,
    load_or_create_key,
    register_key,
)

CLIENT_TARGET = "custom_components.pypowerwall.v1r_setup.httpx.Client"


@pytest.fixture(autouse=True)
def _small_keys(monkeypatch):
    monkeypatch.setattr(v1r_setup, "KEY_SIZE", 1024)


def _client(status=200, body=None, error=None):
    client = MagicMock()
    if error:
        client.request.side_effect = error
    else:
        resp = MagicMock(status_code=status)
        resp.json.return_value = body
        client.request.return_value = resp
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    return factory, client


def _clients_response(public_der: bytes, state: int, key: str) -> dict:
    client = {"public_key": base64.b64encode(public_der).decode(), "state": state}
    if key == "add":
        inner = {"add_authorized_client_response": {"client": client}}
    else:
        inner = {"list_authorized_clients_response": {"clients": [client]}}
    return {"response": {"message": {"payload": {"authorization": {"message": inner}}}}}


class TestLoadOrCreateKey:
    def test_creates_then_reuses(self, tmp_path):
        directory = str(tmp_path / "pypowerwall")
        path, der = load_or_create_key(directory)
        assert path == os.path.join(directory, KEY_FILENAME)
        with open(path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        assert (
            key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.PKCS1
            )
            == der
        )
        assert load_or_create_key(directory) == (path, der)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_owner_only_permissions(self, tmp_path):
        path, _ = load_or_create_key(str(tmp_path))
        assert os.stat(path).st_mode & 0o777 == 0o600


class TestListEnergySites:
    def test_returns_energy_sites_only(self):
        body = {
            "response": [
                {"id": 1, "vin": "car"},
                {"energy_site_id": 123, "gateway_id": "GW1", "site_name": "Home"},
            ]
        }
        factory, client = _client(body=body)
        with patch(CLIENT_TARGET, factory):
            sites = list_energy_sites("tok")
        assert [(s.site_id, s.din, s.name) for s in sites] == [("123", "GW1", "Home")]
        assert client.request.call_args.args == (
            "GET",
            "https://owner-api.teslamotors.com/api/1/products",
        )
        assert client.request.call_args.kwargs["headers"] == {"Authorization": "Bearer tok"}
        assert factory.call_args.kwargs["http2"] is True

    def test_no_sites(self):
        factory, _ = _client(body={"response": [{"vin": "car"}]})
        with patch(CLIENT_TARGET, factory), pytest.raises(NoEnergySiteError):
            list_energy_sites("tok")

    @pytest.mark.parametrize(
        "factory_args",
        [
            {"status": 401, "body": {}},
            {"body": ["not", "a", "dict"]},
            {"error": httpx.ConnectError("down")},
        ],
    )
    def test_failures(self, factory_args):
        factory, _ = _client(**factory_args)
        with patch(CLIENT_TARGET, factory), pytest.raises(V1rSetupError):
            list_energy_sites("tok")


class TestRegisterKey:
    def test_posts_add_request_and_returns_state(self):
        der = b"public-key"
        factory, client = _client(body=_clients_response(der, KEY_PENDING, "add"))
        with patch(CLIENT_TARGET, factory):
            assert register_key("tok", "123", der) == KEY_PENDING

        method, url = client.request.call_args.args
        assert method == "POST"
        assert url == "https://owner-api.teslamotors.com/api/1/energy_sites/123/command"
        payload = client.request.call_args.kwargs["json"]
        request = payload["command_properties"]["message"]["authorization"][
            "add_authorized_client_request"
        ]
        assert request["public_key"] == base64.b64encode(der).decode()
        assert request["key_type"] == 1
        assert payload["command_type"] == "grpc_command"

    def test_key_state_matches_our_key_only(self):
        der = b"public-key"
        factory, _ = _client(body=_clients_response(b"someone-else", KEY_VERIFIED, "list"))
        with patch(CLIENT_TARGET, factory):
            assert key_state("tok", "123", der) is None

        factory, client = _client(body=_clients_response(der, KEY_VERIFIED, "list"))
        with patch(CLIENT_TARGET, factory):
            assert key_state("tok", "123", der) == KEY_VERIFIED
        message = client.request.call_args.kwargs["json"]["command_properties"]["message"]
        assert message == {"authorization": {"list_authorized_clients_request": {}}}
