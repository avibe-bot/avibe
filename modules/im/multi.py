"""Multi-platform IM runtime wrapper."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, cast

from config.v2_settings import _infer_channel_platform, _infer_user_platform, _split_scoped_key
from .base import BaseIMClient, InlineKeyboard, MessageContext

logger = logging.getLogger(__name__)


class IMClientRemovalError(RuntimeError):
    """Raised when a hot-remove cannot stop a platform runtime cleanly."""


class MultiIMClient(BaseIMClient):
    """Delegate inbound/outbound messaging across multiple IM clients."""

    def __init__(
        self,
        clients: Dict[str, BaseIMClient],
        primary_platform: str,
        auxiliary_clients: Optional[Dict[str, BaseIMClient]] = None,
    ):
        if clients and primary_platform not in clients:
            raise ValueError(f"Primary platform '{primary_platform}' is not in enabled clients")
        self.clients = clients
        self._auxiliary_clients = auxiliary_clients or {}
        self.primary_platform = primary_platform
        self._threads: Dict[str, threading.Thread] = {}
        self._run_exceptions: Dict[str, BaseException] = {}
        self._run_started = threading.Event()
        # Guards mutations of ``clients`` / ``_threads`` so the run() monitor
        # loop (IM worker thread) and runtime add/remove_client calls (the
        # reconcile path, on the asyncio loop) can't see a half-updated map.
        self._clients_lock = threading.RLock()
        self._removing_platforms: set[str] = set()
        self._stop_requested = threading.Event()
        self._ready_lock = threading.Lock()
        self._ready_platforms: set[str] = set()
        self._ready_emitted = False
        self._registered_callbacks: Optional[tuple[Optional[Callable], Optional[Dict[str, Callable]], Optional[Callable], Dict[str, Any]]] = None
        if clients:
            super().__init__(clients[primary_platform].config)
            self.formatter = clients[primary_platform].formatter
        else:
            config, formatter = self._workbench_fallback_runtime()
            super().__init__(config)
            self.formatter = formatter

    @staticmethod
    def _workbench_fallback_runtime() -> tuple[Any, Any]:
        from .avibe import AvibeConfig
        from .formatters.avibe_formatter import AvibeFormatter

        return AvibeConfig(), AvibeFormatter()

    def _use_workbench_fallback_runtime(self) -> None:
        config, formatter = self._workbench_fallback_runtime()
        self.primary_platform = "avibe"
        self.config = config
        self.formatter = formatter

    def _client_snapshot(self) -> Dict[str, BaseIMClient]:
        with self._clients_lock:
            return dict(self.clients)

    def set_auxiliary_client(self, platform: str, client: BaseIMClient) -> None:
        """Register a delivery-only client that is not part of the run loop."""
        with self._clients_lock:
            self._auxiliary_clients[platform] = client

    def _primary_client(self) -> BaseIMClient:
        with self._clients_lock:
            client = self.clients.get(self.primary_platform)
            if client is not None:
                return client
            if self.clients:
                return next(iter(self.clients.values()))
        raise ValueError("No IM platforms are enabled")

    def set_primary_platform(self, primary_platform: str) -> None:
        with self._clients_lock:
            if self.clients and primary_platform not in self.clients:
                raise ValueError(f"Primary platform '{primary_platform}' is not in enabled clients")
            self.primary_platform = primary_platform
            client = self.clients.get(primary_platform)
            if client is not None:
                self.config = client.config
                self.formatter = client.formatter
            elif not self.clients:
                self._use_workbench_fallback_runtime()

    def _resolve_platform(self, context: Optional[MessageContext] = None) -> str:
        if context is not None:
            if context.platform:
                return context.platform
            ps = context.platform_specific or {}
            platform = ps.get("platform")
            if isinstance(platform, str) and (platform in self.clients or platform in self._auxiliary_clients):
                return platform
        return self.primary_platform

    def get_client(self, platform: str) -> BaseIMClient:
        with self._clients_lock:
            client = self.clients.get(platform) or self._auxiliary_clients.get(platform)
            if client is not None:
                return client
        try:
            return self.clients[platform]
        except KeyError as exc:
            raise ValueError(f"Platform '{platform}' is not enabled") from exc

    def _resolve_platform_from_file_info(self, file_info: Dict[str, Any]) -> str:
        platform = str(file_info.get("platform") or "").strip()
        if platform in self.clients:
            return platform

        url = str(file_info.get("url") or file_info.get("url_private_download") or "")
        if "wechat" in url:
            return "wechat"
        if "discordapp" in url or "discord.com" in url:
            return "discord"
        if "slack" in url:
            return "slack"
        if "feishu" in url or "lark" in url:
            return "lark"
        return self.primary_platform

    def get_client_for_context(self, context: Optional[MessageContext] = None) -> BaseIMClient:
        return self.get_client(self._resolve_platform(context))

    def supports_question_modal(self, context: Optional[MessageContext] = None) -> bool:
        return callable(getattr(self.get_client_for_context(context), "open_question_modal", None))

    def get_default_parse_mode(self) -> Optional[str]:
        try:
            return self._primary_client().get_default_parse_mode()
        except ValueError:
            return None

    def should_use_thread_for_reply(self) -> bool:
        clients = self._client_snapshot()
        if not clients:
            return False
        return (
            len({client.should_use_thread_for_reply() for client in clients.values()}) == 1
            and self._primary_client().should_use_thread_for_reply()
        )

    def should_use_thread_for_dm_session(self) -> bool:
        clients = self._client_snapshot()
        if not clients:
            return False
        return (
            len({client.should_use_thread_for_dm_session() for client in clients.values()}) == 1
            and self._primary_client().should_use_thread_for_dm_session()
        )

    def register_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_command: Optional[Dict[str, Callable]] = None,
        on_callback_query: Optional[Callable] = None,
        **kwargs: Any,
    ):
        super().register_callbacks(
            on_message=on_message, on_command=on_command, on_callback_query=on_callback_query, **kwargs
        )

        self._registered_callbacks = (on_message, on_command, on_callback_query, dict(kwargs))
        for platform, client in self._client_snapshot().items():
            self._register_client_callbacks(platform, client)

    def _register_client_callbacks(self, platform: str, client: BaseIMClient) -> None:
        if self._registered_callbacks is None:
            return
        on_message, on_command, on_callback_query, kwargs = self._registered_callbacks
        wrapped_kwargs = {key: self._wrap_additional_callback(platform, value) for key, value in kwargs.items()}
        wrapped_kwargs["on_ready"] = self._wrap_on_ready(platform, kwargs.get("on_ready"))
        wrapped_commands: Optional[Dict[str, Callable]] = None
        if on_command is not None:
            wrapped_commands = {}
            for name, handler in on_command.items():
                wrapped = self._wrap_context_callback(platform, handler)
                if wrapped is not None:
                    wrapped_commands[name] = cast(Callable, wrapped)
        client.register_callbacks(
            on_message=self._wrap_context_callback(platform, on_message),
            on_command=wrapped_commands,
            on_callback_query=self._wrap_callback_query(platform, on_callback_query),
            **wrapped_kwargs,
        )

    def _wrap_additional_callback(self, platform: str, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None

        signature = inspect.signature(callback)
        supports_platform = "platform" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )

        async def _wrapped(*args: Any, **kwargs: Any):
            mutable_args = list(args)
            if mutable_args and isinstance(mutable_args[0], MessageContext):
                self._annotate_context(platform, mutable_args[0])
            if supports_platform and "platform" not in kwargs:
                kwargs["platform"] = platform
            await callback(*mutable_args, **kwargs)

        return _wrapped

    def _wrap_context_callback(self, platform: str, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None

        async def _wrapped(context: MessageContext, *args: Any, **kwargs: Any):
            self._annotate_context(platform, context)
            await callback(context, *args, **kwargs)

        return _wrapped

    def _wrap_callback_query(self, platform: str, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None

        async def _wrapped(context: MessageContext, *args: Any, **kwargs: Any):
            self._annotate_context(platform, context)
            await callback(context, *args, **kwargs)

        return _wrapped

    def _wrap_on_ready(self, platform: str, callback: Optional[Callable]) -> Optional[Callable]:
        if callback is None:
            return None

        async def _wrapped(*args: Any, **kwargs: Any):
            with self._ready_lock:
                self._ready_platforms.add(platform)
            if self._mark_ready_if_complete():
                await callback(*args, **kwargs)

        return _wrapped

    def _mark_ready_if_complete(self) -> bool:
        """Return True once when all currently registered clients are ready."""
        with self._ready_lock:
            client_count = len(self._client_snapshot())
            logger.info(
                "IM runtime ready progress (%d/%d)",
                len(self._ready_platforms),
                client_count,
            )
            if client_count > 0 and not self._ready_emitted and len(self._ready_platforms) >= client_count:
                self._ready_emitted = True
                return True
        return False

    def _aggregate_ready_callback(self) -> Optional[Callable]:
        if self._registered_callbacks is None:
            return None
        return self._registered_callbacks[3].get("on_ready")

    def _fire_aggregate_ready_from_thread(self, callback: Callable) -> None:
        async def _call_ready() -> None:
            result = callback()
            if inspect.isawaitable(result):
                await result

        try:
            asyncio.run(_call_ready())
        except Exception:
            logger.exception("MultiIMClient aggregate on_ready callback failed")

    def _emit_empty_ready_once(self) -> None:
        callback = getattr(self, "on_ready_callback", None)
        if callback is None:
            return
        with self._ready_lock:
            if self._ready_emitted or self.clients:
                return
            self._ready_emitted = True

        async def _call_ready() -> None:
            result = callback()
            if inspect.isawaitable(result):
                await result

        try:
            asyncio.run(_call_ready())
        except Exception:
            logger.exception("MultiIMClient empty-runtime on_ready callback failed")

    @staticmethod
    def _annotate_context(platform: str, context: MessageContext) -> None:
        context.platform = platform
        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific.setdefault("platform", platform)

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> str:
        # Forward ``subtext`` only when set: it is a concise-status-bubble extra
        # that only Slack/Discord implement; passing it (even as None) to adapters
        # that don't declare the kwarg would raise TypeError.
        kwargs = {"parse_mode": parse_mode, "reply_to": reply_to}
        if subtext is not None:
            kwargs["subtext"] = subtext
        return await self.get_client_for_context(context).send_message(context, text, **kwargs)

    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> str:
        # Forward subtext only when set, so adapters that don't declare the kwarg
        # aren't handed it (mirrors send_message / edit_message forwarding).
        kwargs = {"parse_mode": parse_mode}
        if subtext is not None:
            kwargs["subtext"] = subtext
        return await self.get_client_for_context(context).send_message_with_buttons(
            context, text, keyboard, **kwargs
        )

    def supports_message_editing(self, context: Optional[MessageContext] = None) -> bool:
        if context is None:
            clients = self._client_snapshot()
            return bool(clients) and all(client.supports_message_editing() for client in clients.values())
        return self.get_client_for_context(context).supports_message_editing(context)

    async def upload_markdown(
        self, context: MessageContext, title: str, content: str, filetype: str = "markdown"
    ) -> str:
        return await self.get_client_for_context(context).upload_markdown(context, title, content, filetype=filetype)

    async def upload_file_from_path(self, context: MessageContext, file_path: str, title: Optional[str] = None) -> str:
        return await self.get_client_for_context(context).upload_file_from_path(context, file_path, title=title)

    async def upload_image_from_path(self, context: MessageContext, file_path: str, title: Optional[str] = None) -> str:
        return await self.get_client_for_context(context).upload_image_from_path(context, file_path, title=title)

    async def upload_video_from_path(self, context: MessageContext, file_path: str, title: Optional[str] = None) -> str:
        return await self.get_client_for_context(context).upload_video_from_path(context, file_path, title=title)

    async def download_file(
        self, file_info: Dict[str, Any], max_bytes: Optional[int] = None, timeout_seconds: int = 30
    ):
        client = self.get_client(self._resolve_platform_from_file_info(file_info))
        return await client.download_file(file_info, max_bytes=max_bytes, timeout_seconds=timeout_seconds)

    async def download_file_to_path(
        self,
        file_info: Dict[str, Any],
        target_path: str,
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ):
        client = self.get_client(self._resolve_platform_from_file_info(file_info))
        return await client.download_file_to_path(
            file_info,
            target_path,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> bool:
        kwargs = {"text": text, "keyboard": keyboard, "parse_mode": parse_mode}
        if subtext is not None:
            kwargs["subtext"] = subtext
        return await self.get_client_for_context(context).edit_message(context, message_id, **kwargs)

    async def remove_inline_keyboard(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        return await self.get_client_for_context(context).remove_inline_keyboard(
            context,
            message_id,
            text=text,
            parse_mode=parse_mode,
        )

    async def dismiss_form_message(self, context: MessageContext) -> None:
        await self.get_client_for_context(context).dismiss_form_message(context)

    async def open_question_modal(
        self,
        trigger_id: Any,
        context: MessageContext,
        pending: Any,
        callback_prefix: str = "claude_question",
    ):
        client = self.get_client_for_context(context)
        open_modal = getattr(client, "open_question_modal", None)
        if not callable(open_modal):
            return await self.send_message(
                context,
                "Modal UI is not available. Please reply with a custom message.",
            )
        return await open_modal(
            trigger_id=trigger_id,
            context=context,
            pending=pending,
            callback_prefix=callback_prefix,
        )

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        return await self.clients[self.primary_platform].answer_callback(callback_id, text=text, show_alert=show_alert)

    def register_handlers(self):
        for client in self._client_snapshot().values():
            client.register_handlers()

    def run(self):
        self._run_started.set()
        self._stop_requested.clear()
        with self._clients_lock:
            self._threads = {}
            self._run_exceptions = {}
            for platform, client in self.clients.items():
                thread = threading.Thread(target=self._run_client, args=(platform, client), daemon=True)
                thread.start()
                self._threads[platform] = thread
            if not self.clients:
                self._emit_empty_ready_once()

        try:
            # Idle-loop until an explicit stop. The runtime stays alive even when
            # no platform threads remain — that is a valid state now: hot
            # reconcile can disable every IM platform (workbench-only) or be
            # mid-rebuild, and the runtime must keep running so a platform can be
            # added back without restarting the service. (Previously this broke
            # out when ``_threads`` emptied, which — via Controller._run_im_runtime
            # calling loop.stop() — would tear the whole service down.)
            while not self._stop_requested.is_set():
                should_exit = False
                crash_exception: BaseException | None = None
                with self._clients_lock:
                    for platform, thread in list(self._threads.items()):
                        if thread.is_alive():
                            continue
                        if platform in self._removing_platforms:
                            continue
                        logger.warning("IM runtime for %s exited", platform)
                        self._threads.pop(platform, None)
                    active_platforms = [platform for platform in self.clients if platform not in self._removing_platforms]
                    live_active_threads = [
                        platform
                        for platform in active_platforms
                        if (thread := self._threads.get(platform)) is not None and thread.is_alive()
                    ]
                    if active_platforms and not live_active_threads:
                        logger.error("All enabled IM runtime threads exited")
                        crash_exception = next(
                            (self._run_exceptions[platform] for platform in active_platforms if platform in self._run_exceptions),
                            None,
                        )
                        should_exit = True
                if crash_exception is not None:
                    raise crash_exception
                if should_exit:
                    break
                time.sleep(0.5)
        finally:
            self.stop()
            for thread in list(self._threads.values()):
                thread.join(timeout=1.0)
            self._run_started.clear()

    def _run_client(self, platform: str, client: BaseIMClient) -> None:
        try:
            client.run()
        except Exception as exc:
            with self._clients_lock:
                self._run_exceptions[platform] = exc
            logger.exception("IM runtime for %s crashed", platform)

    def add_client(self, platform: str, client: BaseIMClient) -> None:
        """Start one platform's client (+ its runtime thread) at runtime.

        Used by the hot-reconcile path to enable a platform without restarting
        the service. A no-op if the platform is already present (callers rebuild
        via remove_client + add_client for credential changes).
        """
        with self._clients_lock:
            if platform in self.clients:
                logger.warning("add_client: platform %s already present; skipping", platform)
                return
            self._removing_platforms.discard(platform)
            self._run_exceptions.pop(platform, None)
            self._register_client_callbacks(platform, client)
            self.clients[platform] = client
            if self.primary_platform not in self.clients:
                self.primary_platform = platform
                self.config = client.config
                self.formatter = client.formatter
            # If the runtime isn't looping yet (pre-run / stopped), just register
            # the client — run() will start its thread. Otherwise start it now.
            if self._run_started.is_set() and not self._stop_requested.is_set():
                thread = threading.Thread(target=self._run_client, args=(platform, client), daemon=True)
                thread.start()
                self._threads[platform] = thread
        logger.info("Hot-added IM platform %s", platform)

    def remove_client(self, platform: str) -> Optional[BaseIMClient]:
        """Stop and drop one platform's client (+ join its thread) at runtime.

        Blocking (signals the client's stop and joins its thread), so the
        reconcile path calls this via ``asyncio.to_thread`` to avoid blocking the
        loop. Returns the removed client, or ``None`` if it wasn't present.
        """
        with self._clients_lock:
            client = self.clients.get(platform)
            if client is not None:
                self._removing_platforms.add(platform)
            thread = self._threads.get(platform)
        if client is None:
            return None

        try:
            stop_attr = getattr(client, "stop", None)
            if callable(stop_attr) and not inspect.iscoroutinefunction(stop_attr):
                try:
                    stop_attr()
                except Exception:
                    logger.exception("Failed to stop IM client for %s", platform)
            if thread is not None:
                thread.join(timeout=5.0)
                if thread.is_alive():
                    message = f"IM thread for {platform} did not stop within timeout"
                    logger.error("%s; hot-remove failed", message)
                    raise IMClientRemovalError(message)
            verify_stopped = getattr(client, "verify_stopped", None)
            if callable(verify_stopped) and not bool(verify_stopped()):
                message = f"IM client for {platform} did not stop all runtime resources"
                logger.error("%s; hot-remove failed", message)
                raise IMClientRemovalError(message)

            ready_callback = None
            emit_empty_ready = False
            with self._clients_lock:
                self.clients.pop(platform, None)
                self._threads.pop(platform, None)
                self._run_exceptions.pop(platform, None)
                self._removing_platforms.discard(platform)
                if self.clients:
                    if self.primary_platform not in self.clients:
                        next_platform, next_client = next(iter(self.clients.items()))
                        self.primary_platform = next_platform
                        self.config = next_client.config
                        self.formatter = next_client.formatter
                else:
                    self._use_workbench_fallback_runtime()
                    emit_empty_ready = True
        except Exception:
            with self._clients_lock:
                self._removing_platforms.discard(platform)
            raise
        with self._ready_lock:
            self._ready_platforms.discard(platform)
        if emit_empty_ready:
            self._emit_empty_ready_once()
        elif self._mark_ready_if_complete():
            ready_callback = self._aggregate_ready_callback()
        if ready_callback is not None:
            self._fire_aggregate_ready_from_thread(ready_callback)
        logger.info("Hot-removed IM platform %s", platform)
        return client

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        platform, raw_user_id = _split_scoped_key(str(user_id))
        platform = platform or _infer_user_platform(user_id)
        client = self.clients.get(platform) or self._auxiliary_clients.get(platform) or self._primary_client()
        return await client.get_user_info(raw_user_id)

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        platform, raw_channel_id = _split_scoped_key(str(channel_id))
        platform = platform or _infer_channel_platform(channel_id)
        client = self.clients.get(platform) or self._auxiliary_clients.get(platform) or self._primary_client()
        return await client.get_channel_info(raw_channel_id)

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        return await self.get_client_for_context(context).add_reaction(context, message_id, emoji)

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        return await self.get_client_for_context(context).remove_reaction(context, message_id, emoji)

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        return await self.get_client_for_context(context).send_typing_indicator(context)

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        return await self.get_client_for_context(context).clear_typing_indicator(context)

    async def delete_message(self, context: MessageContext, message_id: str) -> bool:
        delete = getattr(self.get_client_for_context(context), "delete_message", None)
        if not callable(delete):
            return False
        return await delete(context, message_id)

    async def send_dm(self, user_id: str, text: str, **kwargs):
        platform, raw_user_id = _split_scoped_key(str(user_id))
        platform = platform or _infer_user_platform(user_id)
        client = self.clients.get(platform) or self._auxiliary_clients.get(platform) or self._primary_client()
        return await client.send_dm(raw_user_id, text, **kwargs)

    def stop(self):
        self._stop_requested.set()
        for client in self._client_snapshot().values():
            stop_attr = getattr(client, "stop", None)
            if callable(stop_attr) and not inspect.iscoroutinefunction(stop_attr):
                try:
                    stop_attr()
                except Exception:
                    logger.exception("Failed to stop IM client")

    async def shutdown(self) -> None:
        self._stop_requested.set()
        self.stop()
        for thread in list(self._threads.values()):
            await asyncio.to_thread(thread.join, 2.0)

        for client in self._client_snapshot().values():
            if any(thread.is_alive() for thread in self._threads.values()):
                break
            shutdown_attr = getattr(client, "shutdown", None)
            if callable(shutdown_attr):
                try:
                    if inspect.iscoroutinefunction(shutdown_attr):
                        await shutdown_attr()
                    else:
                        shutdown_attr()
                except Exception:
                    logger.exception("Failed to shutdown IM client")

    def format_markdown(self, text: str) -> str:
        try:
            return self._primary_client().format_markdown(text)
        except ValueError:
            return text
