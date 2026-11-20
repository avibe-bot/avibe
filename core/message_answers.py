"""One durable answer per question, reserved with its Delivery before dispatch.

Callers hold the SQLite writer reservation. Delivery state owns execution;
this table only binds client operation identities to their immutable receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select

from storage import message_deliveries as deliveries, messages_service
from storage.models import message_answer_operations as operations, message_deliveries, messages


def revision(options: list[str]) -> str:
    return hashlib.sha256(json.dumps(options, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Answer:
    request_id: str
    operation_id: str
    revision: str | None
    choice: str
    quick: bool
    legacy: bool
    fingerprint: str

    def receipt(self, outcome: str | None, delivery_id=None, **extra) -> dict:
        return {
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "outcome": outcome,
            "choice": self.choice,
            "delivery_id": delivery_id,
            **extra,
        }


def parse(payload: dict) -> Answer | None:
    metadata = payload.get("metadata") or {}
    quick = metadata.get("quick_reply_for") if isinstance(metadata, dict) else None
    free = payload.get("in_reply_to")
    if not quick and not free:
        if "operation_id" in payload or "revision" in payload:
            raise ValueError("invalid_answer")
        return None
    target = quick or free
    operation = payload.get("operation_id")
    rev = payload.get("revision")
    text = payload.get("text") or (payload.get("content") or {}).get("text")
    legacy = operation is None and rev is None and bool(quick)
    if (
        bool(quick)
        and bool(free)
        or not isinstance(target, str)
        or not target.strip()
        or len(target) > 256
        or not isinstance(text, str)
        or not text.strip()
        or (not legacy and (not isinstance(operation, str) or not operation.strip() or len(operation) > 128))
        or (not legacy and (not isinstance(rev, str) or len(rev) != 64))
        or bool((payload.get("content") or {}).get("attachments"))
    ):
        raise ValueError("invalid_answer")
    return Answer(
        target,
        operation or str(uuid.uuid4()),
        rev,
        text,
        bool(quick),
        legacy,
        json.dumps([target, rev, bool(quick), text], ensure_ascii=False),
    )


def _key(session_id: str, principal: str, answer: Answer):
    return and_(
        operations.c.session_id == session_id,
        operations.c.principal == principal,
        operations.c.operation_id == answer.operation_id,
    )


def prepare(conn, session_id: str, principal: str, answer: Answer) -> dict | None:
    """Return a refusal/replay; None reserves permission to create a Delivery now."""
    existing = conn.execute(select(operations).where(_key(session_id, principal, answer))).mappings().first()
    if existing:
        if existing["fingerprint"] != answer.fingerprint:
            return {"status": 409, "body": {"error": {"code": "operation_conflict"}}}
        receipt = observe(conn, session_id, principal, answer)
        original = (receipt or {}).get("outcome")
        return {
            "status": 200 if receipt else 202,
            "publish_answer": existing["receipt_json"] is None and original == "accepted",
            "body": {
                "answer": {
                    **(receipt or answer.receipt(None, existing["delivery_id"])),
                    "outcome": "duplicate",
                    "original_outcome": original,
                },
                **({"dispatch_error": "dispatch_pending"} if receipt is None else {}),
            },
        }

    def refuse(outcome: str, status: int, *, choice=None, delivery_id=None):
        receipt = answer.receipt(outcome, delivery_id)
        if choice is not None:
            receipt["choice"] = choice
        conn.execute(
            operations.insert().values(
                session_id=session_id,
                principal=principal,
                operation_id=answer.operation_id,
                request_id=answer.request_id,
                fingerprint=answer.fingerprint,
                choice=answer.choice,
                receipt_json=json.dumps(receipt),
            )
        )
        return {
            "status": 200 if answer.legacy and outcome == "already_handled" else status,
            "body": {"answer": receipt, **({"already_answered": True} if outcome == "already_handled" else {})},
        }

    target = messages_service.get_message(conn, answer.request_id, session_id=session_id)
    if target is None or target.get("author") != "agent":
        return refuse("not_found", 404)
    content = target.get("content") or {}
    chosen = content.get("quick_reply_chosen")
    winner = (
        conn.execute(
            select(operations.c.choice, operations.c.delivery_id)
            .join(message_deliveries, message_deliveries.c.id == operations.c.delivery_id)
            .where(operations.c.session_id == session_id, operations.c.request_id == answer.request_id)
            .where(message_deliveries.c.state != "retired")
            .limit(1)
        )
        .mappings()
        .first()
    )
    if winner or chosen is not None:
        return refuse(
            "already_handled",
            409,
            choice=winner["choice"] if winner else chosen,
            delivery_id=winner["delivery_id"] if winner else None,
        )
    options = content.get("quick_replies")
    if not isinstance(options, list) or not options:
        return refuse("stale", 409)
    if answer.revision is not None and answer.revision != revision(options):
        return refuse("stale", 409)
    if answer.quick and answer.choice not in options:
        return {"status": 400, "body": {"error": {"code": "invalid_answer"}}}
    newer = conn.execute(
        select(messages.c.id)
        .where(
            messages.c.session_id == session_id,
            messages.c.author.in_(("user", "agent")),
            messages.c.type.in_(messages_service.TRANSCRIPT_TYPES),
            or_(
                messages.c.created_at > target["created_at"],
                and_(messages.c.created_at == target["created_at"], messages.c.id > answer.request_id),
            ),
        )
        .limit(1)
    ).first()
    pending = conn.execute(
        select(message_deliveries.c.id)
        .where(
            message_deliveries.c.session_id == session_id,
            message_deliveries.c.state.not_in(("retired", "accepted")),
            func.json_extract(message_deliveries.c.snapshot_json, "$.author") == "user",
            message_deliveries.c.submitted_at >= target["created_at"],
        )
        .limit(1)
    ).first()
    if newer or pending:
        return refuse("stale", 409)
    return None


def bind(conn, session_id: str, principal: str, answer: Answer, delivery_id: str) -> None:
    conn.execute(
        operations.insert().values(
            session_id=session_id,
            principal=principal,
            operation_id=answer.operation_id,
            request_id=answer.request_id,
            fingerprint=answer.fingerprint,
            choice=answer.choice,
            delivery_id=delivery_id,
        )
    )


def acknowledge(conn, session_id: str, principal: str, answer: Answer, delivery_id: str) -> tuple[dict[str, Any], bool]:
    from storage.delivery_states import policy_for

    saved = conn.execute(select(operations.c.receipt_json).where(_key(session_id, principal, answer))).scalar_one()
    if saved is not None:
        return json.loads(saved), False
    current = deliveries.get_delivery(conn, delivery_id)
    if current is None or policy_for(current["state"]).submission != "admitted":
        return answer.receipt(None, delivery_id), False
    receipt = answer.receipt("accepted", delivery_id)
    conn.execute(
        operations.update().where(_key(session_id, principal, answer)).values(receipt_json=json.dumps(receipt))
    )
    # Ordinary text owns the same question, but remains an ordinary user input.
    target = messages_service.get_message(conn, answer.request_id, session_id=session_id)
    if target is not None:
        content = dict(target["content"])
        content["quick_reply_chosen"] = answer.choice
        conn.execute(
            messages.update().where(messages.c.id == answer.request_id).values(content_json=json.dumps(content))
        )
    return receipt, True


def failed(conn, session_id: str, principal: str, answer: Answer, delivery_id: str, error: str) -> None:
    current = deliveries.get_delivery(conn, delivery_id)
    if current is not None and current["state"] == "retired":
        receipt = answer.receipt(None, delivery_id, dispatch_error=error)
        conn.execute(
            operations.update().where(_key(session_id, principal, answer)).values(receipt_json=json.dumps(receipt))
        )


def observe(conn, session_id: str, principal: str, answer: Answer) -> dict | None:
    """Resolve an interrupted HTTP acknowledgement from durable Delivery evidence."""
    from storage.delivery_states import policy_for

    row = conn.execute(select(operations).where(_key(session_id, principal, answer))).mappings().one()
    if row["receipt_json"]:
        return json.loads(row["receipt_json"])
    current = deliveries.get_delivery(conn, row["delivery_id"])
    if current is not None and policy_for(current["state"]).submission == "admitted":
        return acknowledge(conn, session_id, principal, answer, row["delivery_id"])[0]
    if current is not None and current["state"] == "retired":
        failed(conn, session_id, principal, answer, row["delivery_id"], "dispatch_failed")
        return answer.receipt(None, row["delivery_id"], dispatch_error="dispatch_failed")
    return None
