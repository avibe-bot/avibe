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
import logging
import time
from typing import Protocol

from core.memory.attachments import workbench_capture_attachments
from core.memory.im_attachments import (
    IM_ATTACHMENT_CAPTURE_PLATFORMS,
    select_memory_attachments,
)
from core.memory.types import CaptureAttachment, CaptureRequest, CaptureSkipped


logger = logging.getLogger(__name__)


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
    is_ordinary_attachment: bool | None = None
    attachment_lease: object = None
    attachment_capture_status: object = None
    attachment_config_generation: object = None
    attachment_failures: object = None
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
        """Return the default Memory project. User turns do not use workdir."""

        del facts
        from core.memory.project_ids import DEFAULT_MEMORY_PROJECT_ID

        return DEFAULT_MEMORY_PROJECT_ID

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

    def admits_attachment_turn(self, facts: InboundTurnFacts) -> bool:
        """Authorize retaining an already-materialized IM lease for Memory."""

        platform = self.platform_of(facts)
        return bool(
            _asserted_true(facts.memory_enabled)
            and platform in IM_ATTACHMENT_CAPTURE_PLATFORMS
            and _nonempty_str(facts.message_id)
            and _nonempty_str(facts.session_id)
            and _asserted_true(facts.is_ordinary_attachment)
            and _has_native_files(facts.files)
            and self.admits(facts)
        )

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
        if not isinstance(facts.text, str):
            return CaptureSkipped(reason="memory_invalid_input")
        workbench = platform == WORKBENCH_PLATFORM
        config_generation: int | None = None
        # Converted before the text check so an attachment-only turn is judged
        # on the uploads Memory can actually carry: a turn whose every upload
        # was filtered out would otherwise be enqueued with no text and no
        # attachment, giving the provider nothing to extract.
        if workbench:
            attachments = workbench_capture_attachments(facts.files)
            if not _is_ordinary_human_text(facts, attachments=attachments):
                return CaptureSkipped(reason="memory_invalid_input")
        else:
            native_files = _native_file_count(facts.files)
            if native_files is None:
                return CaptureSkipped(reason="memory_invalid_input")
            attachments: tuple[CaptureAttachment, ...] = ()
            if native_files:
                if not self.admits_attachment_turn(facts):
                    return CaptureSkipped(reason="memory_invalid_input")
                status = facts.attachment_capture_status
                config_generation = _attachment_config_generation(
                    facts.attachment_config_generation
                )
                if status == "ready" and config_generation is not None:
                    try:
                        selection = select_memory_attachments(facts.attachment_lease)  # type: ignore[arg-type]
                    except (OSError, ValueError):
                        _log_attachment_skip(platform, native_files, "unavailable")
                    else:
                        attachments = selection.attachments
                        skipped = (
                            *_materialization_failures(
                                facts.attachment_failures,
                                maximum=native_files,
                            ),
                            *selection.skipped,
                        )[:native_files]
                        if skipped:
                            reasons = set(skipped)
                            reason = (
                                skipped[0]
                                if len(reasons) == 1
                                else "mixed_rejections"
                            )
                            _log_attachment_skip(
                                platform,
                                len(skipped),
                                reason,
                            )
                else:
                    reason = "not_configured" if status == "not_configured" else "unavailable"
                    _log_attachment_skip(platform, native_files, reason)
                if not facts.text.strip() and not attachments:
                    return CaptureSkipped(reason="memory_invalid_input")
            elif not _asserted_true(facts.is_ordinary_text) or not facts.text.strip():
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
            attachment_config_generation=config_generation,
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


def _attachment_config_generation(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _materialization_failures(
    value: object,
    *,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    closed = {"download_failed", "file_too_large"}
    return tuple(reason for reason in value if reason in closed)[:maximum]


def _native_file_count(value: object) -> int | None:
    if value is None:
        return 0
    if not isinstance(value, (list, tuple)):
        return None
    return len(value)


def _has_native_files(value: object) -> bool:
    count = _native_file_count(value)
    return count is not None and count > 0


def _log_attachment_skip(platform: str, count: int, reason: str) -> None:
    logger.info(
        "memory_attachment_capture_skipped platform=%s count=%d reason=%s",
        platform,
        count,
        reason,
    )


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
