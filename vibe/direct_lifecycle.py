"""One Web UI-loop owner for the optional direct path; never owns the tunnel.

Configuration and backend observations invalidate an in-flight start on the
owning loop. Only the runner starts/stops resources. Blocking certificate and
configuration work runs off-loop; neither startup nor local withdrawal waits
for a backend reply. Worker results carry the generation that authorized them.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from config.v2_config import V2Config
from vibe.direct_certificates import CertificateResult, DirectCertificateClient
from vibe.direct_rendezvous import DirectRendezvous, rendezvous as default_rendezvous

logger = logging.getLogger(__name__)
PERMISSION_MAX_AGE_SECONDS = 15 * 60
CONFIG_POLL_INTERVAL_SECONDS = 0.5
BINARY_REFRESH_INTERVAL_SECONDS = 5.0
BINARY_OBSERVATION_MAX_AGE_SECONDS = 15.0
TICK_SECONDS = 0.1
_MANAGED_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-app\.avibe\.bot$")
DIRECT_STATES = frozenset({
    "disabled", "waiting-permission", "waiting-certificate", "transport-not-installed",
    "starting", "running", "stopping", "error",
})


@dataclass(frozen=True, slots=True)
class DirectAccessStatus:
    """Public projection: no address, endpoint key, token, or key-material path."""

    enabled: bool
    state: str
    reason: str
    certificate_not_after: str | None = None
    transport_running: bool = False
    direct_access_allowed: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "state": self.state, "reason": self.reason,
            "certificate_not_after": self.certificate_not_after,
            "transport_running": self.transport_running,
            "direct_access_allowed": self.direct_access_allowed,
        }


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Local request ticket, never a wire field. A signature alone cannot fence ABA."""

    generation: int
    signature: tuple[Any, ...]
    sequence: int


BinaryResolver = Callable[[V2Config], str | Path | None]


def resolve_direct_transport_binary(config: V2Config) -> Path | None:
    """Worker-only: inspect the exact packaged pin; never install or run it."""
    if not config.remote_access.vibe_cloud.direct or not _is_paired(config) or managed_direct_hostname(config) is None:
        return None
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    return DirectTransportRuntimeManager().resolve_binary()


def managed_direct_hostname(config: V2Config) -> str | None:
    try:
        parsed = urlsplit(config.remote_access.vibe_cloud.public_url)
        hostname = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not _MANAGED_HOSTNAME.fullmatch(hostname)):
            return None
        return hostname
    except (ValueError, TypeError):
        return None


def _config_signature(config: V2Config) -> tuple[Any, ...]:
    from vibe import remote_access

    cloud = config.remote_access.vibe_cloud
    return (
        bool(cloud.direct), cloud.instance_id, cloud.public_url,
        remote_access._pairing_identity_fingerprint(remote_access._pairing_identity_from_cloud(cloud)),
    )


def _is_paired(config: V2Config | None) -> bool:
    return config is not None and config.remote_access.vibe_cloud.runtime_credentials() is not None


def _default_ingress_factory(app: Any, **kwargs: Any) -> Any:
    from vibe.direct_ingress import DirectIngress
    return DirectIngress(app, **kwargs)


def _default_supervisor_factory(executable: str, ingress: Any, rendezvous: DirectRendezvous) -> Any:
    from vibe.direct_transport import DirectTransportSupervisor
    return DirectTransportSupervisor(executable, ingress, rendezvous)


def _certificate_usable(result: CertificateResult | None) -> bool:
    if result is None or not result.ready or not result.certificate_file or not result.key_file:
        return False
    try:
        expiry = datetime.fromisoformat(result.certificate_not_after.replace("Z", "+00:00"))
        return expiry.tzinfo is not None and expiry.timestamp() > time.time()
    except (ValueError, AttributeError, TypeError, OverflowError):
        return False


def _subprocess_loop_supported() -> bool:
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return sys.platform != "win32" or (proactor is not None and isinstance(asyncio.get_running_loop(), proactor))


