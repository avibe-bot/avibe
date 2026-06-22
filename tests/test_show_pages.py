import json
from dataclasses import dataclass

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from core.show_pages import ShowPageError, ShowPageStore, ensure_show_page_dir, show_cli_event_token, show_page_payload
from storage.pagination import PageRequest
from vibe import cli


@dataclass(frozen=True)
class _FakeShowRuntimeResult:
    available: bool
    reason: str | None = None


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


def test_runtime_prepare_cli_reports_warning_only_failure(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--json"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            assert force is False
            assert offline is None
            return {"ok": False, "reason": "runtime_node_missing"}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())

    assert cli.cmd_runtime(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "runtime_node_missing"


def test_runtime_prepare_cli_preserves_offline_environment(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--json"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            assert force is False
            assert offline is None
            return {"ok": True}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())

    assert cli.cmd_runtime(args) == 0


def test_runtime_manager_from_args_preserves_offline_environment(monkeypatch, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "status"])
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_OFFLINE", "1")

    manager = cli._show_runtime_manager_from_args(args)

    assert manager.offline is True


def test_runtime_prepare_cli_strict_fails_when_prepare_fails(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["runtime", "prepare", "--strict"])

    class FakeRuntimeManager:
        def prepare(self, *, force=False, offline=None):
            return {"ok": False, "reason": "runtime_archive_download_failed"}

    monkeypatch.setattr(cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())

    assert cli.cmd_runtime(args) == 1
    assert "runtime_archive_download_failed" in capsys.readouterr().err


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
    app_tsx = (page_dir / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Building your Show Page" in app_tsx
    assert "Please visualize this session as a Show Page." in app_tsx
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
