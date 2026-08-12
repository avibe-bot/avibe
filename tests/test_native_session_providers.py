import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import json

from modules.agents.native_sessions.base import build_resume_preview, build_tail_preview
from modules.agents.native_sessions import claude as claude_module
from modules.agents.native_sessions.claude import ClaudeNativeSessionProvider, encode_project_path
from modules.agents.native_sessions import codex as codex_module
from modules.agents.native_sessions.codex import CodexNativeSessionProvider
from modules.agents.native_sessions.opencode import OpenCodeNativeSessionProvider
from modules.agents.native_sessions import service as service_module
from modules.agents.native_sessions.service import AgentNativeSessionService
from modules.agents.native_sessions.types import NativeResumeSession


def test_claude_provider_falls_back_to_history_jsonl(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    history_path = tmp_path / "history.jsonl"
    projects_root.mkdir(parents=True, exist_ok=True)

    working_path = "/Users/alice/avibe"
    history_path.write_text(
        "\n".join(
            [
                '{"display":"old prompt","timestamp":1766078000000,"project":"/Users/alice/avibe","sessionId":"sess_a"}',
                '{"display":"latest prompt","timestamp":1766079000000,"project":"/Users/alice/avibe","sessionId":"sess_a"}',
                '{"display":"other project","timestamp":1766079100000,"project":"/Users/alice/other","sessionId":"sess_b"}',
            ]
        ),
        encoding="utf-8",
    )

    provider = ClaudeNativeSessionProvider(root=str(projects_root), history_path=str(history_path))

    items = provider.list_metadata(working_path)

    assert [item.native_session_id for item in items] == ["sess_a"]
    hydrated = provider.hydrate_preview(items[0])
    assert hydrated.last_agent_message == "latest prompt"
    assert hydrated.last_agent_tail == "latest prompt"


def test_claude_project_path_encoding_handles_windows_paths() -> None:
    assert encode_project_path("C:\\Users\\alice\\vibe-remote") == "C--Users-alice-vibe-remote"


def test_claude_provider_scans_candidate_jsonl_when_history_has_results(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    history_path = tmp_path / "history.jsonl"
    projects_root.mkdir(parents=True, exist_ok=True)

    working_path = "/Users/alice/avibe"
    history_path.write_text(
        '{"display":"history prompt","timestamp":1766079000000,"project":"/Users/alice/avibe","sessionId":"sess_history"}',
        encoding="utf-8",
    )

    candidate_dir = projects_root / encode_project_path(working_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "sess_sdk.jsonl").write_text(
        "\n".join(
            [
                '{"type":"user","timestamp":"2026-03-27T09:59:00Z","cwd":"/Users/alice/avibe","message":{"content":"sdk prompt"}}',
                '{"type":"assistant","timestamp":"2026-03-27T10:00:00Z","message":{"content":"sdk reply"}}',
            ]
        ),
        encoding="utf-8",
    )

    provider = ClaudeNativeSessionProvider(root=str(projects_root), history_path=str(history_path))

    items = provider.list_metadata(working_path)

    assert {item.native_session_id for item in items} == {"sess_history", "sess_sdk"}


def test_claude_provider_keeps_legacy_slash_only_project_dir(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    history_path = tmp_path / "history.jsonl"
    projects_root.mkdir(parents=True, exist_ok=True)
    history_path.write_text("", encoding="utf-8")

    working_path = "/Users/alice/my.repo"
    legacy_dir = projects_root / working_path.replace("/", "-")
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "sess_legacy.jsonl").write_text(
        "\n".join(
            [
                '{"type":"user","timestamp":"2026-03-27T09:59:00Z","cwd":"/Users/alice/my.repo","message":{"content":"legacy prompt"}}',
                '{"type":"assistant","timestamp":"2026-03-27T10:00:00Z","message":{"content":"legacy reply"}}',
            ]
        ),
        encoding="utf-8",
    )

    provider = ClaudeNativeSessionProvider(root=str(projects_root), history_path=str(history_path))

    items = provider.list_metadata(working_path)

    assert [item.native_session_id for item in items] == ["sess_legacy"]


def test_claude_provider_does_not_scan_unrelated_project_jsonl(tmp_path: Path, monkeypatch) -> None:
    projects_root = tmp_path / "projects"
    history_path = tmp_path / "history.jsonl"
    projects_root.mkdir(parents=True, exist_ok=True)
    history_path.write_text("", encoding="utf-8")

    working_path = "/Users/alice/avibe"
    candidate_dir = projects_root / encode_project_path(working_path)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    target_jsonl = candidate_dir / "sess_target.jsonl"
    target_jsonl.write_text(
        '{"type":"assistant","timestamp":"2026-03-27T10:00:00Z","message":{"content":"done"}}\n',
        encoding="utf-8",
    )

    unrelated_dir = projects_root / "-Users-alice-other"
    unrelated_dir.mkdir(parents=True, exist_ok=True)
    unrelated_jsonl = unrelated_dir / "sess_other.jsonl"
    unrelated_jsonl.write_text(
        '{"type":"assistant","timestamp":"2026-03-27T10:00:00Z","message":{"content":"should not read"}}\n',
        encoding="utf-8",
    )

    read_paths: list[Path] = []
    original_read_json_lines = claude_module.read_json_lines

    def _tracking_read_json_lines(path: Path) -> list[dict]:
        read_paths.append(Path(path))
        return original_read_json_lines(path)

    monkeypatch.setattr(claude_module, "read_json_lines", _tracking_read_json_lines)
    provider = ClaudeNativeSessionProvider(root=str(projects_root), history_path=str(history_path))

    items = provider.list_metadata(working_path)

    assert [item.native_session_id for item in items] == ["sess_target"]
    assert target_jsonl in read_paths
    assert unrelated_jsonl not in read_paths


def test_claude_provider_uses_global_index_fallback_without_scanning_all_jsonl(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    history_path = tmp_path / "history.jsonl"
    projects_root.mkdir(parents=True, exist_ok=True)
    history_path.write_text("", encoding="utf-8")

    working_path = "/Users/alice/avibe"
    indexed_dir = projects_root / "-Users-alice"
    indexed_dir.mkdir(parents=True, exist_ok=True)
    session_jsonl = indexed_dir / "sess_idx.jsonl"
    session_jsonl.write_text(
        "\n".join(
            [
                '{"type":"user","timestamp":"2026-03-27T09:59:00Z","cwd":"/Users/alice/avibe","message":{"content":"hello"}}',
                '{"type":"assistant","timestamp":"2026-03-27T10:00:00Z","message":{"content":"reply from indexed session"}}',
            ]
        ),
        encoding="utf-8",
    )
    (indexed_dir / "sessions-index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "sessionId": "sess_idx",
                        "projectPath": "/Users/alice/avibe",
                        "created": "2026-03-27T09:59:00Z",
                        "modified": "2026-03-27T10:00:00Z",
                        "firstPrompt": "hello",
                        "fullPath": str(session_jsonl),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    provider = ClaudeNativeSessionProvider(root=str(projects_root), history_path=str(history_path))

    items = provider.list_metadata(working_path)

    assert [item.native_session_id for item in items] == ["sess_idx"]
    hydrated = provider.hydrate_preview(items[0])
    assert hydrated.last_agent_message == "reply from indexed session"
    assert hydrated.last_agent_tail.startswith("...")
    assert "indexed session" in hydrated.last_agent_tail


def test_codex_provider_skips_empty_rollout_path(monkeypatch) -> None:
    provider = CodexNativeSessionProvider(db_path="/tmp/does-not-matter.sqlite")
    item = NativeResumeSession(
        agent="codex",
        agent_prefix="cx",
        native_session_id="thread_1",
        working_path="/tmp/project",
        created_at=None,
        updated_at=None,
        sort_ts=1.0,
        locator={"title": "Fallback title", "rollout_path": ""},
    )

    called = False

    def _unexpected_read_json_lines(_path: Path) -> list[dict]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(codex_module, "read_json_lines", _unexpected_read_json_lines)

    hydrated = provider.hydrate_preview(item)

    assert called is False
    assert hydrated.last_agent_message == "Fallback title"
    assert hydrated.last_agent_tail == "Fallback title"


def test_opencode_title_provider_ignores_default_title(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table session (id text primary key, directory text, title text)")
        conn.execute(
            "insert into session (id, directory, title) values (?, ?, ?)",
            ("ses_default", "/repo", "New session - 2026-06-02T07:35:03.127Z"),
        )
        conn.execute(
            "insert into session (id, directory, title) values (?, ?, ?)",
            ("ses_legacy", "/repo", "vibe-remote:base-session-1"),
        )
        conn.execute(
            "insert into session (id, directory, title) values (?, ?, ?)",
            ("ses_title", "/repo", "Implement session titles"),
        )

    provider = OpenCodeNativeSessionProvider(db_path=str(db_path))

    assert provider.get_title(native_session_id="ses_default", working_path="/repo") is None
    assert provider.get_title(native_session_id="ses_legacy", working_path="/repo") is None
    title = provider.get_title(native_session_id="ses_title", working_path="/repo")
    assert title is not None
    assert title.title == "Implement session titles"
    assert title.source == "backend"
    assert title.confidence == "high"


def test_opencode_title_provider_uses_xdg_data_home(tmp_path: Path, monkeypatch) -> None:
    data_home = tmp_path / "xdg-data"
    db_path = data_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table session (id text primary key, directory text, title text)")
        conn.execute(
            "insert into session (id, directory, title) values (?, ?, ?)",
            ("ses_title", "/repo", "Use XDG data home"),
        )

    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    provider = OpenCodeNativeSessionProvider()

    assert provider.db_path == db_path
    title = provider.get_title(native_session_id="ses_title", working_path="/repo")
    assert title is not None
    assert title.title == "Use XDG data home"


def test_codex_title_provider_reads_thread_title(tmp_path: Path) -> None:
    db_path = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text,
                first_user_message text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            ("thread_empty", "/repo", 0, "", ""),
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            ("thread_title", "/repo", 0, "Backend title", ""),
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            ("thread_archived", "/repo", 1, "Archived title", "Archived prompt"),
        )

    provider = CodexNativeSessionProvider(db_path=str(db_path))

    assert provider.get_title(native_session_id="thread_empty", working_path="/repo") is None
    assert provider.get_title(native_session_id="thread_archived", working_path="/repo") is None
    title = provider.get_title(native_session_id="thread_title", working_path="/repo")
    assert title is not None
    assert title.title == "Backend title"
    assert title.source == "backend"


def test_codex_title_provider_prefers_derived_first_message_over_thread_title(tmp_path: Path) -> None:
    db_path = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text,
                first_user_message text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            (
                "thread_with_first_message",
                "/repo",
                0,
                "Maybe generated title",
                "帮我检查 Codex session title",
            ),
        )

    provider = CodexNativeSessionProvider(db_path=str(db_path))

    title = provider.get_title(native_session_id="thread_with_first_message", working_path="/repo")

    assert title is not None
    assert title.title == "帮我检查 Codex"
    assert title.source == "derived_first_prompt"
    assert title.confidence == "low"


def test_codex_title_provider_derives_when_thread_title_is_first_message(tmp_path: Path) -> None:
    db_path = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text,
                first_user_message text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            (
                "thread_prompt_title",
                "/repo",
                0,
                "  帮我\n实现 session title 回填  ",
                "  帮我\n实现 session title 回填  ",
            ),
        )

    provider = CodexNativeSessionProvider(db_path=str(db_path))

    title = provider.get_title(native_session_id="thread_prompt_title", working_path="/repo")

    assert title is not None
    assert title.title == "帮我 实现 sess"
    assert title.source == "derived_first_prompt"
    assert title.confidence == "low"


def test_codex_title_provider_honors_codex_home(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    db_path = codex_home / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text,
                first_user_message text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            ("thread_title", "/repo", 0, "CODEX_HOME title", ""),
        )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    provider = CodexNativeSessionProvider()

    title = provider.get_title(native_session_id="thread_title", working_path="/repo")

    assert title is not None
    assert title.title == "CODEX_HOME title"


def test_codex_title_provider_reads_legacy_schema_without_first_message(tmp_path: Path) -> None:
    db_path = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title) values (?, ?, ?, ?)",
            ("thread_legacy", "/repo", 0, "Legacy backend title"),
        )

    provider = CodexNativeSessionProvider(db_path=str(db_path))

    title = provider.get_title(
        native_session_id="thread_legacy",
        working_path="/repo",
        first_user_message="Caller prompt should not replace legacy title",
    )

    assert title is not None
    assert title.title == "Legacy backend title"
    assert title.source == "backend"
    assert title.confidence == "high"