class _OwnedRendezvous:
    """Generation-local publication view; the shared rendezvous stays unchanged."""

    def __init__(self, lifecycle: DirectAccessLifecycle, generation: int, ingress: Any) -> None:
        self._owner, self._generation, self._ingress = lifecycle, generation, ingress

    def publish(self, endpoint: Any, *, reason: str = "not_running") -> None:
        if endpoint is not None and not self._owner._can_commit(self._generation, self._ingress):
            return
        self._owner._rendezvous.publish(endpoint, reason=reason)

    def set_admission_listener(self, listener: Any) -> None:
        if listener is not None and not self._owner._can_commit(self._generation, self._ingress):
            return
        self._owner._rendezvous.set_admission_listener(listener)


class DirectAccessLifecycle:
    def __init__(
        self, app: Any, *, certificate_client: Any = None,
        binary_resolver: BinaryResolver | None = None,
        ingress_factory: Callable[..., Any] | None = None,
        supervisor_factory: Callable[..., Any] | None = None,
        rendezvous: DirectRendezvous | None = None,
        config_loader: Callable[[], V2Config] | None = None,
    ) -> None:
        self._app = app
        self._certificate_client = certificate_client or DirectCertificateClient()
        self._binary_resolver = binary_resolver or resolve_direct_transport_binary
        self._ingress_factory = ingress_factory or _default_ingress_factory
        self._supervisor_factory = supervisor_factory or _default_supervisor_factory
        self._rendezvous = rendezvous or default_rendezvous
        self._config_loader = config_loader or V2Config.load
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: asyncio.Task | None = None
        self._config_task: asyncio.Task | None = None
        self._config_read_task: asyncio.Task | None = None
        self._binary_task: asyncio.Task | None = None
        self._binary: str | None = None
        self._binary_valid_until = 0.0
        self._next_binary_check = 0.0
        self._binary_timer: asyncio.TimerHandle | None = None
        self._start_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._preparation_task: asyncio.Task | None = None
        self._permission_timer: asyncio.TimerHandle | None = None
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._shutdown = False
        self._pairing_suspended = False
        self._config: V2Config | None = None
        self._signature: tuple[Any, ...] | None = None
        self._generation = 0
        self._permission_allowed: bool | None = None
        self._permission_received_at: float | None = None
        self._permission_reason = "permission_required"
        self._resume_required = False
        self._request_sequence = 0
        self._last_response_sequence = 0
        self._certificate_result: CertificateResult | None = None
        self._poll_at: float | None = 0.0
        self._retry_cancelled = False
        self._cancelled_retry_used = False
        self._ingress: Any = None
        self._supervisor: Any = None
        self._resource_generation: int | None = None
        self._failed_generation: int | None = None
        self._state_lock = threading.Lock()
        self._status = DirectAccessStatus(False, "disabled", "setting_off")

    async def start(self) -> None:
        """Scheduling only: no config load, HTTP, key I/O, or listener awaited."""
        if self._runner is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._shutdown = False
        self._next_binary_check = 0.0
        self._runner = asyncio.create_task(self._run(), name="direct-access-lifecycle")
        self._config_task = asyncio.create_task(self._watch_config(), name="direct-access-config")

    async def stop(self) -> None:
        if self._runner is None:
            return
        self._shutdown = True
        self._invalidate()
        await self._runner
        self._runner = None
        if self._config_task is not None:
            self._config_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._config_task
            self._config_task = None
        # Do not cancel the worker wrapper: cancellation cannot stop synchronous
        # work. Its generation is obsolete; its eventual result is discarded.
        if self._permission_timer is not None:
            self._permission_timer.cancel()
        self._clear_binary()
        self._loop = None
        _unregister(self)

    def _invalidate(self) -> None:
        with self._state_lock:
            self._generation += 1
        self._failed_generation = None
        self._resource_generation = None
        # A worker from the retired generation can have replaced the on-disk
        # chain before its result is fenced. Never restart from cached old paths:
        # the current generation must observe the certificate client afresh.
        self._certificate_result = None
        self._poll_at = 0.0
        if self._start_task is not None:
            self._start_task.cancel()
        self._rendezvous.publish(None, reason="state_changed")
        self._wake.set()

    def notify_config_changed(self, config: V2Config) -> None:
        # The caller may reuse/mutate its object after returning. Never enqueue it.
        self._submit(self._observe_config, copy.deepcopy(config))

    def _observe_config(self, config: V2Config) -> None:
        if self._shutdown:
            return
        signature = _config_signature(config)
        if signature == self._signature:
            return
        previous = self._signature
        self._next_binary_check = 0.0
        self._clear_binary()
        self._invalidate()
        with self._state_lock:
            self._config = config
            self._signature = signature
        self._pairing_suspended = False
        self._permission_allowed = None
        self._permission_received_at = None
        self._permission_reason = "permission_required"
        self._resume_required = previous is not None
        if previous is None or signature[1:] != previous[1:]:
            self._cancelled_retry_used = False
        self._retry_cancelled = False
        # Existing report worker serializes delivery. Local stop never waits for it.
        if previous is None or previous != signature:
            from vibe import remote_access
            remote_access._report_runtime_status_async(
                copy.deepcopy(config),
                event="direct_paths_enabled" if signature[0] else "direct_paths_disabled",
            )

    def runtime_status_request(self, config: V2Config) -> PermissionRequest | None:
        signature = _config_signature(config)
        with self._state_lock:
            if self._shutdown or signature != self._signature or not signature[0]:
                return None
            self._request_sequence += 1
            return PermissionRequest(self._generation, signature, self._request_sequence)

    def submit_runtime_status_response(
        self, ticket: PermissionRequest | None, result: dict[str, Any],
    ) -> None:
        """Accept only the ticket captured before sending an opted-in report."""
        if isinstance(ticket, PermissionRequest):
            self._submit(self._observe_permission, ticket, dict(result), time.monotonic())

    def _observe_permission(self, ticket: PermissionRequest, result: dict[str, Any], received: float) -> None:
        if (ticket.generation != self._generation or ticket.signature != self._signature
                or ticket.sequence <= self._last_response_sequence or self._pairing_suspended):
            return
        self._last_response_sequence = ticket.sequence
        allowed = result.get("direct_access_allowed") is True
        if not allowed:
            self._deny("permission_denied")
        else:
            self._resume_required |= self._permission_allowed is False
            self._permission_allowed = True
            self._permission_received_at = received
            self._permission_reason = "permission_allowed"
            if self._permission_timer is not None:
                self._permission_timer.cancel()
            remaining = max(0.0, received + PERMISSION_MAX_AGE_SECONDS - time.monotonic())
            self._permission_timer = asyncio.get_running_loop().call_later(
                remaining, self._expire_permission,
            )
            self._wake.set()

    def _expire_permission(self) -> None:
        if not self._shutdown and self._permission_allowed is True and not self._permission_fresh():
            self._deny("permission_expired")

    def _deny(self, reason: str) -> None:
        self._invalidate()
        self._permission_allowed = False
        self._permission_received_at = None
        self._permission_reason = reason
        self._resume_required = True
        self._rendezvous.publish(None, reason=reason)

    def _permission_fresh(self) -> bool:
        return (self._permission_allowed is True and self._permission_received_at is not None
                and 0 <= time.monotonic() - self._permission_received_at < PERMISSION_MAX_AGE_SECONDS)

    async def prepare_pairing_change(self) -> None:
        """Fence starts, then let the one resource owner stop before key discard.

        Does not load config or await a certificate worker: the writer already
        holds the config lock. A failed config commit deliberately stays closed.
        """
        self._pairing_suspended = True
        self._deny("pairing_changed")
        self._stopped.clear()
        self._wake.set()
        await self._stopped.wait()

    def stop_for_pairing_change(self, timeout_seconds: float = 10.0) -> bool:
        loop = self._loop
        if loop is None:
            return True
        try:
            if asyncio.get_running_loop() is loop:
                return False
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self.prepare_pairing_change(), loop)
        try:
            future.result(timeout=timeout_seconds)
            return True
        except Exception:
            # Do not cancel the stop: a timed-out writer must not leave a child.
            return False

    def _load_config(self, generation: int) -> tuple[int, V2Config]:
        return generation, copy.deepcopy(self._config_loader())

    async def _watch_config(self) -> None:
        while not self._shutdown:
            try:
                # Config observation must not wait for binary hashing: CLI
                # opt-out is a disk-only write, without the Web notification.
                if self._config_read_task is None:
                    self._config_read_task = asyncio.create_task(asyncio.to_thread(
                        self._load_config, self._generation,
                    ), name="direct-config-observation")
                    self._config_read_task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None,
                    )
                generation, config = await asyncio.shield(self._config_read_task)
                if generation == self._generation and not self._shutdown:
                    self._observe_config(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Permission AND binary observations expire independently.
            finally:
                if self._config_read_task is not None and self._config_read_task.done():
                    self._config_read_task = None
            await asyncio.sleep(CONFIG_POLL_INTERVAL_SECONDS)

    def _resolve_binary(self, generation: int, started: float, config: V2Config) -> tuple[int, float, str | None]:
        binary = None
        try:
            resolved = self._binary_resolver(config)
            binary = str(resolved) if resolved else None
        except Exception:
            pass  # An unreadable optional runtime is unavailable, not a Web UI failure.
        return generation, started, binary

    def _binary_check_done(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        self._wake.set()

    def _refresh_binary_observation(self) -> None:
        # The loop only collects/schedules work. One retained binary slot stays
        # occupied until the actual worker ends, across opt-out and stop/start.
        # Neither this worker nor its completion can block the config observer.
        if self._binary_task is not None and self._binary_task.done():
            task, self._binary_task = self._binary_task, None
            if not task.cancelled():
                generation, started, binary = task.result()
                if generation == self._generation and not self._shutdown:
                    self._observe_binary(binary, started)
        config = self._config
        if (self._binary_task is not None or self._shutdown or self._pairing_suspended
                or config is None or not config.remote_access.vibe_cloud.direct
                or not _is_paired(config) or managed_direct_hostname(config) is None
                or self._loop.time() < self._next_binary_check):
            return
        self._binary_task = asyncio.create_task(asyncio.to_thread(
            self._resolve_binary, self._generation, self._loop.time(), copy.deepcopy(config),
        ), name="direct-binary-observation")
        self._binary_task.add_done_callback(self._binary_check_done)

    def _clear_binary(self) -> None:
        self._binary = None
        self._binary_valid_until = 0.0
        if self._binary_timer is not None:
            self._binary_timer.cancel()
            self._binary_timer = None

    def _observe_binary(self, binary: str | None, started: float) -> None:
        loop = asyncio.get_running_loop()
        valid_until = started + BINARY_OBSERVATION_MAX_AGE_SECONDS
        self._next_binary_check = started + BINARY_REFRESH_INTERVAL_SECONDS
        if valid_until <= loop.time():
            binary = None
        if binary != self._binary:
            if self._binary is not None:
                self._invalidate()
            # A certificate result may have completed while the binary was
            # unavailable. Its rejection must not strand future polling.
            self._poll_at = 0.0
        self._clear_binary()
        self._binary = binary
        if binary is not None:
            self._binary_valid_until = valid_until
            self._binary_timer = loop.call_at(valid_until, self._expire_binary)
        self._wake.set()

    def _expire_binary(self) -> None:
        if not self._shutdown and self._binary is not None and self._loop.time() >= self._binary_valid_until:
            self._clear_binary()
            self._next_binary_check = 0.0
            self._invalidate()

    def _gate(self) -> tuple[str, str, str | None]:
        config = self._config
        if self._shutdown:
            return "disabled", "shutdown", None
        if config is None or not config.remote_access.vibe_cloud.direct:
            return "disabled", "setting_off", None
        if self._pairing_suspended or not _is_paired(config):
            return "error", "pairing_changed" if self._pairing_suspended else "unpaired", None
        if managed_direct_hostname(config) is None:
            return "error", "hostname_not_direct", None
        binary = self._binary if self._loop is not None and self._loop.time() < self._binary_valid_until else None
        if not binary:
            return "transport-not-installed", "transport_not_installed", None
        if not _subprocess_loop_supported():
            return "error", "windows_proactor_required", None
        if not self._permission_fresh():
            return "waiting-permission", self._permission_reason, None
        return "starting", "starting", str(binary)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                if self._permission_allowed is True and not self._permission_fresh():
                    self._deny("permission_expired")
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Optional direct failures must never terminate the owner loop
                # or escape into Web UI startup/heartbeat handling.
                with contextlib.suppress(Exception):
                    await self._stop_direct("lifecycle_error")
                self._failed_generation = self._generation
                self._set_state("error", "lifecycle_error")
                logger.warning("Direct access lifecycle transition failed")
            if self._shutdown:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _reconcile(self) -> None:
        self._refresh_binary_observation()
        state, reason, binary = self._gate()
        if binary is None:
            await self._stop_direct(reason)
            self._set_state(state, reason)
            return
        if self._resource_generation != self._generation:
            await self._stop_direct("generation_changed")
        if self._failed_generation == self._generation:
            return
        if self._poll_at is not None and time.time() >= self._poll_at and self._poll_task is None:
            self._schedule_poll()
        if not _certificate_usable(self._certificate_result):
            await self._stop_direct("certificate_not_ready")
            self._set_state("waiting-certificate", "certificate_not_ready")
            return
        if self._supervisor is None:
            generation = self._generation
            self._start_task = asyncio.create_task(self._start_direct(generation, binary))
            try:
                # Separate task permits callbacks to fence an awaiting start.
                # A timeout also bounds ingress failure; never wait on HTTP here.
                remaining_permission = max(
                    0.0, self._permission_received_at + PERMISSION_MAX_AGE_SECONDS - time.monotonic(),
                )
                await asyncio.wait_for(self._start_task, timeout=min(10.0, remaining_permission))
                if self._resource_generation != generation or self._gate()[2] is None:
                    await self._stop_direct("start_fenced")
                    return
            except asyncio.CancelledError:
                await self._stop_direct("start_fenced")
                return
            except Exception:
                await self._stop_direct("transport_start_failed")
                self._failed_generation = generation
                self._set_state("error", "transport_start_failed")
                return
            finally:
                self._start_task = None
        if self._supervisor is not None:
            diagnostics = self._supervisor.status
            self._set_state(
                "running" if diagnostics.endpoint_id else "starting",
                "published" if diagnostics.endpoint_id else (diagnostics.reason or "transport_starting"),
            )

    async def _start_direct(self, generation: int, binary: str) -> None:
        self._set_state("starting", "starting")
        result = self._certificate_result
        self._ingress = self._ingress_factory(
            self._app, hostname=managed_direct_hostname(self._config),
            certfile=result.certificate_file, keyfile=result.key_file,
        )
        ingress = self._ingress
        prepared = await self._prepare_tls(ingress._prepare_start, generation=generation, ingress=ingress)
        if not self._can_commit(generation, ingress):
            return
        await ingress._start_prepared(prepared)
        if not self._can_commit(generation, ingress):
            return
        self._supervisor = self._supervisor_factory(binary, self._ingress, _OwnedRendezvous(self, generation, ingress))
        await self._supervisor.start()
        if generation != self._generation or self._gate()[2] is None:
            return
        self._resource_generation = generation

    def _can_commit(self, generation: int, ingress: Any) -> bool:
        return (generation == self._generation and ingress is self._ingress
                and self._gate()[2] is not None and _certificate_usable(self._certificate_result))

    async def _prepare_tls(self, operation: Callable, *args: Any, generation: int, ingress: Any) -> Any:
        # One slot across ALL ingress objects and start/reload generations.
        # A cancelled waiter leaves its worker here until actual completion.
        while self._preparation_task is not None and not self._preparation_task.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(self._preparation_task)
        if generation != self._generation or ingress is not self._ingress or self._gate()[2] is None:
            raise asyncio.CancelledError
        task = asyncio.create_task(asyncio.to_thread(operation, *args), name="direct-tls-prepare")
        self._preparation_task = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _stop_direct(self, reason: str) -> None:
        self._rendezvous.publish(None, reason=reason)
        if self._supervisor is not None or self._ingress is not None:
            self._set_state("stopping", reason)
        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None
        if self._ingress is not None:
            await self._ingress.stop()
            self._ingress = None
        self._resource_generation = None
        self._rendezvous.publish(None, reason=reason)
        self._stopped.set()

    def _schedule_poll(self, *, rotate_key: bool = False) -> asyncio.Task:
        generation = self._generation
        generation_signature = self._signature
        assert generation_signature is not None
        permission_deadline = self._permission_received_at + PERMISSION_MAX_AGE_SECONDS
        resume, self._resume_required = self._resume_required, False
        retry, self._retry_cancelled = self._retry_cancelled, False
        self._poll_at = None

        async def poll() -> CertificateResult:
            try:
                options = {
                    "allowed": True,
                    "resume": resume,
                    "expected_pairing_fingerprint": generation_signature[3],
                    "permission_deadline": permission_deadline,
                }
                if retry:
                    options["retry"] = True
                if rotate_key:
                    options["rotate_key"] = True
                result = await asyncio.to_thread(self._certificate_client.poll, **options)
                if generation == self._generation and self._gate()[2] is not None:
                    await self._accept_certificate(generation, result)
                return result
            except asyncio.CancelledError:
                raise
            except Exception:
                if generation == self._generation:
                    self._poll_at = time.time() + 30.0
                return CertificateResult("unavailable", last_error_code="local_state_unavailable")
            finally:
                if generation != self._generation and self._permission_fresh():
                    self._poll_at = 0.0
                self._poll_task = None
                self._wake.set()

        self._poll_task = asyncio.create_task(poll(), name="direct-certificate-poll")
        return self._poll_task

    async def _accept_certificate(self, generation: int, result: CertificateResult) -> None:
        if generation != self._generation or self._gate()[2] is None:
            return
        if result.last_error_code == "direct_access_not_allowed":
            self._deny("permission_denied")
            return
        if result.last_error_code == "permission_expired":
            # This poll carried an older receipt's deadline. A fresh grant can
            # have arrived without changing the configuration generation. Keep
            # the installed certificate and retry under the current authority;
            # a request-local refusal is not a terminal certificate outcome.
            self._resume_required = True
            self._poll_at = time.time()
            self._wake.set()
            return
        if result.reload_required and self._ingress is not None and _certificate_usable(result):
            ingress = self._ingress
            try:
                prepared = await self._prepare_tls(
                    ingress._prepare_certificate_reload, result.certificate_file, result.key_file,
                    generation=generation, ingress=ingress,
                )
                if generation != self._generation or self._gate()[2] is None:
                    return
                if ingress is self._ingress:
                    ingress._apply_certificate_reload(prepared, result.certificate_file, result.key_file)
                # The old certificate may have expired while preparing TLS,
                # retiring this ingress without invalidating the grant. Keep
                # the valid renewal below for a fresh ingress instead of losing
                # both the certificate and the client's next polling deadline.
            except Exception:
                if generation == self._generation:
                    self._deny("certificate_reload_failed")
                return
        self._certificate_result = result
        self._poll_at = getattr(result, "next_poll_at", None)
        if getattr(result, "status", None) == "cancelled" and not self._cancelled_retry_used:
            self._cancelled_retry_used = True
            self._retry_cancelled = True
            self._poll_at = max(time.time(), getattr(result, "next_poll_at", None) or 0)
        self._wake.set()

    async def rotate_key(self) -> CertificateResult:
        """Explicit one-shot only. Return the client's conflict/error unchanged.

        In particular certificate_order_in_progress remains visible when an
        active-key backend renewal causes the client to abandon the rotation.
        Automatic rotation timing is deliberately undecided.
        """
        if self._gate()[2] is None:
            return CertificateResult("disabled", last_error_code="direct_access_not_allowed")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("direct lifecycle belongs to the Web UI loop")
        if self._poll_task is not None:
            return CertificateResult("unavailable", last_error_code="certificate_order_in_progress")
        return await asyncio.shield(self._schedule_poll(rotate_key=True))

    @property
    def direct_paths_enabled(self) -> bool:
        """Loop-only setting observation for request diagnostics, not readiness.

        Reuse the existing config observer; request admission must never load
        config or acquire the cross-thread status lock. Disk-only writers become
        visible on that observer's next successful read, not atomically here.
        """
        return not self._shutdown and self._signature is not None and bool(self._signature[0])

    def status(self) -> DirectAccessStatus:
        with self._state_lock:
            return self._status

    def _set_state(self, state: str, reason: str) -> None:
        assert state in DIRECT_STATES
        with self._state_lock:
            self._status = DirectAccessStatus(
                enabled=bool(self._signature and self._signature[0]), state=state, reason=reason,
                certificate_not_after=getattr(self._certificate_result, "certificate_not_after", None),
                transport_running=bool(self._supervisor is not None and self._supervisor.running),
                direct_access_allowed=self._permission_fresh(),
            )

    def _submit(self, callback: Callable, *args: Any) -> None:
        loop = self._loop
        if loop is not None and not self._shutdown:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(callback, *args)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: DirectAccessLifecycle | None = None


def _unregister(lifecycle: DirectAccessLifecycle) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is lifecycle:
            _ACTIVE = None


def active_lifecycle() -> DirectAccessLifecycle | None:
    with _ACTIVE_LOCK:
        return _ACTIVE


def register_active_lifecycle(lifecycle: DirectAccessLifecycle) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = lifecycle


def stop_for_pairing_change(timeout_seconds: float = 10.0) -> bool:
    lifecycle = active_lifecycle()
    return lifecycle is None or lifecycle.stop_for_pairing_change(timeout_seconds)


def notify_config_changed(config: V2Config) -> None:
    lifecycle = active_lifecycle()
    if lifecycle is not None:
        lifecycle.notify_config_changed(config)


def runtime_status_request(config: V2Config) -> PermissionRequest | None:
    lifecycle = active_lifecycle()
    return lifecycle.runtime_status_request(config) if lifecycle is not None else None


def accept_runtime_status_response(
    ticket: PermissionRequest | None,
    result: dict[str, Any],
) -> None:
    lifecycle = active_lifecycle()
    if lifecycle is not None:
        lifecycle.submit_runtime_status_response(ticket, result)


def status_for_config(config: V2Config | None) -> dict[str, Any]:
    lifecycle = active_lifecycle()
    if lifecycle is not None:
        return lifecycle.status().as_dict()
    enabled = bool(config and config.remote_access.vibe_cloud.direct)
    state, reason = "disabled", "setting_off"
    if enabled:
        if not _is_paired(config):
            state, reason = "error", "unpaired"
        elif managed_direct_hostname(config) is None:
            state, reason = "error", "hostname_not_direct"
        elif resolve_direct_transport_binary(config) is None:
            state, reason = "transport-not-installed", "transport_not_installed"
        else:
            state, reason = "waiting-permission", "owner_not_running"
    return DirectAccessStatus(enabled, state, reason).as_dict()
