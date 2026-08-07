"""PR7R's current-master evidence baseline.

The matrix is deliberately conservative: a cell is proven only by an end-to-end
test that enters through that trigger, crosses that lane's real backend path,
and inspects the exact Run's terminal state. No such complete probe exists yet,
so every cell stays explicit and unproven instead of borrowing nearby coverage.
"""

from typing import Final

BACKENDS: Final = ("claude", "codex", "opencode")
LANES: Final = ("direct_im", "durable_workbench")
TRIGGERS: Final = ("scheduler_cron", "scheduler_at", "manual_cli", "watch")
OUTCOMES: Final = (
    "success",
    "failure",
    "resultless_termination",
    "user_stop",
    "terminal_persistence_failure",
    "pending_output_delivery",
    "post_delivery_local_settlement_failure",
)


def _gap(backend: str, lane: str, trigger: str, outcome: str) -> tuple[str, str]:
    return (
        "unproven",
        f"Probe: admit a real {backend} Run through {trigger}, drive the "
        f"{lane} lane to {outcome}, and assert the exact Run row and "
        "definition projection.",
    )


RUN_TERMINAL_TRUTH_MATRIX: Final = {
    (backend, lane, trigger): {
        outcome: _gap(backend, lane, trigger, outcome) for outcome in OUTCOMES
    }
    for backend in BACKENDS
    for lane in LANES
    for trigger in TRIGGERS
}

# 3 backends × 2 lanes × 4 triggers × 7 outcomes.
UNPROVEN_BUDGET: Final = 168

EXACT_TURN_PROGRESS_SIGNALS: Final = {
    (backend, lane): (
        "unproven",
        f"Probe: drive a bound {lane} Run through {backend}'s production "
        "handoff and dispatcher, then assert progress carries the exact Turn "
        "and participating Run ids.",
    )
    for backend in BACKENDS
    for lane in LANES
}

QUESTION_PROBES: Final = {
    "Q1": tuple(
        RUN_TERMINAL_TRUTH_MATRIX[(backend, lane, trigger)][outcome]
        for backend in BACKENDS
        for lane in LANES
        for trigger in TRIGGERS
        for outcome in OUTCOMES
    ),
    "Q3": tuple(
        (
            "unproven",
            f"Probe: record {backend}/{lane} mixed-source Turn membership and "
            "Turn-level cancellation cardinality.",
        )
        for backend in BACKENDS
        for lane in LANES
    ),
    "Q4": tuple(
        (
            "unproven",
            f"Probe: establish {backend}/{lane} pre-terminal evidence precedence.",
        )
        for backend in BACKENDS
        for lane in LANES
    ),
    "Q5": (("unproven", "bounded-history crash-window reconciliation"),),
}


def _question(
    open_verdict: str, probes: tuple[tuple[str, str], ...]
) -> dict[str, str | tuple[str, ...]]:
    unsupported = {status for status, _ in probes} - {"unproven", "covered"}
    if unsupported:
        raise ValueError(f"Unsupported proof status: {sorted(unsupported)}")
    open_probes = tuple(label for status, label in probes if status == "unproven")
    resolved_evidence = tuple(label for status, label in probes if status == "covered")
    return {
        "verdict": open_verdict if open_probes else "answered",
        "answer": open_probes or resolved_evidence,
        "open_blockers": open_probes,
        "resolved_evidence": resolved_evidence,
    }


PR7R_QUESTIONS: Final = {
    question: _question(
        "blocked" if question == "Q2" else "open",
        tuple(EXACT_TURN_PROGRESS_SIGNALS.values())
        if question == "Q2"
        else QUESTION_PROBES[question],
    )
    for question in ("Q1", "Q2", "Q3", "Q4", "Q5")
}
