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
    assert harness.issued_credential is None
    assert "__Host-avibe_show_identity_session=" in response.headers["Set-Cookie"]
    assert "samesite=none" in response.headers["Set-Cookie"].lower()
    assert "Path=/" in response.headers["Set-Cookie"]
    assert harness.later_request().status_code == 200
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


def test_callback_origin_is_exact_server_owned_scheme_host_and_effective_port() -> None:
    contract = ShowIdentityScenarioHarness().handshake["callback_origin_state_machine"]
    server_owned_origins = [vector["server_owned_origin"] for vector in contract["positive_vectors"]]
    for vector in contract["positive_vectors"]:
        harness = ShowIdentityScenarioHarness(callback_origin=vector["server_owned_origin"])
        start = harness.begin()
        assert start.callback_url == vector["redirect_uri"]
        state = harness.state_signer.verify(start.state)
        assert state is not None and state["callback_origin"] == vector["server_owned_origin"]
        response = harness.form_post(start, harness.issue_assertion(start))
        assert response.status_code == 303, vector["id"]
        assert response.headers["Location"] == state["safe_public_return_target"]
        session_token = harness.client.cookies.get(harness.session_contract["cookie"]["name"])
        record = harness.identity_sessions.records[harness.identity_sessions.token_hash(session_token)]
        assert record["callback_origin"] == vector["server_owned_origin"]
        assert harness.later_request().status_code == 200

    for redirect_uri in [
        "https://show.example.test:8443/auth/show-identity/callback",
        "http://show.example.test/auth/show-identity/callback",
        "https://attacker.example/auth/show-identity/callback",
        "https://show.example.test/auth/show-identity/callback/other",
        "https://show.example.test/auth/show-identity/callback?query=1",
        "https://show.example.test/auth/show-identity/callback#fragment",
    ]:
        assert not BackendRs256Issuer.authorize_redirect_uri(
            redirect_uri,
            server_owned_origins,
            contract["fixed_callback_path"],
        )

    mutation_names = {
        "replace_callback_port",
        "replace_callback_scheme",
        "replace_callback_host",
        "replace_callback_path",
        "add_callback_query",
        "add_callback_fragment",
    }
    for mutation in mutation_names:
        harness = ShowIdentityScenarioHarness()
        assert harness.exercise_negative(mutation) == "reject_without_credential_or_page_bytes"
        assert harness.verifier.verify_call_count == 0
        assert harness.cookie_selection_count == 0
        assert harness.session_rotation_count == 0
        assert harness.page_lookup_count == 0


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

    clock = {"now": REFERENCE_NOW}
    expiring_provider = JwksProvider(harness.issuer.jwks())
    expiring_verifier = AvibeJwtVerifier(
        harness.pairing,
        expiring_provider,
        harness.backend,
        lambda: clock["now"],
    )
    assert expiring_verifier.verify(token) == claims
    clock["now"] += 300
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(expiring_verifier.verify, [token, token])) == [claims, claims]
    assert expiring_provider.forced_refresh_count == 1

    retention = harness.backend["signing_keys"]["minimum_previous_key_availability"]
    assert (
        retention["derived_seconds"]
        == (retention["maximum_assertion_lifetime_seconds"] + 2 * retention["verifier_clock_skew_seconds"])
        == 720
    )
    retention_issuer = BackendRs256Issuer(harness.pairing)
    boundary_claims = {
        **claims,
        "iat": REFERENCE_NOW + harness.backend["verifier_clock_skew_seconds"],
        "exp": REFERENCE_NOW
        + harness.backend["verifier_clock_skew_seconds"]
        + harness.backend["maximum_lifetime_seconds"],
        "jti": "jti_previous_key_derived_boundary_0005",
    }
    boundary_token = retention_issuer.issue(boundary_claims)
    retention_issuer.rotate()
    before_clock = {"now": REFERENCE_NOW + 719}
    before_verifier = AvibeJwtVerifier(
        harness.pairing,
        JwksProvider(retention_issuer.jwks()),
        harness.backend,
        lambda: before_clock["now"],
    )
    assert before_verifier.verify(boundary_token) == boundary_claims
    retention_issuer.retire_previous()
    after_clock = {"now": REFERENCE_NOW + 721}
    after_verifier = AvibeJwtVerifier(
        harness.pairing,
        JwksProvider(retention_issuer.jwks()),
        harness.backend,
        lambda: after_clock["now"],
    )
    assert after_verifier.verify(boundary_token) is None

    unavailable_at_expiry = JwksProvider(harness.issuer.jwks())
    unavailable_clock = {"now": REFERENCE_NOW}
    unavailable_at_expiry_verifier = AvibeJwtVerifier(
        harness.pairing,
        unavailable_at_expiry,
        harness.backend,
        lambda: unavailable_clock["now"],
    )
    unavailable_at_expiry.available = False
    unavailable_clock["now"] += 300
    assert unavailable_at_expiry_verifier.verify(token) is None
    assert unavailable_at_expiry.forced_refresh_count == 1


