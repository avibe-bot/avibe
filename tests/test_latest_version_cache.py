"""Properties of the managed-dependency latest-version cache.

The point of the file tier is that a *different process* inherits the answer, so
the tests below simulate that the only honest way: clear the memory tier and ask
again, exactly as a fresh ``vibe runtime prepare`` would.
"""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace

import pytest

from core import latest_version_cache
from core.latest_version_cache import cache_path, cached_latest


class _NovelFailure(Exception):
    """An exception type no hand-written list of failure modes could contain."""


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


def _remember(name: str, *, age: float, value: str | None) -> None:
    """Seed the memory tier alone with an entry of a given age."""

    latest_version_cache._MEMORY[name] = latest_version_cache._Entry(time.time() - age, value)  # noqa: SLF001


def _persisted() -> dict:
    return json.loads(cache_path().read_text(encoding="utf-8"))


def _not_json(token: str) -> float:
    """Refuse the tokens Python emits but the JSON grammar does not contain."""

    raise AssertionError(f"the cache file must be readable by any JSON parser, found {token!r}")


def test_a_second_process_inherits_the_answer_without_probing() -> None:
    probe = _Probe("0.1.15")

    assert cached_latest("askill", probe) == "0.1.15"
    _cold_process()
    assert cached_latest("askill", probe) == "0.1.15"

    assert probe.calls == 1


def test_a_failed_probe_stops_at_the_memory_tier() -> None:
    """The file holds answers; a failure is not one, so it does not travel.

    Inheriting one never bought what it looked like it bought: a process that
    reads a cached ``None`` reports ``latest_unavailable`` and reinstalls askill
    outright, where re-probing might find the blip already over. It saves one
    HTTP request against a budget that — in the only case this arises — is
    already exhausted, and pays for it with the race in the next test.
    """

    probe = _Probe(None, "0.1.15")

    assert cached_latest("askill", probe) is None
    assert not cache_path().exists()

    # Same process asks again: the memory tier still backs off, which is the
    # tier that needs to — a service polling a dead registry, not a fresh CLI.
    assert cached_latest("askill", probe) is None
    assert probe.calls == 1

    _cold_process()
    assert cached_latest("askill", probe) == "0.1.15"
    assert probe.calls == 2


def test_a_failed_probe_never_writes_the_file() -> None:
    """Why the same-name race is gone rather than narrowed.

    Two cold processes miss the same name and both probe, outside any
    cross-process lock, so the failure can land after the success. No
    check-then-write can stop that — it only shrinks the window. A failure that
    never touches the file cannot lose the race in the first place.
    """

    stale = time.time() - latest_version_cache.SUCCESS_TTL_SECONDS - 1
    _write_entries({"askill": {"at": stale, "value": "0.1.15"}})
    before = cache_path().read_bytes()
    _cold_process()

    # Expired, so this process really does probe — and really does fail.
    assert cached_latest("askill", _Probe(None)) is None
    assert cache_path().read_bytes() == before


def test_the_memory_tier_retries_a_failure_far_sooner_than_a_success() -> None:
    """Both TTLs still govern, in the one tier where the backoff pays for itself."""

    probe = _Probe("0.1.15")

    _remember("askill", age=latest_version_cache.FAILURE_TTL_SECONDS + 1, value=None)
    assert cached_latest("askill", probe) == "0.1.15"

    _remember("askill", age=latest_version_cache.FAILURE_TTL_SECONDS - 1, value=None)
    assert cached_latest("askill", _never_probed) is None

    _remember("askill", age=latest_version_cache.SUCCESS_TTL_SECONDS - 60, value="0.1.14")
    assert cached_latest("askill", _never_probed) == "0.1.14"
    assert probe.calls == 1


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


def test_a_null_answer_on_disk_is_ignored_rather_than_inherited() -> None:
    """The rule holds in both directions: never written, so never trusted.

    Otherwise a corrupted byte or a foreign writer could pin
    ``latest_unavailable`` — and therefore an askill reinstall — on every cold
    process for the whole failure TTL.
    """

    _write_entries({"askill": {"at": time.time(), "value": None}})

    assert cached_latest("askill", _Probe("0.1.15")) == "0.1.15"
    assert _persisted()["entries"]["askill"]["value"] == "0.1.15"


