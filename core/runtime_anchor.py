"""The runtime identity of one agent session, as a pair rather than a string.

WHY THIS MODULE EXISTS. A Claude/Codex/OpenCode runtime is identified by two
independent facts — the session anchor (``{platform}_{thread}``, optionally
suffixed with a subagent or routing-agent name) and the working directory. Every
producer holds both as separate values, and every storage consumer
(``agent_sessions.session_anchor`` and ``agent_sessions.workdir`` are separate
columns) wants them apart again. The composite string ``f"{anchor}:{workdir}"``
exists only because the in-process caches in ``core.handlers.session_handler``
key on a single hashable value.

Joining two strings with a separator that both halves may legally contain makes
the pair unrecoverable: a subagent literally named ``/review`` and a Windows
workdir ``C:\\repo`` both put a path-shaped segment after a colon, so
``base:/review:C:\\repo`` has three readings and the string alone cannot say
which one the producer meant. Recovering it by rule is not a formatting nicety —
the anchor half feeds the teardown resolve, and a wrong anchor matches no row, so
the caller reads "nothing to settle" and dismantles a backend with its runs still
``running``.

So the pair is never destroyed. :class:`RuntimeAnchor` is constructed where both
halves are still in hand, carried to the teardown paths intact, and rendered to a
composite string only for the cache keys that need one. :meth:`RuntimeAnchor.parse`
exists for legacy strings with no structured origin (a persisted
``legacy_session_key``, a restart-recovery row); it applies one documented rule and
makes no attempt to disambiguate, because a caller that reached for it has already
lost the information that would decide.

NO IMPORTS BEYOND THE STANDARD LIBRARY, deliberately. This sits on the lightweight
import chain the native-session contract pins (``core.handlers.session_handler``
must import without ``sqlite3`` — ``tests/test_native_session_providers.py``), and
it is also the module ``storage.agent_session_rows`` imports
:func:`normalize_workdir` from, so it must not import ``storage`` back.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["RuntimeAnchor", "normalize_workdir"]


def normalize_workdir(value: Any) -> Optional[str]:
    """The one spelling of a working directory, for comparing two of them.

    ``agent_sessions.workdir`` is written through this, so any caller that wants to
    match a stored row has to arrive through it too. It previously lived in
    ``storage.agent_session_rows`` with a second copy in
    ``core.services.agent_run_target``; both import it from here now, because a
    workdir normalized two different ways is a lookup that silently misses.
    """

    text = str(value or "").strip()
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


# The remainder of a composite key, at the colon that separates anchor from working
# path: a POSIX root (``/x``), a Windows root-relative or UNC path (``\x``,
# ``\\host\share``), or a drive-qualified path (``C:\x`` / ``C:/x``).
_WORKING_PATH_HEAD = re.compile(r"[A-Za-z]:[\\/]|[\\/]")


@dataclass(frozen=True)
class RuntimeAnchor:
    """A session anchor and the working directory it is bound to.

    ``workdir`` is stored VERBATIM (stripped only), never normalized, because
    :attr:`key` has to reproduce the exact string the in-process caches are already
    keyed by — normalizing here would silently orphan every live cache entry. Use
    :attr:`storage_workdir` when comparing against a stored row; that is the only
    place the two spellings must agree, and it is the mismatch that used to make an
    un-normalized caller workdir miss a normalized row.
    """

    session_anchor: str
    workdir: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_anchor", str(self.session_anchor or "").strip())
        object.__setattr__(self, "workdir", str(self.workdir or "").strip())

    def __bool__(self) -> bool:
        return bool(self.session_anchor)

    @property
    def key(self) -> str:
        """The composite cache key, byte-identical to what the producers build."""

        if not self.workdir:
            return self.session_anchor
        return f"{self.session_anchor}:{self.workdir}"

    @property
    def storage_workdir(self) -> Optional[str]:
        """This anchor's workdir spelled the way ``agent_sessions.workdir`` is."""

        return normalize_workdir(self.workdir)

    @classmethod
    def parse(cls, composite_key: Optional[str]) -> "RuntimeAnchor":
        """Best-effort recovery of a pair from a string that lost its structure.

        FOR LEGACY STRINGS ONLY. Every live teardown path carries a
        :class:`RuntimeAnchor` built where both halves were still separate; this is
        the fallback for a persisted ``legacy_session_key`` or a restart-recovery row
        that predates it.

        THE RULE: an anchor never contains a filesystem path, and the working path is
        appended exactly once, absolute. So the separator is the FIRST colon whose
        remainder starts like an absolute path — POSIX ``/``, Windows ``\\`` or UNC
        ``\\\\host``, or a drive letter (``C:\\repo`` / ``C:/repo``) — and every colon
        after it belongs to the working directory. Scanning from the left rather than
        with ``rpartition`` is what keeps ``{anchor}:C:\\repo`` from splitting into
        ``{anchor}:C`` and ``\\repo``.

        When no absolute-path marker appears anywhere the historical ``rpartition``
        split is kept, which preserves the answer for non-path keys (``"a:b"`` ->
        ``("a", "b")``).

        AMBIGUITY IS NOT RESOLVED HERE, because it cannot be: when an anchor suffix is
        itself path-shaped, two readings are equally well-formed and the string does
        not record which one the producer meant. This returns the first-match reading
        and does not consult storage to rank the alternatives. Callers that need the
        right answer must carry the pair instead of re-deriving it — which is why this
        method has no callers on the settlement paths.
        """

        resolved = str(composite_key or "").strip()
        if not resolved:
            return cls("", "")
        separator_index = resolved.find(":")
        while separator_index != -1:
            working_path = resolved[separator_index + 1 :]
            if _WORKING_PATH_HEAD.match(working_path):
                return cls(resolved[:separator_index], working_path)
            separator_index = resolved.find(":", separator_index + 1)
        anchor, separator, working_path = resolved.rpartition(":")
        if not separator:
            return cls(resolved, "")
        return cls(anchor, working_path)
