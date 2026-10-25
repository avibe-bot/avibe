"""Ephemeral file transport: metadata-only inspection and consume-before-start."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.terminal_bench.credential import LIMIT, clean, consume, metadata, prepare_upload
from scripts.terminal_bench.profile import Profile
from scripts.terminal_bench import runner

SENTINEL = "fake-private-key-not-for-logs"


def secret_file(tmp_path):
    private = tmp_path / "credential"
    private.mkdir(mode=0o700)
    path = private / "provider.key"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(SENTINEL)
    return path


@pytest.mark.parametrize("shape", ["normal", "symlink", "hardlink", "oversized", "public", "fifo", "empty"])
def test_input_metadata_and_failure_removal(tmp_path, shape):
    path = secret_file(tmp_path)
    external = tmp_path / "unchanged"
    external.write_text("outside")
    if shape == "symlink":
        path.unlink()
        path.symlink_to(external)
    elif shape == "hardlink":
        path.unlink()
        os.link(external, path)
        path.chmod(0o600)
    elif shape == "oversized":
        path.write_bytes(b"x" * (LIMIT + 1))
    elif shape == "public":
        path.chmod(0o644)
    elif shape == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif shape == "empty":
        path.write_text("")
    if shape == "normal":
        prepared = prepare_upload(path)
        info = metadata(path)
        assert prepared == info
        assert info == {"regular": True, "mode": "0600", "directory_mode": "0700", "size": len(SENTINEL)}
        assert SENTINEL not in json.dumps(info)
        assert consume(path) == SENTINEL
    else:
        before = external.stat()
        with pytest.raises((OSError, ValueError)):
            prepare_upload(path)
        assert (external.stat().st_uid, external.stat().st_mode) == (before.st_uid, before.st_mode)
        with pytest.raises((OSError, ValueError)):
            consume(path)
    assert not path.exists() and not path.is_symlink()
    assert external.read_text() == "outside"
    assert clean(path) == {"removed": True}
    assert not path.parent.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="real uploaded UID adoption needs a root Linux sandbox")
def test_prepare_adopts_foreign_upload_uid_but_read_stays_strict(tmp_path):
    """Compose cp preserves UID501; strict consume must work only after setup adopts it."""
    path = secret_file(tmp_path)
    os.chown(path, 501, -1)
    with pytest.raises(ValueError, match="owned by the runner"):
        metadata(path)
    assert prepare_upload(path)["mode"] == "0600"
    assert path.stat().st_uid == os.geteuid()
    assert consume(path) == SENTINEL
    assert not path.exists()


def test_rejects_symlink_parent_without_touching_other_directory(tmp_path):
    elsewhere = tmp_path / "other-trial"
    elsewhere.mkdir()
    actual = secret_file(elsewhere)
    (tmp_path / "credential").symlink_to(actual.parent, target_is_directory=True)
    with pytest.raises(OSError):
        consume(tmp_path / "credential/provider.key")
    assert actual.read_text() == SENTINEL
    with pytest.raises(OSError):
        clean(tmp_path / "credential/provider.key")
    assert actual.read_text() == SENTINEL


def test_key_unlinked_before_service_and_delivered_only_to_bootstrap_stdin(tmp_path, monkeypatch, capsys):
    path = secret_file(tmp_path)
    work = tmp_path / "task"
    work.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(Profile(model="fixture", base_url="http://127.0.0.1:9/v1").payload()))
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("fixture")
    observed = []

    def start(self):
        assert not path.exists(), "credential file must disappear BEFORE measured service starts"
        assert self.profile.credential_env not in self.env
        self._result["outcome"] = "ready"

    def helper(argv, **kwargs):
        observed.append((argv, kwargs))
        assert SENTINEL not in " ".join(argv)
        assert SENTINEL not in json.dumps(kwargs["env"])
        if "configure" in argv:
            assert kwargs["input"] == SENTINEL
            result = {"source_id": "fixture-source"}
        elif "agent" in argv and "run" in argv:
            result = {"run": {"id": "fixture-run", "session_id": "fixture-session"}}
        elif "runs" in argv:
            result = {"run": {"status": "succeeded", "result_text": SENTINEL}}
        elif "harness" in argv:
            result = {"controller": {"available": True}, "runs": [], "watches": [], "tasks": []}
        else:
            result = {}
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(runner.FullRuntimeTrial, "start", start)
    monkeypatch.setattr(subprocess, "run", helper)
    assert runner.main([
        "--workdir", str(work), "--state", str(tmp_path / "state"), "--output", str(tmp_path / "out"),
        "--profile", str(profile), "--instruction", str(instruction), "--credential-file", str(path),
    ]) == 0
    output = capsys.readouterr()
    assert observed and SENTINEL not in output.out + output.err
    assert SENTINEL not in (tmp_path / "out/execution.json").read_text()
    assert SENTINEL not in (tmp_path / "state/profile.json").read_text()
    assert not path.exists()
