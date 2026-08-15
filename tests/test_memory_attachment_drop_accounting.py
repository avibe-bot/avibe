"""Executable matrix for deterministic IM attachment drop accounting."""

from __future__ import annotations

import logging
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.handlers.message_handler import MessageHandler
from core.memory.admission import CaptureAdmission, InboundTurnFacts
from core.memory.attachments import AttachmentPinError
from core.memory.types import CaptureAttachment, CaptureRequest, OperationFailed
from modules.im.base import FileAttachment, MessageContext


CAPTION = "keep this caption"


@dataclass(frozen=True, slots=True)
class _DropOutcome:
    captured_text: str | None


_Injection = Callable[
    [pytest.MonkeyPatch, pytest.LogCaptureFixture, Path],
    Awaitable[_DropOutcome],
]


@dataclass(frozen=True, slots=True)
class _DropCase:
    name: str
    reason: str
    expected_caption: str | None
    inject: _Injection


class _Principals:
    @staticmethod
    def principal_for_user_key(_user_key: str) -> str:
        return "u-" + "1" * 32

    @staticmethod
    def project_for_workdir(_workdir: str) -> str:
        return "default"


class _Bindings:
    @staticmethod
    def is_enabled_user(_platform: str, _user_id: str) -> bool:
        return True


def _admission() -> CaptureAdmission:
    return CaptureAdmission(principals=_Principals(), bindings=_Bindings())


def _facts(**overrides: object) -> InboundTurnFacts:
    values: dict[str, object] = {
        "platform": "slack",
        "user_id": "U1",
        "message_id": "m-drop-matrix",
        "session_id": "session-drop-matrix",
        "workdir": "/tmp/project",
        "text": CAPTION,
        "files": [object()],
        "is_dm": True,
        "is_ordinary_text": False,
        "is_ordinary_attachment": True,
        "attachment_lease": object(),
        "attachment_capture_status": "ready",
        "attachment_config_generation": 1,
        "memory_enabled": True,
    }
    values.update(overrides)
    return InboundTurnFacts(**values)


async def _inject_selection_rejection(
    monkeypatch: pytest.MonkeyPatch,
    _caplog: pytest.LogCaptureFixture,
    _tmp_path: Path,
) -> _DropOutcome:
    monkeypatch.setattr(
        "core.memory.admission.select_memory_attachments",
        lambda _lease: SimpleNamespace(
            attachments=(),
            skipped=("unsupported_type",),
        ),
    )
    request = _admission().decide(_facts())
    assert isinstance(request, CaptureRequest)
    assert request.attachments == ()
    return _DropOutcome(captured_text=request.text)


async def _inject_partial_materialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    _caplog: pytest.LogCaptureFixture,
    _tmp_path: Path,
) -> _DropOutcome:
    attachment = CaptureAttachment(
        kind="pdf",
        name="receipt.pdf",
        uri="file:///leased/receipt.pdf",
        ext="pdf",
    )
    monkeypatch.setattr(
        "core.memory.admission.select_memory_attachments",
        lambda _lease: SimpleNamespace(attachments=(attachment,), skipped=()),
    )
    request = _admission().decide(
        _facts(
            files=[object(), object()],
            attachment_failures=("download_failed",),
        )
    )
    assert isinstance(request, CaptureRequest)
    assert request.attachments == (attachment,)
    return _DropOutcome(captured_text=request.text)


async def _inject_generation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    _caplog: pytest.LogCaptureFixture,
    _tmp_path: Path,
) -> _DropOutcome:
    from tests.test_memory_slice3 import _context, _controller

    controller = _controller()
    attachment = CaptureAttachment(
        kind="pdf",
        name="receipt.pdf",
        uri="file:///leased/receipt.pdf",
        ext="pdf",
    )
    monkeypatch.setattr(
        "core.memory.admission.select_memory_attachments",
        lambda _lease: SimpleNamespace(attachments=(attachment,), skipped=()),
    )

    async def attachment_capture_status() -> str:
        controller.memory_runtime.attachment_generation = 2
        return "ready"

    controller.memory_runtime.attachment_capture_status = attachment_capture_status
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_ordinary_attachment = True
    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )
    assert reservation is not None

    await controller.capture_user_memory(
        context,
        CAPTION,
        "stable-session",
        attachment_lease=object(),
        attachment_reservation=reservation,
    )
    request = controller.memory_module.accepted[0]
    assert request.attachments == ()
    return _DropOutcome(captured_text=request.text)


