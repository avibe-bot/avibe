"""An installed Avibe's use of the engine channel (vibe/model_hub_runtime/engine_channel.py).

What this protects: an Avibe release with no shipped key has no channel at
all; an engine manifest override or offline mode keeps the channel from
choosing an engine; and reading the channel never installs, selects, or
starts anything.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from core.dependency_network import DependencyNetworkError
from core.model_hub_engine_channel import ChannelError, ResolutionAction, TrustRoots
from tests.engine_channel_support import (
    NOW,
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
    options.setdefault("clock", lambda: NOW)
    options.setdefault("avibe_version", "3.2.6")
    if signer is not None:
        options.setdefault("roots", signer.roots())
    return EngineChannel(manager, fetch=fetch, **options), fetch


def _index(signer: Signer, sequence: int = 1, *, revoked: list | None = None) -> bytes:
    releases = [
        release_entry(manifest_bytes(VERSION, int(BUILD) - 1 or 1)) if int(BUILD) > 1 else None,
        release_entry(manifest_bytes(VERSION, int(BUILD) + 1)),
        release_entry(manifest_bytes(VERSION, int(BUILD) + 2)),
    ]
    return signed(signer, sequence, releases=[item for item in releases if item], revoked=revoked)


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
    assert first.expires_at == "2026-11-17T06:00:00Z"
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
        "index_stale",
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
    expired, _ = _channel(tmp_path, signer, _index(signer, 6), clock=lambda: NOW + timedelta(days=8))
    assert expired.refresh().reason == "expired"
    assert channel.snapshot().payload.sequence == 5


def test_without_a_working_verifier_nothing_is_recorded_or_selected(tmp_path) -> None:
    class Unavailable:
        def verify(self, **_request) -> None:
            raise ChannelError("verifier_too_old", "signature checks need avault 0.1.10 or later")

    signer = Signer()
    recorded, _ = _channel(tmp_path, signer, _index(signer, 5))
    recorded.refresh()
    channel, _ = _channel(tmp_path, signer, _index(signer, 6), verifier=Unavailable())

    assert channel.refresh().reason == "verifier_too_old"
    resolution = channel.resolve(current=BUNDLED)
    assert (resolution.action, resolution.reason) == (ResolutionAction.KEEP, "verifier_too_old")
    assert recorded.snapshot().payload.sequence == 5


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
    channel = EngineChannel(
        EngineRuntimeManager(runtime_dir=tmp_path / "engine"), roots=signer.roots(), fetch=fetch, clock=lambda: NOW
    )

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
