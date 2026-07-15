import json
import os
from dataclasses import dataclass

import pytest

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from core.show_pages import (
    ShowPage,
    ShowPageError,
    ShowPageStore,
    _default_index_html,
    _extract_icon_path,
    ensure_show_page_dir,
    show_cli_event_token,
    show_page_payload,
)
from storage.pagination import PageRequest
from vibe import cli


@dataclass(frozen=True)
class _FakeShowRuntimeResult:
    available: bool
    reason: str | None = None


def _stub_runtime_prepare_dependencies(
    monkeypatch,
    *,
    askill_result=None,
    avault_result=None,
    tmux_result=None,
    git_result=None,
):
    calls = {"askill": [], "avault": [], "tmux": [], "git": []}

    def fake_askill(offline=False):
        calls["askill"].append({"offline": offline})
        return askill_result or {"ok": True, "installed": True}

    def fake_avault(offline=False):
        calls["avault"].append({"offline": offline})
        return avault_result or {"ok": True, "installed": True}

    def fake_tmux(offline=False, force=False):
        calls["tmux"].append({"offline": offline, "force": force})
        return tmux_result or {"ok": True, "installed": True}

    def fake_git(offline=None, force=False):
        calls["git"].append({"offline": offline, "force": force})
        return git_result or {"ok": True, "installed": True}

    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", fake_askill)
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", fake_avault)
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", fake_tmux)
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", fake_git)
    return calls


def test_show_without_subcommand_prints_help(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["show"])

    assert args.command == "show"
    assert args.show_command is None

    assert cli.cmd_show(args) == 0
    captured = capsys.readouterr()
    assert "Manage the one visual Show Page attached to an Agent Session." in captured.out
    assert "usage: vibe show [-h] {list,path,status,update,mark,event} ..." in captured.out
    assert "vibe show list" in captured.out
    assert "vibe show path --session-id sesk8m4q2p7x" in captured.out


