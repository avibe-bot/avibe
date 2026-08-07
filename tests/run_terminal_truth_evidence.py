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


def _gap(lane: str, trigger: str, outcome: str) -> tuple[str, str]:
    return (
        "unproven",
        f"Probe: admit a real Run through {trigger}, drive the {lane} lane to "
        f"{outcome}, and assert the exact Run row and definition projection.",
    )


RUN_TERMINAL_TRUTH_MATRIX: Final = {
    (lane, trigger): {
        outcome: _gap(lane, trigger, outcome) for outcome in OUTCOMES
    }
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

PR7R_QUESTIONS: Final = {
    "Q1": {
        "verdict": "open",
        "answer": "The durable lane has partial nonterminal evidence; the direct-IM admission boundary does not.",
    },
    "Q2": {
        "verdict": "blocked",
        "answer": "No backend/lane pair has an end-to-end exact Turn-and-Run progress probe, so a generic inactivity timeout remains blocked.",
    },
    "Q3": {
        "verdict": "open",
        "answer": "Mixed-source Run coalescing and cancellation policy have not been driven together through real admission.",
    },
    "Q4": {
        "verdict": "open",
        "answer": "Some Run-scoped pre-terminal facts exist, but no cross-backend Turn-scoped proof is complete.",
    },
    "Q5": {
        "verdict": "open",
        "answer": "Health is derived, while last-run fields are stored separately; crash-window reconciliation remains unproven.",
    },
}
