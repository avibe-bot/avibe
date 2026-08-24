"""Key-parity guard for the backend i18n bundles.

``vibe/i18n`` falls back to English for a missing key, so a translation added to
one bundle and forgotten in the other degrades silently — the user sees English in
a Chinese conversation, and a typo'd key leaks the raw dotted path into a message.
Both failure modes are cheap to catch here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.failure_notices import (
    NOTICE_FAILURE_CLASS_I18N_KEYS,
    NOTICE_ORIGIN_PLATFORM_I18N_KEYS,
    NOTICE_REASON_I18N_KEYS,
    NOTICE_REASON_UNKNOWN_I18N_KEY,
    PER_FIRE_INTERRUPT_REASONS,
)
from core.run_settlement import (
    RUN_INTERRUPTION_REASONS,
    SETTLEMENT_I18N_KEYS,
    SWEEP_I18N_KEYS,
)
from core.services.sessions import SESSION_ARCHIVED_I18N_KEY, session_archived_message
from core.show_session_events import SHOW_EVENT_ERROR_I18N_KEYS
from storage.background import (
    SWEEP_REASON_ORPHANED,
    SWEEP_REASON_QUEUE_HOLD_EXPIRED,
    SWEEP_REASON_TRANSPORT_UNAVAILABLE,
)
from vibe.cli import (
    _MEMORY_CLI_ATTACHMENT_STATE_I18N_KEYS,
    _MEMORY_CLI_PROVIDER_STATE_I18N_KEYS,
    _MEMORY_CLI_REASON_I18N_KEYS,
    _MEMORY_CLI_RUNTIME_STATE_I18N_KEYS,
)
from vibe.i18n import get_supported_languages, t

I18N_DIR = Path(__file__).resolve().parents[1] / "vibe" / "i18n"


def _flatten(value: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flat.update(_flatten(item, path))
        else:
            flat[path] = item
    return flat


def _bundle(lang: str) -> dict[str, str]:
    return _flatten(json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8")))


def test_backend_bundles_have_identical_keys() -> None:
    en = _bundle("en")
    zh = _bundle("zh")
    assert sorted(en) == sorted(zh), (
        f"missing in zh: {sorted(set(en) - set(zh))}; missing in en: {sorted(set(zh) - set(en))}"
    )


def test_no_backend_translation_is_blank() -> None:
    for lang in ("en", "zh"):
        blank = [key for key, value in _bundle(lang).items() if not str(value).strip()]
        assert blank == [], f"{lang} has blank translations: {blank}"


@pytest.mark.parametrize(
    "key",
    sorted(
        {
            *_MEMORY_CLI_RUNTIME_STATE_I18N_KEYS.values(),
            *_MEMORY_CLI_PROVIDER_STATE_I18N_KEYS.values(),
            *_MEMORY_CLI_ATTACHMENT_STATE_I18N_KEYS.values(),
            *_MEMORY_CLI_REASON_I18N_KEYS.values(),
            "memory.cli.runtimeState.unknown",
            "memory.cli.providerState.unknown",
            "memory.cli.attachmentCaptureState.unknown",
            "memory.cli.reason.unknown",
            "memory.cli.unknownVersion",
        }
    ),
)
def test_every_memory_cli_status_label_resolves(key: str) -> None:
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang}"
        assert resolved.strip()


@pytest.mark.parametrize(
    "reason,key",
    sorted(SETTLEMENT_I18N_KEYS.items()) + sorted(SWEEP_I18N_KEYS.items()),
)
def test_every_run_settlement_reason_resolves(reason: str, key: str) -> None:
    # These strings land in the run's user-visible ``error`` column, so an unresolved
    # key would show up verbatim in the Runs UI and the callback message.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()


@pytest.mark.parametrize(
    ("key", "language", "expected"),
    [
        (
            "harness.run.interrupted.restarted",
            "en",
            "[Avibe Harness] This run was interrupted during execution. Completed work is "
            "preserved. Trigger it again if the work still needs doing.",
        ),
        (
            "harness.run.interrupted.restarted",
            "zh",
            "[Avibe Harness] 本次 run 执行途中被中断。已执行的部分不会丢失，"
            "如果这项工作仍需完成，请重新触发。",
        ),
        (
            "harness.run.interrupted.orphaned",
            "en",
            "[Avibe Harness] Nothing is executing this run, most likely because the service "
            "restarted during execution. Completed work is preserved. Trigger it again if the "
            "work still needs doing.",
        ),
        (
            "harness.run.interrupted.orphaned",
            "zh",
            "[Avibe Harness] 本次 run 没有任何执行在推进它，通常是服务在它执行途中"
            "重启导致的。已执行的部分不会丢失，如果这项工作仍需完成，请重新触发。",
        ),
    ],
)
def test_interrupted_run_detail_copy_matches_product_language(
    key: str,
    language: str,
    expected: str,
) -> None:
    assert t(key, language) == expected


def test_sweep_reason_i18n_map_covers_every_store_sweep_reason() -> None:
    # ``SWEEP_I18N_KEYS`` spells its keys as literals so ``core.run_settlement``
    # stays dependency-free (see the comment there). This is the guard that makes
    # that safe: add a sweep reason in the store without a translation and the
    # sweep would stamp a run with an empty ``error``, which reads to the user as
    # "it just failed" with no explanation.
    assert set(SWEEP_I18N_KEYS) == {
        SWEEP_REASON_ORPHANED,
        SWEEP_REASON_TRANSPORT_UNAVAILABLE,
        SWEEP_REASON_QUEUE_HOLD_EXPIRED,
    }


@pytest.mark.parametrize(
    "code,key",
    sorted(SHOW_EVENT_ERROR_I18N_KEYS.items()),
)
def test_every_show_event_error_resolves(code: str, key: str) -> None:
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (code={code})"
        assert resolved.strip()


def test_session_archived_message_resolves_in_every_language() -> None:
    # This string ships in the ``409 session_archived`` response body, which direct
    # API/CLI consumers read verbatim and the Web UI renders as its fallback when
    # ``errors.session_archived`` is missing — so an unresolved key would leak the
    # dotted path to a user, and a missing translation would leak English.
    for lang in get_supported_languages():
        resolved = session_archived_message(lang)
        assert resolved != SESSION_ARCHIVED_I18N_KEY, f"{SESSION_ARCHIVED_I18N_KEY} is not translated in {lang}"
        assert resolved.strip()
        assert resolved == t(SESSION_ARCHIVED_I18N_KEY, lang)


def test_notice_reason_i18n_map_covers_exactly_the_interruption_lane() -> None:
    # ``harness.notice.interrupted`` renders the reason INTO the sentence a user
    # reads, so an unmapped reason is a wire identifier leaking into product copy —
    # "was interrupted (backend_refresh)". Same drift guard as ``SWEEP_I18N_KEYS``
    # above: the map has to track ``RUN_INTERRUPTION_REASONS`` exactly, since that
    # frozenset is what gates the interrupted branch. A reason added there without a
    # label would silently fall back to the generic string and lose its explanation.
    assert set(NOTICE_REASON_I18N_KEYS) == set(RUN_INTERRUPTION_REASONS), (
        "unlabelled: "
        f"{sorted(set(RUN_INTERRUPTION_REASONS) - set(NOTICE_REASON_I18N_KEYS))}; "
        "stale: "
        f"{sorted(set(NOTICE_REASON_I18N_KEYS) - set(RUN_INTERRUPTION_REASONS))}"
    )


@pytest.mark.parametrize(
    "reason,key",
    sorted(NOTICE_REASON_I18N_KEYS.items()) + [("<unmapped>", NOTICE_REASON_UNKNOWN_I18N_KEY)],
)
def test_every_notice_reason_label_resolves(reason: str, key: str) -> None:
    # Including the fallback: an unknown reason must render a LOCALIZED generic
    # label, so if that key were missing the notice would print the dotted path
    # instead — a worse leak than the raw reason it replaced.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()


def test_notice_failure_class_map_covers_exactly_the_per_fire_lane() -> None:
    # The FAILED lane's own vocabulary, drift-pinned the same way the interrupted
    # lane's is — and against a DERIVED set, so the two maps cannot both claim a
    # reason or both drop one. ``PER_FIRE_INTERRUPT_REASONS`` is the settlement and
    # sweep vocabularies minus ``RUN_INTERRUPTION_REASONS``, i.e. exactly the
    # discriminator ``is_interruption`` applies, so a reason moved between lanes
    # fails here instead of silently losing its label.
    assert set(NOTICE_FAILURE_CLASS_I18N_KEYS) == set(PER_FIRE_INTERRUPT_REASONS), (
        "unlabelled: "
        f"{sorted(set(PER_FIRE_INTERRUPT_REASONS) - set(NOTICE_FAILURE_CLASS_I18N_KEYS))}; "
        "stale: "
        f"{sorted(set(NOTICE_FAILURE_CLASS_I18N_KEYS) - set(PER_FIRE_INTERRUPT_REASONS))}"
    )
    # And the two maps are disjoint: one reason, one lane, one label.
    assert not set(NOTICE_FAILURE_CLASS_I18N_KEYS) & set(NOTICE_REASON_I18N_KEYS)


def test_a_dispatch_failure_class_joins_the_per_fire_lane_and_not_the_interrupted_one() -> None:
    """#1060's class has to land in the FAILED lane, and the derivation has to say why.

    The pin above is an equality between two sets that both grow, so it stays green for
    a reason added correctly AND for one added to the wrong source vocabulary. This is
    the direction pin for ``delivery_target_missing`` specifically, and each clause is a
    thing that would silently break if it were placed in the interrupted lane instead:

    * ``failure_id`` would become ``interrupt:{run}:{reason}`` rather than the bare run
      id the live path's dedup looks up, so the notice would be re-sent;
    * every fire of a permanently broken definition would notify separately, since the
      interruption lane bypasses streak suppression;
    * the definition would read HEALTHY, because the health window excludes
      out-of-band interruptions.

    All three are the opposite of what #1060 asked for. It is a per-fire verdict about
    the definition, so it belongs where the per-fire verdicts are.
    """

    from core.run_settlement import (
        DISPATCH_FAILURE_REASONS,
        INTERRUPT_REASON_DELIVERY_TARGET_MISSING,
    )

    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING in DISPATCH_FAILURE_REASONS
    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING not in RUN_INTERRUPTION_REASONS, (
        "the interrupted lane would change the notice's identity, unsuppress it, and "
        "take it out of derived health"
    )
    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING in PER_FIRE_INTERRUPT_REASONS, (
        "so the derived per-fire set has to admit it, or its label has nowhere to live"
    )
    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING in NOTICE_FAILURE_CLASS_I18N_KEYS

    # And it is NOT a settlement or a sweep reason: those vocabularies describe a run
    # that was dispatched, and a run whose target could not be resolved never was. A
    # ``harness.run.interrupted.*`` twin would also be copy no caller renders, because
    # the run's ``error`` column already holds the exception's own text.
    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING not in SETTLEMENT_I18N_KEYS
    assert INTERRUPT_REASON_DELIVERY_TARGET_MISSING not in SWEEP_I18N_KEYS


@pytest.mark.parametrize("reason,key", sorted(NOTICE_FAILURE_CLASS_I18N_KEYS.items()))
def test_every_notice_failure_class_label_resolves(reason: str, key: str) -> None:
    # No generic fallback here by design (``notice_failure_class_i18n_key`` returns
    # ``None`` and the line is omitted), so every mapped label has to be real in
    # every language or the class line prints a dotted path.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (reason={reason})"
        assert resolved.strip()
        assert reason not in resolved, (
            f"{key} leaks the wire value {reason!r} into product copy in {lang}"
        )


@pytest.mark.parametrize("platform,key", sorted(NOTICE_ORIGIN_PLATFORM_I18N_KEYS.items()))
def test_every_notice_origin_platform_label_resolves(platform: str, key: str) -> None:
    # The origin line renders the platform INSIDE a translated sentence, so an
    # unresolved key would print a dotted path where a product name belongs. Same
    # contract as the failure-class labels, and the same reason the map is closed: an
    # unmapped platform takes no line rather than leaking its wire value.
    for lang in get_supported_languages():
        resolved = t(key, lang)
        assert resolved != key, f"{key} is not translated in {lang} (platform={platform})"
        assert resolved.strip()


def test_the_origin_platform_map_covers_every_registered_platform() -> None:
    """Drift pin against the platform registry, in BOTH directions.

    A platform Avibe can run on but cannot name has no origin line at all, which is a
    silent hole rather than a visible one. A label for a platform that does not exist is
    copy no caller can reach. Note the wire value is ``lark``, not ``feishu``:
    ``modules/im/feishu.py`` builds its contexts with ``platform="lark"``, and keying the
    map on the product name would have made every Feishu origin unnameable.
    """

    from config.platform_registry import PLATFORM_REGISTRY

    assert set(NOTICE_ORIGIN_PLATFORM_I18N_KEYS) == set(PLATFORM_REGISTRY), (
        "missing labels: "
        f"{sorted(set(PLATFORM_REGISTRY) - set(NOTICE_ORIGIN_PLATFORM_I18N_KEYS))}; "
        "labels for unknown platforms: "
        f"{sorted(set(NOTICE_ORIGIN_PLATFORM_I18N_KEYS) - set(PLATFORM_REGISTRY))}"
    )


def test_the_origin_lines_keep_their_placeholders_in_both_languages() -> None:
    """Every origin sentence has to carry the ids it exists to render.

    A translation that drops ``{channel}`` or ``{url}`` silently renders a notice that
    names no conversation and offers no link — the exact hole this round closes, but
    reintroduced in one bundle only, which is the failure mode ``vibe/i18n``'s
    English fallback hides.
    """

    expected = {
        "harness.notice.origin": {"{origin}"},
        "harness.notice.originLink": {"{url}"},
        "harness.notice.originChannel": {"{platform}", "{channel}"},
        "harness.notice.originChannelThread": {"{platform}", "{channel}", "{thread}"},
        "harness.notice.originDirect": {"{platform}", "{user}"},
    }
    for lang in ("en", "zh"):
        bundle = _bundle(lang)
        for key, placeholders in expected.items():
            assert key in bundle, f"{key} missing from {lang}"
            missing = [name for name in placeholders if name not in bundle[key]]
            assert not missing, f"{lang} {key} dropped {missing}: {bundle[key]!r}"


def test_the_command_fires_refusal_sentences_resolve_in_every_language() -> None:
    """OBS-HARNESS-COMMAND-TASK-018 — the run's ``error`` column is product copy.

    Both keys are sentences ``core/scheduled_tasks.py`` composes itself, and both land
    in the run ledger's ``error``, which the Harness detail pane, ``vibe runs show`` and
    the failure notice render verbatim — the same column the settlement reasons above
    already fill through ``vibe/i18n``. ``str(exc)`` and a worker's own stderr stay
    untranslated on that channel because nobody here wrote them; our own copy does not
    get that exemption, and an unresolved key would print the dotted path to a user.
    """

    from core.scheduled_tasks import _TASK_RESULT_NOT_RECORDED_I18N_KEY

    for key in (_TASK_RESULT_NOT_RECORDED_I18N_KEY, "harness.command.workerNotRecorded"):
        for lang in get_supported_languages():
            resolved = t(key, lang)
            assert resolved != key, f"{key} is not translated in {lang}"
            assert resolved.strip()
        assert t(key, "zh") != t(key, "en"), (
            f"{key} was added to zh.json as the English string; a Chinese install would "
            "read a mixed-language failure"
        )
