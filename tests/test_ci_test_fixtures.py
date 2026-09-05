from __future__ import annotations

import builtins
from contextlib import closing
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conftest import _reset_cached_sqlite_engines, _reset_oauth_runtime_state


def test_database_copies_preserve_the_entire_template_and_isolate_mutations(
    tmp_path, sqlite_db_factory, _sqlite_state_template_factory
):
    template = _sqlite_state_template_factory()
    original = template.read_bytes()
    first = sqlite_db_factory(tmp_path / "first" / "vibe.sqlite")
    second = sqlite_db_factory(tmp_path / "second" / "vibe.sqlite")
    assert first.read_bytes() == second.read_bytes() == original

    with closing(sqlite3.connect(first)) as connection, connection:
        connection.execute("CREATE TABLE test_mutation (value TEXT)")
        connection.execute("INSERT INTO test_mutation VALUES ('first only')")
    third = sqlite_db_factory(tmp_path / "third" / "vibe.sqlite")
    assert template.read_bytes() == second.read_bytes() == third.read_bytes() == original
    assert first.read_bytes() != original
    with closing(sqlite3.connect(third)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()


def test_database_factory_never_overwrites_existing_state(tmp_path, sqlite_db_factory):
    target = tmp_path / "vibe.sqlite"
    target.write_bytes(b"existing database")
    with pytest.raises(FileExistsError):
        sqlite_db_factory(target)
    assert target.read_bytes() == b"existing database"


def test_database_factory_rejects_paths_outside_test_isolation(tmp_path, sqlite_db_factory):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    with pytest.raises(ValueError):
        sqlite_db_factory(outside / "vibe.sqlite")
    assert not outside.exists()

    link = tmp_path / "outside-link"
    link.symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        sqlite_db_factory(link / outside.name / "vibe.sqlite")
    assert not outside.exists()


@pytest.mark.no_sqlite_template
def test_migration_tests_still_begin_without_seeded_state():
    from config import paths

    assert not paths.get_sqlite_state_path().exists()


@pytest.mark.parametrize("fixture", [_reset_cached_sqlite_engines, _reset_oauth_runtime_state])
def test_cache_cleanup_does_not_import_unused_runtime_modules(monkeypatch, fixture):
    names = ("storage.db", "storage.importer", "vibe.remote_access", "vibe.ui_server")
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    imports = []
    original_import = builtins.__import__

    def record_import(name, *args, **kwargs):
        imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", record_import)
    lifecycle = fixture.__wrapped__()
    next(lifecycle)
    with pytest.raises(StopIteration):
        next(lifecycle)
    assert not imports
    assert all(name not in sys.modules for name in names)


@pytest.mark.parametrize("loaded_before_setup", [False, True])
def test_cache_cleanup_covers_owners_loaded_before_or_during_the_test(monkeypatch, loaded_before_setup):
    for name in ("storage.db", "storage.importer", "vibe.remote_access", "vibe.ui_server"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    lifecycles = [
        fixture.__wrapped__()
        for fixture in (_reset_cached_sqlite_engines, _reset_oauth_runtime_state)
    ]
    dispose = Mock()
    reset_importer = Mock()
    clear_hostnames = Mock()
    handshake = {"pending": {}}
    diagnostics = {"client": [1.0]}
    rate_limit = {"client": [1.0]}
    modules = {
        "storage.db": SimpleNamespace(dispose_cached_sqlite_engines=dispose),
        "storage.importer": SimpleNamespace(reset_ensured_sqlite_state=reset_importer),
        "vibe.remote_access": SimpleNamespace(
            _clear_active_hostnames_cache=clear_hostnames, _oauth_handshakes=handshake,
        ),
        "vibe.ui_server": SimpleNamespace(_oauth_diag_log_state=diagnostics, _auth_ratelimit=rate_limit),
    }
    if loaded_before_setup:
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
    for lifecycle in lifecycles:
        next(lifecycle)
    if loaded_before_setup:
        assert not handshake and not diagnostics and not rate_limit
    else:
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
    handshake["pending"] = {}
    diagnostics["client"] = [1.0]
    rate_limit["client"] = [1.0]
    for lifecycle in lifecycles:
        with pytest.raises(StopIteration):
            next(lifecycle)
    for cleanup in (dispose, reset_importer, clear_hostnames):
        assert cleanup.call_count == (2 if loaded_before_setup else 1)
        cleanup.assert_called_with()
    assert not handshake and not diagnostics and not rate_limit
