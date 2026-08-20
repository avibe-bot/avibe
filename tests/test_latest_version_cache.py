"""Properties of the managed-dependency latest-version cache.

The point of the file tier is that a *different process* inherits the answer, so
the tests below simulate that the only honest way: clear the memory tier and ask
again, exactly as a fresh ``vibe runtime prepare`` would.
"""

from __future__ import annotations

import json
import time

import pytest

from core import latest_version_cache
from core.latest_version_cache import cache_path, cached_latest, invalidate


class _Probe:
    """A fetch callable that records how often it was actually asked."""

    def __init__(self, *values: str | None) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self._values[min(self.calls - 1, len(self._values) - 1)]


def _never_probed() -> str | None:
    raise AssertionError("the cached answer should have satisfied this lookup")


def _cold_process() -> None:
    """Drop the memory tier, leaving only what a new process would find."""

    latest_version_cache._MEMORY.clear()  # noqa: SLF001


def _persisted() -> dict:
    return json.loads(cache_path().read_text(encoding="utf-8"))


def test_a_second_process_inherits_the_answer_without_probing() -> None:
    probe = _Probe("0.1.15")

    assert cached_latest("askill", probe) == "0.1.15"
    _cold_process()
    assert cached_latest("askill", probe) == "0.1.15"

    assert probe.calls == 1


def test_a_failed_probe_is_also_inherited_so_a_rate_limit_is_not_re_hit() -> None:
    """A 403 costs the next process a fallback reinstall; don't earn it twice."""

    probe = _Probe(None, "0.1.15")

    assert cached_latest("askill", probe) is None
    _cold_process()
    assert cached_latest("askill", probe) is None

    assert probe.calls == 1


def test_a_failed_probe_expires_far_sooner_than_a_successful_one() -> None:
    probe = _Probe(None, "0.1.15")
    cached_latest("askill", probe)

    aged = latest_version_cache.FAILURE_TTL_SECONDS + 1
    _write_entries({"askill": {"at": time.time() - aged, "value": None}})
    _cold_process()
    assert cached_latest("askill", probe) == "0.1.15"

    aged = latest_version_cache.SUCCESS_TTL_SECONDS - 60
    _write_entries({"askill": {"at": time.time() - aged, "value": "0.1.15"}})
    _cold_process()
    assert cached_latest("askill", probe) == "0.1.15"
    assert probe.calls == 2


def test_an_expired_entry_is_re_probed() -> None:
    probe = _Probe("0.1.15", "0.1.16")
    cached_latest("askill", probe)

    _write_entries(
        {"askill": {"at": time.time() - latest_version_cache.SUCCESS_TTL_SECONDS - 1, "value": "0.1.15"}}
    )
    _cold_process()

    assert cached_latest("askill", probe) == "0.1.16"
    assert probe.calls == 2


def test_a_timestamp_from_the_future_is_never_live() -> None:
    """A clock that stepped backwards must not pin an entry until it catches up."""

    probe = _Probe("0.1.16")
    _write_entries({"askill": {"at": time.time() + 86_400, "value": "0.1.15"}})

    assert cached_latest("askill", probe) == "0.1.16"
    assert probe.calls == 1


def test_invalidate_forgets_the_entry_in_both_tiers() -> None:
    probe = _Probe("0.1.15", "0.1.16")
    cached_latest("askill", probe)

    invalidate("askill")

    assert "askill" not in _persisted()["entries"]
    assert cached_latest("askill", probe) == "0.1.16"
    _cold_process()
    assert cached_latest("askill", probe) == "0.1.16"
    assert probe.calls == 2


def test_entries_for_other_dependencies_survive_a_write() -> None:
    """askill runs from the CLI and opencode from the service; neither may evict."""

    cached_latest("askill", _Probe("0.1.15"))
    cached_latest("opencode", _Probe("1.2.3"))
    _cold_process()

    assert cached_latest("askill", _never_probed) == "0.1.15"
    assert set(_persisted()["entries"]) == {"askill", "opencode"}

    invalidate("opencode")
    _cold_process()
    assert cached_latest("askill", _never_probed) == "0.1.15"


def test_invalidating_an_absent_name_does_not_create_a_file() -> None:
    invalidate("never-cached")

    assert not cache_path().exists()


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        json.dumps({"schema_version": 999, "entries": {"askill": {"at": 0, "value": "0.1.15"}}}),
        json.dumps({"schema_version": 1, "entries": "not-a-mapping"}),
        json.dumps(["not", "a", "mapping"]),
        json.dumps({"schema_version": 1}),
    ],
)
def test_an_unusable_cache_file_costs_a_probe_and_nothing_else(content) -> None:
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_text(content, encoding="utf-8")
    probe = _Probe("0.1.15")

    assert cached_latest("askill", probe) == "0.1.15"
    assert probe.calls == 1
    assert _persisted()["entries"]["askill"]["value"] == "0.1.15"


@pytest.mark.parametrize(
    "entry",
    [
        {"at": "yesterday", "value": "0.1.15"},
        {"at": True, "value": "0.1.15"},
        {"at": 0, "value": {"nested": "junk"}},
        "not-a-mapping",
    ],
)
def test_an_unusable_entry_is_skipped_without_discarding_its_neighbours(entry) -> None:
    fresh = time.time()
    _write_entries({"askill": entry, "opencode": {"at": fresh, "value": "1.2.3"}})

    assert cached_latest("opencode", _never_probed) == "1.2.3"
    assert cached_latest("askill", _Probe("0.1.15")) == "0.1.15"


def test_a_lookup_still_answers_when_the_state_directory_cannot_be_written(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        latest_version_cache,
        "write_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only filesystem")),
    )

    assert cached_latest("askill", _Probe("0.1.15")) == "0.1.15"


def _write_entries(entries: dict) -> None:
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_text(
        json.dumps({"schema_version": latest_version_cache.SCHEMA_VERSION, "entries": entries}),
        encoding="utf-8",
    )
