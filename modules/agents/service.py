import logging
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .base import (
    AGENT_RUNTIME_TURN_KEY,
    AGENT_RUNTIME_TURN_TOKEN,
    AGENT_TURN_TOKEN,
    AgentRequest,
    BaseAgent,
)

logger = logging.getLogger(__name__)

STALE_STOP_REASONS = {"not_active", "runtime_unavailable"}


class AgentService:
    """Registry and dispatcher for agent implementations."""

    def __init__(self, controller):
        self.controller = controller
        self.agents: Dict[str, BaseAgent] = {}
        self.default_agent = "claude"
        self._turn_gates: dict[str, _RuntimeTurnGate] = {}
        # Strong refs to fire-and-forget tasks (e.g. the cancellation tidy) so the
        # event loop doesn't GC them before they run (asyncio only weak-refs tasks).
        self._background_tasks: set[asyncio.Task] = set()

    def register(self, agent: BaseAgent):
        self.agents[agent.name] = agent
        logger.info(f"Registered agent backend: {agent.name}")

    def get(self, agent_name: Optional[str]) -> BaseAgent:
        target = agent_name or self.default_agent
        if target in self.agents:
            return self.agents[target]
        raise KeyError(target)

    async def handle_message(self, agent_name: str, request: AgentRequest):
        agent = self.get(agent_name)
        runtime_key = self._runtime_turn_key(agent, request)
        gate = self._get_turn_gate(runtime_key)
        await gate.lock.acquire()
        gate.token = uuid.uuid4().hex
        gate.backend = agent.name
        gate.runtime_started = False
        self._stamp_runtime_turn(request, runtime_key, gate.token)
        try:
            # INBOUND status chokepoint (one of exactly two — the other is the outbound
            # MessageDispatcher.emit_agent_message). Every turn, every source (chat /
            # scheduled / Show Page), every backend funnels through here, so this is the
            # single place that marks an avibe session "running". The matching idle /
            # failed is written by the outbound terminal result. Non-avibe turns carry
            # no workbench session id and are skipped.
            manager = getattr(self.controller, "session_turns", None)
            if manager is not None:
                manager.on_running(request.context)
            # Turn-start work runs HERE — after the gate/on_running confirm the
            # turn is actually running — not while it was queued behind another
            # turn. Claiming the shared per-turn trigger id and posting the
            # "starting" status bubble now means a queued turn never hijacks the
            # running turn's bubble key or posts a premature bubble. Both hooks
            # are optional and guarded so a missing hook or a bubble failure can
            # never break the turn.
            await self._begin_turn_status(request.context)
            self._track_processing_indicator_turn(request)
            await agent.handle_message(request)
        except asyncio.CancelledError:
            # Shutdown / SIGTERM / supersede cancels the turn mid-flight. Without a
            # terminal emit the concise status bubble stays stuck on its last
            # action. Best effort: SCHEDULE (don't await) a silent terminal result
            # so the outbound chokepoint collapses the bubble + settles the dot;
            # awaiting here would swallow the cancellation. (C3)
            #
            # The runtime turn must be released AFTER that emit runs, NOT before:
            # releasing clears the turn token, and the result branch drops any
            # emit whose turn is no longer current — so an early release would
            # make the scheduled tidy a no-op and leave the bubble stuck. Release
            # inside the scheduled task's finally; fall back to a synchronous
            # release if the emit can't be scheduled so the gate never leaks.
            emit = getattr(self.controller, "emit_agent_message", None)
            scheduled = False
            if callable(emit):
                try:

                    async def _tidy_on_cancel() -> None:
                        try:
                            await emit(request.context, "result", "", is_error=True, level="silent")
                        except Exception:
                            logger.debug("Failed to emit terminal tidy on cancellation", exc_info=True)
                        finally:
                            self.release_runtime_turn(request.context)

                    tidy_task = asyncio.create_task(_tidy_on_cancel())
                    self._background_tasks.add(tidy_task)
                    tidy_task.add_done_callback(self._background_tasks.discard)
                    scheduled = True
                except Exception:
                    logger.debug("Failed to schedule terminal tidy on cancellation", exc_info=True)
            if not scheduled:
                self.release_runtime_turn(request.context)
            raise
        except Exception:
            # The message handler converts backend exceptions into a terminal
            # error result using the same context. Try that shared terminal path
            # here too; if delivery itself is broken, the finally below still
            # releases this turn's token so later prompts cannot hang forever.
            try:
                emit = getattr(self.controller, "emit_agent_message", None)
                if callable(emit):
                    try:
                        await emit(request.context, "result", "", is_error=True, level="silent")
                    except Exception:
                        logger.debug("Failed to emit terminal result for backend exception", exc_info=True)
            finally:
                self.release_runtime_turn(request.context)
            raise

    async def clear_sessions(self, session_key: str) -> Dict[str, int]:
        cleared: Dict[str, int] = {}
        for name in list(self.agents.keys()):
            count = await self.clear_backend_sessions(name, session_key)
            if count:
                cleared[name] = count
        return cleared

    async def clear_backend_sessions(self, agent_name: str, session_key: str) -> int:
        agent = self.get(agent_name)
        runtime_key_getter = getattr(agent, "runtime_turn_keys_for_session_key", None)
        runtime_keys = runtime_key_getter(session_key) if callable(runtime_key_getter) else set()
        runtime_tokens = self._runtime_turn_tokens(runtime_keys, backend=agent.name)
        count = await agent.clear_sessions(session_key)
        for runtime_key, runtime_token in runtime_tokens.items():
            self.release_runtime_turn_key(runtime_key, runtime_token)
        return count

    def release_runtime_turns_for_backend(self, agent_name: str) -> None:
        runtime_tokens = self.runtime_turn_tokens_for_backend(agent_name)
        self.release_runtime_turn_tokens(runtime_tokens)

    def runtime_turn_tokens_for_backend(self, agent_name: str) -> dict[str, str]:
        agent = self.agents.get(agent_name)
        backend = agent.name if agent is not None else agent_name
        return {
            runtime_key: gate.token
            for runtime_key, gate in self._turn_gates.items()
            if gate.backend == backend and gate.token
        }

    def release_runtime_turn_tokens(self, runtime_tokens: dict[str, str]) -> None:
        for runtime_key, runtime_token in runtime_tokens.items():
            self.release_runtime_turn_key(runtime_key, runtime_token)

    async def handle_stop(self, agent_name: str, request: AgentRequest) -> bool:
        agent = self.get(agent_name)
        runtime_key = self._runtime_turn_key(agent, request)
        gate = self._turn_gates.get(runtime_key)
        if gate is not None and gate.token:
            self._stamp_runtime_turn(request, runtime_key, gate.token)
        handled = await agent.handle_stop(request)
        if (
            not handled
            and getattr(request, "stop_failure_reason", None) in STALE_STOP_REASONS
            and gate is not None
            and gate.token
            and gate.runtime_started
        ):
            self.release_runtime_turn(request.context)
        return handled

    def mark_runtime_turn_started(self, context: Any) -> None:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None or gate.token != runtime_token:
            return
        gate.runtime_started = True

    def runtime_turn_started(self, context: Any) -> bool:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return False
        gate = self._turn_gates.get(runtime_key)
        return bool(gate is not None and gate.token == runtime_token and gate.runtime_started)

    def release_runtime_turn(self, context: Any) -> None:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None or gate.token != runtime_token:
            return
        self.release_runtime_turn_key(runtime_key, runtime_token)

    def release_runtime_turn_key(self, runtime_key: str, runtime_token: str | None = None) -> None:
        runtime_key = str(runtime_key or "").strip()
        if not runtime_key:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None:
            return
        if runtime_token is not None and gate.token != runtime_token:
            return
        gate.token = ""
        gate.backend = ""
        gate.runtime_started = False
        if gate.lock.locked():
            gate.lock.release()

    def emit_matches_runtime_turn(self, context: Any) -> bool:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return True
        gate = self._turn_gates.get(runtime_key)
        return gate is not None and gate.token == runtime_token

    def backend_alive(self, context: Any) -> Optional[bool]:
        """Resolve the backend for this turn (via its runtime gate) and ask it
        whether it is still alive. Returns ``None`` when the backend is unknown
        or has no probe — callers treat ``None`` as alive (no false alarm)."""
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        gate = self._turn_gates.get(runtime_key) if runtime_key else None
        backend = gate.backend if gate else None
        agent = self.agents.get(backend) if backend else None
        if agent is None:
            return None
        probe = getattr(agent, "backend_alive", None)
        if not callable(probe):
            return None
        try:
            return probe(context)
        except Exception:
            logger.debug("backend_alive probe raised for %s", backend, exc_info=True)
            return None

    def _get_turn_gate(self, runtime_key: str) -> "_RuntimeTurnGate":
        if runtime_key not in self._turn_gates:
            self._turn_gates[runtime_key] = _RuntimeTurnGate()
        return self._turn_gates[runtime_key]

    @staticmethod
    def _runtime_turn_key(agent: BaseAgent, request: AgentRequest) -> str:
        runtime_key = getattr(agent, "runtime_turn_key", None)
        if callable(runtime_key):
            return runtime_key(request)
        return (
            str(getattr(request, "composite_session_id", "") or "").strip()
            or str(getattr(request, "base_session_id", "") or "").strip()
            or "default"
        )

    def _runtime_turn_tokens(self, runtime_keys: set[str], *, backend: str | None = None) -> dict[str, str]:
        tokens = {}
        for runtime_key in runtime_keys:
            gate = self._turn_gates.get(runtime_key)
            if gate is None or not gate.token:
                continue
            if backend is not None and gate.backend != backend:
                continue
            tokens[runtime_key] = gate.token
        return tokens

    @staticmethod
    def _stamp_runtime_turn(request: AgentRequest, runtime_key: str, runtime_token: str) -> None:
        if request.context.platform_specific is None:
            request.context.platform_specific = {}
        request.context.platform_specific[AGENT_RUNTIME_TURN_KEY] = runtime_key
        request.context.platform_specific[AGENT_RUNTIME_TURN_TOKEN] = runtime_token
        if request.context.platform_specific.get(AGENT_TURN_TOKEN):
            return
        request.context.platform_specific[AGENT_TURN_TOKEN] = runtime_token

    async def _begin_turn_status(self, context: Any) -> None:
        """Run turn-start status work at the real start of the turn.

        Claims the shared per-turn trigger id (so the dispatcher's consolidated
        bubble key points at THIS turn) and posts the initial "starting" status
        bubble. Both are best-effort: a controller without these hooks, or a
        bubble failure, must not break the turn. The bubble is awaited inline so
        it exists before the backend's first process emit can edit it.
        """
        update_thread_message_id = getattr(self.controller, "update_thread_message_id", None)
        if callable(update_thread_message_id):
            try:
                update_thread_message_id(context)
            except Exception:
                logger.debug("update_thread_message_id failed at turn start", exc_info=True)
        dispatcher = getattr(self.controller, "message_dispatcher", None)
        begin_status_bubble = getattr(dispatcher, "begin_status_bubble", None) if dispatcher else None
        if callable(begin_status_bubble):
            try:
                await begin_status_bubble(context)
            except Exception:
                logger.debug("begin_status_bubble failed at turn start", exc_info=True)

    def _track_processing_indicator_turn(self, request: AgentRequest) -> None:
        handle = getattr(request, "processing_indicator", None)
        if handle is None:
            return
        service = getattr(self.controller, "processing_indicator", None)
        track = getattr(service, "track_turn", None)
        if callable(track):
            track(request.context, request)

    async def refresh_runtime_config(self, agent_name: str, runtime_config: Any) -> bool:
        """Refresh a backend's live runtime state from the latest config.

        Backend adapters own their cached transports/sessions, so the service
        centralizes dispatch while adapters decide how to apply the new
        runtime config. Returns ``False`` when the backend is not registered or
        does not expose the refresh contract.
        """
        agent = self.agents.get(agent_name)
        if agent is None:
            return False
        refresh = getattr(agent, "refresh_runtime_config", None)
        if not callable(refresh):
            return False
        runtime_tokens = self.runtime_turn_tokens_for_backend(agent.name)
        try:
            await refresh(runtime_config)
            return True
        finally:
            self.release_runtime_turn_tokens(runtime_tokens)


@dataclass
class _RuntimeTurnGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    token: str = ""
    backend: str = ""
    runtime_started: bool = False
