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


def test_public_trial_requires_owner_even_with_direct_addresses_and_fences_old_offers():
    rendezvous = DirectRendezvous(_Clock())
    ips = ("192.168.1.20:51234",)
    a = "https://aps1-1.relay.n0.iroh.link./"
    b = "https://use1-1.relay.n0.iroh.link./"
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ips))
    before = _offer(rendezvous)
    def owner_offer():
        return rendezvous.offer(instance_id="inst_123", hostname="alex.avibe.bot", subject="owner",
                                client_endpoint_id=CLIENT_ENDPOINT, is_owner=True)
    revisions = [before["generation"]]
    for url in (None, a, b, None):
        rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, ips, url, "public-trial-v1"))
        assert _offer(rendezvous, client="d" * 64)["status"] == "unavailable"
        assert not rendezvous.is_admitted("d" * 64)
        offer = owner_offer()
        assert offer["endpoint"]["relay"] == ({"policy": "public-trial-v1", "url": url} if url else None)
        revisions.append(offer["generation"])
    assert len(set(revisions)) == len(revisions)
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, (), a, "public-trial-v1"))
    assert owner_offer()["endpoint"]["addresses"] == []
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, (), None, "public-trial-v1"))
    assert owner_offer()["status"] == "unavailable"


def test_offer_and_renewal_logs_only_offer_counts_and_safe_reason(caplog):
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    rv = DirectRendezvous()
    relay = next(iter(direct_rendezvous.PUBLIC_TRIAL_RELAYS))
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.8:3210", "[2606:4700::1]:1234"), relay, "public-trial-v1"))
    args = dict(instance_id="private-instance", hostname="private.example", subject="private-subject", client_endpoint_id=CLIENT_ENDPOINT)
    result = rv.offer(**args, is_owner=True)
    rv.offer(**args, is_owner=True)
    rv.offer(**args, is_owner=False)
    rv.publish(None, reason=CLIENT_ENDPOINT)
    rv.offer(**args)
    lines = [r.getMessage() for r in caplog.records if r.name == "vibe.direct_rendezvous"]
    prefix = f"direct rendezvous generation={result['generation']} "
    assert lines[:2] == [prefix + "public_trial_v1=yes lan=1 ipv6=1 outcome=offered reason=none suppressed=0",
                         prefix + "public_trial_v1=no lan=0 ipv6=0 outcome=refused reason=owner_required suppressed=1"]
    assert len(lines) == 3 and lines[-1].endswith("outcome=refused reason=unavailable suppressed=0")
    for private in (*args.values(), INSTANCE_ENDPOINT, relay, "192.168", "2606:4700"):
        assert private not in "\n".join(lines)


@pytest.mark.parametrize("reason", ["transport_installing", "transport_install_failed"])
def test_install_refusal_log_preserves_fixed_reason(reason, caplog):
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    rv = DirectRendezvous()
    rv.publish(None, reason=reason)
    _offer(rv)
    assert caplog.messages[-1].endswith(f"outcome=refused reason={reason} suppressed=0")


def test_repeat_log_window_is_per_caller_and_keeps_permission_codes(monkeypatch, caplog):
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    now = [100.0]
    monkeypatch.setattr(direct_rendezvous.time, "monotonic", lambda: now[0])
    rv = DirectRendezvous()
    rv.publish(None, reason="permission_required")
    _offer(rv)
    for i in range(4):
        now[0] += 14
        _offer(rv, client=f"{i:064x}")
    _offer(rv, subject="another-caller")
    now[0] += 4
    _offer(rv)
    rv.publish(None, reason="permission_denied")
    _offer(rv)
    assert [r.getMessage().split("reason=")[1] for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
        "permission_required suppressed=0", "permission_required suppressed=0",
        "permission_required suppressed=4", "permission_denied suppressed=0",
    ]


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
    rendezvous.set_admission_listener(lambda endpoint_id, ttl, _epoch: (events.append((endpoint_id, ttl)), order.append("notice")))

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
    rendezvous.set_admission_listener(lambda endpoint_id, ttl, _epoch: events.append((endpoint_id, ttl)))
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


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_trial_rendezvous_withholds_peer_admission_and_relay_from_nonowners(instance, role):
    config, rendezvous = instance
    rendezvous.publish(DirectEndpoint(INSTANCE_ENDPOINT, (), "https://aps1-1.relay.n0.iroh.link./", "public-trial-v1"))
    payload = _post(_signed_in(config, role=role), _body()).get_json()
    assert payload["status"] == "unavailable" and "endpoint" not in payload
    assert not rendezvous.is_admitted(CLIENT_ENDPOINT)


