"""Who gets told about a failed harness run, and how often.

The durable half — ``metadata.owed_failure_notice``, stamped by whichever UPDATE
transitions a run to ``failed`` — lives in ``storage/background.py``. This module
owns the POLICY over it, kept separate from the delivery plumbing in
``core/scheduled_tasks.py`` so every branch is testable without a controller.

Three lanes, and the asymmetries between them are the whole design:

* **Failures** of a definition can recur *unboundedly* — every tick produces
  another one — so without a scope the user gets a message per tick forever. They
  share one **consecutive-failure streak** with a single canonical notice.
* **Interruptions** (a deploy, an eviction, a lifetime cap, a user Stop) hit a run
  **at most once**: the run is terminalized and never re-dispatched, so the notice
  count is bounded by the number of runs. Per-run notices are self-bounding, and
  there is nothing to suppress. Giving them a streak-shaped scope would silence
  all but one of the several runs a single restart interrupts — and
  ``create_per_run`` means there genuinely are several.
* **Binding changes** are the odd one out twice over. They are the only notice a
  SUCCEEDED run can owe — a ``create_once`` definition whose pinned session was
  deleted rebinds, retries, and works — so no terminal transition stamps them and
  ``core/scheduled_tasks.py`` writes them explicitly at the moment the transition
  is recorded. And they arrive already scoped: ``SessionBindingChange.signature``
  is "one broken binding, one notification", so they carry their own bound and are
  delivered per-transition like an interruption rather than per-streak.

Suppression is applied HERE, by the drain, not by the terminal writers. Three
reasons. Every terminal transition stamps unconditionally, and a policy predicate
inside each of five writers is five chances for them to disagree. A streak read at
stamp time races with concurrent executions of the same definition, while at drain
time one component reads it once. And definition-level notification policy does
not belong in SQL UPDATE helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from storage.sqlite_semantics import sqlite_cast_integer

from core.run_settlement import (
    DISPATCH_FAILURE_REASONS,
    INTERRUPT_REASON_DELIVERY_TARGET_MISSING,
    INTERRUPT_REASON_EVICTED,
    INTERRUPT_REASON_LIFETIME_TIMEOUT,
    INTERRUPT_REASON_RESTARTED,
    RUN_INTERRUPTION_REASONS,
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_REFUSED_CONCURRENT_TURN,
    SETTLED_BY_STOPPED,
    SETTLEMENT_I18N_KEYS,
    SWEEP_I18N_KEYS,
)

#: What the drain decided to do with one owed notice this tick.
ACTION_DELIVER = "deliver"
#: Not delivered, no attempt consumed, no backoff burned — reconsidered next tick.
#: Used both while an earlier run of the definition has not settled and while this
#: streak's canonical notice is still trying.
ACTION_DEFER = "defer"
#: Terminal: the streak's notice was already delivered, so this row is a duplicate.
ACTION_SKIP = "skip"

#: Exponential backoff from the 2s tick, capped. The Nth FAILED attempt waits
#: ``BACKOFF_SECONDS[N - 1]``, so the first failure waits the first declared interval
#: and the sequence advances exactly once per consumed attempt. Running off the end is
#: the dead letter.
BACKOFF_SECONDS: tuple[float, ...] = (2.0, 8.0, 32.0, 128.0, 512.0)
#: How many attempts a notice gets before it dead-letters to ``failed``. A visible
#: dead letter, rather than a silent drop or an infinite loop.
#:
#: Derived, not chosen: N attempts are separated by N-1 waits, so one attempt per
#: declared interval plus the attempt that finds no interval left is exactly
#: ``len(BACKOFF_SECONDS) + 1``. Writing it as ``len(BACKOFF_SECONDS)`` made the
#: sequence one rung shorter than it declared and pushed the whole mapping off by one.
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

#: How long a claimed notice is reserved to the owner performing its delivery.
#:
#: The drain's single-flight authority is a CLAIM taken before the external send: one
#: guarded UPDATE consumes the attempt and pushes ``next_attempt_at`` this far out, so
#: a second owner either loses that CAS or reads the lease and stands down. Without it
#: both owners read ``pending``, both send, and only then does either write — a
#: duplicate message that no database predicate can recall.
#:
#: This constant is therefore the RECOVERY BOUND for a claimant that dies mid-send:
#: the lease is stored as an instant, not held as a lock, so a killed process releases
#: it by expiry and the notice becomes eligible again with no operator action.
#:
#: Why 600 s, from both sides. It has to exceed the longest a live delivery can
#: legitimately take, or a healthy claimant is overtaken while still sending and the
#: duplicate comes back: a full ladder walk is five rungs, each able to sit on an
#: adapter's HTTP timeout of roughly 30-60 s, so the worst case is a few minutes. And
#: it has to exceed the retry ladder's own cap of ``BACKOFF_SECONDS[-1]`` (512 s), so a
#: lease can never expire sooner than the backoff a failed attempt would have armed —
#: otherwise the recovery path would become the faster retry and quietly replace the
#: backoff it exists to respect.
#:
#: The cost is stated plainly: expiry-retry means the guarantee is AT LEAST ONCE, not
#: exactly once. A claimant that died after the transport accepted its send but before
#: its acknowledgement leaves a delivered message and an eligible row, and recovery
#: sends again. ``NOTICE_DELIVERY_TIMEOUT_SECONDS`` adds a second entrance to the same
#: window rather than a new one: a claimant CANCELLED after the transport accepted its
#: request is in exactly that state, having delivered without acknowledging. Closing it
#: needs an idempotency key the transport itself honours; until then the residual is
#: one duplicate per claimant death or cancel-after-accept inside that window. (Not
#: "bounded by ``MAX_ATTEMPTS``": a claimant that dies on EVERY attempt never survives
#: to write the dead letter, so claims keep consuming attempts past the ceiling — the
#: bound is per death, not global.)
CLAIM_LEASE_SECONDS: float = 600.0

#: How long ONE delivery — the whole ladder walk, not one rung — may take before it is
#: cancelled and its attempt treated as failed.
#:
#: The claim above buys single-flight against a competing owner and, through lease
#: expiry, recovery from an owner that DIES. Neither covers an owner that never
#: RETURNS: a transport that accepted the request and hung (a half-open socket, a
#: platform that stopped answering, an adapter with no timeout of its own) leaves the
#: pass suspended inside the send, the row ineligible for as long as the lease holds,
#: and nothing anywhere reporting it — the drain is not crashed, it is stopped. The
#: notice would be owed indefinitely, and so would every notice behind it.
#:
#: Why 300 s, from both sides, on the same arithmetic as the lease. The worst
#: LEGITIMATE walk — five rungs, each able to sit on an adapter's HTTP timeout of
#: roughly 30-60 s — comes to AT MOST this same 300 s, so the bound MATCHES the worst
#: case rather than exceeding it: a walk that saturates every rung's timeout is
#: cancelled at the line and consumed as an ordinary failed attempt, retried on the
#: declared backoff, and any rung that had already persisted its receipt is absorbed
#: by the duplicate short-circuit on the retry. That trade is deliberate — a rarely
#: mislabelled slowest-possible walk costs one bounded retry, while a longer deadline
#: would hold every notice behind a wedge for that much longer. And it has to stay
#: strictly BELOW ``CLAIM_LEASE_SECONDS``, so a timed-out claimant is cancelled and
#: its backoff durably written while its OWN lease still holds: a replacement claimant
#: can never coexist with a transport coroutine that is still unwinding.
#:
#: What the deadline does to the claim is the load-bearing half: it CONSUMES it. The
#: attempt was made durable by the claim before the send, so the timeout takes the
#: ordinary retry path and arms ``BACKOFF_SECONDS[attempts - 1]``. Rewinding
#: ``attempts`` instead would let a transport that hangs every time retry without bound
#: and never dead-letter — the unbounded loop the raising-rung handler exists to
#: prevent, reached by a different road.
#:
#: The stated assumption, because it is an assumption and not a proof: the bound holds
#: only for a transport that HONOURS cancellation. ``asyncio.wait_for`` cancels the
#: inner task and awaits that cancellation before raising, so our adapters — none of
#: which swallow ``CancelledError`` — are cancelled rather than detached. One that
#: swallowed it would hang the deadline too and re-wedge the drain; because the drain
#: is dispatched off the store watch rather than awaited by it, that failure is
#: confined to notice delivery instead of stopping every periodic pass.
NOTICE_DELIVERY_TIMEOUT_SECONDS: float = 300.0

#: How long ONE DRAIN PASS may keep pulling notices out of its batch.
#:
#: What this is NOT for, refuted first because the obvious diagnosis is wrong: a
#: wedged batch does not re-select itself forever. ``list_owed_failure_notices``
#: orders by ``next_attempt_at ASC`` — least-recently-deferred first — so the fresh
#: backoff instants a slow pass just armed sort BEHIND a notice stamped while it was
#: working, and the newcomer is picked up next pass. Starvation by re-selection is
#: already closed by that ordering.
#:
#: What it IS for is the SERIAL cost of one pass. Deliveries within a pass run one at
#: a time, and that is deliberate: row #2 of a streak only observes row #1's ``sent``
#: because the passes are ordered, so parallelising them reopens the same-streak
#: duplicate window that serialization closes. But a batch of ten, each legitimately
#: sitting on ``NOTICE_DELIVERY_TIMEOUT_SECONDS``, is ten times that deadline before
#: the pass so much as returns — and a notice stamped one second in waits behind all
#: of it. So the fix is a BUDGET, not concurrency.
#:
#: Why twice the delivery deadline, from both sides. It has to be at least ONE full
#: worst-case delivery, or a pass could spend its budget before finishing the notice
#: it started and the drain would make no progress at all — the budget is checked
#: BETWEEN notices and never cancels a delivery in flight, so anything smaller just
#: means "one notice per pass" while a value below the deadline would additionally
#: read as an attempt to bound a send it cannot bound. And it has to be small enough
#: that a fresh notice's wait is a function of the budget rather than of the batch
#: size: the worst wait becomes budget + one delivery instead of ten deliveries, and
#: it stops growing when the batch limit does.
#:
#: What a budget-truncated pass costs the rows it did not reach: nothing. They were
#: never claimed, so no attempt is consumed and no backoff is armed; they stay exactly
#: as eligible as they were, and the ASC ordering puts the oldest of them first.
NOTICE_DRAIN_PASS_BUDGET_SECONDS: float = 2 * NOTICE_DELIVERY_TIMEOUT_SECONDS

#: How long the settled-prefix deferral waits on an earlier nonterminal run before
#: treating it as settled. See ``earliest_unsettled_run_before``.
DEFERRAL_STALE_AFTER_SECONDS = 3600.0

#: What a notice is ABOUT. Absent means ``failure``, so every notice stamped
#: before this field existed keeps its lane — the field is additive, and the
#: eligibility index is expressed over ``state``/``next_attempt_at`` only, so
#: adding it needs no migration.
NOTICE_KIND_FAILURE = "failure"
#: A pinned session binding was replaced under the user. Unlike a failure this can
#: be reported by a run that SUCCEEDED, which is exactly why it needs its own kind:
#: nothing in the failure lane stamps a succeeded row.
NOTICE_KIND_BINDING_CHANGE = "binding_change"

#: How long a DEFERRED notice steps aside before being reconsidered.
#:
#: Deferral has to be durable, not just a Python-side ``continue``. A deferred row
#: that stays immediately-eligible is re-selected by every tick and keeps occupying
#: the batch, so one definition with more than a batch worth of pending failures
#: starves every other definition's notices — and interruptions — indefinitely,
#: with no error and a drain that looks healthy because it is busy.
#:
#: Short enough to stay responsive once the blocker resolves (the canonical is sent
#: or dead-letters, or the earlier run settles), long enough that the row is not
#: back in the next tick. It consumes NO attempt: the row has not been tried.
DEFERRAL_RECHECK_SECONDS = 30.0


#: SHORT user-visible labels for the interruption lane's reasons.
#:
#: ``harness.notice.interrupted`` renders its ``reason=`` inside the sentence, so the
#: value substituted there is product copy whether or not anyone treated it as such.
#: The drain interpolated the WIRE value, and a Chinese user was told 被中断
#: (backend_refresh): an internal identifier, untranslated, mid-sentence.
#:
#: Same construction as ``SETTLEMENT_I18N_KEYS`` / ``SWEEP_I18N_KEYS`` and for the same
#: reason — an explicit map rather than ``f"harness.notice.reason.{reason}"`` — so the
#: settlement vocabulary stays free to change without a renamed value silently
#: degrading into a raw key in the middle of a translated sentence.
#:
#: These are LABELS, not the long explanations in ``harness.run.interrupted.*``. That
#: family is the run's ``error`` column, read on its own; this one is a parenthetical
#: inside a headline, so it has to be a phrase.
#:
#: The key set must equal ``RUN_INTERRUPTION_REASONS``, since that frozenset is what
#: gates the interrupted branch; pinned by
#: ``tests/test_i18n_backend_keys.py::test_notice_reason_i18n_map_covers_exactly_the_interruption_lane``.
NOTICE_REASON_I18N_KEYS: dict[str, str] = {
    SETTLED_BY_STOPPED: "harness.notice.reason.stopped",
    SETTLED_BY_BACKEND_REFRESH: "harness.notice.reason.backendRefresh",
    INTERRUPT_REASON_EVICTED: "harness.notice.reason.evicted",
    INTERRUPT_REASON_RESTARTED: "harness.notice.reason.restarted",
    INTERRUPT_REASON_LIFETIME_TIMEOUT: "harness.notice.reason.lifetimeTimeout",
    # The sweep's "owner vanished" class, spelled as a literal exactly as
    # ``RUN_INTERRUPTION_REASONS`` does — the constant itself lives in the store, and
    # importing storage from core would invert the layering.
    "orphaned": "harness.notice.reason.orphaned",
}

#: The label for a reason with no entry above — a LOCALIZED generic, never the wire
#: value and never the dotted key path.
#:
#: ``NOTICE_REASON_I18N_KEYS.get(reason, reason)`` would be the obvious spelling and is
#: the original defect again, reachable in precisely the window the drift test exists to
#: close: between a reason being added to the lane and CI noticing it has no label.
NOTICE_REASON_UNKNOWN_I18N_KEY = "harness.notice.reason.unknown"


def notice_reason_i18n_key(reason: Optional[str]) -> str:
    """The i18n key for one interruption reason's short label."""

    return NOTICE_REASON_I18N_KEYS.get(
        str(reason or "").strip(), NOTICE_REASON_UNKNOWN_I18N_KEY
    )


