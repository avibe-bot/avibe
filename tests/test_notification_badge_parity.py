"""The server's Inbox badge and the Web page's live badge follow one rule.

The page splits its live unread map by the Owner's Bot list and sums the loud
categories itself (``ui/src/lib/notificationPreferences.ts``); the push worker
and the desktop host read the server's ``badge_total``. Both sides run the same
fixture, so the sidebar number and the app icon cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.notification_categories import split_unread_counts
from core.notification_preferences import badge_total

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "ui/src/lib/notificationBadge.fixture.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_server_badge_matches_the_shared_fixture(case):
    counts = split_unread_counts(FIXTURE["unread_by_session"], bot_session_ids=case["bot_session_ids"])

    assert counts == case["unread_by_category"]
    assert badge_total(counts, case["preferences"]) == case["badge_total"]
