"""20261030_0072: ``session_digests`` backfilled from the checkpoints a database already holds.

A database a released version wrote has every Vibey Session's checkpoints and no
digest. After the upgrade, each Session that has a checkpoint holding any of the
mind, memo, and now is recallable by its latest such checkpoint; downgrade drops
the table and the next upgrade rebuilds it from the same rows.
"""

import json
import sqlite3

import pytest
from alembic import command

from storage import migrations

pytestmark = pytest.mark.no_sqlite_template

REVISION = "20261030_0072"
PREVIOUS_REVISION = "20261020_0071"
V5 = {
    "version": 1,
    "mode": "normal",
    "checkpoint": "# 6. Now\n## Current step\n- 核对清单",
    "mind": "# 1. Where I stand\n## For whom and why\n用户要按时报税。",
    "memo": [
        {"seq": 3, "text": "材料齐了吗？", "truncated": False},
        {"seq": 9, "text": "先看清单\n再交", "truncated": True},
    ],
}
# A drop that left the checkpoint out: it holds nothing to recall.
LEFT_OUT = {"version": 1, "mode": "dropped", "checkpoint": "", "mind": "", "memo": []}
# A checkpoint-v4 row has no mind or memo: its whole text is the now.
V4 = {"version": 1, "mode": "normal", "checkpoint": "# 1. Self and method\n- 只读核验"}


def _session(conn, session_id: str) -> None:
    conn.execute(
        "insert into agent_sessions (id, agent_backend, agent_variant, session_anchor, native_session_id, status,"
        " metadata_json, created_at, updated_at) values (?, 'vibey', 'vibey', ?, '', 'active', '{}', ?, ?)",
        (session_id, session_id, "2026-10-20T00:00:00Z", "2026-10-20T00:00:00Z"),
    )


def _checkpoint(conn, event_id: str, session_id: str, at: str, content) -> None:
    conn.execute(
        "insert into agent_events (id, session_id, platform, event_type, visibility, context_seq, content_json,"
        " metadata_json, created_at, updated_at) values (?, ?, 'avibe', 'context_compaction', 'context', ?, ?, '{}',"
        " ?, ?)",
        (event_id, session_id, int(event_id[-1]), content if isinstance(content, str) else json.dumps(content), at, at),
    )


def _digests(path) -> dict:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("select session_id, compaction_id, updated_at, mind, memo, now from session_digests")
        return {row[0]: row[1:] for row in rows}


def test_upgrade_gives_each_session_its_latest_checkpoint_memory(tmp_path):
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision=PREVIOUS_REVISION)
    with sqlite3.connect(path) as conn:
        for session_id in ("ses_taxes", "ses_v4", "ses_broken", "ses_plain"):
            _session(conn, session_id)
        _checkpoint(conn, "evt_1", "ses_taxes", "2026-10-21T00:00:00Z", {**V5, "checkpoint": "# 6. Now\n- 旧的"})
        _checkpoint(conn, "evt_2", "ses_taxes", "2026-10-22T00:00:00Z", V5)
        _checkpoint(conn, "evt_3", "ses_taxes", "2026-10-23T00:00:00Z", LEFT_OUT)
        _checkpoint(conn, "evt_4", "ses_v4", "2026-10-21T00:00:00Z", V4)
        _checkpoint(conn, "evt_5", "ses_broken", "2026-10-21T00:00:00Z", "{not json")

    migrations.run_migrations(path)

    expected = {
        "ses_taxes": (
            "evt_2",
            "2026-10-22T00:00:00Z",
            "# 1. Where I stand\n## For whom and why\n用户要按时报税。",
            "# 5. The user's words\n- (#3) 材料齐了吗？\n- (#9) 先看清单\n  再交\n"
            "  [cut short; the whole message is #9 in the earlier record]",
            "# 6. Now\n## Current step\n- 核对清单",
        ),
        "ses_v4": ("evt_4", "2026-10-21T00:00:00Z", None, None, "# 1. Self and method\n- 只读核验"),
    }
    with sqlite3.connect(path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone() == (REVISION,)
    assert _digests(path) == expected

    command.downgrade(migrations.alembic_config(path), PREVIOUS_REVISION)
    with sqlite3.connect(path) as conn:
        assert "session_digests" not in {row[0] for row in conn.execute("select name from sqlite_master")}
    migrations.run_migrations(path)
    assert _digests(path) == expected
