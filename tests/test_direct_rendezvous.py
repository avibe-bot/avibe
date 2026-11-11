"""Contract of the direct-path rendezvous (docs/plans/remote-direct-rendezvous.md)."""

from __future__ import annotations

import pytest

from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_session_cookie
from vibe import direct_rendezvous, remote_access
from vibe.authorization import http_authorization_policy
from vibe.direct_rendezvous import (
    DIRECT_ALPN,
    RENDEZVOUS_TTL_SECONDS,
    DirectEndpoint,
    DirectRendezvous,
    classify_address,
)
from vibe.ui_server import app

ORIGIN = "https://alex.avibe.bot"
REMOTE_PEER = {"REMOTE_ADDR": "203.0.113.10"}
INSTANCE_ENDPOINT = "a" * 64
CLIENT_ENDPOINT = "c" * 64
NOW = 1_800_000_000
PATH = "/api/direct/rendezvous"


class _Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


def _offer(rendezvous: DirectRendezvous, client: str = CLIENT_ENDPOINT, subject: str = "sub-1") -> dict:
    return rendezvous.offer(
        instance_id="inst_123",
        hostname="alex.avibe.bot",
        subject=subject,
        client_endpoint_id=client,
    )


# --- which addresses a client may be told about -------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.168.1.20:51234", {"kind": "lan", "address": "192.168.1.20:51234"}),
        ("10.0.0.5:1", {"kind": "lan", "address": "10.0.0.5:1"}),
        ("172.31.255.1:65535", {"kind": "lan", "address": "172.31.255.1:65535"}),
        ("[fd12:3456:789a::1]:51234", {"kind": "lan", "address": "[fd12:3456:789a::1]:51234"}),
        ("[240E:B86:551:7700:0:0:0:1]:51234", {"kind": "ipv6", "address": "[240e:b86:551:7700::1]:51234"}),
        # Withheld: no path in this phase can reach them, or they name this machine.
        ("127.0.0.1:51234", None),
        ("[::1]:51234", None),
        ("0.0.0.0:51234", None),
        ("[::]:51234", None),
        ("169.254.10.1:51234", None),
        ("[fe80::1]:51234", None),
        ("[fe80::1%en0]:51234", None),
        ("100.64.0.1:51234", None),
        ("203.0.113.9:51234", None),
        ("8.8.8.8:51234", None),
        ("172.32.0.1:51234", None),
        ("224.0.0.251:5353", None),
        ("[ff02::fb]:5353", None),
        ("[2001:db8::1]:51234", None),
        ("[::ffff:192.168.1.20]:51234", None),
        # Not a socket address.
        ("192.168.1.20", None),
        ("192.168.1.20:0", None),
        ("192.168.1.20:65536", None),
        ("fd12::1:51234", None),
        ("printer.local:51234", None),
        ("", None),
    ],
)
def test_only_lan_and_global_ipv6_socket_addresses_are_candidates(address, expected):
    assert classify_address(address) == expected


# --- the published endpoint and its generations -------------------------------


def test_offer_describes_the_endpoint_and_admits_the_client():
    clock = _Clock()
    rendezvous = DirectRendezvous(clock)
    rendezvous.publish(
        DirectEndpoint(
            INSTANCE_ENDPOINT,
            ("192.168.1.20:51234", "127.0.0.1:51234", "[240e:b86:551:7700::1]:51234", "192.168.1.20:51234"),
        )
    )

    offer = _offer(rendezvous)

    generation = offer.pop("generation")
    assert generation.endswith(":1")
    assert offer == {
        "version": 1,
        "status": "ready",
        "instance_id": "inst_123",
        "hostname": "alex.avibe.bot",
        "issued_at": NOW,
        "expires_at": NOW + RENDEZVOUS_TTL_SECONDS,
        "alpn": DIRECT_ALPN,
        "endpoint": {
            "id": INSTANCE_ENDPOINT,
            "addresses": [
                {"kind": "lan", "address": "192.168.1.20:51234"},
                {"kind": "ipv6", "address": "[240e:b86:551:7700::1]:51234"},
            ],
            "relay": None,
        },
        "client": {"endpoint_id": CLIENT_ENDPOINT, "admitted_until": NOW + RENDEZVOUS_TTL_SECONDS},
    }
    assert rendezvous.is_admitted(CLIENT_ENDPOINT)
    assert not rendezvous.is_admitted("d" * 64)


