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
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from config.v2_config import V2Config
from vibe.connection_settings import relay_enrollment_status, suspend_elapsed
from vibe.direct_certificates import CertificateResult, DirectCertificateClient
from vibe.direct_rendezvous import DirectRendezvous, rendezvous as default_rendezvous

logger = logging.getLogger(__name__)
PERMISSION_MAX_AGE_SECONDS = 15 * 60
CONFIG_POLL_INTERVAL_SECONDS = 0.5
BINARY_REFRESH_INTERVAL_SECONDS = 5.0
BINARY_OBSERVATION_MAX_AGE_SECONDS = 15.0
INSTALL_RETRY_INITIAL_SECONDS = 30.0
INSTALL_RETRY_MAX_SECONDS = 900.0
TICK_SECONDS = 0.1
_MANAGED_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-app\.avibe\.bot$")
DIRECT_STATES = frozenset({
    "disabled", "waiting-permission", "waiting-certificate", "transport-not-installed",
    "installing", "starting", "running", "stopping", "error",
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
    streams_opened: int = 0
    refusals: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "state": self.state, "reason": self.reason,
            "certificate_not_after": self.certificate_not_after,
            "transport_running": self.transport_running,
            "direct_access_allowed": self.direct_access_allowed,
            "streams_opened": self.streams_opened, "refusals": self.refusals,
        }


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Local request ticket, never a wire field. A signature alone cannot fence ABA."""

    generation: int
    signature: tuple[Any, ...]
    sequence: int
    binding_epoch: object


def _report_fields(state: str, reason: str, certificate_error: str | None) -> dict[str, str]:
    """Wire vocabulary only. Never pass through a backend error or local diagnostic."""
    if state == "running":
        return {"direct_state": "running"}
    if reason in {"permission_denied", "permission_expired"}:
        fields = {"direct_state": "denied"}
        if reason == "permission_expired":
            fields["direct_reason"] = "permission_expired"
        return fields
    if state == "installing":
        return {"direct_state": "installing"}
    if reason == "executable_not_found":
        return {"direct_state": "install_failed", "direct_reason": "spawn"}
    if reason in {"transport_install_failed", "transport_start_failed"}:
        # A start exception can be the ingress, the child, or its handshake.
        # Do not claim a spawn/hash/download cause without producer evidence.
        return {"direct_state": "install_failed", "direct_reason": "other"}
    if state == "waiting-certificate":
        if not certificate_error or certificate_error in {"certificate_budget_queued", "certificate_order_in_progress"}:
            return {"direct_state": "certificate_pending"}
        code = {"acme_rate_limited": "rate_limited", "backend_unavailable": "provider",
                "dns_challenge_visibility_timeout": "timeout"}.get(certificate_error, "other")
        return {"direct_state": "certificate_failed", "direct_reason": code}
    # No evidence for one of the wire states yet. Absence is not a failure.
    return {}


BinaryResolver = Callable[[V2Config], str | Path | None]


def _effective_relay(config: V2Config) -> bool:
    """The user's side of the public relay: wanted unless they turned it off.

    This grants nothing by itself. The relay policy is used only while the
    server's fresh ``public_traversal_allowed`` is true (``_observe_permission``).
    No host enrollment and no local opt-in is required.
    """
    cloud = config.remote_access.vibe_cloud
    if cloud.public_relay_choice == "off":
        return False
    enrollment = relay_enrollment_status(config)
    if enrollment == 'unknown':
        return False
    if cloud.public_relay_trial or cloud.public_relay_choice == "on":
        return True
    # No recorded choice and the setting off. Before choices were recorded,
    # off was the untouched default everywhere except on a pairing the host
    # had enrolled: there it was once on, and someone turned it off.
    return enrollment in {'absent', 'nonmatch'}


def _decline_relay(private_copy: V2Config) -> None:
    """Ask about the policy without the relay, on a copy that is never saved."""
    private_copy.remote_access.vibe_cloud.set_path_choice("public_relay_trial", False)


def resolve_direct_transport_binary(config: V2Config) -> Path | None:
    """Offline status: inspect the pin required by the observed local preference."""
    return _verified_binary(config, _effective_relay(config))


def _resolve_selected_binary(config: V2Config) -> Path | None:
    # Runner supplied a private copy with this generation's policy. A second
    # marker read here could silently weaken a required public-compatible pin.
    return _verified_binary(config, config.remote_access.vibe_cloud.public_relay_trial)


def _verified_binary(config: V2Config, require_public: bool) -> Path | None:
    """Worker-only: inspect the exact packaged pin; never install or run it."""
    if not config.remote_access.vibe_cloud.direct or not _is_paired(config) or managed_direct_hostname(config) is None:
        return None
    from core.direct_transport_runtime import DirectTransportRuntimeManager

    manager = DirectTransportRuntimeManager()
    if require_public:
        return manager.resolve_binary(require_public_trial=True)
    return manager.resolve_binary()


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
        _effective_relay(config),
    )


def _is_paired(config: V2Config | None) -> bool:
    from vibe.binding_admission import reset_in_progress
    return config is not None and not reset_in_progress() and config.remote_access.vibe_cloud.runtime_credentials() is not None


def _default_ingress_factory(app: Any, **kwargs: Any) -> Any:
    from vibe.direct_ingress import DirectIngress
    return DirectIngress(app, **kwargs)


def _default_supervisor_factory(executable: str, ingress: Any, rendezvous: DirectRendezvous, **kwargs: Any) -> Any:
    from vibe.direct_transport import DirectTransportSupervisor
    return DirectTransportSupervisor(executable, ingress, rendezvous, **kwargs)


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

    def clear_paths(self) -> None:
        if self._owner._can_commit(self._generation, self._ingress):
            self._owner._rendezvous.clear_paths()

    def observe_path(self, peer: str, epoch: int, ordinal: int, phase: str, path: dict | None) -> None:
        if self._owner._can_commit(self._generation, self._ingress):
            self._owner._rendezvous.observe_path(peer, epoch, ordinal, phase, path)

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
        self._binary_resolver = binary_resolver or _resolve_selected_binary
        self._ingress_factory = ingress_factory or _default_ingress_factory
        self._supervisor_factory = supervisor_factory or _default_supervisor_factory
        self._rendezvous = rendezvous or default_rendezvous
        self._config_loader = config_loader or V2Config.load
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: asyncio.Task | None = None
        self._config_task: asyncio.Task | None = None
        self._config_read_task: asyncio.Task | None = None
        self._binary_task: asyncio.Task | None = None
        self._install_task: asyncio.Task | None = None
        self._opt_in_generation = 0
        self._install_at = 0.0
        self._install_delay = INSTALL_RETRY_INITIAL_SECONDS
        self._install_reason = "other"
        self._install_failed = False
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
        self._suspension_token: str | None = None
        self._config: V2Config | None = None
        self._signature: tuple[Any, ...] | None = None
        self._generation = 0
        self._permission_allowed: bool | None = None
        self._permission_received_at: float | None = None
        self._permission_reason = "permission_required"
        self._public_traversal_allowed = False
        self._resume_required = False
        self._binding_epoch = object()
        self._request_sequence = 0
        self._last_response_sequence = 0
        self._certificate_result: CertificateResult | None = None
        self._certificate_retry_delay = INSTALL_RETRY_INITIAL_SECONDS
        self._poll_at: float | None = 0.0
        self._retry_cancelled = False
        self._cancelled_retry_used = False
        self._ingress: Any = None
        self._supervisor: Any = None
        self._resource_generation: int | None = None
        self._failed_generation: int | None = None
        self._next_start = 0.0
        self._start_delay = INSTALL_RETRY_INITIAL_SECONDS
        self._state_lock = threading.Lock()
        self._settled_generation: int | None = None
        self._status = DirectAccessStatus(False, "disabled", "setting_off")
        self._reported_generation: int | None = None
        self._reported_fields: dict[str, str] = {}

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
        with self._state_lock:
            self._binding_epoch = object()
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
            self._settled_generation = None
        self._failed_generation = None
        self._start_delay = INSTALL_RETRY_INITIAL_SECONDS
        self._next_start = 0.0
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
        self._install_failed = False
        self._next_binary_check = 0.0
        self._clear_binary()
        self._invalidate()
        if signature[0] and not (previous and previous[0]):
            self._opt_in_generation = self._generation
            self._install_at = 0.0
            self._install_delay = INSTALL_RETRY_INITIAL_SECONDS
        with self._state_lock:
            self._binding_epoch = object()
            self._config = config
            self._signature = signature
        self._pairing_suspended = False
        self._permission_allowed = None
        self._permission_received_at = None
        self._permission_reason = "permission_required"
        self._public_traversal_allowed = False
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
            if self._shutdown or self._pairing_suspended or signature != self._signature or not signature[0]:
                return None
            self._request_sequence += 1
            return PermissionRequest(self._generation, signature, self._request_sequence, self._binding_epoch)

    def submit_runtime_status_response(
        self, ticket: PermissionRequest | None, result: dict[str, Any],
    ) -> None:
        """Accept only the ticket captured before sending an opted-in report."""
        if isinstance(ticket, PermissionRequest):
            self._submit(self._observe_permission, ticket, dict(result), suspend_elapsed())

    def _observe_permission(self, ticket: PermissionRequest, result: dict[str, Any], received: float) -> None:
        if (ticket.binding_epoch is not self._binding_epoch or ticket.signature != self._signature
                or ticket.sequence <= self._last_response_sequence or self._pairing_suspended):
            return
        self._last_response_sequence = ticket.sequence
        allowed = result.get("direct_access_allowed") is True
        if not allowed:
            self._deny("permission_denied")
        else:
            if not self._permission_fresh():
                self._next_binary_check = 0.0  # a missing pin may now be installed
            traversal = result.get("public_traversal_allowed") is True and bool(ticket.signature[-1])
            if traversal != self._public_traversal_allowed:
                self._clear_binary()
                self._next_binary_check = 0.0
                # A change of policy retires the published endpoint and fences
                # already-started work; this report still renews ordinary direct.
                self._invalidate()
                self._public_traversal_allowed = traversal
            self._resume_required |= self._permission_allowed is False
            self._permission_allowed = True
            self._permission_received_at = received
            self._permission_reason = "permission_allowed"
            if self._permission_timer is not None:
                self._permission_timer.cancel()
            remaining = max(0.0, received + PERMISSION_MAX_AGE_SECONDS - suspend_elapsed())
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
        self._public_traversal_allowed = False
        self._permission_received_at = None
        self._permission_reason = reason
        self._resume_required = True
        self._rendezvous.publish(None, reason=reason)

    def _permission_fresh(self) -> bool:
        return (self._permission_allowed is True and self._permission_received_at is not None
                and 0 <= suspend_elapsed() - self._permission_received_at < PERMISSION_MAX_AGE_SECONDS)

    async def prepare_pairing_change(self) -> str:
        """Fence starts, then let the one resource owner stop before key discard.

        Does not load config or await a certificate worker: the writer already
        holds the config lock. A failed config commit deliberately stays closed.
        """
        with self._state_lock:
            self._pairing_suspended = True
            self._binding_epoch = object()
        # Random, not a counter: a token kept across a UI restart must match nothing.
        token = self._suspension_token = secrets.token_hex(16)
        self._deny("pairing_changed")
        self._stopped.clear()
        self._wake.set()
        await self._stopped.wait()
        return token

    def _release_pairing_suspension(self, token: str) -> None:
        """A pairing change was abandoned with the same config: open starts again.

        The config signature is unchanged, so ``_observe_config`` would never
        clear the fence. Permission is asked for afresh, as after any stop.
        """
        if self._shutdown or not self._pairing_suspended or token != self._suspension_token:
            return  # Another pairing change stopped it since: that fence is not ours to open.
        self._pairing_suspended = False
        self._permission_allowed = None
        self._permission_received_at = None
        self._permission_reason = "permission_required"
        self._public_traversal_allowed = False
        self._resume_required = True
        self._wake.set()

    def release_pairing_suspension(self, token: str) -> None:
        self._submit(self._release_pairing_suspension, token)

    def suspend_for_pairing_change(self, timeout_seconds: float = 10.0) -> str | None | bool:
        """The suspension token, None when nothing was running, or False when it could not stop."""
        loop = self._loop
        if loop is None:
            return None
        try:
            if asyncio.get_running_loop() is loop:
                return False
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self.prepare_pairing_change(), loop)
        try:
            return future.result(timeout=timeout_seconds)
        except Exception:
            # Do not cancel the stop: a timed-out writer must not leave a child.
            return False

    def stop_for_pairing_change(self, timeout_seconds: float = 10.0) -> bool:
        return self.suspend_for_pairing_change(timeout_seconds) is not False

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

    def _install_pin(self, generation: int, signature: tuple[Any, ...], deadline: float) -> tuple[int, bool | None, str]:
        from core.direct_transport_runtime import DirectTransportRuntimeManager
        from core.managed_runtime import omit_managed_runtime_log_details

        try:
            if _config_signature(self._config_loader()) != signature:
                return generation, None, "other"
        except Exception:
            return generation, None, "other"
        with self._state_lock:
            if self._shutdown or generation != self._generation or suspend_elapsed() >= deadline:
                return generation, None, "other"
        # An admitted download may finish after OFF; never use its returned path.
        logger.info("Direct transport install reason=transport_installing")
        reason = "other"
        try:
            with omit_managed_runtime_log_details():
                result = DirectTransportRuntimeManager().ensure()
                ok = result.get("ok") is True
                reason = {
                    "direct-transport_archive_checksum_mismatch": "integrity",
                    "direct-transport_binary_checksum_mismatch": "integrity",
                    "direct-transport_archive_download_failed": "download",
                    "direct-transport_manifest_download_failed": "download",
                }.get(result.get("reason"), "other")
        except Exception:
            ok = False
        if ok:
            logger.info("Direct transport install succeeded")
        else:
            logger.warning("Direct transport install reason=transport_install_failed")
        return generation, ok, reason

    def _resolve_binary(self, generation: int, started: float, config: V2Config) -> tuple[int, float, str | None, bool]:
        binary = None
        missing = False
        try:
            resolved = self._binary_resolver(config)
            binary = str(resolved) if resolved else None
            if not binary:
                # An installed pin rejected only by trial policy isn't missing.
                if config.remote_access.vibe_cloud.public_relay_trial:
                    _decline_relay(config)
                    missing = self._binary_resolver(config) is None
                else:
                    missing = True
        except Exception:
            pass  # An unreadable optional runtime is unavailable, not a Web UI failure.
        return generation, started, binary, missing

    def _binary_check_done(self, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()
        self._wake.set()

    def _refresh_binary_observation(self) -> None:
        # The loop only collects/schedules work. One retained binary slot stays
        # occupied until the actual worker ends, across opt-out and stop/start.
        # Neither this worker nor its completion can block the config observer.
        if self._install_task is not None:
            if not self._install_task.done():
                return
            task, self._install_task = self._install_task, None
            if not task.cancelled():
                generation, ok, reason = task.result()
                if ok is False and generation >= self._opt_in_generation:
                    self._install_reason = reason
                    self._install_failed = True
                    self._install_at = self._loop.time() + self._install_delay
                    self._install_delay = min(self._install_delay * 2, INSTALL_RETRY_MAX_SECONDS)
                # A skipped admission re-observes at ordinary cadence; a new
                # config generation has already requested an immediate check.
                if ok is not None:
                    self._next_binary_check = 0.0
        if self._binary_task is not None and self._binary_task.done():
            task, self._binary_task = self._binary_task, None
            if not task.cancelled():
                generation, started, binary, missing = task.result()
                if generation == self._generation and not self._shutdown:
                    self._observe_binary(binary, started)
                    now = self._loop.time()
                    if self._binary:
                        self._install_failed = False
                        self._install_at = 0.0
                        self._install_delay = INSTALL_RETRY_INITIAL_SECONDS
                    elif (missing and self._permission_fresh() and not self._pairing_suspended and now >= self._install_at
                            and now < started + BINARY_OBSERVATION_MAX_AGE_SECONDS):
                        self._install_task = asyncio.create_task(asyncio.to_thread(
                            self._install_pin, self._generation, self._signature,
                            self._permission_received_at + PERMISSION_MAX_AGE_SECONDS,
                        ), name="direct-transport-install")
                        self._install_task.add_done_callback(self._binary_check_done)
                        return
        config = self._config
        if (self._binary_task is not None or self._shutdown or self._pairing_suspended
                or config is None or not config.remote_access.vibe_cloud.direct
                or not _is_paired(config) or managed_direct_hostname(config) is None
                or self._loop.time() < self._next_binary_check):
            return
        binary_config = copy.deepcopy(config)
        binary_config.remote_access.vibe_cloud.set_path_choice('public_relay_trial', self._public_traversal_allowed)
        self._binary_task = asyncio.create_task(asyncio.to_thread(
            self._resolve_binary, self._generation, self._loop.time(), binary_config,
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
            if self._install_task is not None:
                return "installing", "transport_installing", None
            if self._install_failed:
                return "error", "transport_install_failed", None
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
                self._back_off_start(self._generation)
                self._set_state("error", "lifecycle_error")
                logger.warning("Direct access lifecycle transition failed")
            if self._shutdown:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _back_off_start(self, generation: int) -> None:
        if self._generation == generation:
            self._failed_generation = generation
            self._next_start = self._loop.time() + self._start_delay
            self._start_delay = min(INSTALL_RETRY_MAX_SECONDS, self._start_delay * 2)

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
            if self._loop.time() < self._next_start:
                return
            self._failed_generation = None
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
                    0.0, self._permission_received_at + PERMISSION_MAX_AGE_SECONDS - suspend_elapsed(),
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
                self._back_off_start(generation)
                self._set_state("error", "transport_start_failed")
                return
            finally:
                self._start_task = None
        if self._supervisor is not None:
            diagnostics = self._supervisor.status
            if diagnostics.endpoint_id:
                self._start_delay = INSTALL_RETRY_INITIAL_SECONDS
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
        from vibe.direct_rendezvous import PUBLIC_TRIAL

        self._supervisor = self._supervisor_factory(
            binary, self._ingress, _OwnedRendezvous(self, generation, ingress),
            **({"relay_policy": PUBLIC_TRIAL} if self._public_traversal_allowed and self._permission_fresh() else {}),
        )
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
        binding_epoch = self._binding_epoch
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
                if result.last_error_code == 'direct_access_not_allowed' and binding_epoch is self._binding_epoch:
                    # Binding-level refusal, even if an ordinary policy change
                    # retired the endpoint while the certificate HTTP was out.
                    # Only heartbeats issued after this fence can restore it.
                    with self._state_lock:
                        self._binding_epoch = object()
                    self._deny('permission_denied')
                elif generation == self._generation and self._gate()[2] is not None:
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
        if (not result.ready and result.status == "failed" and result.last_error_code in
                {"order_failed", "acme_rate_limited", "backend_unavailable", "dns_challenge_visibility_timeout"}):
            # orderBody reports a settled failure without its provider detail.
            # Explicit credential, policy and ACME authorization refusals aren't
            # a settled wire order and are not in this retry set.
            self._retry_cancelled = True
            self._poll_at = max(self._poll_at or 0, time.time() + self._certificate_retry_delay)
            self._certificate_retry_delay = min(self._certificate_retry_delay * 2, INSTALL_RETRY_MAX_SECONDS)
        elif not result.ready and not result.certificate_not_after and result.status in {'failed', 'cancelled', 'expired'}:
            # No current leaf to refresh and no autonomous work for this order.
            # Waiting on a passed terminal deadline would just take the file lock every tick.
            self._poll_at = None
        elif result.ready:
            self._certificate_retry_delay = INSTALL_RETRY_INITIAL_SECONDS
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

    def report_fields(self, ticket: PermissionRequest | None) -> dict[str, str]:
        with self._state_lock:
            if (ticket is None or self._shutdown or ticket.signature != self._signature
                    or ticket.binding_epoch is not self._binding_epoch or ticket.generation != self._generation
                    or self._reported_generation != self._generation):
                return {}
            return self._reported_fields.copy()

    def applied_generation(self, config: V2Config) -> int | None:
        signature = _config_signature(config)
        with self._state_lock:
            return self._settled_generation if not self._shutdown and self._signature == signature else None

    def _set_state(self, state: str, reason: str) -> None:
        assert state in DIRECT_STATES
        diagnostics = self._supervisor.status if self._supervisor is not None else None
        with self._state_lock:
            if state not in {"starting", "stopping"} and (self._supervisor is None or self._resource_generation == self._generation):
                self._settled_generation = self._generation
            self._reported_generation = self._generation
            self._reported_fields = _report_fields(state, reason, getattr(self._certificate_result, "last_error_code", None))
            if self._permission_allowed is False and self._permission_reason in {"permission_denied", "permission_expired"}:
                self._reported_fields = _report_fields("waiting-permission", self._permission_reason, None)
            elif reason == "transport_install_failed":
                self._reported_fields["direct_reason"] = self._install_reason
            self._status = DirectAccessStatus(
                enabled=bool(self._signature and self._signature[0]), state=state, reason=reason,
                certificate_not_after=getattr(self._certificate_result, "certificate_not_after", None),
                transport_running=bool(self._supervisor is not None and self._supervisor.running),
                direct_access_allowed=self._permission_fresh(),
                streams_opened=diagnostics.connections if diagnostics else 0,
                refusals=diagnostics.refusals if diagnostics else 0,
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


def suspend_for_pairing_change(timeout_seconds: float = 10.0) -> str | None | bool:
    lifecycle = active_lifecycle()
    return None if lifecycle is None else lifecycle.suspend_for_pairing_change(timeout_seconds)


def release_pairing_suspension(token: str) -> None:
    lifecycle = active_lifecycle()
    if lifecycle is not None:
        lifecycle.release_pairing_suspension(token)


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
