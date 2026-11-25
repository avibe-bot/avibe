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


# One address, as avibe.bot's ordinary other-account refusal names the account that confirmed it:
# printable, no whitespace, angle brackets or quotes, one ``@`` and a dotted domain.
_EMAIL = re.compile(r'[^\s@<>"]{1,64}@[^\s@<>"]+\.[^\s@<>".]+')


def confirming_email(value) -> str | None:
    if text(value, 254) and _EMAIL.fullmatch(value) and ".." not in value.partition("@")[2]:
        return value
    return None


class OtherAccountRefusal(ClaimError):
    """The ordinary refusal confirmed by another account. ``confirming_account_email`` is that
    account's address (never the Owner's); it goes to this claim's starter only, never to a log."""

    def __init__(self, email: str):
        super().__init__("other_account")
        self.confirming_account_email = email

    def __repr__(self):
        return "OtherAccountRefusal('other_account')"


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


def is_constrained_key(key: str) -> bool:
    return isinstance(key, str) and key.startswith("vrp2_") and _TOKEN32.fullmatch(key[5:]) is not None


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
    _user_code = re.compile(r"[A-Z2-9]{10}", re.ASCII)

    def _start_constraints(self, mode, origin) -> dict:
        return {}

    def _check_start_ack(self, data):
        self._decision_ticket = data.get("claim_id") if data.get("ordinary_decision_readback") is True else None

    def _disposition(self, data, key):
        disposition = data.get("binding_disposition")
        if key.startswith("vrp2_") or disposition not in (None, "new", "existing"):
            raise ClaimError("invalid_claim_response")
        return disposition  # Ordinary issuance: target check only, never a journal discriminator.

    def _check_error(self, suffix, payload, status, data):
        if (status != 409 or suffix != "/redeem" or not self._decision_ticket
                or payload.get("claim_id") != self._decision_ticket or not isinstance(data, dict)):
            return
        proof = {"claim_id": self._decision_ticket, "code_verifier": self._verifier}
        reading = payload == {**proof, "read_decision": True}
        accepting = (text(payload.get("accepted_subject"), 200)
                     and payload == {**proof, "accepted_subject": payload["accepted_subject"]})
        reason = data.get("reason")
        if ((reading or (accepting and reason == "active_pairing_recent"))
                and reason in ("paired_installation_loopback_required", "active_pairing_recent")
                and data == {"error": "binding_unavailable", "reason": reason}):
            raise ClaimError(reason)
        # Only the tagged read carries it, and only with exactly these three fields; anything else
        # is the unexplained refusal. It never offers a reset.
        if (reading and reason == "other_account"
                and set(data) == {"error", "reason", "confirming_account_email"} and data["error"] == "binding_unavailable"):
            email = confirming_email(data["confirming_account_email"])
            if email:
                raise OtherAccountRefusal(email)

    def __init__(self, backend_url: str):
        self._target, error = remote_access._normalize_pairing_backend_url(backend_url)
        if self._target is None:
            raise ClaimError("control_plane_unreachable" if error == "pairing_backend_unresolvable" else "invalid_backend", 5)
        self.base = self._target.base_url
        self._verifier = secrets.token_urlsafe(32)
        self._decision_ticket = None

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
            self._check_error(suffix, payload, status, data)
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
            # Cancelling at avibe.bot may then bring the browser back here (outcome=declined).
            # Older instances do not send it, and avibe.bot keeps sending their browsers to /app.
            payload.update(return_origin=origin, redirect_state_challenge=s256(state), cancel_return=True)
        payload.update(self._start_constraints(mode, origin))
        status, d, _ = self._post("", payload)
        self._check_start_ack(d)
        try:
            code, ident, expiry, interval = (d[k] for k in ("user_code", "claim_id", "expires_at", "poll_interval_seconds"))
            url = self.base + "/connect/" + code
            if (status == 201 and str(UUID(ident)) == ident and self._user_code.fullmatch(code)
                    and d["user_url"] == url and type(interval) is int and 1 <= interval <= 86400):
                return Ticket(ident, code, url, expiry, _instant(expiry), interval, self._decision_ticket == ident)
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        raise ClaimError("invalid_claim_response")

    def observe(self, ticket: Ticket) -> Grant | Pending:
        proof = self._proof(ticket)
        if self._decision_ticket and ticket.id == self._decision_ticket:
            proof["read_decision"] = True
        status, d, delay = self._post("/redeem", proof)
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
            if (status == 200 and d.get("status") == "redeemed" and Grant.parse(d, instance["hostname"]) == approved
                    and text(instance["id"], 120) and instance["public_url"] == "https://" + approved.hostname
                    and text(pairing["pairing_key"], 512) and _instant(pairing["expires_at"]) <= ticket.deadline):
                key = pairing["pairing_key"]
                return Issuance(instance["id"], approved.hostname, key, self._disposition(d, key))
        except (KeyError, TypeError, ClaimError):
            pass
        raise ClaimError("fresh_claim_required")

    def _proof(self, ticket: Ticket) -> dict:
        return {"claim_id": ticket.id, "code_verifier": self._verifier}


