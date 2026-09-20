#!/usr/bin/env python3
"""Real Avibe Vault CLI with a test-owned file custody store (never Keychain).

Only custody binary selection/store are changed. CLI parsing, named selection,
sealed envelope delivery, new-session spawn, stdin and child waiting are real.
"""
import os
from pathlib import Path
import sys

root = Path(os.environ["AVIBE_FEEDBACK_FIXTURE_ROOT"]).resolve()
for name in ("HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    assert root in Path(os.environ[name]).resolve().parents
sys.path.insert(0, os.environ["AVIBE_FEEDBACK_SOURCE_ROOT"])
from vibe import api, cli  # noqa: E402

api._avault_store_args = lambda: ["--store", "file"]
api._require_avault_path = lambda: os.environ["AVIBE_FEEDBACK_TEST_AVAULT"]
cli.main()
