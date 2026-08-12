"""Processing indicator lifecycle management.

This module owns the short-lived UI state shown while a user turn is being
processed: acknowledgement messages, acknowledgement reactions, and typing
indicators.  Agent implementations should not know platform-specific cleanup
details; they should only ask this service to delete or finish an indicator.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Optional

from config.platform_registry import PlatformCapabilities, get_platform_descriptor
from modules.im import MessageContext
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

_PROCESSING_INDICATOR_MODES = ("typing", "reaction", "message")
ACK_REACTION_EMOJI = "👀"
# Shown while an IM message is waiting behind a running turn (blocked on the
# runtime gate). It is promoted to ACK_REACTION_EMOJI when this message's turn
# actually starts. See AgentService.handle_message (show_queued_reaction /
# promote_reaction_to_running).
QUEUED_REACTION_EMOJI = "👌"
# Admission receipts for an IM message that did NOT start its own turn. Without
# them a message sent while a turn is already running gets no feedback at all:
# the message handler hands it to its durable Delivery owner and returns before
# any processing indicator is started, and a steered input never dispatches a
# turn of its own. See MessageHandler._admit_human_delivery.
STEERED_REACTION_EMOJI = "✍️"
UNCONFIRMED_REACTION_EMOJI = "🤔"
NOT_DELIVERED_REACTION_EMOJI = "🤷"
# Terminal receipts that REPLACE the running 👀 instead of clearing it. Plain
# removal is the right ending for a turn that produced its own result — the
# result IS the receipt — but it is ambiguous for a turn that ended without one:
# a stopped turn and a turn whose runtime died both leave a message that simply
# stopped having a reaction, which is also what a healthy silent completion
# looks like. These two say which happened, and they persist on the triggering
# message rather than costing a line in the thread.
STOPPED_REACTION_EMOJI = "⏹️"
INTERRUPTED_REACTION_EMOJI = "⚠️"
# Delivery state -> admission receipt. States that own a turn (``claimed``,
# ``steering``, ``interrupt_waiting``) are absent on purpose: their feedback is
# the turn's own processing indicator, and an ``accepted`` Delivery is only
# reported here when it was steered into a turn already running.
_ADMISSION_ACK_REACTIONS: dict[str, str] = {
    "accepted": STEERED_REACTION_EMOJI,
    "queued": QUEUED_REACTION_EMOJI,
    "pending_steer": QUEUED_REACTION_EMOJI,
    "reconciling_steer": UNCONFIRMED_REACTION_EMOJI,
    "retired": NOT_DELIVERED_REACTION_EMOJI,
}
# Backstop for receipts nothing ever reads back (a queue drained by a Stop, a
# session archived mid-wait, a terminal ✍️/🤷 on a message no other Delivery
# touches). Bounded FIFO eviction: dropping the oldest key only forfeits a later
# replace/clear, never the reaction itself.
_ADMISSION_ACK_REGISTRY_LIMIT = 1024
# Registry marker for a message whose own turn has started. Kept so a receipt
# still in flight when the turn began cannot decorate it afterwards.
_ADMISSION_ACK_CONSUMED = "\x00turn-started"


@dataclass
class ProcessingIndicatorHandle:
    """Runtime handle for a processing indicator."""

    context: MessageContext
    ack_message_id: Optional[str] = None
    ack_message_channel_id: Optional[str] = None
    ack_reaction_message_id: Optional[str] = None
    ack_reaction_emoji: Optional[str] = None
    # Captured only by start(), which runs for human turns. Backend-initiated
    # turns may reuse a context with an old message_id and must not stamp it.
    terminal_reaction_message_id: Optional[str] = None
    typing_indicator_active: bool = False
    typing_indicator_task: Optional[asyncio.Task] = None
    # True when the reaction indicator is the selected mode for this turn. The
    # reaction itself is added at the runtime gate (queued 👌 → running 👀), not
    # here, so this flag tells the gate hooks whether to act. It is intentionally
    # NOT part of to_snapshot/from_snapshot: the only snapshot/restore path
    # (OpenCode poll loop) runs AFTER the reaction was already promoted to 👀, and
    # finish() keys off ack_reaction_emoji directly, so the flag is not load-bearing
    # across a restore.
    reaction_indicator_selected: bool = False

    def to_snapshot(self) -> dict[str, Any]:
        payload = self.context.platform_specific or {}
        return {
            "platform": self.context.platform or payload.get("platform") or "",
            "user_id": self.context.user_id or "",
            "channel_id": self.context.channel_id or "",
            "thread_id": self.context.thread_id or "",
            "message_id": self.context.message_id or "",
            "is_dm": bool(payload.get("is_dm", False)),
            "context_token": str(payload.get("context_token") or ""),
            "ack_message_id": self.ack_message_id,
            "ack_message_channel_id": self.ack_message_channel_id,
            "ack_reaction_message_id": self.ack_reaction_message_id,
            "ack_reaction_emoji": self.ack_reaction_emoji,
            "terminal_reaction_message_id": self.terminal_reaction_message_id,
            "typing_indicator_active": self.typing_indicator_active,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "ProcessingIndicatorHandle":
        platform = str(data.get("platform") or "")
        context_token = str(data.get("context_token") or "")
        platform_specific: dict[str, Any] = {}
        if platform:
            platform_specific["platform"] = platform
        if data.get("is_dm") is not None:
            platform_specific["is_dm"] = bool(data.get("is_dm"))
        if context_token:
            platform_specific["context_token"] = context_token
        context = MessageContext(
            user_id=str(data.get("user_id") or ""),
            channel_id=str(data.get("channel_id") or ""),
            platform=platform or None,
            thread_id=data.get("thread_id") or None,
            message_id=data.get("message_id") or None,
            platform_specific=platform_specific or None,
        )
        return cls(
            context=context,
            ack_message_id=data.get("ack_message_id") or None,
            ack_message_channel_id=data.get("ack_message_channel_id") or data.get("channel_id") or None,
            ack_reaction_message_id=data.get("ack_reaction_message_id") or None,
            ack_reaction_emoji=data.get("ack_reaction_emoji") or None,
            terminal_reaction_message_id=(
                data.get("terminal_reaction_message_id")
                or data.get("ack_reaction_message_id")
                or data.get("message_id")
                or None
            ),
            typing_indicator_active=bool(data.get("typing_indicator_active", False)),
        )


class ProcessingIndicatorService:
    """Start and finish processing indicators through one owner."""

    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config
        self._indicators_by_turn_token: dict[str, Any] = {}
        self._admission_acks: dict[str, str] = {}
        self._admission_ack_locks: dict[str, asyncio.Lock] = {}

    def _get_im_client(self, context: MessageContext):
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            return getter(context)
        return self.controller.im_client

    def _get_context_platform(self, context: MessageContext) -> str:
        return (
            context.platform
            or (context.platform_specific or {}).get("platform")
            or getattr(self.config, "platform", "")
        )

    def _capabilities(self, context: MessageContext) -> PlatformCapabilities:
        return get_platform_descriptor(self._get_context_platform(context)).capabilities

    def _mode_supported(
        self,
        capabilities: PlatformCapabilities,
        mode: str,
        context: MessageContext,
    ) -> bool:
        if mode == "typing":
            return capabilities.supports_typing_indicator
        if mode == "reaction":
            return capabilities.supports_reaction_indicator and bool(self._reaction_target_message_id(context))
        if mode == "message":
            return capabilities.supports_message_indicator
        return False

    def _reaction_target_message_id(self, context: MessageContext) -> Optional[str]:
        payload = context.platform_specific or {}
        if isinstance(payload, dict):
            target_id = payload.get("processing_indicator_message_id")
            if target_id:
                return str(target_id)
        return context.message_id

    def _candidate_modes(self, capabilities: PlatformCapabilities) -> list[str]:
        preferred = capabilities.preferred_processing_indicator
        configured = getattr(self.config, "ack_mode", "typing")
        if capabilities.force_preferred_processing_indicator:
            candidates = [preferred]
        else:
            candidates = [configured, preferred, "typing", "reaction", "message"]
        return [
            mode
            for index, mode in enumerate(candidates)
            if mode in _PROCESSING_INDICATOR_MODES and mode not in candidates[:index]
        ]

    def _concise_status_bubble_active(self, context: MessageContext) -> bool:
        check = getattr(self.controller, "uses_concise_status_bubble", None)
        if not callable(check):
            return False
        try:
            return bool(check(context))
        except Exception:
            logger.debug("uses_concise_status_bubble check failed; not suppressing", exc_info=True)
            return False

    def _processing_modes(self, context: MessageContext) -> list[str]:
        capabilities = self._capabilities(context)
        return [
            mode
            for mode in self._candidate_modes(capabilities)
            if self._mode_supported(capabilities, mode, context)
        ]

    def target_context(self, context: MessageContext) -> MessageContext:
        """Return the platform-appropriate context for immediate ACK-style replies."""

        im_client = self._get_im_client(context)
        capabilities = self._capabilities(context)
        if capabilities.supports_threads and im_client.should_use_thread_for_reply() and context.thread_id:
            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                platform=context.platform,
                thread_id=context.thread_id,
                message_id=context.message_id,
                platform_specific=context.platform_specific,
            )
        return context

    def _get_ack_text(self, agent_name: str) -> str:
        label = agent_name or self.controller.agent_service.default_agent
        agent_label = label.capitalize() if label else ""
        lang = self.controller._get_lang() if hasattr(self.controller, "_get_lang") else getattr(self.config, "language", "en")
        return f"📨 {i18n_t('message.ack', lang, agent=agent_label)}"

    async def _typing_keepalive_loop(self, context: MessageContext) -> None:
        im_client = self._get_im_client(context)
        try:
            while True:
                await asyncio.sleep(5)
                ok = await im_client.send_typing_indicator(context)
                if not ok:
                    logger.debug("Typing keepalive not applied for %s", context.user_id)
        except asyncio.CancelledError:
            raise

    def _admission_ack_registry(self) -> dict[str, str]:
        registry = getattr(self, "_admission_acks", None)
        if not isinstance(registry, dict):
            registry = {}
            self._admission_acks = registry
        return registry

    def _remember_admission_ack(self, key: str, value: str) -> None:
        registry = self._admission_ack_registry()
        registry.pop(key, None)
        registry[key] = value
        while len(registry) > _ADMISSION_ACK_REGISTRY_LIMIT:
            registry.pop(next(iter(registry)), None)

    def _admission_ack_locks_registry(self) -> dict[str, list]:
        locks = getattr(self, "_admission_ack_locks", None)
        if not isinstance(locks, dict):
            locks = {}
            self._admission_ack_locks = locks
        return locks

    @asynccontextmanager
    async def _admission_ack_guard(self, key: str):
        """Serialize every receipt operation on one message.

        Both halves await a platform call, so without this an ``add_reaction``
        still in flight would record its emoji *after* the turn's ``start()``
        already looked for one to clear, stranding a 👌 next to the running
        indicator.
        """

        locks = self._admission_ack_locks_registry()
        entry = locks.get(key)
        if entry is None:
            entry = locks[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] <= 0:
                locks.pop(key, None)

    def _admission_ack_key(self, context: MessageContext) -> Optional[str]:
        message_id = self._reaction_target_message_id(context)
        if not message_id:
            return None
        return "|".join(
            (
                self._get_context_platform(context),
                str(context.channel_id or ""),
                str(message_id),
            )
        )

    @staticmethod
    def _admission_receipt(state: str, admission: str) -> Optional[str]:
        """Resolve one Delivery state into the receipt it should show.

        ``accepted`` is deliberately gated on explicit steer provenance: an
        idempotent re-entry of an already accepted Delivery reports the state
        without an ``admission``, and that observation says nothing about
        whether the input joined a running turn or started its own.
        """

        normalized = str(state or "").strip()
        if normalized == "accepted" and admission != "steered":
            return None
        return _ADMISSION_ACK_REACTIONS.get(normalized)

    async def ack_delivery_state(
        self,
        context: MessageContext,
        *,
        state: str,
        admission: str = "",
    ) -> Optional[str]:
        """Report the admission outcome of one IM input as a reaction receipt.

        Applies only to input that did not start a turn of its own: a started
        turn already answers with the normal processing indicator. Returns the
        emoji that was applied, or ``None`` when nothing was reported.
        """

        if admission == "started":
            return None
        emoji = self._admission_receipt(state, admission)
        if not emoji:
            return None
        message_id = self._reaction_target_message_id(context)
        if not message_id:
            return None
        capabilities = self._capabilities(context)
        if not self._mode_supported(capabilities, "reaction", context):
            # Platforms without reactions (e.g. WeChat) stay silent rather than
            # posting a bubble per queued message.
            return None
        key = self._admission_ack_key(context)
        if not key:
            return None
        async with self._admission_ack_guard(key):
            registry = self._admission_ack_registry()
            previous = registry.get(key)
            if previous == _ADMISSION_ACK_CONSUMED:
                # This message's own turn already took the message over; a late
                # receipt would sit next to (or replace) the running indicator.
                return None
            if previous == emoji:
                return emoji
            im_client = self._get_im_client(context)
            if previous:
                try:
                    await im_client.remove_reaction(context, message_id, previous)
                except Exception as err:
                    logger.debug("Failed to remove previous admission ack: %s", err)
            try:
                applied = await im_client.add_reaction(context, message_id, emoji)
            except Exception as err:
                logger.debug("Failed to add admission ack reaction: %s", err)
                applied = False
            if not applied:
                registry.pop(key, None)
                return None
            # Every receipt is remembered, terminal ones included. A reaction
            # target can be shared — a quick-reply callback reacts on its bot
            # echo, so two Deliveries settle on one message — and a platform
            # shows one reaction per (message, emoji, bot). The receipt therefore
            # describes the message rather than any one Delivery: last writer
            # wins, and the previous receipt is removed instead of stacked. The
            # FIFO cap bounds what a terminal receipt leaves behind.
            self._remember_admission_ack(key, emoji)
            return emoji

    async def clear_admission_ack(self, context: MessageContext) -> None:
        """Remove the admission receipt once this input's own turn takes over."""

        key = self._admission_ack_key(context)
        if not key:
            return
        async with self._admission_ack_guard(key):
            registry = self._admission_ack_registry()
            emoji = registry.get(key)
            # Mark the message as taken over before awaiting the platform call:
            # a receipt that arrives late must not re-decorate a running turn.
            self._remember_admission_ack(key, _ADMISSION_ACK_CONSUMED)
            if not emoji or emoji == _ADMISSION_ACK_CONSUMED:
                return
            try:
                await self._get_im_client(context).remove_reaction(
                    context,
                    self._reaction_target_message_id(context),
                    emoji,
                )
            except Exception as err:
                logger.debug("Failed to remove admission ack reaction: %s", err)

    async def clear_merged_admission_acks(self, context: MessageContext) -> None:
        """Clear the receipts of every Delivery merged into this one Turn.

        Deliveries without a native message id (quick-reply callbacks) can be
        merged into a single Turn, but only the FIRST one hydrates the dispatch
        context. The others are accepted as part of that Turn and never
        dispatched on their own, so their 👌 would stay up forever unless this
        Turn clears their reaction targets too.
        """

        payload = context.platform_specific or {}
        targets = payload.get("delivery_ack_targets") if isinstance(payload, dict) else None
        if not isinstance(targets, (list, tuple)) or not targets:
            return
        own_target = self._reaction_target_message_id(context)
        for target in targets:
            target_id = str(target or "").strip()
            if not target_id or target_id == own_target:
                continue
            merged = copy.copy(context)
            spec = dict(payload)
            spec["processing_indicator_message_id"] = target_id
            merged.platform_specific = spec
            await self.clear_admission_ack(merged)

    async def start(self, context: MessageContext, agent_name: str, *, enabled: bool = True) -> ProcessingIndicatorHandle:
        handle = ProcessingIndicatorHandle(
            context=context,
            terminal_reaction_message_id=self._reaction_target_message_id(context),
        )
        # A queued input carries an admission receipt (👌) that this turn's own
        # indicator now replaces. Platforms that stack reactions would otherwise
        # show both, and the receipt would outlive the wait it described.
        await self.clear_admission_ack(context)
        await self.clear_merged_admission_acks(context)
        if not enabled:
            return handle

        if self._concise_status_bubble_active(context):
            # The concise status bubble is the primary progress indicator, but we
            # still want the lightweight reaction ack AND typing keepalive (both
            # best-effort, both cleaned up on finish). We only drop the ack MESSAGE
            # mode — a separate text bubble would duplicate the status bubble and
            # can't be deleted on Slack. (B2)
            #
            # The reaction is SELECTED here but ADDED at the runtime gate so a
            # message waiting behind a running turn shows the queued 👌 and only
            # flips to 👀 when its turn truly starts (show_queued_reaction /
            # promote_reaction_to_running). Typing stays eager for immediate feedback.
            capabilities = self._capabilities(context)
            if self._mode_supported(capabilities, "reaction", context):
                handle.reaction_indicator_selected = True
            if self._mode_supported(capabilities, "typing", context):
                await self._start_typing_indicator(handle)
            return handle

        for mode in self._processing_modes(context):
            if mode == "message" and await self._start_message_indicator(handle, agent_name):
                return handle
            if mode == "typing" and await self._start_typing_indicator(handle):
                return handle
            if mode == "reaction":
                # Reaction is the selected mode but is added at the runtime gate
                # (queued 👌 → running 👀), not eagerly here. Selecting it preserves
                # the single-mode "first match wins" contract: we stop trying lower
                # modes just as if the reaction had been applied.
                handle.reaction_indicator_selected = True
                return handle

        return handle

    async def _start_message_indicator(self, handle: ProcessingIndicatorHandle, agent_name: str) -> bool:
        ack_context = self.target_context(handle.context)
        try:
            ack_message_id = await self._get_im_client(ack_context).send_message(
                ack_context,
                self._get_ack_text(agent_name),
            )
        except Exception as ack_err:
            logger.debug("Failed to send ack message: %s", ack_err)
            return False

        if not ack_message_id:
            logger.info("Ack message not applied (platform returned empty message id)")
            return False

        handle.ack_message_id = ack_message_id
        handle.ack_message_channel_id = ack_context.channel_id
        return True

    async def _start_typing_indicator(self, handle: ProcessingIndicatorHandle) -> bool:
        context = handle.context
        im_client = self._get_im_client(context)
        try:
            ok = await im_client.send_typing_indicator(context)
        except Exception as ack_err:
            logger.debug("Failed to send typing ack: %s", ack_err)
            return False

        if not ok:
            logger.info("Typing indicator not applied (platform returned False)")
            return False

        handle.typing_indicator_active = True
        handle.typing_indicator_task = asyncio.create_task(self._typing_keepalive_loop(context))
        return True

    async def _start_reaction_indicator(
        self,
        handle: ProcessingIndicatorHandle,
        *,
        emoji: str = ACK_REACTION_EMOJI,
    ) -> bool:
        context = handle.context
        message_id = self._reaction_target_message_id(context)
        if not message_id:
            return False
        im_client = self._get_im_client(context)
        try:
            ok = await im_client.add_reaction(context, message_id, emoji)
        except Exception as ack_err:
            logger.debug("Failed to add reaction ack: %s", ack_err)
            return False

        if not ok:
            logger.info("Ack reaction not applied (platform returned False)")
            return False

        handle.ack_reaction_message_id = message_id
        handle.ack_reaction_emoji = emoji
        return True

    async def stamp_orphaned_terminal_reaction(
        self,
        context: MessageContext,
        message_id: str,
        terminal_emoji: str,
    ) -> bool:
        """Retire a running 👀 left behind by a runtime that died mid-turn.

        The in-memory handle does NOT survive a service restart — only the
        OpenCode poll loop snapshots one, and it restores after the promotion to
        👀 — so a turn whose process disappeared leaves its running reaction on
        the user's message with nothing left to clear it. Recovery recovers the
        originating message id from the durable ledger, so it can finish the
        lifecycle the dead process could not: drop the stale 👀 and leave the
        terminal receipt in its place.

        Both halves are best-effort and independent. A platform that already lost
        the reaction (message deleted, history trimmed) must still get the
        terminal receipt attempted, so the removal failing does not skip it.
        """

        if not message_id or not terminal_emoji:
            return False
        im_client = self._get_im_client(context)
        try:
            await im_client.remove_reaction(context, message_id, ACK_REACTION_EMOJI)
        except Exception as err:
            logger.debug("Failed to remove orphaned ack reaction: %s", err)
        try:
            return bool(await im_client.add_reaction(context, message_id, terminal_emoji))
        except Exception as err:
            logger.debug("Failed to stamp orphaned terminal reaction: %s", err)
            return False

    async def _stamp_terminal_reaction_without_indicator(
        self,
        handle: ProcessingIndicatorHandle,
        terminal_emoji: str,
    ) -> bool:
        """Leave the terminal receipt when the turn's indicator was not a reaction.

        There is no 👀 to replace here, so nothing is removed — the receipt is
        added to the message that started the turn. Capability-gated and
        best-effort: a platform without reactions (WeChat) or without a target
        message id simply ends with no receipt, same as before.
        """

        context = handle.context
        message_id = handle.terminal_reaction_message_id
        if not message_id or not self._capabilities(context).supports_reaction_indicator:
            return False
        try:
            return bool(await self._get_im_client(context).add_reaction(context, message_id, terminal_emoji))
        except Exception as err:
            logger.debug("Failed to add terminal reaction without an indicator: %s", err)
            return False

    def _resolve_handle(self, request_or_handle: Any) -> tuple[ProcessingIndicatorHandle, Optional[Any]]:
        if isinstance(request_or_handle, ProcessingIndicatorHandle):
            return request_or_handle, None
        request = request_or_handle
        return self.handle_from_request(request), request

    @staticmethod
    def _sync_reaction_to_request(handle: ProcessingIndicatorHandle, request: Optional[Any]) -> None:
        # Keep the request's parallel reaction fields in lockstep with the handle.
        # handle_from_request prefers the live handle object, but the OpenCode poll
        # snapshot path reads request.ack_reaction_* directly, so they must not drift.
        if request is not None:
            request.ack_reaction_message_id = handle.ack_reaction_message_id
            request.ack_reaction_emoji = handle.ack_reaction_emoji

    async def show_queued_reaction(self, request_or_handle: Any) -> bool:
        """Add the queued 👌 reaction for a message waiting behind a running turn.

        Acts only when the reaction indicator is the selected mode for this turn and
        no reaction is shown yet. Returns True only when 👌 was actually applied, so
        the caller knows it must be cleaned up if the queued message is cancelled
        before it ever runs.
        """
        handle, request = self._resolve_handle(request_or_handle)
        if not handle.reaction_indicator_selected:
            return False
        if handle.ack_reaction_emoji:
            return False
        applied = await self._start_reaction_indicator(handle, emoji=QUEUED_REACTION_EMOJI)
        if applied:
            self._sync_reaction_to_request(handle, request)
        return applied

    async def promote_reaction_to_running(self, request_or_handle: Any, *, agent_name: Optional[str] = None) -> None:
        """Switch the reaction to the running 👀 when this turn actually starts.

        queued 👌 shown -> remove 👌 then add 👀; nothing shown yet (non-busy fast
        path) -> add 👀; already 👀 -> no-op (idempotent). No-op when the reaction
        indicator is not the selected mode. If the 👀 add fails at runtime, fall back
        through the remaining indicator modes (``agent_name`` labels the ack message).
        """
        handle, request = self._resolve_handle(request_or_handle)
        if not handle.reaction_indicator_selected:
            return
        if handle.ack_reaction_emoji == ACK_REACTION_EMOJI:
            return
        if handle.ack_reaction_emoji == QUEUED_REACTION_EMOJI and handle.ack_reaction_message_id:
            removed = False
            try:
                removed = bool(
                    await self._get_im_client(handle.context).remove_reaction(
                        handle.context,
                        handle.ack_reaction_message_id,
                        handle.ack_reaction_emoji,
                    )
                )
            except Exception as err:
                logger.debug("Failed to remove queued reaction on promote: %s", err)
            if not removed:
                # The queued 👌 is still on the message. Keep owning it on the handle
                # (do NOT clear the fields and do NOT stack 👀 on top) so finish() can
                # still remove it on the terminal result. Better a stale 👌 that gets
                # cleaned up than a leaked one or two reactions at once.
                return
            handle.ack_reaction_message_id = None
            handle.ack_reaction_emoji = None
        applied = await self._start_reaction_indicator(handle, emoji=ACK_REACTION_EMOJI)
        if not applied and not handle.typing_indicator_active and not handle.ack_message_id:
            # The reaction add failed at runtime (e.g. missing reactions scope or a
            # transient API error). Because start() deferred the reaction, the normal
            # mode loop never tried a lower candidate — fall through the remaining
            # modes here so the user still gets a processing ack instead of none (P2):
            # typing first when supported, else the ack message (e.g. Lark/Feishu
            # support reactions + message but NOT typing). Best-effort, capability-gated.
            capabilities = self._capabilities(handle.context)
            fell_back = False
            if self._mode_supported(capabilities, "typing", handle.context):
                fell_back = await self._start_typing_indicator(handle)
            if not fell_back and self._mode_supported(capabilities, "message", handle.context):
                fell_back = await self._start_message_indicator(handle, agent_name or "")
            if fell_back:
                self._warn_ack_mode_downgraded(
                    handle.context,
                    "typing" if handle.typing_indicator_active else "message",
                )
        self._sync_reaction_to_request(handle, request)

    def _warn_ack_mode_downgraded(self, context: MessageContext, applied_mode: str) -> None:
        """Report that the configured ack mode failed and a lower one was used.

        The fallback ladder is deliberately silent about *which* mode won, so a
        user who picked ``reaction`` and keeps seeing an ack message has nothing
        to go on. Warn once per downgraded turn, naming the channel — a DM whose
        channel_id is a user id is the usual cause.
        """

        configured = str(getattr(self.config, "ack_mode", "typing") or "typing")
        if configured != "reaction":
            return
        logger.warning(
            "Ack mode 'reaction' failed for %s channel=%s; downgraded to '%s'. "
            "Check that the bot can react in this conversation (reactions:write, membership, "
            "and a real channel id — a DM context must not carry the user id).",
            context.platform or "unknown",
            context.channel_id,
            applied_mode,
        )

    @staticmethod
    def _turn_tokens(context: MessageContext) -> set[str]:
        payload = context.platform_specific or {}
        tokens = set()
        for key in ("turn_token", "agent_runtime_turn_token"):
            token = str(payload.get(key) or "").strip()
            if token:
                tokens.add(token)
        return tokens

    def track_turn(self, context: MessageContext, request_or_handle: Any) -> None:
        """Remember this turn's indicator for terminal-result cleanup.

        Backends still clean up explicitly with their request object. This registry
        is the outbound terminal fallback: a result emit can recover the original
        handle by turn token even when a backend terminal branch lost the request.
        """

        for token in self._turn_tokens(context):
            self._indicators_by_turn_token[token] = request_or_handle

    def _forget_turn(self, context: MessageContext, handle: ProcessingIndicatorHandle) -> None:
        for token in self._turn_tokens(context):
            tracked = self._indicators_by_turn_token.get(token)
            if tracked is handle or getattr(tracked, "processing_indicator", None) is handle:
                self._indicators_by_turn_token.pop(token, None)

    async def finish_terminal_turn(self, context: MessageContext) -> None:
        """Finish the processing indicator for a terminal result emit."""

        for token in self._turn_tokens(context):
            tracked = self._indicators_by_turn_token.pop(token, None)
            if tracked is not None:
                await self.finish(tracked)
                return

    def _delete_context(self, handle: ProcessingIndicatorHandle, channel_id: Optional[str]) -> MessageContext:
        target_channel_id = channel_id or handle.ack_message_channel_id
        if target_channel_id and target_channel_id != handle.context.channel_id:
            return replace(handle.context, channel_id=target_channel_id)
        return handle.context

    def _should_delete_ack_message(self, handle: ProcessingIndicatorHandle) -> bool:
        return self._capabilities(handle.context).supports_message_indicator_delete

    def _should_clear_typing_indicator(self, handle: ProcessingIndicatorHandle) -> bool:
        return self._capabilities(handle.context).typing_indicator_requires_clear

    async def _delete_ack_message_for_handle(
        self,
        handle: ProcessingIndicatorHandle,
        *,
        request: Optional[Any] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        ack_id = handle.ack_message_id
        if not ack_id:
            return
        if self._should_delete_ack_message(handle):
            im_client = self._get_im_client(handle.context)
            if hasattr(im_client, "delete_message"):
                try:
                    await im_client.delete_message(self._delete_context(handle, channel_id), ack_id)
                except Exception as err:
                    logger.debug("Could not delete ack message: %s", err)
        handle.ack_message_id = None
        if request is not None:
            request.ack_message_id = None

    def apply_to_request(self, request: Any, handle: ProcessingIndicatorHandle) -> None:
        request.processing_indicator = handle
        request.ack_message_id = handle.ack_message_id
        request.ack_reaction_message_id = handle.ack_reaction_message_id
        request.ack_reaction_emoji = handle.ack_reaction_emoji
        request.terminal_reaction_message_id = handle.terminal_reaction_message_id
        request.typing_indicator_active = handle.typing_indicator_active
        request.typing_indicator_task = handle.typing_indicator_task

    def handle_from_request(self, request: Any) -> ProcessingIndicatorHandle:
        handle = getattr(request, "processing_indicator", None)
        if isinstance(handle, ProcessingIndicatorHandle):
            handle.ack_message_id = handle.ack_message_id or getattr(request, "ack_message_id", None)
            handle.ack_reaction_message_id = handle.ack_reaction_message_id or getattr(
                request,
                "ack_reaction_message_id",
                None,
            )
            handle.ack_reaction_emoji = handle.ack_reaction_emoji or getattr(request, "ack_reaction_emoji", None)
            handle.terminal_reaction_message_id = handle.terminal_reaction_message_id or getattr(
                request,
                "terminal_reaction_message_id",
                None,
            )
            handle.typing_indicator_active = handle.typing_indicator_active or bool(
                getattr(request, "typing_indicator_active", False)
            )
            handle.typing_indicator_task = handle.typing_indicator_task or getattr(request, "typing_indicator_task", None)
            return handle
        return ProcessingIndicatorHandle(
            context=request.context,
            ack_message_id=getattr(request, "ack_message_id", None),
            ack_message_channel_id=getattr(request.context, "channel_id", None),
            ack_reaction_message_id=getattr(request, "ack_reaction_message_id", None),
            ack_reaction_emoji=getattr(request, "ack_reaction_emoji", None),
            terminal_reaction_message_id=getattr(request, "terminal_reaction_message_id", None),
            typing_indicator_active=bool(getattr(request, "typing_indicator_active", False)),
            typing_indicator_task=getattr(request, "typing_indicator_task", None),
        )

    def handle_from_snapshot(self, data: dict[str, Any]) -> ProcessingIndicatorHandle:
        return ProcessingIndicatorHandle.from_snapshot(data)

    def snapshot_request(self, request: Any) -> dict[str, Any]:
        return self.handle_from_request(request).to_snapshot()

    async def delete_ack_message(self, request: Any, *, channel_id: Optional[str] = None) -> None:
        handle = self.handle_from_request(request)
        await self._delete_ack_message_for_handle(handle, request=request, channel_id=channel_id)
        if getattr(request, "processing_indicator", None) is None:
            request.processing_indicator = handle

    async def finish(
        self,
        request_or_handle: Any,
        *,
        terminal_emoji: Optional[str] = None,
    ) -> None:
        """Clear the turn's indicator, optionally leaving a terminal receipt.

        ``terminal_emoji`` replaces the running 👀 rather than clearing it, for
        the endings that produce no result of their own (see
        ``STOPPED_REACTION_EMOJI`` / ``INTERRUPTED_REACTION_EMOJI``). Omitting it
        keeps the historical behavior — a turn that emitted a result needs no
        second receipt. Adding the replacement is best-effort: the removal is
        what the handle's bookkeeping is keyed on, so a platform that rejects the
        new emoji still ends with a cleanly cleared indicator.
        """

        if isinstance(request_or_handle, ProcessingIndicatorHandle):
            handle = request_or_handle
            request = None
        else:
            request = request_or_handle
            handle = self.handle_from_request(request)

        await self._delete_ack_message_for_handle(handle, request=request)

        typing_task = handle.typing_indicator_task
        if typing_task is not None:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Failed to stop typing keepalive task", exc_info=True)
            finally:
                handle.typing_indicator_task = None
                if request is not None:
                    request.typing_indicator_task = None

        if handle.typing_indicator_active and self._should_clear_typing_indicator(handle):
            try:
                await self._get_im_client(handle.context).clear_typing_indicator(handle.context)
            except Exception as err:
                logger.debug("Failed to clear typing indicator: %s", err)

        if handle.typing_indicator_active:
            handle.typing_indicator_active = False
            if request is not None:
                request.typing_indicator_active = False

        if terminal_emoji and not handle.ack_reaction_emoji:
            # The receipt is not a *reaction* feature, it is the only trace a
            # silent ending leaves. ack_mode defaults to 'typing' on every
            # platform that has it, so gating the stamp on "the indicator
            # happened to be a reaction" would make ⏹️/⚠️ invisible for the
            # default configuration — exactly the turns that emit no result.
            # Stamp on the originating message instead, wherever reactions work.
            await self._stamp_terminal_reaction_without_indicator(handle, terminal_emoji)

        if handle.ack_reaction_message_id and handle.ack_reaction_emoji:
            reaction_message_id = handle.ack_reaction_message_id
            removed = False
            try:
                removed = bool(
                    await self._get_im_client(handle.context).remove_reaction(
                        handle.context,
                        handle.ack_reaction_message_id,
                        handle.ack_reaction_emoji,
                    )
                )
            except Exception as err:
                logger.debug("Failed to remove reaction ack: %s", err)
            else:
                # Adapters report a failed removal by RETURNING False (Slack,
                # Discord, Telegram, Feishu) as often as by raising, and
                # ``promote_reaction_to_running`` already treats that value as
                # authoritative. Stamping on an unremoved 👀 would leave the
                # message showing running AND stopped at once, so the receipt is
                # owed only to a removal that actually happened.
                if terminal_emoji and removed:
                    try:
                        await self._get_im_client(handle.context).add_reaction(
                            handle.context,
                            reaction_message_id,
                            terminal_emoji,
                        )
                    except Exception as err:
                        logger.debug("Failed to add terminal reaction: %s", err)
            finally:
                handle.ack_reaction_message_id = None
                handle.ack_reaction_emoji = None
                if request is not None:
                    request.ack_reaction_message_id = None
                    request.ack_reaction_emoji = None

        if request is not None and getattr(request, "processing_indicator", None) is None:
            request.processing_indicator = handle

        self._forget_turn(handle.context, handle)
