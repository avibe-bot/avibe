"""Controller-owned notify and owner-confirm interactions for Computer Use scripts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import secrets
from typing import Any

from core.computer_script_sessions import (
    ComputerScriptSessionError,
    ComputerScriptSessionManager,
)
from core.delivery_target import routed_delivery_context
from core.message_output import MessageOutput
from core.services.dispatch import SOURCE_SCHEDULED
from modules.agents.question_ui import PendingQuestion, Question
from modules.im import MessageContext
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
# A request carries the message JSON-escaped (a control character becomes a
# six-byte ``\uXXXX`` escape) plus the connection token and keys, so the transport cap
# leaves room for the worst-case envelope around a message the limit admits.
MAX_INTERACTION_REQUEST_BYTES = 6 * MAX_MESSAGE_BYTES + 4 * 1024
# ``notify`` row events naming a script's own words to the owner. The message-type
# catalog lists them under ``agentRenderWhenEvents``, so the Web draws them as the
# Agent's message instead of a runtime status pill; IM delivery reads no metadata.
NOTIFY_EVENT = "computer_script_notify"
CONFIRM_EVENT = "computer_script_confirm"
_YES_ANSWERS = frozenset(
    {
        "y",
        "yes",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "ok",
        "okay",
        "是",
        "确认",
        "同意",
        "可以",
        "好",
        "好的",
    }
)
_ANSWER_TRAILING_PUNCTUATION = ".!。！"
_NO_ANSWERS = frozenset(
    {
        "n",
        "no",
        "cancel",
        "cancelled",
        "canceled",
        "decline",
        "declined",
        "拒绝",
        "取消",
        "否",
        "不",
    }
)


class ComputerScriptInteractionError(RuntimeError):
    """A script-facing notify or confirm interaction failed."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.params = dict(params or {})
        super().__init__(code)


@dataclass
class _PendingConfirm:
    token: str
    turn: Any
    context: MessageContext
    question: PendingQuestion
    future: asyncio.Future[bool]
    # Answerable only once the owner has been shown the prompt.
    armed: bool = False


