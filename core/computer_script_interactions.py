"""Controller-owned notifications for Computer Use scripts."""

from __future__ import annotations

import logging
from typing import Any

from core.computer_script_sessions import (
    ComputerScriptSessionError,
    ComputerScriptSessionManager,
)
from core.delivery_target import routed_delivery_context
from core.message_output import MessageOutput
from modules.im import MessageContext

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
# A request carries the message JSON-escaped (a control character becomes a
# six-byte ``\uXXXX`` escape) plus the connection token and keys, so the transport cap
# leaves room for the worst-case envelope around a message the limit admits.
MAX_INTERACTION_REQUEST_BYTES = 6 * MAX_MESSAGE_BYTES + 4 * 1024
# ``notify`` row event naming a script's own words to the owner. The message-type
# catalog lists it under ``agentRenderWhenEvents``, so the Web draws it as the
# Agent's message instead of a runtime status pill; IM delivery reads no metadata.
NOTIFY_EVENT = "computer_script_notify"


class ComputerScriptInteractionError(RuntimeError):
    """A script-facing notification interaction failed."""

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


class ComputerScriptInteractionManager:
    """Own script notifications on the launching Turn's surface."""

    def __init__(
        self,
        controller: Any,
        script_sessions: ComputerScriptSessionManager,
    ) -> None:
        self._controller = controller
        self._script_sessions = script_sessions

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