def test_admission_listener_notifies_before_offer_return_and_on_extension():
    """The notice hook runs before the route can return and repeats on refresh."""
    rendezvous = DirectRendezvous(_Clock())
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    events: list[tuple[str, int]] = []
    order: list[str] = []
    rendezvous.set_admission_listener(lambda endpoint_id, ttl: (events.append((endpoint_id, ttl)), order.append("notice")))

    _offer(rendezvous)
    order.append("answer")
    _offer(rendezvous)
    order.append("answer")

    assert events == [(CLIENT_ENDPOINT, RENDEZVOUS_TTL_SECONDS)] * 2
    assert order == ["notice", "answer", "notice", "answer"]


def test_admission_listener_reports_early_drops_and_no_listener_is_optional():
    """Evictions send zero, while a rendezvous without a listener remains unchanged."""
    rendezvous = DirectRendezvous(_Clock())
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    events: list[tuple[str, int]] = []
    rendezvous.set_admission_listener(lambda endpoint_id, ttl: events.append((endpoint_id, ttl)))
    clients = [f"{index:064x}" for index in range(9)]
    for client in clients:
        _offer(rendezvous, client=client, subject="same-subject")

    assert events[-2:] == [(clients[0], 0), (clients[-1], RENDEZVOUS_TTL_SECONDS)]
    rendezvous.set_admission_listener(None)
    no_listener = DirectRendezvous(_Clock())
    no_listener.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    assert _offer(no_listener)["status"] == "ready"
    assert no_listener.is_admitted(CLIENT_ENDPOINT)

    rendezvous = DirectRendezvous(_Clock())
    addresses = ("192.168.1.20:51234", "[240e:b86:551:7700::1]:51234", "10.0.0.5:51234")
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, addresses))
    first = _offer(rendezvous)["generation"]

    # The same set in another order, with a withheld address and a repeat.
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, (*reversed(addresses), "127.0.0.1:1", addresses[0])))
    unchanged = _offer(rendezvous)["generation"]
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.7.7:51234",)))
    moved = _offer(rendezvous)["generation"]
    still_admitted = rendezvous.is_admitted(CLIENT_ENDPOINT)
    rendezvous.publish(DirectEndpoint("b" * 64, ("192.168.7.7:51234",)))
    admitted_after_new_key = rendezvous.is_admitted(CLIENT_ENDPOINT)
    rekeyed = _offer(rendezvous)["generation"]

    assert unchanged == first
    assert len({first, moved, rekeyed}) == 3
    assert still_admitted
    assert not admitted_after_new_key
    # Another process never repeats this one's generations.
    assert DirectRendezvous(_Clock())._boot != rendezvous._boot


@pytest.mark.parametrize(
    ("publish", "reason"),
    [
        (None, "not_running"),
        (lambda r: r.publish(None, reason="no_certificate"), "no_certificate"),
        (lambda r: r.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("127.0.0.1:51234",))), "no_reachable_address"),
    ],
)
def test_without_a_dialable_endpoint_the_client_is_told_to_stay_on_the_tunnel(publish, reason):
    rendezvous = DirectRendezvous(_Clock())
    if publish is not None:
        publish(rendezvous)

    offer = _offer(rendezvous)

    assert offer == {
        "version": 1,
        "status": "unavailable",
        "reason": reason,
        "instance_id": "inst_123",
        "hostname": "alex.avibe.bot",
        "retry_after_seconds": 60,
    }
    assert not rendezvous.is_admitted(CLIENT_ENDPOINT)


def test_admission_expires_and_is_bounded_per_subject():
    clock = _Clock()
    rendezvous = DirectRendezvous(clock)
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    clients = [f"{index:064x}" for index in range(10)]

    for client in clients:
        _offer(rendezvous, client=client, subject="noisy")
    _offer(rendezvous, client=CLIENT_ENDPOINT, subject="quiet")

    assert [rendezvous.is_admitted(client) for client in clients] == [False, False] + [True] * 8
    assert rendezvous.is_admitted(CLIENT_ENDPOINT)
    clock.now += RENDEZVOUS_TTL_SECONDS
    assert not rendezvous.is_admitted(CLIENT_ENDPOINT)


def test_publish_refuses_anything_but_a_public_endpoint_id():
    rendezvous = DirectRendezvous(_Clock())

    for endpoint_id in ("", "A" * 64, "a" * 63, "g" * 64, "a" * 65):
        with pytest.raises(ValueError):
            rendezvous.publish(DirectEndpoint(endpoint_id, ("192.168.1.20:51234",)))


