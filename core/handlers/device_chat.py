"""Device-scoped text generation. Never constructs an Agent, tool, Run or Delivery.

The sole executable path is a provider stream with an empty tool surface. Durable
records are transcripts only; no record is a resumable job or authorization.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

from core.agent_core.ai.provider import Done, ProviderError, ToolCallStart, ToolCallDelta
from core.agent_core.ai.registry import create_adapter
from core.agent_core.ai.provider import ModelRequest
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, TextBlock, ThinkingBlock, UserMessage

POLICY = "device_chat_only_v1"


class ToolsForbidden(ValueError):
    """A model tried to cross the text-only boundary; nothing was executed."""


def identifier(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid_device_identity")
    return value


@dataclass
class Conversation:
    id: str
    device_id: str
    bot_id: str
    model: str
    role: str
    display_name: str
    records: list[dict] = field(default_factory=list)
    messages: list = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    task: asyncio.Task | None = None
    closed: bool = False


class DeviceChat:
    """One host-owned conversation. Restart never restores an executable context."""

    def __init__(self, controller):
        from config.paths import get_state_dir

        self.controller = controller
        self.root = get_state_dir() / "device-voice"
        self.active: Conversation | None = None
        self.lifecycle = asyncio.Lock()

    async def _save(self, conversation: Conversation) -> None:
        from config.atomic_io import write_atomic

        document = json.dumps({
            "version": 1, "policy": POLICY, "source": "device_voice", "conversation_id": conversation.id,
            "device_id": conversation.device_id, "bot": "littlevibey", "bot_id": conversation.bot_id,
            "model": conversation.model, "closed": conversation.closed, "turns": conversation.records,
        }, ensure_ascii=False)

        def save():
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_atomic(self.root / f"{conversation.id}.json", document)

        from core.runtime_work import land_step

        step, cancelled = await land_step(asyncio.to_thread(save))
        step.result()
        if cancelled:
            raise asyncio.CancelledError

    async def open(self, device_id: str) -> str:
        async with self.lifecycle:
            return await self._open(device_id)

    async def _open(self, device_id: str) -> str:
        from core.bots import bot_from_agent

        agent = await asyncio.to_thread(self.controller.vibe_agent_store.require_enabled, "littlevibey")
        if agent.backend != "vibey" or not agent.model:
            raise ValueError("device_chat_configuration_unsupported")
        bot = bot_from_agent(agent)
        if bot is None:
            raise ValueError("device_chat_configuration_unsupported")
        if not all(callable(getattr(getattr(self.controller, "model_hub_runtime", None), name, None))
                   for name in ("resolve_hop", "settle_turn", "retire_process_scope")):
            raise ValueError("device_chat_model_unavailable")
        # The single device process may have crashed. A fresh connection retires
        # its previous generation; it never adopts its transcript or pending work.
        if self.active is not None:
            await self._close(self.active.id)
        conversation = Conversation(str(uuid4()), device_id, agent.id, agent.model,
                                    bot.role, bot.display_name or "littlevibey")
        try:
            await self._save(conversation)
        except asyncio.CancelledError:
            conversation.closed = True
            await self._save(conversation)
            raise
        self.active = conversation
        return conversation.id

    async def close_device(self, device_id: str) -> None:
        """Recover an open whose successful response the device process lost."""
        async with self.lifecycle:
            if self.active is not None and self.active.device_id == device_id:
                await self._close(self.active.id)

    async def close(self, conversation_id: str) -> None:
        async with self.lifecycle:
            await self._close(conversation_id)

    async def _close(self, conversation_id: str) -> None:
        conversation = self.active
        if conversation is None or conversation.id != identifier(conversation_id):
            return
        conversation.closed = True
        if conversation.task is not None and conversation.task is not asyncio.current_task():
            conversation.task.cancel()
            await asyncio.gather(conversation.task, return_exceptions=True)
        await self._save(conversation)
        if self.active is conversation:
            self.active = None

    def history(self, conversation_id: str | None) -> dict:
        if conversation_id is None:
            return {"conversations": [path.stem for path in sorted(self.root.glob("*.json"))]}
        value = json.loads((self.root / f"{identifier(conversation_id)}.json").read_text())
        if value.get("version") != 1 or value.get("policy") != POLICY:
            raise ValueError("device_chat_record_unsupported")
        return value

    async def answer(self, conversation_id: str, request_id: str, text: str) -> str:
        conversation = self.active
        identifier(conversation_id)
        identifier(request_id)
        if (conversation is None or conversation.id != conversation_id or conversation.closed
                or conversation.task is not None or request_id in conversation.seen or len(conversation.seen) >= 128):
            raise ValueError("device_chat_not_active")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("invalid_device_text")
        conversation.seen.add(request_id)
        conversation.task = asyncio.current_task()
        record = {"request_id": request_id, "input": text, "status": "pending"}
        conversation.records.append(record)
        try:
            await self._save(conversation)
            answer = await asyncio.wait_for(self._generate(conversation, request_id, text), 45)
            if conversation.closed:
                raise ValueError("device_chat_closed")
            record.update(status="completed", output=answer)
            # Only plain text from this conversation is reused. No opaque/tool
            # history, filesystem instructions or shared Bot messages enter here.
            conversation.messages.extend([("user", text), ("assistant", answer)])
            conversation.messages = conversation.messages[-16:]
            return answer
        except ToolsForbidden:
            raise
        except Exception:
            # Provider/config errors never expose credentials or response bodies.
            raise ValueError("device_chat_unavailable") from None
        finally:
            if record["status"] == "pending":
                record["status"] = "refused"
            try:
                await self._save(conversation)
            finally:
                conversation.task = None

    async def _generate(self, conversation: Conversation, request_id: str, text: str) -> str:
        from modules.agents.vibey.models import selection_from_hop
        from core.agent_core.messages import Origin

        hub = self.controller.model_hub_runtime
        scope = "device-chat:" + conversation.id
        turn_id = "device-chat:" + request_id
        adapter = None
        cancel = CancelToken()
        try:
            hop = await hub.resolve_hop(conversation.model, process_scope=scope, turn_id=turn_id)
            selection = selection_from_hop(hop)
            # Explicitly erase provider-native declarations as well as local tools.
            endpoint = replace(selection.endpoint, hosted_tools={}, image_models=None)
            origin = Origin(endpoint.provider, endpoint.protocol, endpoint.model_id)
            messages = tuple(
                UserMessage((TextBlock(content),)) if role == "user" else
                AssistantMessage((TextBlock(content),), origin, "stop")
                for role, content in conversation.messages
            ) + (UserMessage((TextBlock(text),)),)
            request = ModelRequest(
                endpoint=endpoint, tools=(), messages=messages, max_tokens=512,
                system=(
                    f"You are {conversation.display_name} (littlevibey). Role: {conversation.role}\n"
                    "This is an isolated device conversation. Reply naturally in one or two short complete sentences. "
                    "You can only converse and suggest; you cannot execute actions, access files, delegate, or grant approval. "
                    "For requests to do work, tell the user to send the specific task in signed-in Avibe Web. "
                    "Never claim that saying confirm here authorizes or performs an action."
                ),
            )
            adapter = create_adapter(endpoint.protocol, gateway=True)
            async with aclosing(adapter.stream(request, cancel)) as events:
                async for event in events:
                    if isinstance(event, (ToolCallStart, ToolCallDelta)):
                        raise ToolsForbidden("device_chat_tools_forbidden")
                    if isinstance(event, ProviderError):
                        raise ValueError("device_chat_response_refused")
                    if isinstance(event, Done):
                        message = event.message
                        if message.stop_reason == "tool_use" or any(
                            not isinstance(block, (TextBlock, ThinkingBlock)) for block in message.content
                        ):
                            raise ToolsForbidden("device_chat_tools_forbidden")
                        if message.stop_reason != "stop":
                            raise ValueError("device_chat_response_refused")
                        answer = "".join(block.text or "" for block in message.content if isinstance(block, TextBlock)).strip()
                        if not answer or len(answer) > 8000:
                            raise ValueError("invalid_device_answer")
                        return answer
            raise ValueError("missing_device_answer")
        finally:
            cancel.cancel()
            try:
                if adapter is not None:
                    await adapter.aclose()
            finally:
                try:
                    pending = hub.settle_turn(turn_id, settled_by=POLICY, ts=datetime.now(timezone.utc).isoformat())
                    if pending is not None:
                        await pending
                finally:
                    hub.retire_process_scope("vibey", scope, terminal_turn_id=turn_id)
