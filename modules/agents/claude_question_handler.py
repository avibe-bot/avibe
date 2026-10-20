"""Question/answer coordination for the Claude Code agent.

This module handles:
- Detecting AskUserQuestion tool calls from Claude Code
- Presenting question UI to the user via the common QuestionUIHandler
- Submitting answers back to Claude Code via tool result
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from core.agent_output import Notice, emit_output
from modules.agents.base import AgentRequest
from modules.agents.question_ui import (
    Question,
    QuestionOption,
    QuestionUIHandler,
    PendingQuestion,
)

if TYPE_CHECKING:
    from modules.agents.claude_agent import ClaudeAgent

logger = logging.getLogger(__name__)


class ClaudeQuestionHandler:
    """Handles AskUserQuestion tool calls for Claude Code agent.

    This class:
    - Detects AskUserQuestion ToolUseBlock
    - Converts Claude's question format to the common format
    - Uses QuestionUIHandler for UI rendering and answer waiting
    - Submits answers back as tool results
    """

    # Callback prefix for routing
    CALLBACK_PREFIX = "claude_question"

    def __init__(self, agent: "ClaudeAgent", controller, im_client, settings_manager):
        self._agent = agent
        self._controller = controller
        self._im_client = im_client
        self._settings_manager = settings_manager

        # The shared UI handler
        self._ui_handler = QuestionUIHandler(
            controller=controller,
            im_client=im_client,
            settings_manager=settings_manager,
            callback_prefix=self.CALLBACK_PREFIX,
        )

        # Map base_session_id -> tool_use_id for submitting answers
        self._pending_tool_use_ids: Dict[str, str] = {}
        # Map base_session_id -> SDK client for submitting answers
        self._pending_clients: Dict[str, Any] = {}
        # Map base_session_id -> composite_session_id for SDK query
        self._pending_composite_ids: Dict[str, str] = {}

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_pending(self, base_session_id: str) -> Optional[PendingQuestion]:
        """Get pending question for a session."""
        return self._ui_handler.get_pending(base_session_id)

    async def clear(self, base_session_id: str) -> None:
        """Clear all state for a session."""
        await self._ui_handler.clear(base_session_id)
        self._pending_tool_use_ids.pop(base_session_id, None)
        self._pending_clients.pop(base_session_id, None)
        self._pending_composite_ids.pop(base_session_id, None)

    def is_ask_user_question(self, tool_use_block) -> bool:
        """Check if a ToolUseBlock is an AskUserQuestion call."""
        return getattr(tool_use_block, "name", None) == "AskUserQuestion"

    async def handle_ask_user_question(
        self,
        request: AgentRequest,
        tool_use_block,
        client,
        composite_session_id: str,
    ) -> bool:
        """Handle an AskUserQuestion tool call.

        Args:
            request: The agent request
            tool_use_block: The ToolUseBlock with AskUserQuestion
            client: The Claude SDK client for submitting answers
            composite_session_id: Session ID for SDK query

        Returns:
            True if answer was received, False if timed out
        """
        tool_use_id = getattr(tool_use_block, "id", None)
        tool_input = getattr(tool_use_block, "input", {})

        if not tool_use_id:
            logger.error("AskUserQuestion tool call missing id")
            return False

        logger.info(
            "Handling AskUserQuestion for session %s, tool_use_id=%s",
            request.base_session_id,
            tool_use_id,
        )

        # Convert Claude's question format to common format
        questions = self._convert_questions(tool_input)
        if not questions:
            logger.warning("AskUserQuestion had no valid questions")
            return False

        # Build prompt text
        prompt_text = self._ui_handler.build_prompt_text(questions)

        # Extract option labels from first question for simple button case
        option_labels = [opt.label for opt in questions[0].options] if questions else []

        # Create pending question
        pending = PendingQuestion(
            questions=questions,
            prompt_text=prompt_text,
            option_labels=option_labels,
            base_session_id=request.base_session_id,
            thread_id=request.context.thread_id,
            agent_data={
                "tool_use_id": tool_use_id,
            },
        )

        # Store state for answer submission
        self._pending_tool_use_ids[request.base_session_id] = tool_use_id
        self._pending_clients[request.base_session_id] = client
        self._pending_composite_ids[request.base_session_id] = composite_session_id
        self._ui_handler.set_pending(request.base_session_id, pending)

        # Render question UI
        message_id = await self._ui_handler.render_question_ui(request, pending)
        if message_id:
            pending.prompt_message_id = message_id

        # Wait for answer
        answered = await self._ui_handler.wait_for_answer(
            request,
            pending,
            on_timeout=self._on_timeout,
        )

        return answered

    async def process_answer(
        self,
        request: AgentRequest,
        pending: PendingQuestion,
    ) -> bool:
        """Process user's answer and submit to Claude Code.

        Args:
            request: Agent request with answer in message field
            pending: Pending question state

        Returns:
            True if answer was submitted successfully
        """
        base_session_id = request.base_session_id
        tool_use_id = self._pending_tool_use_ids.get(base_session_id)
        client = self._pending_clients.get(base_session_id)
        composite_session_id = self._pending_composite_ids.get(base_session_id)

        if not tool_use_id or not client or not composite_session_id:
            logger.error(
                "Missing state for answer submission: tool_use_id=%s, client=%s, composite=%s",
                tool_use_id,
                client is not None,
                composite_session_id,
            )
            await emit_output(
                self._controller, request.context, Notice("Claude question context is missing. Please retry your request.")
            )
            return False

        # Parse answer from callback data
        answers_payload = self._parse_answer(request.message, pending)
        if answers_payload is None:
            await emit_output(self._controller, request.context, Notice("Please provide an answer."))
            return False

        # Update UI to show selection
        await self._ui_handler.update_prompt_after_answer(request, pending)

        # Build answer text for Claude
        answer_text = self._build_answer_text(answers_payload, pending)

        # Submit tool result to Claude
        try:
            await self._submit_tool_result(client, composite_session_id, tool_use_id, answer_text)
        except Exception as err:
            logger.error(f"Failed to submit answer to Claude: {err}", exc_info=True)
            await emit_output(
                self._controller, request.context, Notice(f"Failed to submit answer to Claude: {err}. Please retry.")
            )
            return False

        await self._ui_handler.send_answer_receipt(request, answers_payload)

        # Add reaction to indicate answer received
        await self._ui_handler.add_answer_reaction(request, pending)

        # Clean up and signal
        self._pending_tool_use_ids.pop(base_session_id, None)
        self._pending_clients.pop(base_session_id, None)
        self._pending_composite_ids.pop(base_session_id, None)
        self._ui_handler.pop_pending(base_session_id)
        self._ui_handler.signal_answer_received(base_session_id)

        logger.info("Answer submitted for session %s", base_session_id)
        return True

    async def open_modal(self, request: AgentRequest, pending: PendingQuestion) -> None:
        """Open the question modal."""
        await self._ui_handler.open_modal(request, pending)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _convert_questions(self, tool_input: Dict[str, Any]) -> List[Question]:
        """Convert Claude's AskUserQuestion format to common Question format.

        Claude format:
        {
            "questions": [
                {
                    "question": "Which library?",
                    "header": "Library",
                    "options": [{"label": "axios", "description": "HTTP client"}],
                    "multiSelect": false
                }
            ]
        }
        """
        questions_raw = tool_input.get("questions", [])
        if not isinstance(questions_raw, list):
            return []

        questions = []
        for q in questions_raw:
            if not isinstance(q, dict):
                continue

            options_raw = q.get("options", [])
            options = []
            for opt in options_raw:
                if not isinstance(opt, dict):
                    continue
                label = opt.get("label", "")
                desc = opt.get("description", "")
                if label:
                    options.append(QuestionOption(label=str(label), description=str(desc)))

            question = Question(
                question=str(q.get("question", "")),
                header=str(q.get("header", "")),
                options=options,
                # Claude uses "multiSelect", our common format uses "multiple"
                multiple=bool(q.get("multiSelect", False)),
            )
            questions.append(question)

        return questions

    def _parse_answer(self, message: str, pending: PendingQuestion) -> Optional[List[List[str]]]:
        """Parse answer from callback data or user message.

        Returns answers_payload in format [[answer1], [answer2], ...]
        """
        prefix = f"{self.CALLBACK_PREFIX}:"

        # Button click: "claude_question:choose:1"
        if message.startswith(f"{prefix}choose:"):
            try:
                choice_idx = int(message.rsplit(":", 1)[-1]) - 1
                if 0 <= choice_idx < len(pending.option_labels):
                    answer = pending.option_labels[choice_idx]
                    return [[answer] for _ in range(pending.question_count)]
            except Exception:
                pass
            return None

        # Modal submission: "claude_question:modal:{...}"
        if message.startswith(f"{prefix}modal:"):
            try:
                payload = json.loads(message.split(":", 2)[-1])
                answers = payload.get("answers", [])
                if isinstance(answers, list) and answers:
                    # Normalize to List[List[str]]
                    normalized = []
                    for answer in answers:
                        if isinstance(answer, list):
                            normalized.append([str(x) for x in answer if x])
                        elif answer:
                            normalized.append([str(answer)])
                        else:
                            normalized.append([])
                    return normalized
            except Exception:
                logger.debug("Failed to parse modal answers")
            return None

        # Plain text answer
        text = message.strip()
        if text and not message.startswith(prefix):
            return [[text] for _ in range(pending.question_count)]

        return None

    def _build_answer_text(self, answers_payload: List[List[str]], pending: PendingQuestion) -> str:
        """Build answer text to send back to Claude.

        Format: "User has answered your questions: Q1=answer1, Q2=answer2"
        """
        parts = []
        for idx, answers in enumerate(answers_payload):
            if not answers:
                continue
            joined = ", ".join(answers)
            if len(answers_payload) > 1:
                parts.append(f"Q{idx + 1}={joined}")
            else:
                parts.append(joined)

        if not parts:
            return "User provided no answer"

        return f"User has answered your questions: {'; '.join(parts)}"

    async def _submit_tool_result(
        self,
        client,
        composite_session_id: str,
        tool_use_id: str,
        answer_text: str,
    ) -> None:
        """Submit tool result to Claude Code SDK.

        Claude expects a user message with tool_result content block.
        """
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": answer_text,
                    }
                ],
            },
            "parent_tool_use_id": None,
            "session_id": composite_session_id,
        }

        # Use transport.write directly since query() may not support this format
        if hasattr(client, "_transport") and client._transport:
            await client._transport.write(json.dumps(message) + "\n")
            logger.info(
                "Submitted tool result for %s: %s",
                tool_use_id,
                answer_text[:100],
            )
        else:
            raise RuntimeError("Claude SDK client transport not available")

    async def _on_timeout(self, request: AgentRequest, pending: PendingQuestion) -> None:
        """Handle timeout when user doesn't answer."""
        # Clean up our state
        self._pending_tool_use_ids.pop(request.base_session_id, None)
        self._pending_clients.pop(request.base_session_id, None)
        self._pending_composite_ids.pop(request.base_session_id, None)