def test_trial_consent_only_changes_from_host_shell(instance):
    from vibe import api, cli
    from vibe.authorization import AuthorizationContext, invocation_authority
    from config.v2_config import V2Config

    from vibe import connection_settings as cs

    config = V2Config.load()
    config.remote_access.vibe_cloud.instance_secret = 'test-pairing-credential'
    config.save()
    assert not cs.relay_enrolled(config)
    parser = cli.build_parser()
    def args(state):
        return parser.parse_args(["remote", "direct", "relay", "public-trial", state])

    assert not V2Config.load().remote_access.vibe_cloud.public_relay_trial
    with pytest.raises(ValueError, match="instance host shell"):
        api.save_config({"remote_access": {"vibe_cloud": {"public_relay_trial": True}}})
    with invocation_authority(AuthorizationContext(is_remote=True, instance_role="owner")):
        assert cli.cmd_remote_direct(args("on")) == 1
    assert not V2Config.load().remote_access.vibe_cloud.public_relay_trial
    assert cli.cmd_remote_direct(args("on")) == 0
    assert V2Config.load().remote_access.vibe_cloud.public_relay_trial
    with pytest.raises(ValueError, match="instance host shell"):
        api.save_config({"remote_access": {"vibe_cloud": {"public_relay_trial": False}}})
    assert cli.cmd_remote_direct(args("off")) == 0
    assert not V2Config.load().remote_access.vibe_cloud.public_relay_trial
    assert cs.relay_enrolled(V2Config.load())


def test_path_kind_comes_from_registered_sign_in_not_rendezvous_ua(instance, monkeypatch):
    from storage import device_registry
    from sqlalchemy.exc import OperationalError

    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    epochs = []
    rv.set_admission_listener(lambda peer, ttl, epoch: epochs.append(epoch))
    for i, (ua, expected) in enumerate((("AvibeDesktop/3.2.9-rc.3", "desktop"),
                                         ("AvibeiOS/56.1", "ios"), ("Safari/1", "browser"), ("", "unknown"))):
        peer = f"{i:064x}"
        cookie = remote_access.make_session_cookie(config, "viewer@example.com", "sub-viewer", session_claims={
            "vibe_instance_id": config.remote_access.vibe_cloud.instance_id, "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "email",
        }, device_headers={"User-Agent": ua})
        client = app.test_client()
        client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
        response = _post(client, _body(peer), headers={**csrf_headers(client, ORIGIN), "User-Agent": "Chrome/1"})
        assert response.get_json()["status"] == "ready"
        assert rv.observe_path(peer, epochs[-1], 1, "open", None) == expected
        assert rv.observe_path(peer, 0, 1, "close", None) == "unknown"
    def unavailable(*args):
        raise OperationalError(None, None, OSError())
    monkeypatch.setattr(device_registry, "session_kind", unavailable)
    assert _post(client, _body(peer)).get_json()["status"] == "ready"
    assert rv.observe_path(peer, epochs[-1], 1, "close", None) == "unknown"


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
    (grant,) = rendezvous._admissions.values()
    assert rendezvous.is_admitted(CLIENT_ENDPOINT)
    assert (grant.subject, grant.until) == ("sub-viewer", NOW + RENDEZVOUS_TTL_SECONDS)


@pytest.mark.parametrize("steps", [("bind", "waiting", "ownership", "deadline", "foreign"), ("socket",),
                                   # A duration, never negative: after two other entries its codes here are 0 and 1.
                                   ("bind", "bind", "late", "late")])
