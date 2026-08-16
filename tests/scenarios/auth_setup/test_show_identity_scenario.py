from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.scenario_harness.show_identity import (
    REFERENCE_NOW,
    AvibeJwtVerifier,
    BackendRs256Issuer,
    JwksProvider,
    PairingRecord,
    ShowIdentityScenarioHarness,
    _decode,
    _encode,
)


def _replace_header(token: str, header: dict[str, object]) -> str:
    _, payload, signature = token.split(".")
    encoded = _encode(json.dumps(header, separators=(",", ":")).encode())
    return f"{encoded}.{payload}.{signature}"


def test_form_post_closes_custom_host_identity_and_membership_loop() -> None:
    """Scenario: AUTH-SETUP-404; SHOW-LIVE-003; SHOW-LIVE-037."""
    harness = ShowIdentityScenarioHarness()
    start = harness.begin()

    assert start.callback_url == "https://show.example.test/auth/show-identity/callback"
    assert start.cookie_name == f"__Secure-avibe_show_identity_c_{start.nonce}"
    assert "Path=/auth/show-identity/callback" in start.set_cookie
    assert "Secure" in start.set_cookie
    assert "HttpOnly" in start.set_cookie
    assert "samesite=none" in start.set_cookie.lower()
    assert "Domain=" not in start.set_cookie
    assert "Max-Age=300" in start.set_cookie
    assert start.state not in start.callback_url

    assertion = harness.issue_assertion(start)
    assert len(assertion.split(".")) == 3
    assert json.loads(_decode(assertion.split(".")[0])) == {
        "alg": "RS256",
        "kid": harness.issuer.current_kid,
        "typ": "JWT",
    }
    response = harness.form_post(start, assertion)
    assert response.status_code == 303
    assert response.headers["Location"] == "/p/identity-contract-share/"
    assert response.headers["Cache-Control"] == "no-store"
    assert harness.http_callback_count == 1
    assert harness.issued_credential == {
        "instance_id": "ins_identity_contract",
        "page_id": "ses_identity_contract",
        "share_id": "identity-contract-share",
        "audience_revision": 12,
        "admitted_normalized_email": "bob@example.com",
    }
    assert f'{start.cookie_name}=""' in response.headers["Set-Cookie"]
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    retained_until = next(iter(harness.consumption_store.retained_until.values()))
    assert retained_until == REFERENCE_NOW + 360

    for negative in harness.scenario["negative_callbacks"]:
        negative_harness = ShowIdentityScenarioHarness()
        assert negative_harness.exercise_negative(negative["mutation"]) == negative["outcome"], negative["id"]
        assert negative_harness.successful_callback_count == (
            1 if negative["mutation"] == "reuse_consumed_nonce" else 0
        )