#: The ``interrupt_reason`` values that stay in the FAILED lane — the per-fire verdicts.
#:
#: DERIVED, not retyped: every reason the settlement, sweep and dispatch-failure
#: vocabularies name, minus the ones ``RUN_INTERRUPTION_REASONS`` claims for the
#: interrupted lane. That subtraction is the same discriminator ``is_interruption``
#: applies, so the two cannot disagree about which lane a reason belongs to.
#:
#: ``DISPATCH_FAILURE_REASONS`` joined the union rather than being retyped into one of
#: the other two, and that choice is load-bearing for this derivation's honesty. The
#: obvious shortcut — declaring ``delivery_target_missing`` in ``SETTLEMENT_I18N_KEYS``
#: with a ``harness.run.interrupted.*`` twin — would have made the drift pin pass while
#: recording a dispatch failure as a waiter settlement AND shipping a long description
#: no caller renders (``SETTLEMENT_I18N_KEYS`` is only ever consulted for a run whose
#: turn actually existed). One vocabulary per origin, one union here.
PER_FIRE_INTERRUPT_REASONS = frozenset(
    (set(SETTLEMENT_I18N_KEYS) | set(SWEEP_I18N_KEYS) | set(DISPATCH_FAILURE_REASONS))
    - set(RUN_INTERRUPTION_REASONS)
)