def test_entries_for_other_dependencies_survive_a_write() -> None:
    """askill runs from the CLI and opencode from the service; neither may evict."""

    cached_latest("askill", _Probe("0.1.15"))
    cached_latest("opencode", _Probe("1.2.3"))
    _cold_process()

    assert cached_latest("askill", _never_probed) == "0.1.15"
    assert set(_persisted()["entries"]) == {"askill", "opencode"}

    # opencode expires and is re-probed; the rewrite that publishes its new
    # answer must carry askill's untouched entry along with it.
    _write_entries(
        {
            "askill": _persisted()["entries"]["askill"],
            "opencode": {"at": time.time() - latest_version_cache.SUCCESS_TTL_SECONDS - 1, "value": "1.2.3"},
        }
    )
    _cold_process()
    assert cached_latest("opencode", _Probe("1.2.4")) == "1.2.4"
    _cold_process()
    assert cached_latest("askill", _never_probed) == "0.1.15"


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


def test_bytes_that_are_not_a_document_cost_a_probe_and_nothing_else() -> None:
    """Not one more entry in an enumeration — the property the loader promises.

    A non-UTF-8 file raises ``UnicodeDecodeError`` rather than
    ``JSONDecodeError``, which is exactly the member a hand-written list of
    failure modes was always going to be missing.
    """

    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_bytes(b'{"schema_version": 1, "entries": {"\xff\xfe": 0}}')
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
        # Raises rather than mismatches: ``float`` of an integer this large is an
        # ``OverflowError``, which is an ``ArithmeticError`` and so slips past any
        # guard written in terms of ``ValueError``.
        {"at": int("9" * 400), "value": "0.1.15"},
    ],
)
def test_an_unusable_entry_is_skipped_without_discarding_its_neighbours(entry) -> None:
    fresh = time.time()
    _write_entries({"askill": entry, "opencode": {"at": fresh, "value": "1.2.3"}})

    assert cached_latest("opencode", _never_probed) == "1.2.3"
    assert cached_latest("askill", _Probe("0.1.15")) == "0.1.15"


@pytest.mark.parametrize("at", [float("inf"), float("nan")])
def test_a_timestamp_that_is_not_a_number_never_reaches_the_file_again(at) -> None:
    """The file this process writes has to stay readable by every other reader.

    ``inf`` and ``NaN`` neither raise nor mismatch: they survive a JSON round
    trip and merely compare false against every TTL, so simply skipping the
    lookup looks like enough. It is not, because publishing *another* name
    rewrites the whole file, and ``json.dumps`` renders them as the bare
    ``Infinity``/``NaN`` tokens that are not JSON. One poisoned neighbour then
    costs every stricter reader the entire file instead of one entry.
    """

    expired = time.time() - latest_version_cache.SUCCESS_TTL_SECONDS - 1
    _write_entries({"askill": {"at": at, "value": "0.1.15"}, "opencode": {"at": expired, "value": "1.2.3"}})
    _cold_process()

    # opencode re-probes, and publishing its answer rewrites the whole file --
    # carrying whatever askill's entry was parsed into along with it.
    assert cached_latest("opencode", _Probe("1.2.4")) == "1.2.4"

    written = json.loads(cache_path().read_text(encoding="utf-8"), parse_constant=_not_json)
    assert written["entries"]["opencode"]["value"] == "1.2.4"
    assert "askill" not in written["entries"]


def test_an_unforeseen_entry_failure_still_costs_only_a_probe(monkeypatch) -> None:
    """The property, stated where enumerating members kept failing to state it.

    Two review rounds each handed over the next member of a list —
    ``UnicodeDecodeError`` from non-UTF-8 bytes, then ``OverflowError`` from an
    integer too large for a float. Both were fixed by naming that member. This
    test instead raises something no list could have named, and demands the
    answer every other unusable entry already gets: one probe, no traceback. It
    fails the moment the guard is narrowed back into an enumeration.
    """

    def boom(_value: float) -> bool:
        raise _NovelFailure("no enumeration was ever going to list this one")

    monkeypatch.setattr(latest_version_cache, "math", SimpleNamespace(isfinite=boom))
    _write_entries({"askill": {"at": time.time(), "value": "0.1.15"}})

    probe = _Probe("0.1.16")
    assert cached_latest("askill", probe) == "0.1.16"
    assert probe.calls == 1


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
