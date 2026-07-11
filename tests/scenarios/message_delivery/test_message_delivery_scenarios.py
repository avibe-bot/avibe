from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.scenario_harness.core import ScenarioExpect, ScenarioRunner, ScenarioStep
from core.message_output import MessageOutput
from tests.scenario_harness.message_delivery import MessageDeliveryHarness


class MessageDeliveryScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_output_scenario_does_not_settle_newer_turn(self):
        """Scenario: MESSAGE-DELIVERY-003"""
        harness = MessageDeliveryHarness(platform="slack")
        harness.controller.runtime_turn_current = False
        harness.context.platform_specific = {
            "agent_runtime_turn_key": "runtime-1",
            "agent_runtime_turn_token": "older-turn",
            "turn_token": "older-turn",
        }
        runner = ScenarioRunner(harness)

        with (
            patch("core.message_dispatcher.agent_message_exists", return_value=False),
            patch("core.message_dispatcher.persist_agent_message"),
        ):
            await runner.run(
                ScenarioStep(
                    "deliver_background_result",
                    lambda h: h.emit_output(
                        "background result",
                        MessageOutput(
                            completes_turn=False,
                            detached=True,
                            idempotency_key="task-1:completion",
                            activity_id="task-1",
                        ),
                    ),
                )
            )

        ScenarioExpect.text_contains(harness, "background result")
        self.assertEqual(harness.controller.turn_terminal_calls, 0)
        self.assertEqual(harness.controller.stream_completion_calls, 0)
        self.assertEqual(harness.controller.runtime_release_calls, 0)

    async def test_one_turn_multiple_outputs_scenario_completes_once(self):
        """Scenario: MESSAGE-DELIVERY-004"""
        harness = MessageDeliveryHarness(platform="slack")
        runner = ScenarioRunner(harness)

        with (
            patch("core.message_dispatcher.agent_message_exists", return_value=False),
            patch("core.message_dispatcher.persist_agent_message"),
        ):
            await runner.run(
                ScenarioStep(
                    "emit_intermediate_output",
                    lambda h: h.emit_output(
                        "first output",
                        MessageOutput(
                            completes_turn=False,
                            idempotency_key="output-1",
                            sequence=1,
                        ),
                    ),
                ),
                ScenarioStep(
                    "emit_terminal_output",
                    lambda h: h.emit_output(
                        "final output",
                        MessageOutput(
                            completes_turn=True,
                            idempotency_key="output-2",
                            sequence=2,
                        ),
                    ),
                ),
            )

        self.assertEqual(harness.rendered_texts(), ["first output", "final output"])
        self.assertEqual(harness.controller.turn_terminal_calls, 1)
        self.assertEqual(harness.controller.stream_completion_calls, 0)
        self.assertEqual(harness.controller.runtime_release_calls, 1)

    async def test_scheduled_result_delivery_scenario_finalizes_anchor(self):
        """Scenario: MESSAGE-DELIVERY-001"""
        harness = MessageDeliveryHarness(platform="slack")
        harness.context.platform_specific = {
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_scheduled-1",
            "scheduled_anchor_required": True,
        }
        runner = ScenarioRunner(harness)

        await runner.run(
            ScenarioStep("emit_result", lambda h: h.emit_result("hello")),
        )

        ScenarioExpect.step_history(runner, ["emit_result"])
        ScenarioExpect.text_contains(harness, "hello")
        self.assertEqual(harness.finalized_calls, [("C123", None, "msg-1")])

    async def test_scheduled_result_delivery_override_scenario_uses_parent_channel_target(self):
        """Scenario: MESSAGE-DELIVERY-002"""
        harness = MessageDeliveryHarness(platform="slack", thread_id="171717.123")
        harness.context.platform_specific = {
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_171717.123",
            "delivery_override": {
                "user_id": "scheduled",
                "channel_id": "C123",
                "thread_id": None,
                "platform": "slack",
                "is_dm": False,
            },
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "slack::C123",
                "clear_source": False,
            },
        }
        runner = ScenarioRunner(harness)

        await runner.run(
            ScenarioStep("emit_result", lambda h: h.emit_result("hello")),
        )

        ScenarioExpect.step_history(runner, ["emit_result"])
        ScenarioExpect.text_contains(harness, "hello")
        self.assertEqual(harness.finalized_calls, [("C123", "171717.123", "msg-1")])


if __name__ == "__main__":
    unittest.main()
