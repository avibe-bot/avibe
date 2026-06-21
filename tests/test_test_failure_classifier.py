"""Regression tests for the Settings → Backends "Test connection" classifier.

Pins the mapping from raw CLI stderr/stdout to the structured error
codes the UI dispatches into ``settings.backends.testFailure*`` i18n
sentences. The classifier is conservative — anything it doesn't
recognise stays ``cli_failed`` so the user still sees the raw detail.
"""

from __future__ import annotations

import pytest

from core.agent_auth_service import _classify_test_failure


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Error: 401 Unauthorized", "invalid_credentials"),
        ("invalid api key", "invalid_credentials"),
        ("Authentication failed: no credentials configured", "invalid_credentials"),
        ("not logged in", "not_logged_in"),
        ("403 Forbidden", "forbidden"),
        ("Access denied for organization", "forbidden"),
        ("Model not found: gpt-9.0", "model_not_found"),
        ("unknown model 'sonnet-99'", "model_not_found"),
        ("429 Too Many Requests", "rate_limited"),
        ("rate limit exceeded for tier", "rate_limited"),
        ("quota exceeded", "rate_limited"),
        ("Connection refused", "endpoint_unreachable"),
        ("could not resolve host", "endpoint_unreachable"),
        ("getaddrinfo failed", "endpoint_unreachable"),
        ("ECONNREFUSED 127.0.0.1:8080", "endpoint_unreachable"),
        ("Request timed out after 30s", "endpoint_unreachable"),
        ("ssl certificate problem", "endpoint_unreachable"),
        ("502 Bad Gateway", "server_error"),
        ("503 Service Unavailable", "server_error"),
        ("Internal Server Error", "server_error"),
        ("Not inside a trusted directory and --skip-git-repo-check was not specified.", "trust_check_failed"),
    ],
)
def test_classifier_maps_common_stderr_patterns(stderr: str, expected: str) -> None:
    assert _classify_test_failure("", stderr) == expected


def test_classifier_combines_stdout_and_stderr() -> None:
    # Some CLIs emit auth errors on stdout (Claude --print piped output);
    # the classifier must search both streams.
    assert _classify_test_failure("Error: invalid api key", "") == "invalid_credentials"


def test_classifier_is_case_insensitive() -> None:
    assert _classify_test_failure("", "401 UNAUTHORIZED") == "invalid_credentials"
    assert _classify_test_failure("", "Connection Refused") == "endpoint_unreachable"


def test_classifier_falls_back_to_cli_failed_for_unknown_output() -> None:
    assert _classify_test_failure("", "Unexpected token in response") == "cli_failed"
    assert _classify_test_failure("", "") == "cli_failed"


def test_classifier_more_specific_wins_over_generic() -> None:
    """A response that contains both 401 and other words should be
    classified as ``invalid_credentials`` (auth wins over generic 5xx
    mention)."""
    msg = "Backend returned 401 (from upstream which sometimes 502s)"
    assert _classify_test_failure("", msg) == "invalid_credentials"