#: SHORT user-visible labels for the FAILED lane's classes — D5's "the error and its
#: class", which the per-fire lane dropped entirely.
#:
#: WHY NOT ``NOTICE_REASON_I18N_KEYS``. Its key set is drift-pinned to
#: ``RUN_INTERRUPTION_REASONS``, which is precisely the set this lane EXCLUDES: a reason
#: with an entry there always takes the interrupted headline, so reusing that map here
#: would render the localized generic ("for a reason outside the run itself") for every
#: failed-lane notice — a sentence that is both uninformative and, on this lane, untrue.
#: Same construction, second vocabulary.
#:
#: WHY NOT ``harness.run.interrupted.*`` either: that family is the run's ``error``
#: column, already printed on its own line by ``harness.notice.error``, so reusing it
#: would print the same sentence twice. These are parenthetical labels.
#:
#: Key set pinned to ``PER_FIRE_INTERRUPT_REASONS`` by
#: ``tests/test_i18n_backend_keys.py::test_notice_failure_class_map_covers_exactly_the_per_fire_lane``.
NOTICE_FAILURE_CLASS_I18N_KEYS: dict[str, str] = {
    SETTLED_BY_NO_TERMINAL_RESULT: "harness.notice.class.noTerminalResult",
    SETTLED_BY_REFUSED_CONCURRENT_TURN: "harness.notice.class.refusedConcurrentTurn",
    # Sweep reasons, spelled as literals for the same layering reason
    # ``NOTICE_REASON_I18N_KEYS`` spells ``orphaned`` as one.
    "transport_unavailable": "harness.notice.class.transportUnavailable",
    "queue_hold_expired": "harness.notice.class.queueHoldExpired",
    # The dispatch lane. #1060's field case: the notice's ``error`` line already
    # carries "agent session id not found: <id>", which is the exception's own text
    # and names the session — but a raw id is not a diagnosis. The label is what turns
    # it into one, and it is the only place the notice says the failure is about the
    # DESTINATION rather than about the work.
    INTERRUPT_REASON_DELIVERY_TARGET_MISSING: "harness.notice.class.deliveryTargetMissing",
}


