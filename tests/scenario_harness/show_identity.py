from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient


CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs/plans/show-access-contracts/identity-auth.json"
REFERENCE_NOW = 1_800_000_000
SIGNING_KEY = b"show-identity-contract-reference-key"


@dataclass(frozen=True)
class IdentityHandshakeStart:
    state: str
    nonce: str
    callback_url: str
    set_cookie: str


class ShowIdentityScenarioHarness:
    """Executable reference boundary for the cross-repository identity contract."""

    def __init__(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.scenario = self.contract["closed_loop_scenario"]
        self.backend = self.contract["backend_assertion"]
        self.handshake = self.contract["local_handshake"]
        self.start_contract = self.scenario["start"]
        self.callback_contract = self.scenario["callback"]
        self.cookie_contract = self.handshake["correlation_cookie"]
        self.consumed_nonces: set[str] = set()
        self.consumed_jtis: set[str] = set()
        self.issued_credential: dict[str, Any] | None = None
        self.successful_callback_count = 0
        self.http_callback_count = 0
        self.current_page = {
            "instance_id": self.start_contract["instance_id"],
            "page_id": self.start_contract["page_id"],
            "share_id": self.start_contract["share_id"],
            "availability": "active",
            "access_mode": "limited",
            "audience_revision": self.callback_contract["resolved_audience_revision"],
            "emails": list(self.callback_contract["current_local_emails"]),
        }

        self.app = FastAPI()
        self.app.add_api_route("/p/{share_id}/", self._start, methods=["GET"])
        self.app.add_api_route(
            self.cookie_contract["path"],
            self._callback,
            methods=["POST"],
        )
        self.client = TestClient(self.app)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def sign(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        signature = hmac.new(SIGNING_KEY, body, hashlib.sha256).digest()
        return f"{self._encode(body)}.{self._encode(signature)}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            encoded_body, encoded_signature = token.split(".", 1)
            body = self._decode(encoded_body)
            signature = self._decode(encoded_signature)
            expected = hmac.new(SIGNING_KEY, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            value = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _state_payload(self) -> dict[str, Any]:
        return {
            "instance_id": self.start_contract["instance_id"],
            "share_id": self.start_contract["share_id"],
            "safe_public_return_target": self.start_contract["safe_public_return_target"],
            "callback_hostname": self.start_contract["callback_hostname"],
            "nonce": self.start_contract["nonce"],
            "correlation_cookie_sha256": self.start_contract["correlation_cookie_sha256"],
            "issued_at": REFERENCE_NOW,
            "expires_at": self.start_contract["signed_state_expires_at"],
        }

    def _assertion_claims(self) -> dict[str, Any]:
        return {
            "iss": "https://avibe.bot",
            "aud": "avibe-show-identity:oauth-client-contract",
            "sub": "usr_identity_contract",
            "iat": REFERENCE_NOW,
            "exp": REFERENCE_NOW + self.backend["ttl_seconds"],
            "jti": self.callback_contract["jti"],
            "nonce": self.callback_contract["nonce"],
            "instance_id": self.callback_contract["instance_id"],
            "verified_email": self.callback_contract["verified_email"],
        }

    async def _start(self, share_id: str):
        if share_id != self.current_page["share_id"]:
            return JSONResponse({"error": "show_not_found"}, status_code=404)
        state = self.sign(self._state_payload())
        response = JSONResponse(
            {
                "authorize_request": {
                    "state": state,
                    "nonce": self.start_contract["nonce"],
                    "redirect_uri": self.callback_contract["callback_url"],
                }
            }
        )
        response.set_cookie(
            self.cookie_contract["name"],
            self.start_contract["correlation_cookie"],
            max_age=self.start_contract["correlation_cookie_expires_at"] - REFERENCE_NOW,
            secure=self.cookie_contract["secure"],
            httponly=self.cookie_contract["http_only"],
            samesite=self.cookie_contract["same_site"],
            path=self.cookie_contract["path"],
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def begin(self) -> IdentityHandshakeStart:
        response = self.client.get(
            f"https://{self.start_contract['callback_hostname']}{self.start_contract['safe_public_return_target']}",
        )
        assert response.status_code == 200
        authorize_request = response.json()["authorize_request"]
        return IdentityHandshakeStart(
            state=authorize_request["state"],
            nonce=authorize_request["nonce"],
            callback_url=authorize_request["redirect_uri"],
            set_cookie=response.headers["Set-Cookie"],
        )

    def issue_assertion(self, claims: dict[str, Any] | None = None) -> str:
        return self.sign(claims or self._assertion_claims())

    def _deny(self, outcome: str = "reject_without_credential_or_page_bytes") -> JSONResponse:
        response = JSONResponse(
            {"error": "identity_callback_rejected", "outcome": outcome},
            status_code=403,
        )
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            self.cookie_contract["name"],
            secure=self.cookie_contract["secure"],
            httponly=self.cookie_contract["http_only"],
            samesite=self.cookie_contract["same_site"],
            path=self.cookie_contract["path"],
        )
        return response

    async def _callback(self, request: Request):
        self.http_callback_count += 1
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if request.url.query or content_type != self.callback_contract["content_type"]:
            return self._deny()
        decoded_form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        if set(decoded_form) != set(self.backend["delivery"]["form_fields"]):
            return self._deny()
        if any(len(values) != 1 for values in decoded_form.values()):
            return self._deny()
        if request.headers.get("Origin") != self.callback_contract["cross_site_source"]:
            return self._deny()
        if request.headers.get("Sec-Fetch-Site") != "cross-site":
            return self._deny()
        if "assertion=" in request.headers.get("Referer", ""):
            return self._deny()

        state = self.verify(decoded_form["state"][0])
        claims = self.verify(decoded_form["assertion"][0])
        cookie = request.cookies.get(self.cookie_contract["name"])
        if state is None or claims is None or cookie is None:
            return self._deny()
        if set(state) != set(self.handshake["signed_state_fields"]):
            return self._deny()
        if set(claims) != set(self.backend["required_signed_claims"]):
            return self._deny()
        if (
            request.url.hostname != state["callback_hostname"]
            or request.url.hostname not in self.start_contract["allowed_callback_hostnames"]
        ):
            return self._deny()
        if state["issued_at"] > REFERENCE_NOW or state["expires_at"] < REFERENCE_NOW:
            return self._deny()
        if not (claims["iat"] <= REFERENCE_NOW <= claims["exp"]):
            return self._deny()
        if claims["exp"] - claims["iat"] > self.backend["maximum_lifetime_seconds"]:
            return self._deny()
        if claims["iss"] != "https://avibe.bot" or claims["aud"] != "avibe-show-identity:oauth-client-contract":
            return self._deny()
        if claims["verified_email"] != claims["verified_email"].strip(" \t\r\n\f\v").lower():
            return self._deny()
        if claims["nonce"] != state["nonce"] or claims["instance_id"] != state["instance_id"]:
            return self._deny()
        if claims["nonce"] in self.consumed_nonces or claims["jti"] in self.consumed_jtis:
            return self._deny()
        if hashlib.sha256(cookie.encode("utf-8")).hexdigest() != state["correlation_cookie_sha256"]:
            return self._deny()

        page = self.current_page
        if (
            page["instance_id"] != state["instance_id"]
            or page["share_id"] != state["share_id"]
            or state["safe_public_return_target"] != f"/p/{page['share_id']}/"
        ):
            return self._deny()
        if page["availability"] != "active" or page["access_mode"] != "limited":
            return self._deny()
        if claims["verified_email"] not in page["emails"]:
            return self._deny("generic_deny_without_page_bytes_or_login_loop")

        self.consumed_nonces.add(claims["nonce"])
        self.consumed_jtis.add(claims["jti"])
        self.issued_credential = {
            "instance_id": page["instance_id"],
            "page_id": page["page_id"],
            "share_id": page["share_id"],
            "audience_revision": page["audience_revision"],
        }
        self.successful_callback_count += 1
        response = RedirectResponse(state["safe_public_return_target"], status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            self.cookie_contract["name"],
            secure=self.cookie_contract["secure"],
            httponly=self.cookie_contract["http_only"],
            samesite=self.cookie_contract["same_site"],
            path=self.cookie_contract["path"],
        )
        return response

    def form_post(
        self,
        start: IdentityHandshakeStart,
        assertion: str,
        *,
        base_url: str | None = None,
        path_suffix: str = "",
        referer: str = "https://avibe.bot/show-identity/authorize",
    ):
        callback = urlsplit(start.callback_url)
        return self.client.post(
            f"{base_url or f'{callback.scheme}://{callback.netloc}'}{callback.path}{path_suffix}",
            data={"state": start.state, "assertion": assertion},
            headers={
                "Origin": self.callback_contract["cross_site_source"],
                "Referer": referer,
                "Sec-Fetch-Site": "cross-site",
            },
            follow_redirects=False,
        )

    def replace_cookie(self, value: str) -> None:
        self.client.cookies.set(
            self.cookie_contract["name"],
            value,
            domain=self.start_contract["callback_hostname"],
            path=self.cookie_contract["path"],
        )

    def exercise_negative(self, mutation: str) -> str:
        if mutation in {"identity_not_verified", "identity_unavailable"}:
            error = next(item for item in self.backend["terminal_errors"] if item["code"] == mutation)
            assert error["cache_control"] == "no-store" and error["assertion_returned"] is False
            return f"{mutation}_no_store_without_assertion"
        if mutation == "add_callback_fragment":
            callback = urlsplit(self.callback_contract["callback_url"] + "#assertion=leak")
            assert callback.fragment and self.backend["callback_uri_policy"]["fragment_allowed"] is False
            return "reject_without_credential_or_page_bytes"

        start = self.begin()
        claims = self._assertion_claims()
        if mutation == "reuse_consumed_nonce":
            first = self.form_post(start, self.issue_assertion(claims))
            assert first.status_code == 303
            # Even a replay that forges the consumed cookie still fails on nonce/jti state.
            self.replace_cookie(self.start_contract["correlation_cookie"])
        elif mutation == "replace_assertion_instance_id":
            claims["instance_id"] = "ins_other"
        elif mutation == "replace_signed_return_share":
            payload = self.verify(start.state)
            assert payload is not None
            payload["safe_public_return_target"] = "/p/other-share/"
            start = IdentityHandshakeStart(self.sign(payload), start.nonce, start.callback_url, start.set_cookie)
        elif mutation == "replace_correlation_cookie":
            self.replace_cookie("cookie_other_identity_0001")
        elif mutation == "add_callback_query":
            response = self.form_post(start, self.issue_assertion(claims), path_suffix="?assertion=leak")
            return response.json()["outcome"]
        elif mutation == "place_assertion_in_browser_history":
            response = self.form_post(start, self.issue_assertion(claims), path_suffix="?assertion=history-leak")
            return response.json()["outcome"]
        elif mutation == "place_assertion_in_referrer":
            response = self.form_post(
                start,
                self.issue_assertion(claims),
                referer="https://avibe.bot/show-identity/authorize?assertion=referrer-leak",
            )
            return response.json()["outcome"]
        elif mutation == "replace_callback_hostname":
            response = self.form_post(start, self.issue_assertion(claims), base_url="https://attacker.example")
            return response.json()["outcome"]
        elif mutation == "add_page_authorization_claim":
            claims["page_authorization"] = "limited"
        elif mutation != "reuse_consumed_nonce":
            raise AssertionError(mutation)

        response = self.form_post(start, self.issue_assertion(claims))
        return response.json()["outcome"]