def test_show_path_help_uses_explicit_session_id(capsys):
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["show", "path", "--help"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Run: vibe show path --session-id sesk8m4q2p7x" in captured.out
    assert "`vibe show update --session-id sesk8m4q2p7x --visibility public`" in captured.out
    assert "Run: vibe show path\n" not in captured.out


def test_runtime_prepare_cli_reports_warning_only_failure(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--json"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            assert force is False
            assert offline is None
            return {"ok": False, "reason": "runtime_node_missing"}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    calls = _stub_runtime_prepare_dependencies(monkeypatch)

    assert cli.cmd_runtime(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "runtime_node_missing"
    assert payload["askill"] == {"ok": True, "installed": True}
    assert payload["avault"] == {"ok": True, "installed": True}
    assert payload["tmux"] == {"ok": True, "installed": True}
    assert payload["git"] == {"ok": True, "installed": True}
    assert calls["tmux"] == [{"offline": False, "force": False}]
    assert calls["git"] == [{"offline": None, "force": False}]


def test_runtime_prepare_cli_preserves_offline_environment(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--json"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            assert force is False
            assert offline is None
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    calls = _stub_runtime_prepare_dependencies(monkeypatch)

    assert cli.cmd_runtime(args) == 0
    assert calls["tmux"] == [{"offline": False, "force": False}]
    assert calls["git"] == [{"offline": None, "force": False}]


def test_runtime_manager_from_args_preserves_offline_environment(monkeypatch, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "status"])
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_OFFLINE", "1")

    manager = cli._show_runtime_manager_from_args(args)

    assert manager.offline is True


def test_runtime_status_reports_effective_git_resolution(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "status", "--json"])

    class FakeRuntimeManager:
        def status(self):
            return {"provider": "manifest-cache", "installed": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_git_runtime_status",
        lambda: {
            "id": "git",
            "resolution": "vendored",
            "path": "/tmp/runtime/git/bin/git",
            "version": "2.55.0",
            "agent": {
                "resolution": "system",
                "path": "/usr/bin/git",
                "version": "2.50.1",
            },
        },
    )

    assert cli.cmd_runtime(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["git"]["resolution"] == "vendored"
    assert payload["git"]["path"].endswith("/bin/git")
    assert payload["git"]["agent"]["resolution"] == "system"


def test_runtime_clean_cleans_git_runtime(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "clean", "--json", "--keep-previous", "2"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1):
            assert keep_previous == 2
            return {"ok": True, "removed": ["show-old"]}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        cli,
        "_clean_git_runtime",
        lambda *, keep_previous: {"ok": True, "removed": [f"git-old-{keep_previous}"]},
    )

    assert cli.cmd_runtime(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["git"]["removed"] == ["git-old-2"]


def test_runtime_prepare_cli_strict_fails_when_prepare_fails(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--strict"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": False, "reason": "runtime_archive_download_failed"}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    calls = _stub_runtime_prepare_dependencies(monkeypatch)

    assert cli.cmd_runtime(args) == 1
    assert "runtime_archive_download_failed" in capsys.readouterr().err
    assert calls["tmux"] == [{"offline": False, "force": False}]


def test_runtime_prepare_cli_strict_fails_when_git_prepare_fails(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--strict"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    _stub_runtime_prepare_dependencies(
        monkeypatch,
        git_result={"ok": False, "reason": "git_archive_checksum_mismatch"},
    )

    assert cli.cmd_runtime(args) == 1
    assert "git_archive_checksum_mismatch" in capsys.readouterr().err


def test_runtime_prepare_cli_strict_allows_pending_git_publication(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--strict"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    _stub_runtime_prepare_dependencies(
        monkeypatch,
        git_result={"ok": False, "reason": "git_runtime_unpublished"},
    )

    assert cli.cmd_runtime(args) == 0
    assert "git_runtime_unpublished" in capsys.readouterr().err


def test_runtime_prepare_cli_strict_allows_unsupported_git_platform(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--strict"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    _stub_runtime_prepare_dependencies(
        monkeypatch,
        git_result={"ok": False, "reason": "git_platform_unsupported"},
    )

    assert cli.cmd_runtime(args) == 0
    assert "git_platform_unsupported" in capsys.readouterr().err


def test_runtime_prepare_cli_skips_avault_offline(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--offline", "--json"])
    seen = {"askill": None, "avault": None, "tmux": None, "git": None}

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            assert offline is True
            return {"ok": True}

    def fake_askill(offline=False):
        seen["askill"] = offline
        return {"ok": True, "skipped": True, "reason": "offline"}

    def fake_avault(offline=False):
        seen["avault"] = offline
        return {"ok": True, "skipped": True, "reason": "offline"}

    def fake_tmux(offline=False, force=False):
        seen["tmux"] = {"offline": offline, "force": force}
        return {"ok": True, "skipped": True, "reason": "offline"}

    def fake_git(offline=None, force=False):
        seen["git"] = {"offline": offline, "force": force}
        return {"ok": True, "installed": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(cli, "_ensure_askill_during_prepare", fake_askill)
    monkeypatch.setattr(cli, "_ensure_avault_during_prepare", fake_avault)
    monkeypatch.setattr(cli, "_ensure_tmux_during_prepare", fake_tmux)
    monkeypatch.setattr(cli, "_ensure_git_during_prepare", fake_git)

    assert cli.cmd_runtime(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen == {
        "askill": True,
        "avault": True,
        "tmux": {"offline": True, "force": False},
        "git": {"offline": True, "force": False},
    }
    assert payload["avault"] == {"ok": True, "skipped": True, "reason": "offline"}
    assert payload["tmux"] == {"ok": True, "skipped": True, "reason": "offline"}
    assert payload["git"] == {"ok": True, "installed": True}


def test_runtime_prepare_cli_prints_status_skipped_tmux_as_skipped(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    _stub_runtime_prepare_dependencies(
        monkeypatch,
        tmux_result={"ok": True, "status": "skipped", "reason": "terminal_disabled"},
    )

    assert cli.cmd_runtime(args) == 0
    captured = capsys.readouterr()
    assert "tmux: skipped (terminal_disabled)." in captured.out
    assert "tmux ready." not in captured.out


def test_runtime_prepare_tmux_respects_terminal_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")
    monkeypatch.delenv("VIBE_INSTALL_SKIP_TMUX", raising=False)
    monkeypatch.setattr("core.tmux_runtime.ensure_tmux_installed", lambda force=False: pytest.fail("tmux install should be skipped"))

    assert cli._ensure_tmux_during_prepare() == {"ok": True, "status": "skipped", "reason": "terminal_disabled"}


def test_runtime_prepare_tmux_runs_when_terminal_enabled(monkeypatch):
    calls = []
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.delenv("VIBE_INSTALL_SKIP_TMUX", raising=False)
    monkeypatch.setattr("core.tmux_runtime.ensure_tmux_installed", lambda force=False: calls.append(force) or {"ok": True})

    assert cli._ensure_tmux_during_prepare(force=True) == {"ok": True}
    assert calls == [True]


def _save_config() -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.public_url = "https://alex.avibe.bot"
    config.save()
    return config


def test_store_defaults_to_private_and_rotates_public_share(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        page = store.ensure("ses123")
        assert page.visibility == "private"
        assert page.share_id is None

        public_page = store.update_visibility("ses123", "public")
        assert public_page.visibility == "public"
        assert public_page.share_id

        rotated, old_share_id = store.rotate_share("ses123")
        assert old_share_id == public_page.share_id
        assert rotated.share_id != old_share_id
        assert store.get_by_share_id(old_share_id) is None
        assert store.get_by_share_id(rotated.share_id).session_id == "ses123"

        private_page = store.update_visibility("ses123", "private")
        assert private_page.visibility == "private"
        assert private_page.share_id == rotated.share_id

        offline_page = store.update_visibility("ses123", "offline")
        assert offline_page.offline
        assert offline_page.offline_at is not None
    finally:
        store.close()


def test_rotate_share_requires_public(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    store = ShowPageStore()
    try:
        store.ensure("ses123")
        try:
            store.rotate_share("ses123")
        except ShowPageError as exc:
            assert exc.code == "not_public"
        else:
            raise AssertionError("rotate_share should fail while private")
    finally:
        store.close()


def _expect_show_page_error(fn, code):
    try:
        fn()
    except ShowPageError as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
    else:
        raise AssertionError(f"expected ShowPageError({code})")


def test_set_share_id_sets_custom_public_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.ensure("ses123")
        public_page = store.update_visibility("ses123", "public")
        random_share_id = public_page.share_id
        assert random_share_id

        updated, previous = store.set_share_id("ses123", "q3-roadmap")
        assert updated.share_id == "q3-roadmap"
        assert previous == random_share_id
        # The custom suffix resolves; the auto-generated one is revoked.
        assert store.get_by_share_id("q3-roadmap").session_id == "ses123"
        assert store.get_by_share_id(random_share_id) is None
        # public_url reflects the custom suffix.
        assert show_page_payload(public_page)  # original payload still builds
        assert show_page_payload(updated)["public_url"].endswith("/p/q3-roadmap/")

        # A custom suffix survives a private/public round-trip (not regenerated).
        store.update_visibility("ses123", "private")
        back = store.update_visibility("ses123", "public")
        assert back.share_id == "q3-roadmap"
    finally:
        store.close()


def test_set_share_id_requires_public(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    store = ShowPageStore()
    try:
        store.ensure("ses123")  # defaults to private
        _expect_show_page_error(lambda: store.set_share_id("ses123", "my-demo"), "not_public")
    finally:
        store.close()


def test_set_share_id_rejects_taken_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.update_visibility("ses-a", "public")
        store.update_visibility("ses-b", "public")
        store.set_share_id("ses-a", "shared-demo")
        _expect_show_page_error(lambda: store.set_share_id("ses-b", "shared-demo"), "share_id_taken")
        # The original owner keeps the suffix.
        assert store.get_by_share_id("shared-demo").session_id == "ses-a"
    finally:
        store.close()


def test_set_share_id_rejects_invalid_format(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "public")
        for bad in ["no", "-bad", "bad-", "a b", "a/b", "汉字"]:
            _expect_show_page_error(lambda b=bad: store.set_share_id("ses123", b), "invalid_share_id")
        _expect_show_page_error(lambda: store.set_share_id("ses123", "   "), "missing_share_id")
    finally:
        store.close()


def test_set_share_id_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "public")
        first, _ = store.set_share_id("ses123", "stable-link")
        again, previous = store.set_share_id("ses123", "stable-link")
        assert again.share_id == "stable-link"
        assert previous == "stable-link"
        # Re-saving the same value is a no-op, not a self-collision rewrite.
        assert again.updated_at == first.updated_at
    finally:
        store.close()


def test_set_share_id_rejects_archived_session(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope
    from storage import messages_service

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    store = ShowPageStore()
    try:
        # Make it public while no session row exists (so not archived yet).
        store.update_visibility("ses-arch", "public")
        now = messages_service._utc_now_iso()
        engine = create_sqlite_engine()
        with engine.begin() as conn:
            scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_arch", now=now)
            conn.execute(
                agent_sessions.insert().values(
                    id="ses-arch",
                    scope_id=scope_id,
                    agent_backend="codex",
                    agent_variant="default",
                    session_anchor="anchor_arch",
                    native_session_id="",
                    status="archived",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
        _expect_show_page_error(lambda: store.set_share_id("ses-arch", "later-name"), "session_archived")
    finally:
        store.close()


def test_show_update_set_share_id_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    # Keep the CLI hermetic: skip the best-effort session prewarm side effect.
    monkeypatch.setattr(cli, "_prewarm_show_page_session_best_effort", lambda *a, **k: None)

    store = ShowPageStore()
    try:
        store.update_visibility("ses123", "public")
        store.update_visibility("ses-other", "public")
    finally:
        store.close()

    parser = cli.build_parser()
    ok_args = parser.parse_args(["show", "update", "--session-id", "ses123", "--share-id", "demo-link", "--json"])
    assert cli.cmd_show_update(ok_args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["share_id"] == "demo-link"
    assert payload["public_url"].endswith("/p/demo-link/")

    # A taken suffix on another public page exits 1 with a machine-readable code.
    taken_args = parser.parse_args(["show", "update", "--session-id", "ses-other", "--share-id", "demo-link", "--json"])
    assert cli.cmd_show_update(taken_args) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "share_id_taken"


def test_show_update_share_id_archived_creates_no_page(monkeypatch, tmp_path, capsys):
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope
    from storage import messages_service

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()
    monkeypatch.setattr(cli, "_prewarm_show_page_session_best_effort", lambda *a, **k: None)

    now = messages_service._utc_now_iso()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_arch_cli", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses-arch-cli",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_arch_cli",
                native_session_id="",
                status="archived",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    args = cli.build_parser().parse_args(
        ["show", "update", "--session-id", "ses-arch-cli", "--share-id", "demo", "--json"]
    )
    assert cli.cmd_show_update(args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "session_archived"

    # The failed command must NOT have materialized a Show Page row for the
    # archived session (the CLI no longer pre-ensures before the store guard).
    store = ShowPageStore()
    try:
        assert store.get("ses-arch-cli") is None
    finally:
        store.close()


def test_store_lists_pages_by_updated_time_and_visibility(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.ensure("ses-old")
        store.ensure("ses-public")
        store.update_visibility("ses-public", "public")
        store.ensure("ses-offline")
        store.update_visibility("ses-offline", "offline")

        pages = store.list()
        assert [page.session_id for page in pages] == ["ses-offline", "ses-public", "ses-old"]

        public_pages = store.list(visibility="public")
        assert [page.session_id for page in public_pages] == ["ses-public"]
    finally:
        store.close()


def test_store_lists_show_pages_with_page_and_query(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        for index in range(25):
            store.ensure(f"ses-{index:02d}")

        first_page = store.list_page(page_request=PageRequest(page=1, limit=20))
        second_page = store.list_page(page_request=PageRequest(page=2, limit=20))
        filtered = store.list_page(session_id="ses-2", query="ses-24", page_request=PageRequest(page=1, limit=20))

        assert first_page.has_more is True
        assert len(first_page.items) == 20
        assert second_page.has_more is False
        assert len(second_page.items) == 5
        assert [page.session_id for page in filtered.items] == ["ses-24"]
    finally:
        store.close()


def test_store_escapes_show_page_session_id_prefix_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.ensure("foo_bar")
        store.ensure("fooxbar")
        store.ensure("fooybar")

        underscore_pages = store.list_page(session_id="foo_", page_request=PageRequest(page=1, limit=20))
        query_pages = store.list_page(query="foo_", page_request=PageRequest(page=1, limit=20))

        assert [page.session_id for page in underscore_pages.items] == ["foo_bar"]
        assert [page.session_id for page in query_pages.items] == ["foo_bar"]
    finally:
        store.close()


def test_show_page_dir_creates_default_index(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = ensure_show_page_dir("ses123")

    index_path = page_dir / "index.html"
    assert page_dir == tmp_path / "show" / "ses123"
    assert index_path.exists()
    index_html = index_path.read_text(encoding="utf-8")
    assert 'src="./src/main.tsx"' in index_html
    assert '<div id="root"></div>' in index_html
    assert "Ready to visualize" not in index_html
    assert "Loading Show Page" not in index_html
    assert "fallback-shell" not in index_html
    # PWA "Add to Home Screen": declared standalone-capable.
    assert 'name="apple-mobile-web-app-capable" content="yes"' in index_html
    # Ship NO icon or app-title here, so a page's own apple-touch-icon /
    # apple-mobile-web-app-title is never shadowed (iOS picks the FIRST
    # apple-touch-icon in source order). The default icon comes from the Avibe
    # origin root via iOS's root-directory fallback, not a competing link.
    assert 'rel="apple-touch-icon"' not in index_html
    assert 'name="apple-mobile-web-app-title"' not in index_html
    # Must NOT link the workbench manifest — its start_url "/" would hijack the
    # installed Home Screen icon back to the workbench instead of this page.
    assert 'rel="manifest"' not in index_html
    # §7.1j: the App-icon self-serve guidance lives in the artifact the agent edits.
    # It must tell the author to give the app its Dock / App Library icon via a static
    # FILE (favicon at root or a relative <link rel=icon>), NOT a JS-injected one —
    # closing the JS-injection blind spot at the source. This <head> comment holds a
    # `<link rel="icon">` EXAMPLE, but it is inside an HTML comment, so the scaffold
    # still resolves to no icon (see test_extract_icon_path_stock_scaffold_index_is_null).
    assert "App icon (Avibe Dock / App Library)" in index_html
    assert "Do NOT inject the icon from JavaScript" in index_html
    main_tsx = (page_dir / "src" / "main.tsx").read_text(encoding="utf-8")
    assert "globalThis.__AVIBE_SHOW__" in main_tsx
    assert "declare global" in main_tsx
    assert "const injected: VibeShowRuntimeConfig = globalThis.__AVIBE_SHOW__ ?? {}" in main_tsx
    assert "globalThis.__AVIBE_SHOW__ = {" in main_tsx
    assert main_tsx.index("const injected: VibeShowRuntimeConfig") < main_tsx.index("globalThis.__AVIBE_SHOW__ = {")
    assert "sessionId: injected.sessionId ??" in main_tsx
    assert "basePath: injected.basePath ??" in main_tsx
    assert 'eventsPath: injected.eventsPath ?? "__show/events"' in main_tsx
    assert 'streamPath: injected.streamPath ?? "__show/events?stream=1"' in main_tsx
    assert 'writeToken: injected.writeToken ?? readCookie("vibe_show_event_token")' in main_tsx
    # The runtime-owned shell just renders <App/>; routing is NOT moved into
    # main.tsx/index.html, so adding a page never requires touching the shell.
    assert 'import App from "./App"' in main_tsx
    assert "router" not in main_tsx.lower()
    app_tsx = (page_dir / "src" / "App.tsx").read_text(encoding="utf-8")
    # The default App is now a multi-page router host, not the old single-page
    # placeholder.
    assert "Building your Show Page" not in app_tsx
    assert 'from "./router"' in app_tsx
    assert "<RouterView" in app_tsx
    assert (page_dir / "api" / "health.ts").exists()


def test_extract_icon_path_custom_relative_href(tmp_path):
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><link rel="icon" href="./favicon.svg"></head></html>',
        encoding="utf-8",
    )
    # A leading "./" is normalized away; the path stays relative to /show/<sid>/.
    assert _extract_icon_path(tmp_path) == "favicon.svg"


def test_extract_icon_path_relative_subdir_href(tmp_path):
    (tmp_path / "index.html").write_text('<link rel="icon" href="assets/logo.png">', encoding="utf-8")
    assert _extract_icon_path(tmp_path) == "assets/logo.png"


def test_extract_icon_path_shortcut_icon_rel(tmp_path):
    # rel="shortcut icon" still carries the "icon" token, so it matches.
    (tmp_path / "index.html").write_text('<link rel="shortcut icon" href="fav.ico">', encoding="utf-8")
    assert _extract_icon_path(tmp_path) == "fav.ico"


def test_extract_icon_path_first_icon_wins(tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="icon" href="first.svg"><link rel="icon" href="second.svg">',
        encoding="utf-8",
    )
    assert _extract_icon_path(tmp_path) == "first.svg"


def test_extract_icon_path_missing_index_is_null(tmp_path):
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_no_link_is_null(tmp_path):
    (tmp_path / "index.html").write_text("<html><head><title>x</title></head></html>", encoding="utf-8")
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_apple_touch_icon_ignored(tmp_path):
    # apple-touch-icon is not rel="icon"; prefer the letter avatar over it.
    (tmp_path / "index.html").write_text('<link rel="apple-touch-icon" href="touch.png">', encoding="utf-8")
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_stock_scaffold_index_is_null(tmp_path):
    # The real scaffold ships NO icon link, so an un-customized page yields null
    # and the letter avatar is used. This locks that contract to the scaffold.
    (tmp_path / "index.html").write_text(_default_index_html("ses123"), encoding="utf-8")
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_stock_vite_icons_are_null(tmp_path):
    # The Vite default favicon ships as an ABSOLUTE href, and even a relative copy
    # of the generic mascot is treated as stock — both prefer the letter avatar.
    for href in ("/vite.svg", "./vite.svg", "assets/vite.svg"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_absolute_href_is_null(tmp_path):
    # Absolute (root-relative) hrefs resolve to the workbench origin, not the
    # page workspace — only same-workspace relative paths are allowed.
    (tmp_path / "index.html").write_text('<link rel="icon" href="/favicon.ico">', encoding="utf-8")
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_external_and_scheme_hrefs_are_null(tmp_path):
    for href in (
        "https://cdn.example.com/i.png",
        "//cdn.example.com/i.png",
        "data:image/svg+xml,<svg/>",
    ):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_parent_traversal_is_null(tmp_path):
    for href in ("../secret.svg", "../../a/b.svg", "a/../../b.svg"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_encoded_and_backslash_traversal_is_null(tmp_path):
    # The browser normalizes %2e%2e/, encoded slashes, and backslashes before it
    # resolves the icon URL, so these must be rejected even without a literal "../".
    for href in (
        "%2e%2e/other/icon.svg",
        "..%2fother%2ficon.svg",
        "..\\other\\icon.svg",
        "%2fetc%2fpasswd",
        "sub/%2e%2e/%2e%2e/secret.svg",
    ):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_skips_symlinked_index(tmp_path):
    # An agent could point index.html at a large/special file via a symlink; skip it.
    real = tmp_path / "real.html"
    real.write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (tmp_path / "index.html").symlink_to(real)
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_reads_only_head_of_large_index(tmp_path):
    # A huge inline page must not stall the read; the icon <link> in <head> (top) is
    # still found, and the trailing bulk beyond the head limit is never scanned.
    head = '<head><link rel="icon" href="favicon.svg"></head>'
    (tmp_path / "index.html").write_text(head + "<!-- padding -->" * 20000, encoding="utf-8")
    assert _extract_icon_path(tmp_path) == "favicon.svg"


def test_extract_icon_path_resolves_through_base_href(tmp_path):
    # A <base href="assets/"> BEFORE the icon link makes the browser resolve the
    # icon as assets/favicon.svg; the resolver must match that document semantics.
    (tmp_path / "index.html").write_text(
        '<head><base href="assets/"><link rel="icon" href="favicon.svg"></head>',
        encoding="utf-8",
    )
    assert _extract_icon_path(tmp_path) == "assets/favicon.svg"


def test_extract_icon_path_base_after_icon_does_not_apply(tmp_path):
    # A <base> AFTER the icon link does not affect it (document order).
    (tmp_path / "index.html").write_text(
        '<head><link rel="icon" href="favicon.svg"><base href="assets/"></head>',
        encoding="utf-8",
    )
    assert _extract_icon_path(tmp_path) == "favicon.svg"


def test_extract_icon_path_base_escaping_workspace_is_null(tmp_path):
    for base in ("/other/", "https://cdn.example.com/", "../"):
        (tmp_path / "index.html").write_text(
            f'<head><base href="{base}"><link rel="icon" href="favicon.svg"></head>',
            encoding="utf-8",
        )
        assert _extract_icon_path(tmp_path) is None, base


def test_extract_icon_path_root_relative_hrefs_are_null(tmp_path):
    # Root-relative / protocol-relative hrefs root at the ORIGIN, not the workspace,
    # so they reject — including a literal "/w/…" that would otherwise collide with
    # the synthetic resolution prefix and be mis-served as workspace-relative (Codex).
    for href in ("/favicon.svg", "/w/icon.svg", "//cdn.example.com/icon.svg"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_root_relative_base_is_null(tmp_path):
    # A root-relative <base href="/w/"> likewise escapes the workspace.
    for base in ("/w/", "/", "//cdn.example.com/"):
        (tmp_path / "index.html").write_text(
            f'<head><base href="{base}"><link rel="icon" href="favicon.svg"></head>',
            encoding="utf-8",
        )
        assert _extract_icon_path(tmp_path) is None, base


def test_extract_icon_path_malformed_href_returns_null_not_raises(tmp_path):
    # A malformed absolute URL (urlsplit raises ValueError) must fall back to the
    # letter avatar, NEVER propagate: _extract_icon_path runs while building
    # /api/show-pages, so one bad page must not break the whole inventory (Codex).
    for href in ("http://[bad]/icon.svg", "http://[bad/icon.svg"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href
    # A malformed <base> is equally contained.
    (tmp_path / "index.html").write_text(
        '<head><base href="http://[bad]/"><link rel="icon" href="favicon.svg"></head>',
        encoding="utf-8",
    )
    assert _extract_icon_path(tmp_path) is None


def test_extract_icon_path_hidden_dot_segments_are_null(tmp_path):
    # Hidden / dot segments are denied for icons exactly as the Show Page static
    # server denies them (`_is_show_page_dot_path`); the icon endpoint must not
    # become a bypass for that policy — even for an image-extension dot-file (Codex).
    for href in (".env.svg", "assets/.secret.png", ".git/logo.png", ".favicon.svg", "a/.b/c.png"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_runtime_api_hrefs_are_null(tmp_path):
    for href in ("api/health", "api/health.svg", "__show/events.png", "__events"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_non_image_extensions_are_null(tmp_path):
    for href in ("icon.txt", "icon.js", "icon.html", "favicon", "noext/"):
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) is None, href


def test_extract_icon_path_accepts_whitelisted_image_extensions(tmp_path):
    cases = {
        "icon.png": "icon.png",
        "icon.jpg": "icon.jpg",
        "icon.jpeg": "icon.jpeg",
        "icon.webp": "icon.webp",
        "icon.gif": "icon.gif",
        "icon.ico": "icon.ico",
        "logo.SVG": "logo.SVG",  # extension is case-insensitive; the path keeps its case
    }
    for href, expected in cases.items():
        (tmp_path / "index.html").write_text(f'<link rel="icon" href="{href}">', encoding="utf-8")
        assert _extract_icon_path(tmp_path) == expected, href


def _icon_page(session_id: str) -> ShowPage:
    return ShowPage(
        session_id=session_id,
        visibility="private",
        share_id=None,
        offline_at=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_show_page_payload_icon_version_is_a_token_when_icon_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = ensure_show_page_dir("sesicon")
    # A page that customized its index.html with its own relative favicon.
    (page_dir / "index.html").write_text('<link rel="icon" href="./brand.svg">', encoding="utf-8")
    (page_dir / "brand.svg").write_text("<svg>v1</svg>", encoding="utf-8")

    version = show_page_payload(_icon_page("sesicon"))["icon_version"]

    # An opaque, non-empty token — NOT the path (the frontend never composes a path).
    assert isinstance(version, str) and version
    assert "brand.svg" not in version


def test_show_page_payload_icon_version_null_for_default_scaffold(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_show_page_dir("sesdefault")  # writes the stock scaffold index.html (no icon)
    assert show_page_payload(_icon_page("sesdefault"))["icon_version"] is None


def test_show_page_payload_icon_version_follows_the_file(monkeypatch, tmp_path):
    # Freshness invariant (§7.1f versioned-URL): the token follows the resolved icon
    # FILE, so ANY change to the icon content changes the next payload's token with no
    # client update-site enumeration. Overwrite → new token; repoint <link> → new token.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = ensure_show_page_dir("sesfresh")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg>v1</svg>", encoding="utf-8")
    v1 = show_page_payload(_icon_page("sesfresh"))["icon_version"]
    assert v1

    # Overwrite the same file with different bytes → the token changes.
    (page_dir / "favicon.svg").write_text("<svg>v2-longer</svg>", encoding="utf-8")
    v2 = show_page_payload(_icon_page("sesfresh"))["icon_version"]
    assert v2 and v2 != v1

    # Repoint <link rel=icon> to a different file → the token changes again.
    (page_dir / "logo.png").write_bytes(b"\x89PNG\r\n")
    (page_dir / "index.html").write_text('<link rel="icon" href="logo.png">', encoding="utf-8")
    v3 = show_page_payload(_icon_page("sesfresh"))["icon_version"]
    assert v3 and v3 != v2


def test_show_page_payload_icon_version_tracks_content_not_just_mtime(monkeypatch, tmp_path):
    # The token hashes CONTENT, so a regeneration that preserves size AND mtime
    # (`cp -p`/`rsync`, deterministic build artifacts) STILL changes it — an
    # mtime+size identity would collide and, under immutable caching, keep serving
    # the stale icon (Codex). Identical bytes, in contrast, keep the token stable.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = ensure_show_page_dir("sesctnt")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    favicon = page_dir / "favicon.svg"
    favicon.write_text("<svg>AAAA</svg>", encoding="utf-8")
    stat = favicon.stat()
    v1 = show_page_payload(_icon_page("sesctnt"))["icon_version"]

    # Same byte length + restored mtime — ONLY the content differs.
    favicon.write_text("<svg>BBBB</svg>", encoding="utf-8")
    os.utime(favicon, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert favicon.stat().st_size == stat.st_size
    assert favicon.stat().st_mtime_ns == stat.st_mtime_ns
    v2 = show_page_payload(_icon_page("sesctnt"))["icon_version"]
    assert v1 and v2 and v1 != v2  # content change is caught despite identical mtime+size

    # Restoring the exact bytes restores the token (identical icon → cache hit).
    favicon.write_text("<svg>AAAA</svg>", encoding="utf-8")
    assert show_page_payload(_icon_page("sesctnt"))["icon_version"] == v1


def test_resolve_show_page_icon_rejects_oversized_icon(monkeypatch, tmp_path):
    # A page pointing <link rel=icon> at a large in-workspace asset must NOT make
    # /api/show-pages read it in full per row (or the endpoint materialize it): an
    # oversized icon is dropped to the letter avatar (None), not hashed/served (Codex).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_pages._ICON_MAX_BYTES", 16)
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("sesbig")
    (page_dir / "index.html").write_text('<link rel="icon" href="big.png">', encoding="utf-8")
    (page_dir / "big.png").write_bytes(b"x" * 64)  # over the (patched) 16-byte cap

    assert resolve_show_page_icon("sesbig") is None
    assert show_page_payload(_icon_page("sesbig"))["icon_version"] is None
    # A file at/under the cap is still accepted.
    (page_dir / "big.png").write_bytes(b"y" * 16)
    assert resolve_show_page_icon("sesbig") is not None


_CONVENTIONAL_ICONS = (
    ("favicon.svg", "image/svg+xml"),
    ("favicon.ico", "image/x-icon"),
    ("favicon.png", "image/png"),
    ("public/favicon.svg", "image/svg+xml"),
    ("public/favicon.ico", "image/x-icon"),
    ("public/favicon.png", "image/png"),
)


def test_resolve_icon_falls_back_to_each_conventional_file(monkeypatch, tmp_path):
    # When index.html declares NO <link rel=icon>, each workspace-conventional favicon
    # location resolves + serves identically (§7.1h item 4a) — most agent-built pages
    # ship no link, so this is what gives them an icon.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_icon_version

    page_dir = ensure_show_page_dir("sesconv")
    (page_dir / "index.html").write_text("<title>no icon link</title>", encoding="utf-8")
    for rel, ctype in _CONVENTIONAL_ICONS:
        for existing, _ in _CONVENTIONAL_ICONS:  # isolate: only `rel` present this pass
            existing_path = page_dir / existing
            if existing_path.exists():
                existing_path.unlink()
        target = page_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"<svg/>" if rel.endswith(".svg") else b"\x00icon")

        resolved = resolve_show_page_icon("sesconv")
        assert resolved is not None, rel
        assert resolved[0] == (page_dir / rel).resolve(), rel
        assert resolved[1] == ctype, rel
        # icon_version covers conventional icons identically (non-null token).
        assert show_page_icon_version("sesconv"), rel


def test_resolve_icon_conventional_order_root_before_public(monkeypatch, tmp_path):
    # Precedence within the conventions: root wins over public/, and svg > ico > png.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("sesorder")
    (page_dir / "index.html").write_text("<title>x</title>", encoding="utf-8")
    (page_dir / "public").mkdir()
    (page_dir / "public" / "favicon.svg").write_bytes(b"<svg/>")
    (page_dir / "favicon.png").write_bytes(b"png")
    (page_dir / "favicon.ico").write_bytes(b"ico")
    (page_dir / "favicon.svg").write_bytes(b"<svg/>")

    resolved = resolve_show_page_icon("sesorder")
    assert resolved is not None
    assert resolved[0] == (page_dir / "favicon.svg").resolve()  # root svg wins


def test_resolve_icon_link_tag_wins_over_conventional(monkeypatch, tmp_path):
    # An explicit, valid <link rel=icon> beats the conventional favicon files (§7.1h).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("seslink")
    (page_dir / "index.html").write_text('<link rel="icon" href="brand.svg">', encoding="utf-8")
    (page_dir / "brand.svg").write_bytes(b"<svg>brand</svg>")
    (page_dir / "favicon.svg").write_bytes(b"<svg>fallback</svg>")  # NOT chosen

    resolved = resolve_show_page_icon("seslink")
    assert resolved is not None
    assert resolved[0] == (page_dir / "brand.svg").resolve()


def test_resolve_icon_falls_back_when_link_is_unusable(monkeypatch, tmp_path):
    # An explicit <link rel=icon> pointing at a MISSING file must not strand the page
    # icon-less — fall through to a conventional favicon (§7.1h Codex). A usable link
    # still wins (covered above); this covers the unusable-link case.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("sesbroken")
    (page_dir / "index.html").write_text('<link rel="icon" href="missing.svg">', encoding="utf-8")
    (page_dir / "favicon.png").write_bytes(b"png")  # the conventional fallback

    resolved = resolve_show_page_icon("sesbroken")
    assert resolved is not None
    assert resolved[0] == (page_dir / "favicon.png").resolve()


def test_resolve_icon_unusable_link_and_no_conventional_is_none(monkeypatch, tmp_path):
    # Broken explicit link AND no conventional favicon on disk → None (letter avatar)
    # — the fall-through must not invent an icon (§7.1h Codex, adjudicated case 3).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("sesbroken2")
    (page_dir / "index.html").write_text('<link rel="icon" href="missing.svg">', encoding="utf-8")
    assert resolve_show_page_icon("sesbroken2") is None


def test_resolve_icon_none_without_link_or_conventional(monkeypatch, tmp_path):
    # No link and no conventional favicon → None (letter avatar).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon

    page_dir = ensure_show_page_dir("sesnone")
    (page_dir / "index.html").write_text("<title>nothing</title>", encoding="utf-8")
    assert resolve_show_page_icon("sesnone") is None


def test_read_show_page_icon_enforces_the_token(monkeypatch, tmp_path):
    # ?v= is a content assertion: the correct token yields the bytes; a wrong/empty
    # token is None (→ 404), so `immutable` caching is honest (URL ⇒ one content).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import read_show_page_icon, show_page_icon_version

    page_dir = ensure_show_page_dir("sesread")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg>hi</svg>", encoding="utf-8")
    token = show_page_icon_version("sesread")

    assert read_show_page_icon("sesread", token) == (b"<svg>hi</svg>", "image/svg+xml")
    assert read_show_page_icon("sesread", "deadbeefdeadbeef") is None  # wrong token
    assert read_show_page_icon("sesread", "") is None  # no token


def test_read_show_page_icon_rejects_symlink_swap(monkeypatch, tmp_path):
    # TOCTOU: a regular file accepted by resolve is replaced by a symlink to a file
    # OUTSIDE the workspace before the read. The result must be None (never the
    # swapped-in target) — caught by the re-resolution's within-root guard and, for
    # a swap in the resolve→open window, by the O_NOFOLLOW open. Even the (stale)
    # correct token must not serve it.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import read_show_page_icon, show_page_icon_version

    page_dir = ensure_show_page_dir("sesswap")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.svg">', encoding="utf-8")
    (page_dir / "favicon.svg").write_text("<svg>ok</svg>", encoding="utf-8")
    token = show_page_icon_version("sesswap")
    outside = tmp_path / "outside_secret.svg"
    outside.write_text("<svg>SECRET</svg>", encoding="utf-8")
    (page_dir / "favicon.svg").unlink()
    os.symlink(outside, page_dir / "favicon.svg")

    assert read_show_page_icon("sesswap", token) is None


def test_read_show_page_icon_rejects_oversized_at_read_time(monkeypatch, tmp_path):
    # TOCTOU: the file grows past the cap AFTER resolve's stat accepted it (a swap
    # race). The descriptor `fstat` re-checks the cap, so the huge file is rejected
    # on the fd → None, never buffered. Patch resolve to hand back the over-cap file
    # (simulating "resolve saw it small") so the read-time gate is exercised alone.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_pages._ICON_MAX_BYTES", 16)
    from core.show_pages import read_show_page_icon

    page_dir = ensure_show_page_dir("sesgrow")
    big = page_dir / "big.png"
    big.write_bytes(b"z" * 64)  # over the patched 16-byte cap
    monkeypatch.setattr("core.show_pages.resolve_show_page_icon", lambda sid: (big, "image/png"))

    assert read_show_page_icon("sesgrow", "anytoken") is None


def test_read_workspace_file_safely_regular_head_and_capped(tmp_path):
    # The shared chokepoint: a regular file reads (head up to `limit`; or full within
    # `cap`), an over-cap file is refused, a missing file is None.
    from core.show_pages import _read_workspace_file_safely

    f = tmp_path / "f.txt"
    f.write_bytes(b"0123456789")
    assert _read_workspace_file_safely(f, 4) == b"0123"  # cap=False → head up to limit
    assert _read_workspace_file_safely(f, 100) == b"0123456789"
    assert _read_workspace_file_safely(f, 100, cap=True) == b"0123456789"  # full within cap
    assert _read_workspace_file_safely(f, 4, cap=True) is None  # over cap → refused
    assert _read_workspace_file_safely(tmp_path / "nope.txt", 100) is None  # missing


def test_read_workspace_file_safely_refuses_symlink(tmp_path):
    # O_NOFOLLOW refuses a symlink swapped in for a workspace file. Skip where the
    # flag is absent (native Windows) so windows-smoke stays green.
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW not available on this platform")
    from core.show_pages import _read_workspace_file_safely

    (tmp_path / "outside.txt").write_bytes(b"SECRET")
    link = tmp_path / "link.txt"
    os.symlink(tmp_path / "outside.txt", link)
    assert _read_workspace_file_safely(link, 100) is None
    assert _read_workspace_file_safely(link, 100, cap=True) is None


def test_read_workspace_file_safely_refuses_non_regular_fifo(tmp_path):
    # A FIFO (special file) swapped in must be refused by the descriptor fstat, so a
    # read can never block the inventory. Skip where mkfifo is unavailable (Windows).
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo not available on this platform")
    from core.show_pages import _read_workspace_file_safely

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert _read_workspace_file_safely(fifo, 100) is None
    assert _read_workspace_file_safely(fifo, 100, cap=True) is None


# --- Icon self-serve upload (§7.1j) -------------------------------------------------

# (upload filename, content-type) -> the canonical on-disk extension the server writes.
_ICON_UPLOAD_CASES = (
    ("icon.svg", "image/svg+xml", "svg"),
    ("icon.png", "image/png", "png"),
    ("icon.ico", "image/x-icon", "ico"),
    ("icon.jpg", "image/jpeg", "jpg"),
    ("icon.jpeg", "image/jpeg", "jpg"),  # jpeg folds to the single canonical jpg
    ("icon.webp", "image/webp", "webp"),
)


@pytest.mark.parametrize("filename, content_type, canonical", _ICON_UPLOAD_CASES)
def test_write_show_page_icon_happy_path_per_extension(monkeypatch, tmp_path, filename, content_type, canonical):
    # Each whitelisted type writes the SERVER-chosen workspace-root favicon.<ext> (the
    # client never supplies a path) and becomes the resolved, servable icon (§7.1j).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    version = write_show_page_icon("sesup", b"<bytes>", filename=filename, content_type=content_type)

    page_dir = show_page_dir("sesup")
    target = page_dir / f"favicon.{canonical}"
    assert target.is_file() and target.read_bytes() == b"<bytes>"
    resolved = resolve_show_page_icon("sesup")
    assert resolved is not None and resolved[0] == target.resolve()
    assert isinstance(version, str) and version  # a fresh non-empty token


def test_write_show_page_icon_derives_ext_from_filename_when_type_generic(monkeypatch, tmp_path):
    # Browsers sometimes send a generic/blank content-type for .ico/.svg; the filename
    # extension is the fallback signal, still whitelisted.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import show_page_dir, write_show_page_icon

    write_show_page_icon("sesgen", b"i", filename="brand.ico", content_type="application/octet-stream")
    assert (show_page_dir("sesgen") / "favicon.ico").is_file()


def test_write_show_page_icon_rejects_non_whitelisted_type(monkeypatch, tmp_path):
    # A non-image type (or extension) is refused with a clear code and writes NOTHING.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import show_page_dir, write_show_page_icon

    with pytest.raises(ShowPageError) as excinfo:
        write_show_page_icon("sesbad", b"<html>", filename="evil.html", content_type="text/html")
    assert excinfo.value.code == "invalid_icon_type"
    # A content-type that disagrees with a whitelisted extension is also refused.
    with pytest.raises(ShowPageError):
        write_show_page_icon("sesbad", b"x", filename="icon.svg", content_type="image/png")
    # A whitelisted extension can't smuggle an explicit non-image content-type through.
    with pytest.raises(ShowPageError):
        write_show_page_icon("sesbad", b"x", filename="logo.svg", content_type="text/html")
    assert not any((show_page_dir("sesbad")).glob("favicon.*"))


def test_write_show_page_icon_rejects_empty_and_oversized(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.show_pages._ICON_MAX_BYTES", 8)
    monkeypatch.setattr("core.show_pages.SHOW_PAGE_ICON_MAX_UPLOAD_BYTES", 8)
    from core.show_pages import write_show_page_icon

    with pytest.raises(ShowPageError) as empty:
        write_show_page_icon("sescap", b"", filename="i.svg", content_type="image/svg+xml")
    assert empty.value.code == "icon_required"
    with pytest.raises(ShowPageError) as big:
        write_show_page_icon("sescap", b"x" * 9, filename="i.svg", content_type="image/svg+xml")
    assert big.value.code == "icon_too_large"


def test_write_show_page_icon_removes_sibling_root_favicons(monkeypatch, tmp_path):
    # Exactly ONE conventional source must remain, so a stale root favicon.svg can't
    # shadow the freshly-uploaded favicon.ico in the resolver's svg>ico>png order.
    # Sibling root variants go; a public/ copy (tried only after root) is left alone.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    page_dir = show_page_dir("sessib")
    (page_dir / "public").mkdir(parents=True)
    (page_dir / "favicon.svg").write_text("<svg>old</svg>", encoding="utf-8")
    (page_dir / "favicon.png").write_bytes(b"oldpng")
    (page_dir / "public" / "favicon.svg").write_text("<svg>pub</svg>", encoding="utf-8")

    write_show_page_icon("sessib", b"NEWICO", filename="new.ico", content_type="image/x-icon")

    assert not (page_dir / "favicon.svg").exists()
    assert not (page_dir / "favicon.png").exists()
    assert (page_dir / "favicon.ico").read_bytes() == b"NEWICO"
    assert (page_dir / "public" / "favicon.svg").exists()  # a non-sibling copy is untouched
    resolved = resolve_show_page_icon("sessib")
    assert resolved is not None and resolved[0] == (page_dir / "favicon.ico").resolve()


def test_write_show_page_icon_preserves_explicitly_linked_root_favicon(monkeypatch, tmp_path):
    # The sibling cleanup must NOT delete a root favicon the page explicitly links from
    # index.html when a different extension is uploaded — the explicit link keeps winning
    # (we never edit index.html), so deleting it would 404 the page's own favicon (P2).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    page_dir = ensure_show_page_dir("seskeep")
    (page_dir / "index.html").write_text('<link rel="icon" href="favicon.png">', encoding="utf-8")
    (page_dir / "favicon.png").write_bytes(b"LINKED-PNG")

    write_show_page_icon("seskeep", b"<svg>UP</svg>", filename="i.svg", content_type="image/svg+xml")

    # The explicitly-linked favicon.png survives; the uploaded favicon.svg is written but
    # dormant, and the explicit link still resolves.
    assert (page_dir / "favicon.png").read_bytes() == b"LINKED-PNG"
    assert (page_dir / "favicon.svg").read_bytes() == b"<svg>UP</svg>"
    resolved = resolve_show_page_icon("seskeep")
    assert resolved is not None and resolved[0] == (page_dir / "favicon.png").resolve()


def test_write_show_page_icon_does_not_follow_symlink_at_target(monkeypatch, tmp_path):
    # A symlink placed at the favicon name must NOT be written through: the write
    # unlinks it first and lands a fresh regular file, so an outside target is
    # untouched and the served icon is the uploaded bytes (defense-in-depth, §7.1j).
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW / symlink semantics not available on this platform")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    page_dir = show_page_dir("seslnk")
    page_dir.mkdir(parents=True)
    outside = tmp_path / "outside.svg"
    outside.write_text("<svg>SECRET-OUTSIDE</svg>", encoding="utf-8")
    os.symlink(outside, page_dir / "favicon.svg")

    write_show_page_icon("seslnk", b"<svg>MINE</svg>", filename="i.svg", content_type="image/svg+xml")

    favicon = page_dir / "favicon.svg"
    assert not favicon.is_symlink()  # the symlink entry was replaced, not followed
    assert favicon.read_bytes() == b"<svg>MINE</svg>"
    assert outside.read_text(encoding="utf-8") == "<svg>SECRET-OUTSIDE</svg>"  # never written through
    resolved = resolve_show_page_icon("seslnk")
    assert resolved is not None and resolved[0] == favicon.resolve()


def test_write_show_page_icon_preserves_old_favicon_on_write_failure(monkeypatch, tmp_path):
    # A failure while the replacement lands (e.g. disk full during os.replace) must NOT
    # destroy the user's existing icon — old favicons stay until the new one succeeds, and
    # the other-ext cleanup only runs afterwards, so a prior favicon.png survives (§7.1j P2).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    page_dir = show_page_dir("sesfail")
    page_dir.mkdir(parents=True)
    (page_dir / "favicon.png").write_bytes(b"OLD-PNG")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.show_pages.os.replace", _boom)

    with pytest.raises(OSError):
        write_show_page_icon("sesfail", b"<svg>NEW</svg>", filename="i.svg", content_type="image/svg+xml")

    # The old icon (a different extension) survives and still resolves; no temp is orphaned.
    assert (page_dir / "favicon.png").read_bytes() == b"OLD-PNG"
    assert not (page_dir / "favicon.svg").exists()
    assert not list(page_dir.glob(".favicon.*.tmp"))
    resolved = resolve_show_page_icon("sesfail")
    assert resolved is not None and resolved[0] == (page_dir / "favicon.png").resolve()


def test_write_show_page_icon_version_changes_on_replace(monkeypatch, tmp_path):
    # Re-uploading different bytes changes icon_version (the content-versioned URL busts
    # the cache); identical bytes keep it stable — the freshness invariant on the write.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import write_show_page_icon

    v1 = write_show_page_icon("sesver", b"<svg>1</svg>", filename="i.svg", content_type="image/svg+xml")
    v2 = write_show_page_icon("sesver", b"<svg>2-longer</svg>", filename="i.svg", content_type="image/svg+xml")
    assert v1 and v2 and v1 != v2
    v3 = write_show_page_icon("sesver", b"<svg>2-longer</svg>", filename="i.svg", content_type="image/svg+xml")
    assert v3 == v2  # identical bytes → identical token (cache hit)


def test_write_show_page_icon_explicit_link_still_wins(monkeypatch, tmp_path):
    # Ledger (§7.1j): we never edit index.html, so a USABLE explicit <link rel=icon>
    # keeps winning in resolve order over the uploaded conventional favicon — the
    # editor only covers the common no-link case.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core.show_pages import resolve_show_page_icon, show_page_dir, write_show_page_icon

    page_dir = ensure_show_page_dir("sesuplink")
    (page_dir / "index.html").write_text('<link rel="icon" href="brand.svg">', encoding="utf-8")
    (page_dir / "brand.svg").write_text("<svg>brand</svg>", encoding="utf-8")

    write_show_page_icon("sesuplink", b"<svg>uploaded</svg>", filename="i.svg", content_type="image/svg+xml")

    # The uploaded favicon.svg exists on disk, but the explicit link still resolves.
    assert (page_dir / "favicon.svg").read_bytes() == b"<svg>uploaded</svg>"
    resolved = resolve_show_page_icon("sesuplink")
    assert resolved is not None and resolved[0] == (page_dir / "brand.svg").resolve()


def test_canonical_upload_icon_ext_rules():
    from core.show_pages import _canonical_upload_icon_ext

    # Content-type is authoritative; filename is the fallback; jpeg folds to jpg.
    assert _canonical_upload_icon_ext("x.png", "image/png") == "png"
    assert _canonical_upload_icon_ext("x.JPEG", None) == "jpg"
    assert _canonical_upload_icon_ext(None, "image/webp") == "webp"
    assert _canonical_upload_icon_ext("x.ico", "image/vnd.microsoft.icon") == "ico"
    # Not whitelisted, or a recognizable type/extension that disagree → None (415).
    assert _canonical_upload_icon_ext("x.gif", "image/gif") is None  # gif is servable, not uploadable
    assert _canonical_upload_icon_ext("x.txt", "text/plain") is None
    assert _canonical_upload_icon_ext("x.svg", "image/png") is None  # mismatch
    # An EXPLICIT non-image content-type is rejected even with a whitelisted extension;
    # only a blank / generic (octet-stream) type falls back to the filename (Codex P3).
    assert _canonical_upload_icon_ext("logo.svg", "text/html") is None
    assert _canonical_upload_icon_ext("logo.svg", "application/octet-stream") == "svg"
    assert _canonical_upload_icon_ext("logo.svg", "") == "svg"


def test_fresh_workspace_scaffolds_placeholder_and_minimal_router(monkeypatch, tmp_path):
    # A brand-new Show Page workspace starts as a clean "being generated"
    # placeholder for the user, plus a minimal file-based router and one extra page
    # so the agent can see the multi-page affordance. It is intentionally small and
    # English-only — a starting point, not a content template.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = ensure_show_page_dir("sesmulti")

    router = (page_dir / "src" / "router.tsx").read_text(encoding="utf-8")
    # File-based discovery of pages.
    assert "import.meta.glob" in router
    assert '"./pages/**/*.tsx"' in router
    assert "eager: true" in router
    # Hash-based client routing: nested deep-link + refresh work in both private
    # /show/ and public /p/ serving modes with no server cooperation.
    assert "hashchange" in router
    assert "useSyncExternalStore" in router
    # A concrete path wins over a matching [param] route of the same length.
    assert "routeSpecificity" in router
    assert "export function RouterView" in router
    # A malformed hash param must degrade gracefully, not throw and blank the app.
    assert "safeDecode" in router
    # Pages exported as React exotic components (memo/forwardRef) are accepted.
    assert "isRenderablePage" in router
    assert "$$typeof" in router
    # The locale/nav machinery from the old rich demo is gone (English-only, no nav).
    assert "activeLocale" not in router
    assert "navItems" not in router

    # The home page is the user-facing "building" placeholder: a pulsing dot, and a
    # nudge prompt revealed only after a delay. It hints the built-in UI by rendering
    # Card + Button, leaving Badge as a commented import.
    home = (page_dir / "src" / "pages" / "index.tsx").read_text(encoding="utf-8")
    assert "export default function" in home
    assert "Building your Show Page" in home
    assert "animate-ping" in home  # the pulsing "working" dot
    assert "NUDGE_AFTER_MS = 90_000" in home  # nudge only after ~90s, no nagging on arrival
    assert "@/components/ui/card" in home
    assert "@/components/ui/button" in home  # Button is a live import now
    assert "// import { Badge }" in home  # Badge stays a commented hint

    # One extra page demonstrates "add a file = add a route"; the old items demo is gone.
    second = page_dir / "src" / "pages" / "second.tsx"
    assert second.exists()
    assert "A second page" in second.read_text(encoding="utf-8")
    assert not (page_dir / "src" / "pages" / "items").exists()

    # App is a minimal shell that renders the router — no hardcoded nav/route table.
    app_tsx = (page_dir / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "<RouterView" in app_tsx
    assert "navItems" not in app_tsx
    assert "activeLocale" not in app_tsx

    # The whole starter is English-only (no CJK), even though the agent prompt is
    # localized elsewhere.
    for rel in ("src/pages/index.tsx", "src/pages/second.tsx", "src/router.tsx", "src/App.tsx"):
        text = (page_dir / rel).read_text(encoding="utf-8")
        assert not any("一" <= ch <= "鿿" for ch in text), f"unexpected CJK in {rel}"

    # styles.css keeps the two imports the runtime's Tailwind pipeline requires,
    # with the tailwindcss import first.
    styles = (page_dir / "src" / "styles.css").read_text(encoding="utf-8")
    assert styles.splitlines()[0].strip() == '@import "tailwindcss";'
    assert '@import "@avibe/show-ui/theme.css";' in styles


def test_existing_single_page_workspace_is_preserved(monkeypatch, tmp_path):
    # An existing single-page workspace (its own src/App.tsx) must not be migrated:
    # ensure() leaves App.tsx byte-for-byte and does NOT drop router/pages files
    # next to it. Only genuinely-missing shell files are (re)materialized so the
    # workspace stays runnable.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    page_dir = paths.get_show_page_dir("seslegacy")
    (page_dir / "src").mkdir(parents=True, exist_ok=True)
    legacy_app = "export default function App() {\n  return <h1>Legacy single page</h1>\n}\n"
    (page_dir / "src" / "App.tsx").write_text(legacy_app, encoding="utf-8")

    ensure_show_page_dir("seslegacy")

    # The agent's page is untouched.
    assert (page_dir / "src" / "App.tsx").read_text(encoding="utf-8") == legacy_app
    # No multi-page demo is sprinkled next to a customized single-page app.
    assert not (page_dir / "src" / "router.tsx").exists()
    assert not (page_dir / "src" / "pages").exists()
    # Missing shell/workspace files are still materialized.
    assert (page_dir / "index.html").exists()
    assert (page_dir / "src" / "main.tsx").exists()
    assert (page_dir / "src" / "styles.css").exists()
    assert (page_dir / "api" / "health.ts").exists()


def test_show_path_cli_json_creates_page(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True}).encode("utf-8")

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(["show", "path", "--session-id", "ses123", "--json"])
    assert cli.cmd_show_path(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["visibility"] == "private"
    assert payload["active_url"] == "https://alex.avibe.bot/show/ses123/"
    assert payload["private_url"] == "https://alex.avibe.bot/show/ses123/"
    assert payload["public_url"] is None
    assert payload["url_available"] is True
    assert payload["url_guidance"] is None
    assert "Do not send implementation details such as local paths to the user unless they ask for them." in payload["next_actions"]
    assert "Treat the Show Page as the primary collaboration surface; put meaningful updates there first." in payload["next_actions"]
    assert (
        "Use visual thinking: diagrams, timelines, maps, comparisons, dashboards, or small prototypes when they help."
        in payload["next_actions"]
    )
    assert (tmp_path / "show" / "ses123" / "index.html").exists()
    assert captured["url"] == "http://127.0.0.1:5123/api/show/sessions/ses123/prewarm"
    assert captured["payload"] == {}
    assert captured["timeout"] == 3


def test_show_path_cli_keeps_page_when_prewarm_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    def _urlopen(_request, timeout):
        raise OSError("runtime unavailable")

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(["show", "path", "--session-id", "ses123", "--json"])
    assert cli.cmd_show_path(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (tmp_path / "show" / "ses123" / "index.html").exists()


def test_show_path_defaults_to_caller_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesCaller")
    paths.ensure_data_dirs()
    _save_config()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {})

    args = cli.build_parser().parse_args(["show", "path", "--json"])
    assert cli.cmd_show_path(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "sesCaller"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Show Page session defaulted to this Agent Session.",
        "session_id": "sesCaller",
    }
    assert (tmp_path / "show" / "sesCaller" / "index.html").exists()


def test_show_path_requires_session_without_caller(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    paths.ensure_data_dirs()
    _save_config()

    args = cli.build_parser().parse_args(["show", "path", "--json"])
    assert cli.cmd_show_path(args) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "missing_session_target"
    assert payload["help_command"] == "vibe show path --help"


def test_show_path_cli_prewarm_uses_verified_loopback_for_non_loopback_host(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    config = _save_config()
    config.ui.setup_host = "192.168.2.3"
    config.ui.setup_port = 15130
    config.save()
    attempted = []

    class _Response:
        def __init__(self, payload=None):
            self.payload = payload or {"ok": True}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def _urlopen(request, timeout):
        attempted.append((request.full_url, timeout))
        if request.full_url == "http://127.0.0.1:15130/status":
            return _Response({"ui_pid": 123})
        return _Response()

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(["show", "path", "--session-id", "ses123", "--json"])
    assert cli.cmd_show_path(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert attempted == [
        ("http://127.0.0.1:15130/status", 1),
        ("http://127.0.0.1:15130/api/show/sessions/ses123/prewarm", 3),
    ]


def test_show_path_cli_prewarm_falls_back_to_configured_ui_host_after_loopback_mismatch(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    config = _save_config()
    config.ui.setup_host = "192.168.2.3"
    config.ui.setup_port = 15130
    config.save()
    attempted = []

    class _Response:
        def __init__(self, payload=None):
            self.payload = payload or {"ok": True}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def _urlopen(request, timeout):
        attempted.append((request.full_url, timeout, bool(getattr(request, "data", None))))
        if request.full_url == "http://127.0.0.1:15130/status":
            return _Response({"ui_pid": 999})
        if request.full_url.startswith("http://127.0.0.1:15130/"):
            raise AssertionError("unverified loopback target received the prewarm token")
        return _Response()

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(["show", "path", "--session-id", "ses123", "--json"])
    assert cli.cmd_show_path(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert attempted == [
        ("http://127.0.0.1:15130/status", 1, False),
        ("http://192.168.2.3:15130/api/show/sessions/ses123/prewarm", 3, True),
    ]


def test_show_list_cli_json_reports_existing_pages(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.ensure("ses-private")
        store.update_visibility("ses-public", "public")
    finally:
        store.close()

    args = cli.build_parser().parse_args(["show", "list", "--json"])
    assert cli.cmd_show_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert [page["session_id"] for page in payload["pages"]] == ["ses-public", "ses-private"]
    public_page = payload["pages"][0]
    assert public_page["visibility"] == "public"
    assert public_page["active_url"] == public_page["public_url"]
    assert public_page["active_url"].startswith("https://alex.avibe.bot/p/")


def test_show_list_cli_json_reports_pagination(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        for index in range(25):
            store.ensure(f"ses-page-{index:02d}")
    finally:
        store.close()

    args = cli.build_parser().parse_args(["show", "list", "--json"])
    assert cli.cmd_show_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 20
    assert payload["pagination"]["has_more"] is True
    assert payload["pagination"]["next_page"] == 2
    assert "vibe show list --json --page 2 --limit 20" == payload["pagination"]["next_command"]
    assert "More records are available" in payload["message"]


def test_show_list_cli_next_command_uses_absolute_time_filters(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        for index in range(25):
            store.ensure(f"ses-page-{index:02d}")
    finally:
        store.close()

    args = cli.build_parser().parse_args(
        ["show", "list", "--json", "--updated-after", "2026-05-25T08:00:00+08:00", "--limit", "10"]
    )
    assert cli.cmd_show_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert "--updated-after 2026-05-25T00:00:00+00:00" in payload["pagination"]["next_command"]
    assert "--updated-after 2026-05-25T08:00:00+08:00" not in payload["pagination"]["next_command"]
    assert payload["pagination"]["next_command"].endswith("--json --page 2 --limit 10")


def test_show_list_cli_filters_visibility(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()

    store = ShowPageStore()
    try:
        store.ensure("ses-private")
        store.update_visibility("ses-public", "public")
    finally:
        store.close()

    args = cli.build_parser().parse_args(["show", "list", "--visibility", "private"])
    assert cli.cmd_show_list(args) == 0

    output = capsys.readouterr().out
    assert "Count: 1" in output
    assert "Filter: visibility=private" in output
    assert "- ses-private" in output
    assert "- ses-public" not in output


def test_show_page_payload_requires_enabled_avibe_cloud(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    config = _save_config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    store = ShowPageStore()
    try:
        page = store.ensure("ses123")
        payload = show_page_payload(page)
        assert payload["active_url"] is None
        assert payload["private_url"] is None
        assert payload["public_url"] is None
        assert payload["url_available"] is False
        assert "Avibe Cloud is not connected" in payload["url_guidance"]
        assert "avibe.bot" in payload["url_guidance"]
        assert "`vibe remote pair`" in payload["url_guidance"]
    finally:
        store.close()


def test_show_update_cli_reports_transition_urls(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    prewarmed = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True}).encode("utf-8")

    def _urlopen(request, timeout):
        prewarmed.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return _Response()

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    parser = cli.build_parser()
    assert cli.cmd_show_path(parser.parse_args(["show", "path", "--session-id", "ses123", "--json"])) == 0
    capsys.readouterr()

    args = parser.parse_args(["show", "update", "--session-id", "ses123", "--visibility", "public", "--json"])
    assert cli.cmd_show_update(args) == 0
    public_payload = json.loads(capsys.readouterr().out)
    assert public_payload["visibility"] == "public"
    assert public_payload["active_url"] == public_payload["public_url"]
    assert public_payload["public_url"].startswith("https://alex.avibe.bot/p/")
    assert public_payload["previous_private_url"] == "https://alex.avibe.bot/show/ses123/"
    share_path = "/" + public_payload["public_url"].split("https://alex.avibe.bot/", 1)[1]
    assert prewarmed[-1] == (
        "http://127.0.0.1:5123/api/show/sessions/ses123/prewarm",
        {"base_path": share_path},
    )

    args = parser.parse_args(["show", "update", "--session-id", "ses123", "--visibility", "private", "--json"])
    assert cli.cmd_show_update(args) == 0
    private_payload = json.loads(capsys.readouterr().out)
    assert private_payload["visibility"] == "private"
    assert private_payload["active_url"] == "https://alex.avibe.bot/show/ses123/"
    assert private_payload["previous_public_url"] == public_payload["public_url"]
    assert prewarmed[-1] == ("http://127.0.0.1:5123/api/show/sessions/ses123/prewarm", {})


def test_show_status_and_update_default_to_caller_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesCaller")
    paths.ensure_data_dirs()
    _save_config()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {})

    parser = cli.build_parser()
    assert cli.cmd_show_path(parser.parse_args(["show", "path", "--json"])) == 0
    capsys.readouterr()

    assert cli.cmd_show_status(parser.parse_args(["show", "status", "--json"])) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["session_id"] == "sesCaller"
    assert status_payload["session_default_notice"]["session_id"] == "sesCaller"

    assert cli.cmd_show_update(parser.parse_args(["show", "update", "--visibility", "public", "--json"])) == 0
    update_payload = json.loads(capsys.readouterr().out)
    assert update_payload["session_id"] == "sesCaller"
    assert update_payload["visibility"] == "public"
    assert update_payload["session_default_notice"]["session_id"] == "sesCaller"


def test_show_update_rotate_share_fails_while_private(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    parser = cli.build_parser()
    args = parser.parse_args(["show", "update", "--session-id", "ses123", "--rotate-share", "--json"])
    assert cli.cmd_show_update(args) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "not_public"


def test_show_mark_cli_records_event_and_message(monkeypatch, tmp_path, capsys):
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions, messages, show_session_events
    from storage.settings_service import upsert_scope
    from storage import messages_service
    from sqlalchemy import select

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses123",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_ses123",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    args = cli.build_parser().parse_args(
        [
            "show",
            "mark",
            "--session-id",
            "ses123",
            "--target",
            "mark-default-summary",
            "--body",
            "Review this summary.",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["event"]["type"] == "assistant.mark.created"
    assert payload["event"]["message_id"]
    assert payload["event"]["transcript_text"].startswith("[agent-mark:default:created] mark-default-summary")

    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).scalar_one() == payload["event"]["id"]
        assert "Review this summary." in conn.execute(select(messages.c.content_text)).scalar_one()


def test_show_mark_defaults_to_caller_session(monkeypatch, tmp_path, capsys):
    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesCaller")
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="sesCaller",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_sesCaller",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    args = cli.build_parser().parse_args(
        [
            "show",
            "mark",
            "--target",
            "mark-default-summary",
            "--body",
            "Review this summary.",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "sesCaller"
    assert payload["session_default_notice"]["session_id"] == "sesCaller"
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.session_id)).scalar_one() == "sesCaller"


def test_show_mark_cli_posts_to_live_ui_when_running(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "event": {
                        "id": "show_evt_live",
                        "session_id": "ses123",
                        "scope_id": "scope123",
                        "type": "assistant.mark.created",
                        "actor": "assistant",
                        "scope": "default",
                        "anchor": {},
                        "payload": {},
                        "transcript_text": "[agent-mark:default] mark-default-summary\n\nReview this summary.",
                        "message_id": "msg_live",
                        "message": {"id": "msg_live"},
                        "created_at": "now",
                    },
                }
            ).encode("utf-8")

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["client"] = request.headers["X-vibe-show-client"]
        captured["cli_token"] = request.headers["X-vibe-show-cli-token"]
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(
        [
            "show",
            "mark",
            "--session-id",
            "ses123",
            "--target",
            "mark-default-summary",
            "--body",
            "Review this summary.",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["id"] == "show_evt_live"
    assert captured["url"] == "http://127.0.0.1:5123/api/show/sessions/ses123/events"
    assert captured["client"] == "cli"
    assert captured["cli_token"] == show_cli_event_token()
    assert captured["payload"]["type"] == "assistant.mark.created"
    assert captured["timeout"] == 3


def test_show_event_cli_records_generic_event(monkeypatch, tmp_path, capsys):
    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, messages, show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses123",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_ses123",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            "ses123",
            "--event-json",
            json.dumps(
                {
                    "type": "human.annotation.created",
                    "annotation": {
                        "intent": "question",
                        "comment": "Clarify this.",
                        "anchor": {"selector": "[mark-default='summary']", "textQuote": "summary"},
                    },
                }
            ),
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["event"]["type"] == "human.annotation.created"
    assert "Clarify this." in payload["event"]["transcript_text"]

    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).scalar_one() == payload["event"]["id"]
        assert "Clarify this." in conn.execute(select(messages.c.content_text)).scalar_one()


def test_show_event_defaults_to_caller_session(monkeypatch, tmp_path, capsys):
    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesCaller")
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="sesCaller",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_sesCaller",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--type",
            "assistant.page.updated",
            "--event-json",
            '{"summary":"Updated."}',
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "sesCaller"
    assert payload["session_default_notice"]["session_id"] == "sesCaller"
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.session_id)).scalar_one() == "sesCaller"


def test_show_event_cli_dispatch_flag_updates_annotation_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "event": {
                        "id": "show_evt_live",
                        "session_id": "ses123",
                        "scope_id": "scope123",
                        "type": "human.annotation.created",
                        "actor": "human",
                        "scope": "default",
                        "anchor": {},
                        "payload": {"dispatch": True},
                        "transcript_text": "[show-annotation:default:created] comment",
                        "message_id": "msg_live",
                        "message": {"id": "msg_live"},
                        "created_at": "now",
                    },
                }
            ).encode("utf-8")

    def _urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            "ses123",
            "--event-json",
            json.dumps({"type": "human.annotation.created", "annotation": {"comment": "Clarify this."}}),
            "--dispatch",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    assert captured["payload"]["annotation"]["dispatch"] is True
    assert "payload" not in captured["payload"]


def test_show_event_cli_dispatch_preserves_top_level_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True, "event": {"id": "show_evt_live"}}).encode("utf-8")

    def _urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            "ses123",
            "--type",
            "human.intent.submitted",
            "--event-json",
            json.dumps({"comment": "Pick B", "intent": "choose"}),
            "--dispatch",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    assert captured["payload"]["payload"]["comment"] == "Pick B"
    assert captured["payload"]["payload"]["intent"] == "choose"
    assert captured["payload"]["payload"]["dispatch"] is True
    assert captured["payload"]["type"] == "human.intent.submitted"


def test_show_event_cli_dispatch_fallback_records_and_dispatches(monkeypatch, tmp_path, capsys):
    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses123",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_ses123",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": None})
    dispatched = []

    async def _fake_run_dispatch(event):
        dispatched.append(event)

    monkeypatch.setattr("vibe.ui_server._run_show_event_dispatch", _fake_run_dispatch)

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            "ses123",
            "--type",
            "human.intent.submitted",
            "--event-json",
            json.dumps({"comment": "Pick B"}),
            "--dispatch",
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"]["payload"]["comment"] == "Pick B"
    assert payload["event"]["payload"]["dispatch"] is True
    assert dispatched and dispatched[0]["id"] == payload["event"]["id"]
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).scalar_one() == payload["event"]["id"]


def test_show_event_cli_fallback_rejects_mismatched_session_id(monkeypatch, tmp_path, capsys):
    from sqlalchemy import select

    from storage import messages_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, show_session_events
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _save_config()
    ensure_sqlite_state()

    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_show", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id="ses123",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_ses123",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})

    def _urlopen(request, timeout):
        raise cli.urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    args = cli.build_parser().parse_args(
        [
            "show",
            "event",
            "--session-id",
            "ses123",
            "--event-json",
            json.dumps(
                {
                    "sessionId": "ses_other",
                    "type": "human.annotation.created",
                    "annotation": {"comment": "Wrong session."},
                }
            ),
            "--json",
        ]
    )
    assert cli.cmd_show(args) == 1

    error_payload = json.loads(capsys.readouterr().err)
    assert error_payload["code"] == "session_mismatch"
    with engine.connect() as conn:
        assert conn.execute(select(show_session_events.c.id)).first() is None


def test_show_mark_cli_posts_to_configured_ui_host_when_running(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    config = _save_config()
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "100.97.103.112"
    config.ui.setup_port = 15130
    config.save()
    monkeypatch.setattr(cli.runtime, "read_status", lambda: {"ui_pid": 123})

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True, "event": {"id": "show_evt_live"}}).encode("utf-8")

    def _urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)

    event = cli._post_show_mark_to_live_ui(
        "ses123",
        {"type": "assistant.mark.created", "mark": {"target": "summary", "body": "body"}},
    )

    assert event == {"id": "show_evt_live"}
    assert captured["url"] == "http://100.97.103.112:15130/api/show/sessions/ses123/events"
    assert captured["timeout"] == 3
