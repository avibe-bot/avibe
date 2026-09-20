"""Capture admission decided without a Controller, IM client, or Workbench."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from avibe_memory.admission import (
    CaptureAdmission,
    InboundTurnFacts,
)
from avibe_memory.attachments import workbench_capture_attachments
from avibe_memory.im_attachments import select_memory_attachments
from avibe_memory.types import CaptureAttachment, CaptureRequest, CaptureSkipped
from core.handlers.inbound_attachments import InboundAttachmentMaterializer
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext
from modules.im.message_facts import is_original_human_workbench_text
from storage.message_deliveries import (
    LEGACY_MEMORY_CLI_ADMITTED_METADATA as MEMORY_CLI_ADMITTED_METADATA,
    LEGACY_MEMORY_ORDINARY_TEXT_METADATA as MEMORY_ORDINARY_TEXT_METADATA,
    LEGACY_MEMORY_USER_ID_METADATA as MEMORY_USER_ID_METADATA,
    legacy_is_cli_admitted as is_cli_admitted,
    legacy_is_ordinary_text as is_ordinary_text,
    legacy_memory_merge_identity as merge_identity,
)


PRINCIPAL = "u-" + ("1" * 32)
PROJECT = "p-" + ("2" * 32)


class _Principals:
    """Derive a stable principal, or refuse the way an unopenable store does."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.keys: list[str] = []

    def principal_for_user_key(self, user_key: str) -> str:
        if self.raises:
            raise RuntimeError("memory store unavailable")
        self.keys.append(user_key)
        return PRINCIPAL

class _Bindings:
    def __init__(self, *, enabled: bool = True, raises: bool = False) -> None:
        self.enabled = enabled
        self.raises = raises

    def is_enabled_user(self, platform: str, user_id: str) -> bool:
        if self.raises:
            raise RuntimeError("settings store unreadable")
        return self.enabled


def _admission(*, principals=None, bindings=None) -> CaptureAdmission:
    return CaptureAdmission(
        principals=principals or _Principals(),
        bindings=bindings or _Bindings(),
    )


def _facts(**overrides) -> InboundTurnFacts:
    defaults = {
        "platform": "slack",
        "user_id": "user-1",
        "message_id": "native-1",
        "session_id": "stable-session",
        "workdir": "/tmp/project",
        "text": "ordinary text",
        "is_dm": True,
        "is_ordinary_text": True,
    }
    return InboundTurnFacts(**{**defaults, **overrides})


@pytest.mark.parametrize("name", [None, "", "小王 Élodie 🌱"])
def test_sender_name_is_optional_metadata_not_principal_input(name) -> None:
    principals = _Principals()
    admission = _admission(principals=principals)
    captured = admission.decide(_facts(sender_name=name))
    assert captured.sender_name == name
    assert captured.principal_id == PRINCIPAL
    assert captured.text == "ordinary text"
    assert principals.keys == ["slack:user-1"]


class _PdfDownloader:
    async def download_file_to_path(
        self,
        _file_info,
        _target_path,
        *,
        target_fd=None,
        **_kwargs,
    ) -> FileDownloadResult:
        assert target_fd is not None
        os.write(target_fd, b"%PDF-1.7\nslack attachment")
        return FileDownloadResult(True)


def _slack_pdf_lease(tmp_path: Path):
    context = MessageContext(
        user_id="user-1",
        channel_id="dm-1",
        platform="slack",
        files=[
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
                size=24,
            )
        ],
    )
    return asyncio.run(
        InboundAttachmentMaterializer(
            effective_home=tmp_path,
            attachments_root=tmp_path / "downloads",
        ).materialize(context, _PdfDownloader())
    )


@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu", "lark", "wechat"])
def test_every_enabled_bound_dm_user_is_admitted(platform: str) -> None:
    admission = _admission()

    assert admission.admits(_facts(platform=platform)) is True
    assert admission.admits(_facts(platform=platform, is_dm=False)) is False


