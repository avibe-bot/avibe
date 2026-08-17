from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
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


def _strict_json_object(value: bytes) -> dict[str, Any] | None:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_json_member")
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=reject_duplicates)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _origin_from_url(value: str) -> dict[str, Any] | None:
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.hostname is None:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return None
    return {
        "scheme": parsed.scheme.lower(),
        "normalized_host": parsed.hostname.lower(),
        "effective_port": port,
    }


def _origin_url(origin: dict[str, Any]) -> str:
    scheme = origin["scheme"]
    host = origin["normalized_host"]
    port = origin["effective_port"]
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}{'' if port == default_port else f':{port}'}"


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

    @staticmethod
    def authorize_redirect_uri(
        redirect_uri: str,
        server_owned_origins: list[dict[str, Any]],
        callback_path: str,
    ) -> bool:
        try:
            parsed = urlsplit(redirect_uri)
        except (TypeError, ValueError):
            return False
        return bool(
            parsed.scheme == "https"
            and not parsed.query
            and not parsed.fragment
            and parsed.path == callback_path
            and _origin_from_url(redirect_uri) in server_owned_origins
        )

    def rotate(self) -> None:
        self.previous_key = self.current_key
        self.previous_kid = self.current_kid
        self._generation += 1
        self.current_kid = f"show-identity-key-{self._generation}"
        self.current_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def retire_previous(self) -> None:
        self.previous_kid = None
        self.previous_key = None

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

    def issue_raw_json(
        self,
        protected_json: str,
        payload_json: str,
        *,
        key: rsa.RSAPrivateKey | None = None,
    ) -> str:
        signing_input = f"{_encode(protected_json.encode())}.{_encode(payload_json.encode())}".encode()
        signature = (key or self.current_key).sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode()}.{_encode(signature)}"


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
        self.fetch_count = 0

    def fetch(self, *, force: bool) -> dict[str, Any]:
        self.fetch_count += 1
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
        now: Callable[[], int] | None = None,
    ) -> None:
        self.pairing = pairing
        self.provider = provider
        self.contract = contract
        self.now = now or (lambda: REFERENCE_NOW)
        self._keys: dict[str, Any] = {}
        self._fingerprints: dict[str, tuple[str, str]] = {}
        self._refresh_attempted: set[str] = set()
        self._refresh_lock = threading.Lock()
        self._cache_expires_at = 0
        self.verify_call_count = 0
        if not pairing.issuer.startswith("https://"):
            raise ValueError("issuer_not_https")
        expected_jwks_uri = f"{pairing.issuer}/oauth/jwks.json"
        if pairing.jwks_uri != expected_jwks_uri:
            raise ValueError("jwks_not_exact_paired_same_origin_uri")
        self._install(provider.fetch(force=False))
        self._cache_expires_at = self.now() + contract["jwks_verification"]["cache_policy"]["maximum_age_seconds"]

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

    def _refresh_if_needed(self, kid: str) -> bool:
        now = self.now()
        with self._refresh_lock:
            cache_expired = now >= self._cache_expires_at
            unknown_kid = kid not in self._keys
            if not cache_expired and not unknown_kid:
                return True
            if unknown_kid and not cache_expired and kid in self._refresh_attempted:
                return False
            if unknown_kid:
                self._refresh_attempted.add(kid)
            try:
                self._install(self.provider.fetch(force=True))
            except (RuntimeError, ValueError):
                return False
            self._cache_expires_at = now + self.contract["jwks_verification"]["cache_policy"]["maximum_age_seconds"]
            return kid in self._keys

    def _wire_parts(self, token: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        try:
            if (
                not isinstance(token, str)
                or len(token.encode("utf-8")) > self.contract["wire_protocol"]["maximum_compact_token_bytes"]
            ):
                return None
            segments = token.split(".")
            if len(segments) != 3 or any(not segment for segment in segments):
                return None
            header = _strict_json_object(_decode(segments[0]))
            claims = _strict_json_object(_decode(segments[1]))
        except (TypeError, ValueError):
            return None
        if header is None or claims is None:
            return None

        strict = self.contract["wire_protocol"]["strict_json_boundary"]
        if set(header) != set(strict["header_string_limits"]):
            return None
        if set(claims) != set(self.contract["required_signed_claims"]):
            return None
        for field, maximum in strict["header_string_limits"].items():
            value = header[field]
            if not isinstance(value, str) or not value or len(value) > maximum:
                return None
        for field, maximum in strict["payload_string_limits"].items():
            value = claims[field]
            if not isinstance(value, str) or not value or len(value) > maximum:
                return None
        if any(type(claims[field]) is not int for field in strict["numeric_date_fields"]):
            return None
        return header, claims

    def verify(self, token: str) -> dict[str, Any] | None:
        self.verify_call_count += 1
        wire = self._wire_parts(token)
        if wire is None:
            return None
        header, unverified_claims = wire
        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            return None
        kid = header.get("kid")

        if not self._refresh_if_needed(kid):
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
        except (TypeError, ValueError, jwt.PyJWTError):
            return None
        required = self.contract["required_signed_claims"]
        if not isinstance(claims, dict) or set(claims) != set(required) or claims != unverified_claims:
            return None
        if claims["iss"] != self.pairing.issuer or claims["aud"] != self.pairing.audience:
            return None
        if claims["instance_id"] != self.pairing.instance_id:
            return None
        now = self.now()
        if not (claims["iat"] - self.contract["verifier_clock_skew_seconds"] <= now):
            return None
        if not (now <= claims["exp"] + self.contract["verifier_clock_skew_seconds"]):
            return None
        if not (claims["iat"] <= claims["exp"]):
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


class LocalIdentitySessionStore:
    def __init__(self, contract: dict[str, Any], now: Callable[[], int]) -> None:
        self.contract = contract
        self.now = now
        self.store_generation = secrets.token_urlsafe(16)
        self._lock = threading.Lock()
        self.records: dict[str, dict[str, Any]] = {}
        self.lineages: dict[str, dict[str, Any]] = {}

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def rotate(
        self,
        pairing: PairingRecord,
        claims: dict[str, Any],
        callback_origin: dict[str, Any],
        prior_flow: dict[str, Any],
    ) -> str | None:
        with self._lock:
            lineage_id = prior_flow["lineage_id"]
            if prior_flow["prior_token_hash"] is not None:
                candidate = self.lineages.get(prior_flow["lineage_id"])
                if (
                    candidate is None
                    or candidate["current_token_hash"] != prior_flow["prior_token_hash"]
                    or candidate["current_generation"] != prior_flow["lineage_generation"]
                ):
                    return None
            if lineage_id is None:
                lineage_id = uuid.uuid4().hex
                self.lineages[lineage_id] = {"current_generation": 0, "current_token_hash": None}

            lineage = self.lineages[lineage_id]
            old_hash = lineage["current_token_hash"]
            if old_hash is not None:
                self.records.pop(old_hash, None)
            lineage_generation = lineage["current_generation"] + 1
            token = secrets.token_urlsafe(32)
            token_hash = self.token_hash(token)
            issued_at = self.now()
            self.records[token_hash] = {
                "paired_instance_id": pairing.instance_id,
                "issuer": pairing.issuer,
                "subject": claims["sub"],
                "normalized_verified_email": claims["verified_email"],
                "callback_origin": dict(callback_origin),
                "lineage_id": lineage_id,
                "lineage_generation": lineage_generation,
                "store_generation": self.store_generation,
                "issued_at": issued_at,
                "expires_at": issued_at + self.contract["maximum_lifetime_seconds"],
            }
            lineage.update(current_generation=lineage_generation, current_token_hash=token_hash)
            return token

    def _record_is_current(
        self,
        record: dict[str, Any],
        pairing: PairingRecord,
        callback_origin: dict[str, Any],
    ) -> bool:
        lineage = self.lineages.get(record["lineage_id"])
        return bool(
            type(record["issued_at"]) is int
            and type(record["expires_at"]) is int
            and record["issued_at"] <= self.now() <= record["expires_at"]
            and record["paired_instance_id"] == pairing.instance_id
            and record["issuer"] == pairing.issuer
            and record["callback_origin"] == callback_origin
            and record["store_generation"] == self.store_generation
            and lineage is not None
            and record["lineage_generation"] == lineage["current_generation"]
        )

    def validate(
        self,
        token: str | None,
        *,
        pairing: PairingRecord,
        request_origin: dict[str, Any],
    ) -> dict[str, Any] | None:
        if token is None:
            return None
        record = self.records.get(self.token_hash(token))
        if record is None:
            return None
        if not self._record_is_current(record, pairing, request_origin):
            return None
        return record

    def reset(self) -> None:
        with self._lock:
            self.store_generation = secrets.token_urlsafe(16)
            self.records.clear()
            self.lineages.clear()


class ShowIdentityScenarioHarness:
    """Executable HTTP/JWT/JWKS reference boundary, not browser conformance."""

    def __init__(self, callback_origin: dict[str, Any] | None = None) -> None:
        self.now = REFERENCE_NOW
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.scenario = self.contract["closed_loop_scenario"]
        self.backend = self.contract["backend_assertion"]
        self.handshake = self.contract["local_handshake"]
        self.start_contract = self.scenario["start"]
        self.callback_contract = self.scenario["callback"]
        self.cookie_contract = self.handshake["correlation_cookie"]
        self.session_contract = self.handshake["identity_session"]
        self.callback_origin = dict(callback_origin or self.start_contract["callback_origin"])
        self.state_signer = LocalStateSigner()
        self.pairing = PairingRecord(instance_id=self.start_contract["instance_id"])
        self.issuer = BackendRs256Issuer(self.pairing)
        self.jwks_provider = JwksProvider(self.issuer.jwks())
        self.verifier = AvibeJwtVerifier(self.pairing, self.jwks_provider, self.backend, lambda: self.now)
        self.consumption_store = AtomicConsumptionStore()
        self.identity_sessions = LocalIdentitySessionStore(self.session_contract, lambda: self.now)
        self.issued_credential: dict[str, Any] | None = None
        self.successful_callback_count = 0
        self.http_callback_count = 0
        self.cookie_selection_count = 0
        self.page_lookup_count = 0
        self.session_rotation_count = 0
        self.last_callback_cookie_names: set[str] = set()
        self.last_browser_sent_cookie_names: set[str] = set()
        self.flow_prior_sessions: dict[str, dict[str, Any]] = {}
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
        self.app.add_api_route("/p/{share_id}/__identity_session", self._protected_request, methods=["GET"])
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

    async def _start(self, request: Request, share_id: str):
        page = self.pages.get(share_id)
        if page is None:
            return JSONResponse({"error": "show_not_found"}, status_code=404)
        nonce = secrets.token_urlsafe(32)
        cookie_value = secrets.token_urlsafe(32)
        cookie_name = self.cookie_name(nonce)
        callback_url = f"{_origin_url(self.callback_origin)}{self.cookie_contract['path']}"
        session_cookie_name = self.session_contract["cookie"]["name"]
        prior_token = request.cookies.get(session_cookie_name)
        prior_record = self.identity_sessions.validate(
            prior_token,
            pairing=self.pairing,
            request_origin=_origin_from_url(str(request.url)) or {},
        )
        self.flow_prior_sessions[nonce] = {
            "prior_token_hash": self.identity_sessions.token_hash(prior_token)
            if prior_token is not None and prior_record is not None
            else None,
            "lineage_id": prior_record["lineage_id"] if prior_record is not None else None,
            "lineage_generation": prior_record["lineage_generation"] if prior_record is not None else None,
            "expires_at": self.now
            + self.backend["ttl_seconds"]
            + self.handshake["signed_state_lifecycle"]["verifier_clock_skew_seconds"],
        }
        payload = {
            "instance_id": page["instance_id"],
            "page_id": page["page_id"],
            "share_id": page["share_id"],
            "safe_public_return_target": f"/p/{page['share_id']}/",
            "callback_origin": dict(self.callback_origin),
            "nonce": nonce,
            "correlation_cookie_sha256": hashlib.sha256(cookie_value.encode()).hexdigest(),
            "issued_at": self.now,
            "expires_at": self.now + self.backend["ttl_seconds"],
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
            max_age=(
                self.backend["ttl_seconds"] + self.handshake["signed_state_lifecycle"]["verifier_clock_skew_seconds"]
            ),
            secure=True,
            httponly=True,
            samesite=self.cookie_contract["same_site"].lower(),
            path=self.cookie_contract["path"],
        )
        if prior_token is not None and prior_record is None:
            response.delete_cookie(
                session_cookie_name,
                secure=True,
                httponly=True,
                samesite=self.session_contract["cookie"]["same_site"].lower(),
                path="/",
            )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def begin(self, *, share_id: str | None = None, client: TestClient | None = None) -> IdentityHandshakeStart:
        selected_share = share_id or self.start_contract["share_id"]
        response = (client or self.client).get(f"{_origin_url(self.callback_origin)}/p/{selected_share}/")
        assert response.status_code == 200
        request = response.json()["authorize_request"]
        assert self.issuer.authorize_redirect_uri(
            request["redirect_uri"],
            self.start_contract["server_owned_callback_origins"],
            self.cookie_contract["path"],
        )
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
            "iat": self.now,
            "exp": self.now + self.backend["ttl_seconds"],
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
                samesite=self.cookie_contract["same_site"].lower(),
                path=self.cookie_contract["path"],
            )
        return response

    def _state_is_current(self, state: dict[str, Any]) -> bool:
        issued_at = state.get("issued_at")
        expires_at = state.get("expires_at")
        lifecycle = self.handshake["signed_state_lifecycle"]
        if type(issued_at) is not int or type(expires_at) is not int:
            return False
        skew = lifecycle["verifier_clock_skew_seconds"]
        return (
            issued_at <= expires_at
            and expires_at - issued_at <= lifecycle["maximum_lifetime_seconds"]
            and issued_at <= self.now + skew
            and self.now <= expires_at + skew
        )

    async def _protected_request(self, request: Request, share_id: str):
        session_cookie = request.cookies.get(self.session_contract["cookie"]["name"])
        session = self.identity_sessions.validate(
            session_cookie,
            pairing=self.pairing,
            request_origin=_origin_from_url(str(request.url)) or {},
        )
        if session is None:
            return self._deny("identity_required_without_page_bytes")
        page = self.pages.get(share_id)
        if page is None or page["instance_id"] != session["paired_instance_id"]:
            return self._deny("identity_required_without_page_bytes")
        if page["availability"] != "active" or page["access_mode"] != "limited":
            return self._deny("generic_deny_without_page_bytes_or_login_loop")
        if session["normalized_verified_email"] not in page["emails"]:
            return self._deny("generic_deny_without_page_bytes_or_login_loop")
        self.issued_credential = {
            "instance_id": page["instance_id"],
            "page_id": page["page_id"],
            "share_id": page["share_id"],
            "audience_revision": page["audience_revision"],
            "admitted_normalized_email": session["normalized_verified_email"],
        }
        return JSONResponse({"outcome": "serve_shared_after_current_membership"})

    def later_request(
        self,
        share_id: str | None = None,
        *,
        base_url: str | None = None,
        client: TestClient | None = None,
    ):
        selected_share = share_id or self.start_contract["share_id"]
        origin = base_url or _origin_url(self.callback_origin)
        return (client or self.client).get(f"{origin}/p/{selected_share}/__identity_session")

    async def _callback(self, request: Request):
        self.http_callback_count += 1
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if (
            request.url.query
            or request.url.path != self.cookie_contract["path"]
            or content_type != self.callback_contract["content_type"]
        ):
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
        if not self._state_is_current(state):
            nonce = state.get("nonce")
            return self._deny(cookie_name=self.cookie_name(nonce) if isinstance(nonce, str) and nonce else None)
        actual_origin = _origin_from_url(str(request.url))
        trusted_origins = self.start_contract["server_owned_callback_origins"]
        if (
            actual_origin is None
            or state.get("callback_origin") not in trusted_origins
            or actual_origin != state.get("callback_origin")
        ):
            return self._deny()
        cookie_name = self.cookie_name(state["nonce"])
        self.cookie_selection_count += 1
        self.last_callback_cookie_names = set(request.cookies)
        cookie = request.cookies.get(cookie_name)
        claims = self.verifier.verify(decoded_form["assertion"][0])
        if cookie is None or claims is None:
            return self._deny(cookie_name=cookie_name)
        if claims["nonce"] != state["nonce"] or claims["instance_id"] != state["instance_id"]:
            return self._deny(cookie_name=cookie_name)
        if hashlib.sha256(cookie.encode()).hexdigest() != state["correlation_cookie_sha256"]:
            return self._deny(cookie_name=cookie_name)

        session_cookie = self.session_contract["cookie"]
        current_session_token = request.cookies.get(session_cookie["name"])
        current_session_hash = (
            self.identity_sessions.token_hash(current_session_token) if current_session_token is not None else None
        )
        prior_flow = self.flow_prior_sessions.pop(state["nonce"], None)
        if (
            prior_flow is None
            or self.now > prior_flow["expires_at"]
            or current_session_hash != prior_flow["prior_token_hash"]
        ):
            return self._deny(
                "identity_flow_superseded_restart_required",
                cookie_name=cookie_name,
            )

        retained_until = max(state["expires_at"], claims["exp"]) + self.backend["verifier_clock_skew_seconds"]
        if not self.consumption_store.consume(claims["nonce"], claims["jti"], retained_until):
            return self._deny(cookie_name=cookie_name)

        self.page_lookup_count += 1
        page = self.pages.get(state["share_id"])
        if page is None:
            return self._deny(cookie_name=cookie_name)
        if (
            page["instance_id"] != state["instance_id"]
            or page["page_id"] != state["page_id"]
            or state["safe_public_return_target"] != f"/p/{page['share_id']}/"
        ):
            return self._deny(cookie_name=cookie_name)
        identity_session_token = self.identity_sessions.rotate(
            self.pairing,
            claims,
            state["callback_origin"],
            prior_flow,
        )
        if identity_session_token is None:
            return self._deny(
                "identity_flow_superseded_restart_required",
                cookie_name=cookie_name,
            )
        self.session_rotation_count += 1
        self.successful_callback_count += 1
        response = RedirectResponse(state["safe_public_return_target"], status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie(
            cookie_name,
            secure=True,
            httponly=True,
            samesite=self.cookie_contract["same_site"].lower(),
            path=self.cookie_contract["path"],
        )
        response.set_cookie(
            session_cookie["name"],
            identity_session_token,
            max_age=self.session_contract["maximum_lifetime_seconds"],
            secure=True,
            httponly=True,
            samesite="none",
            path="/",
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
        client: TestClient | None = None,
    ):
        callback = urlsplit(start.callback_url)
        selected_client = client or self.client
        request_url = f"{base_url or f'{callback.scheme}://{callback.netloc}'}{callback.path}{path_suffix}"
        request_parts = urlsplit(request_url)
        referer_origin = _origin_from_url(referer)
        request_origin = _origin_from_url(request_url)
        cross_site = referer_origin is not None and request_origin is not None and referer_origin != request_origin
        sent_cookies: list[str] = []
        sent_names: set[str] = set()
        for cookie in selected_client.cookies.jar:
            domain_matches = request_parts.hostname == cookie.domain.lstrip(".")
            path_matches = request_parts.path.startswith(cookie.path)
            secure_matches = not cookie.secure or request_parts.scheme == "https"
            session_blocked = (
                cross_site
                and cookie.name == self.session_contract["cookie"]["name"]
                and self.session_contract["cookie"]["same_site"].lower() != "none"
            )
            if domain_matches and path_matches and secure_matches and not session_blocked:
                sent_cookies.append(f"{cookie.name}={cookie.value}")
                sent_names.add(cookie.name)
        self.last_browser_sent_cookie_names = sent_names
        return selected_client.post(
            request_url,
            data={"state": state or start.state, "assertion": assertion},
            headers={"Referer": referer, "Cookie": "; ".join(sent_cookies)},
            follow_redirects=False,
        )

    def replace_cookie(
        self,
        start: IdentityHandshakeStart,
        value: str | None = None,
        *,
        client: TestClient | None = None,
    ) -> None:
        (client or self.client).cookies.set(
            start.cookie_name,
            value or start.cookie_value,
            domain=self.callback_origin["normalized_host"],
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
        elif mutation == "replace_callback_host":
            return self.form_post(
                start,
                self.issue_assertion(start, claims),
                base_url="https://attacker.example",
            ).json()["outcome"]
        elif mutation == "replace_callback_port":
            response = self.form_post(
                start, self.issue_assertion(start, claims), base_url="https://show.example.test:8443"
            )
            return response.json()["outcome"]
        elif mutation == "replace_callback_scheme":
            response = self.form_post(start, self.issue_assertion(start, claims), base_url="http://show.example.test")
            return response.json()["outcome"]
        elif mutation == "replace_callback_path":
            response = self.form_post(start, self.issue_assertion(start, claims), path_suffix="/other")
            assert response.status_code == 404
            return "reject_without_credential_or_page_bytes"
        elif mutation == "add_page_authorization_claim":
            claims["page_authorization"] = "limited"
        elif mutation != "reuse_consumed_nonce":
            raise AssertionError(mutation)
        return self.form_post(start, self.issue_assertion(start, claims)).json()["outcome"]
