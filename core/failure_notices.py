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

from core.run_settlement import RUN_INTERRUPTION_REASONS

#: What the drain decided to do with one owed notice this tick.
ACTION_DELIVER = "deliver"
#: Not delivered, no attempt consumed, no backoff burned — reconsidered next tick.
#: Used both while an earlier run of the definition has not settled and while this
#: streak's canonical notice is still trying.
ACTION_DEFER = "defer"
#: Terminal: the streak's notice was already delivered, so this row is a duplicate.
ACTION_SKIP = "skip"

#: Exponential backoff from the 2s tick, capped. Attempt N waits
#: ``BACKOFF_SECONDS[N]``; running off the end is the dead letter.
BACKOFF_SECONDS: tuple[float, ...] = (2.0, 8.0, 32.0, 128.0, 512.0)
#: How many attempts a notice gets before it dead-letters to ``failed``. A visible
#: dead letter, rather than a silent drop or an infinite loop.
MAX_ATTEMPTS = len(BACKOFF_SECONDS)

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
    beyond taste: ``failure_streak`` accepts a ``succeeded`` anchor, so reading the
    streak for a binding notice on a successful rebind would sweep in the definition's
    surrounding FAILURES and defer or skip the notice behind a canonical row that has
    nothing to do with it — the notice would be stamped, durable, and never sent.
    """

    return is_interruption(notice) or is_binding_change(notice)


def decide(
    *,
    run_id: str,
    definition_id: Optional[str],
    notice: Optional[dict[str, Any]],
    streak: list[dict[str, Any]],
    earlier_unsettled: Optional[dict[str, Any]],
) -> NoticeDecision:
    """What to do with one pending owed notice.

    ``streak`` is this run's consecutive-failure streak oldest first, each entry
    carrying its own ``notice`` (or ``None``); ``earlier_unsettled`` is an
    earlier-created execution of the same definition that has not settled.
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

    others = [row for row in streak if row["id"] != run_id]

    # Evidence of delivery ANYWHERE in the streak settles it: this row is a
    # duplicate. Note this is checked before the canonical computation, because a
    # sent notice is not necessarily the earliest row.
    if any((row.get("notice") or {}).get("state") == "sent" for row in others):
        return NoticeDecision(ACTION_SKIP, "streak_already_notified")

    # The canonical notice is the earliest still-trying row. ``failed`` (dead
    # lettered) and ``skipped`` rows drop out, which is exactly how promotion
    # works: a streak whose canonical exhausted its retries still owes the user the
    # news, so the claim on delivery outlives any single row.
    canonical = next(
        (
            row
            for row in streak
            if (row.get("notice") or {}).get("state") == "pending"
        ),
        None,
    )
    if canonical is None or canonical["id"] == run_id:
        return NoticeDecision(ACTION_DELIVER, "canonical")

    # The canonical notice is still pending — by design, because it keeps retrying.
    # Absence of an acknowledgement is NOT evidence that nothing is in flight, so
    # this row waits rather than notifying. At most one notice per streak is ever in
    # flight, which is the property "once, not daily" was asking for.
    return NoticeDecision(ACTION_DEFER, f"canonical_pending:{canonical['id']}")


def next_attempt(notice: dict[str, Any]) -> tuple[int, Optional[float]]:
    """The attempt number this delivery is, and how long to wait if it fails.

    ``None`` for the delay means there is no attempt left: the notice dead-letters.
    """

    attempts = int(notice.get("attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        return attempts, None
    return attempts, BACKOFF_SECONDS[attempts]
