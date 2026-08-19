"""Closed-loop scenario evidence for Slack Memory attachment capture."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from core.caller_context import AVIBE_SESSION_ID_ENV
from core.memory.admission import CaptureAdmission, InboundTurnFacts
from core.memory.observations import AddAck, AddRejected
from core.memory.types import RecallItems, RecallPolicy, memory_item_payload
from modules.im.base import FileAttachment
from modules.im.message_facts import (
    is_ordinary_discord_attachment,
    is_ordinary_feishu_attachment,
    is_ordinary_telegram_attachment,
    is_ordinary_wechat_attachment,
)
from tests.scenario_harness.memory_im_attachments import (
    PNG_BYTES,
    PRINCIPAL,
    PROJECT,
    MemoryIMAttachmentScenarioHarness,
)
from vibe import cli, internal_client


def _xlsx_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", b"content types")
        archive.writestr("xl/workbook.xml", b"workbook")
    return payload.getvalue()


def test_bound_slack_dm_attachment_reaches_search_with_redacted_call_log(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-001."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(tmp_path)
    asyncio.run(
        harness.capture(
            text="Remember this screenshot",
            payloads={"screenshot.png": ("image/png", PNG_BYTES)},
        )
    )

    assert harness.downloader is not None
    assert harness.downloader.calls == [
        {"name": "screenshot.png", "max_bytes": None}
    ]
    assert len(harness.provider.captures) == 1
    assert len(harness.provider.flushes) == 1
    assert harness.provider.observed_payloads == [PNG_BYTES]
    assert len(harness.provider.call_log) == 1

    call = harness.provider.call_log[0]
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    assert call.kind == "multimodal_llm"
    assert "data:image/png" not in call.request_json
    assert encoded not in call.request_json
    assert "file://" not in call.request_json
    assert "[ATTACHMENT_OMITTED]" in call.request_json

    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, "ses-memory-im-attachment")

    def search_sync(query: str, limit: int, **kwargs):
        assert kwargs == {
            "mode": "hybrid",
            "project": None,
            "caller_session_id": "ses-memory-im-attachment",
        }
        result = asyncio.run(
            harness.module.recall(
                query,
                policy=RecallPolicy(mode="hybrid", max_results=limit),
                principal_id=PRINCIPAL,
                project_id=PROJECT,
            )
        )
        assert isinstance(result, RecallItems)
        return {
            "status_code": 200,
            "body": {
                "status": "ok",
                "items": [memory_item_payload(item) for item in result.items],
            },
        }

    monkeypatch.setattr(internal_client, "memory_search_sync", search_sync)
    args = cli.build_parser().parse_args(
        ["memory", "search", "screenshot", "--limit", "3", "--json"]
    )

    assert cli.cmd_memory(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["items"] == [
        {
            "kind": "fact",
            "text": "Captured Slack attachment screenshot.png",
            "date": None,
        }
    ]


@pytest.mark.parametrize(
    ("bound", "is_dm"),
    [(False, True), (True, False)],
)
def test_denied_slack_scope_never_reaches_memory_provider(
    tmp_path,
    monkeypatch,
    bound: bool,
    is_dm: bool,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-002."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(
        tmp_path,
        bound=bound,
        is_dm=is_dm,
    )
    asyncio.run(
        harness.capture(
            text="Do not capture",
            payloads={"private.png": ("image/png", PNG_BYTES)},
        )
    )

    assert harness.downloader is not None
    assert len(harness.downloader.calls) == 1
    assert harness.provider.captures == []
    assert harness.provider.flushes == []
    assert harness.provider.call_log == []
    assert harness.memory_bundle_entries == ()


def test_missing_multimodal_config_preserves_text_without_attachment_activity(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    mixed_root = tmp_path / "mixed"
    monkeypatch.setenv("AVIBE_HOME", str(mixed_root / "avibe-home"))
    mixed = MemoryIMAttachmentScenarioHarness(
        mixed_root,
        attachment_status="not_configured",
    )
    asyncio.run(
        mixed.capture(
            text="Remember the accompanying note",
            payloads={"note.png": ("image/png", PNG_BYTES)},
        )
    )

    assert len(mixed.provider.captures) == 1
    assert mixed.provider.captures[0].text == "Remember the accompanying note"
    assert mixed.provider.captures[0].attachments == ()
    assert len(mixed.provider.flushes) == 1
    assert mixed.provider.call_log == []
    assert mixed.memory_bundle_entries == ()

    attachment_only_root = tmp_path / "attachment-only"
    monkeypatch.setenv("AVIBE_HOME", str(attachment_only_root / "avibe-home"))
    attachment_only = MemoryIMAttachmentScenarioHarness(
        attachment_only_root,
        attachment_status="not_configured",
    )
    asyncio.run(
        attachment_only.capture(
            text="",
            payloads={"only.png": ("image/png", PNG_BYTES)},
        )
    )

    assert attachment_only.provider.captures == []
    assert attachment_only.provider.flushes == []
    assert attachment_only.provider.call_log == []
    assert attachment_only.memory_bundle_entries == ()


def test_invalid_sibling_preserves_valid_attachment_and_leaves_no_memory_leak(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(tmp_path)
    asyncio.run(
        harness.capture(
            text="Keep only the valid image",
            payloads={
                "valid.png": ("image/png", PNG_BYTES),
                "excluded.svg": (
                    "image/svg+xml",
                    b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
                ),
            },
        )
    )

    assert harness.downloader is not None
    assert [call["name"] for call in harness.downloader.calls] == [
        "valid.png",
        "excluded.svg",
    ]
    assert len(harness.provider.captures) == 1
    assert [item.name for item in harness.provider.captures[0].attachments] == [
        "valid.png"
    ]
    assert len(harness.provider.call_log) == 1
    assert harness.memory_bundle_entries == ()
    assert not tuple(harness.home.rglob("*.part"))


def test_claimed_attachment_preflight_failure_retries_caption_as_text_only(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-009."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(tmp_path)
    original_drain = harness.module.drain

    async def corrupt_bundle_then_drain() -> int:
        bundle_root = harness.home / "memory" / "attachments" / "bundles"
        pinned_file = next(bundle_root.glob("*/*"))
        pinned_file.write_bytes(b"corrupt after durable enqueue")
        return await original_drain()

    monkeypatch.setattr(harness.module, "drain", corrupt_bundle_then_drain)
    asyncio.run(
        harness.capture(
            text="Keep this caption even if the image breaks",
            payloads={"evidence.png": ("image/png", PNG_BYTES)},
        )
    )

    assert len(harness.provider.captures) == 1
    assert harness.provider.captures[0].text == (
        "Keep this caption even if the image breaks"
    )
    assert harness.provider.captures[0].attachments == ()
    assert harness.provider.observed_payloads == []
    assert harness.provider.call_log == []
    assert harness.memory_bundle_entries == ()


def test_rejected_attachment_preserves_caption_without_multimodal_provider_call(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-010."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(tmp_path)
    asyncio.run(
        harness.capture(
            text="Keep this caption without the rejected file",
            payloads={
                "excluded.svg": (
                    "image/svg+xml",
                    b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
                )
            },
        )
    )

    assert len(harness.provider.captures) == 1
    assert harness.provider.captures[0].text == (
        "Keep this caption without the rejected file"
    )
    assert harness.provider.captures[0].attachments == ()
    assert harness.provider.call_log == []
    assert harness.memory_bundle_entries == ()


def test_provider_attachment_rejection_retries_caption_as_text_only(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-011."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    harness = MemoryIMAttachmentScenarioHarness(tmp_path)
    harness.provider.add_results.extend(
        (
            AddRejected(
                request_id="unsupported-attachment",
                error_code="UNSUPPORTED_FORMAT",
                server_fault=False,
            ),
            AddAck(request_id="caption-accepted", status="accumulated"),
        )
    )

    asyncio.run(
        harness.capture(
            text="Keep this caption when the provider rejects the image",
            payloads={"unsupported.png": ("image/png", PNG_BYTES)},
        )
    )

    assert len(harness.provider.captures) == 2
    assert harness.provider.captures[0].attachments
    assert harness.provider.captures[1].text == (
        "Keep this caption when the provider rejects the image"
    )
    assert harness.provider.captures[1].attachments == ()
    assert harness.memory_bundle_entries == ()


def test_office_attachment_requires_soffice_and_preserves_valid_siblings(
    tmp_path,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-012."""

    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: False)
    without_home = tmp_path / "without-soffice"
    monkeypatch.setenv("AVIBE_HOME", str(without_home / "avibe-home"))
    without_soffice = MemoryIMAttachmentScenarioHarness(without_home)
    asyncio.run(
        without_soffice.capture(
            text="Keep the image when Office conversion is missing",
            payloads={
                "valid.png": ("image/png", PNG_BYTES),
                "report.xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _xlsx_bytes(),
                ),
            },
        )
    )

    assert [item.name for item in without_soffice.provider.captures[0].attachments] == [
        "valid.png"
    ]

    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    with_home = tmp_path / "with-soffice"
    monkeypatch.setenv("AVIBE_HOME", str(with_home / "avibe-home"))
    with_soffice = MemoryIMAttachmentScenarioHarness(with_home)
    asyncio.run(
        with_soffice.capture(
            text="Capture the spreadsheet when Office conversion is present",
            payloads={
                "valid.png": ("image/png", PNG_BYTES),
                "report.xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _xlsx_bytes(),
                ),
            },
        )
    )

    assert [item.name for item in with_soffice.provider.captures[0].attachments] == [
        "valid.png",
        "report.xlsx",
    ]
    assert [item.kind for item in with_soffice.provider.captures[0].attachments] == [
        "image",
        "doc",
    ]
    assert with_soffice.memory_bundle_entries == ()

    availability = [True, False]
    monkeypatch.setattr(
        "core.memory.modality.office_conversion_available",
        lambda: availability.pop(0),
    )
    stale_home = tmp_path / "soffice-removed-before-delivery"
    monkeypatch.setenv("AVIBE_HOME", str(stale_home / "avibe-home"))
    stale_soffice = MemoryIMAttachmentScenarioHarness(stale_home)
    asyncio.run(
        stale_soffice.capture(
            text="Keep the image after Office conversion disappears",
            payloads={
                "valid.png": ("image/png", PNG_BYTES),
                "report.xlsx": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _xlsx_bytes(),
                ),
            },
        )
    )

    assert availability == []
    assert [item.name for item in stale_soffice.provider.captures[0].attachments] == [
        "valid.png"
    ]
    assert stale_soffice.memory_bundle_entries == ()


