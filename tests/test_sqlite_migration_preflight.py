"""The migration owner must refuse incompatible revisions before any mutation.

The old backup decision only compared stamps, so it both copied incompatible
state and repaired its schema before Alembic rejected the revision. Existing
migration/backup coverage exercises valid upgrades, not this refusal boundary.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from alembic.script import ScriptDirectory
import pytest

from storage import migrations
import vibe


pytestmark = pytest.mark.no_sqlite_template


def _seed_database(db_path: Path, revisions: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            "CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);"
            "CREATE TABLE agents(id TEXT PRIMARY KEY);"
            "INSERT INTO agents VALUES ('preserve-me');"
        )
        conn.executemany("INSERT INTO alembic_version VALUES (?)", [(revision,) for revision in revisions])


def _snapshot(db_path: Path) -> dict[str, bytes]:
    """Logical DB/schema and exact rollback artifacts, not SQLite bookkeeping."""
    with closing(sqlite3.connect(db_path)) as conn:
        files = {"database.sql": "\n".join(conn.iterdump()).encode()}
    candidates = list(db_path.parent.glob(".*.backup-lifecycle.json"))
    root = db_path.parent / "backups"
    files.update({str(path.relative_to(db_path.parent)): path.read_bytes() for path in candidates if path.is_file()})
    if root.exists():
        files["backups/"] = b""
        for path in root.rglob("*"):
            files[str(path.relative_to(db_path.parent)) + ("/" if path.is_dir() else "")] = (
                b"" if path.is_dir() else path.read_bytes()
            )
    return files


def _refuse_unchanged(
    db_path: Path, *, revision: str = "head", before: dict[str, bytes] | None = None
) -> str:
    if before is None:
        before = _snapshot(db_path)
    with pytest.raises(Exception) as caught:
        migrations.run_migrations(db_path, revision=revision)
    # This assertion fails on the old code because backup/repair already ran,
    # before checking the new exception type or its diagnostic contract.
    assert _snapshot(db_path) == before
    assert isinstance(caught.value, migrations.IncompatibleDatabaseRevisionError)
    message = str(caught.value)
    assert str(db_path.resolve()) in message
    assert sys.executable in message
    assert vibe.__version__ in message
    assert str(Path(migrations.__file__).resolve()) in message
    assert "stored revisions" in message
    assert "installed heads" in message
    assert "requested target" in message
    assert "Hint:" in message
    return message


@pytest.mark.parametrize("journal", ["delete", "wal", "wal-closed"])
@pytest.mark.parametrize("stored", ["future", "mixed", "prefix", "symbolic"])
def test_incompatible_revision_refusal_preserves_repairable_database_and_backups(
    tmp_path: Path, journal: str, stored: str
) -> None:
    head = ScriptDirectory.from_config(migrations.alembic_config()).get_current_head()
    revisions = {
        "future": ("20990101_9999",),
        "mixed": (head, "20990101_9999"),
        "prefix": (head[:-1],),
        "symbolic": ("head",),
    }[stored]
    db_path = tmp_path / "vibe.sqlite"
    _seed_database(db_path, (head,))
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "keep-me.txt").write_bytes(b"existing rollback data")
    receipt = tmp_path / ".vibe.sqlite.backup-lifecycle.json"
    receipt.write_bytes(b'{"phase":"ready","preserve":"exact bytes"}\n')
    # Exercise committed WAL revisions both with a live writer and after clean
    # close. SQLite's own checkpoint/sidecar bookkeeping is allowed on open.
    with closing(sqlite3.connect(db_path)) as owner:
        owner.execute(f"PRAGMA journal_mode={journal.split('-')[0]}")
        with owner:
            owner.execute("DELETE FROM alembic_version")
            owner.executemany("INSERT INTO alembic_version VALUES (?)", [(value,) for value in revisions])
            owner.execute("INSERT INTO agents VALUES ('committed-tail')")
        if journal == "wal-closed":
            owner.close()
        for _ in range(2):
            message = _refuse_unchanged(db_path)
            assert all(value in message for value in revisions)
            assert head in message
    with closing(sqlite3.connect(db_path)) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(agents)")] == ["id"]
        assert conn.execute("SELECT id FROM agents ORDER BY id").fetchall() == [
            ("committed-tail",), ("preserve-me",)
        ]


def test_unknown_stamp_without_user_tables_is_refused_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            "CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);"
            "INSERT INTO alembic_version VALUES ('20990101_9999');"
        )
    assert "20990101_9999" in _refuse_unchanged(db_path)
    assert not (tmp_path / "backups").exists()


def test_ancestor_target_refused_before_backup_or_repair(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _seed_database(db_path, ("20261030_0072",))
    message = _refuse_unchanged(db_path, revision="20260821_0060")
    assert "20261030_0072" in message
    assert "20260821_0060" in message
    assert not (tmp_path / "backups").exists()


@pytest.fixture
def revision_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Alembic DAG: lexical order runs backwards and two heads merge."""
    root = tmp_path / "alembic"
    versions = root / "versions"
    versions.mkdir(parents=True)
    shutil.copyfile(migrations.alembic_dir() / "env.py", root / "env.py")
    for revision, parent in (
        ("z_root", None), ("left", "z_root"), ("right", "z_root"), ("a_merge", ("left", "right"))
    ):
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {parent!r}\n"
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    pass\ndef downgrade():\n    pass\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(migrations, "alembic_dir", lambda: root)


