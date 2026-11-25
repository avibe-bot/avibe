"""Private evidence for released Desktop redemption; never authority to retire a record."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Literal, Mapping

Disposition = Literal["ended_unknown", "expired_unconsumed"]
_OBSERVATION = {"desktop_dispatch", "desktop_reply", "desktop_failure"}


def prepared(claim: Mapping) -> bool:
    return (claim.get("phase") == "prepared" and bool(claim.get("desktop_enrollment"))
            and not any(k in claim for k in ("personal_claim", "pairing", "target_identity", "target_fingerprint",
                                              "redeemed_at", "applied_at", "retirement")))


def begin(claim: Mapping) -> dict:
    """An exact replay keeps its wire request ID, but no previous local reply."""
    current = dict(claim, desktop_dispatch=secrets.token_hex(16))
    current.pop("desktop_reply", None)
    current.pop("desktop_failure", None)
    return current


def _canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def same_request(current: Mapping, sent: Mapping) -> bool:
    """A duplicate waiter is not a new grant: the entire immutable request must still match."""
    def request(record):
        return _canonical({k: v for k, v in record.items() if k not in _OBSERVATION})

    return (prepared(current) and prepared(sent) and bool(sent.get("redeem_request"))
            and request(current) == request(sent))


def receipt(current: Mapping, kind: Disposition, observed_dispatch: str) -> dict:
    # Shipped writers can copy unknown fields on renewal or replace desktop_failure.
    # Bind the result to the complete record and its actual dispatch, not to display
    # text alone. This equality check doesn't authenticate against the OS user.
    record = {k: v for k, v in current.items() if k != "desktop_reply"}
    digest = hashlib.sha256(_canonical([kind, observed_dispatch, record])).hexdigest()
    return {"kind": kind, "observed_dispatch": observed_dispatch, "stamp": digest}


def disposition(claim: Mapping) -> Disposition | None:
    saved = claim.get("desktop_reply")
    if not prepared(claim) or not claim.get("redeem_request") or not isinstance(saved, dict):
        return None
    kind, observed, current = saved.get("kind"), saved.get("observed_dispatch"), claim.get("desktop_dispatch")
    if (kind not in ("ended_unknown", "expired_unconsumed")
            or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{32}", v) for v in (observed, current))
            or (kind == "ended_unknown" and observed != current)):
        return None
    return kind if saved == receipt(claim, kind, observed) else None
