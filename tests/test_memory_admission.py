"""Capture admission decided without a Controller, IM client, or Workbench."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory.admission import CaptureAdmission, InboundTurnFacts
from core.memory.types import CaptureRequest, CaptureSkipped
from modules.im.message_facts import is_ordinary_workbench_text


PRINCIPAL = "u-" + ("1" * 32)


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
        "text": "ordinary text",
        "is_dm": True,
        "is_ordinary_text": True,
        "memory_enabled": True,
    }
    return InboundTurnFacts(**{**defaults, **overrides})


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
    assert request.provenance == "user_input"
    assert request.text == "ordinary text"
    assert request.attachments == ()
    assert request.occurred_at_ms > 0


def test_workbench_turn_uses_its_own_source_namespace() -> None:
    request = _admission().decide(_facts(platform="avibe", user_id="local", is_dm=False))

    assert isinstance(request, CaptureRequest)
    assert request.source_message_id == f"workbench:{PRINCIPAL}:native-1"


@pytest.mark.parametrize(
    "facts,reason",
    [
        (_facts(memory_enabled=False), "memory_disabled"),
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


def test_disabled_memory_is_answered_before_any_directory_lookup() -> None:
    principals = _Principals(raises=True)
    bindings = _Bindings(raises=True)

    decision = _admission(principals=principals, bindings=bindings).decide(
        _facts(memory_enabled=False)
    )

    assert decision == CaptureSkipped(reason="memory_disabled")


def test_workbench_attachment_only_turn_is_captured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    attachment = tmp_path / "attachments" / "avibe" / "receipt.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"pdf")

    request = _admission().decide(
        _facts(
            platform="avibe",
            user_id="local",
            is_dm=False,
            text="",
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


def test_workbench_submits_are_classified_beside_their_im_siblings() -> None:
    assert is_ordinary_workbench_text({"text": "hello"}, None) is True
    assert is_ordinary_workbench_text({"text": "hello"}, "msg-7") is False
    assert is_ordinary_workbench_text({"files": [{"name": "a.png"}]}, None) is False
    assert is_ordinary_workbench_text({"metadata": {"forwarded": True}}, None) is False
    assert is_ordinary_workbench_text({"metadata": {"forward_origin": "chat"}}, None) is False
    assert is_ordinary_workbench_text("not a payload", None) is False