def test_codex_title_provider_preserves_title_when_first_message_column_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table threads (
                id text primary key,
                cwd text,
                archived integer,
                title text,
                first_user_message text
            )
            """
        )
        conn.execute(
            "insert into threads (id, cwd, archived, title, first_user_message) values (?, ?, ?, ?, ?)",
            ("thread_sparse", "/repo", 0, "Sparse backend title", ""),
        )

    provider = CodexNativeSessionProvider(db_path=str(db_path))

    title = provider.get_title(
        native_session_id="thread_sparse",
        working_path="/repo",
        first_user_message="Caller prompt should not replace sparse title",
    )

    assert title is not None
    assert title.title == "Sparse backend title"
    assert title.source == "backend"
    assert title.confidence == "high"


def test_claude_title_provider_derives_first_10_visible_chars(tmp_path: Path) -> None:
    provider = ClaudeNativeSessionProvider(root=str(tmp_path / "projects"), history_path=str(tmp_path / "history.jsonl"))

    title = provider.get_title(
        native_session_id="claude-1",
        working_path="/repo",
        first_user_message="  帮我\n实现 session title 回填  ",
    )

    assert title is not None
    assert title.title == "帮我 实现 sess"
    assert title.source == "derived_first_prompt"
    assert title.confidence == "low"


def test_native_session_service_preserves_agent_visibility_when_limited() -> None:
    def _item(agent: str, prefix: str, session_id: str, sort_ts: float) -> NativeResumeSession:
        return NativeResumeSession(
            agent=agent,
            agent_prefix=prefix,
            native_session_id=session_id,
            working_path="/tmp/project",
            created_at=None,
            updated_at=None,
            sort_ts=sort_ts,
            last_agent_message=session_id,
            last_agent_tail=f"...{session_id[-4:]}",
        )

    oc_provider = SimpleNamespace(
        agent_name="opencode",
        list_metadata=lambda working_path: [_item("opencode", "oc", f"oc_{i}", 200 - i) for i in range(5)],
        hydrate_preview=lambda item: item,
    )
    cc_provider = SimpleNamespace(
        agent_name="claude",
        list_metadata=lambda working_path: [_item("claude", "cc", "cc_1", 50)],
        hydrate_preview=lambda item: item,
    )
    cx_provider = SimpleNamespace(
        agent_name="codex",
        list_metadata=lambda working_path: [_item("codex", "cx", f"cx_{i}", 100 - i) for i in range(5)],
        hydrate_preview=lambda item: item,
    )

    service = AgentNativeSessionService(providers=[oc_provider, cc_provider, cx_provider])

    items = service.list_recent_sessions("/tmp/project", limit=5)

    assert len(items) == 5
    assert {item.agent for item in items} == {"opencode", "claude", "codex"}


def test_native_session_service_loads_default_providers_lazily(monkeypatch) -> None:
    calls: list[str] = []

    class _StubProvider:
        agent_name = "claude"

        def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
            return []

        def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
            return item

    def _fake_import_module(module_path: str):
        calls.append(module_path)
        return SimpleNamespace(ClaudeNativeSessionProvider=_StubProvider)

    monkeypatch.setattr(service_module.importlib, "import_module", _fake_import_module)
    service = AgentNativeSessionService(
        provider_specs=(
            service_module.NativeSessionProviderSpec(
                agent_name="claude",
                module_path="modules.agents.native_sessions.claude",
                class_name="ClaudeNativeSessionProvider",
            ),
        )
    )

    assert calls == []

    assert service.list_recent_sessions("/tmp/project", limit=5) == []
    assert calls == ["modules.agents.native_sessions.claude"]


def test_native_session_lightweight_imports_do_not_require_sqlite() -> None:
    """The agent-setup / command-handler / session-handler import path must NOT
    transitively pull in sqlite: those modules only need the avibe-cloud URL
    availability helpers, which now live in the storage-free ``core.avibe_cloud``
    (not ``core.show_pages``, which imports ``storage.db`` to back ``ShowPageStore``)."""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    script = """
import importlib.abc

class BlockSqlite(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlite3" or fullname.startswith("sqlite3.") or fullname == "_sqlite3":
            raise ImportError("blocked sqlite for test")
        return None

import sys
sys.meta_path.insert(0, BlockSqlite())

for module_name in [
    "modules.agents.native_sessions",
    "core.handlers.command_handlers",
    "core.handlers.session_handler",
]:
    __import__(module_name)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_build_tail_preview_strips_edge_symbols() -> None:
    assert build_tail_preview("前文很多很多很多。**最后一句话？？？**") == "...很多。**最后一句话"


def test_build_resume_preview_preserves_line_breaks() -> None:
    text = "第一段第一行\n第二行\n\n第三行\n---\n[button]"

    assert build_resume_preview(text, limit=200) == "第一段第一行\n第二行\n\n第三行"
