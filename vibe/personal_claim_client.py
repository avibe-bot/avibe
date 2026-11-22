"""Verifier-side personal claims. The account's read/grant requests belong to avibe.bot."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from vibe import remote_access


class ClaimError(Exception):
    """Only local, allowlisted codes may cross the caller boundary."""

    def __init__(self, code: str, retry_after: int = 0, *, claim_id=None, expires_at=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.claim_id, self.expires_at = claim_id, expires_at


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def s256(value: str) -> str:
    return _b64(hashlib.sha256(value.encode("ascii")).digest())


def return_proof(state: str, claim_id: str) -> str:
    return _b64(hmac.digest(state.encode("ascii"), ("avibe-claim-return:v1:" + claim_id).encode("ascii"), "sha256"))


def local_origin(value: str) -> bool:
    """Compare the literal spelling: DNS, noncanonical IPs and URL normalization are not admission."""
    try:
        parts = urlsplit(value)
        host, port = parts.hostname or "", parts.port
        if host == "localhost":
            canonical = host
        else:
            addr = ipaddress.ip_address(host)
            if addr.version == 4:
                if not any(addr in net for net in _V4):
                    return False
                canonical = str(addr)
            elif addr == ipaddress.IPv6Address("::1") or addr in _ULA:
                canonical = f"[{addr.compressed}]"
            else:
                return False
        return "%" not in value and value == "http://" + canonical + (f":{port}" if port not in (80, None) else "")
    except (ValueError, UnicodeError):
        return False


_V4 = tuple(map(ipaddress.IPv4Network, ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_ULA = ipaddress.IPv6Network("fc00::/7")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def text(value, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.strip() == value and value.isprintable()


def _valid_hostname(value: str) -> bool:
    return text(value, 253) and all(_HOST.fullmatch(s) for s in value.split("."))


def _instant(value: str) -> float:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is not None:
            return when.astimezone(timezone.utc).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        pass
    raise ClaimError("invalid_claim_response")


@dataclass(frozen=True)
class Grant:
    subject: str
    email: str
    hostname: str

    @classmethod
    def parse(cls, data: dict, hostname: str) -> Grant:
        account = data.get("account")
        if not (isinstance(account, dict) and text(account.get("subject"), 200) and text(account.get("email"), 320)
                and _valid_hostname(hostname)):
            raise ClaimError("invalid_claim_response")
        return cls(account["subject"], account["email"], hostname)


@dataclass(frozen=True, repr=False)
class Ticket:
    id: str
    code: str
    url: str
    expiry: str
    deadline: float
    interval: int
    ordinary_decision_readback: bool = False


@dataclass(frozen=True, repr=False)
class Issuance:
    instance_id: str
    hostname: str
    key: str
    binding_disposition: str | None = None


@dataclass(frozen=True)
class Pending:
    delay: int


def _retry_after(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value):
        digits = value.lstrip("0")
        return max(1, int(digits)) if len(digits) <= 9 and digits else (1 if not digits else 1_000_000_000)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo:
            return max(1, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, OverflowError, TypeError):
        pass
    return 5


class ClaimsClient:
    def __init__(self, backend_url: str):
        self._target, error = remote_access._normalize_pairing_backend_url(backend_url)
        if self._target is None:
            raise ClaimError("control_plane_unreachable" if error == "pairing_backend_unresolvable" else "invalid_backend", 5)
        self.base = self._target.base_url
        self._verifier = secrets.token_urlsafe(32)

    def _post(self, suffix: str, payload: dict, *, accepting: bool = False):
        """Try each pinned address at connect time. Never duplicate an ambiguous start POST."""
        target = self._target
        url = self.base + "/api/v1/claims" + suffix
        connection = None
        try:
            proxy = remote_access._validated_backend_proxy_url(url)
            if target.requires_proxy and not proxy:
                raise ClaimError("control_plane_unreachable", 5)
            for host in target.connect_hosts:
                try:
                    connection = remote_access._validated_backend_connection(host, target, 20, proxy)
                    connection.connect()
                    break
                except (OSError, http.client.HTTPException):
                    if connection is not None:
                        connection.close()
                        connection = None
            else:
                raise ClaimError("control_plane_unreachable", 5)
            connection.request("POST", urlsplit(url).path, json.dumps(payload).encode(), {
                "Host": target.host_header, "Content-Type": "application/json", "Accept": "application/json",
            })
            response = connection.getresponse()
            status = response.status
            hint = response.getheader("Retry-After") or ""
            # Producer sends positive delta-seconds. Never shorten a long delay; the local deadline wins.
            delay = _retry_after(hint)
            raw = response.read(16385)
        except (OSError, http.client.HTTPException, ValueError, RuntimeError):
            raise ClaimError("control_plane_unreachable", 5) from None
        finally:
            if connection is not None:
                connection.close()
        try:
            data = json.loads(raw) if len(raw) <= 16384 else None
        except (UnicodeError, ValueError, RecursionError):
            data = None
        if status == 429:
            raise ClaimError("rate_limited", delay)
        reason = data.get("error") if isinstance(data, dict) else None
        if status >= 500:
            # Uncertain publication is resolved only by the same accept; spent yields 410.
            raise ClaimError("control_plane_unreachable", delay)
        if status >= 300 or not isinstance(data, dict):
            # Never project arbitrary remote fields or error messages.
            codes = {"taken": "name_taken", "claim_unavailable": "claim_unavailable", "already_redeemed": "fresh_claim_required",
                     "already_started": "already_started", "binding_unavailable": "binding_unavailable", "subject_mismatch": "subject_mismatch"}
            fallback = "fresh_claim_required" if accepting else "invalid_claim_response"
            raise ClaimError(codes.get(reason, fallback) if isinstance(reason, str) else fallback)
        return status, data, delay

    def start(self, installation: str, device: str, mode: str, locale: str, *, origin="", state="") -> Ticket:
        if mode == "desktop" and self.base != "https://avibe.bot":
            raise ClaimError("invalid_backend")  # never send the machine identity to an alternate issuer
        payload = {"installation_id": installation, "device_name": device, "mode": mode, "locale": locale,
                   "code_challenge": s256(self._verifier)}
        if mode == "redirect":
            if not local_origin(origin):
                raise ClaimError("link_required")
            payload.update(return_origin=origin, redirect_state_challenge=s256(state))
        status, d, _ = self._post("", payload)
        try:
            code, ident, expiry, interval = (d[k] for k in ("user_code", "claim_id", "expires_at", "poll_interval_seconds"))
            url = self.base + "/connect/" + code
            if (status == 201 and str(UUID(ident)) == ident and re.fullmatch(r"[A-Z2-9]{10}", code)
                    and d["user_url"] == url and type(interval) is int and 1 <= interval <= 86400):
                return Ticket(ident, code, url, expiry, _instant(expiry), interval, d.get("ordinary_decision_readback") is True)
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        raise ClaimError("invalid_claim_response")

    def observe(self, ticket: Ticket) -> Grant | Pending:
        status, d, delay = self._post("/redeem", self._proof(ticket))
        if status == 202 and d.get("status") == "pending":
            return Pending(delay)
        if status == 200 and d.get("status") == "granted":
            return Grant.parse(d, d.get("hostname"))
        raise ClaimError("invalid_claim_response")

    def decline(self, ticket: Ticket) -> None:
        status, d, _ = self._post("/redeem", {**self._proof(ticket), "decline": True})
        if status != 200 or d.get("status") != "declined":
            raise ClaimError("invalid_claim_response")

    def accept(self, ticket: Ticket, approved: Grant) -> Issuance | Pending:
        status, d, delay = self._post("/redeem", {**self._proof(ticket), "accepted_subject": approved.subject}, accepting=True)
        if status == 202 and d.get("status") == "pending":
            return Pending(delay)
        try:
            instance, pairing = d["instance"], d["pairing"]
            disposition = d.get("binding_disposition")
            if (status == 200 and d.get("status") == "redeemed" and Grant.parse(d, instance["hostname"]) == approved
                    and text(instance["id"], 120) and instance["public_url"] == "https://" + approved.hostname
                    and text(pairing["pairing_key"], 512) and _instant(pairing["expires_at"]) <= ticket.deadline
                    and disposition in (None, "new", "existing")):
                return Issuance(instance["id"], approved.hostname, pairing["pairing_key"], disposition)
        except (KeyError, TypeError, ClaimError):
            pass
        raise ClaimError("fresh_claim_required")

    def _proof(self, ticket: Ticket) -> dict:
        return {"claim_id": ticket.id, "code_verifier": self._verifier}
