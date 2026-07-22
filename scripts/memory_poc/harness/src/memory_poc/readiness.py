from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchReadiness:
    """First public-hybrid-search observations after a successful flush."""

    profile_ms: int | None
    episode_ms: int | None
    atomic_fact_ms: int | None
    timeout_ms: int
    measurement_started: bool = True

    @classmethod
    def pending(cls, *, timeout_ms: int) -> SearchReadiness:
        return cls(profile_ms=None, episode_ms=None, atomic_fact_ms=None, timeout_ms=timeout_ms)

    @classmethod
    def not_measured(cls, *, timeout_ms: int) -> SearchReadiness:
        return cls(
            profile_ms=None,
            episode_ms=None,
            atomic_fact_ms=None,
            timeout_ms=timeout_ms,
            measurement_started=False,
        )

    @property
    def complete(self) -> bool:
        return (
            self.measurement_started
            and self.profile_ms is not None
            and self.episode_ms is not None
            and self.atomic_fact_ms is not None
        )

    @property
    def max_observed_ms(self) -> int | None:
        values = (self.profile_ms, self.episode_ms, self.atomic_fact_ms)
        observed = [value for value in values if value is not None]
        return max(observed) if observed else None
