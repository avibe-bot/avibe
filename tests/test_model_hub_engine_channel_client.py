"""An installed Avibe's use of the engine channel (vibe/model_hub_runtime/engine_channel.py).

What this protects: an Avibe release with no shipped key has no channel at
all; an engine manifest override or offline mode keeps the channel from
choosing an engine; the recorded index keeps deciding for as long as it is the
newest this machine has; and reading the channel never installs, selects, or
starts anything.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.dependency_network import DependencyNetworkError
from core.model_hub_engine_channel import ChannelError, ResolutionAction, TrustRoots
from tests.engine_channel_support import (
    LocalVerifier,
    Signer,
    manifest_bytes,
    release_entry,
    signed,
    tombstone,
)
from vibe.model_hub_runtime import engine_channel
from vibe.model_hub_runtime.engine_channel import EngineChannel
from vibe.model_hub_runtime.installer import EngineRuntimeManager

BUNDLED = json.loads(
    (Path(engine_channel.__file__).with_name("cliproxyapi_manifest.json")).read_text(encoding="utf-8")
)["asset_release_tag"]
VERSION, BUILD = BUNDLED.removeprefix("model-hub-engine-").rsplit("-", 1)
NEWER = f"model-hub-engine-{VERSION}-{int(BUILD) + 1}"


class Fetch:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _channel(tmp_path, signer: Signer | None, body: bytes | Exception = b"", **options) -> tuple[EngineChannel, Fetch]:
    fetch = Fetch(body)
    manager = options.pop("manager", None) or EngineRuntimeManager(runtime_dir=tmp_path / "engine")
    options.setdefault("verifier", LocalVerifier())
    options.setdefault("avibe_version", "3.2.6")
    if signer is not None:
        options.setdefault("roots", signer.roots())
    return EngineChannel(manager, fetch=fetch, **options), fetch


def _index(signer: Signer, sequence: int = 1, *, revoked: list | None = None, **fields) -> bytes:
    releases = [
        release_entry(manifest_bytes(VERSION, int(BUILD) - 1 or 1)) if int(BUILD) > 1 else None,
        release_entry(manifest_bytes(VERSION, int(BUILD) + 1)),
        release_entry(manifest_bytes(VERSION, int(BUILD) + 2)),
    ]
    return signed(signer, sequence, releases=[item for item in releases if item], revoked=revoked, **fields)


def test_this_avibe_ships_no_channel_key_so_the_channel_is_off(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-005: without a shipped key there is no request, no verifier call, no selection."""
    # Commissioning the channel is a reviewed change to this document; until
    # then nothing downloads, verifies, or selects.
    assert json.loads(engine_channel.SHIPPED_TRUST_ROOTS_PATH.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "sequence_floor": 0,
        "roots": [],
    }
    verifier = LocalVerifier()
    channel, fetch = _channel(tmp_path, None, _index(Signer()), verifier=verifier)

    status = channel.refresh()

    assert (status.state, status.reason) == ("uncommissioned", "uncommissioned")
    assert channel.snapshot() is None
    assert channel.resolve(current=BUNDLED).action is ResolutionAction.KEEP
    assert channel.resolve(current=BUNDLED).reason == "uncommissioned"
    assert fetch.calls == 0 and verifier.calls == 0
    assert not (tmp_path / "engine").exists()


