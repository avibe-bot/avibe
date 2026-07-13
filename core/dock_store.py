"""Server-side Dock state: which apps are pinned to the workbench Dock, and in
what order the resident tiles sit.

The Dock is durable *product* state — it follows the user across devices — not
per-browser UI state, so it lives in the shared ``state_meta`` KV under a single
versioned key, alongside the other cross-device workbench state. v1 knows two
kinds of dock item:

- built-in apps, keyed by their app id verbatim (``files`` / ``terminal`` /
  ``editor``). They are reorderable but not unpinnable.
- pinned Show Pages, keyed ``show:<session_id>``.

Future item kinds (``app:<id>`` …) slot into the same ``order`` list without a
migration — see docs/plans/dock-pinned-show-page-apps.md §7.

``order`` covers every resident tile, built-ins included. The document is
*reconciled on read* (drop unknown ids, append any missing built-ins/pins) and
*validated on write* (order must be exactly the known id set, no duplicates,
bounded), so a stale or corrupt blob can never desync the Dock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.chat_discovery import get_state_meta, set_state_meta
from core.show_pages import ShowPageStore, validate_session_id
from storage.sessions_service import read_session_display_meta

# The single state_meta key holding the whole dock document.
DOCK_STATE_KEY = "workbench.dock.v1"

# Every Dock mutation funnels through the single local vibe process — multiple
# devices/tabs are multiple clients of ONE server — so a module lock makes each
# read-modify-write of the document atomic. Without it, two near-simultaneous
# pins both load the same doc and the later ``_save`` silently drops the other's
# pin. This matches the whole ``state_meta`` layer's single-process assumption;
# it does not (and need not here) guard against multiple OS processes.
_DOCK_MUTATION_LOCK = threading.Lock()

# Built-in resident apps, in their canonical Dock order. Mirrors the frontend
# APP_LIST ids (ui/src/apps/registry.tsx); these ids are a stable contract
# shared across the client/server boundary — keep the two in sync.
BUILTIN_DOCK_IDS: tuple[str, ...] = ("files", "terminal", "editor")

# Namespace prefix for a pinned Show Page dock id.
SHOW_PREFIX = "show:"

# Defensive cap on the resident-tile count so one corrupt/hostile write can't
# balloon the order list. Far above any real Dock (built-ins + pinned pages).
MAX_DOCK_ITEMS = 200


class DockError(ValueError):
    """A bad Dock request (unknown page to pin, invalid order).

    ``code`` maps to an HTTP status at the route layer: ``*_not_found`` → 404,
    everything else → 400.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _show_id(session_id: str) -> str:
    return f"{SHOW_PREFIX}{session_id}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_doc(raw: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Pull a well-typed ``(order, pins)`` out of whatever is stored, tolerating a
    missing key, wrong types, or a corrupt blob (→ empty)."""
    order: list[str] = []
    pins: list[dict[str, str]] = []
    if isinstance(raw, dict):
        raw_order = raw.get("order")
        if isinstance(raw_order, list):
            order = [item for item in raw_order if isinstance(item, str)]
        raw_pins = raw.get("pins")
        if isinstance(raw_pins, list):
            for entry in raw_pins:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("session_id")
                if not isinstance(sid, str) or not sid:
                    continue
                pins.append(
                    {
                        "session_id": sid,
                        "title_snapshot": str(entry.get("title_snapshot") or ""),
                        "pinned_at": str(entry.get("pinned_at") or ""),
                    }
                )
    return order, pins


def _reconcile(order: list[str], pins: list[dict[str, str]]) -> dict[str, Any]:
    """Canonicalize a dock doc: dedupe pins by session id; drop unknown/duplicate
    order ids; append any missing built-ins, then any missing pins, at the end.

    Pure — the same algorithm runs client-side (reconcileDock in DockContext),
    so both ends agree on the canonical shape.
    """
    seen_pins: set[str] = set()
    deduped_pins: list[dict[str, str]] = []
    for pin in pins:
        sid = pin["session_id"]
        if sid in seen_pins:
            continue
        seen_pins.add(sid)
        deduped_pins.append(pin)

    # Clamp on read as well as on write: a corrupt or hand-edited stored doc could
    # hold more pins than the write paths admit, so bound them here too (built-ins
    # are always kept; excess pins beyond the cap are dropped) to keep the Dock —
    # and GET /api/dock — from ballooning.
    max_pins = max(0, MAX_DOCK_ITEMS - len(BUILTIN_DOCK_IDS))
    if len(deduped_pins) > max_pins:
        deduped_pins = deduped_pins[:max_pins]

    pin_ids = [_show_id(pin["session_id"]) for pin in deduped_pins]
    known = set(BUILTIN_DOCK_IDS) | set(pin_ids)

    result: list[str] = []
    seen: set[str] = set()
    for item in order:
        if item in known and item not in seen:
            result.append(item)
            seen.add(item)
    for builtin in BUILTIN_DOCK_IDS:
        if builtin not in seen:
            result.append(builtin)
            seen.add(builtin)
    for pin_id in pin_ids:
        if pin_id not in seen:
            result.append(pin_id)
            seen.add(pin_id)
    return {"order": result, "pins": deduped_pins}


def _load(db_path: Path | None) -> dict[str, Any]:
    order, pins = _coerce_doc(get_state_meta(DOCK_STATE_KEY, db_path=db_path))
    return _reconcile(order, pins)


def _save(doc: dict[str, Any], db_path: Path | None) -> None:
    set_state_meta(DOCK_STATE_KEY, doc, db_path=db_path)


def load_dock(*, db_path: Path | None = None) -> dict[str, Any]:
    """Return the reconciled Dock document ``{order, pins}``."""
    return _load(db_path)


def pin_show_page(session_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """Pin a session's Show Page to the Dock (idempotent).

    Captures the session's current title as ``title_snapshot`` so the tile stays
    labelled even after the session is archived. Raises ``ShowPageError`` for a
    malformed id (→ 400) or ``DockError`` when the session has no Show Page
    (→ 404). Never creates a page — pinning only records an existing one.
    """
    session_id = validate_session_id(session_id)
    store = ShowPageStore(db_path)
    try:
        page = store.get(session_id)
    finally:
        store.close()
    if page is None:
        raise DockError("This session has no Show Page to pin.", code="show_page_not_found")

    # Serialize the whole read-modify-write so a concurrent pin can't lost-update.
    with _DOCK_MUTATION_LOCK:
        doc = _load(db_path)
        if any(pin["session_id"] == session_id for pin in doc["pins"]):
            return doc  # already pinned → idempotent no-op (keeps its place + snapshot)

        # Enforce the same cap ``set_dock_order`` does, so a new pin can't push the
        # order past MAX_DOCK_ITEMS (which would then make every reorder rejected).
        if len(doc["order"]) >= MAX_DOCK_ITEMS:
            raise DockError("The Dock is full — unpin an app before pinning another.", code="dock_full")

        meta = read_session_display_meta([session_id], db_path=db_path)
        title = (meta.get(session_id) or {}).get("title") or ""
        doc["pins"].append(
            {"session_id": session_id, "title_snapshot": title, "pinned_at": _utc_now_iso()}
        )
        doc["order"].append(_show_id(session_id))
        doc = _reconcile(doc["order"], doc["pins"])
        _save(doc, db_path)
        return doc


def unpin_show_page(session_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """Remove a pinned Show Page from the Dock (idempotent; never 404s).

    Unpin is Dock-only — it leaves the Show Page itself, its visibility, and any
    open windows untouched.
    """
    sid = (session_id or "").strip()
    show_id = _show_id(sid)
    with _DOCK_MUTATION_LOCK:
        doc = _load(db_path)
        pinned = any(pin["session_id"] == sid for pin in doc["pins"])
        if not pinned and show_id not in doc["order"]:
            return doc  # nothing to remove → idempotent no-op
        pins = [pin for pin in doc["pins"] if pin["session_id"] != sid]
        order = [item for item in doc["order"] if item != show_id]
        doc = _reconcile(order, pins)
        _save(doc, db_path)
        return doc


def set_dock_order(order: Any, *, db_path: Path | None = None) -> dict[str, Any]:
    """Persist a new resident-tile order.

    The order must be exactly the current known id set (built-ins + pinned
    pages), with no duplicates and within the size cap. A stale client — one
    that omits a pin added by another tab, say — is rejected rather than allowed
    to clobber the newer pin. Raises ``DockError`` (``invalid_order`` → 400).
    """
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        raise DockError("Dock order must be a list of ids.", code="invalid_order")
    if len(order) > MAX_DOCK_ITEMS:
        raise DockError("Dock order is too large.", code="invalid_order")
    if len(order) != len(set(order)):
        raise DockError("Dock order has duplicate ids.", code="invalid_order")

    with _DOCK_MUTATION_LOCK:
        doc = _load(db_path)
        known = set(BUILTIN_DOCK_IDS) | {_show_id(pin["session_id"]) for pin in doc["pins"]}
        if set(order) != known:
            raise DockError("Dock order must match the current dock items.", code="invalid_order")

        doc = {"order": list(order), "pins": doc["pins"]}
        _save(doc, db_path)
        return doc