def notice_failure_class_i18n_key(reason: Optional[str]) -> Optional[str]:
    """The i18n key for a failed-lane class label, or ``None`` when there is none.

    ``None`` rather than a generic, deliberately, and it is the opposite choice from
    ``notice_reason_i18n_key``'s. There the reason is rendered INSIDE a sentence that is
    printed either way, so a missing label has to degrade to something readable. Here the
    label IS the line: with nothing real to name, the honest rendering is no line at all
    rather than "Class: unknown" — copy about nothing, on the lane that already carries
    the most lines.
    """

    return NOTICE_FAILURE_CLASS_I18N_KEYS.get(str(reason or "").strip())


#: Display names for the platform a definition was CREATED on, keyed by the wire value
#: ``PLATFORM_REGISTRY`` uses (note ``lark``, not ``feishu``). A CLOSED map for the same
#: reason ``NOTICE_FAILURE_CLASS_I18N_KEYS`` is one: the origin line renders inside a
#: translated sentence, so an unknown platform must take no line rather than
#: interpolate a raw identifier into product copy. Drift-pinned against the registry by
#: ``test_every_notice_origin_platform_label_resolves``.
NOTICE_ORIGIN_PLATFORM_I18N_KEYS: dict[str, str] = {
    "slack": "harness.notice.platform.slack",
    "discord": "harness.notice.platform.discord",
    "telegram": "harness.notice.platform.telegram",
    "lark": "harness.notice.platform.lark",
    "wechat": "harness.notice.platform.wechat",
    "avibe": "harness.notice.platform.avibe",
}