def test_unreadable_shipped_roots_leave_the_channel_off(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "roots.json"
    broken.write_text('{"schema_version": 1, "sequence_floor": 0, "roots": [{"key_id": "k"}]}')
    monkeypatch.setattr(engine_channel, "SHIPPED_TRUST_ROOTS_PATH", broken)
    assert engine_channel.shipped_trust_roots() == TrustRoots()
    monkeypatch.setattr(engine_channel, "SHIPPED_TRUST_ROOTS_PATH", tmp_path / "absent.json")
    assert engine_channel.shipped_trust_roots() == TrustRoots()


def test_a_refresh_records_the_index_and_resolves_without_touching_the_installed_engine(tmp_path) -> None:
    signer = Signer()
    channel, fetch = _channel(tmp_path, signer, _index(signer, 4))

    first, second = channel.refresh(), channel.refresh()

    assert (first.state, first.sequence, first.changed) == ("accepted", 4, True)
    assert (second.state, second.changed) == ("accepted", False)
    resolution = channel.resolve(current=BUNDLED)
    assert resolution.action is ResolutionAction.ADOPT
    assert resolution.target.asset_release_tag == f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    assert resolution.target.manifest_bytes == manifest_bytes(VERSION, int(BUILD) + 2)
    assert channel.bundled_release_tag() == BUNDLED
    # A decision only: no pointer, install claim, download, or version directory appeared.
    assert sorted(path.name for path in (tmp_path / "engine").iterdir()) == ["channel"]
    assert fetch.calls == 2


@pytest.mark.parametrize(
    "override",
    [
        lambda root: {"manifest_path": root / "override.json"},
        lambda root: {"manifest_url": "https://example.invalid/manifest.json"},
    ],
)
def test_an_engine_manifest_override_opts_out_of_the_channel(tmp_path, override) -> None:
    """MH-ENGINE-CHANNEL-006: a manifest override names the engine by itself."""

    signer = Signer()
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "engine", **override(tmp_path))
    channel, fetch = _channel(tmp_path, signer, _index(signer), manager=manager)

    status = channel.refresh()

    assert (status.state, status.reason) == ("opted_out", "manifest_override")
    assert channel.snapshot() is None
    assert (channel.resolve(current=BUNDLED).action, channel.resolve(current=BUNDLED).reason) == (
        ResolutionAction.KEEP,
        "manifest_override",
    )
    assert fetch.calls == 0


def test_the_test_harness_manifest_environment_override_also_opts_out(tmp_path, monkeypatch) -> None:
    signer = Signer()
    monkeypatch.setenv("VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH", str(tmp_path / "offline-manifest.json"))
    channel, fetch = _channel(tmp_path, signer, _index(signer))

    assert channel.refresh().state == "opted_out" and fetch.calls == 0


