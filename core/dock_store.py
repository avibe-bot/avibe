"""Server-side Dock state: which apps are *installed*, and which of them sit in
the workbench Dock (and in what order).

The Dock is durable *product* state — it follows the user across devices — not
per-browser UI state, so it lives in the shared ``state_meta`` KV under a single
versioned key, alongside the other cross-device workbench state. It knows two
kinds of item:

- built-in apps, keyed by their app id verbatim (``files`` / ``terminal`` /
  ``editor`` / ``library``). Always installed; individually dockable/undockable.
- pinned Show Pages, keyed ``show:<session_id>``. A pin IS the install record.

Future item kinds (``app:<id>`` …) slot into the same lists without a migration —
see docs/plans/dock-pinned-show-page-apps.md §7 / §7.1c.

Two layers (§7.1c):

- ``pins`` — the *installed* AI pages. Built-ins are implicitly installed.
- ``order`` — the *docked* subset: the resident tiles, in user order. It is a
  SUBSET of the known ids {built-ins ∪ pins}; any known id may be absent
  (undocked), built-ins included, and the empty Dock is a valid state.

The document is *reconciled on read* (dedupe pins, drop unknown/duplicate order
ids — but never force-append, so an undocked tile stays undocked) and *validated
on write* (order is a subset of the known ids, no duplicates, bounded). An absent
document seeds the default of every built-in docked; once any document exists it
is authoritative, so a stale or corrupt blob can never desync the Dock.
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
BUILTIN_DOCK_IDS: tuple[str, ...] = ("files", "terminal", "editor", "library")

# Namespace prefix for a pinned Show Page dock id.
SHOW_PREFIX = "show:"

# Defensive cap so one corrupt/hostile write can't balloon the installed set or
# the order list. ``MAX_PINNED_PAGES`` is the FIXED install budget (not a flat
# constant) so adding a built-in never shrinks it or drops an existing valid pin
# on reconcile; ``MAX_DOCK_ITEMS`` bounds the docked order (≤ built-ins ∪ pins).
# Far above any real Dock.
MAX_PINNED_PAGES = 197
MAX_DOCK_ITEMS = len(BUILTIN_DOCK_IDS) + MAX_PINNED_PAGES


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
    """Canonicalize a dock doc under the two-layer model (§7.1c): dedupe pins by
    session id; drop unknown/duplicate order ids. The order is left as the stored
    SUBSET — built-ins and pins are NOT force-appended, so an undocked tile
    (built-in included) stays undocked and the empty Dock survives a round-trip.

    Dropping an order id whose ``show:<sid>`` has no matching pin keeps the Dock
    pin-consistent (a removed pin cascades out of the order on the next read).

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
    # hold more pins than the write paths admit, so bound them here too (excess
    # pins beyond the fixed install budget are dropped) to keep the installed set —
    # and GET /api/dock — from ballooning. Same constant the pin path guards on, so
    # the two never disagree about what a read would keep.
    if len(deduped_pins) > MAX_PINNED_PAGES:
        deduped_pins = deduped_pins[:MAX_PINNED_PAGES]

    pin_ids = [_show_id(pin["session_id"]) for pin in deduped_pins]
    known = set(BUILTIN_DOCK_IDS) | set(pin_ids)

    result: list[str] = []
    seen: set[str] = set()
    for item in order:
        if item in known and item not in seen:
            result.append(item)
            seen.add(item)
    return {"order": result, "pins": deduped_pins}


def _load(db_path: Path | None) -> dict[str, Any]:
    raw = get_state_meta(DOCK_STATE_KEY, db_path=db_path)
    if not isinstance(raw, dict):
        # No dock document has ever been stored → seed the default: every built-in
        # docked, nothing installed. Only an ABSENT document seeds; a stored doc is
        # honored as-is (including an empty/partial order — built-ins are
        # undockable now, so "nothing docked" is a legitimate saved state).
        return {"order": list(BUILTIN_DOCK_IDS), "pins": []}
    order, pins = _coerce_doc(raw)
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

        # Bound the installed set to the same fixed budget reconcile clamps to, so
        # a new pin can't grow ``pins`` past what a read would keep (which would
        # then silently drop the just-added page).
        if len(doc["pins"]) >= MAX_PINNED_PAGES:
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
    """Persist the docked subset, in order.

    Under the two-layer model (§7.1c) the order is a SUBSET of the known ids
    (built-ins ∪ pinned pages): every id must be known and unique, but ids may be
    OMITTED — that is how a tile (built-in included) is undocked — and the empty
    order (nothing docked) is valid. An id that is not a real dock item is still
    rejected, so a stale client that references a pin removed by another tab
    can't resurrect it. Raises ``DockError`` (``invalid_order`` → 400).
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
        if not set(order) <= known:
            raise DockError("Dock order has an unknown id.", code="invalid_order")

        doc = {"order": list(order), "pins": doc["pins"]}
        _save(doc, db_path)
        return doc