def notice_origin_platform_i18n_key(platform: Optional[str]) -> Optional[str]:
    """The i18n key for a creation-origin platform label, or ``None`` when unmapped."""

    return NOTICE_ORIGIN_PLATFORM_I18N_KEYS.get(str(platform or "").strip())


@dataclass(frozen=True)
class NoticeDecision:
    action: str
    reason: str = ""


def is_interruption(notice: Optional[dict[str, Any]]) -> bool:
    """Whether this notice belongs to the per-run interruption lane.

    Membership in a closed set, never ``interrupt_reason is not None``: master
    stamps that field for ``no_terminal_result``, ``refused_concurrent_turn``,
    ``transport_unavailable`` and ``queue_hold_expired`` too, all of which recur on
    every fire and therefore belong in the suppressed failure lane. Reading
    presence as "interrupted" would give the commonest failures an unsuppressed
    notice per tick — the daily spam this scope exists to prevent.
    """

    reason = str((notice or {}).get("interrupt_reason") or "").strip()
    return reason in RUN_INTERRUPTION_REASONS


def notice_kind(notice: Optional[dict[str, Any]]) -> str:
    """This notice's lane, defaulting to ``failure`` for rows stamped without one."""

    return str((notice or {}).get("kind") or "").strip() or NOTICE_KIND_FAILURE


