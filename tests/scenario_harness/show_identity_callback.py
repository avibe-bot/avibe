from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from typing import Any


class StubShowIdentityBackend:
    """Identity-only Backend boundary for the Show login contract scenario."""

    ISSUER = "https://avibe.example.test"
    AUDIENCE = "avibe-show-identity:oauth-client-1"

    def __init__(self, *, now: int) -> None:
        self.now = now
        self.authorize_requests: list[dict[str, str]] = []
        self.assertions: dict[str, dict[str, object]] = {}

    def authorize(
        self,
        request: dict[str, str],
        *,
        assertion_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if set(request) != {"state", "nonce", "redirect_uri"}:
            raise ValueError("invalid authorize request")
        self.authorize_requests.append(deepcopy(request))
        claims: dict[str, object] = {
            "iss": self.ISSUER,
            "aud": self.AUDIENCE,
            "sub": "user-1",
            "iat": self.now,
            "exp": self.now + 300,
            "jti": f"jti-{len(self.authorize_requests)}",
            "nonce": request["nonce"],
            "instance_id": "instance-1",
            "verified_email": "alice@example.com",
        }
        claims.update(assertion_overrides or {})
        assertion = f"stub-header.stub-payload.signature-{len(self.authorize_requests)}"
        self.assertions[assertion] = claims
        return {
            "method": "POST",
            "path": "/auth/show-identity/callback",
            "form": {"state": request["state"], "assertion": assertion},
        }

    def verify_assertion(self, assertion: str) -> dict[str, object] | None:
        claims = self.assertions.get(assertion)
        return deepcopy(claims) if claims is not None else None


class ShowIdentityCallbackHarness:
    """Reference boundary from limited entry through a later local admission."""

    CALLBACK_ORIGIN = "https://show.example.test"
    CALLBACK_PATH = "/auth/show-identity/callback"
    INSTANCE_ID = "instance-1"
    NOW = 1_786_935_600
    SESSION_LIFETIME = 2_592_000
    DEFAULT_BROWSER = "browser-a"

    def __init__(self) -> None:
        self.now = self.NOW
        self.backend = StubShowIdentityBackend(now=self.now)
        self.browser_pending_flow_cookies: dict[str, str] = {}
        self.browser_session_cookies: dict[str, str] = {}
        self.records: dict[str, dict[str, object]] = {}
        self.emails = {"alice@example.com"}
        self.loaded_documents: set[str] = set()
        self.membership_checks = 0
        self.events: list[str] = []
        self._flow_number = 0
        self._session_number = 0
        self._signed_states: dict[str, dict[str, object]] = {}
        self._pending_flows: dict[str, dict[str, object]] = {}

    @property
    def browser_session_cookie(self) -> str | None:
        return self.browser_session_cookies.get(self.DEFAULT_BROWSER)

    @property
    def pending_flow_count(self) -> int:
        return len(self._pending_flows)

    def navigate(
        self,
        path: str = "/p/stable_alpha/",
        *,
        browser_id: str = DEFAULT_BROWSER,
    ) -> dict[str, str]:
        self.events.append("local.top_level_navigation")
        if path != "/p/stable_alpha/":
            return {"decision": "not_found"}
        session_cookie = self.browser_session_cookies.get(browser_id)
        if session_cookie is None:
            return {"decision": "identity_login_required"}
        digest = hashlib.sha256(session_cookie.encode("ascii")).hexdigest()
        record = self.records.get(digest)
        if (
            record is None
            or record["instance_id"] != self.INSTANCE_ID
            or record["callback_origin"] != self.CALLBACK_ORIGIN
            or int(record["expires_at"]) <= self.now
        ):
            return {"decision": "identity_login_required"}
        self.membership_checks += 1
        if record["normalized_verified_email"] not in self.emails:
            return {"decision": "denied_not_current_member"}
        document_id = f"document-{len(self.loaded_documents) + 1}"
        self.loaded_documents.add(document_id)
        return {"decision": "admitted_shared", "document_id": document_id}

    def start_login(
        self,
        *,
        browser_id: str = DEFAULT_BROWSER,
        state_overrides: dict[str, object] | None = None,
        assertion_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._flow_number += 1
        state = f"signed-state-{self._flow_number}"
        flow_id = f"flow-{self._flow_number}"
        nonce = f"nonce-{self._flow_number}"
        pending_flow_cookie = self.browser_pending_flow_cookies.get(browser_id)
        if pending_flow_cookie is None:
            pending_flow_cookie = self._opaque_token("pending-flow", browser_id, self._flow_number)
            self.browser_pending_flow_cookies[browser_id] = pending_flow_cookie
        state_claims: dict[str, object] = {
            "flow_id": flow_id,
            "instance_id": self.INSTANCE_ID,
            "nonce": nonce,
            "callback_origin": self.CALLBACK_ORIGIN,
            "safe_return_path": "/p/stable_alpha/",
            "iat": self.now,
            "exp": self.now + 300,
        }
        state_claims.update(state_overrides or {})
        self._signed_states[state] = state_claims
        pending_digest = hashlib.sha256(pending_flow_cookie.encode("ascii")).hexdigest()
        self._pending_flows[pending_digest] = {
            "flow_id": flow_id,
            "signed_state_digest": hashlib.sha256(state.encode("ascii")).hexdigest(),
            "expires_at": self.now + 300,
        }
        self.events.append("local.signed_state_signer")
        authorize_request = {
            "state": state,
            "nonce": nonce,
            "redirect_uri": f"{self.CALLBACK_ORIGIN}{self.CALLBACK_PATH}",
        }
        self.events.append("backend.authorize")
        form_post = self.backend.authorize(
            authorize_request,
            assertion_overrides=assertion_overrides,
        )
        return {
            "browser_id": browser_id,
            "authorize_request": authorize_request,
            "pending_flow_cookie": pending_flow_cookie,
            "pending_flow_set_cookie": self._pending_flow_cookie_projection(pending_flow_cookie),
            "form_post": form_post,
        }

    def post_callback(
        self,
        form_post: dict[str, Any],
        *,
        pending_flow_cookie: str | None,
        browser_id: str = DEFAULT_BROWSER,
    ) -> dict[str, object]:
        self.events.append("local.callback_http_boundary")
        if (form_post.get("method"), form_post.get("path")) != ("POST", self.CALLBACK_PATH):
            return {"decision": "identity_retry_required"}
        form = form_post.get("form")
        if not isinstance(form, dict):
            return {"decision": "identity_retry_required"}

        self.events.append("local.signed_state_verifier")
        state = self._signed_states.get(form.get("state"))
        if state is None or not self._valid_time_window(state):
            return {"decision": "identity_retry_required"}

        self.events.append("local.pending_flow_cookie_verifier")
        if not isinstance(pending_flow_cookie, str):
            return {"decision": "identity_retry_required"}
        pending_digest = hashlib.sha256(pending_flow_cookie.encode("ascii")).hexdigest()
        pending = self._pending_flows.get(pending_digest)
        signed_state_digest = hashlib.sha256(form["state"].encode("ascii")).hexdigest()
        if (
            pending is None
            or pending["signed_state_digest"] != signed_state_digest
            or pending["flow_id"] != state.get("flow_id")
            or int(pending["expires_at"]) <= self.now
        ):
            return {"decision": "identity_retry_required"}

        self.events.append("local.assertion_verifier")
        assertion = form.get("assertion")
        claims = self.backend.verify_assertion(assertion) if isinstance(assertion, str) else None
        expected = {
            "iss": self.backend.ISSUER,
            "aud": self.backend.AUDIENCE,
            "nonce": state["nonce"],
            "instance_id": self.INSTANCE_ID,
        }
        if (
            claims is None
            or not self._valid_time_window(claims)
            or any(claims.get(field) != value for field, value in expected.items())
        ):
            return {"decision": "identity_retry_required"}

        self._session_number += 1
        token = self._opaque_token("identity-session", browser_id, self._session_number)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        self.events.append("local.identity_session_digest_store")
        self.records[digest] = {
            "token_sha256": digest,
            "instance_id": self.INSTANCE_ID,
            "callback_origin": self.CALLBACK_ORIGIN,
            "subject": claims["sub"],
            "normalized_verified_email": claims["verified_email"],
            "created_at": self.now,
            "expires_at": self.now + self.SESSION_LIFETIME,
        }
        self.browser_session_cookies[browser_id] = token
        self._signed_states.pop(form["state"], None)
        self._pending_flows.pop(pending_digest, None)
        self.browser_pending_flow_cookies.pop(browser_id, None)
        self.events.append("local.identity_session_set_cookie")
        return {
            "decision": "return_to_share",
            "location": state["safe_return_path"],
            "set_cookie": {
                "name": "__Host-avibe_show_identity_session",
                "value": token,
                "host_only": True,
                "domain": None,
                "secure": True,
                "http_only": True,
                "same_site": "Lax",
                "path": "/",
                "maximum_age_seconds": self.SESSION_LIFETIME,
            },
        }

    def complete_login(self, flow: dict[str, object]) -> dict[str, object]:
        return self.post_callback(
            flow["form_post"],
            pending_flow_cookie=flow["pending_flow_cookie"],
            browser_id=flow["browser_id"],
        )

    def remove_member(self) -> None:
        self.emails.remove("alice@example.com")

    def advance_clock(self, seconds: int) -> None:
        self.now += seconds
        self.backend.now = self.now

    def _valid_time_window(self, claims: dict[str, object]) -> bool:
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        return type(issued_at) is int and type(expires_at) is int and issued_at <= self.now < expires_at

    @staticmethod
    def _opaque_token(purpose: str, browser_id: str, sequence: int) -> str:
        material = hashlib.sha256(f"{purpose}:{browser_id}:{sequence}".encode()).digest()
        return base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")

    @staticmethod
    def _pending_flow_cookie_projection(value: str) -> dict[str, object]:
        return {
            "name": "__Secure-avibe_show_identity_flow",
            "value": value,
            "host_only": True,
            "domain": None,
            "secure": True,
            "http_only": True,
            "same_site": "None",
            "path": "/auth/show-identity",
            "maximum_age_seconds": 300,
        }
