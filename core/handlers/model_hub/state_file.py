"""Durable replacement for the Model Hub's private on-disk state documents.

Five collections persist under the state directory — resolution events, source
provenance, credential revocations, OAuth flow bindings, token usage — and every
one of them wants the same three things: the parent directory to exist, one
compact credential-free JSON encoding, and a replacement that either lands whole
or leaves the previous document exactly as it was.

Each of them used to spell that out for itself, in the same seven lines. Which is
how one temp file that outlived a failed write was really five latent orphans in
one directory: the writers deliberately swallow ``OSError`` so metering and event
recording cannot be broken by a full disk, and nothing downstream would ever
notice the residue. A bounded ledger whose state directory grows without bound is
not bounded.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

STATE_FILE_MODE = 0o600


def write_state_document(path: Path, payload: Any) -> None:
    """Replace ``path`` with ``payload`` as one compact owner-only JSON document.

    The temporary file is removed unless the replacement itself succeeded, so an
    encoding, ``fsync``, ``chmod``, or rename that raises costs the caller the
    write it was already prepared to lose and nothing else.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    replaced = False
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, STATE_FILE_MODE)
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:
            with suppress(OSError):
                temporary.unlink()
