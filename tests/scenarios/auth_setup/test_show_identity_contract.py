from __future__ import annotations

import copy
import json
import urllib.parse
from pathlib import Path
from typing import Any


CONTRACT = Path(__file__).resolve().parents[3] / "docs/plans/show-access-contracts/identity-auth.json"


def _evaluate_callback(
    backend: dict[str, Any],
    start: dict[str, Any],
    callback: dict[str, Any],
    consumed_nonces: set[str],
    consumed_jtis: set[str],
) -> str:
    if callback.get("backend_error") in {"identity_not_verified", "identity_unavailable"}:
        return f"{callback['backend_error']}_no_store_without_assertion"
    if not start["signed_state_valid"] or not callback["assertion_valid"]:
        return "reject_without_credential_or_page_bytes"
    parsed_callback = urllib.parse.urlsplit(callback["callback_url"])
    if callback["http_method"] != "POST" or callback["delivery"] != "form_post":
        return "reject_without_credential_or_page_bytes"
    if callback["assertion_location"] != "form_body":
        return "reject_without_credential_or_page_bytes"
    if parsed_callback.scheme != "https" or parsed_callback.path != "/auth/show-identity/callback":
        return "reject_without_credential_or_page_bytes"
    if parsed_callback.hostname not in start["allowed_callback_hostnames"]:
        return "reject_without_credential_or_page_bytes"
    if parsed_callback.query or parsed_callback.fragment:
        return "reject_without_credential_or_page_bytes"
    if callback["nonce"] in consumed_nonces or callback["jti"] in consumed_jtis:
        return "reject_without_credential_or_page_bytes"
    if callback["nonce"] != start["nonce"] or callback["instance_id"] != start["instance_id"]:
        return "reject_without_credential_or_page_bytes"
    if callback["correlation_cookie"] != start["correlation_cookie"]:
        return "reject_without_credential_or_page_bytes"
    if callback["resolved_page_id"] != start["page_id"] or callback["resolved_share_id"] != start["share_id"]:
        return "reject_without_credential_or_page_bytes"
    if start["safe_public_return_target"] != f"/p/{start['share_id']}/":
        return "reject_without_credential_or_page_bytes"
    if set(callback.get("extra_claims", ())) & set(backend["forbidden_claims"]):
        return "reject_without_credential_or_page_bytes"
    consumed_nonces.add(callback["nonce"])
    consumed_jtis.add(callback["jti"])
    if callback["verified_email"] not in callback["current_local_emails"]:
        return "generic_deny_without_page_bytes_or_login_loop"
    return "issue_share_bound_browsing_credential"


def test_limited_show_identity_contract_closes_signed_state_callback_and_membership_loop() -> None:
    """Scenario: AUTH-SETUP-404; SHOW-LIVE-003; SHOW-LIVE-037."""
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    backend = contract["backend_assertion"]
    scenario = contract["closed_loop_scenario"]
    start = scenario["start"]
    callback = scenario["callback"]
    consumed_nonces: set[str] = set()
    consumed_jtis: set[str] = set()

    assert _evaluate_callback(backend, start, callback, consumed_nonces, consumed_jtis) == callback["outcome"]
    assert callback["nonce"] in consumed_nonces
    assert callback["jti"] in consumed_jtis
    assert backend["authorize_endpoint"] == {
        "method": "GET",
        "path_template": "/api/v1/instances/{instanceId}/show-identity/authorize",
    }
    assert backend["request_inputs"]["required"] == ["state", "nonce", "redirect_uri"]
    assert backend["request_inputs"]["additional_allowed"] is False

    protected = scenario["protected_request"]
    assert protected["credential_binding"] == {
        "instance_id": callback["instance_id"],
        "page_id": callback["resolved_page_id"],
        "share_id": callback["resolved_share_id"],
        "audience_revision": callback["resolved_audience_revision"],
    }
    assert protected["membership_rechecked"] is True
    assert protected["outcome"] == "serve_shared"

    for negative in scenario["negative_callbacks"]:
        mutated_start = copy.deepcopy(start)
        mutated_callback = copy.deepcopy(callback)
        negative_consumed: set[str] = set()
        negative_jtis: set[str] = set()
        mutation = negative["mutation"]
        if mutation == "reuse_consumed_nonce":
            negative_consumed.add(mutated_callback["nonce"])
        elif mutation == "replace_assertion_instance_id":
            mutated_callback["instance_id"] = "ins_other"
        elif mutation == "replace_signed_return_share":
            mutated_start["safe_public_return_target"] = "/p/other-share/"
        elif mutation == "replace_correlation_cookie":
            mutated_callback["correlation_cookie"] = "cookie_other_identity_0001"
        elif mutation == "add_callback_query":
            mutated_callback["callback_url"] += "?assertion=leak"
        elif mutation == "add_callback_fragment":
            mutated_callback["callback_url"] += "#assertion=leak"
        elif mutation == "place_assertion_in_browser_history":
            mutated_callback["assertion_location"] = "browser_history"
        elif mutation == "place_assertion_in_referrer":
            mutated_callback["assertion_location"] = "referrer"
        elif mutation == "replace_callback_hostname":
            mutated_callback["callback_url"] = "https://attacker.example/auth/show-identity/callback"
        elif mutation == "add_page_authorization_claim":
            mutated_callback["extra_claims"] = ["page_authorization"]
        elif mutation in {"identity_not_verified", "identity_unavailable"}:
            mutated_callback["backend_error"] = mutation
        else:
            raise AssertionError(mutation)
        assert (
            _evaluate_callback(backend, mutated_start, mutated_callback, negative_consumed, negative_jtis)
            == negative["outcome"]
        )
