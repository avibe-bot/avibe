"""Step B: concise status-bubble settings plumbing.

V2Config carries agent_progress_style + heartbeat/no-output thresholds (parsed,
validated, round-tripped); Controller getters expose them to the dispatcher.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import V2Config
from core.controller import Controller
from tests.test_api_save_config_merge import _full_config_payload


def _payload(**extra):
    base = _full_config_payload()
    base.update(extra)
    return base


class ConfigParsingTests(unittest.TestCase):
    def test_defaults(self):
        cfg = V2Config.from_payload(_payload())
        self.assertEqual(cfg.agent_progress_style, "off")
        self.assertEqual(cfg.agent_status_heartbeat_ms, 8000)
        self.assertEqual(cfg.agent_status_no_output_ms, 180000)

    def test_valid_overrides(self):
        cfg = V2Config.from_payload(
            _payload(
                agent_progress_style="off",
                agent_status_heartbeat_ms=12000,
                agent_status_no_output_ms=60000,
            )
        )
        self.assertEqual(cfg.agent_progress_style, "off")
        self.assertEqual(cfg.agent_status_heartbeat_ms, 12000)
        self.assertEqual(cfg.agent_status_no_output_ms, 60000)

    def test_invalid_values_fall_back_to_defaults(self):
        cfg = V2Config.from_payload(
            _payload(
                agent_progress_style="bogus",
                agent_status_heartbeat_ms=-5,
                agent_status_no_output_ms="nope",
            )
        )
        self.assertEqual(cfg.agent_progress_style, "off")
        self.assertEqual(cfg.agent_status_heartbeat_ms, 8000)
        self.assertEqual(cfg.agent_status_no_output_ms, 180000)

    def test_bool_is_not_accepted_as_int(self):
        cfg = V2Config.from_payload(_payload(agent_status_heartbeat_ms=True))
        self.assertEqual(cfg.agent_status_heartbeat_ms, 8000)

    def test_out_of_range_int_falls_back_to_default(self):
        # Above the sane cap (heartbeat 1h / no-output 24h) → default, so a
        # fat-fingered value can't silence the heartbeat.
        cfg = V2Config.from_payload(
            _payload(agent_status_heartbeat_ms=10_000_000, agent_status_no_output_ms=10**12)
        )
        self.assertEqual(cfg.agent_status_heartbeat_ms, 8000)
        self.assertEqual(cfg.agent_status_no_output_ms, 180000)

    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            cfg = V2Config.from_payload(
                _payload(agent_progress_style="verbose", agent_status_heartbeat_ms=15000)
            )
            cfg.save(config_path=path)
            loaded = V2Config.load(config_path=path)
            self.assertEqual(loaded.agent_progress_style, "verbose")
            self.assertEqual(loaded.agent_status_heartbeat_ms, 15000)
            self.assertEqual(loaded.agent_status_no_output_ms, 180000)


class ControllerGetterTests(unittest.TestCase):
    def _fake(self, **overrides):
        cfg = V2Config.from_payload(_payload())
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return types.SimpleNamespace(config=cfg)

    def test_progress_style_getter(self):
        fake = self._fake(agent_progress_style="off")
        self.assertEqual(Controller.get_progress_style_for_context(fake, None), "off")

    def test_progress_style_getter_guards_bad_value(self):
        fake = self._fake(agent_progress_style="bogus")
        self.assertEqual(Controller.get_progress_style_for_context(fake, None), "off")

    def test_interval_getters(self):
        fake = self._fake(agent_status_heartbeat_ms=9000, agent_status_no_output_ms=45000)
        self.assertEqual(Controller.get_heartbeat_interval_ms_for_context(fake, None), 9000)
        self.assertEqual(Controller.get_no_output_hint_after_ms_for_context(fake, None), 45000)

    def test_interval_getters_guard_bad_value(self):
        fake = self._fake(agent_status_heartbeat_ms=0, agent_status_no_output_ms=-1)
        self.assertEqual(Controller.get_heartbeat_interval_ms_for_context(fake, None), 8000)
        self.assertEqual(Controller.get_no_output_hint_after_ms_for_context(fake, None), 180000)

    def test_interval_getters_reject_bool(self):
        # bool is an int subclass; the getter must not treat True as 1ms.
        fake = self._fake(agent_status_heartbeat_ms=True, agent_status_no_output_ms=False)
        self.assertEqual(Controller.get_heartbeat_interval_ms_for_context(fake, None), 8000)
        self.assertEqual(Controller.get_no_output_hint_after_ms_for_context(fake, None), 180000)


if __name__ == "__main__":
    unittest.main()
