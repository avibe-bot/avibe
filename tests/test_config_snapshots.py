"""Config consumers must not obstruct publication while holding an old snapshot.

The existing load/save race test does not force the OS handles to overlap.
These handshakes pin that interleaving, through real consumers and OS calls;
the ordinary-reader/legacy-replace controls expose either half of the Windows
protocol regression. All state and children belong to the test home.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from config import paths, v2_config
from config.atomic_io import read_snapshot, write_atomic
from config.v2_config import V2Config, update_config_fields


def _consume(path: Path, consumer: str):
    if consumer == "config":
        config = V2Config.load(path)
        return config.language, config.runtime.log_level, config.update.prereleases
    if consumer == "prerelease":
        from vibe.upgrade import prerelease_updates_enabled

        return prerelease_updates_enabled()
    if consumer == "ordinary":
        return path.read_bytes()
    if consumer == "snapshot":
        return read_snapshot(path)
    raise AssertionError(consumer)


def _held_consumer(path_text: str, consumer: str, ready, release, result) -> None:
    path = Path(path_text)
    os.environ["AVIBE_HOME"] = str(path.parent.parent)
    identity = path.stat()
    original_open = io.open

    def pause_after_open(*args, **kwargs):
        handle = original_open(*args, **kwargs)
        actual = os.fstat(handle.fileno())
        if (actual.st_dev, actual.st_ino) == (identity.st_dev, identity.st_ino) and handle.readable():
            # Intercept only scheduling, AFTER the real consumer selected and
            # acquired its real OS handle. This also catches the pre-fix
            # Path.read_bytes/read_text, so the regression fails on old code.
            ready.put("open")
            if not release.wait(30):
                handle.close()
                raise TimeoutError("publication did not release its reader")
        return handle

    try:
        with patch("io.open", pause_after_open):
            result.put(("ok", _consume(path, consumer)))
    except BaseException as exc:
        result.put(("error", repr(exc)))
        raise


@contextmanager
def _holding(path: Path, consumer: str):
    context = multiprocessing.get_context("spawn")
    ready, result = context.Queue(), context.Queue()
    release = context.Event()
    child = context.Process(
        target=_held_consumer, args=(str(path), consumer, ready, release, result),
    )
    observed = []
    child.start()
    try:
        assert ready.get(timeout=30) == "open"
        yield observed
    finally:
        release.set()
        try:
            # Drain before join: a child's queue feeder can otherwise wait for
            # the parent when the snapshot exceeds the OS pipe buffer.
            observed.append(result.get(timeout=30))
        finally:
            child.join(timeout=30)
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
            ready.close()
            result.close()
        assert child.exitcode == 0, f"reader failed: {child.exitcode}"


@pytest.fixture(params=["plain", "config-\U0001f30f"])
def config_path(request, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / request.param))
    config = V2Config.default()
    config.language = "en"
    config.runtime.log_level = "INFO"
    config.update.prereleases = False
    config.save()
    return paths.get_config_path()


@pytest.mark.parametrize("consumer", ["config", "prerelease"])
@pytest.mark.parametrize("publication", ["transaction", "migration"])
def test_config_publication_keeps_held_consumers_on_the_old_snapshot(config_path, consumer, publication):
    """A config load and a raw consumer both allow save AND migration publication."""
    before = config_path.read_bytes()

    def change(config):
        config.language = "zh"
        config.runtime.log_level = "DEBUG"
        config.update.prereleases = True

    with _holding(config_path, consumer) as result:
        if publication == "transaction":
            update_config_fields(change)
        else:
            payload = json.loads(before)
            payload["language"] = "zh"
            payload["runtime"]["log_level"] = "DEBUG"
            payload["update"]["prereleases"] = True
            backup, warning = v2_config._persist_migrated_config_payload(
                config_path, before.decode("utf-8"), payload,
            )
            assert warning is None
            assert backup.read_bytes() == before
            committed = config_path.read_bytes()
            # A stale migration still refuses to overwrite the new snapshot.
            backup, warning = v2_config._persist_migrated_config_payload(
                config_path, before.decode("utf-8"), json.loads(before),
            )
            assert backup is None and "file changed" in warning
            assert config_path.read_bytes() == committed
        assert _consume(config_path, "config") == ("zh", "DEBUG", True)
        assert _consume(config_path, "prerelease") is True
        assert not list(config_path.parent.glob("*.tmp"))
    assert result == [("ok", ("en", "INFO", False) if consumer == "config" else False)]


@pytest.mark.parametrize(
    ("consumer", "publication"),
    [("ordinary", "atomic"), ("snapshot", "legacy")],
)
def test_windows_requires_both_cooperating_reader_and_publisher(config_path, consumer, publication):
    """The old reader OR old rename still denies publication on native Windows."""
    before = config_path.read_bytes()
    source = config_path.with_name("next.json")
    source.write_bytes(b'{"new": true}')

    def publish():
        if publication == "atomic":
            write_atomic(config_path, b'{"new": true}')
        else:
            os.replace(source, config_path)

    with _holding(config_path, consumer) as result:
        if os.name == "nt":
            with pytest.raises(PermissionError) as raised:
                publish()
            assert raised.value.winerror in {5, 32}
            assert config_path.read_bytes() == before
        else:
            publish()
            assert config_path.read_bytes() == b'{"new": true}'
        assert not list(config_path.parent.glob(".config.json.*.tmp"))
    assert result == [("ok", before)]
    # The controlled blocking handle was the only obstruction.
    write_atomic(config_path, b'{"after_close": true}')
    assert config_path.read_bytes() == b'{"after_close": true}'


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only replacement policy")
def test_read_only_config_still_refuses_publication_and_preserves_files(config_path):
    """POSIX rename flags must not become IGNORE_READONLY or an unlink fallback."""
    before = config_path.read_bytes()
    config_path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PermissionError) as raised:
            update_config_fields(lambda config: setattr(config, "language", "zh"))
        assert raised.value.winerror == 5
        assert config_path.read_bytes() == before
        assert not config_path.stat().st_mode & stat.S_IWRITE
        assert not list(config_path.parent.glob(".config.json.*.tmp"))
    finally:
        config_path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_snapshot_text_retains_newlines_and_bytes_contract(tmp_path):
    """Migration comparisons used read_text; loads used exact decoded bytes."""
    path = tmp_path / "config.json"
    path.write_bytes(b'{\r\n"language": "en"\r}\n')
    assert read_snapshot(path) == b'{\r\n"language": "en"\r}\n'
    assert read_snapshot(path, encoding="utf-8") == '{\n"language": "en"\n}\n'


def test_consent_rejection_after_fsync_keeps_external_snapshot(config_path, monkeypatch):
    """The native publisher must retain the existing flushed-mode/consent boundary."""
    from config import atomic_io

    flushed = []
    fsync = os.fsync

    def record_sync(descriptor):
        info = os.fstat(descriptor)
        flushed.append((info.st_dev, info.st_ino))
        fsync(descriptor)

    def reject():
        [temporary] = config_path.parent.glob(".config.json.*.tmp")
        info = temporary.stat()
        assert (info.st_dev, info.st_ino) in flushed
        assert info.st_mode & stat.S_IWRITE
        assert temporary.read_bytes() == b"proposed"
        write_atomic(config_path, "external")
        raise RuntimeError("consent changed")

    monkeypatch.setattr(atomic_io.os, "fsync", record_sync)
    with pytest.raises(RuntimeError, match="consent changed"):
        write_atomic(config_path, "proposed", before_replace=reject)
    assert config_path.read_bytes() == b"external"
    assert not list(config_path.parent.glob(".config.json.*.tmp"))
