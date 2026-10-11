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
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from config import paths
from config.v2_config import V2Config, update_config_fields


def test_transaction_and_load_persistence_share_lock_order(isolated_config_home, monkeypatch):
    from config import v2_config as module

    migration_entered = threading.Event()
    transaction_attempted = threading.Event()
    errors = []
    owner = threading.local()
    lock = threading.RLock()

    class OrderedLock:
        def __enter__(self):
            if threading.current_thread().name == "config-transaction":
                transaction_attempted.set()
            lock.acquire()
            owner.depth = getattr(owner, "depth", 0) + 1

        def __exit__(self, *_args):
            owner.depth -= 1
            lock.release()

    original_file_lock = module._config_file_lock

    @contextmanager
    def checked_file_lock(path):
        if threading.current_thread().name == "config-transaction":
            transaction_attempted.set()
        # Fail deterministically before an inverted entrant can deadlock the
        # test process. The real re-entrant OS file lock still guards writes.
        assert getattr(owner, "depth", 0) > 0, "file lock acquired before CONFIG_LOCK"
        with original_file_lock(path):
            yield

    monkeypatch.setattr(module, "CONFIG_LOCK", OrderedLock())
    monkeypatch.setattr(module, "_config_file_lock", checked_file_lock)

    def migration():
        try:
            with module.CONFIG_LOCK:
                migration_entered.set()
                assert transaction_attempted.wait(3)
                V2Config.load(isolated_config_home, persist_migrations=True)
                raw = isolated_config_home.read_text()
                payload = json.loads(raw)
                payload["language"] = "zh"
                _backup, warning = module._persist_migrated_config_payload(isolated_config_home, raw, payload)
                assert warning is None
        except BaseException as exc:
            errors.append(exc)

    def transaction():
        try:
            assert migration_entered.wait(3)
            with module.config_write_transaction(isolated_config_home) as config:
                assert config.language == "zh"
                config.runtime.log_level = "DEBUG"
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=migration), threading.Thread(target=transaction, name="config-transaction")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert not errors
    result = V2Config.load(isolated_config_home)
    assert (result.language, result.runtime.log_level) == ("zh", "DEBUG")


def test_shared_config_lock_allows_nested_load_save_transaction(isolated_config_home):
    from config import v2_config as module

    with module.CONFIG_LOCK, module.config_file_lock(isolated_config_home):
        with module.config_write_transaction(isolated_config_home) as config:
            config.language = "zh"
            config.save(isolated_config_home)
            with module.config_write_transaction(isolated_config_home) as nested:
                assert nested.language == "zh"
            assert V2Config.load(isolated_config_home, persist_migrations=True).language == "zh"
    assert V2Config.load(isolated_config_home).language == "zh"


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


def _txn_worker_b(home: str, attempted: threading.Event, done: threading.Event, saw_queue) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import update_config_fields

    def mutator(cfg: V2Config) -> None:
        saw_queue.put(cfg.language)
        cfg.runtime.log_level = "DEBUG"

    # Signal AFTER imports/setup but BEFORE the transaction: the parent
    # waits for this before concluding anything about lock blocking, so
    # a slow spawn/import on a loaded CI host cannot fake the
    # "B never attempted entry" outcome.
    attempted.set()
    update_config_fields(mutator)
    done.set()


def _migration_lock_worker(home: str, entered: threading.Event, release: threading.Event) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config import paths as _paths
    from config.v2_config import _config_file_lock

    with _config_file_lock(_paths.get_config_path()):
        entered.set()
        assert release.wait(timeout=15), "migration lock worker never released"


def _ordinary_save_worker(home: str, attempted: threading.Event, done: threading.Event) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import V2Config

    attempted.set()
    config = V2Config.load()
    config.language = "zh"
    config.save()
    done.set()


def _transaction_worker(
    home: str,
    attempted: threading.Event,
    entered: threading.Event,
    done: threading.Event,
) -> None:
    import os as _os

    _os.environ["AVIBE_HOME"] = home
    from config.v2_config import update_config_fields

    def mutator(config: V2Config) -> None:
        entered.set()
        config.language = "zh"

    attempted.set()
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
    b_attempted = ctx.Event()
    b_done = ctx.Event()
    saw_queue = ctx.Queue()

    a = ctx.Process(target=_txn_worker_a, args=(str(isolated_config_home.parent.parent), entered, release))
    b = ctx.Process(
        target=_txn_worker_b,
        args=(str(isolated_config_home.parent.parent), b_attempted, b_done, saw_queue),
    )
    a.start()
    assert entered.wait(timeout=15), "worker A never entered its transaction"
    b.start()
    # B has reached the transaction call (imports done); from here the
    # only thing between it and completion is the file lock.
    assert b_attempted.wait(timeout=15), "worker B never reached the transaction"
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


def test_ordinary_save_waits_for_migration_file_lock(
    isolated_config_home: Path,
) -> None:
    """Every ordinary save must share the lock used by migration persistence."""

    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    attempted = ctx.Event()
    done = ctx.Event()
    home = str(isolated_config_home.parent.parent)

    migration = ctx.Process(
        target=_migration_lock_worker,
        args=(home, entered, release),
    )
    saver = ctx.Process(
        target=_ordinary_save_worker,
        args=(home, attempted, done),
    )
    migration.start()
    assert entered.wait(timeout=15), "migration worker never acquired its lock"
    saver.start()
    assert attempted.wait(timeout=15), "save worker never reached save"
    assert not done.wait(timeout=1.0), "ordinary save bypassed the migration file lock"

    release.set()
    migration.join(timeout=15)
    saver.join(timeout=15)
    assert migration.exitcode == 0, f"migration worker failed: {migration.exitcode}"
    assert saver.exitcode == 0, f"save worker failed: {saver.exitcode}"
    assert V2Config.load().language == "zh"


