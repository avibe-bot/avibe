"""Decide whether one inbound turn may enter Memory.

Capture admission is a security boundary: it decides whose conversation
reaches the Memory store. It used to be spread over six `Controller` methods
carrying a nullable `is_ordinary_text` bool between layers, which meant the
rule could only be exercised through a `Controller`.

The contract in `docs/plans/memory-plugin-system.md` already says that
"platform adapters classify native events but do not own Memory business
logic". This module is the other half of that sentence: surfaces normalize
their native event into `InboundTurnFacts`, and `CaptureAdmission` alone turns
those facts into a `CaptureRequest` or a `CaptureSkipped`.

Every unresolved identity, platform, or settings fact fails closed. A lookup
that raises is never an authorization grant.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

from core.memory.attachments import workbench_capture_attachments
from core.memory.types import CaptureAttachment, CaptureRequest, CaptureSkipped


WORKBENCH_PLATFORM = "avibe"
IM_PLATFORMS = frozenset({"slack", "discord", "telegram", "lark", "feishu", "wechat"})
ADMISSIBLE_PLATFORMS = IM_PLATFORMS | {WORKBENCH_PLATFORM}


class PrincipalDirectory(Protocol):
    """Derives install-local opaque identifiers for Memory scope inputs."""

    def principal_for_user_key(self, user_key: str) -> str: ...

    def project_for_workdir(self, workdir: str) -> str: ...


class UserBindingDirectory(Protocol):
    """Answers whether an IM user is bound to this install and still enabled."""

    def is_enabled_user(self, platform: str, user_id: str) -> bool: ...


@dataclass(frozen=True)
class InboundTurnFacts:
    """One inbound turn as its surface classified it, with no platform types.

    The annotations state what a well-behaved surface supplies. They are not a
    guarantee: these values originate in untyped platform payloads, so
    `CaptureAdmission` re-checks every one of them before admitting a turn.
    """

    platform: str | None = None
    user_id: str | None = None
    message_id: str | None = None
    session_id: str | None = None
    workdir: str | None = None
    text: str | None = None
    # Kept opaque: only its emptiness and, for the Workbench, its conversion
    # through `workbench_capture_attachments` are Memory's business.
    files: object = None
    is_dm: bool = False
    is_ordinary_text: bool | None = None
    memory_enabled: bool = False


class CaptureAdmission:
    """The single authority on whether a turn may enter Memory."""

    def __init__(
        self,
        *,
        principals: PrincipalDirectory,
        bindings: UserBindingDirectory,
    ) -> None:
        self._principals = principals
        self._bindings = bindings

    @staticmethod
    def platform_of(facts: InboundTurnFacts) -> str | None:
        """Return the surface Memory recognizes, or None for anything else."""

        platform = facts.platform
        if not isinstance(platform, str) or platform not in ADMISSIBLE_PLATFORMS:
            return None
        return platform

    def principal_for(self, facts: InboundTurnFacts) -> str | None:
        """Derive this turn's principal, or None when identity is unresolved."""

        platform = self.platform_of(facts)
        user_id = _attributed_user_id(facts)
        if platform is None or user_id is None:
            return None
        try:
            return self._principals.principal_for_user_key(f"{platform}:{user_id}")
        except Exception:
            return None

    def project_for(self, facts: InboundTurnFacts) -> str | None:
        """Derive this turn's opaque project, or None when cwd is unresolved."""

        workdir = facts.workdir
        if not isinstance(workdir, str) or not workdir or workdir != workdir.strip():
            return None
        try:
            return self._principals.project_for_workdir(workdir)
        except Exception:
            return None

    def admits(self, facts: InboundTurnFacts) -> bool:
        """Admit an attributed human Workbench turn or a bound private IM turn."""

        platform = self.platform_of(facts)
        user_id = _attributed_user_id(facts)
        if platform is None or user_id is None:
            return False
        if platform == WORKBENCH_PLATFORM:
            return True
        if not _asserted_true(facts.is_dm):
            return False
        try:
            return bool(self._bindings.is_enabled_user(platform, user_id))
        except Exception:
            # Direct Memory reads and capture must never turn a settings read
            # failure into an implicit authorization grant.
            return False

    def decide(self, facts: InboundTurnFacts) -> CaptureRequest | CaptureSkipped:
        """Turn one classified turn into the capture it earns, or a skip."""

        if not _asserted_true(facts.memory_enabled):
            return CaptureSkipped(reason="memory_disabled")
        platform = self.platform_of(facts)
        if platform is None:
            return CaptureSkipped(reason="memory_access_denied")
        message_id = facts.message_id
        session_id = facts.session_id
        if not _nonempty_str(message_id) or not _nonempty_str(session_id):
            return CaptureSkipped(reason="memory_invalid_input")
        principal_id = self.principal_for(facts)
        if principal_id is None or not self.admits(facts):
            return CaptureSkipped(reason="memory_access_denied")
        project_id = self.project_for(facts)
        if project_id is None:
            return CaptureSkipped(reason="memory_invalid_input")
        workbench = platform == WORKBENCH_PLATFORM
        # Converted before the text check so an attachment-only turn is judged
        # on the uploads Memory can actually carry: a turn whose every upload
        # was filtered out would otherwise be enqueued with no text and no
        # attachment, giving the provider nothing to extract.
        attachments = workbench_capture_attachments(facts.files) if workbench else ()
        if not _is_ordinary_human_text(facts, attachments=attachments):
            return CaptureSkipped(reason="memory_invalid_input")
        if not workbench and bool(facts.files):
            # IM attachments are out of scope; no provider-side download
            # pipeline is exposed for them.
            return CaptureSkipped(reason="memory_invalid_input")

        source_prefix = "workbench" if workbench else f"im:{platform}"
        return CaptureRequest(
            source_message_id=f"{source_prefix}:{principal_id}:{message_id}",
            session_id=session_id,
            principal_id=principal_id,
            project_id=project_id,
            provenance="user_input",
            text=facts.text,
            occurred_at_ms=int(time.time() * 1000),
            attachments=attachments,
            app=platform,
        )


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _asserted_true(value: object) -> bool:
    """Treat anything but a literal ``True`` as an unstated fact.

    These flags are annotated as bools but originate in untyped platform
    payloads, where a JSON ``"false"`` is a truthy string. Truth-testing them
    would let a new surface widen capture by mis-typing a flag: a ``"false"``
    ``is_dm`` would carry a bound user's public-channel turns into Memory. The
    boundary normalizes here so no caller has to be trusted to do it.
    """

    return value is True


def _attributed_user_id(facts: InboundTurnFacts) -> str | None:
    """Return the user this turn is attributed to, or None for a fallback id.

    ``"workbench"`` is the unresolved local fallback identity, not a person, so
    it can never own a principal.
    """

    user_id = facts.user_id
    if not isinstance(user_id, str) or not user_id.strip() or user_id == "workbench":
        return None
    return user_id


def _is_ordinary_human_text(
    facts: InboundTurnFacts,
    *,
    attachments: tuple[CaptureAttachment, ...],
) -> bool:
    """Accept only surface-normalized ordinary human text.

    ``attachments`` is the already-converted capture attachment tuple, which is
    empty for every non-Workbench surface.
    """

    if not isinstance(facts.text, str):
        return False
    # A Workbench attachment-only turn carries no text but is still captured,
    # as long as one upload survived conversion.
    return _asserted_true(facts.is_ordinary_text) and (
        bool(facts.text.strip()) or bool(attachments)
    )