def test_offline_mode_uses_no_network_and_keeps_recorded_withdrawals_in_force(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-006: offline mode adopts nothing and still applies a recorded withdrawal."""

    signer = Signer()
    online, _ = _channel(tmp_path, signer, _index(signer, 2, revoked=[tombstone(NEWER, f"model-hub-engine-{VERSION}-{int(BUILD) + 2}")]))
    assert online.refresh().state == "accepted"

    offline_manager = EngineRuntimeManager(runtime_dir=tmp_path / "engine", offline=True)
    offline, fetch = _channel(tmp_path, signer, _index(signer, 3), manager=offline_manager)

    assert offline.refresh().state == "offline" and fetch.calls == 0
    assert (offline.resolve(current=BUNDLED).action, offline.resolve(current=BUNDLED).reason) == (
        ResolutionAction.KEEP,
        "offline",
    )
    withdrawn = offline.resolve(current=NEWER)
    assert withdrawn.action is ResolutionAction.WITHDRAW and withdrawn.download_authorized is False


@pytest.mark.parametrize(
    ("body", "state", "reason"),
    [
        (DependencyNetworkError({"kind": "dns", "message": "DNS lookup failed"}), "unavailable", "dns"),
        (OSError("connection reset"), "unavailable", "network"),
        (b"not json", "rejected", "envelope_invalid"),
        (b"", "rejected", "envelope_invalid"),
    ],
)
def test_a_failed_refresh_reports_why_and_keeps_what_was_recorded(tmp_path, body, state, reason) -> None:
    signer = Signer()
    good, _ = _channel(tmp_path, signer, _index(signer, 5))
    good.refresh()
    recorded = (tmp_path / "engine" / "channel" / "state.json").read_bytes()
    channel, _ = _channel(tmp_path, signer, body)

    status = channel.refresh()

    assert (status.state, status.reason) == (state, reason)
    assert (tmp_path / "engine" / "channel" / "state.json").read_bytes() == recorded
    assert channel.resolve(current=BUNDLED).action is ResolutionAction.ADOPT


def test_an_index_another_key_signed_or_an_older_one_is_rejected(tmp_path) -> None:
    signer = Signer()
    channel, _ = _channel(tmp_path, signer, _index(signer, 5))
    channel.refresh()

    assert _channel(tmp_path, signer, _index(Signer(), 6))[0].refresh().reason == "signature_invalid"
    assert _channel(tmp_path, signer, _index(signer, 4))[0].refresh().reason == "replayed"
    assert channel.snapshot().payload.sequence == 5


def test_an_avibe_upgrade_that_raises_the_floor_stops_adoption_from_the_older_recorded_index(tmp_path) -> None:
    signer = Signer()
    withdrawal = [tombstone(NEWER, f"model-hub-engine-{VERSION}-{int(BUILD) + 2}")]
    before, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=withdrawal))
    assert before.refresh().state == "accepted"
    assert before.resolve(current=BUNDLED).action is ResolutionAction.ADOPT

    upgraded, _ = _channel(tmp_path, signer, roots=signer.roots(floor=100))

    resolution = upgraded.resolve(current=BUNDLED)
    assert (resolution.action, resolution.reason) == (ResolutionAction.KEEP, "index_below_floor")
    # The recorded withdrawal still applies; it authorizes no download.
    withdrawn = upgraded.resolve(current=NEWER)
    assert withdrawn.action is ResolutionAction.WITHDRAW and withdrawn.download_authorized is False
    # Offline is a different reason for the same restraint, and is the one named when both apply.
    offline_manager = EngineRuntimeManager(runtime_dir=tmp_path / "engine", offline=True)
    both, _ = _channel(tmp_path, signer, manager=offline_manager, roots=signer.roots(floor=100))
    assert (both.resolve(current=BUNDLED).action, both.resolve(current=BUNDLED).reason) == (
        ResolutionAction.KEEP,
        "offline",
    )


LONG_AGO = datetime(2001, 1, 1, tzinfo=timezone.utc)


def test_the_recorded_index_keeps_deciding_when_no_newer_one_can_be_fetched(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-013: an index accepted once is this machine's index until a higher sequence arrives."""

    signer = Signer()
    newest = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    # Issued long before any clock this test could run under.
    old = _index(signer, 5, issued_at=LONG_AGO)
    assert _channel(tmp_path, signer, old)[0].refresh().state == "accepted"
    recorded = (tmp_path / "engine" / "channel" / "state.json").read_bytes()

    unreachable, fetch = _channel(tmp_path, signer, OSError("connection reset"))
    assert unreachable.refresh().state == "unavailable" and fetch.calls == 1
    resolution = unreachable.resolve(current=BUNDLED)

    # Nothing aged out: the same index still selects, and may still be downloaded from.
    assert (resolution.action, resolution.target.asset_release_tag) == (ResolutionAction.ADOPT, newest)
    assert resolution.download_authorized is True
    assert unreachable.snapshot().payload.issued_at == LONG_AGO
    # The same bytes offered again are accepted as what the machine already has.
    again, _ = _channel(tmp_path, signer, old)
    status = again.refresh()
    assert (status.state, status.sequence, status.changed) == ("accepted", 5, False)
    assert (tmp_path / "engine" / "channel" / "state.json").read_bytes() == recorded


def test_no_clock_is_read_to_accept_an_index_or_to_decide_from_it(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-CHANNEL-013: an index cannot age, by its date or by how long ago it was accepted.

    The other cases show that an index dated long ago or far ahead is
    accepted. A limit on how long ago it was accepted could not show there,
    and would need the time: so the time is taken away.
    """

    from core import model_hub_engine_channel as contract

    class NoClock(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("the engine channel read the wall clock")

        utcnow = today = now

    monkeypatch.setattr(contract, "datetime", NoClock)
    monkeypatch.setattr(engine_channel, "datetime", NoClock, raising=False)
    signer = Signer()
    newest = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    channel, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=[tombstone(NEWER, newest)]))

    assert channel.refresh().state == "accepted"
    assert channel.snapshot().payload.sequence == 5
    assert channel.resolve(current=BUNDLED).action is ResolutionAction.ADOPT
    assert channel.resolve(current=NEWER).action is ResolutionAction.WITHDRAW
    # Neither hold is decided by the time either.
    offline, _ = _channel(
        tmp_path, signer, manager=EngineRuntimeManager(runtime_dir=tmp_path / "engine", offline=True)
    )
    below_floor, _ = _channel(tmp_path, signer, roots=signer.roots(floor=100))
    assert [reader.resolve(current=BUNDLED).reason for reader in (offline, below_floor)] == [
        "offline",
        "index_below_floor",
    ]


def test_a_machine_that_never_receives_a_withdrawal_keeps_following_the_index_it_has(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-013: the limit of having no expiry, stated as behaviour.

    The signature shows an index was authorized and the sequence stops a
    machine going back from one it has seen. Neither shows that a newer index
    was received.
    """

    signer = Signer()
    engine_b = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    ten = _index(signer, 10, issued_at=LONG_AGO)
    eleven = _index(signer, 11, revoked=[tombstone(engine_b, NEWER)])
    # This machine saw sequence 10, which lists B, and can no longer reach the index.
    behind, _ = _channel(tmp_path / "behind", signer, ten)
    assert behind.refresh().state == "accepted"
    cut_off, _ = _channel(tmp_path / "behind", signer, OSError("no route to host"))
    assert cut_off.refresh().state == "unavailable"

    still_adopts = cut_off.resolve(current=BUNDLED)

    # It may newly install B, which sequence 11 withdrew, however much later.
    assert (still_adopts.action, still_adopts.target.asset_release_tag) == (ResolutionAction.ADOPT, engine_b)
    assert still_adopts.download_authorized is True
    # A machine that did receive 11 does not, moves off B if it runs it, and refuses to go back to 10.
    current, _ = _channel(tmp_path / "current", signer, eleven)
    assert current.refresh().state == "accepted"
    informed = current.resolve(current=BUNDLED)
    assert (informed.action, informed.target.asset_release_tag) == (ResolutionAction.ADOPT, NEWER)
    assert current.resolve(current=engine_b).action is ResolutionAction.WITHDRAW
    assert _channel(tmp_path / "current", signer, ten)[0].refresh().reason == "replayed"
    assert current.snapshot().payload.sequence == 11


class _Unavailable:
    def verify(self, **_request) -> None:
        raise ChannelError("verifier_too_old", "signature checks need avault 0.1.10 or later")


def test_without_a_working_verifier_nothing_is_recorded_or_selected(tmp_path) -> None:
    signer = Signer()
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5))
    recorded.refresh()
    channel, _ = _channel(tmp_path, signer, _index(signer, 6), verifier=_Unavailable())

    assert channel.refresh().reason == "verifier_too_old"
    resolution = channel.resolve(current=BUNDLED)
    assert (resolution.action, resolution.reason) == (ResolutionAction.KEEP, "verifier_too_old")
    assert recorded.snapshot().payload.sequence == 5