_TOKEN32 = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", re.ASCII)
_MASKED = re.compile(r"(?:\*{3}|[a-zA-Z0-9]\*{3}@[a-zA-Z0-9]\*{3})", re.ASCII)


def loopback_origin(value: str) -> bool:
    if not local_origin(value):
        return False
    host = urlsplit(value).hostname
    return host == "localhost" or ipaddress.ip_address(host).is_loopback


class ConfirmedRefusal(ClaimError):
    """Only a proved v2 observation can produce one; by itself this cannot reset anything."""

    def __init__(self, reason, *, owner_hint=None):
        super().__init__("installation_unverified")
        self.reason = reason
        self.owner_hint = owner_hint


class ConstrainedClaimsClient(ClaimsClient):
    _user_code = re.compile(r"RB2-[A-HJ-NP-Z2-9]{10}", re.ASCII)

    def __init__(self, backend_url: str, instance_id: str, instance_secret: str):
        if not backend_url or not instance_id or not instance_secret:
            raise ClaimError("local_binding_unavailable")
        super().__init__(backend_url)
        self._predecessor = {"instance_id": instance_id, "instance_secret": instance_secret}
        self._ticket_id = self._callback_token = None

    def _start_constraints(self, mode, origin):
        if mode != "redirect" or not loopback_origin(origin):
            raise ClaimError("direct_loopback_required")
        return {"predecessor": self._predecessor}

    def _check_start_ack(self, data):
        if data.get("predecessor_constrained") is not True:
            raise ClaimError("constrained_claim_unsupported")

    def start(self, *args, **kwargs):
        if self._ticket_id is not None:
            raise ClaimError("already_started")
        ticket = super().start(*args, **kwargs)
        self._ticket_id = ticket.id
        return ticket

    def confirm_return(self, ticket: Ticket, token: str):
        # Called only after the flow has checked the exact browser cookie, origin and HMAC.
        if (ticket.id != self._ticket_id or self._callback_token is not None or not isinstance(token, str)
                or not _TOKEN32.fullmatch(token)):
            raise ClaimError("invalid_return")
        self._callback_token = token

    def _proof(self, ticket):
        if ticket.id != self._ticket_id or not self._ticket_id:
            raise ClaimError("invalid_return")
        proof = {**super()._proof(ticket), "predecessor_constrained": True}
        if self._callback_token is not None:
            proof["callback_token"] = self._callback_token
        return proof

    def observe(self, ticket):
        if self._callback_token is None:
            raise ClaimError("invalid_return")
        return super().observe(ticket)

    def accept(self, ticket, approved):
        if self._callback_token is None:
            raise ClaimError("invalid_return")
        return super().accept(ticket, approved)

    def _disposition(self, data, key):
        disposition = data.get("binding_disposition")
        if disposition not in ("existing", "new") or not is_constrained_key(key):
            raise ClaimError("invalid_claim_response")
        return disposition

    def _check_error(self, suffix, payload, status, data):
        if not suffix and status == 422:
            raise ClaimError("constrained_claim_unsupported")
        if status != 409 or not isinstance(data, dict) or data.get("error") != "installation_unverified":
            return
        if (suffix == "/redeem" and self._callback_token is not None and self._ticket_id is not None
                and payload == {"claim_id": self._ticket_id, "code_verifier": self._verifier,
                                "predecessor_constrained": True, "callback_token": self._callback_token}):
            base = {"error": "installation_unverified"}
            if data == {**base, "reason": "active_pairing_recent"}:
                raise ConfirmedRefusal("active_pairing_recent")
            reset = {**base, "recovery": "reset_as_new"}
            if data == {**reset, "reason": "predecessor_unrecoverable"}:
                raise ConfirmedRefusal("predecessor_unrecoverable")
            hint = data.get("owner_hint")
            if (isinstance(hint, str) and _MASKED.fullmatch(hint)
                    and data == {**reset, "reason": "other_account", "owner_hint": hint}):
                raise ConfirmedRefusal("other_account", owner_hint=hint)
        raise ClaimError("installation_unverified")
