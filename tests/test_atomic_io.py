"""Properties every caller of ``write_atomic`` relies on.

Stated as properties rather than as a list of call sites: the whole point of
folding nineteen modules onto one function is that the next caller inherits
these guarantees without anybody remembering to extend a list here.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from config import atomic_io
from config.atomic_io import write_atomic


def test_write_atomic_creates_missing_parent_directories(tmp_path) -> None:
    target = tmp_path / "deep" / "nested" / "state.json"

    write_atomic(target, json.dumps({"ok": True}))

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


@pytest.mark.parametrize("payload", ["héllo\n", b"h\xc3\xa9llo\n"])
def test_write_atomic_round_trips_text_and_bytes(tmp_path, payload) -> None:
    target = tmp_path / "payload.bin"

    write_atomic(target, payload)

    assert target.read_bytes() == b"h\xc3\xa9llo\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_write_atomic_result_is_owner_private_even_over_a_loose_file(tmp_path) -> None:
    """No window in which a file holding agent credentials is world-readable."""

    target = tmp_path / "auth.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)

    write_atomic(target, "{}\n")

    assert target.stat().st_mode & 0o777 == 0o600


def test_write_atomic_leaves_the_destination_intact_when_the_swap_fails(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "state.json"
    write_atomic(target, "original")

    monkeypatch.setattr(
        atomic_io.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError):
        write_atomic(target, "replacement")

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(f".{target.name}.*")) == []


def test_write_atomic_leaves_no_temp_file_when_the_payload_cannot_be_persisted(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "state.json"

    monkeypatch.setattr(
        atomic_io.os,
        "fsync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        write_atomic(target, "never lands")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_concurrent_writers_publish_whole_payloads_never_fragments(tmp_path) -> None:
    """A per-call unique temp name is what keeps overlapping writers honest.

    A shared ``<name>.tmp`` would let one writer's ``os.replace`` publish another
    writer's half-flushed bytes; readers would see a payload nobody wrote.
    """

    target = tmp_path / "status.json"
    payloads = [json.dumps({"writer": index, "filler": "x" * 40_000}) for index in range(8)]
    whole = set(payloads)
    observed: list[str] = []
    stop = threading.Event()

    def _write(payload: str) -> None:
        for _ in range(20):
            write_atomic(target, payload)

    def _read() -> None:
        while not stop.is_set():
            try:
                observed.append(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    writers = [threading.Thread(target=_write, args=(payload,)) for payload in payloads]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    stop.set()
    reader.join(timeout=5)

    assert target.read_text(encoding="utf-8") in whole
    assert observed, "reader never managed to observe the file"
    assert set(observed) <= whole
    assert list(tmp_path.glob(f".{target.name}.*")) == []


def test_write_atomic_replaces_a_symlink_by_default(tmp_path) -> None:
    """State this process owns: resolving a planted symlink is the wrong answer."""

    real = tmp_path / "elsewhere.json"
    real.write_text("untouched", encoding="utf-8")
    link = tmp_path / "state.json"
    link.symlink_to(real)

    write_atomic(link, "mine")

    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "mine"
    assert real.read_text(encoding="utf-8") == "untouched"


def test_write_atomic_follows_a_symlink_when_asked(tmp_path) -> None:
    """A user's dotfiles link must survive an edit to the file it stands for."""

    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir()
    real.write_text("old", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real)

    write_atomic(link, "new", follow_symlinks=True)

    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("missing", ["fchmod", "O_DIRECTORY"])
def test_write_atomic_does_not_require_optional_os_attributes(
    tmp_path, monkeypatch, missing
) -> None:
    """Neither is available everywhere Avibe runs; both are only ever nice to have."""

    monkeypatch.delattr(atomic_io.os, missing, raising=False)
    target = tmp_path / "state.json"

    write_atomic(target, "portable")

    assert target.read_text(encoding="utf-8") == "portable"
