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

    def __init__(self) -> None:
        self.backend = StubShowIdentityBackend(now=self.NOW)
        self.browser_session_cookie: str | None = None
        self.records: dict[str, dict[str, object]] = {}
        self.emails = {"alice@example.com"}
        self.loaded_documents: set[str] = set()
        self.membership_checks = 0
        self.events: list[str] = []
        self._flow_number = 0
        self._signed_states: dict[str, dict[str, object]] = {}
        self._current_correlation_secret: str | None = None

    def navigate(self, path: str = "/p/stable_alpha/") -> dict[str, str]:
        self.events.append("local.top_level_navigation")
        if path != "/p/stable_alpha/":
            return {"decision": "not_found"}
        if self.browser_session_cookie is None:
            return {"decision": "identity_login_required"}
        digest = hashlib.sha256(self.browser_session_cookie.encode("ascii")).hexdigest()
        record = self.records.get(digest)
        if (
            record is None
            or record["instance_id"] != self.INSTANCE_ID
            or record["callback_origin"] != self.CALLBACK_ORIGIN
            or int(record["expires_at"]) <= self.NOW
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
        assertion_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._flow_number += 1
        state = f"signed-state-{self._flow_number}"
        nonce = f"nonce-{self._flow_number}"
        correlation_secret = f"correlation-secret-{self._flow_number}"
        self._signed_states = {
            state: {
                "instance_id": self.INSTANCE_ID,
                "nonce": nonce,
                "callback_origin": self.CALLBACK_ORIGIN,
                "safe_return_path": "/p/stable_alpha/",
            }
        }
        self._current_correlation_secret = correlation_secret
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
            "authorize_request": authorize_request,
            "correlation_cookie": correlation_secret,
            "form_post": form_post,
        }

    def post_callback(
        self,
        form_post: dict[str, Any],
        *,
        correlation_cookie: str | None,
    ) -> dict[str, object]:
        self.events.append("local.callback_http_boundary")
        if (form_post.get("method"), form_post.get("path")) != ("POST", self.CALLBACK_PATH):
            return {"decision": "identity_retry_required"}
        form = form_post.get("form")
        if not isinstance(form, dict):
            return {"decision": "identity_retry_required"}

        self.events.append("local.signed_state_verifier")
        state = self._signed_states.get(form.get("state"))
        if state is None:
            return {"decision": "identity_retry_required"}

        self.events.append("local.correlation_cookie_verifier")
        if correlation_cookie != self._current_correlation_secret:
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
        if claims is None or any(claims.get(field) != value for field, value in expected.items()):
            return {"decision": "identity_retry_required"}

        token = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        self.events.append("local.identity_session_digest_store")
        self.records[digest] = {
            "token_sha256": digest,
            "instance_id": self.INSTANCE_ID,
            "callback_origin": self.CALLBACK_ORIGIN,
            "subject": claims["sub"],
            "normalized_verified_email": claims["verified_email"],
            "created_at": self.NOW,
            "expires_at": self.NOW + self.SESSION_LIFETIME,
        }
        self.browser_session_cookie = token
        self._signed_states.clear()
        self._current_correlation_secret = None
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
            },
        }

    def complete_login(self, flow: dict[str, object]) -> dict[str, object]:
        return self.post_callback(
            flow["form_post"],
            correlation_cookie=flow["correlation_cookie"],
        )

    def remove_member(self) -> None:
        self.emails.remove("alice@example.com")