def test_workbench_turn_with_resolved_identity_needs_no_binding() -> None:
    bindings = _Bindings(raises=True)
    admission = _admission(bindings=bindings)

    assert admission.admits(_facts(platform="avibe", user_id="local", is_dm=False)) is True


@pytest.mark.parametrize(
    "facts",
    [
        # The unresolved local fallback identity is not a person.
        _facts(platform="avibe", user_id="workbench"),
        _facts(user_id=""),
        _facts(user_id="   "),
        _facts(user_id=None),
        # A surface Memory does not recognize, including automation sources
        # such as a scheduled task or `vibe agent run` reusing a session.
        _facts(platform="harness"),
        _facts(platform="cli"),
        _facts(platform=None),
    ],
)
def test_unresolved_identity_or_platform_fails_closed(facts: InboundTurnFacts) -> None:
    admission = _admission()

    assert admission.admits(facts) is False
    assert admission.principal_for(facts) is None
    assert isinstance(admission.decide(facts), CaptureSkipped)


def test_disabled_or_unbound_im_user_is_not_admitted() -> None:
    assert _admission(bindings=_Bindings(enabled=False)).admits(_facts()) is False


def test_unreadable_settings_store_is_not_an_authorization_grant() -> None:
    assert _admission(bindings=_Bindings(raises=True)).admits(_facts()) is False


def test_unavailable_principal_directory_yields_no_principal() -> None:
    admission = _admission(principals=_Principals(raises=True))

    assert admission.principal_for(_facts()) is None
    assert admission.decide(_facts()) == CaptureSkipped(reason="memory_access_denied")


def test_principal_is_derived_from_the_platform_scoped_user_key() -> None:
    principals = _Principals()

    assert _admission(principals=principals).principal_for(_facts()) == PRINCIPAL
    assert principals.keys == ["slack:user-1"]


def test_admitted_im_turn_becomes_a_namespaced_capture_request() -> None:
    request = _admission().decide(_facts(platform="telegram"))

    assert isinstance(request, CaptureRequest)
    assert request.source_message_id == f"im:telegram:{PRINCIPAL}:native-1"
    assert request.session_id == "stable-session"
    assert request.principal_id == PRINCIPAL
    assert request.project_id == "default"
    assert request.provenance == "user_input"
    assert request.text == "ordinary text"
    assert request.attachments == ()
    assert request.occurred_at_ms > 0


def test_workbench_turn_uses_its_own_source_namespace() -> None:
    request = _admission().decide(_facts(platform="avibe", user_id="local", is_dm=False))

    assert isinstance(request, CaptureRequest)
    assert request.source_message_id == f"workbench:{PRINCIPAL}:native-1"


def test_user_turns_capture_into_default_without_a_workdir() -> None:
    """Scenario: MEMORY-SEARCH-001"""
    request = _admission().decide(_facts(workdir=None))

    assert isinstance(request, CaptureRequest)
    assert request.project_id == "default"


@pytest.mark.parametrize(
    "facts,reason",
    [
        (_facts(is_dm=False), "memory_access_denied"),
        (_facts(message_id=None), "memory_invalid_input"),
        (_facts(message_id=""), "memory_invalid_input"),
        (_facts(session_id=None), "memory_invalid_input"),
        (_facts(session_id=""), "memory_invalid_input"),
        (_facts(is_ordinary_text=False), "memory_invalid_input"),
        (_facts(is_ordinary_text=None), "memory_invalid_input"),
        (_facts(text=""), "memory_invalid_input"),
        (_facts(text="   "), "memory_invalid_input"),
        (_facts(text=None), "memory_invalid_input"),
        (_facts(files=[object()]), "memory_invalid_input"),
    ],
)
def test_ineligible_turns_skip_with_their_reason(facts: InboundTurnFacts, reason: str) -> None:
    assert _admission().decide(facts) == CaptureSkipped(reason=reason)