class _ScenarioPrincipals:
    def principal_for_user_key(self, user_key: str) -> str:
        assert user_key.split(":", 1)[0] in {"discord", "telegram", "lark", "wechat"}
        return PRINCIPAL


class _ScenarioBindings:
    def is_enabled_user(self, platform: str, user_id: str) -> bool:
        return platform in {"discord", "telegram", "lark", "wechat"} and user_id == "U1"


def _platform_attachment_fact(platform: str, file: FileAttachment) -> bool:
    if platform == "discord":
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            edited_at=None,
            attachments=[object()],
            embeds=[],
            components=[],
            stickers=[],
            sticker_items=[],
            webhook_id=None,
            flags=SimpleNamespace(forwarded=False),
            message_snapshots=(),
            is_system=lambda: False,
        )
        return is_ordinary_discord_attachment(message, [file])
    if platform == "telegram":
        return is_ordinary_telegram_attachment(
            {
                "from": {"id": "U1", "is_bot": False},
                "document": {"file_id": "native-file", "file_name": file.name},
            },
            [file],
        )
    if platform == "lark":
        return is_ordinary_feishu_attachment(
            {
                "sender": {"sender_type": "user"},
                "message": {"message_type": "file"},
            },
            {"file_key": "native-file", "file_name": file.name},
            [file],
            shared_text=None,
        )
    return is_ordinary_wechat_attachment(
        {
            "from_user_id": "U1",
            "item_list": [
                {
                    "type": 4,
                    "file_item": {
                        "file_name": file.name,
                        "media": {"encrypt_query_param": "native-file"},
                    },
                }
            ],
        },
        [file],
    )