def test_listener_faults_log_only_the_three_fields_without_changing_the_offer(instance, caplog, steps):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready" and "listener_faults" not in baseline
    assert not any("listener_fault" in line for line in caplog.messages)
    caplog.clear()
    faults = [{"step": step, "code": i - 2, "background_seconds": i} for i, step in enumerate(steps)]
    response = _post(client, {**_body(), "listener_faults": faults})
    assert response.status_code == 200 and response.get_json() == baseline
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
        ("INFO", f"direct rendezvous listener_fault step={step} code={i - 2} background_seconds={i}")
        for i, step in enumerate(steps)
    ]


def test_invalid_listener_fault_batches_are_ignored_in_full(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready"
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    good = {"step": "bind", "code": -1, "background_seconds": 0}
    for faults in (None, [], good, [good] * 6, [good, None], [good, {"step": "bind"}],
                   [good, {**good, "extra": "private"}], [good, {**good, "step": "unknown"}],
                   [good, {**good, "step": []}], [good, {**good, "code": False}],
                   [good, {**good, "code": 1.5}], [good, {**good, "background_seconds": True}],
                   [good, {**good, "code": -(2**31) - 1}], [good, {**good, "code": 2**31}],
                   [good, {**good, "background_seconds": 2**31}],
                   [good, {**good, "background_seconds": "1"}], [good, {**good, "background_seconds": -1}],
                   # A late first bind carries how long it took: not a negative number.
                   [good, {**good, "step": "late", "code": -1}]):
        response = _post(client, {**_body(), "listener_faults": faults})
        assert response.status_code == 200 and response.get_json() == baseline
    assert not any("listener_fault" in line for line in caplog.messages)


def _leg_fault(**changes):
    return {"stage": "tls", "code": "closed_by_page", "path": "relay", "strict": False, "ms": 650, "count": 17, **changes}


def test_leg_faults_log_only_their_fixed_words_and_numbers_without_changing_the_offer(instance, caplog):
    from vibe import direct_rendezvous

    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready" and "leg_faults" not in baseline
    assert not any("leg_fault" in line for line in caplog.messages)
    caplog.clear()
    faults = [
        _leg_fault(),
        _leg_fault(stage="dial", code="timeout", path="none", ms=0, count=1),
        _leg_fault(stage="revert", code="unreachable", path="none", strict=True, ms=2**31 - 1, count=2**31 - 1),
        _leg_fault(stage="connect", code="status", path="lan", strict=True, ms=12, count=3),
        _leg_fault(stage="tls", code="lost_race", path="punched", ms=40, count=2),
    ]
    response = _post(client, {**_body(), "leg_faults": faults})
    assert response.status_code == 200 and response.get_json() == baseline
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
        ("INFO", "direct rendezvous leg_fault stage=tls code=closed_by_page path=relay strict=no ms=650 count=17"),
        ("INFO", "direct rendezvous leg_fault stage=dial code=timeout path=none strict=no ms=0 count=1"),
        ("INFO", "direct rendezvous leg_fault stage=revert code=unreachable path=none strict=yes "
                 f"ms={2**31 - 1} count={2**31 - 1}"),
        ("INFO", "direct rendezvous leg_fault stage=connect code=status path=lan strict=yes ms=12 count=3"),
        ("INFO", "direct rendezvous leg_fault stage=tls code=lost_race path=punched strict=no ms=40 count=2"),
    ]
    # Every word of the contract is accepted, one entry at a time.
    for key, words in (("stage", direct_rendezvous._LEG_FAULT_STAGES), ("code", direct_rendezvous._LEG_FAULT_CODES),
                       ("path", direct_rendezvous._LEG_FAULT_PATHS)):
        for word in words:
            caplog.clear()
            _post(client, {**_body(), "leg_faults": [_leg_fault(**{key: word})]})
            assert len([line for line in caplog.messages if f" {key}={word} " in line]) == 1, (key, word)
    assert direct_rendezvous._LEG_FAULT_STAGES == (
        "rendezvous", "dial", "open", "connect", "tls", "health", "handover", "revert")
    assert direct_rendezvous._LEG_FAULT_CODES == (
        "timeout", "refused", "unreachable", "denied", "status", "closed_by_page", "closed_by_peer",
        "lost_race", "proxy_auth", "expired", "other", "invalid_offer", "no_permitted_path",
        "listener_closed", "superseded", "not_ready", "connection_ended", "invalid_head",
        "not_ready_certificate", "not_ready_owner_required", "not_ready_not_running", "not_ready_permission",
        "not_ready_transport", "not_ready_disabled", "not_ready_no_address", "not_ready_other",
        "path_unselected",
        "path_refused_relay", "path_refused_private", "path_refused_cgnat", "path_refused_ula",
        "path_refused_linklocal", "path_refused_loopback", "path_refused_public", "path_refused_global",
        "path_refused_nat64", "path_refused_mapped", "path_refused_other")
    assert direct_rendezvous._LEG_FAULT_PATHS == ("lan", "punched", "relay", "none")
    # It rides beside the iPhone's fields without either changing the other.
    caplog.clear()
    both = _post(client, {**_body(), "leg_faults": [_leg_fault()],
                          "listener_faults": [{"step": "bind", "code": 48, "background_seconds": 0}]})
    assert both.get_json() == baseline
    assert sorted(line.split()[2] for line in caplog.messages if "_fault " in line) == ["leg_fault", "listener_fault"]


