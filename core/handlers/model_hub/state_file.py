"""Durable replacement for the Model Hub's private on-disk state documents.

Five collections persist under the state directory — resolution events, source
provenance, credential revocations, OAuth flow bindings, token usage — and every
one of them wants the same compact credential-free JSON encoding. That encoding
is what this module still owns; the atomic replacement underneath it belongs to
``config.atomic_io``, which every other state writer in Avibe shares.

Each of the five used to spell the whole thing out for itself, in the same seven
lines. Which is how one temp file that outlived a failed write was really five
latent orphans in one directory: the writers deliberately swallow ``OSError`` so
metering and event recording cannot be broken by a full disk, and nothing
downstream would ever notice the residue. A bounded ledger whose state directory
grows without bound is not bounded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.atomic_io import write_atomic


def write_state_document(path: Path, payload: Any) -> None:
    """Replace ``path`` with ``payload`` as one compact owner-only JSON document.

    Compact and ``ensure_ascii=False``: these documents are machine-read ledgers
    that routinely carry non-ASCII model and source names, so escaping them buys
    nothing and costs bytes on every append.
    """

    write_atomic(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
