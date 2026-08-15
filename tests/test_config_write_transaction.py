"""Tests for the cross-process config write transaction (#1458).

Pins two contracts:

- ``update_config_fields`` performs its load INSIDE the file lock, so a
  concurrent writer's committed fields survive a later save — the
  stale-snapshot race that ``CONFIG_LOCK`` cannot fix across processes.
- Direct ``load → mutate → save`` pairs outside the transaction DO lose
  interleaved writes; the test asserts the race shape itself so the
  primitive's value stays grounded in a failing baseline.

Cross-process behavior is exercised through the same flock every
production writer takes (threads + the real file lock; separate OS
processes would only add scheduling noise the lock already excludes).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from config import paths
from config.v2_config import V2Config, update_config_fields


@pytest.fixture()
def isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.default().save()
    return paths.get_config_path()


def test_update_config_fields_writes_and_returns_fresh_state(
    isolated_config_home: Path,
) -> None:
    updated = update_config_fields(lambda cfg: setattr(cfg, "language", "zh"))
    assert updated.language == "zh"
    assert V2Config.load().language == "zh"


def _txn_worker_a(home: str, entered: threading.Event, release: threading.Event) -> None:
    """Hold the transaction open (file lock held) until released."""
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import update_config_fields

    def mutator(cfg: V2Config) -> None:
        cfg.language = "zh"
        entered.set()
        assert release.wait(timeout=15), "worker A never released"

    update_config_fields(mutator)


def _txn_worker_b(home: str, done: threading.Event, saw_queue) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import update_config_fields

    def mutator(cfg: V2Config) -> None:
        saw_queue.put(cfg.language)
        cfg.runtime.log_level = "DEBUG"

    update_config_fields(mutator)
    done.set()


def test_transaction_blocks_second_process_until_first_releases(
    isolated_config_home: Path,
) -> None:
    """The cross-process contract, exercised across REAL processes:
    while worker A sits inside its transaction (file lock held), worker
    B's transaction must not enter — and once A releases, B's mutator
    sees A's committed field. Thread-based variants of this test are
    vacuous: the process-local ``CONFIG_LOCK`` serializes them before
    the file lock, so they would pass even with a no-op flock."""

    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    b_done = ctx.Event()
    saw_queue = ctx.Queue()

    a = ctx.Process(target=_txn_worker_a, args=(str(isolated_config_home.parent.parent), entered, release))
    b = ctx.Process(target=_txn_worker_b, args=(str(isolated_config_home.parent.parent), b_done, saw_queue))
    a.start()
    assert entered.wait(timeout=15), "worker A never entered its transaction"
    b.start()
    # B must be blocked on the file lock while A holds it. Generous
    # margin: an unblocked trivial transaction finishes well inside it.
    assert not b_done.wait(timeout=1.0), "B entered the transaction while A held the file lock"
    release.set()
    a.join(timeout=15)
    b.join(timeout=15)
    assert a.exitcode == 0, f"worker A failed: {a.exitcode}"
    assert b.exitcode == 0, f"worker B failed: {b.exitcode}"

    loaded = V2Config.load()
    # Both writes survive.
    assert loaded.language == "zh"
    assert loaded.runtime.log_level == "DEBUG"
    # B's snapshot was loaded after A's save (inside the lock).
    assert saw_queue.get(timeout=5) == "zh"


def test_mutator_exception_aborts_without_write(isolated_config_home: Path) -> None:
    def boom(cfg: V2Config) -> None:
        cfg.language = "zh"
        raise RuntimeError("mutator failed")

    with pytest.raises(RuntimeError):
        update_config_fields(boom)
    assert V2Config.load().language == "en"


def test_plain_load_save_pair_loses_interleaved_write(
    isolated_config_home: Path,
) -> None:
    """The race baseline: a load→mutate→save cycle outside the
    transaction reverts fields committed between its load and its save.
    Guards against 'just use CONFIG_LOCK' regressions by keeping the
    failing shape executable."""

    stale = V2Config.load()  # snapshot taken early

    # A concurrent transactional writer commits in between.
    update_config_fields(lambda cfg: setattr(cfg, "language", "zh"))

    stale.save()  # writes the early snapshot

    loaded = V2Config.load()
    # The interleaved write was reverted by the stale full-snapshot
    # save — exactly the #1458 defect class.
    assert loaded.language == "en"


def test_transaction_reentrant_with_save(isolated_config_home: Path) -> None:
    """save() itself opens the Memory transaction; the write transaction
    must re-enter cleanly rather than deadlock on the held file lock."""

    def mutator(cfg: V2Config) -> None:
        cfg.agents.codex.oauth_relay_marker = {"provider_id": "OpenAI", "base_url": "https://r/v1"}

    update_config_fields(mutator)
    assert V2Config.load().agents.codex.oauth_relay_marker == {
        "provider_id": "OpenAI",
        "base_url": "https://r/v1",
    }
