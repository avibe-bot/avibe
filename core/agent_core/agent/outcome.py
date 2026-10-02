"""One owner for the first primary run cause and supplementary diagnostics."""

from collections import deque
from typing import Optional

from core.agent_core.agent.events import RunEndReason


class OutcomeOwner:
    def __init__(self) -> None:
        self._reason: Optional[RunEndReason] = None
        self.diagnostics: deque[tuple[str, str]] = deque()

    def primary(self, reason: RunEndReason) -> None:
        if self._reason is None:
            self._reason = reason

    @property
    def reason(self) -> RunEndReason:
        if self._reason is None:
            raise RuntimeError("the run has no primary outcome")
        return self._reason

    def foreground_leaked(self) -> None:
        # The sole cleanup exception to first-cause ownership.
        if self._reason == "completed":
            self._reason = "error"

    def diagnostic(self, kind: str, message: str) -> None:
        self.diagnostics.append((kind, message))