def is_binding_change(notice: Optional[dict[str, Any]]) -> bool:
    """Whether this notice reports a session binding that was replaced."""

    return notice_kind(notice) == NOTICE_KIND_BINDING_CHANGE


def bypasses_suppression(notice: Optional[dict[str, Any]]) -> bool:
    """Whether this notice is delivered per-run, outside the failure streak.

    Two lanes qualify, for the SAME reason and not by accident:

    * an interruption hits a run at most once, so per-run notices are self-bounding;
    * a binding change is already scoped by ``SessionBindingChange.signature`` — one
      broken binding, one notification — so it carries its own bound and does not
      need the streak's.

    Asking this instead of ``is_interruption`` at the drain's streak gate matters
    beyond taste: ``failure_streak_decision`` accepts a ``succeeded`` anchor, so
    reading the streak for a binding notice on a successful rebind would sweep in the
    definition's surrounding FAILURES and defer or skip the notice behind a canonical
    row that has nothing to do with it — the notice would be stamped, durable, and
    never sent.
    """

    return is_interruption(notice) or is_binding_change(notice)


def decide(
    *,
    run_id: str,
    definition_id: Optional[str],
    notice: Optional[dict[str, Any]],
    streak_facts: Optional[dict[str, Any]],
    earlier_unsettled: Optional[dict[str, Any]],
) -> NoticeDecision:
    """What to do with one pending owed notice.

    ``streak_facts`` is ``SQLiteBackgroundTaskStore.failure_streak_decision``'s answer
    — ``in_streak``, ``has_sent_elsewhere``, ``earliest_pending_id`` — or ``None`` when
    no streak was read (the bypass lanes below return before it is consulted).
    ``earlier_unsettled`` is an earlier-created execution of the same definition that
    has not settled.

    Facts, not rows. This function used to receive the streak itself and rederive
    "anyone sent?" and "who is canonical?" from it in Python, which is what made the
    read cost the streak's length and let the three reads behind it disagree about
    where the streak ENDED. Both questions are one indexed comparison each, so they
    are answered where the rows are. The outcomes are unchanged, reason strings
    included.
    """

    if is_interruption(notice):
        # Per-run, always, with no suppression scope and no deferral: the streak is
        # irrelevant to a notice that can only happen once for this run, and
        # deferring behind an unrelated run would delay a D1 notice for no gain.
        return NoticeDecision(ACTION_DELIVER, "interruption")

    if is_binding_change(notice):
        # Same shape, different bound. The transition is already deduped on
        # ``SessionBindingChange.signature`` before it is ever stamped, so there is
        # nothing here for the streak to suppress — and the run this notice rides on
        # may have SUCCEEDED, which no streak reasoning is written for.
        return NoticeDecision(ACTION_DELIVER, "binding_change")

    if not definition_id:
        # A one-off or ad-hoc run has no definition, so it has no streak and is
        # never suppressed. Every such failure is a first failure.
        return NoticeDecision(ACTION_DELIVER, "no_definition")

    if earlier_unsettled is not None:
        # Classification is deferred while any earlier-created run of the same
        # definition is nonterminal, so the streak is a function of a settled prefix
        # and cannot be rewritten by a straggler. Cheap for every definition that
        # holds an execution lock — those serialize, so the predicate never fires.
        return NoticeDecision(ACTION_DEFER, f"earlier_run_unsettled:{earlier_unsettled['id']}")

    facts = streak_facts or {}

    # Evidence of delivery ANYWHERE in the streak settles it: this row is a
    # duplicate. Note this is checked before the canonical is consulted, because a
    # sent notice is not necessarily the earliest row.
    if facts.get("has_sent_elsewhere"):
        return NoticeDecision(ACTION_SKIP, "streak_already_notified")

    # The canonical notice is the earliest still-trying row. ``failed`` (dead
    # lettered) and ``skipped`` rows drop out, which is exactly how promotion
    # works: a streak whose canonical exhausted its retries still owes the user the
    # news, so the claim on delivery outlives any single row.
    #
    # ``None`` covers two cases that decide the same way and always did: this run
    # belongs to no streak at all (``in_streak`` false — a one-off, an interruption,
    # another definition's run), and a streak in which nothing is pending any more.
    # Both mean nobody else has a claim on telling the user.
    canonical_id = facts.get("earliest_pending_id")
    if canonical_id is None or canonical_id == run_id:
        return NoticeDecision(ACTION_DELIVER, "canonical")

    # The canonical notice is still pending — by design, because it keeps retrying.
    # Absence of an acknowledgement is NOT evidence that nothing is in flight, so
    # this row waits rather than notifying. At most one notice per streak is ever in
    # flight, which is the property "once, not daily" was asking for.
    return NoticeDecision(ACTION_DEFER, f"canonical_pending:{canonical_id}")