def test_transaction_load_and_mutator_wait_for_migration_file_lock(
    isolated_config_home: Path,
) -> None:
    """The transaction owns the migration lock before it reads or mutates."""

    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    migration_entered = ctx.Event()
    release = ctx.Event()
    attempted = ctx.Event()
    mutator_entered = ctx.Event()
    done = ctx.Event()
    home = str(isolated_config_home.parent.parent)
    migration = ctx.Process(
        target=_migration_lock_worker,
        args=(home, migration_entered, release),
    )
    writer = ctx.Process(
        target=_transaction_worker,
        args=(home, attempted, mutator_entered, done),
    )

    migration.start()
    assert migration_entered.wait(timeout=15), "migration worker never acquired its lock"
    writer.start()
    assert attempted.wait(timeout=15), "transaction writer never reached the transaction"
    assert not mutator_entered.wait(timeout=1.0), "transaction mutated before taking the migration lock"

    release.set()
    migration.join(timeout=15)
    writer.join(timeout=15)
    assert migration.exitcode == 0, f"migration worker failed: {migration.exitcode}"
    assert writer.exitcode == 0, f"transaction writer failed: {writer.exitcode}"
    assert mutator_entered.is_set()
    assert done.is_set()
    assert V2Config.load().language == "zh"


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


def test_plain_load_completes_while_another_thread_holds_the_config_lock(
    isolated_config_home: Path,
) -> None:
    """A plain read takes no config lock: a held writer section cannot stall it."""
    from config import v2_config as module

    held = threading.Event()
    release = threading.Event()
    loaded: list[V2Config] = []

    def writer_section() -> None:
        with module.CONFIG_LOCK:
            held.set()
            release.wait()

    def read() -> None:
        loaded.append(V2Config.load())

    holder = threading.Thread(target=writer_section)
    holder.start()
    reader = threading.Thread(target=read)
    try:
        assert held.wait(30)
        reader.start()
        # A hang guard only: the read waits on nothing while the lock stays held.
        reader.join(30)
        assert not reader.is_alive(), "a plain load waited for the config lock"
        assert loaded[0].language == "en"
    finally:
        release.set()
        holder.join()
        if reader.is_alive():
            reader.join()


def test_a_load_racing_a_save_sees_the_old_file_or_the_new_one(isolated_config_home: Path) -> None:
    """Writers publish config.json atomically, so a lock-free read never sees a partial file."""

    # Large enough that an in-place rewrite would span many writes and reads.
    padding = "x" * 500_000
    versions = {"en": f"/old/{padding}", "zh": f"/new/{padding}"}

    def write(language: str) -> None:
        def mutate(cfg: V2Config) -> None:
            cfg.language = language
            cfg.runtime.default_cwd = versions[language]

        update_config_fields(mutate)

    write("en")
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for index in range(60):
                write(("zh", "en")[index % 2])
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop.set()

    thread = threading.Thread(target=writer)
    thread.start()
    observed = 0
    try:
        while not stop.is_set() or observed == 0:
            config = V2Config.load()
            assert config.load_warnings == ()
            assert config.runtime.default_cwd == versions[config.language]
            observed += 1
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert not list(isolated_config_home.parent.glob(f"{isolated_config_home.name}.bak-*"))


def test_pair_preclaim_migration_reads_config_under_the_sqlite_writer_without_the_config_lock(
    isolated_config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pair()'s pre-claim migration holds the SQLite writer when it reads the config.

    A reconcile in another thread holds config_file_lock and waits for that writer.
    When the read also took CONFIG_LOCK, the two waited on each other until SQLite's
    busy_timeout failed the reconcile with "database is locked".
    """
    from config.v2_config import config_file_lock
    from storage import remote_access_authorization_service as authorization
    from storage import resource_access_service
    from vibe import remote_access

    # Initialize state first so neither thread migrates the schema below.
    remote_access._run_pending_deferred_context_migration()
    holds_sqlite_writer = threading.Event()
    original = resource_access_service._configured_resource_state

    def configured_resource_state():
        # Called after reserve_write_lock(): this thread owns the SQLite writer now.
        holds_sqlite_writer.set()
        return original()

    monkeypatch.setattr(resource_access_service, "_configured_resource_state", configured_resource_state)
    outcome: dict[str, object] = {}

    def preclaim() -> None:
        try:
            outcome["preclaim"] = remote_access._run_pending_deferred_context_migration()
        except BaseException as exc:
            outcome["preclaim"] = exc

    worker = threading.Thread(target=preclaim)
    try:
        with config_file_lock():
            worker.start()
            assert holds_sqlite_writer.wait(30)
            result = authorization.reconcile_instance_binding(instance_id="inst_A", instance_kind="personal")
    finally:
        # After the config lock is released, so a pre-claim still waiting for it can finish.
        worker.join()
    assert result["ok"]
    assert isinstance(outcome["preclaim"], dict)