def test_an_engine_this_machine_recorded_as_withdrawn_is_not_kept_when_the_index_cannot_be_checked(tmp_path) -> None:
    signer = Signer()
    fallback = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=[tombstone(NEWER, fallback)]))
    recorded.refresh()
    channel, _ = _channel(tmp_path, signer, verifier=_Unavailable())

    blocked = channel.resolve(current=NEWER)

    # No fallback can be named from an index that cannot be authenticated, so Model Hub is blocked.
    assert (blocked.action, blocked.reason, blocked.target) == (ResolutionAction.BLOCKED, "verifier_too_old", None)
    assert blocked.tombstone.asset_release_tag == NEWER
    assert channel.resolve(current=BUNDLED).action is ResolutionAction.KEEP


def _damaged_roots(root: Path) -> Path:
    path = root / "roots.json"
    path.write_text('{"schema_version": 1, "sequence_floor": 0, "roots": [{"key_id": "k"}]}', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "keyless",
    [
        # A downgrade to an Avibe release that ships no key.
        lambda root, monkeypatch: {"roots": TrustRoots()},
        # The shipped document is damaged, or missing from the installation.
        lambda root, monkeypatch: monkeypatch.setattr(engine_channel, "SHIPPED_TRUST_ROOTS_PATH", _damaged_roots(root)),
        lambda root, monkeypatch: monkeypatch.setattr(engine_channel, "SHIPPED_TRUST_ROOTS_PATH", root / "absent.json"),
    ],
    ids=["no-shipped-key", "damaged-roots", "missing-roots"],
)
def test_a_recorded_withdrawal_stays_in_force_when_this_avibe_has_no_readable_key(tmp_path, monkeypatch, keyless) -> None:
    """MH-ENGINE-CHANNEL-005: losing the key stops adoption; it does not bring a withdrawn engine back."""

    signer = Signer()
    fallback = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=[tombstone(NEWER, fallback)]))
    assert recorded.refresh().state == "accepted"
    assert recorded.resolve(current=NEWER).action is ResolutionAction.WITHDRAW
    state_path = tmp_path / "engine" / "channel" / "state.json"
    before = state_path.read_bytes()
    verifier = LocalVerifier()
    channel, fetch = _channel(tmp_path, None, _index(signer, 6), verifier=verifier, **(keyless(tmp_path, monkeypatch) or {}))

    status = channel.refresh()
    blocked = channel.resolve(current=NEWER)

    assert (status.state, status.reason) == ("uncommissioned", "uncommissioned")
    # No key can authenticate the record, so no fallback is taken from it and nothing may be downloaded.
    assert (blocked.action, blocked.reason, blocked.target) == (ResolutionAction.BLOCKED, "uncommissioned", None)
    assert blocked.tombstone.asset_release_tag == NEWER and blocked.download_authorized is False
    # An engine with no recorded withdrawal is left alone, as on a machine that never had a channel.
    kept = channel.resolve(current=BUNDLED)
    assert (kept.action, kept.reason) == (ResolutionAction.KEEP, "uncommissioned")
    assert channel.snapshot() is None
    assert fetch.calls == 0 and verifier.calls == 0
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("commissioned", [True, False], ids=["shipped-key", "no-shipped-key"])
def test_a_manifest_override_leaves_the_channel_even_with_a_recorded_withdrawal(tmp_path, commissioned) -> None:
    """MH-ENGINE-CHANNEL-006: the override is the one authorized way out, whatever keys this Avibe ships."""

    signer = Signer()
    fallback = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=[tombstone(NEWER, fallback)]))
    assert recorded.refresh().state == "accepted"
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "engine", manifest_path=tmp_path / "override.json")
    verifier = LocalVerifier()
    roots = signer.roots() if commissioned else TrustRoots()
    channel, fetch = _channel(tmp_path, None, _index(signer, 6), manager=manager, roots=roots, verifier=verifier)

    status = channel.refresh()
    resolution = channel.resolve(current=NEWER)

    assert (status.state, status.reason) == ("opted_out", "manifest_override")
    assert (resolution.action, resolution.reason, resolution.tombstone) == (
        ResolutionAction.KEEP,
        "manifest_override",
        None,
    )
    assert fetch.calls == 0 and verifier.calls == 0