def _attempts_read(value: Any) -> int:
    """``attempts`` as SQLite's CAS will read it — CAST semantics, not ``int()``.

    The field lives in a JSON blob, so it can hold anything; the value this policy
    increments and the value the guarded write asserts MUST be the same number, or
    the claim can never match and the row stays eligible and unchanged on every
    drain pass — one of the ten batch slots occupied forever, every notice behind
    it starved. The first version of this function degraded to 0 on any conversion
    error, which agreed with SQLite for ``"abc"`` but diverged for every
    numeric-PREFIX string (``"3x"`` reads 3 under ``CAST``, 0 under ``int()``),
    for ``"1e100"`` (1 vs 0), and for out-of-range numbers (SQLite saturates at
    the signed-64-bit bounds; ``int()`` does not) — exactly the split the round-18
    review named. Both readers now consume one dependency-neutral model of
    ``CAST(... AS INTEGER)`` (``storage.sqlite_semantics.sqlite_cast_integer``),
    pinned by an executable parity oracle against real SQLite, so a drift fails
    against the engine rather than surviving as an argument.
    """

    return sqlite_cast_integer(value)


def next_attempt(notice: dict[str, Any]) -> tuple[int, Optional[float]]:
    """The attempt number this delivery is, and how long to wait if it fails.

    ``None`` for the delay means there is no attempt left: the notice dead-letters.

    The index is off the attempt this delivery IS, minus one — the Nth failed attempt
    arms ``BACKOFF_SECONDS[N - 1]``. Indexing on the incremented number instead skipped
    the first declared interval entirely (a freshly stamped notice waited 8 s, never
    2 s) and shortened the ladder by one rung, so the declared cap was never armed and
    ``CLAIM_LEASE_SECONDS``' "must exceed the retry cap" argument was about an interval
    the code could not reach. One interval per consumed attempt, in order, then the
    dead letter.
    """

    # The POLICY counter clamps a negative read to the start of the ladder; the CAS
    # expectation deliberately does not (``notice_write_expectation`` asserts the raw
    # stored value, so the guarded claim still matches the row as it is). CAST
    # semantics can read a negative from an out-of-band value ("-3q" reads -3,
    # INT64_MIN from a saturated number), and incrementing FROM it would grant the
    # notice more than ``MAX_ATTEMPTS`` attempts — ~9.2 quintillion for the saturated
    # case, each on the shortest backoff. Clamped, the first claim stamps attempt 1
    # over the poison and the row is a normal notice again in one pass.
    attempts = max(_attempts_read(notice.get("attempts")), 0) + 1
    if attempts >= MAX_ATTEMPTS:
        return attempts, None
    return attempts, BACKOFF_SECONDS[attempts - 1]