@pytest.mark.parametrize("stored", ["z_root", "20990101_9999"])
def test_hot_journal_recovers_committed_revision_before_preflight(
    tmp_path: Path, revision_graph: None, stored: str
) -> None:
    """A crash must not block compatible startup or expose an uncommitted stamp."""
    db_path = tmp_path / "vibe.sqlite"
    _seed_database(db_path, (stored,))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE payload(data BLOB)")
    before = _snapshot(db_path)
    # Spill an uncommitted revision/page change, then exit without cleanup.
    # A read-only/immutable preflight cannot recover this committed snapshot.
    subprocess.run(
        [sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=1')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE alembic_version SET version_num = 'left'")
for _ in range(20):
    conn.execute('INSERT INTO payload VALUES (zeroblob(8192))')
os._exit(0)
""", str(db_path)], check=True,
    )
    journal = db_path.with_name(db_path.name + "-journal")
    assert journal.read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
    if stored == "20990101_9999":
        # Do not read the original between the crash and the migration entry:
        # preflight itself must trigger recovery before rejecting the revision.
        _refuse_unchanged(db_path, before=before)
    else:
        migrations.run_migrations(db_path)
        backup = next((tmp_path / "backups").glob("*/vibe.sqlite"))
        assert _snapshot(backup)["database.sql"] == before["database.sql"]
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("a_merge",)]
            assert conn.execute("SELECT COUNT(*) FROM payload").fetchone() == (0,)
    assert not journal.exists()


def test_known_sibling_target_is_not_a_forward_upgrade(tmp_path: Path, revision_graph: None) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _seed_database(db_path, ("right",))
    message = _refuse_unchanged(db_path, revision="left")
    assert "left" in message and "right" in message and "a_merge" in message


@pytest.mark.parametrize("current", [("z_root",), ("left", "right")])
def test_forward_graph_paths_keep_fresh_backup_and_same_head_skips_it(
    tmp_path: Path, revision_graph: None, current: tuple[str, ...]
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    _seed_database(db_path, current)
    with closing(sqlite3.connect(db_path)) as conn:
        before = list(conn.iterdump())
    migrations.run_migrations(db_path)
    backups = list((tmp_path / "backups").glob("*/vibe.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert list(conn.iterdump()) == before
    manifest = json.loads(backups[0].with_name("manifest.json").read_text())
    assert manifest["from_revisions"] == sorted(current)
    assert manifest["to_revisions"] == ["a_merge"]
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("a_merge",)]
        assert conn.execute("SELECT id FROM agents").fetchall() == [("preserve-me",)]
    before_same_head = {name: data for name, data in _snapshot(db_path).items() if name.startswith("backups/")}
    migrations.run_migrations(db_path)
    assert {name: data for name, data in _snapshot(db_path).items() if name.startswith("backups/")} == before_same_head
