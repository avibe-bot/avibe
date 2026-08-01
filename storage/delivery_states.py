from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


OrderingRole = Literal["claimable", "fence", "turn_owned", "terminal"]
NativeEffect = Literal["none", "possible", "unknown", "accepted"]
RunCancelAction = Literal["retire", "turn_owner", "complete"]
SubmissionStatus = Literal["reserved", "admitted", "retired"]


@dataclass(frozen=True)
class DeliveryStatePolicy:
    ordering: OrderingRole
    native_effect: NativeEffect
    run_cancel: RunCancelAction
    submission: SubmissionStatus


# This is the sole runtime meaning of a Delivery state. Callers express an
# operation (claim or cancel) and consume a derived property instead of
# maintaining their own state lists.
DELIVERY_STATE_MATRIX: Mapping[str, DeliveryStatePolicy] = MappingProxyType(
    {
        "reserved": DeliveryStatePolicy("fence", "none", "retire", "reserved"),
        "queued": DeliveryStatePolicy("claimable", "none", "retire", "admitted"),
        "claimed": DeliveryStatePolicy(
            "turn_owned", "possible", "turn_owner", "admitted"
        ),
        "pending_steer": DeliveryStatePolicy(
            "fence", "none", "retire", "admitted"
        ),
        "steering": DeliveryStatePolicy(
            "fence", "possible", "turn_owner", "admitted"
        ),
        "interrupt_waiting": DeliveryStatePolicy(
            "turn_owned", "possible", "turn_owner", "admitted"
        ),
        "reconciling_steer": DeliveryStatePolicy(
            "fence", "unknown", "turn_owner", "admitted"
        ),
        "accepted": DeliveryStatePolicy(
            "terminal", "accepted", "complete", "admitted"
        ),
        "retired": DeliveryStatePolicy(
            "terminal", "none", "complete", "retired"
        ),
    }
)

DELIVERY_STATES = tuple(DELIVERY_STATE_MATRIX)
CLAIMABLE_QUEUE_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.ordering == "claimable"
)
FENCE_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.ordering == "fence"
)
RUN_CANCEL_RETIRE_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.run_cancel == "retire"
)
ADMITTED_DELIVERY_STATES = tuple(
    state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.submission == "admitted"
)


def policy_for(state: str) -> DeliveryStatePolicy:
    try:
        return DELIVERY_STATE_MATRIX[state]
    except KeyError as exc:
        raise ValueError(f"unknown Delivery state: {state}") from exc
