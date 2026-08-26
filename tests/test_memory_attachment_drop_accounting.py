"""Result-driven telemetry for IM attachment capture attempts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from avibe_memory.attachments import AttachmentPinError
from avibe_memory.types import CaptureAttachment
from modules.im.base import FileAttachment


def _attachment_context(message_id: str, count: int):
    from tests.test_memory_slice3 import _context

    context = _context("slack", ordinary=False)
    context.message_id = message_id
    context.files = [
        FileAttachment(
            name=f"attachment-{index}.pdf",
            mimetype="application/pdf",
            url=f"https://files.slack.test/private/{index}",
        )
        for index in range(count)
    ]
    context.is_original_human_attachment = True
    return context


def _assert_single_conservation_record(
    caplog: pytest.LogCaptureFixture,
    *,
    total: int,
    captured: int,
) -> None:
    records = [
        record.getMessage()
        for record in caplog.records
        if record.message.startswith("memory_attachment_capture ")
    ]
    assert len(records) == 1
    fields = dict(part.split("=", 1) for part in records[0].split()[1:])
    assert set(fields) == {"platform", "total", "captured", "dropped"}
    assert fields["platform"] == "slack"
    assert int(fields["total"]) == total
    assert int(fields["captured"]) == captured
    assert int(fields["dropped"]) == total - captured


@pytest.mark.asyncio
async def test_attachment_capture_telemetry_uses_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    from tests.test_memory_module import _module
    from tests.test_memory_slice3 import _Runtime, _controller

    caplog.set_level("INFO", logger="avibe_memory.admission")

    # A partial download followed by reservation failure reaches the terminal
    # text-only result with no captured attachments.
    controller = _controller()
    controller.memory_module.reserve_capture_admission = Mock(
        side_effect=RuntimeError("reservation unavailable")
    )
    context = _attachment_context("reservation-failure", 2)
    assert (
        controller.reserve_memory_attachment_capture(context, "stable-session")
        is None
    )
    await controller.capture_user_memory(
        context,
        "keep this caption",
        "stable-session",
    )
    _assert_single_conservation_record(caplog, total=2, captured=0)

    # The stale state produced when /new wins during materialization follows the
    # same no-reservation path and still emits exactly one terminal record.
    caplog.clear()
    controller = _controller()
    context = _attachment_context("stale-after-reset", 1)
    await controller.capture_user_memory(
        context,
        "keep this caption",
        "stable-session",
    )
    _assert_single_conservation_record(caplog, total=1, captured=0)

    # One materialized survivor reaches pinning, but the terminal text fallback
    # reports zero because no attachment bundle was actually enqueued.
    caplog.clear()
    module, _store, provider = _module(tmp_path)
    attachment_store = module._attachment_store
    controller = _controller()
    controller.memory_module = module
    controller.memory_runtime = _Runtime(module)
    source = tmp_path / "attachments" / "avibe" / "survivor.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"survivor")
    survivor = CaptureAttachment(
        kind="pdf",
        name="survivor.pdf",
        uri=source.as_uri(),
        ext="pdf",
    )
    monkeypatch.setattr(
        "avibe_memory.admission.select_memory_attachments",
        lambda _lease: SimpleNamespace(attachments=(survivor,), skipped=()),
    )

    def fail_pin(*_args, **_kwargs):
        raise AttachmentPinError(
            "memory_store_unavailable",
            "leased source changed before pinning",
        )

    monkeypatch.setattr(attachment_store, "pin", fail_pin)
    context = _attachment_context("pin-failure", 2)
    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )
    assert reservation is not None
    await controller.capture_user_memory(
        context,
        "keep this caption",
        "stable-session",
        attachment_lease=object(),
        attachment_reservation=reservation,
    )
    await module.wait_writer_idle_for_tests()
    assert len(provider.captures) == 1
    assert provider.captures[0].text == "keep this caption"
    assert provider.captures[0].attachments == ()
    await module.close_writer()
    _assert_single_conservation_record(caplog, total=2, captured=0)