@pytest.mark.parametrize(
    ("scenario_id", "platform"),
    [
        pytest.param("MEMORY-IM-ATTACH-005", "discord", id="MEMORY-IM-ATTACH-005"),
        pytest.param("MEMORY-IM-ATTACH-006", "telegram", id="MEMORY-IM-ATTACH-006"),
        pytest.param("MEMORY-IM-ATTACH-007", "lark", id="MEMORY-IM-ATTACH-007"),
        pytest.param("MEMORY-IM-ATTACH-008", "wechat", id="MEMORY-IM-ATTACH-008"),
    ],
)
def test_enabled_platform_attachment_fact_reaches_memory_admission(
    scenario_id: str,
    platform: str,
) -> None:
    """Scenarios MEMORY-IM-ATTACH-005..008 cover each newly enabled adapter."""

    suffix_by_platform = {
        "discord": "005",
        "telegram": "006",
        "lark": "007",
        "wechat": "008",
    }
    assert scenario_id.endswith(suffix_by_platform[platform])
    file = FileAttachment(
        name="evidence.pdf",
        mimetype="application/pdf",
        url="native-file",
    )
    admission = CaptureAdmission(
        principals=_ScenarioPrincipals(),
        bindings=_ScenarioBindings(),
    )
    facts = InboundTurnFacts(
        platform=platform,
        user_id="U1",
        message_id="native-message",
        session_id="stable-session",
        files=[file],
        is_dm=True,
        is_ordinary_attachment=_platform_attachment_fact(platform, file),
        memory_enabled=True,
    )

    assert facts.is_ordinary_attachment is True
    assert admission.admits_attachment_turn(facts) is True
