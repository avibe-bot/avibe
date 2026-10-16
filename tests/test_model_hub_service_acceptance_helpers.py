"""Identity checks used by MH-SERVICE-001 must not mistake clock drift for exit."""

from hashlib import sha256
from types import SimpleNamespace

import psutil
import pytest

from tests.e2e import test_model_hub_service_acceptance as acceptance
from tests.fake_pid_helpers import fake_pid

_PID = fake_pid()
_MARKER = "engine-a"
_FINGERPRINT = "sha256:" + sha256(_MARKER.encode("ascii")).hexdigest()


@pytest.mark.parametrize(
    ("live_create_time", "marker", "fingerprint", "expected"),
    [
        pytest.param(100.0, _MARKER, _FINGERPRINT, True, id="same-engine"),
        pytest.param(99.0, _MARKER, _FINGERPRINT, True, id="birth-time-drift"),
        pytest.param(100.0, "engine-b", _FINGERPRINT, False, id="recycled-pid-same-birth-time"),
        pytest.param(100.0, None, _FINGERPRINT, False, id="no-marker"),
        pytest.param(100.0, "", _FINGERPRINT, False, id="empty-marker"),
        pytest.param(100.0, "engine-\u4e00", _FINGERPRINT, False, id="malformed-marker"),
        pytest.param(100.0, psutil.AccessDenied(_PID), _FINGERPRINT, False, id="unreadable-marker"),
        pytest.param(100.0, psutil.NoSuchProcess(_PID), _FINGERPRINT, False, id="process-exited"),
        pytest.param(100.0, _MARKER, None, False, id="missing-fingerprint"),
        pytest.param(100.0, _MARKER, 123, False, id="malformed-fingerprint"),
    ],
)
def test_engine_identity_uses_marker_not_birth_time(
    monkeypatch, live_create_time, marker, fingerprint, expected,
):
    def environ():
        if isinstance(marker, psutil.Error):
            raise marker
        return {"AVIBE_PROCESS_IDENTITY": marker} if marker is not None else {}

    def process(pid):
        assert pid == _PID
        return SimpleNamespace(create_time=lambda: live_create_time, environ=environ)

    monkeypatch.setattr(acceptance.psutil, "Process", process)
    entry = {"pid": _PID, "create_time": 100.0}
    if fingerprint is not None:
        entry["worker_fingerprint"] = fingerprint

    assert acceptance._engine_is(entry) is expected


@pytest.mark.parametrize("live_create_time", [100.0, 99.0])
def test_service_cleanup_still_checks_its_unmarked_records(monkeypatch, live_create_time):
    """Engine markers must not disable cleanup or authorize killing a reused service pid."""
    service = object.__new__(acceptance._Service)
    service._started = [{"pid": _PID, "create_time": 100.0}]
    monkeypatch.setattr(service, "vibe", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(service, "_remember_recorded", lambda: None)
    killed = []
    monkeypatch.setattr(service, "_kill", killed.append)
    monkeypatch.setattr(
        acceptance.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: live_create_time),
    )

    service.stop()

    assert killed == ([_PID] if live_create_time == 100.0 else [])