def test_rs256_jwt_jwks_rotation_and_failure_vectors_are_executable() -> None:
    harness = ShowIdentityScenarioHarness()
    start = harness.begin()
    claims = harness.assertion_claims(start, jti="jti_current_key_contract_0001")
    token = harness.issue_assertion(start, claims)
    assert harness.verifier.verify(token) == claims

    unsupported_headers = [
        {"alg": "RS256", "typ": "JWT"},
        {"alg": "none", "typ": "JWT", "kid": "none-key"},
        {"alg": "RS256", "typ": "JWT", "kid": harness.issuer.current_kid, "jku": "https://evil.test/jwks"},
        {"alg": "RS256", "typ": "JWT", "kid": harness.issuer.current_kid, "jwk": {}},
        {"alg": "RS256", "typ": "JWT", "kid": harness.issuer.current_kid, "crit": ["exp"]},
        {"alg": "RS256", "typ": "JWT", "kid": harness.issuer.current_kid, "b64": False},
    ]
    for header in unsupported_headers:
        assert harness.verifier.verify(_replace_header(token, header)) is None

    old_token = token
    harness.issuer.rotate()
    new_claims = {**claims, "jti": "jti_new_key_contract_0002"}
    new_token = harness.issuer.issue(new_claims)
    rotated = AvibeJwtVerifier(harness.pairing, JwksProvider(harness.issuer.jwks()), harness.backend)
    assert rotated.verify(old_token) == claims
    assert rotated.verify(new_token) == new_claims

    refresh_issuer = BackendRs256Issuer(harness.pairing)
    initial_jwks = refresh_issuer.jwks()
    refresh_issuer.rotate()
    refresh_claims = {**claims, "jti": "jti_refresh_contract_0003"}
    refresh_token = refresh_issuer.issue(refresh_claims)
    provider = JwksProvider(initial_jwks, refresh_issuer.jwks())
    refresh_verifier = AvibeJwtVerifier(harness.pairing, provider, harness.backend)
    assert refresh_verifier.verify(refresh_token) == refresh_claims
    assert provider.forced_refresh_count == 1

    unknown_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    unknown = harness.issuer.issue(
        {**claims, "jti": "jti_unknown_contract_0004"},
        key=unknown_key,
        kid="unknown-key",
    )
    unknown_provider = JwksProvider(harness.issuer.jwks())
    unknown_verifier = AvibeJwtVerifier(harness.pairing, unknown_provider, harness.backend)
    assert unknown_verifier.verify(unknown) is None
    assert unknown_verifier.verify(unknown) is None
    assert unknown_provider.forced_refresh_count == 1

    coalesced_provider = JwksProvider(harness.issuer.jwks())
    coalesced_verifier = AvibeJwtVerifier(harness.pairing, coalesced_provider, harness.backend)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(coalesced_verifier.verify, [unknown, unknown])) == [None, None]
    assert coalesced_provider.forced_refresh_count == 1

    duplicate = harness.issuer.jwks()
    duplicate["keys"].append(dict(duplicate["keys"][0]))
    with pytest.raises(ValueError, match="duplicate_or_missing_kid"):
        AvibeJwtVerifier(harness.pairing, JwksProvider(duplicate), harness.backend)

    changed = harness.issuer.jwks()
    replacement = BackendRs256Issuer(harness.pairing).current_key
    changed["keys"][0] = BackendRs256Issuer.public_jwk(replacement, harness.issuer.current_kid)
    changed_provider = JwksProvider(harness.issuer.jwks(), changed)
    changed_verifier = AvibeJwtVerifier(harness.pairing, changed_provider, harness.backend)
    assert changed_verifier.verify(unknown) is None
    assert changed_provider.forced_refresh_count == 1

    unavailable_provider = JwksProvider(harness.issuer.jwks())
    unavailable_verifier = AvibeJwtVerifier(harness.pairing, unavailable_provider, harness.backend)
    unavailable_provider.available = False
    assert unavailable_verifier.verify(unknown) is None
    assert unavailable_provider.forced_refresh_count == 1

    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_jwks = {"keys": [BackendRs256Issuer.public_jwk(weak_key, "weak-key")]}
    with pytest.raises(ValueError, match="rsa_modulus_too_small"):
        AvibeJwtVerifier(harness.pairing, JwksProvider(weak_jwks), harness.backend)

    with pytest.raises(ValueError, match="malformed_jwks"):
        AvibeJwtVerifier(harness.pairing, JwksProvider({"not_keys": []}), harness.backend)

    wrong_pairing = PairingRecord(jwks_uri="https://keys.evil.test/oauth/jwks.json")
    with pytest.raises(ValueError, match="jwks_not_exact_paired_same_origin_uri"):
        AvibeJwtVerifier(wrong_pairing, JwksProvider(harness.issuer.jwks()), harness.backend)


