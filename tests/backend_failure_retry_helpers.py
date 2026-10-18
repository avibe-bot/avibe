"""Real durable failed-Turn fixtures shared by retry boundary tests."""

import asyncio

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


def previous_process(step):
    """What the previous process committed for the Turn before the restart, then startup T2.

    ``tool``: a ``write`` call cut mid-call (T2 gives it the interrupted result);
    ``handover``: a bash command still running (T2 hands it to a Watch);
    ``partial``: the text a failed stream carried, committed as a non-final response;
    ``running``: a bash command left running, its call still open (startup T2 has not run);
    ``final``: the Turn's final reply;
    ``silent_final``: delivered narration, then a final with nothing to show.
    """
    from core.agent_core.agent.recovery import settle_open_calls
    from core.agent_core.messages import ToolCallBlock, UserMessage, text
    from core.agent_core.tools.base import CallInstance, JobStatus, ToolResult
    from storage.agent_transcript import SQLiteTranscriptStore
    from tests.agent_core.fakes import FakeJobHost, assistant

    def write(engine, session_id, turn_id):
        with engine.connect() as conn:
            input_id = deliveries.delivery_for_turn(conn, turn_id)["message_id"]
        store, jobs = SQLiteTranscriptStore(engine), FakeJobHost()
        jobs.states["job_1"] = JobStatus("running")
        call = (
            ToolCallBlock(id="call_bash", name="bash", arguments={"command": "sleep 60; echo done >> s6.txt"})
            if step in {"handover", "running"}
            else ToolCallBlock(id="call_write", name="write", arguments={"path": "notes.md", "content": "x"})
        )

        async def handed_over(_call, _job_id, _status, watch_id):
            return ToolResult((text(f"Command is still running and is now Watch {watch_id}."),))

        async def previous_process_then_t2():
            await store.consume_input(session_id, input_id, UserMessage((text("run it"),)))
            if step == "final":
                await store.append_response(session_id, assistant("全部完成"), final=True)
                return
            if step == "silent_final":
                # The narration the dispatcher delivered and persisted, then the silent final.
                with engine.begin() as conn:
                    messages_service.append(
                        conn, scope_id=None, session_id=session_id, platform="avibe", author="agent",
                        source="agent", message_type="interim",
                        text="日志读完了。\n缓存被两个写者共享。\n锁已加上。\n已验证。",
                    )
                # A final whose reply is all ``<silent>``: it replies, and shows nothing.
                await store.append_response(session_id, assistant(""), final=True, replied=True)
                return
            if step == "partial":
                await store.append_response(session_id, assistant("先读完日志", stop_reason="error"), final=False)
                return
            response = await store.append_response(session_id, assistant("", calls=(call,)), final=False)
            if step == "running":
                return
            instance = CallInstance(session_id, response.row_id, response.context_seq, call.id)
            await settle_open_calls(
                session_id=session_id, store=store, jobs=jobs,
                job_ids={instance: "job_1"} if step == "handover" else {}, render_result=handed_over,
            )

        asyncio.run(previous_process_then_t2())
        if step == "handover":
            assert jobs.watches == {"job_1": "watch_job_1"}

    return write
