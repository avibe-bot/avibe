"""Contract tests for Session and Bot mention resolution across Web, previews, push, and IM."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text, update

from config.v2_config import RemoteAccessConfig, VibeCloudRemoteAccessConfig
from core.bots import BotService
from core.vibe_agents import VibeAgentStore
from core.mention_resolution import (
    extract_mention_candidates,
    render_im_mentions,
    render_plain_mentions_in_conn,
    resolve_mentions_in_conn,
)
from modules.im.feishu import FeishuBot
from modules.im.formatters.telegram_formatter import TelegramFormatter
from modules.im.slack import SlackBot
from modules.im.wechat import WeChatBot
from storage import messages_service, projects_service
from storage.agent_session_rows import (
    PRIVATE_AGENT_RUN_SCOPE_TYPE,
    create_agent_session_row,
)
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations
from storage.models import agent_sessions, scopes
from vibe.authorization import AuthorizationContext


def _setup_db(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    return db_path, engine


def _seed_project_session(
    conn,
    *,
    project_name: str,
    folder: str,
    session_id: str,
    title: str | None,
    visibility: str = "foreground",
):
    from pathlib import Path

    Path(folder).mkdir(parents=True, exist_ok=True)
    project = projects_service.create_project(
        conn,
        folder_path=folder,
        display_name=project_name,
    )
    scope_id = f"avibe::project::{project['id']}"
    create_agent_session_row(
        conn,
        scope_id=scope_id,
        agent_name="vibey",
        agent_backend="vibey",
        agent_variant="vibey",
        session_anchor=f"anchor-{session_id}",
        session_id=session_id,
        title=title,
        visibility=visibility,
    )
    return project, scope_id


def _owner_context() -> AuthorizationContext:
    return AuthorizationContext(
        is_remote=True,
        subject="owner-1",
        instance_role="owner",
    )


def _viewer_context(subject: str = "viewer-1") -> AuthorizationContext:
    return AuthorizationContext(
        is_remote=True,
        subject=subject,
        instance_role="viewer",
        instance_kind="organization",
    )


def test_extract_mention_candidates_skips_code_and_urls_and_reads_bare_in_cjk():
    text_body = (
        "好，已经起了一个后台 Agent（#sesw34zgzgcvp），同时看看 #<ses6jr7c5h2q6> 和 @<bot:product>。\n"
        "- parent\n"
        "    - child sees #<sesnested0001> and #sesnested0002\n"
        "1. step\n"
        "    ordered continuation #<sesordered001>\n"
        "\n"
        "    loose continuation #sesloose00001\n"
        "\n"
        "Para line\n"
        "    lazy continuation #<seslazy000001>\n"
        "\n"
        "Use ``` to open a fence. Then #<sesafterbt001>.\n"
        "\n"
        "Code span `#<sescode00001>` and `#sescode00002` and `echo\n#<sesmultiline>` and ``quote `\n#<sesdblbt0001>`` stay untouched.\n"
        "```\n#<sesfence0001>\n#sesfence0002\n```\n"
        "````markdown\n```\n#<sesnestedf01>\n```\n#<sesnestedf02>\n````\n"
        "10. Step\n\n    ```py\n    x=1\n\n    print('#<seslistf0001>')\n    ```\n\n"
        "1. Step\n   - sub\n\n     ```py\n     x=1\n\n     print('#<seslistf0002>')\n     ```\n\n"
        "```\n```\nSee #<sesemptyf001>\n\n"
        "```py\r\nx=1\r\n```\r\nAfter CRLF #<sescrlf00001>\n\n"
        "Intro\n\n    a ```x``` b\n    more #<sesmidf00001>\n\n"
        "- run ```cmd``` first\n\n    continuation #<seslistmid01>\n\n"
        "~~~\n#sestilde0001\n~~~\n"
        "\n"
        "    #<sesindent001>\n"
        "\t#sesindent002\n"
        "\n"
        "URL fragment https://example.com/page#sesurl000001, [see #seslabel0001](https://example.com), "
        "[link](#seslink00001), ![img](#<sesimg000001>), and <https://example.com/#sesauto00001> stay untouched.\n"
        "Marker-dest links [3.2.9 检查](#<sesmlink0001>), [产品](@<bot:product>), and @<agent:vibey> / [v](@<agent:antigravity>) are extracted.\n"
        "```ts\n#sesstreaming1\n"
    )
    sids, bots, agent_names = extract_mention_candidates(text_body, include_bare_sessions=True)
    assert sids == [
        "sesw34zgzgcvp",
        "ses6jr7c5h2q6",
        "sesnested0001",
        "sesnested0002",
        "sesordered001",
        "sesloose00001",
        "seslazy000001",
        "sesafterbt001",
        "sesemptyf001",
        "sescrlf00001",
        "seslistmid01",
        "sesmlink0001",
    ]
    assert bots == ["product"]
    assert agent_names == ["vibey", "antigravity"]

    # Without bare mode (e.g. non-agent messages), bare #ses... is ignored.
    sids_bracket_only, bots_bracket_only, agents_bracket_only = extract_mention_candidates(
        text_body, include_bare_sessions=False
    )
    assert sids_bracket_only == [
        "ses6jr7c5h2q6",
        "sesnested0001",
        "sesordered001",
        "seslazy000001",
        "sesafterbt001",
        "sesemptyf001",
        "sescrlf00001",
        "seslistmid01",
        "sesmlink0001",
    ]
    assert bots_bracket_only == ["product"]
    assert agents_bracket_only == ["vibey", "antigravity"]


def test_resolve_mentions_uses_live_titles_enforces_access_and_uses_pk_index(tmp_path, monkeypatch):
    db_path, engine = _setup_db(tmp_path, monkeypatch)
    with engine.begin() as conn:
        proj_a, _ = _seed_project_session(
            conn,
            project_name="Alpha",
            folder=str(tmp_path / "alpha"),
            session_id="sesalpha1234",
            title="Initial Title",
        )
        proj_b, _ = _seed_project_session(
            conn,
            project_name="Beta",
            folder=str(tmp_path / "beta"),
            session_id="sesbeta56789",
            title="Secret Beta",
        )
        _seed_project_session(
            conn,
            project_name="Alpha",
            folder=str(tmp_path / "alpha"),
            session_id="sesuntitled1",
            title=None,
            visibility="background",
        )
        # Private agent run pseudo-scope is never openable in chat.
        conn.execute(
            scopes.insert().values(
                id="avibe::private::run1",
                platform="avibe",
                scope_type=PRIVATE_AGENT_RUN_SCOPE_TYPE,
                native_type=PRIVATE_AGENT_RUN_SCOPE_TYPE,
                native_id="run1",
                is_private=True,
                supports_threads=False,
                metadata_json="{}",
                first_seen_at="2026-11-29T00:00:00Z",
                last_seen_at="2026-11-29T00:00:00Z",
                updated_at="2026-11-29T00:00:00Z",
            )
        )
        create_agent_session_row(
            conn,
            scope_id="avibe::private::run1",
            agent_name="vibey",
            agent_backend="vibey",
            agent_variant="vibey",
            session_anchor="anchor-sesprivate01",
            session_id="sesprivate01",
            title="Private Run",
            require_workdir=False,
        )
        # Restrict Beta so member-1 can read Alpha, but NOT Beta.
        from storage import project_access_service

        result = project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": proj_b["id"],
                "revision": 1,
                "mode": "restricted",
                "bindings": [],
            },
        )
        assert result.outcome == "applied"
        # Rename Alpha session after creation to confirm read-time resolution returns current title.
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "sesalpha1234")
            .values(title="Renamed Live Title")
        )
    # Create a Bot so @<bot:product> and its Bot session resolve for Owner only.
    BotService(VibeAgentStore(db_path)).create_bot(
        display_name="产品伙伴",
        handle="product",
        authorization_context=_owner_context(),
    )

    with engine.connect() as conn:
        # Pin query plan: primary-key SEARCH on agent_sessions, never a table scan.
        plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT agent_sessions.id, agent_sessions.title, "
                "agent_sessions.scope_id, scopes.native_type "
                "FROM agent_sessions LEFT OUTER JOIN scopes ON scopes.id = agent_sessions.scope_id "
                "WHERE agent_sessions.id IN ('sesalpha1234', 'sesbeta56789')"
            )
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan)
        assert "SEARCH agent_sessions" in plan_text
        assert "SCAN agent_sessions" not in plan_text

        owner_resolved = resolve_mentions_in_conn(
            conn,
            session_ids=[
                "sesalpha1234",
                "sesbeta56789",
                "sesuntitled1",
                "sesprivate01",
                "ses_product",
                "sesmissing99",
            ],
            bot_handles=["product", "ghost"],
            authorization_context=_owner_context(),
            language="en",
        )
        assert set(owner_resolved.sessions) == {
            "sesalpha1234",
            "sesbeta56789",
            "sesuntitled1",
            "ses_product",
        }
        assert owner_resolved.sessions["sesalpha1234"].display_title == "Renamed Live Title"
        assert owner_resolved.sessions["sesuntitled1"].title is None
        assert owner_resolved.sessions["sesuntitled1"].display_title == "Untitled session"
        assert owner_resolved.sessions["ses_product"].display_title == "产品伙伴"
        assert set(owner_resolved.bots) == {"product"}
        assert owner_resolved.bots["product"].display_name == "产品伙伴"

        # Chinese fallback for untitled sessions.
        zh_resolved = resolve_mentions_in_conn(
            conn,
            session_ids=["sesuntitled1"],
            authorization_context=_owner_context(),
            language="zh",
        )
        assert zh_resolved.sessions["sesuntitled1"].display_title == "未命名会话"

        # Restricted viewer can open Alpha (and untitled under Alpha), NOT Beta or Bot sessions/bots.
        viewer_resolved = resolve_mentions_in_conn(
            conn,
            session_ids=[
                "sesalpha1234",
                "sesbeta56789",
                "sesuntitled1",
                "ses_product",
            ],
            bot_handles=["product"],
            authorization_context=_viewer_context("viewer-1"),
            language="en",
        )
        assert set(viewer_resolved.sessions) == {"sesalpha1234", "sesuntitled1"}
        assert viewer_resolved.bots == {}


def test_transcript_and_inbox_previews_enrich_mentions_per_reader(tmp_path, monkeypatch):
    db_path, engine = _setup_db(tmp_path, monkeypatch)
    with engine.begin() as conn:
        _, scope_a = _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesmain00001",
            title="Main Chat",
        )
        _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesworker001",
            title="登录跳转修复",
            visibility="background",
        )
    BotService(VibeAgentStore(db_path)).create_bot(
        display_name="产品伙伴",
        handle="product",
        authorization_context=_owner_context(),
    )
    VibeAgentStore(db_path).create(name="vibey", backend="vibey")
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=scope_a,
            session_id="sesmain00001",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="好，已经起了一个后台 @<agent:vibey>（[发版检查](#<sesworker001>)），同时同步 @<bot:product>，忽略 #<sesmissing99> 和 @<agent:ghost>。",
        )
        messages_service.append(
            conn,
            scope_id=scope_a,
            session_id="sesmain00001",
            platform="avibe",
            author="harness",
            source="harness",
            message_type="harness",
            text="[Reply truncated; use `vibe data query` to read #<sesworker001>'s transcript in full.] CC @<bot:product> @<agent:vibey> #<sesmissing99>",
        )

    with engine.connect() as conn:
        transcript = messages_service.list_session_messages(
            conn,
            session_id="sesmain00001",
            authorization_context=_owner_context(),
        )
        [msg, harness_msg] = transcript["messages"]
        expected_refs = [
            {
                "kind": "session",
                "session_id": "sesworker001",
                "title": "登录跳转修复",
                "openable": True,
            },
            {
                "kind": "bot",
                "handle": "product",
                "display_name": "产品伙伴",
            },
            {
                "kind": "agent",
                "name": "vibey",
            },
        ]
        assert msg["content"]["references"] == expected_refs
        assert harness_msg["content"]["references"] == expected_refs

        # Restricted viewer gets only openable Session refs; Bot refs and unshared Agent refs are omitted.
        viewer_transcript = messages_service.list_session_messages(
            conn,
            session_id="sesmain00001",
            authorization_context=_viewer_context("viewer-1"),
        )
        assert viewer_transcript["messages"][1]["content"]["references"] == [
            expected_refs[0],
        ]

        inbox = messages_service.list_inbox_sessions(
            conn,
            only_session="sesmain00001",
            authorization_context=_owner_context(),
            resolve_mentions=True,
        )
        [row] = inbox["sessions"]
        assert row["preview_text"] == (
            "好，已经起了一个后台 @vibey（登录跳转修复），同时同步 @产品伙伴，忽略 #<sesmissing99> 和 @<agent:ghost>。"
        )


def test_im_delivery_formats_links_per_platform_and_public_url(tmp_path, monkeypatch):
    db_path, engine = _setup_db(tmp_path, monkeypatch)
    with engine.begin() as conn:
        _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesw34zgzgcvp",
            title="Fix [auth] redirect",
        )
    BotService(VibeAgentStore(db_path)).create_bot(
        display_name="产品伙伴",
        handle="product",
        authorization_context=_owner_context(),
    )

    source = (
        "Started #<sesw34zgzgcvp> (also #sesw34zgzgcvp) with @<bot:product>; "
        "unknown #<sesmissing99> and `#<sesw34zgzgcvp>` stay literal."
    )
    public_cfg = SimpleNamespace(
        language="en",
        remote_access=RemoteAccessConfig(
            vibe_cloud=VibeCloudRemoteAccessConfig(enabled=True, public_url="https://demo.avibe.bot/")
        ),
    )
    local_cfg = SimpleNamespace(
        language="en",
        remote_access=RemoteAccessConfig(
            vibe_cloud=VibeCloudRemoteAccessConfig(enabled=False, public_url="")
        ),
    )

    expected_link = r"[Fix \[auth\] redirect](https://demo.avibe.bot/chat/sesw34zgzgcvp)"
    expected_plain = r"Fix \[auth\] redirect"
    for platform in ("slack", "discord", "telegram", "lark"):
        with_url = render_im_mentions(
            source, platform=platform, config=public_cfg, db_path=db_path
        )
        assert with_url == (
            f"Started {expected_link} (also {expected_link}) with @产品伙伴; "
            "unknown #<sesmissing99> and `#<sesw34zgzgcvp>` stay literal."
        )
        without_url = render_im_mentions(
            source, platform=platform, config=local_cfg, db_path=db_path
        )
        assert without_url == (
            f"Started {expected_plain} (also {expected_plain}) with @产品伙伴; "
            "unknown #<sesmissing99> and `#<sesw34zgzgcvp>` stay literal."
        )

    # WeChat has no clickable masked links: always title alone, even with a public URL.
    wechat_out = render_im_mentions(
        source, platform="wechat", config=public_cfg, db_path=db_path
    )
    assert wechat_out == (
        f"Started {expected_plain} (also {expected_plain}) with @产品伙伴; "
        "unknown #<sesmissing99> and `#<sesw34zgzgcvp>` stay literal."
    )
    assert "https://demo.avibe.bot" not in wechat_out

    # Plain-title IM output escapes Markdown metacharacters so untrusted titles
    # cannot inject links or formatting, and outer Markdown links are never nested.
    with engine.begin() as conn:
        _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesmarkup001",
            title="*[phish](https://evil.test)*",
        )
    VibeAgentStore(db_path).create(name="vibey", backend="vibey")
    VibeAgentStore(db_path).create(name="disabled-agt", backend="vibey", enabled=False)

    # Marker-destination links `[label](#<id>)` / `[label](@<bot:…>)` / `@<agent:name>`
    # resolve to the live title/link on IM and never emit dead `#<ses…>` links.
    link_form_src = (
        "Check [3.2.9 检查](#<sesw34zgzgcvp>), [伙伴](@<bot:product>), @<agent:vibey>, "
        "@<agent:disabled-agt>, @<agent:ghost>, and [missing](#<sesmissing99>)."
    )
    slack_link_form = render_im_mentions(
        link_form_src, platform="slack", config=public_cfg, db_path=db_path
    )
    assert slack_link_form == (
        f"Check {expected_link}, @产品伙伴, @vibey, "
        "@<agent:disabled-agt>, @<agent:ghost>, and [missing](#<sesmissing99>)."
    )
    wechat_link_form = render_im_mentions(
        link_form_src, platform="wechat", config=public_cfg, db_path=db_path
    )
    assert wechat_link_form == (
        f"Check {expected_plain}, @产品伙伴, @vibey, "
        "@<agent:disabled-agt>, @<agent:ghost>, and [missing](#<sesmissing99>)."
    )

    plain_markup_tg = TelegramFormatter().render(
        render_im_mentions(
            "See #<sesmarkup001> and [outer #sesw34zgzgcvp](https://example.com).",
            platform="telegram",
            config=local_cfg,
            db_path=db_path,
        )
    )
    assert "evil.test</a>" not in plain_markup_tg
    assert "<i>" not in plain_markup_tg
    assert "*[phish](https://evil.test)*" in plain_markup_tg
    assert '<a href="https://example.com">outer #sesw34zgzgcvp</a>' in plain_markup_tg

    # Verify each real platform formatter renders the escaped Markdown link cleanly.
    telegram_html = TelegramFormatter().render(
        render_im_mentions(source, platform="telegram", config=public_cfg, db_path=db_path)
    )
    assert (
        '<a href="https://demo.avibe.bot/chat/sesw34zgzgcvp">Fix [auth] redirect</a>'
        in telegram_html
    )
    assert "<code>#&lt;sesw34zgzgcvp&gt;</code>" in telegram_html

    slack_bot = SlackBot.__new__(SlackBot)
    from markdown_to_mrkdwn import SlackMarkdownConverter

    slack_bot.markdown_converter = SlackMarkdownConverter()
    slack_mrkdwn = slack_bot._convert_markdown_to_slack_mrkdwn(
        render_im_mentions(source, platform="slack", config=public_cfg, db_path=db_path)
    )
    assert "<https://demo.avibe.bot/chat/sesw34zgzgcvp|Fix [auth] redirect>" in slack_mrkdwn
    assert "`#<sesw34zgzgcvp>`" in slack_mrkdwn

    feishu_bot = FeishuBot.__new__(FeishuBot)
    feishu_card = json.loads(
        feishu_bot._build_card_json(
            render_im_mentions(source, platform="lark", config=public_cfg, db_path=db_path)
        )
    )
    assert (
        "[Fix &#91;auth&#93; redirect](https://demo.avibe.bot/chat/sesw34zgzgcvp)"
        in feishu_card["body"]["elements"][0]["content"]
    )

    wechat_bot = WeChatBot.__new__(WeChatBot)
    from modules.im.formatters.wechat_formatter import WeChatFormatter

    wechat_bot.formatter = WeChatFormatter()
    wechat_plain = wechat_bot.format_markdown(wechat_out)
    assert "Fix [auth] redirect" in wechat_plain
    assert "https://demo.avibe.bot" not in wechat_plain


def test_web_push_resolves_mentions_before_truncation(tmp_path):
    _db_path, engine = _setup_db(tmp_path)
    with engine.begin() as conn:
        _, scope_a = _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesmain00001",
            title="Main Chat",
        )
        _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesw34zgzgcvp",
            title="登录跳转修复",
        )
        raw = "背景任务已启动：#<sesw34zgzgcvp>，请查看。"
        rendered = render_plain_mentions_in_conn(
            conn, raw, include_bare_sessions=True, authorization_context=_owner_context()
        )
        assert rendered == "背景任务已启动：登录跳转修复，请查看。"

    # Also verify _workbench_event_payload_for_context enriches live SSE frames.
    from vibe import ui_server

    with patch.object(ui_server, "_projects_engine", return_value=engine):
        msg_payload = json.dumps(
            {
                "type": "message.new",
                "data": {
                    "id": "msg_1",
                    "session_id": "sesmain00001",
                    "author": "agent",
                    "text": "Started #<sesw34zgzgcvp> and #<sesmissing99>.",
                    "content": {},
                    "metadata": {},
                },
            }
        )
        projected = json.loads(
            ui_server._workbench_event_payload_for_context(
                _owner_context(), "message.new", msg_payload
            )
            or "{}"
        )
        assert projected["data"]["content"]["references"] == [
            {
                "kind": "session",
                "session_id": "sesw34zgzgcvp",
                "title": "登录跳转修复",
                "openable": True,
            }
        ]

        inbox_payload = json.dumps(
            {
                "type": "inbox.session.updated",
                "data": {
                    "session_id": "sesmain00001",
                    "preview_text": "Started #sesw34zgzgcvp.",
                },
            }
        )
        projected_inbox = json.loads(
            ui_server._workbench_event_payload_for_context(
                _owner_context(), "inbox.session.updated", inbox_payload
            )
            or "{}"
        )
        assert projected_inbox["data"]["preview_text"] == "Started 登录跳转修复."

        # Non-owner viewer on SSE receives neither restricted-Project nor Bot mentions.
        with engine.begin() as conn:
            proj_secret, _ = _seed_project_session(
                conn,
                project_name="SecretProj",
                folder=str(tmp_path / "secret"),
                session_id="sessecret001",
                title="Top Secret",
            )
            from storage import project_access_service

            res = project_access_service.apply_project_access_intent(
                conn,
                {
                    "project_id": proj_secret["id"],
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [],
                },
            )
            assert res.outcome == "applied"

        BotService(VibeAgentStore(_db_path)).create_bot(
            display_name="产品伙伴",
            handle="product",
            authorization_context=_owner_context(),
        )

        secret_msg = json.dumps(
            {
                "type": "message.new",
                "data": {
                    "id": "msg_2",
                    "session_id": "sesmain00001",
                    "author": "agent",
                    "text": "Check #<sesw34zgzgcvp>, #<sessecret001>, #<ses_product>, and @<bot:product>.",
                    "content": {},
                    "metadata": {},
                },
            }
        )
        viewer_msg = json.loads(
            ui_server._workbench_event_payload_for_context(
                _viewer_context("viewer-1"), "message.new", secret_msg
            )
            or "{}"
        )
        assert viewer_msg["data"]["content"]["references"] == [
            {
                "kind": "session",
                "session_id": "sesw34zgzgcvp",
                "title": "登录跳转修复",
                "openable": True,
            }
        ]

        secret_inbox = json.dumps(
            {
                "type": "inbox.session.updated",
                "data": {
                    "session_id": "sesmain00001",
                    "preview_text": "See #sessecret001 and @<bot:product>.",
                },
            }
        )
        viewer_inbox = json.loads(
            ui_server._workbench_event_payload_for_context(
                _viewer_context("viewer-1"), "inbox.session.updated", secret_inbox
            )
            or "{}"
        )
        assert viewer_inbox["data"]["preview_text"] == "See #sessecret001 and @<bot:product>."

        # Live Harness message.new rows are enriched per reader too (owner sees openable
        # Session + Bot refs; restricted viewer sees only openable Session refs).
        harness_sse = json.dumps(
            {
                "type": "message.new",
                "data": {
                    "id": "msg_h_live",
                    "session_id": "sesmain00001",
                    "author": "harness",
                    "text": "[Reply truncated; read #<sesw34zgzgcvp>'s transcript.] #<sessecret001> @<bot:product>",
                    "content": {},
                    "metadata": {},
                },
            }
        )
        owner_harness = json.loads(
            ui_server._workbench_event_payload_for_context(
                _owner_context(), "message.new", harness_sse
            )
            or "{}"
        )
        assert owner_harness["data"]["content"]["references"] == [
            {
                "kind": "session",
                "session_id": "sesw34zgzgcvp",
                "title": "登录跳转修复",
                "openable": True,
            },
            {
                "kind": "session",
                "session_id": "sessecret001",
                "title": "Top Secret",
                "openable": True,
            },
            {
                "kind": "bot",
                "handle": "product",
                "display_name": "产品伙伴",
            },
        ]
        viewer_harness = json.loads(
            ui_server._workbench_event_payload_for_context(
                _viewer_context("viewer-1"), "message.new", harness_sse
            )
            or "{}"
        )
        assert viewer_harness["data"]["content"]["references"] == [
            {
                "kind": "session",
                "session_id": "sesw34zgzgcvp",
                "title": "登录跳转修复",
                "openable": True,
            }
        ]


@pytest.mark.asyncio
async def test_dispatcher_resolve_im_mentions_keeps_avibe_canonical(tmp_path):
    from core.message_dispatcher import ConsolidatedMessageDispatcher
    from modules.im import MessageContext

    db_path, engine = _setup_db(tmp_path)
    with engine.begin() as conn:
        _seed_project_session(
            conn,
            project_name="Main",
            folder=str(tmp_path / "main"),
            session_id="sesw34zgzgcvp",
            title="Auth Fix",
        )

    cfg = SimpleNamespace(
        language="en",
        remote_access=RemoteAccessConfig(
            vibe_cloud=VibeCloudRemoteAccessConfig(enabled=True, public_url="https://demo.avibe.bot")
        ),
    )
    dispatcher = ConsolidatedMessageDispatcher.__new__(ConsolidatedMessageDispatcher)
    dispatcher.controller = SimpleNamespace(config=cfg)

    VibeAgentStore(db_path).create(name="vibey", backend="vibey")

    avibe_ctx = MessageContext(user_id="u1", channel_id="c1", platform="avibe")
    discord_ctx = MessageContext(user_id="u1", channel_id="c1", platform="discord")
    body = "Check #<sesw34zgzgcvp>."
    agent_only = "Use @<agent:vibey> for this."

    assert await dispatcher._resolve_im_mentions(avibe_ctx, body) == body
    assert await dispatcher._resolve_im_mentions(avibe_ctx, agent_only) == agent_only
    with patch("core.mention_resolution.create_sqlite_engine", return_value=create_sqlite_engine(db_path)):
        out = await dispatcher._resolve_im_mentions(discord_ctx, body)
        out_agent = await dispatcher._resolve_im_mentions(discord_ctx, agent_only)
    assert out == "Check [Auth Fix](https://demo.avibe.bot/chat/sesw34zgzgcvp)."
    assert out_agent == "Use @vibey for this."
