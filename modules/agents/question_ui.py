"""Common question UI rendering and answer coordination for all agent backends.

This module provides shared infrastructure for:
- Rendering question prompts (text, buttons, modals)
- Waiting for user answers (event-based coordination)
- Timeout handling

Agent-specific logic (detecting question tools, submitting answers) should
remain in each agent's own handler.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

from modules.agents.base import AgentRequest
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)


def _translate(controller, key: str, **kwargs: Any) -> str:
    translator = getattr(controller, "_t", None)
    if callable(translator):
        return str(translator(key, **kwargs))

    config = getattr(controller, "config", None)
    lang = getattr(config, "language", "en")
    return str(i18n_t(key, lang, **kwargs))


def build_answer_summary(answers_payload: List[List[str]]) -> str:
    if not answers_payload:
        return ""

    if len(answers_payload) == 1:
        return ", ".join([value for value in answers_payload[0] if value])

    lines = []
    for idx, answers in enumerate(answers_payload, start=1):
        joined = ", ".join([value for value in answers if value])
        if joined:
            lines.append(f"Q{idx}: {joined}")
    return "\n".join(lines)


def build_answer_receipt_text(controller, answers_payload: List[List[str]]) -> str:
    summary = build_answer_summary(answers_payload)
    if not summary:
        return ""

    receipt_value = f"\n{summary}" if "\n" in summary else summary
    return _translate(controller, "message.quickReplyNote", text=receipt_value)


@dataclass
class QuestionOption:
    """A single option in a question."""

    label: str
    description: str = ""


@dataclass
class Question:
    """A single question with its options."""

    question: str
    header: str = ""
    options: List[QuestionOption] = field(default_factory=list)
    multiple: bool = False  # multiSelect / multiple


@dataclass
class PendingQuestion:
    """Common pending question state shared across agents."""

    # Question content
    questions: List[Question]
    prompt_text: str
    option_labels: List[str]  # First question's option labels for simple cases

    # Session info
    base_session_id: str
    thread_id: Optional[str] = None

    # UI state
    prompt_message_id: Optional[str] = None

    # Agent-specific data (opaque to this module)
    agent_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def is_multiple(self) -> bool:
        """Check if any question allows multiple selection."""
        return any(q.multiple for q in self.questions)


class QuestionUIHandler:
    """Shared question UI rendering and answer coordination.

    This class handles:
    - Rendering question prompts with buttons or modal triggers
    - Waiting for user answers via asyncio.Event

    Agent-specific logic should remain in agent handlers.
    """

    def __init__(
        self,
        controller,
        im_client,
        settings_manager,
        callback_prefix: str,
    ):
        """Initialize the question UI handler.

        Args:
            controller: Main controller for emitting messages
            im_client: IM client for sending messages
            settings_manager: Settings manager for state persistence
            callback_prefix: Prefix for callback data (e.g., "claude_question")
        """
        self._controller = controller
        self._im_client = im_client
        self._settings_manager = settings_manager
        self._callback_prefix = callback_prefix

        self._pending_questions: Dict[str, PendingQuestion] = {}
        self._question_answer_events: Dict[str, asyncio.Event] = {}
        # Track reactions added after question answer for cleanup
        # Maps base_session_id -> (context, message_id, emoji)
        self._answer_reactions: Dict[str, tuple] = {}

    # -------------------------------------------------------------------------
    # Pending state management
    # -------------------------------------------------------------------------

    def get_pending(self, base_session_id: str) -> Optional[PendingQuestion]:
        return self._pending_questions.get(base_session_id)

    def set_pending(self, base_session_id: str, pending: PendingQuestion) -> None:
        self._pending_questions[base_session_id] = pending

    def pop_pending(self, base_session_id: str) -> Optional[PendingQuestion]:
        return self._pending_questions.pop(base_session_id, None)

    async def clear(self, base_session_id: str) -> None:
        """Clear all state for a session, including removing any answer reaction."""
        self._pending_questions.pop(base_session_id, None)
        self._question_answer_events.pop(base_session_id, None)
        await self._remove_answer_reaction(base_session_id)

    async def _remove_answer_reaction(self, base_session_id: str) -> None:
        """Remove the 👀 reaction added after question answer."""
        reaction_info = self._answer_reactions.pop(base_session_id, None)
        if reaction_info and hasattr(self._im_client, "remove_reaction"):
            context, message_id, emoji = reaction_info
            try:
                await self._im_client.remove_reaction(context, message_id, emoji)
            except Exception as err:
                logger.debug(f"Failed to remove answer reaction: {err}")

    # -------------------------------------------------------------------------
    # Event coordination
    # -------------------------------------------------------------------------

    def _get_or_create_question_event(self, base_session_id: str) -> asyncio.Event:
        """Get or create an event for question answer coordination.

        Clears the event if it already exists (important for nested questions).
        """
        evt = self._question_answer_events.get(base_session_id)
        if evt is None:
            evt = asyncio.Event()
            self._question_answer_events[base_session_id] = evt
        else:
            evt.clear()
        return evt

    def _clear_question_event(self, base_session_id: str, evt: asyncio.Event) -> None:
        """Clear question event, but only if it's still the same object (avoid races)."""
        current = self._question_answer_events.get(base_session_id)
        if current is evt:
            self._question_answer_events.pop(base_session_id, None)

    def signal_answer_received(self, base_session_id: str) -> bool:
        """Signal that an answer has been received. Returns True if event was set."""
        evt = self._question_answer_events.get(base_session_id)
        if evt:
            evt.set()
            return True
        return False

    async def wait_for_answer(
        self,
        request: AgentRequest,
        pending: PendingQuestion,
        on_timeout: Optional[Callable[[AgentRequest, PendingQuestion], Coroutine]] = None,
    ) -> bool:
        """Wait for user to answer the question.

        Waits indefinitely — the user can answer whenever they come back.
        The asyncio task is suspended (no CPU cost) until the event fires.

        Args:
            request: The agent request
            pending: Pending question state
            on_timeout: Unused, kept for API compatibility

        Returns:
            True if answer was received, False if cancelled
        """
        evt = self._get_or_create_question_event(request.base_session_id)

        try:
            await evt.wait()
            logger.info("Answer received for %s, resuming", request.base_session_id)
            return True
        except asyncio.CancelledError:
            logger.info("Question wait cancelled for %s", request.base_session_id)
            raise
        finally:
            self._clear_question_event(request.base_session_id, evt)

    # -------------------------------------------------------------------------
    # UI Rendering
    # -------------------------------------------------------------------------

    def build_prompt_text(self, questions: List[Question]) -> str:
        """Build formatted prompt text from questions."""
        lines: list[str] = []
        for q_idx, q in enumerate(questions):
            title = (q.header or f"Question {q_idx + 1}").strip()
            prompt = (q.question or "").strip()

            lines.append(f"**{title}**")
            if prompt:
                lines.append(prompt)

            for idx, opt in enumerate(q.options, start=1):
                label = opt.label.strip()
                desc = opt.description.strip()
                if desc:
                    lines.append(f"{idx}. *{label}* - {desc}")
                else:
                    lines.append(f"{idx}. *{label}*")

            if q_idx < len(questions) - 1:
                lines.append("")

        return "\n".join(lines)

    async def render_question_ui(
        self,
        request: AgentRequest,
        pending: PendingQuestion,
    ) -> Optional[str]:
        """Render question UI (buttons or modal trigger).

        Returns the message ID of the prompt message, or None if failed.
        """
        text = pending.prompt_text
        questions = pending.questions
        option_labels = pending.option_labels
        question_count = pending.question_count
        is_multiple = pending.is_multiple

        # Multi-select or multi-question or too many options: show modal button
        if is_multiple or question_count != 1 or len(option_labels) > 10:
            return await self._render_modal_trigger(request, text, pending)

        # Single question + single select + <=10 options: show inline buttons
        if (
            question_count == 1
            and not is_multiple
            and len(option_labels) <= 10
            and hasattr(self._im_client, "send_message_with_buttons")
        ):
            return await self._render_inline_buttons(request, text, option_labels)

        # Fallback: text-only
        try:
            await self._im_client.send_message(
                request.context,
                text,
                parse_mode="markdown",
            )
        except Exception as err:
            logger.error(f"Failed to send question prompt: {err}", exc_info=True)
        return None

    async def _render_inline_buttons(
        self,
        request: AgentRequest,
        text: str,
        option_labels: List[str],
    ) -> Optional[str]:
        """Render question with inline buttons."""
        from modules.im import InlineButton, InlineKeyboard

        buttons: list[list[InlineButton]] = []
        row: list[InlineButton] = []
        for idx, label in enumerate(option_labels, start=1):
            callback = f"{self._callback_prefix}:choose:{idx}"
            row.append(InlineButton(text=label, callback_data=callback))
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        keyboard = InlineKeyboard(buttons=buttons)
        try:
            message_id = await self._im_client.send_message_with_buttons(
                request.context,
                text,
                keyboard,
                parse_mode="markdown",
            )
            return message_id
        except Exception as err:
            logger.warning(
                f"Failed to send buttons, falling back to text: {err}",
                exc_info=True,
            )
            # Fallback to text-only
            await self._im_client.send_message(request.context, text, parse_mode="markdown")
            return None

    async def _render_modal_trigger(
        self,
        request: AgentRequest,
        text: str,
        pending: PendingQuestion,
    ) -> Optional[str]:
        """Render question with modal trigger button."""
        from modules.im import InlineButton, InlineKeyboard

        modal_keyboard = InlineKeyboard(
            buttons=[[InlineButton(text="Choose…", callback_data=f"{self._callback_prefix}:open_modal")]]
        )

        try:
            message_id = await self._im_client.send_message_with_buttons(
                request.context,
                text,
                modal_keyboard,
                parse_mode="markdown",
            )
            return message_id
        except Exception as err:
            logger.warning(
                f"Failed to send modal button, falling back to text: {err}",
                exc_info=True,
            )
            # Fallback to text-only
            await self._im_client.send_message(request.context, text, parse_mode="markdown")
            return None

    async def open_modal(self, request: AgentRequest, pending: PendingQuestion) -> None:
        """Open the question modal."""
        trigger_id = None
        interaction = None
        platform = getattr(request.context, "platform", None) or (request.context.platform_specific or {}).get("platform")
        if request.context.platform_specific:
            trigger_id = request.context.platform_specific.get("trigger_id")
            interaction = request.context.platform_specific.get("interaction")
        if not trigger_id and not interaction and platform == "telegram":
            trigger_id = request.context
        if not trigger_id and not interaction:
            await self._im_client.send_message(
                request.context,
                "Modal UI is not available. Please reply with a custom message.",
            )
            return

        if not hasattr(self._im_client, "open_question_modal"):
            await self._im_client.send_message(
                request.context,
                "Modal UI is not available. Please reply with a custom message.",
            )
            return

        try:
            await self._im_client.open_question_modal(
                trigger_id=interaction or trigger_id,
                context=request.context,
                pending=pending,
                callback_prefix=self._callback_prefix,
            )
        except Exception as err:
            logger.error(f"Failed to open question modal: {err}", exc_info=True)
            await self._im_client.send_message(
                request.context,
                f"Failed to open modal: {err}. Please reply with a custom message.",
            )

    # -------------------------------------------------------------------------
    # Answer processing helpers
    # -------------------------------------------------------------------------

    async def update_prompt_after_answer(
        self,
        request: AgentRequest,
        pending: PendingQuestion,
    ) -> None:
        """Remove question controls without writing the answer into the prompt."""
        question_message_id = pending.prompt_message_id
        if not question_message_id or not hasattr(self._im_client, "remove_inline_keyboard"):
            return

        fallback_text = pending.prompt_text
        try:
            await self._im_client.remove_inline_keyboard(
                request.context,
                question_message_id,
                text=fallback_text,
                parse_mode="markdown",
            )
        except Exception as err:
            logger.debug(f"Failed to update question message: {err}")

    async def send_answer_receipt(
        self,
        request: AgentRequest,
        answers_payload: List[List[str]],
    ) -> None:
        """Send a visible follow-up message showing what the user selected."""
        receipt_text = build_answer_receipt_text(self._controller, answers_payload)
        if not receipt_text:
            return

        try:
            await self._im_client.send_message(request.context, receipt_text)
        except Exception as err:
            logger.debug(f"Failed to send answer receipt: {err}")

    async def add_answer_reaction(
        self,
        request: AgentRequest,
        pending: PendingQuestion,
    ) -> None:
        """Add 👀 reaction to question message to indicate answer received."""
        question_message_id = pending.prompt_message_id
        if not question_message_id or not hasattr(self._im_client, "add_reaction"):
            return

        try:
            emoji = "eyes"
            await self._im_client.add_reaction(request.context, question_message_id, emoji)
            # Save for cleanup when request completes
            self._answer_reactions[request.base_session_id] = (request.context, question_message_id, emoji)
        except Exception as err:
            logger.debug(f"Failed to add reaction to question message: {err}")
