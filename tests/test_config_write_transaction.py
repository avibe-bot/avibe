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


def test_transaction_sees_concurrent_write_inside_lock(
    isolated_config_home: Path,
) -> None:
    """The lock serializes whole RMW cycles: two concurrent
    transactions on disjoint fields BOTH survive, and the later one's
    mutator sees the earlier one's committed field (its load ran after
    the earlier save). Under the plain load→save pattern the second
    writer's field would be reverted by the first writer's stale
    snapshot — the #1458 race."""

    a_entered = threading.Event()
    b_saw_a: dict = {}

    def mutator_a(cfg: V2Config) -> None:
        a_entered.set()
        cfg.language = "zh"

    def mutator_b(cfg: V2Config) -> None:
        b_saw_a["language"] = cfg.language
        cfg.runtime.log_level = "DEBUG"

    done: list = []

    def txn(mutator):
        update_config_fields(mutator)
        done.append(True)

    ta = threading.Thread(target=txn, args=(mutator_a,))
    tb = threading.Thread(target=txn, args=(mutator_b,))
    ta.start()
    assert a_entered.wait(timeout=5)  # A is inside its cycle
    tb.start()                        # B queues on the file lock
    ta.join(timeout=5)
    tb.join(timeout=5)
    assert len(done) == 2

    loaded = V2Config.load()
    # Both writes survive.
    assert loaded.language == "zh"
    assert loaded.runtime.log_level == "DEBUG"
    # B's snapshot was loaded after A's save (inside the lock).
    assert b_saw_a["language"] == "zh"


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