def test_flow_specific_cookies_and_atomic_consumption_support_concurrency() -> None:
    harness = ShowIdentityScenarioHarness()
    harness.add_page("ses_identity_other", "identity-other-share", ["bob@example.com"])
    flow_a = harness.begin()
    flow_b = harness.begin(share_id="identity-other-share")
    assert flow_a.cookie_name != flow_b.cookie_name

    response_b = harness.form_post(flow_b, harness.issue_assertion(flow_b))
    assert response_b.status_code == 303
    assert flow_b.cookie_name in response_b.headers["Set-Cookie"]
    assert flow_a.cookie_name not in response_b.headers["Set-Cookie"]
    response_a = harness.form_post(flow_a, harness.issue_assertion(flow_a))
    assert response_a.status_code == 303
    assert harness.successful_callback_count == 2

    same_page = ShowIdentityScenarioHarness()
    same_a = same_page.begin()
    same_b = same_page.begin()
    assert same_page.form_post(same_a, same_page.issue_assertion(same_a)).status_code == 303
    assert same_page.form_post(same_b, same_page.issue_assertion(same_b)).status_code == 303

    swapped = ShowIdentityScenarioHarness()
    swap_a = swapped.begin()
    swap_b = swapped.begin()
    swap_response = swapped.form_post(swap_a, swapped.issue_assertion(swap_b))
    assert swap_response.status_code == 403
    assert swap_a.cookie_name in swap_response.headers["Set-Cookie"]
    assert swap_b.cookie_name not in swap_response.headers["Set-Cookie"]
    assert swapped.form_post(swap_b, swapped.issue_assertion(swap_b)).status_code == 303

    cookie_swap = ShowIdentityScenarioHarness()
    cookie_a = cookie_swap.begin()
    cookie_b = cookie_swap.begin()
    cookie_swap.replace_cookie(cookie_a, cookie_b.cookie_value)
    cookie_swap_response = cookie_swap.form_post(cookie_a, cookie_swap.issue_assertion(cookie_a))
    assert cookie_swap_response.status_code == 403
    assert cookie_a.cookie_name in cookie_swap_response.headers["Set-Cookie"]
    assert cookie_b.cookie_name not in cookie_swap_response.headers["Set-Cookie"]
    assert cookie_swap.form_post(cookie_b, cookie_swap.issue_assertion(cookie_b)).status_code == 303

    invalid_state = ShowIdentityScenarioHarness()
    invalid_a = invalid_state.begin()
    invalid_b = invalid_state.begin()
    invalid_response = invalid_state.form_post(
        invalid_a,
        invalid_state.issue_assertion(invalid_a),
        state="not-a-signed-state",
    )
    assert invalid_response.status_code == 403
    assert "Set-Cookie" not in invalid_response.headers
    assert invalid_state.form_post(invalid_a, invalid_state.issue_assertion(invalid_a)).status_code == 303
    assert invalid_state.form_post(invalid_b, invalid_state.issue_assertion(invalid_b)).status_code == 303

    expiry = ShowIdentityScenarioHarness()
    expired_a = expiry.begin()
    current_b = expiry.begin()
    expired_state = expiry.state_signer.verify(expired_a.state)
    assert expired_state is not None
    expired_state["expires_at"] = REFERENCE_NOW - 1
    expired_response = expiry.form_post(
        expired_a,
        expiry.issue_assertion(expired_a),
        state=expiry.state_signer.sign(expired_state),
    )
    assert expired_response.status_code == 403
    assert expired_a.cookie_name in expired_response.headers["Set-Cookie"]
    assert current_b.cookie_name not in expired_response.headers["Set-Cookie"]
    assert expiry.form_post(current_b, expiry.issue_assertion(current_b)).status_code == 303

    race_harness = ShowIdentityScenarioHarness()
    race = race_harness.begin()
    race_claims = race_harness.assertion_claims(race, jti="jti_atomic_race_contract_0001")
    race_token = race_harness.issue_assertion(race, race_claims)

    def race_post(_: int) -> int:
        with TestClient(race_harness.app) as client:
            client.cookies.set(
                race.cookie_name,
                race.cookie_value,
                domain=race_harness.start_contract["callback_hostname"],
                path=race_harness.cookie_contract["path"],
            )
            response = client.post(
                race.callback_url,
                data={"state": race.state, "assertion": race_token},
                follow_redirects=False,
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        race_results = list(executor.map(race_post, range(2)))
    assert sorted(race_results) == [303, 403]

    replay_harness = ShowIdentityScenarioHarness()
    replay = replay_harness.begin()
    replay_claims = replay_harness.assertion_claims(replay, jti="jti_replay_contract_0002")
    replay_token = replay_harness.issue_assertion(replay, replay_claims)
    assert replay_harness.form_post(replay, replay_token).status_code == 303
    replay_harness.replace_cookie(replay)
    assert replay_harness.form_post(replay, replay_token).status_code == 403

    rotated_flows = ShowIdentityScenarioHarness()
    old_flow = rotated_flows.begin()
    old_token = rotated_flows.issue_assertion(old_flow)
    rotated_flows.issuer.rotate()
    rotated_flows.jwks_provider = JwksProvider(rotated_flows.issuer.jwks())
    rotated_flows.verifier = AvibeJwtVerifier(
        rotated_flows.pairing,
        rotated_flows.jwks_provider,
        rotated_flows.backend,
    )
    new_flow = rotated_flows.begin()
    new_token = rotated_flows.issue_assertion(new_flow)
    assert rotated_flows.form_post(old_flow, old_token).status_code == 303
    assert rotated_flows.form_post(new_flow, new_token).status_code == 303
