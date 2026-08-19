"""The OAuth callback keeps its failure cause attributable.

The callback is reachable without auth, so the error page deliberately says
almost nothing. That left the service log as the only place a real cause could
survive — and it did not: an internal fault reached both surfaces as a bare
exception class name, so `database is locked`, `disk I/O error`, and
`attempt to write a readonly database` were indistinguishable from each other
and from a bug.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from vibe import remote_access, ui_server


@pytest.fixture(autouse=True)
def _clear_diag_rate_limit():
    ui_server._oauth_diag_log_state.clear()
    yield
    ui_server._oauth_diag_log_state.clear()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_unexpected_failure_logs_the_real_cause(caplog: pytest.LogCaptureFixture) -> None:
    exc = sqlite3.OperationalError("database is locked")

    with caplog.at_level(logging.WARNING, logger=ui_server.logger.name):
        ui_server._log_oauth_callback_failure("session_cookie", exc)

    (record,) = _warnings(caplog)
    assert record.exc_info is not None
    assert "database is locked" in caplog.text
    assert "session_cookie" in record.getMessage()


def test_expected_failure_logs_no_traceback(caplog: pytest.LogCaptureFixture) -> None:
    # An unauthenticated caller can produce this at will, and it already carries
    # its own reason and detail. A traceback per bad code is noise, not signal.
    exc = remote_access.OAuthCodeExchangeError("invalid_grant", "code already redeemed")

    with caplog.at_level(logging.WARNING, logger=ui_server.logger.name):
        ui_server._log_oauth_callback_failure("code_exchange", exc)

    (record,) = _warnings(caplog)
    assert record.exc_info is None
    assert "invalid_grant" in record.getMessage()


def test_bad_codes_cannot_suppress_an_internal_fault(caplog: pytest.LogCaptureFixture) -> None:
    # Both branches once shared one rate-limit budget, so a flood of rejected
    # codes could swallow the one line naming a real fault. Each stage and kind
    # of failure gets its own budget instead.
    with caplog.at_level(logging.WARNING, logger=ui_server.logger.name):
        for _ in range(5):
            ui_server._log_oauth_callback_failure(
                "code_exchange", remote_access.OAuthCodeExchangeError("invalid_grant")
            )
        ui_server._log_oauth_callback_failure("code_exchange", sqlite3.OperationalError("disk I/O error"))
        ui_server._log_oauth_callback_failure("session_cookie", sqlite3.OperationalError("database is locked"))

    emitted = [record.getMessage() for record in _warnings(caplog)]
    # One rejected-code line (the rest are rate-limited away), plus both faults.
    assert len(emitted) == 3
    assert "disk I/O error" in caplog.text
    assert "database is locked" in caplog.text


def test_repeat_of_the_same_failure_is_still_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    # The callback is unauthenticated and the service log is unrotated, so the
    # per-key budget has to survive the split above.
    with caplog.at_level(logging.WARNING, logger=ui_server.logger.name):
        for _ in range(4):
            ui_server._log_oauth_callback_failure("session_cookie", sqlite3.OperationalError("database is locked"))

    assert len(_warnings(caplog)) == 1


def test_error_page_diagnostics_never_carry_the_traceback() -> None:
    # The log gets the cause; the unauthenticated page still gets only the class
    # name. This is the boundary the fix must not move.
    exc = sqlite3.OperationalError("attempt to write a readonly database")

    error, diagnostics = ui_server._oauth_exchange_error_diagnostics(exc)
    rendered = ui_server._oauth_error_diagnostics_text({"error": error} | diagnostics)

    assert error == "oauth_exchange_failed"
    assert diagnostics == {"reason": "OperationalError"}
    assert "readonly" not in rendered
    assert "Traceback" not in rendered
