"""Real durable failed-Turn fixtures shared by retry boundary tests."""

from storage import message_deliveries as deliveries, messages_service


def seed_failed_notice(
    conn,
    *,
    session_id,
    scope_id=None,
    backend="codex",
    not_written=False,
    content=None,
    texts=None,
):
    inputs = [
        deliveries.insert_delivery(
            conn,
            delivery_id=deliveries.new_delivery_id(),
            session_id=session_id,
            priority="p3",
            state="queued",
            snapshot=deliveries.message_snapshot(
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                text=text,
                content=content,
                message_kind="original",
            ),
            dispatch_text=text,
        )
        for text in (texts or ["检查这张图"])
    ]
    turn_id = deliveries.new_turn_id()
    deliveries.claim_start_batch(
        conn,
        turn_id=turn_id,
        session_id=session_id,
        backend=backend,
        deliveries=inputs,
        dispatch_text="\n".join(row["dispatch_text"] for row in inputs),
    )
    turn = deliveries.get_turn(conn, turn_id)
    if not not_written:
        deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=turn["version"],
            runtime_key="test-runtime",
            runtime_turn_id="test-native-turn",
            native_turn_id="test-native-turn",
        )
        deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test_acceptance"})
    deliveries.terminalize_turn(
        conn,
        turn_id,
        outcome="not_written" if not_written else "failed",
        settled_by="terminal_result",
        evidence_kind="definitive_prewrite_failure" if not_written else "terminal_result",
    )
    if not_written:
        for row in inputs:
            current = deliveries.get_delivery(conn, row["id"])
            deliveries.record_definitive_attempt(
                conn,
                row["id"],
                expected_version=current["version"],
                expected_states=("claimed",),
                outcome="not_written",
                next_state="queued",
            )
    notice = messages_service.append(
        conn,
        session_id=session_id,
        scope_id=scope_id,
        platform="avibe",
        author="agent",
        source="agent",
        message_type="notify",
        text="Backend interrupted",
        metadata={
            "event": "backend_failure",
            "backend": backend,
            "turn_id": turn_id,
            "failure_id": f"turn:{turn_id}",
        },
    )
    return notice, deliveries.get_turn(conn, turn_id), inputs


def reserve_failure_retry(conn, notice):
    from core.backend_failure_retry import reserve_retry

    return reserve_retry(
        conn,
        session_id=notice["session_id"],
        notice_id=notice["id"],
        snapshot=deliveries.message_snapshot(
            scope_id=notice["scope_id"],
            session_id=notice["session_id"],
            platform="avibe",
            author="user",
            source="user",
            text="continue",
            message_kind="quick_reply",
        ),
    )