def test_a_damaged_record_authorizes_no_channel_change_and_is_kept_as_it_is(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-002: damaged state is neither a fresh start nor an invented withdrawal."""

    signer = Signer()
    fallback = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5, revoked=[tombstone(NEWER, fallback)]))
    assert recorded.refresh().state == "accepted"
    state_path = tmp_path / "engine" / "channel" / "state.json"
    damaged = state_path.read_bytes()[:-40]
    state_path.write_bytes(damaged)
    # A newer index that would be accepted on a machine with an intact record.
    channel, fetch = _channel(tmp_path, signer, _index(signer, 6))

    status = channel.refresh()

    assert (status.state, status.reason) == ("rejected", "state_unreadable") and fetch.calls == 1
    assert state_path.read_bytes() == damaged
    with pytest.raises(ChannelError) as unreadable:
        channel.snapshot()
    assert unreadable.value.reason == "state_unreadable"
    keyless, _ = _channel(tmp_path, None, roots=TrustRoots())
    for reader in (channel, keyless):
        for current in (BUNDLED, NEWER):
            resolution = reader.resolve(current=current)
            # Nothing is adopted and no fallback is taken; and a record that cannot
            # be read does not show that this engine was withdrawn.
            assert (resolution.action, resolution.reason, resolution.target, resolution.tombstone) == (
                ResolutionAction.KEEP,
                "state_unreadable",
                None,
                None,
            )
    assert state_path.read_bytes() == damaged


def test_a_withdrawal_applies_whatever_else_would_stop_adoption(tmp_path, monkeypatch) -> None:
    signer = Signer()
    fallback = f"model-hub-engine-{VERSION}-{int(BUILD) + 2}"
    index = _index(signer, 5, revoked=[tombstone(NEWER, fallback)])
    _channel(tmp_path, signer, index)[0].refresh()

    # A source checkout cannot prove the fallback compatible: blocked, not kept.
    unreleased, _ = _channel(tmp_path, signer, avibe_version="0.0.0.dev0")
    assert unreleased.resolve(current=NEWER).action is ResolutionAction.BLOCKED
    # Routine updates off, and a bundled identity that cannot be read.
    channel, _ = _channel(tmp_path, signer)
    assert channel.resolve(current=NEWER, allow_adoption=False).action is ResolutionAction.WITHDRAW
    monkeypatch.setattr(EngineChannel, "bundled_release_tag", lambda self: None)
    withdrawn = channel.resolve(current=NEWER)
    assert (withdrawn.action, withdrawn.target.asset_release_tag) == (ResolutionAction.WITHDRAW, fallback)
    assert channel.resolve(current=BUNDLED).reason == "engine_identity_unrecognized"


def test_a_source_checkout_version_adopts_nothing(tmp_path) -> None:
    signer = Signer()
    channel, _ = _channel(tmp_path, signer, _index(signer), avibe_version="0.0.0.dev0")
    channel.refresh()

    resolution = channel.resolve(current=BUNDLED)

    assert (resolution.action, resolution.reason) == (ResolutionAction.KEEP, "avibe_version_unreleased")


@pytest.mark.skipif(os.name == "nt", reason="the stand-in avault is a POSIX shell script")
def test_the_default_verifier_only_locates_avault_and_runs_its_keyless_verbs(tmp_path, monkeypatch) -> None:
    from vibe import api

    calls = tmp_path / "calls"
    script = tmp_path / "avault"
    script.write_text(
        f"""#!/bin/sh
echo "$*" >> "{calls}"
case "$1" in
  version) echo "avault 0.1.10"; exit 0 ;;
  verify) cat > /dev/null; echo '{{"valid":true}}'; exit 0 ;;
