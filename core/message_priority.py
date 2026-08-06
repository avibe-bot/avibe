from __future__ import annotations

from typing import Literal, cast


DeliveryIntent = Literal["replace", "steer", "queue"]
DeliveryPriority = Literal["p0", "p1", "p3"]

_PRIORITY_BY_INTENT: dict[DeliveryIntent, DeliveryPriority] = {
    "replace": "p0",
    "steer": "p1",
    "queue": "p3",
}
_INTENT_BY_PRIORITY: dict[DeliveryPriority, DeliveryIntent] = {
    priority: intent for intent, priority in _PRIORITY_BY_INTENT.items()
}
_INTENT_BY_TRIGGER: dict[str, DeliveryIntent] = {
    "im": "steer",
    "scheduled": "queue",
    "task_run": "queue",
    "watch": "steer",
    "hook": "steer",
    "webhook": "steer",
    "agent_run": "steer",
    "show_annotation": "steer",
    "callback": "steer",
}


def priority_for_delivery_intent(intent: str) -> DeliveryPriority:
    normalized = cast(DeliveryIntent, str(intent or "").strip())
    try:
        return _PRIORITY_BY_INTENT[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported delivery intent: {intent}") from exc


def delivery_intent_for_priority(priority: str) -> DeliveryIntent:
    normalized = cast(DeliveryPriority, str(priority or "").strip())
    try:
        return _INTENT_BY_PRIORITY[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported delivery priority: {priority}") from exc


def delivery_intent_for_trigger(trigger_kind: str) -> DeliveryIntent:
    """Map a producer class to its one public delivery policy."""

    normalized = str(trigger_kind or "").strip()
    try:
        return _INTENT_BY_TRIGGER[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported delivery trigger: {trigger_kind}") from exc
