"""A single inventory-backed reset request. Vendor identities never leave this worker."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID

from core.handlers.model_hub import reset_cards
from core.handlers.model_hub.quota import QUOTA_FETCH_TIMEOUT_SECONDS, _load_object


CLAUDE = "https://api.anthropic.com/api"
CHATGPT = "https://chatgpt.com/backend-api/wham"
LIMITS = ("five_hour", "seven_day", "seven_day_overage_included")


def binding_key(vendor, identity):
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError
    namespace = "anthropic" if vendor == "anthropic" else "chatgpt"
    return hashlib.sha256((namespace + "\0" + identity).encode()).hexdigest()


def account_key(*, client, auth, vendor, headers):
    if vendor == "anthropic":
        response = client.management_request(
            "POST",
            "/api-call",
            payload=dict(auth_index=auth.auth_index, method="GET", url=CLAUDE + "/oauth/profile", header=headers),
            timeout=QUOTA_FETCH_TIMEOUT_SECONDS,
        )
        if response.get("status_code") != 200:
            raise ValueError
        identity = str(UUID(_load_object(response.get("body"))["organization"]["uuid"]))
    else:
        identity = auth.account_id
    return binding_key(vendor, identity)


def use_once(*, client, auth, vendor, kind, request_id, headers, expected_key):
    """Return only a closed statement and the pre-use count; never replay or raise upstream text."""

    def call(method, url, data=None):
        request = dict(auth_index=auth.auth_index, method=method, url=url, header=headers)
        if data is not None:
            request.update(data=json.dumps(data), header={**headers, "Content-Type": "application/json"})
        reply = client.management_request("POST", "/api-call", payload=request, timeout=QUOTA_FETCH_TIMEOUT_SECONDS)
        if not isinstance(reply, dict) or reply.get("status_code") != 200:
            raise ValueError
        return reply.get("body")

    # Before the write these are all GETs; failure means nothing has been sent.
    try:
        candidates = []
        if vendor == "anthropic":
            body = call("GET", CLAUDE + "/oauth/usage?cedar_ember=1&skip_spend=1")
            inventory = reset_cards.claude_cards(body, datetime.now(timezone.utc), candidates=candidates)
        elif vendor in ("codex", "openai"):
            count = reset_cards.codex_count(call("GET", CHATGPT + "/usage"))
            details = call("GET", CHATGPT + "/rate-limit-reset-credits") if count else None
            inventory = reset_cards.codex_cards(count, details, datetime.now(timezone.utc), candidates=candidates)
        else:
            return {"statement": "no_card"}
        before = next(row["count"] for row in inventory["cards"] if row["kind"] == kind and row["usable"])
        # Exclude control characters, delimiters and engine substitution markers from vendor-provided handles.
        eligible = [
            (end or datetime.max.replace(tzinfo=timezone.utc), handle)
            for name, end, handle in candidates
            if name == kind and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", handle)
        ]
        _, handle = min(eligible)
        if vendor == "anthropic":
            profile = _load_object(call("GET", CLAUDE + "/oauth/profile"))
            organization = str(UUID(profile["organization"]["uuid"]))
            if binding_key(vendor, organization) != expected_key:
                return {"statement": "no_card"}
            url = f"{CLAUDE}/organizations/{organization}/reset_rate_limits"
            data = {"program": "cedar_ember", "grant_id": handle, "request_id": request_id}
        else:
            if binding_key(vendor, auth.account_id) != expected_key:
                return {"statement": "no_card"}
            url = CHATGPT + "/rate-limit-reset-credits/consume"
            data = {"credit_id": handle, "redeem_request_id": request_id}
    except Exception:
        return {"statement": "no_card"}
    try:
        result = _load_object(call("POST", url, data))
        if vendor == "anthropic":
            status, reason = result.get("result"), result.get("reason")
            if reason in ("stamp_indeterminate", "reset_unconfirmed", "claim_unconfirmed"):
                return {"statement": "unknown"}
            if status in ("unavailable", "ineligible", "already_used") and reason in (
                "not_next_grant",
                "authorization_rejected",
                "unknown_grant",
                "no_grant",
                "tenure",
                "grant_id_required",
            ):
                statement = "refused"
            else:
                statement = {
                    "reset": "ack",
                    "not_limited": "not_limited",
                    "cooldown": "cooldown",
                    "ineligible": "refused",
                    "already_used": "refused",
                }.get(status, "unknown")
            cleared = result.get("cleared", [])
            if not isinstance(cleared, list):
                return {"statement": "unknown"}
            cleared = [limit for limit in LIMITS if limit in cleared]
        else:
            code, windows = result.get("code"), result.get("windows_reset")
            credit = result.get("credit")
            redeemed = (
                isinstance(credit, dict) and isinstance(credit.get("redeemed_at"), str) and bool(credit["redeemed_at"])
            )
            if code == "no_credit":
                statement = "unavailable"
            elif code in ("success", "ok", "consumed", "redeemed") and (
                (type(windows) is int and windows > 0) or redeemed
            ):
                statement = "ack"
            else:
                statement = "unknown"
            cleared = []
        return dict(statement=statement, before=before, cleared=cleared)
    except Exception:
        return {"statement": "unknown"}
