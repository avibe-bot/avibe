#!/usr/bin/env python3
"""Exercise exactly one published predecessor, optionally enabling no-copy in a build.

CI runs the probe without --activate. The existing publisher runs it immediately
before packaging and fills one literal in storage/compatibility.py. An activated
source literal must also pass CI's probe; None keeps a build conservative.
There is no runtime network lookup or persistent proof store.
The --probe subprocess mode must not add this checkout to sys.path: -I imports the
actual previous wheel, not today's editable/source implementation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FUTURE_REVISION = "20991231_9999"
EXTRA = {"opaque": ["future", {"value": 7}]}
TABLES = ("scope_settings", "agent_sessions", "runtime_records", "run_definitions", "messages", "agent_events", "state_meta")


def _probe(action: str, home: Path) -> dict:
    """Run common real store APIs under whichever binary owns this interpreter."""
    import storage
    from storage import importer, migrations

    db = home / "state" / "vibe.sqlite"
    try:
        version = importlib.metadata.version("avibe-os")
    except importlib.metadata.PackageNotFoundError:
        version = "source"
    identity = {"version": version, "storage": str(Path(storage.__file__).resolve())}
    if action == "identity":
        try:
            from storage.compatibility import SUPPORTED_COMPATIBILITY_EPOCHS
        except ModuleNotFoundError as exc:
            if exc.name != "storage.compatibility":
                raise
            return {**identity, "supported_epochs": []}
        return {**identity, "supported_epochs": sorted(SUPPORTED_COMPATIBILITY_EPOCHS)}
    if action == "open":
        try:
            migrations.run_migrations(db)
        except migrations.IncompatibleDatabaseRevisionError:
            return {**identity, "refused": True}
        return {**identity, "refused": False}

    from config.v2_settings import ChannelSettings, SettingsState
    from storage import agent_events_service, messages_service
    from storage.background import SQLiteBackgroundTaskStore
    from storage.db import create_sqlite_engine
    from storage.sessions_service import SQLiteSessionsService
    from storage.settings_service import SQLiteSettingsService
    from vibe.authorization import AuthorizationContext

    owner = AuthorizationContext(instance_role="owner")
    fixture_file = home / "rows.json"
    if action == "seed":
        importer.repair_sqlite_state(db_path=db, state_dir=db.parent)
        settings = SQLiteSettingsService(db)
        sessions = SQLiteSessionsService(db)
        harness = SQLiteBackgroundTaskStore(db)
        try:
            settings.save_state(SettingsState(channels={
                "slack::reader-contract": ChannelSettings(enabled=True, custom_cwd=str(home / "workspace")),
            }), user_context=owner)
            if fixture_file.exists():
                session = json.loads(fixture_file.read_text())["session"]
            else:
                session = sessions.reserve_standalone_agent_session(
                    agent_backend="codex", session_anchor="reader-contract", workdir=str(home / "workspace"),
                    authorization_context=owner,
                )
                assert sessions.bind_agent_session_by_id(
                    session_id=session, native_session_id="reader-contract-native",
                ) == session
            assert sessions.set_agent_session_runtime_marker(
                session, backend="codex", native_session_id="reader-contract-native",
                key="reader_contract_extra", value=EXTRA,
            )
            sessions.upsert_active_poll({
                "opencode_session_id": "reader-contract-poll", "platform": "slack",
                "settings_key": "reader-contract", "base_session_id": "reader-contract",
                "reader_contract_extra": EXTRA,
            })
            assert harness.upsert_watch({
                "id": "reader-contract-watch", "name": "new runtime", "shell_command": "true", "enabled": False,
                "created_at": "2026-11-08T00:00:00Z", "updated_at": "2026-11-08T00:00:00Z",
            })
            with sessions.engine.begin() as conn:
                message = messages_service.append(
                    conn, scope_id=None, session_id=session, platform="slack", author="agent", message_type="output",
                    content={"text": "new runtime", "reader_contract_extra": EXTRA},
                )
                event = agent_events_service.append(
                    conn, scope_id=None, session_id=session, platform="slack", event_type="tool_call",
                    text="new tool trace", content={"reader_contract_extra": EXTRA},
                )
            fixture_file.write_text(json.dumps({"session": session, "message": message["id"], "event": event["id"]}))
        finally:
            settings.close()
            sessions.close()
            harness.close()
        return identity

    fixture = json.loads(fixture_file.read_text())
    settings = SQLiteSettingsService(db)
    sessions = SQLiteSessionsService(db)
    harness = SQLiteBackgroundTaskStore(db)
    engine = create_sqlite_engine(db)
    try:
        state = settings.load_state()
        assert "slack::reader-contract" in state.channels
        session = sessions.get_agent_session_by_id(fixture["session"])
        assert session is not None
        stored = sessions.load_state()
        assert stored.active_polls["reader-contract-poll"]["reader_contract_extra"] == EXTRA
        watch = harness.get_watch("reader-contract-watch")
        assert watch is not None and not watch["enabled"]
        with engine.connect() as conn:
            message = messages_service.get_message(conn, fixture["message"])
            assert message["type"] == "output" and message["text"] == "new runtime"
            assert message["content"]["reader_contract_extra"] == EXTRA
            history = messages_service.list_session_messages(conn, session_id=fixture["session"], authorization_context=owner)
            assert any(row["id"] == fixture["message"] for row in history["messages"])
        if action == "exercise":
            state.channels["slack::reader-contract"].require_mention = True
            settings.save_state(state, user_context=owner)
            sessions.save_state(stored)
            # Whole-record upserts catch INSERT OR REPLACE-style loss of new
            # columns. JSON bags must retain their uninterpreted extensions.
            assert harness.upsert_watch({**watch, "name": "previous writer"})
            with engine.begin() as conn:
                messages_service.append(
                    conn, scope_id=None, session_id=fixture["session"], platform="slack", author="agent",
                    message_type="output", text="previous writer",
                )
                agent_events_service.append(
                    conn, scope_id=None, session_id=fixture["session"], platform="slack",
                    event_type="tool_call", text="previous writer",
                )
        elif action == "verify":
            assert state.channels["slack::reader-contract"].require_mention is True
            assert watch["name"] == "previous writer"
            with engine.connect() as conn:
                history = messages_service.list_session_messages(conn, session_id=fixture["session"], authorization_context=owner)
                assert any(row["text"] == "previous writer" for row in history["messages"])
        else:
            raise ValueError(f"unknown probe action {action}")
    finally:
        settings.close()
        sessions.close()
        harness.close()
        engine.dispose()
    return identity


def _run(*args: str, **kwargs) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)
    return result.stdout


def previous_release(repo: str, *, release_tag: str | None = None) -> dict:
    pages = json.loads(_run("gh", "api", f"repos/{repo}/releases?per_page=100", "--paginate", "--slurp"))
    releases = [item for page in pages for item in page if not item["draft"] and item.get("published_at")]
    # A rerun of an already-published target still compares with its immediate
    # predecessor, not with itself or a release published after that target.
    target = next((item for item in releases if item["tag_name"] == release_tag), None)
    candidates = [item for item in releases if item["tag_name"] != release_tag
                  and (target is None or item["published_at"] < target["published_at"])]
    if not candidates:
        raise RuntimeError("No published predecessor is available; cannot verify the reader contract")
    return max(candidates, key=lambda item: item["published_at"])


def _probe_env(home: Path) -> dict[str, str]:
    # Do not leak the caller's Avibe routing, credentials, config overrides or
    # Python import environment into either binary. All writes are test-owned.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG") if key in os.environ}
    for name, subdir in {
        "HOME": "home", "AVIBE_HOME": "avibe", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "xdg-state", "XDG_CACHE_HOME": "cache", "TMPDIR": "tmp",
    }.items():
        path = home / subdir
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    return env


def run_probe(python: Path, action: str, home: Path, *, current: bool = False) -> dict:
    command = [str(python), "-I"]
    arguments = [str(Path(__file__).resolve()), "--probe", action, "--home", str(home)]
    if current:
        # Only the candidate process gets source imports. The released binary
        # always executes the external script with isolated site-packages.
        command += ["-c", f"import sys,runpy;sys.path.insert(0,{str(ROOT)!r});sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"]
    try:
        result = _run(*command, *arguments, cwd=home, env=_probe_env(home))
    except subprocess.CalledProcessError as exc:
        # These subprocesses receive only the sanitized fixture environment.
        raise RuntimeError(f"Isolated {action} probe failed:\n{exc.stderr[-6000:]}") from exc
    return json.loads(result.strip().splitlines()[-1])


def _artifacts(home: Path) -> dict[str, bytes]:
    state = home / "state"
    files = list(state.glob(".*.backup-lifecycle.json")) + list((state / "backups").rglob("*"))
    return {str(path.relative_to(state)): path.read_bytes() for path in files if path.is_file()}


def _logical_db(db: Path) -> str:
    with closing(sqlite3.connect(db)) as conn:
        return "\n".join(conn.iterdump())


def _column_values(conn: sqlite3.Connection, table: str) -> dict[tuple, tuple]:
    keys = [row[1] for row in sorted(conn.execute(f'PRAGMA table_info("{table}")'), key=lambda row: row[5]) if row[5]]
    columns = ", ".join(f'"{key}"' for key in keys)
    return {tuple(row[:-2]): tuple(row[-2:]) for row in conn.execute(
        f'SELECT {columns}, reader_contract_optional, reader_contract_default FROM "{table}"'
    )}


def verify_predecessor(python: Path, home: Path, *, expected_version: str) -> int | None:
    identity = run_probe(python, "identity", home)
    if identity["version"] != expected_version or not Path(identity["storage"]).is_relative_to(python.parent.parent):
        raise RuntimeError(f"Probe imported the wrong predecessor: {identity}")
    print(json.dumps({"previous_binary": identity}), flush=True)
    run_probe(python, "seed", home)
    run_probe(python, "exercise", home)
    run_probe(Path(sys.executable), "seed", home, current=True)
    db = home / "state" / "vibe.sqlite"
    with closing(sqlite3.connect(db)) as conn, conn:
        row = conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone()
        epoch = json.loads(row[0])
        conn.execute("UPDATE alembic_version SET version_num=?", (FUTURE_REVISION,))
        for table in TABLES:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN reader_contract_optional TEXT')
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN reader_contract_default INTEGER NOT NULL DEFAULT 17')
            conn.execute(f'UPDATE "{table}" SET reader_contract_optional=?, reader_contract_default=23', (table,))
        conn.execute("CREATE TABLE reader_contract_independent(id TEXT PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO reader_contract_independent VALUES ('new', 'preserve')")
        before_columns = {table: _column_values(conn, table) for table in TABLES}
        receipts = conn.execute("SELECT key, value_json, updated_at FROM state_meta WHERE key IN "
                                "('compatibility_epoch', 'json_import_completed_at', 'background_json_import_completed_at') "
                                "ORDER BY key").fetchall()
    artifacts = _artifacts(home)
    if epoch not in identity["supported_epochs"]:
        before = _logical_db(db)
        assert run_probe(python, "open", home)["refused"]
        assert _logical_db(db) == before and _artifacts(home) == artifacts
        print(json.dumps({"reader_contract": "not-applicable", "reason": "predecessor lacks this epoch", "epoch": epoch}), flush=True)
        return None
    assert not run_probe(python, "open", home)["refused"]
    run_probe(python, "exercise", home)
    run_probe(Path(sys.executable), "verify", home, current=True)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(FUTURE_REVISION,)]
        assert conn.execute("SELECT key, value_json, updated_at FROM state_meta WHERE key IN "
                            "('compatibility_epoch', 'json_import_completed_at', 'background_json_import_completed_at') "
                            "ORDER BY key").fetchall() == receipts
        assert conn.execute("SELECT * FROM reader_contract_independent").fetchall() == [("new", "preserve")]
        for table, existing in before_columns.items():
            after = _column_values(conn, table)
            assert all(after.get(key) == value for key, value in existing.items()), f"{table}: old writer lost new values"
            assert all(value == (None, 17) for key, value in after.items() if key not in existing), f"{table}: omitted defaults"
        session = json.loads((home / "rows.json").read_text())["session"]
        payload = conn.execute("SELECT metadata_json FROM agent_sessions WHERE id=?", (session,)).fetchone()[0]
        assert json.loads(payload)["reader_contract_extra"] == EXTRA
    assert _artifacts(home) == artifacts
    # A real protocol reader also refuses unsupported epochs without mutations.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE state_meta SET value_json=? WHERE key='compatibility_epoch'", (str(max(identity["supported_epochs"]) + 1),))
    before = _logical_db(db)
    assert run_probe(python, "open", home)["refused"]
    assert _logical_db(db) == before and _artifacts(home) == artifacts
    print(json.dumps({"reader_contract": "passed", "verified_epoch": epoch}), flush=True)
    return epoch


def activate_build(epoch: int | None) -> None:
    path = ROOT / "storage" / "compatibility.py"
    source = path.read_text(encoding="utf-8")
    replacement = f"VERIFIED_PREDECESSOR_EPOCH: int | None = {epoch!r}"
    source, count = re.subn(r"(?m)^VERIFIED_PREDECESSOR_EPOCH: int \| None = (?:None|[1-9][0-9]*)$", replacement, source)
    if count != 1:
        raise RuntimeError("Missing/ambiguous build capability literal")
    path.write_text(source, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="avibe-bot/avibe")
    parser.add_argument("--release-tag", help="Target being published (excluded from predecessor selection)")
    parser.add_argument("--activate", action="store_true", help="Fill the release build's capability literal after verification")
    parser.add_argument("--probe", choices=("identity", "seed", "open", "exercise", "verify"), help=argparse.SUPPRESS)
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe:
        print(json.dumps(_probe(args.probe, args.home)))
        return 0
    sys.path.insert(0, str(ROOT))
    from scripts.release_package_version import package_version_from_release_tag

    release = previous_release(args.repo, release_tag=args.release_tag)
    tag = release["tag_name"]
    version = package_version_from_release_tag(tag)
    name = f"avibe_os-{version}-py3-none-any.whl"
    asset = next((asset for asset in release["assets"] if asset["name"] == name), None)
    if asset is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise RuntimeError(f"Published predecessor {tag} lacks a checksummed {name}")
    with tempfile.TemporaryDirectory(prefix="sqlite-predecessor-") as directory:
        home = Path(directory).resolve()
        _run("gh", "release", "download", tag, "--repo", args.repo, "--pattern", name, "--dir", str(home))
        wheel = home / name
        digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest != asset["digest"]:
            raise RuntimeError("Published predecessor wheel checksum mismatch")
        print(json.dumps({"previous_release": tag, "published_at": release["published_at"], "wheel": name, "digest": digest}), flush=True)
        venv = home / "venv"
        _run("uv", "venv", "--python", sys.executable, str(venv))
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run("uv", "pip", "install", "--python", str(python), str(wheel))
        epoch = verify_predecessor(python, home, expected_version=version)
        if previous_release(args.repo, release_tag=args.release_tag)["tag_name"] != tag:
            raise RuntimeError("Published predecessor changed during verification; rerun against the actual predecessor")
    if args.activate:
        activate_build(epoch)
    else:
        from storage.compatibility import VERIFIED_PREDECESSOR_EPOCH

        if VERIFIED_PREDECESSOR_EPOCH is not None and VERIFIED_PREDECESSOR_EPOCH != epoch:
            raise RuntimeError("This source enables no-copy without a passing published-predecessor contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