def test_compact_jwt_boundary_rejects_every_malformed_authority_type_without_raise() -> None:
    harness = ShowIdentityScenarioHarness()
    start = harness.begin()
    claims = harness.assertion_claims(start, jti="jti_strict_wire_contract_0001")
    header = {"alg": "RS256", "typ": "JWT", "kid": harness.issuer.current_kid}
    strict = harness.backend["wire_protocol"]["strict_json_boundary"]

    invalid_string_values: list[object] = [None, 7, True, [], {}, ""]
    for field in strict["header_string_limits"]:
        for invalid in invalid_string_values:
            mutated = {**header, field: invalid}
            token = harness.issuer.issue_raw_json(json.dumps(mutated), json.dumps(claims))
            assert harness.verifier.verify(token) is None, (field, invalid)
        oversized = {**header, field: "x" * (strict["header_string_limits"][field] + 1)}
        assert harness.verifier.verify(harness.issuer.issue_raw_json(json.dumps(oversized), json.dumps(claims))) is None

    for field in strict["payload_string_limits"]:
        for invalid in invalid_string_values:
            mutated = {**claims, field: invalid}
            token = harness.issuer.issue_raw_json(json.dumps(header), json.dumps(mutated))
            assert harness.verifier.verify(token) is None, (field, invalid)
        oversized = {**claims, field: "x" * (strict["payload_string_limits"][field] + 1)}
        assert harness.verifier.verify(harness.issuer.issue_raw_json(json.dumps(header), json.dumps(oversized))) is None

    for field in strict["numeric_date_fields"]:
        for invalid in [None, True, 1.5, "1800000000", [], {}]:
            mutated = {**claims, field: invalid}
            token = harness.issuer.issue_raw_json(json.dumps(header), json.dumps(mutated))
            assert harness.verifier.verify(token) is None, (field, invalid)
    inverted_dates = {**claims, "iat": REFERENCE_NOW + 30, "exp": REFERENCE_NOW - 30}
    assert harness.verifier.verify(harness.issue_assertion(start, inverted_dates)) is None

    extra_header = {**header, "extra": "value"}
    extra_payload = {**claims, "extra": "value"}
    malformed_tokens = [
        harness.issuer.issue_raw_json(json.dumps(extra_header), json.dumps(claims)),
        harness.issuer.issue_raw_json(json.dumps(header), json.dumps(extra_payload)),
        "not.a.valid-token",
        "x" * (harness.backend["wire_protocol"]["maximum_compact_token_bytes"] + 1),
    ]
    for field in header:
        missing = {key: value for key, value in header.items() if key != field}
        duplicate = json.dumps(header)[:-1] + f',"{field}":{json.dumps(header[field])}}}'
        malformed_tokens.extend(
            [
                harness.issuer.issue_raw_json(json.dumps(missing), json.dumps(claims)),
                harness.issuer.issue_raw_json(duplicate, json.dumps(claims)),
            ]
        )
    for field in claims:
        missing = {key: value for key, value in claims.items() if key != field}
        duplicate = json.dumps(claims)[:-1] + f',"{field}":{json.dumps(claims[field])}}}'
        malformed_tokens.extend(
            [
                harness.issuer.issue_raw_json(json.dumps(header), json.dumps(missing)),
                harness.issuer.issue_raw_json(json.dumps(header), duplicate),
            ]
        )
    for token in malformed_tokens:
        assert harness.verifier.verify(token) is None


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
                domain=race_harness.callback_origin["normalized_host"],
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


