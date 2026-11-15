"""Bot CLI → scheduler → shared delivery owner; only native execution is stubbed."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from core import internal_server
from core.bots import BotService
from core.handlers.delegation_safety_net import DelegationSafetyNet
from core.handlers.message_handler import MessageHandler
from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore
from core.services.agent_steering import SteerOutcome, result as steer_result
from storage import message_deliveries
from storage.models import session_delegations
import vibe.cli as cli
from tests.test_bot_cli import home  # noqa: F401
from tests.test_bot_user_delivery import _start_active_turn
from tests.test_delegation_safety_net import _finish, _notices
from tests.test_internal_server import _build_controller_double

pytestmark = pytest.mark.usefixtures("in_process_cli_control")


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [["--bot", "receiver"], ["--session-id", "ses_receiver"]])
@pytest.mark.parametrize(
    "busy, flags, outcome, expected",
    [
        (True, [], SteerOutcome.ACCEPTED, "accepted"),
        (False, [], SteerOutcome.ACCEPTED, "claimed"),
        (True, ["--queue"], SteerOutcome.ACCEPTED, "queued"),
        (True, ["--no-reply"], SteerOutcome.ACCEPTED, "accepted"),
        (True, ["--queue", "--no-reply"], SteerOutcome.ACCEPTED, "queued"),
        (True, [], SteerOutcome.REFUSED, "queued"),
        (True, [], SteerOutcome.UNKNOWN, "reconciling_steer"),
    ],
)
async def test_bot_dispatch_uses_shared_admission(home, capsys, monkeypatch, busy, flags, outcome, expected, target):
    with BotService() as bots:
        bots.ensure_main_bot()
        bots.create_bot(display_name="Receiver", handle="receiver")
        bots.create_bot(display_name="Sender", handle="sender")
    controller = _build_controller_double()

    async def prepare_input_metadata(context, *, human):
        return await MessageHandler.prepare_input_metadata(object(), context, human=human)

    controller.message_handler.prepare_input_metadata = prepare_input_metadata
    internal_server.create_app(controller)
    controller.platform_settings_managers = {}
    controller.im_clients = {"avibe": SimpleNamespace()}
    controller.get_im_client_for_context = lambda _: SimpleNamespace(
        should_use_thread_for_reply=lambda: True, should_use_thread_for_dm_session=lambda: False,
    )
    controller.config.language = "en"
    manager = controller.session_turns
    manager._steer = AsyncMock(return_value=steer_result(outcome, reason="test"))
    manager._start_persisted_turn = AsyncMock(return_value=True)
    store = TaskExecutionStore()
    turn_id = None
    run_ids = []
    accepted_pairs = []
    # Different senders must join one running Turn, not one queued Turn per sender.
    for sender in ("main", "sender"):
        monkeypatch.setenv("AVIBE_SESSION_ID", f"ses_{sender}")
        args = cli.build_parser().parse_args(["agent", "run", *target, *flags, "--message", f"From {sender}"])
        with patch("vibe.cli._task_request_store", return_value=store):
            code = cli.cmd_agent_run(args)
        captured = capsys.readouterr()
        payload = json.loads(captured.out or captured.err)
        assert code == 0, payload
        run_id = payload["run"]["id"]
        run_ids.append(run_id)
        run = store.get_run(run_id)
        engine = store.sqlite_backend.engine
        if busy and turn_id is None:
            turn_id = _start_active_turn(engine, "ses_receiver", backend="vibey")
        service = ScheduledTaskService(controller, store=ScheduledTaskStore(home / "tasks.json"), request_store=store)
        metadata = {**run["metadata"], "source_kind": run["source_kind"], "source_actor": run["source_actor"],
                    "callback_session_id": run["callback_session_id"]}
        result = await service._execute_agent_run(
            session_key=None, post_to=None, deliver_key=None, message=run["prompt"], execution_id=run_id,
            session_id=run["session_id"], metadata=metadata,
        )
        assert result.error is None
        saved = store.get_run(run_id)
        with engine.connect() as conn:
            delivery = message_deliveries.get_delivery(conn, saved["delivery_id"])
            assert delivery["state"] == expected
            pairs = list(conn.execute(select(session_delegations).where(
                session_delegations.c.sender_session_id == f"ses_{sender}",
                session_delegations.c.recipient_session_id == "ses_receiver",
            )).mappings())
            assert bool(pairs) == (expected == "accepted" and "--no-reply" not in flags)
            if expected == "accepted":
                assert delivery["turn_id"] == turn_id
                assert pairs == [] or pairs[0]["run_id"] == run_id
                accepted_pairs.extend(pairs)
                request = manager._steer.await_args.args[1]
                rendered = request.input_metadata.render(request.text, SimpleNamespace(include_time_info=False))
                assert f"[Harness: agent run {run_id} from @<bot:{sender}>]" in rendered
                assert ("[No reply needed.]" in rendered) == ("--no-reply" in flags)
                if "--no-reply" not in flags:
                    assert f"report to @<bot:{sender}>" in rendered
        assert run["metadata"]["callback_mode"] == "failure"
        if not busy:
            manager._start_persisted_turn.assert_awaited_once()
            manager._steer.assert_not_awaited()
            break
    if busy:
        manager._start_persisted_turn.assert_not_awaited()
        assert manager._steer.await_count == (0 if "--queue" in flags else 2)
        if expected == "accepted":
            with engine.connect() as conn:
                assert message_deliveries.list_queued(conn, "ses_receiver") == []
            for call in manager._steer.await_args_list:
                assert call.args[1].expected_logical_turn_id == turn_id
            harness = SimpleNamespace(engine=engine)
            safety_net = DelegationSafetyNet(service)
            assert all(not safety_net._notify(pair) for pair in accepted_pairs)
            _finish(harness, turn_id, [SimpleNamespace(id=run_id) for run_id in run_ids])
            assert all(safety_net._notify(pair) for pair in accepted_pairs)
            assert len(_notices(harness)) == (0 if "--no-reply" in flags else 2)
    store.sqlite_backend.close()