@pytest.mark.parametrize("malformed", ["false", "true", "0", "1", 0, 1, None, "", []])
def test_a_non_bool_is_dm_never_admits_a_public_channel_turn(malformed: object) -> None:
    """`is_dm` arrives untyped, and `"false"` is truthy: only `True` may admit."""

    admission = _admission()
    facts = _facts(is_dm=malformed)

    assert admission.admits(facts) is False
    assert admission.decide(facts) == CaptureSkipped(reason="memory_access_denied")


@pytest.mark.parametrize("malformed", ["true", "1", 1, "ordinary"])
def test_a_non_bool_is_ordinary_text_is_not_a_classification(malformed: object) -> None:
    """Only the surface's literal `True` counts as "this is ordinary human text"."""

    assert _admission().decide(_facts(is_ordinary_text=malformed)) == CaptureSkipped(
        reason="memory_invalid_input"
    )


def test_a_literal_true_still_admits_after_strict_normalization() -> None:
    """The strict reading must not reject the well-behaved surfaces."""

    assert isinstance(_admission().decide(_facts(is_dm=True)), CaptureRequest)


def test_bound_slack_attachment_turn_selects_only_the_materialized_lease(
    tmp_path: Path,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-001."""

    batch = _slack_pdf_lease(tmp_path)
    try:
        request = _admission().decide(
            _facts(
                text="remember this receipt",
                files=[object()],
                is_ordinary_text=False,
                is_ordinary_attachment=True,
                attachment_lease=batch.lease,
                attachment_capture_status="ready",
                attachment_config_generation=1,
                attachment_selection=select_memory_attachments(batch.lease),
            )
        )
    finally:
        batch.lease.release()

    assert isinstance(request, CaptureRequest)
    assert request.text == "remember this receipt"
    assert [(item.name, item.kind, item.ext) for item in request.attachments] == [
        ("receipt.pdf", "pdf", "pdf")
    ]


def test_missing_multimodal_keeps_mixed_text_without_an_attachment() -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    request = _admission().decide(
        _facts(
            text="remember the caption",
            files=[object()],
            is_ordinary_text=False,
            is_ordinary_attachment=True,
            attachment_capture_status="not_configured",
        )
    )

    assert isinstance(request, CaptureRequest)
    assert request.text == "remember the caption"
    assert request.attachments == ()


def test_missing_multimodal_skips_attachment_only_turn() -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    assert _admission().decide(
        _facts(
            text="",
            files=[object()],
            is_ordinary_text=False,
            is_ordinary_attachment=True,
            attachment_capture_status="not_configured",
        )
    ) == CaptureSkipped(reason="memory_invalid_input")


def test_im_attachment_only_turn_with_every_upload_filtered_is_not_captured() -> None:
    decision = _admission().decide(
        _facts(
            text="",
            files=[object()],
            is_ordinary_text=False,
            is_ordinary_attachment=True,
            attachment_capture_status="ready",
            attachment_config_generation=1,
            attachment_selection=SimpleNamespace(
                attachments=(),
                skipped=("unsupported_type",),
            ),
        )
    )

    assert decision == CaptureSkipped(reason="memory_invalid_input")


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_dm": False},
        {"is_ordinary_attachment": False},
        {"is_ordinary_attachment": None},
        {"platform": "email", "is_ordinary_attachment": True},
    ],
)
def test_denied_attachment_turn_never_reads_the_lease(
    overrides: dict[str, object],
) -> None:
    """Scenario: MEMORY-IM-ATTACH-002."""

    class ForbiddenSelection:
        @property
        def attachments(self):
            pytest.fail("denied turn reached Memory attachment selection")

    facts = _facts(
        **{
            "files": [object()],
            "is_ordinary_text": False,
            "is_ordinary_attachment": True,
            "attachment_capture_status": "ready",
            "attachment_selection": ForbiddenSelection(),
            **overrides,
        }
    )

    assert isinstance(_admission().decide(facts), CaptureSkipped)


def test_not_configured_attachment_turn_keeps_safe_caption() -> None:
    secret_name = "quarterly-secret.pdf"
    secret_url = "https://files.slack.test/token-bearing-url"
    native_file = SimpleNamespace(name=secret_name, url=secret_url)

    request = _admission().decide(
        _facts(
            text="safe caption",
            files=[native_file],
            is_ordinary_text=False,
            is_ordinary_attachment=True,
            attachment_capture_status="not_configured",
        )
    )

    assert isinstance(request, CaptureRequest)
    assert request.text == "safe caption"
    assert request.attachments == ()


def test_mixed_attachment_rejections_keep_caption() -> None:
    request = _admission().decide(
        _facts(
            text="safe caption",
            files=[object(), object()],
            is_ordinary_text=False,
            is_ordinary_attachment=True,
            attachment_lease=object(),
            attachment_capture_status="ready",
            attachment_config_generation=1,
            attachment_selection=SimpleNamespace(
                attachments=(),
                skipped=("file_too_large", "unsupported_type"),
            ),
        )
    )

    assert isinstance(request, CaptureRequest)
    assert request.attachments == ()


def test_workbench_attachment_only_turn_is_captured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    attachment = tmp_path / "attachments" / "avibe" / "receipt.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"pdf")
    ordinary = is_original_human_workbench_text(
        {"content": {"attachments": [{"token": "receipt"}]}},
        None,
    )

    request = _admission().decide(
        _facts(
            platform="avibe",
            user_id="local",
            is_dm=False,
            text="",
            is_ordinary_text=ordinary,
            files=[
                SimpleNamespace(
                    name="receipt.pdf",
                    mimetype="application/pdf",
                    local_path=str(attachment),
                )
            ],
        )
    )

    assert isinstance(request, CaptureRequest)
    assert request.text == ""
    assert request.attachments[0].kind == "pdf"
    assert request.attachments[0].uri == attachment.as_uri()


def _uploads_dir(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    uploads = tmp_path / "attachments" / "avibe"
    uploads.mkdir(parents=True)
    return uploads


def _upload(uploads: Path, filename: str, mimetype: str) -> SimpleNamespace:
    path = uploads / filename
    path.write_bytes(b"payload")
    return SimpleNamespace(name=filename, mimetype=mimetype, local_path=str(path))


def test_only_extensions_the_provider_can_parse_become_attachments(monkeypatch, tmp_path: Path) -> None:
    """The runtime answers an unparseable extension with a permanent rejection.

    Forwarding one would cost the whole install its capture throughput, so the
    boundary drops it rather than learning the limit from the provider.
    """

    uploads = _uploads_dir(monkeypatch, tmp_path)

    converted = workbench_capture_attachments(
        [
            _upload(uploads, "notes.txt", "text/plain"),
            _upload(uploads, "export.json", "application/json"),
            _upload(uploads, "receipt.pdf", "application/pdf"),
            # Formats that need an external converter never enter Memory.
            _upload(uploads, "report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            # In the runtime's IMAGE set, but needs the absent cairosvg support.
            _upload(uploads, "logo.svg", "image/svg+xml"),
            _upload(uploads, "diagram.png", "image/png"),
        ]
    )

    assert [attachment.name for attachment in converted] == ["notes.txt", "receipt.pdf", "diagram.png"]
    assert [attachment.ext for attachment in converted] == ["txt", "pdf", "png"]
    assert [attachment.kind for attachment in converted] == ["doc", "pdf", "image"]
def test_workbench_turn_of_only_unparseable_uploads_is_not_captured(monkeypatch, tmp_path: Path) -> None:
    uploads = _uploads_dir(monkeypatch, tmp_path)
    ordinary = is_original_human_workbench_text(
        {"content": {"attachments": [{"token": "export"}]}},
        None,
    )

    decision = _admission().decide(
        _facts(
            platform="avibe",
            user_id="local",
            is_dm=False,
            text="",
            is_ordinary_text=ordinary,
            files=[_upload(uploads, "export.json", "application/json")],
        )
    )

    # Capturing it would enqueue a row with neither text nor an attachment.
    assert decision == CaptureSkipped(reason="memory_invalid_input")


def test_unparseable_upload_does_not_cost_its_turn_the_text(monkeypatch, tmp_path: Path) -> None:
    uploads = _uploads_dir(monkeypatch, tmp_path)

    request = _admission().decide(
        _facts(
            platform="avibe",
            user_id="local",
            is_dm=False,
            text="see the attached export",
            files=[_upload(uploads, "export.json", "application/json")],
        )
    )

    assert isinstance(request, CaptureRequest)
    assert request.text == "see the attached export"
    assert request.attachments == ()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (None, (None, False, False)),
        ("not-a-dict", (None, False, False)),
        (1, (None, False, False)),
        (["_memory_user_id", "local"], (None, False, False)),
        ({}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: None}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: ""}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: "   "}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: 1}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: True}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: ["local"]}, (None, False, False)),
        ({MEMORY_USER_ID_METADATA: "local"}, ("local", False, False)),
        ({MEMORY_USER_ID_METADATA: "  local  "}, ("local", False, False)),
        (
            {
                MEMORY_USER_ID_METADATA: "local",
                MEMORY_ORDINARY_TEXT_METADATA: True,
                MEMORY_CLI_ADMITTED_METADATA: True,
            },
            ("local", True, True),
        ),
        (
            {
                MEMORY_ORDINARY_TEXT_METADATA: "true",
                MEMORY_CLI_ADMITTED_METADATA: 1,
            },
            (None, False, False),
        ),
        (
            {
                MEMORY_ORDINARY_TEXT_METADATA: False,
                MEMORY_CLI_ADMITTED_METADATA: False,
            },
            (None, False, False),
        ),
    ],
)
def test_merge_identity_normalizes_unusable_metadata(
    metadata: object,
    expected: tuple[str | None, bool, bool],
) -> None:
    """Queue merge identity matches today's strip/None/`is True` semantics."""

    assert merge_identity(metadata) == expected
    assert is_ordinary_text(metadata) is expected[1]
    assert is_cli_admitted(metadata) is expected[2]


def test_delivery_store_uses_only_legacy_translation_without_capture_admission() -> None:
    """Storage can translate released rows without loading Memory runtime code."""

    script = """
import sys
from storage import message_deliveries
assert message_deliveries.legacy_message_kind({"_memory_ordinary_text": True}) == "original"
assert not any(name == "avibe_memory" or name.startswith("avibe_memory.") for name in sys.modules)
assert "avibe_memory.admission" not in sys.modules
assert "avibe_memory.im_attachments" not in sys.modules
assert "avibe_memory.module" not in sys.modules
assert "avibe_memory.store" not in sys.modules
assert "avibe_memory.types" not in sys.modules
assert "core.handlers" not in sys.modules
assert "httpx" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_session_turns_import_keeps_memory_and_handlers_behind_their_boundaries() -> None:
    """Scenario: MEMORY-INDEP-004.

    The delivery FSM must not load optional Memory or handler implementations.
    """

    script = """
import sys
import core.session_turns
assert "avibe_memory.admission" not in sys.modules
assert "avibe_memory.attachments" not in sys.modules
assert "avibe_memory.im_attachments" not in sys.modules
assert "avibe_memory.module" not in sys.modules
assert "avibe_memory.runtime" not in sys.modules
assert "avibe_memory.types" not in sys.modules
assert "core.handlers" not in sys.modules
assert "core.handlers.message_handler" not in sys.modules
assert "aiohttp" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_workbench_submits_are_classified_beside_their_im_siblings() -> None:
    assert is_original_human_workbench_text({"text": "hello"}, None) is True
    assert is_original_human_workbench_text({"text": "hello"}, "msg-7") is False
    assert (
        is_original_human_workbench_text(
            {"content": {"attachments": [{"token": "upload-1"}]}},
            None,
        )
        is True
    )
    assert is_original_human_workbench_text({"files": [{"name": "a.png"}]}, None) is True
    assert is_original_human_workbench_text({"metadata": {"forwarded": True}}, None) is False
    assert is_original_human_workbench_text({"metadata": {"forward_origin": "chat"}}, None) is False
    assert is_original_human_workbench_text("not a payload", None) is False
