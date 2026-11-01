"""The opt-in desktop startup receipt written by ``vibe start``."""

from __future__ import annotations

import json
import sys
from typing import Literal, TypedDict

DESKTOP_START_RECEIPT_PREFIX = "@avibe-desktop-start:"
DESKTOP_START_RECEIPT_SCHEMA_VERSION: Literal[1] = 1
DESKTOP_START_RECEIPT_MAX_BYTES = 4096


class DesktopStartReceipt(TypedDict, total=False):
    schema_version: Literal[1]
    outcome: Literal["ready", "failed"]
    origin: str
    reason: str


def desktop_start_receipt_line(
    *,
    origin: str,
    outcome: Literal["ready", "failed"],
    reason: str | None = None,
) -> str:
    """Build one bounded receipt frame for the bundled desktop shell."""

    if outcome == "ready" and reason is not None:
        raise ValueError("a ready receipt cannot carry a failure reason")
    if outcome == "failed" and not reason:
        raise ValueError("a failed receipt requires a reason")
    payload: DesktopStartReceipt = {
        "schema_version": DESKTOP_START_RECEIPT_SCHEMA_VERSION,
        "outcome": outcome,
        "origin": origin,
    }
    if reason is not None:
        payload["reason"] = reason
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    frame = DESKTOP_START_RECEIPT_PREFIX.encode("ascii") + encoded + b"\n"
    if len(frame) > DESKTOP_START_RECEIPT_MAX_BYTES:
        raise ValueError("desktop startup receipt exceeds its byte limit")
    return frame.decode("utf-8")


def emit_desktop_start_receipt(
    *,
    origin: str,
    outcome: Literal["ready", "failed"],
    reason: str | None = None,
) -> None:
    """Write and flush one receipt frame without changing normal CLI output."""

    sys.stdout.write(desktop_start_receipt_line(origin=origin, outcome=outcome, reason=reason))
    sys.stdout.flush()
