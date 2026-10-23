"""Service-owned, identity-scoped recovery admission for pending inference."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable

from config.v2_config import ModelHubConfig, ModelHubSourceConfig

from .provenance import TurnUpstreamFailure
from .resolver import SourceRecoveryAnnotation, parse_model_hub_timestamp


logger = logging.getLogger(__name__)
RECOVERY_WINDOW_SECONDS = 120.0
# A wait must end with time left to attempt inside the window. Retry-After has one-second resolution, and the window
# opens at the refusal, after the response its advice counts from: advice equal to the window lands a few
# milliseconds inside it, and the attempt it was waited for would find the window closed. So a retry due within the
# window's last second is one the window does not admit.
RECOVERY_ADMISSION_MARGIN_SECONDS = 1.0
RECOVERY_EXHAUSTED_CODE = "model_hub_recovery_exhausted"
RECOVERY_EXHAUSTED_MESSAGE = "Automatic recovery has ended. Try again or choose another model."
RETRY_DELAYS = {
    "network": (1, 2, 4, 8, 16, 30),
    "server_error": (30, 60, 120),
    "rate_limited": (60, 120, 240, 300),
    "quota_exhausted": (300,),
}


def source_identity(source: ModelHubSourceConfig) -> tuple:
    """Configuration identity, independent of mutable health, inventory and verification.

    Every credential or endpoint change mints a new ``credential_generation``, which verification keeps, so the
    identity changes once per change and never reverts: not when the change is verified, and not when OAuth
    reauthentication keeps the credential's handle. Released Sources keep their digests: a verified one has no
    generation where its identity held a ``None`` marker, and a pending one's marker becomes its generation
    (``ModelHubSourceConfig``).
    """

    return (
        source.kind, source.vendor, source.protocol, source.supply_channel,
        source.base_url, source.credential_ref, source.credential_generation,
    )


def retry_after_deadline(
    value: str | None,
    received_at: datetime,
    *,
    milliseconds: int | None = None,
) -> datetime | None:
    """Validate bounded advice and choose the later valid upstream deadline."""

    deadlines: list[datetime] = []
    if value is not None:
        try:
            if not isinstance(value, str) or len(value) > 128 or not value.isascii():
                raise ValueError
            value = value.strip()
            if value.isdecimal():
                deadlines.append(received_at + timedelta(seconds=int(value)))
            else:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    raise ValueError
                deadlines.append(deadline)
        except (ValueError, TypeError, OverflowError):
            logger.debug("Ignored invalid or past Model Hub Retry-After advice")
    if type(milliseconds) is int and milliseconds >= 0:
        try:
            deadlines.append(received_at + timedelta(milliseconds=milliseconds))
        except (OverflowError, ValueError):
            logger.debug("Ignored invalid Model Hub Retry-After-Ms advice")
    future = [deadline for deadline in deadlines if deadline >= received_at]
    return max(future) if future else None


@dataclass
class RecoveryRequest:
    policy: RecoveryPolicy
    started: float | None = None
    started_at: datetime | None = None
    window_end: datetime | None = None
    attempt_count: int = 0
    source_id: str | None = None
    reason: str | None = None
    observer: Callable[[dict | None], None] | None = None
    # A hard failure this request itself observed ends it with that verdict.
    non_retryable_failure: bool = False
    # Carried across walks so a later blocked walk still names what failed.
    last_upstream_failure: TurnUpstreamFailure | None = None

    def start(self) -> None:
        if self.started is None:
            self.started = self.policy.monotonic()
            self.started_at = self.policy.now()
            self.window_end = self.started_at + timedelta(seconds=self.policy.window_seconds)

    @property
    def remaining(self) -> float:
        if self.started is None:
            return self.policy.window_seconds
        return max(0.0, self.policy.window_seconds - (self.policy.monotonic() - self.started))

    @property
    def expired(self) -> bool:
        return self.started is not None and self.remaining <= 0

    def publish(self, phase: str, *, next_eligible_at: str | None = None) -> None:
        if self.observer is not None and self.started is not None:
            self.observer({
                "phase": phase,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "attempt_count": self.attempt_count,
                "source_id": self.source_id,
                "reason": self.reason,
                "next_eligible_at": next_eligible_at,
                "window_end": self.window_end.isoformat() if self.window_end else None,
            })

    def clear(self) -> None:
        if self.observer is not None:
            self.observer(None)


@dataclass
class _SourceRecovery:
    identity: tuple
    reason: str
    failures: int
    retry_at: datetime
    eligible_at: float
    owner: int | None = None
    failed_generation: int | None = None


class RecoveryPolicy:
    """Live deadlines and half-open ownership; the service supplies synchronization."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        window_seconds: float = RECOVERY_WINDOW_SECONDS,
    ):
        self.now = now
        self.monotonic = monotonic
        self.jitter = jitter
        self.sleep = sleep
        self.window_seconds = window_seconds
        self._sources: dict[str, _SourceRecovery] = {}
        # Exact persisted observations superseded by verified inference. Keep
        # them only while a failed save leaves that same cooldown on disk.
        self._retired_cooldowns: dict[str, tuple] = {}
        self.changed = asyncio.Event()

    def notify(self) -> None:
        # Replacing the event avoids lost wakeups and lets all current waiters wake.
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def reconcile(self, config: ModelHubConfig) -> None:
        sources = {source.id: source for source in config.sources if source.supply_channel == "hub"}
        removed = [
            source_id for source_id, state in self._sources.items()
            if source_id not in sources or source_identity(sources[source_id]) != state.identity
        ]
        for source_id in removed:
            del self._sources[source_id]
        retired = [
            source_id for source_id, observation in self._retired_cooldowns.items()
            if source_id not in sources or self._cooldown_observation(sources[source_id]) != observation
        ]
        for source_id in retired:
            del self._retired_cooldowns[source_id]
        if removed or retired:
            self.notify()

    @staticmethod
    def _cooldown_observation(source: ModelHubSourceConfig) -> tuple | None:
        if source.state.status != "cooldown":
            return None
        return source_identity(source), source.state.retry_at, source.state.detail_key

    def _state(self, source: ModelHubSourceConfig) -> _SourceRecovery | None:
        # Native calls have no HTTP attempt/success owner. Their shipped fixed
        # cooldown and native retry lifecycle remain outside this coordinator.
        if source.supply_channel != "hub":
            return None
        state = self._sources.get(source.id)
        if state is not None and state.identity != source_identity(source):
            del self._sources[source.id]
            self.notify()
            state = None
        retired = self._retired_cooldowns.get(source.id)
        if retired is not None:
            if retired == self._cooldown_observation(source):
                return state
            del self._retired_cooldowns[source.id]
        # Restore eligibility from shipped cooldowns, never a recovered verdict.
        if state is None and source.state.status == "cooldown" and source.state.retry_at:
            retry_at = parse_model_hub_timestamp(source.state.retry_at)
            state = _SourceRecovery(
                source_identity(source),
                (source.state.detail_key or "").rsplit(".", 1)[-1],
                0,
                retry_at,
                self.monotonic() + max(0.0, (retry_at - self.now()).total_seconds()),
            )
            self._sources[source.id] = state
        elif (
            state is not None and source.state.status == "cooldown"
            and source.state.retry_at
        ):
            persisted = parse_model_hub_timestamp(source.state.retry_at)
            if persisted > state.retry_at:
                state.retry_at = persisted
                state.eligible_at = max(
                    state.eligible_at,
                    self.monotonic() + max(0.0, (persisted - self.now()).total_seconds()),
                )
        return state

    def annotations(self, config: ModelHubConfig) -> dict[str, SourceRecoveryAnnotation]:
        self.reconcile(config)
        result = {}
        for source in config.sources:
            state = self._state(source)
            retired = self._retired_cooldowns.get(source.id)
            if state is None:
                if retired is not None:
                    result[source.id] = SourceRecoveryAnnotation(
                        reason="recovery", retired_cooldown=retired[1:],
                    )
                continue
            remaining = max(0.0, state.eligible_at - self.monotonic())
            result[source.id] = SourceRecoveryAnnotation(
                reason=state.reason,
                # Project remaining monotonic time onto the current wall clock.
                retry_at=(self.now() + timedelta(seconds=remaining)).isoformat() if remaining else None,
                in_flight=state.owner is not None,
                retired_cooldown=retired[1:] if retired is not None else None,
            )
        return result

    def failed(
        self,
        source: ModelHubSourceConfig,
        reason: str,
        *,
        generation: int,
        retry_after: str | None = None,
        retry_after_ms: int | None = None,
        response_received_at: datetime | None = None,
    ) -> tuple[datetime, bool]:
        state = self._state(source)
        if state is not None and state.failed_generation is not None and generation <= state.failed_generation:
            return state.retry_at, False
        failures = state.failures + 1 if state is not None else 1
        delays = RETRY_DELAYS[reason]
        base, cap = delays[min(failures - 1, len(delays) - 1)], delays[-1]
        local = self.jitter(base, min(cap, base * 1.2))
        now, mono = self.now(), self.monotonic()
        received = response_received_at
        if received is None or received.tzinfo is None:
            received = now
        upstream = retry_after_deadline(
            retry_after,
            received,
            milliseconds=retry_after_ms,
        )
        deadline = max(now + timedelta(seconds=local), upstream or now)
        eligible_at = mono + (deadline - now).total_seconds()
        if state is not None:
            eligible_at = max(eligible_at, state.eligible_at)
            deadline = max(deadline, state.retry_at, now + timedelta(seconds=eligible_at - mono))
        self._sources[source.id] = _SourceRecovery(
            source_identity(source), reason, failures, deadline, eligible_at,
            failed_generation=generation,
        )
        self.notify()
        return deadline, True

    def claim(self, source: ModelHubSourceConfig, generation: int) -> bool:
        state = self._state(source)
        if state is None:
            return True
        if state.owner is not None or state.eligible_at > self.monotonic():
            return False
        state.owner = generation
        self.notify()
        return True

    def release(self, source_id: str, generation: int | None) -> None:
        state = self._sources.get(source_id)
        if state is not None and generation is not None and state.owner == generation:
            state.owner = None
            self.notify()

    def succeeded(self, source: ModelHubSourceConfig) -> bool:
        state = self._state(source)
        if state is None:
            return False
        del self._sources[source.id]
        observation = self._cooldown_observation(source)
        if observation is not None:
            self._retired_cooldowns[source.id] = observation
        self.notify()
        return True

    async def wait(self, delay: float, changed: asyncio.Event) -> None:
        """Wait without owning the configuration lock or a transport."""

        timer = asyncio.create_task(self.sleep(delay))
        notification = asyncio.create_task(changed.wait())
        try:
            await asyncio.wait((timer, notification), return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            notification.cancel()
            await asyncio.gather(timer, notification, return_exceptions=True)