class ComputerScriptInteractionManager:
    """Own script notifications and confirms on the launching Turn's surface."""

    def __init__(
        self,
        controller: Any,
        script_sessions: ComputerScriptSessionManager,
    ) -> None:
        self._controller = controller
        self._script_sessions = script_sessions
        self._pending: dict[str, _PendingConfirm] = {}
        self._pending_by_token: dict[str, str] = {}

    async def notify(self, token: str, message: str) -> dict[str, Any]:
        binding = self._admit_message(token, message)
        context = routed_delivery_context(binding.turn.context)
        message_id = await self._deliver(
            context,
            message,
            event=NOTIFY_EVENT,
            failure_code="computer_script_notify_failed",
        )
        return {
            "status": "confirmed",
            "effect": "confirmed",
            "route": "message_dispatcher",
            "delivery": {
                "mode": "foreground",
                "delivered_count": 1,
            },
            "message_id": message_id,
        }

    async def confirm(self, token: str, message: str) -> dict[str, Any]:
        binding = self._admit_message(token, message)
        # No person launched an unattended Turn, so nobody is there to answer.
        if getattr(binding.turn, "source", None) == SOURCE_SCHEDULED:
            raise ComputerScriptInteractionError("confirm_unattended")
        # A yes/no reply carries no prompt identity, so a Session may have only
        # one question open; otherwise one answer could approve another action.
        if any(
            record.question.base_session_id == binding.session_id
            for record in self._pending.values()
        ):
            raise ComputerScriptInteractionError("confirm_already_pending")

        context = routed_delivery_context(binding.turn.context)
        expected_approver_id = str(context.user_id or "").strip()
        if not expected_approver_id:
            raise ComputerScriptInteractionError(
                "confirm_approver_unavailable",
                status_code=409,
            )
        loop = asyncio.get_running_loop()
        pending = PendingQuestion(
            questions=[Question(question=message)],
            prompt_text=message,
            option_labels=[],
            base_session_id=binding.session_id,
            thread_id=context.thread_id,
            expected_approver_id=expected_approver_id,
            agent_data={"kind": "computer_script_confirm"},
        )
        record = _PendingConfirm(
            token=token,
            turn=binding.turn,
            context=context,
            question=pending,
            future=loop.create_future(),
        )
        confirm_id = secrets.token_urlsafe(18)
        # Registered before the prompt is sent so it holds the Session's single
        # slot and observes invalidation, but not answerable until delivered.
        self._pending[confirm_id] = record
        self._pending_by_token[token] = confirm_id
        try:
            await self._deliver(
                context,
                self._translate("computer.cli.confirm.prompt", message=message),
                event=CONFIRM_EVENT,
                failure_code="confirm_prompt_failed",
            )
            record.armed = True
            approved = await record.future
        finally:
            self._remove(confirm_id)
            if record.future.done() and not record.future.cancelled():
                # Consume an invalidation that raced a failed prompt send.
                record.future.exception()

        # The answer itself is consumed before Turn admission, so this notice is
        # the transcript's durable record of how the owner answered.
        await self._best_effort_notify(
            context,
            self._translate(
                "computer.cli.confirm.approved" if approved else "computer.cli.confirm.declined"
            ),
        )
        if not approved:
            raise ComputerScriptInteractionError("confirm_refused")
        return {
            "status": "confirmed",
            "effect": "confirmed",
            "route": "owner_confirm",
            "delivery": {
                "mode": "foreground",
                "delivered_count": 1,
            },
        }

    def match_answer(
        self,
        *,
        session_id: str,
        approver_id: str,
        message: str,
    ) -> tuple[str, bool] | None:
        """Return the pending confirm this message answers, without resolving it.

        Only the expected approver's yes/no reply to this Session's pending
        confirm matches. Anything else stays an ordinary message.
        """

        normalized_session_id = str(session_id or "").strip()
        normalized_approver_id = str(approver_id or "").strip()
        if not normalized_session_id or not normalized_approver_id:
            return None
        answer = _parse_answer(message)
        if answer is None:
            return None
        for confirm_id, record in self._pending.items():
            if (
                record.question.base_session_id == normalized_session_id
                and record.question.expected_approver_id == normalized_approver_id
                and record.armed
                and not record.future.done()
            ):
                return confirm_id, answer
        return None

    def resolve_answer(self, confirm_id: str, approved: bool) -> bool:
        """Resolve one matched confirm; ``False`` if it is no longer pending."""

        record = self._pending.get(confirm_id)
        self._remove(confirm_id)
        if record is None or record.future.done():
            return False
        record.future.set_result(bool(approved))
        return True

    def pending_for_token(self, token: str) -> PendingQuestion | None:
        confirm_id = self._pending_by_token.get(token)
        record = self._pending.get(confirm_id) if confirm_id else None
        return record.question if record is not None else None

    def invalidate_token(self, token: str, code: str) -> None:
        confirm_id = self._pending_by_token.get(token)
        if confirm_id is None:
            return
        record = self._pending.get(confirm_id)
        if record is None:
            self._pending_by_token.pop(token, None)
            return
        self._remove(confirm_id)
        if not record.future.done():
            record.future.set_exception(
                ComputerScriptInteractionError(code, status_code=409)
            )

    def invalidate_turn(self, turn: Any) -> None:
        for token, confirm_id in tuple(self._pending_by_token.items()):
            record = self._pending.get(confirm_id)
            if record is not None and record.turn is turn:
                self.invalidate_token(token, "script_turn_ended")

    def close_all(self) -> None:
        for token in tuple(self._pending_by_token):
            self.invalidate_token(token, "script_connection_invalid")

    def _admit_message(self, token: str, message: str) -> Any:
        if not isinstance(message, str) or not message.strip():
            raise ComputerScriptInteractionError(
                "invalid_computer_script_message",
                status_code=400,
            )
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ComputerScriptInteractionError(
                "computer_script_message_too_large",
                status_code=413,
                params={"limit_bytes": MAX_MESSAGE_BYTES},
            )
        try:
            return self._script_sessions.admit(token)
        except ComputerScriptSessionError:
            raise

    async def _deliver(
        self,
        context: MessageContext,
        message: str,
        *,
        event: str,
        failure_code: str,
    ) -> str:
        """Send one notify and return its id; a missing id means not delivered."""

        try:
            message_id = await self._controller.emit_agent_message(
                context,
                "notify",
                message,
                output=MessageOutput(metadata={"event": event}),
            )
        except Exception as exc:
            logger.exception("Computer script message delivery failed")
            raise ComputerScriptInteractionError(
                failure_code,
                status_code=503,
            ) from exc
        if message_id is None:
            # The dispatcher reports a failed IM send by returning no id.
            raise ComputerScriptInteractionError(failure_code, status_code=503)
        return message_id

    async def _best_effort_notify(self, context: MessageContext, message: str) -> None:
        try:
            await self._controller.emit_agent_message(context, "notify", message)
        except Exception:
            logger.debug("Could not send computer script confirm notice", exc_info=True)

    def _remove(self, confirm_id: str) -> None:
        record = self._pending.pop(confirm_id, None)
        if record is not None:
            self._pending_by_token.pop(record.token, None)

    def _translate(self, key: str, **kwargs: Any) -> str:
        translator = getattr(self._controller, "_t", None)
        if callable(translator):
            return str(translator(key, **kwargs))
        language = getattr(getattr(self._controller, "config", None), "language", "en")
        return str(i18n_t(key, language, **kwargs))


def _parse_answer(message: str) -> bool | None:
    normalized = " ".join(str(message or "").strip().lower().split())
    normalized = normalized.rstrip(_ANSWER_TRAILING_PUNCTUATION)
    if normalized in _YES_ANSWERS:
        return True
    if normalized in _NO_ANSWERS:
        return False
    return None