async def _inject_retain_failure(
    _monkeypatch: pytest.MonkeyPatch,
    _caplog: pytest.LogCaptureFixture,
    _tmp_path: Path,
) -> _DropOutcome:
    from tests.test_message_handler_typing import (
        _StubController,
        _StubSessionHandler,
        _capture_reservation,
    )

    controller = _StubController(
        platform="slack",
        ack_mode="reaction",
        typing_result=True,
    )
    controller.session_turns = types.SimpleNamespace(deliver=AsyncMock())
    reservation = _capture_reservation()
    controller.reserve_memory_attachment_capture = Mock(return_value=reservation)
    captured: list[str] = []

    async def capture_user_memory(
        _context,
        text,
        _session_id,
        *,
        attachment_reservation,
        attachment_config_generation,
        attachment_text_only,
    ) -> None:
        assert attachment_reservation is reservation
        assert attachment_config_generation == 1
        assert attachment_text_only is True
        captured.append(text)

    controller.capture_user_memory = capture_user_memory
    handler = MessageHandler(controller)
    handler.set_session_handler(_StubSessionHandler())
    handler._is_duplicate_human_delivery = Mock(return_value=False)
    handler._prepend_message_metadata = AsyncMock(return_value=CAPTION)
    lease = Mock()
    lease.retain.side_effect = RuntimeError("retain failed")
    attachment = FileAttachment(
        name="report.pdf",
        mimetype="application/pdf",
        local_path="/tmp/leased-report.pdf",
        size=10,
    )
    handler._materialize_file_attachments = AsyncMock(
        return_value=types.SimpleNamespace(
            attachments=(attachment,),
            display_errors=(),
            lease=lease,
        )
    )

    async def admit(**_kwargs) -> bool:
        lease.adopt()
        lease.release()
        return True

    handler._admit_human_delivery = AsyncMock(side_effect=admit)
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        message_id="m-retain-matrix",
        platform="slack",
        platform_specific={"is_dm": True},
        files=[FileAttachment("report.pdf", "application/pdf", url="private")],
        is_ordinary_attachment=True,
    )

    await handler.handle_user_message(context, CAPTION)
    await handler.drain_memory_capture_tasks()
    assert len(captured) == 1
    return _DropOutcome(captured_text=captured[0])


async def _inject_pin_failure(
    monkeypatch: pytest.MonkeyPatch,
    _caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    *,
    caption: str,
) -> _DropOutcome:
    from tests.test_memory_module import (
        _attachment_store,
        _module,
        _request,
        _source_attachment,
    )

    attachment_store = _attachment_store()
    module, store, _provider = _module(tmp_path, attachment_store=attachment_store)
    attachment = _source_attachment(
        "drop-matrix-pin.png",
        b"original bytes",
    )

    def fail_pin(*_args, **_kwargs):
        raise AttachmentPinError(
            "memory_store_unavailable",
            "leased source changed before pinning",
        )

    monkeypatch.setattr(attachment_store, "pin", fail_pin)
    request = replace(
        _request(source=f"pin-failure-{bool(caption)}", attachments=(attachment,)),
        text=caption,
        attachment_config_generation=7,
    )
    receipt = await module.capture(request, attachment_platform="slack")
    rows = store.list_queue_rows()
    if caption:
        assert len(rows) == 1
        assert rows[0].payload_attachments is None
        return _DropOutcome(captured_text=rows[0].payload_text)

    assert receipt == OperationFailed(error="memory_store_unavailable")
    assert rows == ()
    return _DropOutcome(captured_text=None)


async def _inject_pin_failure_with_caption(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> _DropOutcome:
    return await _inject_pin_failure(
        monkeypatch,
        caplog,
        tmp_path,
        caption=CAPTION,
    )


async def _inject_pin_failure_without_caption(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> _DropOutcome:
    return await _inject_pin_failure(
        monkeypatch,
        caplog,
        tmp_path,
        caption="",
    )


DROP_ACCOUNTING_MATRIX = (
    _DropCase(
        "selection-rejection",
        "unsupported_type",
        CAPTION,
        _inject_selection_rejection,
    ),
    _DropCase(
        "partial-materialization-failure",
        "download_failed",
        CAPTION,
        _inject_partial_materialization_failure,
    ),
    _DropCase(
        "lease-retention-failure",
        "lease_retain_failed",
        CAPTION,
        _inject_retain_failure,
    ),
    _DropCase(
        "configuration-generation-mismatch",
        "configuration_changed",
        CAPTION,
        _inject_generation_mismatch,
    ),
    _DropCase(
        "pin-failure-with-caption",
        "pin_failed",
        CAPTION,
        _inject_pin_failure_with_caption,
    ),
    _DropCase(
        "pin-failure-without-caption",
        "pin_failed",
        None,
        _inject_pin_failure_without_caption,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    DROP_ACCOUNTING_MATRIX,
    ids=lambda case: case.name,
)
async def test_memory_im_attachment_drop_accounting_matrix(
    case: _DropCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    caplog.set_level(logging.INFO, logger="core.memory.admission")
    outcome = await case.inject(monkeypatch, caplog, tmp_path)

    records = [
        record
        for record in caplog.records
        if record.message.startswith("memory_attachment_capture_skipped")
    ]
    assert len(records) == 1
    assert f"platform=slack count=1 reason={case.reason}" in records[0].getMessage()
    assert outcome.captured_text == case.expected_caption