def test_state_and_identity_session_lifecycles_are_executable() -> None:
    harness = ShowIdentityScenarioHarness()
    lifecycle = harness.handshake["signed_state_lifecycle"]

    for vector in lifecycle["vectors"]:
        candidate = ShowIdentityScenarioHarness()
        start = candidate.begin()
        state = candidate.state_signer.verify(start.state)
        assert state is not None
        if vector["id"] == "STATE-NONINTEGER":
            state["issued_at"] = float(candidate.now)
        else:
            state["issued_at"] = candidate.now + vector["issued_at_delta_seconds"]
            state["expires_at"] = state["issued_at"] + vector["lifetime_seconds"]
        response = candidate.form_post(
            start,
            candidate.issue_assertion(start),
            state=candidate.state_signer.sign(state),
        )
        expected_status = 303 if vector["outcome"] == "accept_state" else 403
        assert response.status_code == expected_status, vector["id"]

    callback = ShowIdentityScenarioHarness()
    start = callback.begin()
    assert callback.form_post(start, callback.issue_assertion(start)).status_code == 303
    session_name = callback.session_contract["cookie"]["name"]
    session_token = callback.client.cookies.get(session_name)
    assert session_token is not None
    assert session_token not in callback.identity_sessions.records
    record = callback.identity_sessions.records[callback.identity_sessions.token_hash(session_token)]
    assert record["expires_at"] - record["issued_at"] == 86400
    assert record["callback_origin"] == callback.callback_origin
    assert record["lineage_generation"] == 1
    assert callback.later_request().json()["outcome"] == "serve_shared_after_current_membership"

    callback.pages[start.share_id]["emails"] = []
    removed = callback.later_request()
    assert removed.status_code == 403
    assert removed.json()["outcome"] == "generic_deny_without_page_bytes_or_login_loop"
    callback.pages[start.share_id]["emails"] = ["bob@example.com"]
    assert callback.later_request(base_url="https://attacker.example").status_code == 403

    callback.now += 86401
    assert callback.later_request().json()["outcome"] == "identity_required_without_page_bytes"

    reset = ShowIdentityScenarioHarness()
    reset_start = reset.begin()
    assert reset.form_post(reset_start, reset.issue_assertion(reset_start)).status_code == 303
    reset.identity_sessions.reset()
    assert reset.later_request().json()["outcome"] == "identity_required_without_page_bytes"

    rotation = ShowIdentityScenarioHarness()
    first = rotation.begin()
    assert rotation.form_post(first, rotation.issue_assertion(first)).status_code == 303
    session_cookie = rotation.session_contract["cookie"]["name"]
    first_token = rotation.client.cookies.get(session_cookie)
    first_record = rotation.identity_sessions.records[rotation.identity_sessions.token_hash(first_token)]
    first_lineage = first_record["lineage_id"]
    second = rotation.begin()
    assert rotation.form_post(second, rotation.issue_assertion(second)).status_code == 303
    assert session_cookie in rotation.last_browser_sent_cookie_names
    assert session_cookie in rotation.last_callback_cookie_names
    second_token = rotation.client.cookies.get(session_cookie)
    assert first_token != second_token
    assert rotation.identity_sessions.token_hash(first_token) not in rotation.identity_sessions.records
    second_record = rotation.identity_sessions.records[rotation.identity_sessions.token_hash(second_token)]
    assert (second_record["lineage_id"], second_record["lineage_generation"]) == (first_lineage, 2)
    rotation.client.cookies.set(
        session_cookie, first_token, domain=rotation.callback_origin["normalized_host"], path="/"
    )
    assert rotation.later_request().json()["outcome"] == "identity_required_without_page_bytes"
    rotation.client.cookies.set(
        session_cookie, second_token, domain=rotation.callback_origin["normalized_host"], path="/"
    )
    assert rotation.later_request().status_code == 200
    rotation.client.cookies.set(
        session_cookie, first_token, domain=rotation.callback_origin["normalized_host"], path="/"
    )
    stale_started_flow = rotation.begin()
    assert rotation.flow_prior_sessions[stale_started_flow.nonce] is None
    assert rotation.form_post(stale_started_flow, rotation.issue_assertion(stale_started_flow)).status_code == 303
    replacement_token = rotation.client.cookies.get(session_cookie)
    replacement_record = rotation.identity_sessions.records[rotation.identity_sessions.token_hash(replacement_token)]
    assert replacement_record["lineage_id"] != first_lineage

    lax_control = ShowIdentityScenarioHarness()
    lax_control.session_contract["cookie"]["same_site"] = "Lax"
    lax_first = lax_control.begin()
    assert lax_control.form_post(lax_first, lax_control.issue_assertion(lax_first)).status_code == 303
    lax_second = lax_control.begin()
    assert lax_control.form_post(lax_second, lax_control.issue_assertion(lax_second)).status_code == 303
    assert session_cookie not in lax_control.last_browser_sent_cookie_names
    assert len(lax_control.identity_sessions.records) == 2

    concurrent = ShowIdentityScenarioHarness()
    base = concurrent.begin()
    assert concurrent.form_post(base, concurrent.issue_assertion(base)).status_code == 303
    base_token = concurrent.client.cookies.get(session_cookie)
    flow_a = concurrent.begin()
    flow_b = concurrent.begin()
    response_a = concurrent.form_post(flow_a, concurrent.issue_assertion(flow_a))
    token_a = concurrent.client.cookies.get(session_cookie)
    concurrent.client.cookies.set(
        session_cookie,
        base_token,
        domain=concurrent.callback_origin["normalized_host"],
        path="/",
    )
    response_b = concurrent.form_post(flow_b, concurrent.issue_assertion(flow_b))
    token_b = concurrent.client.cookies.get(session_cookie)
    assert response_a.status_code == response_b.status_code == 303
    assert token_a != token_b
    assert len(concurrent.identity_sessions.records) == 1
    active_lineages = {item["lineage_id"] for item in concurrent.identity_sessions.records.values()}
    assert len(active_lineages) == 1
    active_record = next(iter(concurrent.identity_sessions.records.values()))
    assert active_record["lineage_generation"] == 3
    concurrent.client.cookies.set(
        session_cookie,
        token_a,
        domain=concurrent.callback_origin["normalized_host"],
        path="/",
    )
    assert concurrent.later_request().status_code == 403
    concurrent.client.cookies.set(
        session_cookie,
        token_b,
        domain=concurrent.callback_origin["normalized_host"],
        path="/",
    )
    assert concurrent.later_request().status_code == 200

    unrelated = ShowIdentityScenarioHarness()
    browser_a = TestClient(unrelated.app)
    browser_b = TestClient(unrelated.app)
    unrelated_a = unrelated.begin(client=browser_a)
    unrelated_b = unrelated.begin(client=browser_b)
    assert unrelated.form_post(unrelated_a, unrelated.issue_assertion(unrelated_a), client=browser_a).status_code == 303
    assert unrelated.form_post(unrelated_b, unrelated.issue_assertion(unrelated_b), client=browser_b).status_code == 303
    assert len({item["lineage_id"] for item in unrelated.identity_sessions.records.values()}) == 2

    unchanged = ShowIdentityScenarioHarness()
    valid = unchanged.begin()
    assert unchanged.form_post(valid, unchanged.issue_assertion(valid)).status_code == 303
    before_lineages = json.loads(json.dumps(unchanged.identity_sessions.lineages))
    invalid = unchanged.begin()
    assert unchanged.form_post(invalid, unchanged.issue_assertion(invalid), state="invalid-state").status_code == 403
    assert unchanged.identity_sessions.lineages == before_lineages
    assert unchanged.exercise_negative("identity_unavailable") == "identity_unavailable_no_store_without_assertion"
    assert unchanged.identity_sessions.lineages == before_lineages
