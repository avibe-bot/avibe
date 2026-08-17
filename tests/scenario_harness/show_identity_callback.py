from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
    STATE_SIGNING_KEY = b"show-identity-contract-state-key"

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

    @property
    def browser_session_cookie(self) -> str | None:
        return self.browser_session_cookies.get(self.DEFAULT_BROWSER)

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
        accept_set_cookie: bool = True,
    ) -> dict[str, object]:
        self._flow_number += 1
        nonce = f"nonce-{self._flow_number}"
        pending_flow_cookie = self._opaque_token("pending-flow", browser_id, self._flow_number)
        if accept_set_cookie:
            self.browser_pending_flow_cookies[browser_id] = pending_flow_cookie
        state_claims: dict[str, object] = {
            "instance_id": self.INSTANCE_ID,
            "nonce": nonce,
            "callback_origin": self.CALLBACK_ORIGIN,
            "safe_return_path": "/p/stable_alpha/",
            "pending_flow_cookie_sha256": hashlib.sha256(pending_flow_cookie.encode("ascii")).hexdigest(),
            "iat": self.now,
            "exp": self.now + 300,
        }
        state_claims.update(state_overrides or {})
        state = self._sign_state(state_claims)
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
            "pending_flow_request_cookie": self._pending_flow_cookie_pair(pending_flow_cookie),
            "pending_flow_set_cookie": self._pending_flow_cookie_projection(pending_flow_cookie),
            "form_post": form_post,
        }

    def post_callback(
        self,
        form_post: dict[str, Any],
        *,
        request_cookie: dict[str, object] | None,
        browser_id: str = DEFAULT_BROWSER,
    ) -> dict[str, object]:
        self.events.append("local.callback_http_boundary")
        if (form_post.get("method"), form_post.get("path")) != ("POST", self.CALLBACK_PATH):
            return {"decision": "identity_retry_required"}
        form = form_post.get("form")
        if not isinstance(form, dict):
            return {"decision": "identity_retry_required"}

        self.events.append("local.signed_state_verifier")
        state = self._verify_state(form.get("state"))
        if state is None or not self._valid_time_window(state):
            return {"decision": "identity_retry_required"}

        self.events.append("local.pending_flow_cookie_verifier")
        if not self._valid_pending_flow_cookie_pair(request_cookie):
            return {"decision": "identity_retry_required"}
        pending_flow_cookie = str(request_cookie["value"])
        pending_digest = hashlib.sha256(pending_flow_cookie.encode("ascii")).hexdigest()
        if not hmac.compare_digest(pending_digest, str(state.get("pending_flow_cookie_sha256", ""))):
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

        if self.browser_pending_flow_cookies.get(browser_id) == pending_flow_cookie:
            self.browser_pending_flow_cookies.pop(browser_id, None)
        self.events.append("local.pending_flow_cookie_expiry")
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
        self.events.append("local.identity_session_set_cookie")
        return {
            "decision": "return_to_share",
            "location": state["safe_return_path"],
            "expire_pending_flow_cookie": self._expired_pending_flow_cookie_projection(),
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
        browser_id = str(flow["browser_id"])
        pending_flow_cookie = self.browser_pending_flow_cookies.get(browser_id)
        return self.post_callback(
            flow["form_post"],
            request_cookie=(
                self._pending_flow_cookie_pair(pending_flow_cookie) if pending_flow_cookie is not None else None
            ),
            browser_id=browser_id,
        )

    def remove_member(self) -> None:
        self.emails.remove("alice@example.com")

    def advance_clock(self, seconds: int) -> None:
        self.now += seconds
        self.backend.now = self.now

    def _valid_time_window(self, claims: dict[str, object]) -> bool:
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        return (
            type(issued_at) is int
            and type(expires_at) is int
            and expires_at - issued_at == 300
            and issued_at <= self.now < expires_at
        )

    @staticmethod
    def _opaque_token(purpose: str, browser_id: str, sequence: int) -> str:
        material = hashlib.sha256(f"{purpose}:{browser_id}:{sequence}".encode()).digest()
        return base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")

    @classmethod
    def _sign_state(cls, claims: dict[str, object]) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_segment = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(cls.STATE_SIGNING_KEY, payload_segment, hashlib.sha256).digest()
        signature_segment = base64.urlsafe_b64encode(signature).rstrip(b"=")
        return f"{payload_segment.decode('ascii')}.{signature_segment.decode('ascii')}"

    @classmethod
    def _verify_state(cls, value: object) -> dict[str, object] | None:
        if not isinstance(value, str):
            return None
        try:
            payload_segment, signature_segment = value.split(".")
            expected = hmac.new(cls.STATE_SIGNING_KEY, payload_segment.encode("ascii"), hashlib.sha256).digest()
            supplied = base64.urlsafe_b64decode(signature_segment + "=" * (-len(signature_segment) % 4))
            if not hmac.compare_digest(expected, supplied):
                return None
            payload = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
            claims = json.loads(payload)
        except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return claims if isinstance(claims, dict) else None

    @staticmethod
    def _pending_flow_cookie_pair(value: str) -> dict[str, str]:
        return {"name": "__Secure-avibe_show_identity_flow", "value": value}

    @staticmethod
    def _valid_pending_flow_cookie_pair(value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {"name", "value"}:
            return False
        token = value.get("value")
        return (
            value.get("name") == "__Secure-avibe_show_identity_flow"
            and isinstance(token, str)
            and token.isascii()
            and len(token) == 43
            and all(character.isalnum() or character in "_-" for character in token)
        )

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

    @staticmethod
    def _expired_pending_flow_cookie_projection() -> dict[str, object]:
        return {
            "name": "__Secure-avibe_show_identity_flow",
            "value": "",
            "host_only": True,
            "domain": None,
            "secure": True,
            "http_only": True,
            "same_site": "None",
            "path": "/auth/show-identity",
            "maximum_age_seconds": 0,
        }