def test_invalid_leg_fault_batches_are_ignored_in_full(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready"
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    good = _leg_fault()
    missing = {key: value for key, value in good.items() if key != "count"}
    for faults in (None, [], good, "tls", [good] * 6, [good, None], [good, missing],
                   [good, {**good, "host": "private.example"}],
                   [good, {**good, "stage": "unknown"}], [good, {**good, "stage": []}], [good, {**good, "stage": 1}],
                   [good, {**good, "stage": "tls\nforged line"}],
                   [good, {**good, "code": "192.168.1.20:51234"}], [good, {**good, "code": {}}],
                   [good, {**good, "code": 502}],
                   [good, {**good, "path": "ipv6"}], [good, {**good, "path": None}], [good, {**good, "path": ["lan"]}],
                   [good, {**good, "strict": 1}], [good, {**good, "strict": "no"}], [good, {**good, "strict": None}],
                   [good, {**good, "ms": -1}], [good, {**good, "ms": 2**31}], [good, {**good, "ms": 1.5}],
                   [good, {**good, "ms": True}], [good, {**good, "ms": "650"}],
                   [good, {**good, "count": 0}], [good, {**good, "count": 2**31}], [good, {**good, "count": 2.0}],
                   [good, {**good, "count": True}], [good, {**good, "count": "17"}]):
        response = _post(client, {**_body(), "leg_faults": faults})
        assert response.status_code == 200 and response.get_json() == baseline, faults
    assert not any("leg_fault" in line for line in caplog.messages)


def test_leg_faults_change_no_answer_and_keep_nothing(instance, caplog):
    config, rv = instance
    client = _signed_in(config)
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    # Reported while the instance has no endpoint: the answer is the same
    # unavailable one, and the entries are logged all the same.
    without = _post(client, _body()).get_json()
    assert without["status"] == "unavailable"
    caplog.clear()
    assert _post(client, {**_body(), "leg_faults": [_leg_fault()]}).get_json() == without
    assert sum("leg_fault" in line for line in caplog.messages) == 1
    # Nothing of a report is kept: the next request logs none.
    caplog.clear()
    assert _post(client, _body()).get_json() == without
    assert not any("leg_fault" in line for line in caplog.messages)
    assert "leg_fault" not in repr(vars(rv))
    # A request the route refuses reports nothing.
    caplog.clear()
    refused = _post(client, {"version": 2, "client_endpoint_id": CLIENT_ENDPOINT, "leg_faults": [_leg_fault()]})
    assert refused.status_code == 400
    assert not any("leg_fault" in line for line in caplog.messages)


def _path_use(**changes):
    entry = {"client": "desktop", "age_min": 1, "path": "relay", "handover_rule": "none", "retired_rule": "none"}
    entry.update({key: 0 for key in direct_rendezvous._PATH_USE_COUNTS + direct_rendezvous._PATH_USE_MILLISECONDS})
    return {**entry, **changes}


def test_path_use_logs_only_its_fixed_words_and_numbers_without_changing_the_offer(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready" and "path_use" not in baseline
    assert not any("path_use" in line for line in caplog.messages)
    caplog.clear()
    entries = [
        # Seventeen connections: the tunnel won every race against the relay.
        _path_use(tunnel=17, raced=17, tunnel_won=17, tunnel_ms=140, handover_asked=3, handover_refused=3,
                  handover_rule="not_usable", retired=1, retired_rule="no_permitted_path"),
        _path_use(client="ios", app="56.1", age_min=0, path="none", tunnel=4, no_direct=4, retired_rule="background", retired=2),
        _path_use(age_min=1440, app="3.2.9-rc.3", path="lan", direct=2**31 - 1, sticky=2, direct_ms=2**31 - 1, strict=1,
                  tunnel_only=1, direct_failed=1, direct_timed_out=1, direct_won=1, handover_done=1,
                  handover_rule="other_documents", retired_rule="lease"),
    ]
    response = _post(client, {**_body(), "path_use": entries})
    assert response.status_code == 200 and response.get_json() == baseline
    assert [(r.levelname, r.getMessage()) for r in caplog.records
            if r.name == "vibe.direct_rendezvous" and "path_use" in r.getMessage()] == [
        ("INFO", "direct rendezvous path_use client=desktop age_min=1 path=relay"
                 " direct=0 tunnel=17 raced=17 direct_won=0 tunnel_won=17 direct_ms=0 tunnel_ms=140"
                 " no_direct=0 sticky=0 strict=0 tunnel_only=0 direct_failed=0 direct_timed_out=0"
                 " handover_asked=3 handover_done=0 handover_refused=3 handover_rule=not_usable"
                 " retired=1 retired_rule=no_permitted_path"),
        ("INFO", "direct rendezvous path_use client=ios age_min=0 path=none"
                 " direct=0 tunnel=4 raced=0 direct_won=0 tunnel_won=0 direct_ms=0 tunnel_ms=0"
                 " no_direct=4 sticky=0 strict=0 tunnel_only=0 direct_failed=0 direct_timed_out=0"
                 " handover_asked=0 handover_done=0 handover_refused=0 handover_rule=none"
                 " retired=2 retired_rule=background app=56.1"),
        ("INFO", "direct rendezvous path_use client=desktop age_min=1440 path=lan"
                 f" direct={2**31 - 1} tunnel=0 raced=0 direct_won=1 tunnel_won=0 direct_ms={2**31 - 1} tunnel_ms=0"
                 " no_direct=0 sticky=2 strict=1 tunnel_only=1 direct_failed=1 direct_timed_out=1"
                 " handover_asked=0 handover_done=1 handover_refused=0 handover_rule=other_documents"
                 " retired=0 retired_rule=lease app=3.2.9-rc.3"),
    ]
    # Every word of the contract is accepted, one entry at a time, and the lists are pinned.
    for key, words in (("client", direct_rendezvous._PATH_USE_CLIENTS), ("path", direct_rendezvous._LEG_FAULT_PATHS),
                       ("handover_rule", direct_rendezvous._PATH_USE_HANDOVER_RULES),
                       ("retired_rule", direct_rendezvous._PATH_USE_RETIRED_RULES)):
        for word in words:
            caplog.clear()
            _post(client, {**_body(), "path_use": [_path_use(**{key: word})]})
            assert sum(f" {key}={word}" in f"{line} " and "path_use" in line for line in caplog.messages) == 1, (key, word)
    assert direct_rendezvous._PATH_USE_CLIENTS == ("ios", "desktop")
    assert direct_rendezvous._PATH_USE_COUNTS == (
        "direct", "tunnel", "raced", "direct_won", "tunnel_won", "no_direct", "sticky", "strict", "tunnel_only",
        "direct_failed", "direct_timed_out", "handover_asked", "handover_done", "handover_refused", "retired")
    assert direct_rendezvous._PATH_USE_MILLISECONDS == ("direct_ms", "tunnel_ms")
    assert direct_rendezvous._PATH_USE_HANDOVER_RULES == (
        "none", "not_usable", "other_documents", "relay_path", "spacing", "cap", "fruitless")
    assert direct_rendezvous._PATH_USE_RETIRED_RULES == (
        "none", "path_monitor", "lease", "revocation", "strict_revert", "idle", "background",
        "network_change") + direct_rendezvous._LEG_FAULT_CODES
    # Ten entries are taken; it rides beside the other reports.
    caplog.clear()
    both = _post(client, {**_body(), "path_use": [_path_use()] * 10, "leg_faults": [_leg_fault()]})
    assert both.get_json() == baseline
    assert sum("path_use" in line for line in caplog.messages) == 10
    assert sum("leg_fault" in line for line in caplog.messages) == 1


def test_invalid_path_use_batches_are_ignored_in_full(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    good = _path_use()
    missing = {key: value for key, value in good.items() if key != "retired"}
    for entries in (None, [], good, "relay", [good] * 11, [good, None], [good, missing],
                    [good, {**good, "host": "private.example"}],
                    [good, {**good, "client": "android"}], [good, {**good, "client": 1}],
                    [good, {**good, "path": "ipv6"}], [good, {**good, "path": []}],
                    [good, {**good, "handover_rule": "192.168.1.20:51234"}], [good, {**good, "handover_rule": {}}],
                    [good, {**good, "retired_rule": "relay\nforged line"}], [good, {**good, "retired_rule": None}],
                    [good, {**good, "age_min": -1}], [good, {**good, "age_min": 1441}], [good, {**good, "age_min": True}],
                    [good, {**good, "age_min": 1.0}],
                    [good, {**good, "tunnel": -1}], [good, {**good, "tunnel": 2**31}], [good, {**good, "tunnel": 1.5}],
                    [good, {**good, "tunnel": True}], [good, {**good, "tunnel": "17"}],
                    [good, {**good, "direct_ms": -1}], [good, {**good, "tunnel_ms": 2**31}],
                    [good, {**good, "retired": None}], [good, {**good, "handover_asked": [1]}]):
        response = _post(client, {**_body(), "path_use": entries})
        assert response.status_code == 200 and response.get_json() == baseline, entries
    assert not any("path_use" in line for line in caplog.messages)
    for value in (None, 1, [], "", "x" * 33, "a\nb", "a\tb", "\x7f", "é"):
        response = _post(client, {**_body(), "path_use": [good, {**good, "app": value}]})
        assert response.get_json() == baseline
    assert not any("path_use" in line for line in caplog.messages)
    # A 32-byte build with printable punctuation cannot forge another log field.
    unusual = "x" * 24 + " path=/%"
    _post(client, {**_body(), "path_use": [{**good, "app": unusual}]})
    assert any(line.endswith(" app=" + "x" * 24 + "%20path%3D%2F%25") for line in caplog.messages)
    caplog.clear()
    # Each count and each time is checked on its own.
    for key in direct_rendezvous._PATH_USE_COUNTS + direct_rendezvous._PATH_USE_MILLISECONDS:
        _post(client, {**_body(), "path_use": [{**good, key: -1}]})
    assert not any("path_use" in line for line in caplog.messages)


def test_public_fault_and_route_fallback_counts_are_bounded_independent_receipts(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready"
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    causes = ["address_benchmark", "address_private", "address_other", "dns_failed", "connect_timeout",
              "connect_refused", "connect_other"]
    faults = [{"cause": cause, "count": 1} for cause in causes]
    reasons = ["open_failed", "open_timed_out", "retry"]
    fallbacks = [{"reason": reason, "count": 2**31-1} for reason in reasons]

    def ask(a, b):
        caplog.clear()
        reply = _post(client, {**_body(), "public_faults": a, "route_fallbacks": b, "path_use": [_path_use()]})
        assert reply.status_code == 200 and reply.get_json() == baseline
        assert any("direct rendezvous path_use " in line for line in caplog.messages)
        return [line for line in caplog.messages if line.startswith(("direct rendezvous public_fault ",
                                                                     "direct rendezvous route_fallback "))]

    fault_lines = [f"direct rendezvous public_fault cause={cause} count=1" for cause in causes]
    fallback_lines = [f"direct rendezvous route_fallback reason={reason} count=2147483647" for reason in reasons]
    assert ask(faults[:5], fallbacks) == fault_lines[:5] + fallback_lines
    assert ask(faults[5:], []) == fault_lines[5:]
    assert ask(faults[:1] * 2, fallbacks) == fallback_lines
    for key, valid in (("cause", faults[0]), ("reason", fallbacks[0])):
        for bad in (None, [], valid, [valid] * 6, [valid, None], [valid, {key: valid[key]}],
                    [valid, {**valid, "host": "example.invalid"}], [valid, {**valid, key: []}],
                    [valid, {**valid, key: "unexpected"}],
                    *([valid, {**valid, "count": count}] for count in (False, True, 0, -1, 1.0, "1", 2**31))):
            if key == "cause":
                assert ask(bad, fallbacks) == fallback_lines
            else:
                assert ask(faults[:1], bad) == fault_lines[:1]


def test_path_guard_batch_is_independent_of_path_use_and_the_offer(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    assert baseline["status"] == "ready"
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    clear = dict(client="ios", age_min=0, path="none", guard_level_s=0, guard_rose="none", held=0)
    held = dict(client="desktop", age_min=1440, path="relay", guard_level_s=600, guard_rose="probation", held=2**31-1)
    raised = {**held, "path": "lan", "guard_level_s": 30, "guard_rose": "adopted", "held": 1}

    def ask(entries):
        caplog.clear()
        response = _post(client, {**_body(), "path_use": [_path_use()], "path_guard": entries})
        assert response.status_code == 200 and response.get_json() == baseline
        assert sum("direct rendezvous path_use " in line for line in caplog.messages) == 1
        return [line for line in caplog.messages if "direct rendezvous path_guard " in line]

    assert ask([clear] * 8 + [raised, held]) == [
        "direct rendezvous path_guard client=ios age_min=0 path=none guard_level_s=0 guard_rose=none held=0"
    ] * 8 + [
        "direct rendezvous path_guard client=desktop age_min=1440 path=lan guard_level_s=30 guard_rose=adopted held=1",
        "direct rendezvous path_guard client=desktop age_min=1440 path=relay guard_level_s=600 guard_rose=probation held=2147483647",
    ]
    for invalid in (None, [], clear, [clear] * 11, [clear, None], [clear, {}],
                    *([clear, {**clear, key: value}] for key, value in (
                        ("extra", "x"), ("client", []), ("path", {}), ("guard_level_s", False),
                        ("guard_level_s", 30.0), ("guard_level_s", 5), ("guard_level_s", 600),
                        ("guard_rose", "adopted"), ("guard_rose", "unknown"), ("guard_rose", []),
                        ("age_min", True), ("age_min", -1), ("age_min", 1441),
                        ("held", False), ("held", 1.0), ("held", -1), ("held", 2**31)))):
        assert ask(invalid) == []


def test_startup_is_bounded_and_independent_of_the_offer_and_path_use(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    cached = dict(bind_ms=240, visible_ms=320, late=False, visible="cached")
    live = dict(bind_ms=2**31-1, visible_ms=0, late=True, visible="live")
    def ask(entries):
        caplog.clear()
        response = _post(client, {**_body(), "path_use": [_path_use(app="56.1")], "startup": entries})
        assert response.get_json() == baseline
        assert any(" path_use " in line and line.endswith(" app=56.1") for line in caplog.messages)
        return [line for line in caplog.messages if " startup " in line]
    first = "direct rendezvous startup bind_ms=240 visible_ms=320 late=no visible=cached"
    last = "direct rendezvous startup bind_ms=2147483647 visible_ms=0 late=yes visible=live"
    assert ask([cached] * 4 + [live]) == [first] * 4 + [last]
    for invalid in (None, [], cached, [cached] * 6, [cached, None], [cached, {"late": True}],
                    *([cached, {**cached, key: value}] for key, value in (
                        ("app", "56.1"), ("bind_ms", True), ("bind_ms", -1), ("visible_ms", 2**31),
                        ("visible_ms", 0.5), ("late", 1), ("visible", {}), ("visible", "none")))):
        assert ask(invalid) == []


def test_handovers_log_each_ask_in_order_without_changing_the_offer(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    first = dict(age_s=86400, ms=2**31 - 1, refused="none", other="none", closed=2**31 - 1, kept=1, path="lan")
    last = dict(age_s=0, ms=0, refused="other_documents", other="unloaded", closed=0, kept=0, path="relay")
    answer = _post(client, {**_body(), "handovers": [first] * 7 + [last]})
    assert answer.status_code == 200 and answer.get_json() == baseline
    assert [r.getMessage() for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
        "direct rendezvous handover age_s=86400 ms=2147483647 refused=none other=none closed=2147483647 kept=1 path=lan"
    ] * 7 + ["direct rendezvous handover age_s=0 ms=0 refused=other_documents other=unloaded closed=0 kept=0 path=relay"]


def test_malformed_handover_batches_leave_other_diagnostics_intact(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    good = dict(age_s=0, ms=1, refused="not_usable", other="none", closed=0, kept=0, path="none")
    invalid = [None, [], good, [good] * 9, [good, None], [good, {}], [good, {**good, "app": "1"}]]
    for key in ("age_s", "ms", "closed", "kept"):
        for value in (True, 0.5, -1, 86401 if key == "age_s" else 2**31):
            invalid.append([good, {**good, key: value}])
    invalid.extend([[good, {**good, "refused": refused, "other": other}] for refused, other in (
        ("none", "voice"), ("not_usable", "unknown"), ("other_documents", "none"),
    )])
    for key in ("refused", "other", "path"):
        for value in ({}, None, "unrecognized"):
            invalid.append([good, {**good, key: value}])
    for entries in invalid:
        caplog.clear()
        answer = _post(client, {**_body(), "handovers": entries, "slow_store_removals_ms": [10]})
        assert answer.status_code == 200 and answer.get_json() == baseline
        assert [r.getMessage() for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
            "direct rendezvous slow_store_removal ms=10"
        ]


def test_slow_store_removals_are_logged_in_milliseconds_without_changing_the_offer(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    response = _post(client, {**_body(), "slow_store_removals_ms": [1200, 485000]})
    assert response.status_code == 200 and response.get_json() == baseline
    assert [r.getMessage() for r in caplog.records if r.name == "vibe.direct_rendezvous"] == [
        "direct rendezvous slow_store_removal ms=1200",
        "direct rendezvous slow_store_removal ms=485000",
    ]


def test_invalid_slow_store_removal_batches_are_ignored_in_full(instance, caplog):
    config, rv = instance
    rv.publish(DirectEndpoint(INSTANCE_ENDPOINT, ("192.168.1.20:51234",)))
    client = _signed_in(config)
    baseline = _post(client, _body()).get_json()
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    for durations in (None, [], 1200, [1200] * 6, [1200, None], [1200, -1], [1200, 2**31],
                      [1200, 1.5], [1200, True], [1200, "1"]):
        response = _post(client, {**_body(), "slow_store_removals_ms": durations})
        assert response.status_code == 200 and response.get_json() == baseline
    assert not any("slow_store_removal" in line for line in caplog.messages)


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
