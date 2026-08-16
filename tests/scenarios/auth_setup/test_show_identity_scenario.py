from __future__ import annotations

from tests.scenario_harness.show_identity import ShowIdentityScenarioHarness


def test_form_post_closes_custom_host_identity_and_membership_loop() -> None:
    """Scenario: AUTH-SETUP-404; SHOW-LIVE-003; SHOW-LIVE-037."""
    harness = ShowIdentityScenarioHarness()
    start = harness.begin()

    assert start.callback_url == "https://show.example.test/auth/show-identity/callback"
    assert "Path=/auth/show-identity/callback" in start.set_cookie
    assert "Secure" in start.set_cookie
    assert "HttpOnly" in start.set_cookie
    assert "SameSite=None" in start.set_cookie
    assert "Domain=" not in start.set_cookie
    assert "Max-Age=300" in start.set_cookie
    assert start.state not in start.callback_url
    assert harness.start_contract["correlation_cookie_expires_at"] <= harness.start_contract["signed_state_expires_at"]

    response = harness.form_post(start, harness.issue_assertion())
    assert response.status_code == 303
    assert response.headers["Location"] == "/p/identity-contract-share/"
    assert response.headers["Cache-Control"] == "no-store"
    assert harness.http_callback_count == 1
    assert harness.issued_credential == {
        "instance_id": "ins_identity_contract",
        "page_id": "ses_identity_contract",
        "share_id": "identity-contract-share",
        "audience_revision": 12,
    }
    assert "Max-Age=0" in response.headers["Set-Cookie"]

    for negative in harness.scenario["negative_callbacks"]:
        negative_harness = ShowIdentityScenarioHarness()
        assert negative_harness.exercise_negative(negative["mutation"]) == negative["outcome"], negative["id"]
        assert negative_harness.successful_callback_count == (
            1 if negative["mutation"] == "reuse_consumed_nonce" else 0
        )
