"""This machine's view of the signed Model Hub engine channel.

``EngineChannel`` joins the channel contract (``core.model_hub_engine_channel``)
to the engine installer: which keys this Avibe release ships, where accepted
state lives, which engine is bundled, and which host this is. It answers three
questions and changes nothing else:

- ``refresh()``: download the index, authenticate it, and remember it;
- ``snapshot()``: the last accepted index, re-authenticated from disk;
- ``resolve()``: what that index means for the engine in effect.

Nothing in Avibe calls this yet. Adoption, activation, scheduling and recovery
belong to the engine lifecycle, which decides when to act on a resolution. A
resolution is a decision, never an installed engine.

Every call may block on the network, on disk, or on a short avault process.
Callers on the controller loop run it on a thread.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.dependency_network import DependencyNetworkError, fetch_bytes
from core.model_hub_engine_channel import (
    CHANNEL_INDEX_URL,
    MAX_ENVELOPE_BYTES,
    AvaultVerifier,
    ChannelError,
    ChannelSnapshot,
    ChannelStateStore,
    Resolution,
    ResolutionAction,
    SignatureVerifier,
    TrustRoots,
    authenticate,
    format_timestamp,
    parse_trust_roots,
    resolve,
)
from core.process_isolation import isolated_subprocess_kwargs
from vibe.model_hub_runtime.installer import EngineRuntimeManager

logger = logging.getLogger(__name__)

SHIPPED_TRUST_ROOTS_PATH = Path(__file__).with_name("engine_channel_roots.json")
_FETCH_TIMEOUT_SECONDS = 15.0


def shipped_trust_roots() -> TrustRoots:
    """The channel keys this Avibe release ships; none means no channel.

    A missing or damaged document is the same as an empty one: the channel
    stays off rather than trusting anything it cannot read.
    """

    try:
        return parse_trust_roots(SHIPPED_TRUST_ROOTS_PATH.read_bytes())
    except (OSError, ValueError):
        logger.warning("Model Hub engine channel trust roots are unreadable; the channel stays off", exc_info=True)
        return TrustRoots()


def _resolve_avault_binary() -> str | None:
    # Avibe's read-only avault path lookup: configuration, then PATH. It
    # installs nothing and runs nothing. Imported late because ``vibe.api``
    # imports this package.
    from vibe import api

    return api._resolve_avault_cli_path()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        return None


class _BoundedResponse:
    """A response whose body may not exceed the largest index the channel allows."""

    def __init__(self, response) -> None:  # noqa: ANN001
        self._response = response

    def __enter__(self) -> "_BoundedResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._response.close()

    def read(self) -> bytes:
        body = self._response.read(MAX_ENVELOPE_BYTES + 1)
        if len(body) > MAX_ENVELOPE_BYTES:
            raise ValueError("the channel index is larger than the channel allows")
        return body


def _open_index(request: urllib.request.Request, *, timeout: float) -> _BoundedResponse:
    return _BoundedResponse(urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout))


def fetch_channel_index() -> bytes:
    """Download the index from its one fixed URL: no redirect, bounded size."""

    request = urllib.request.Request(
        CHANNEL_INDEX_URL,
        headers={"User-Agent": "avibe-model-hub-engine-channel", "Accept": "application/json"},
    )
    return fetch_bytes(request, timeout=_FETCH_TIMEOUT_SECONDS, opener=_open_index)


@dataclass(frozen=True)
class ChannelStatus:
    """The outcome of one refresh.

    ``state`` is ``uncommissioned``, ``opted_out``, ``offline``, ``unavailable``
    (the index could not be downloaded), ``rejected`` (it could not be
    trusted), or ``accepted``. ``reason`` is a stable token for the first five.
    """

    state: str
    reason: str | None = None
    detail: str | None = None
    sequence: int | None = None
    expires_at: str | None = None
    #: Whether an accepted index is newer than the one already recorded.
    changed: bool = False


class EngineChannel:
    def __init__(
        self,
        manager: EngineRuntimeManager | None = None,
        *,
        roots: TrustRoots | None = None,
        verifier: SignatureVerifier | None = None,
        fetch: Callable[[], bytes] = fetch_channel_index,
        clock: Callable[[], datetime] | None = None,
        avibe_version: str | None = None,
    ) -> None:
        self.manager = manager or EngineRuntimeManager()
        self.roots = shipped_trust_roots() if roots is None else roots
        self.verifier = verifier or AvaultVerifier(
            _resolve_avault_binary, popen_kwargs=isolated_subprocess_kwargs(console=False)
        )
        self.store = ChannelStateStore(self.manager.runtime_dir / "channel")
        self._fetch = fetch
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if avibe_version is None:
            from vibe import __version__ as avibe_version
        self._avibe_version = avibe_version

    def opt_out_reason(self) -> str | None:
        """Why this installation does not take part in the channel, if it does not.

        An Avibe release with no shipped key has no channel. An engine manifest
        override, from a test or a local build, names the engine by itself and
        opts out of every channel decision.
        """

        if not self.roots.commissioned:
            return "uncommissioned"
        if self.manager.manifest_path is not None or self.manager.manifest_url:
            return "manifest_override"
        return None

    def refresh(self) -> ChannelStatus:
        """Download, authenticate and record the index. Never raises."""

        reason = self.opt_out_reason()
        if reason is not None:
            return ChannelStatus("uncommissioned" if reason == "uncommissioned" else "opted_out", reason)
        if self.manager.offline:
            return ChannelStatus("offline", "offline")
        try:
            raw = self._fetch()
        except DependencyNetworkError as exc:
            return ChannelStatus("unavailable", str(exc.details.get("kind") or "network"), str(exc))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return ChannelStatus("unavailable", "network", type(exc).__name__)
        try:
            envelope, payload = authenticate(raw, roots=self.roots, verifier=self.verifier)
            changed = self.store.accept(envelope, payload, floor=self.roots.sequence_floor, now=self._clock())
        except ChannelError as exc:
            logger.warning("Model Hub engine channel index rejected: %s", exc)
            return ChannelStatus("rejected", exc.reason, exc.detail)
        except Exception as exc:  # noqa: BLE001 - a refresh reports, it does not raise
            logger.exception("Model Hub engine channel refresh failed")
            return ChannelStatus("rejected", "state_unreadable", type(exc).__name__)
        return ChannelStatus(
            "accepted",
            sequence=payload.sequence,
            expires_at=format_timestamp(payload.expires_at),
            changed=changed,
        )

    def snapshot(self) -> ChannelSnapshot | None:
        """The last accepted index, re-authenticated from disk; no network.

        ``None`` when this installation is outside the channel or has accepted
        nothing yet. Raises ``ChannelError`` when what is on disk cannot be
        trusted, including when avault cannot verify it right now.
        """

        if self.opt_out_reason() is not None:
            return None
        return self.store.cached(roots=self.roots, verifier=self.verifier, now=self._clock())

    def bundled_release_tag(self) -> str | None:
        manifest = self.manager.load_manifest_for_diagnostics()
        tag = manifest.payload.get("asset_release_tag") if manifest is not None else None
        return tag if isinstance(tag, str) else None

    def resolve(
        self,
        *,
        current: str,
        quarantined: frozenset[str] = frozenset(),
        allow_adoption: bool = True,
    ) -> Resolution:
        """What the last accepted index means for the engine release in effect.

        ``current`` is that engine's asset release tag. Offline mode keeps
        every recorded withdrawal in force and authorizes no download.
        """

        reason = self.opt_out_reason()
        if reason is not None:
            return Resolution(ResolutionAction.KEEP, reason)
        try:
            snapshot = self.snapshot()
        except ChannelError as exc:
            logger.warning("Model Hub engine channel state cannot be trusted: %s", exc)
            return Resolution(ResolutionAction.KEEP, exc.reason)
        if snapshot is None:
            return Resolution(ResolutionAction.KEEP, "no_index")
        bundled = self.bundled_release_tag()
        if bundled is None:
            return Resolution(ResolutionAction.KEEP, "engine_identity_unrecognized")
        return resolve(
            snapshot.payload,
            fresh=snapshot.fresh and not self.manager.offline,
            avibe_version=self._avibe_version,
            host_platform=self.manager.host_platform(),
            bundled=bundled,
            current=current,
            quarantined=quarantined,
            allow_adoption=allow_adoption,
        )