esac
exit 70
"""
    )
    script.chmod(0o755)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verification must not install, probe, or unlock avault")

    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: str(script))
    for name in ("_run_avault", "ensure_avault_installed", "install_avault", "avault_status", "refresh_avault_if_stale"):
        monkeypatch.setattr(api, name, forbidden)
    signer = Signer()
    fetch = Fetch(_index(signer, 2))
    channel = EngineChannel(EngineRuntimeManager(runtime_dir=tmp_path / "engine"), roots=signer.roots(), fetch=fetch)

    assert channel.refresh().state == "accepted"
    assert calls.read_text().splitlines() == ["version", "verify"]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server hook
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/index")
            self.end_headers()
            return
        body = b"x" * (engine_channel.MAX_ENVELOPE_BYTES + 1) if self.path == "/huge" else b'{"small": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-User-Agent", self.headers.get("User-Agent", ""))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def index_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_index_download_follows_no_redirect_and_reads_a_bounded_body(index_server, monkeypatch) -> None:
    monkeypatch.setattr(engine_channel, "CHANNEL_INDEX_URL", f"{index_server}/index")
    assert engine_channel.fetch_channel_index() == b'{"small": true}'

    for path in ("/redirect", "/huge"):
        monkeypatch.setattr(engine_channel, "CHANNEL_INDEX_URL", f"{index_server}{path}")
        with pytest.raises(DependencyNetworkError):
            engine_channel.fetch_channel_index()