# --- the route ---------------------------------------------------------------


@pytest.fixture
def instance(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    rendezvous = DirectRendezvous(_Clock())
    monkeypatch.setattr(direct_rendezvous, "rendezvous", rendezvous)
    return config, rendezvous


def _signed_in(config, role: str = "viewer", subject: str = "sub-viewer"):
    client = app.test_client()
    cookie = remote_session_cookie(config, "viewer@example.com", subject, role=role, access_source="email")
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    return client


def _post(client, body, *, origin: str = ORIGIN, headers: dict | None = None):
    return client.post(
        PATH,
        base_url=origin,
        environ_base=REMOTE_PEER,
        headers=csrf_headers(client, origin) if headers is None else headers,
        json=body,
    )


def _body(client_endpoint_id: str = CLIENT_ENDPOINT, version: int = 1) -> dict:
    return {"version": version, "client_endpoint_id": client_endpoint_id}


def test_any_signed_in_remote_role_may_ask():
    assert http_authorization_policy("POST", PATH).minimum_role == "viewer"


def test_signed_in_viewer_gets_the_endpoint_and_is_admitted(instance):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))

    response = _post(_signed_in(config), _body())

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["status"] == "ready"
    assert payload["instance_id"] == config.remote_access.vibe_cloud.instance_id
    assert payload["hostname"] == "alex.avibe.bot"
    assert payload["endpoint"] == {
        "id": INSTANCE_ENDPOINT,
        "addresses": [{"kind": "lan", "address": "192.168.1.20:51234"}],
        "relay": None,
    }
    assert rendezvous._admissions == {CLIENT_ENDPOINT: ("sub-viewer", NOW + RENDEZVOUS_TTL_SECONDS)}


def test_before_a_transport_exists_the_answer_is_unavailable(instance):
    config, _rendezvous = instance

    response = _post(_signed_in(config), _body())

    assert response.status_code == 200
    assert response.get_json() == {
        "version": 1,
        "status": "unavailable",
        "reason": "not_running",
        "instance_id": config.remote_access.vibe_cloud.instance_id,
        "hostname": "alex.avibe.bot",
        "retry_after_seconds": 60,
    }


def test_anonymous_and_local_callers_learn_nothing(instance):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    anonymous = app.test_client()
    local = app.test_client()

    anonymous_response = _post(anonymous, _body())
    local_response = local.post(
        PATH,
        base_url="http://127.0.0.1:5123",
        headers=csrf_headers(local, "http://127.0.0.1:5123"),
        json=_body(),
    )

    assert anonymous_response.status_code == 401
    assert anonymous_response.get_json() == {"ok": False, "error": "remote_access_login_required"}
    assert local_response.status_code == 403
    assert local_response.get_json() == {"ok": False, "error": "remote_session_required"}
    assert rendezvous._admissions == {}


def test_request_without_the_csrf_token_is_refused(instance):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))

    response = _post(_signed_in(config), _body(), headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert rendezvous._admissions == {}


@pytest.mark.parametrize(
    "body",
    [
        _body(version=2),
        _body(client_endpoint_id="C" * 64),
        _body(client_endpoint_id="c" * 63),
        _body(client_endpoint_id="../" + "c" * 61),
        {"version": 1},
        {"client_endpoint_id": CLIENT_ENDPOINT},
        ["version", 1],
    ],
)
def test_malformed_request_is_refused(instance, body):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))

    response = _post(_signed_in(config), body)

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "invalid_request"}
    assert rendezvous._admissions == {}


def test_session_on_a_custom_hostname_stays_on_the_tunnel(instance, monkeypatch):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    monkeypatch.setattr(remote_access, "active_hostnames", lambda config: frozenset({"workbench.example.com"}))
    client = app.test_client()
    cookie = remote_session_cookie(config, "viewer@example.com", "sub-viewer", role="viewer", access_source="email")
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="workbench.example.com")

    response = _post(client, _body(), origin="https://workbench.example.com")

    assert response.status_code == 200
    assert response.get_json() == {
        "version": 1,
        "status": "unavailable",
        "reason": "hostname_not_direct",
        "instance_id": config.remote_access.vibe_cloud.instance_id,
        "hostname": "alex.avibe.bot",
        "retry_after_seconds": 60,
    }
    assert rendezvous._admissions == {}
