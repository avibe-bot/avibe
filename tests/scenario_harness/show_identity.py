from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm


CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs/plans/show-access-contracts/identity-auth.json"
REFERENCE_NOW = 1_800_000_000
STATE_SIGNING_KEY = b"show-identity-local-state-reference-key"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class PairingRecord:
    issuer: str = "https://avibe.bot"
    oauth_client_id: str = "oauth-client-contract"
    instance_id: str = "ins_identity_contract"
    jwks_uri: str = "https://avibe.bot/oauth/jwks.json"

    @property
    def audience(self) -> str:
        return f"avibe-show-identity:{self.oauth_client_id}"


@dataclass(frozen=True)
class IdentityHandshakeStart:
    state: str
    nonce: str
    callback_url: str
    set_cookie: str
    cookie_name: str
    cookie_value: str
    page_id: str
    share_id: str


class LocalStateSigner:
    def sign(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        signature = hmac.new(STATE_SIGNING_KEY, body, hashlib.sha256).digest()
        return f"{_encode(body)}.{_encode(signature)}"

    def verify(self, token: str) -> dict[str, Any] | None:
        try:
            encoded_body, encoded_signature = token.split(".")
            body = _decode(encoded_body)
            signature = _decode(encoded_signature)
            expected = hmac.new(STATE_SIGNING_KEY, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            value = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


class BackendRs256Issuer:
    def __init__(self, pairing: PairingRecord) -> None:
        self.pairing = pairing
        self.current_kid = "show-identity-key-1"
        self.current_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.previous_kid: str | None = None
        self.previous_key: rsa.RSAPrivateKey | None = None
        self._generation = 1

    @staticmethod
    def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        jwk.update({"kid": kid, "kty": "RSA", "use": "sig", "alg": "RS256"})
        return jwk

    def jwks(self) -> dict[str, Any]:
        keys = [self.public_jwk(self.current_key, self.current_kid)]
        if self.previous_key is not None and self.previous_kid is not None:
            keys.append(self.public_jwk(self.previous_key, self.previous_kid))
        return {"keys": keys}

    def rotate(self) -> None:
        self.previous_key = self.current_key
        self.previous_kid = self.current_kid
        self._generation += 1
        self.current_kid = f"show-identity-key-{self._generation}"
        self.current_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def issue(
        self,
        claims: dict[str, Any],
        *,
        headers: dict[str, Any] | None = None,
        key: rsa.RSAPrivateKey | bytes | None = None,
        kid: str | None = None,
        algorithm: str | None = "RS256",
    ) -> str:
        protected = {"typ": "JWT", "kid": kid or self.current_kid}
        if headers:
            protected.update(headers)
        signing_key: Any = key or self.current_key
        if algorithm is None:
            signing_key = None
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers=protected)


class JwksProvider:
    def __init__(
        self,
        initial: dict[str, Any],
        refreshed: dict[str, Any] | None = None,
        *,
        available: bool = True,
    ) -> None:
        self.initial = initial
        self.refreshed = refreshed if refreshed is not None else initial
        self.available = available
        self.forced_refresh_count = 0

    def fetch(self, *, force: bool) -> dict[str, Any]:
        if force:
            self.forced_refresh_count += 1
        if not self.available:
            raise RuntimeError("jwks_unavailable")
        if force:
            return self.refreshed
        return self.initial


class AvibeJwtVerifier:
    def __init__(
        self,
        pairing: PairingRecord,
        provider: JwksProvider,
        contract: dict[str, Any],
    ) -> None:
        self.pairing = pairing
        self.provider = provider
        self.contract = contract
        self._keys: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple[str, str]] = {}
        self._refresh_attempted: set[str] = set()
        self._refresh_lock = threading.Lock()
        if not pairing.issuer.startswith("https://"):
            raise ValueError("issuer_not_https")
        expected_jwks_uri = f"{pairing.issuer}/oauth/jwks.json"
        if pairing.jwks_uri != expected_jwks_uri:
            raise ValueError("jwks_not_exact_paired_same_origin_uri")
        self._install(provider.fetch(force=False))

    def _install(self, document: dict[str, Any]) -> None:
        raw_keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(raw_keys, list):
            raise ValueError("malformed_jwks")
        kids = [item.get("kid") for item in raw_keys if isinstance(item, dict)]
        if any(not isinstance(kid, str) or not kid for kid in kids) or len(kids) != len(set(kids)):
            raise ValueError("duplicate_or_missing_kid")

        installed: dict[str, Any] = {}
        fingerprints: dict[str, tuple[str, str]] = {}
        for item in raw_keys:
            if not isinstance(item, dict):
                raise ValueError("malformed_jwk")
            if (item.get("kty"), item.get("use"), item.get("alg")) != ("RSA", "sig", "RS256"):
                raise ValueError("unsupported_jwk")
            try:
                modulus_bits = int.from_bytes(_decode(item["n"]), "big").bit_length()
                key = RSAAlgorithm.from_jwk(json.dumps(item))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("malformed_rsa_jwk") from exc
            if modulus_bits < 2048:
                raise ValueError("rsa_modulus_too_small")
            kid = item["kid"]
            fingerprint = (item["n"], item["e"])
            if kid in self._fingerprints and self._fingerprints[kid] != fingerprint:
                raise ValueError("changed_material_same_kid")
            installed[kid] = key
            fingerprints[kid] = fingerprint
        self._keys = installed
        self._fingerprints.update(fingerprints)

    @staticmethod
    def _protected_header(token: str) -> dict[str, Any] | None:
        try:
            segments = token.split(".")
            if len(segments) != 3 or any(not segment for segment in segments):
                return None
            header = json.loads(_decode(segments[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return header if isinstance(header, dict) else None

    def verify(self, token: str) -> dict[str, Any] | None:
        header = self._protected_header(token)
        if header is None or set(header) != {"alg", "typ", "kid"}:
            return None
        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            return None
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None

        key = self._keys.get(kid)
        if key is None:
            with self._refresh_lock:
                if kid not in self._refresh_attempted:
                    self._refresh_attempted.add(kid)
                    try:
                        self._install(self.provider.fetch(force=True))
                    except (RuntimeError, ValueError):
                        return None
                key = self._keys.get(kid)
        if key is None:
            return None

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return None
        required = self.contract["required_signed_claims"]
        if not isinstance(claims, dict) or set(claims) != set(required):
            return None
        if claims["iss"] != self.pairing.issuer or claims["aud"] != self.pairing.audience:
            return None
        if claims["instance_id"] != self.pairing.instance_id:
            return None
        if not isinstance(claims["aud"], str):
            return None
        if not (claims["iat"] - self.contract["verifier_clock_skew_seconds"] <= REFERENCE_NOW):
            return None
        if not (REFERENCE_NOW <= claims["exp"] + self.contract["verifier_clock_skew_seconds"]):
            return None
        if claims["exp"] - claims["iat"] > self.contract["maximum_lifetime_seconds"]:
            return None
        email = claims["verified_email"]
        if not isinstance(email, str) or email != email.strip(" \t\r\n\f\v").lower():
            return None
        return claims


class AtomicConsumptionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.retained_until: dict[tuple[str, str], int] = {}

    def consume(self, nonce: str, jti: str, retained_until: int) -> bool:
        with self._lock:
            key = (nonce, jti)
            if key in self.retained_until:
                return False
            if any(consumed_nonce == nonce for consumed_nonce, _ in self.retained_until):
                return False
            if any(consumed_jti == jti for _, consumed_jti in self.retained_until):
                return False
            self.retained_until[key] = retained_until
            return True


class ShowIdentityScenarioHarness:
    """Executable HTTP/JWT/JWKS reference boundary, not browser conformance."""

    def __init__(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.scenario = self.contract["closed_loop_scenario"]
        self.backend = self.contract["backend_assertion"]
        self.handshake = self.contract["local_handshake"]
        self.start_contract = self.scenario["start"]
        self.callback_contract = self.scenario["callback"]
        self.cookie_contract = self.handshake["correlation_cookie"]
        self.state_signer = LocalStateSigner()
        self.pairing = PairingRecord(instance_id=self.start_contract["instance_id"])
        self.issuer = BackendRs256Issuer(self.pairing)
        self.jwks_provider = JwksProvider(self.issuer.jwks())
        self.verifier = AvibeJwtVerifier(self.pairing, self.jwks_provider, self.backend)
        self.consumption_store = AtomicConsumptionStore()
        self.issued_credential: dict[str, Any] | None = None
        self.successful_callback_count = 0
        self.http_callback_count = 0
        self.pages = {
            self.start_contract["share_id"]: {
                "instance_id": self.start_contract["instance_id"],
                "page_id": self.start_contract["page_id"],
                "share_id": self.start_contract["share_id"],
                "availability": "active",
                "access_mode": "limited",
                "audience_revision": self.callback_contract["resolved_audience_revision"],
                "emails": list(self.callback_contract["current_local_emails"]),
            }
        }

        self.app = FastAPI()
        self.app.add_api_route("/p/{share_id}/", self._start, methods=["GET"])
        self.app.add_api_route(self.cookie_contract["path"], self._callback, methods=["POST"])
        self.client = TestClient(self.app)

    @staticmethod
    def cookie_name(nonce: str) -> str:
        return f"__Secure-avibe_show_identity_c_{nonce}"

    def add_page(self, page_id: str, share_id: str, emails: list[str]) -> None:
        self.pages[share_id] = {
            "instance_id": self.pairing.instance_id,
            "page_id": page_id,
            "share_id": share_id,
            "availability": "active",
            "access_mode": "limited",
            "audience_revision": 1,
            "emails": emails,
        }

    async def _start(self, share_id: str):
        page = self.pages.get(share_id)
        if page is None:
            return JSONResponse({"error": "show_not_found"}, status_code=404)
        nonce = secrets.token_urlsafe(32)
        cookie_value = secrets.token_urlsafe(32)
        cookie_name = self.cookie_name(nonce)
        callback_hostname = self.start_contract["callback_hostname"]
        callback_url = f"https://{callback_hostname}{self.cookie_contract['path']}"
        payload = {
            "instance_id": page["instance_id"],
            "page_id": page["page_id"],
            "share_id": page["share_id"],
            "safe_public_return_target": f"/p/{page['share_id']}/",
            "callback_hostname": callback_hostname,
            "nonce": nonce,
            "correlation_cookie_sha256": hashlib.sha256(cookie_value.encode()).hexdigest(),
            "issued_at": REFERENCE_NOW,
            "expires_at": REFERENCE_NOW + self.backend["ttl_seconds"],
        }
        response = JSONResponse(
            {
                "authorize_request": {
                    "state": self.state_signer.sign(payload),
                    "nonce": nonce,
                    "redirect_uri": callback_url,
                    "cookie_name": cookie_name,
                    "cookie_value": cookie_value,
                    "page_id": page["page_id"],
                    "share_id": page["share_id"],
                }
            }
        )
        response.set_cookie(
            cookie_name,
            cookie_value,
            max_age=self.backend["ttl_seconds"],
            secure=True,
            httponly=True,
            samesite="none",
            path=self.cookie_contract["path"],
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def begin(self, *, share_id: str | None = None) -> IdentityHandshakeStart:
        selected_share = share_id or self.start_contract["share_id"]
        response = self.client.get(f"https://{self.start_contract['callback_hostname']}/p/{selected_share}/")
        assert response.status_code == 200
        request = response.json()["authorize_request"]
        return IdentityHandshakeStart(
            state=request["state"],
            nonce=request["nonce"],
            callback_url=request["redirect_uri"],
            set_cookie=response.headers["Set-Cookie"],
            cookie_name=request["cookie_name"],
            cookie_value=request["cookie_value"],
            page_id=request["page_id"],
            share_id=request["share_id"],
        )

    def assertion_claims(self, start: IdentityHandshakeStart, *, jti: str | None = None) -> dict[str, Any]:
        return {
            "iss": self.pairing.issuer,
            "aud": self.pairing.audience,
            "sub": "usr_identity_contract",
            "iat": REFERENCE_NOW,
            "exp": REFERENCE_NOW + self.backend["ttl_seconds"],
            "jti": jti or secrets.token_urlsafe(32),
            "nonce": start.nonce,
            "instance_id": self.pairing.instance_id,
            "verified_email": self.callback_contract["verified_email"],
        }

    def issue_assertion(
        self,
        start: IdentityHandshakeStart,
        claims: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        return self.issuer.issue(claims or self.assertion_claims(start), **kwargs)

    def _deny(self, outcome: str = "reject_without_credential_or_page_bytes", *, cookie_name: str | None = None):
        response = JSONResponse({"error": "identity_callback_rejected", "outcome": outcome}, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        if cookie_name is not None:
            response.delete_cookie(
                cookie_name,
                secure=True,
                httponly=True,
                samesite="none",
                path=self.cookie_contract["path"],
            )
        return response

    async def _callback(self, request: Request):
        self.http_callback_count += 1
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if request.url.query or content_type != self.callback_contract["content_type"]:
            return self._deny()
        decoded_form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        if set(decoded_form) != set(self.backend["delivery"]["form_fields"]):
            return self._deny()
        if any(len(values) != 1 for values in decoded_form.values()):
            return self._deny()
        if "assertion=" in request.headers.get("Referer", ""):
            return self._deny()

        state = self.state_signer.verify(decoded_form["state"][0])
        if state is None or set(state) != set(self.handshake["signed_state_fields"]):
            return self._deny()
        cookie_name = self.cookie_name(state["nonce"])
        cookie = request.cookies.get(cookie_name)
        claims = self.verifier.verify(decoded_form["assertion"][0])
        if cookie is None or claims is None:
            return self._deny(cookie_name=cookie_name)
        if request.url.hostname != state["callback_hostname"]:
            return self._deny(cookie_name=cookie_name)
        if request.url.hostname not in self.start_contract["allowed_callback_hostnames"]:
            return self._deny(cookie_name=cookie_name)
        if state["issued_at"] > REFERENCE_NOW or state["expires_at"] < REFERENCE_NOW:
            return self._deny(cookie_name=cookie_name)
        if claims["nonce"] != state["nonce"] or claims["instance_id"] != state["instance_id"]:
            return self._deny(cookie_name=cookie_name)
        if hashlib.sha256(cookie.encode()).hexdigest() != state["correlation_cookie_sha256"]:
            return self._deny(cookie_name=cookie_name)

        retained_until = max(state["expires_at"], claims["exp"]) + self.backend["verifier_clock_skew_seconds"]
        if not self.consumption_store.consume(claims["nonce"], claims["jti"], retained_until):
            return self._deny(cookie_name=cookie_name)

        page = self.pages.get(state["share_id"])
        if page is None:
            return self._deny(cookie_name=cookie_name)
        if (
            page["instance_id"] != state["instance_id"]
            or page["page_id"] != state["page_id"]
            or state["safe_public_return_target"] != f"/p/{page['share_id']}/"
            or page["availability"] != "active"
            or page["access_mode"] != "limited"
        ):
            return self._deny(cookie_name=cookie_name)
        if claims["verified_email"] not in page["emails"]:
            return self._deny("generic_deny_without_page_bytes_or_login_loop", cookie_name=cookie_name)

        self.issued_credential = {
            "instance_id": page["instance_id"],
            "page_id": page["page_id"],
            "share_id": page["share_id"],
            "audience_revision": page["audience_revision"],
            "admitted_normalized_email": claims["verified_email"],
        }
        self.successful_callback_count += 1
        response = RedirectResponse(state["safe_public_return_target"], status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            cookie_name,
            secure=True,
            httponly=True,
            samesite="none",
            path=self.cookie_contract["path"],
        )
        return response

    def form_post(
        self,
        start: IdentityHandshakeStart,
        assertion: str,
        *,
        state: str | None = None,
        base_url: str | None = None,
        path_suffix: str = "",
        referer: str = "https://avibe.bot/show-identity/authorize",
    ):
        callback = urlsplit(start.callback_url)
        return self.client.post(
            f"{base_url or f'{callback.scheme}://{callback.netloc}'}{callback.path}{path_suffix}",
            data={"state": state or start.state, "assertion": assertion},
            headers={"Referer": referer},
            follow_redirects=False,
        )

    def replace_cookie(self, start: IdentityHandshakeStart, value: str | None = None) -> None:
        self.client.cookies.set(
            start.cookie_name,
            value or start.cookie_value,
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
        claims = self.assertion_claims(start)
        if mutation == "reuse_consumed_nonce":
            first = self.form_post(start, self.issue_assertion(start, claims))
            assert first.status_code == 303
            self.replace_cookie(start)
        elif mutation == "replace_assertion_instance_id":
            claims["instance_id"] = "ins_other"
        elif mutation == "replace_signed_return_share":
            payload = self.state_signer.verify(start.state)
            assert payload is not None
            payload["safe_public_return_target"] = "/p/other-share/"
            start = IdentityHandshakeStart(
                self.state_signer.sign(payload),
                start.nonce,
                start.callback_url,
                start.set_cookie,
                start.cookie_name,
                start.cookie_value,
                start.page_id,
                start.share_id,
            )
        elif mutation == "replace_correlation_cookie":
            self.replace_cookie(start, "other-cookie-secret")
        elif mutation == "add_callback_query":
            return self.form_post(start, self.issue_assertion(start, claims), path_suffix="?assertion=leak").json()[
                "outcome"
            ]
        elif mutation == "place_assertion_in_browser_history":
            return self.form_post(start, self.issue_assertion(start, claims), path_suffix="?assertion=history").json()[
                "outcome"
            ]
        elif mutation == "place_assertion_in_referrer":
            return self.form_post(
                start,
                self.issue_assertion(start, claims),
                referer="https://avibe.bot/show-identity/authorize?assertion=leak",
            ).json()["outcome"]
        elif mutation == "replace_callback_hostname":
            return self.form_post(
                start,
                self.issue_assertion(start, claims),
                base_url="https://attacker.example",
            ).json()["outcome"]
        elif mutation == "add_page_authorization_claim":
            claims["page_authorization"] = "limited"
        elif mutation != "reuse_consumed_nonce":
            raise AssertionError(mutation)
        return self.form_post(start, self.issue_assertion(start, claims)).json()["outcome"]
