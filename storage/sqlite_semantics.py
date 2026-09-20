"""Pure models of SQLite value semantics, shared by both sides of a CAS.

Stdlib-only on purpose ("dependency-neutral"): ``storage`` must never import
``core``, and ``core.failure_notices`` keeps its imports light, so a normalizer
consumed by BOTH — the policy reader that decides an increment/dead-letter and
the storage predicate the guarded write asserts — has to live in a leaf module
with no dependencies of its own. One function, two importers, zero drift.
"""

from __future__ import annotations

import re
from typing import Any

_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)

#: SQLite's CAST skips only ASCII whitespace before an optional sign and digits.
#: Python's ``\s`` also matches unicode spaces, which SQLite reads as 0 — so the
#: class is spelled out. Digits are ASCII only: ``CAST('٣' AS INTEGER)`` is 0.
_CAST_INTEGER_PREFIX = re.compile(r"[ \t\n\r\f\v]*([+-]?)([0-9]*)")


def sqlite_cast_integer(raw: Any) -> int:
    """``CAST(<json_extract result> AS INTEGER)``, modelled exactly.

    ``raw`` is what ``json_extract`` hands back for the stored JSON value —
    ``None`` for null/absent (callers pair this with ``coalesce(..., 0)``), a
    ``bool``/``int``/``float`` for scalars, a ``str`` for JSON text, and a
    ``list``/``dict`` for containers (whose extract is their JSON text, first
    byte ``[``/``{`` — no numeric prefix, so CAST reads 0).

    The semantics that differ from Python's ``int()`` and that this exists for
    (empirically traced against SQLite, and pinned by the executable-oracle
    parity test rather than by this table):

    * numeric-PREFIX parsing: ``'3x'`` → 3, ``'1e100'`` → 1 (the ``e`` ends the
      digits), ``'  +7x'`` → 7, ``'- 5'`` → 0 (space after the sign ends it);
    * truncation toward zero for reals: ``3.9`` → 3, ``-3.9`` → -3;
    * saturation at the signed-64-bit bounds for anything beyond them, in every
      lane — huge JSON integers, ``±1e100``, and digit strings alike.
    """

    if raw is None:
        return 0
    if isinstance(raw, bool):
        return 1 if raw else 0
    if isinstance(raw, int):
        return min(max(raw, _INT64_MIN), _INT64_MAX)
    if isinstance(raw, float):
        if raw != raw:  # NaN — unreachable through JSON, guarded anyway.
            return 0
        if raw >= float(2**63):
            return _INT64_MAX
        if raw < float(_INT64_MIN):
            return _INT64_MIN
        return int(raw)  # int() truncates toward zero, exactly like CAST.
    if isinstance(raw, (list, dict)):
        return 0
    match = _CAST_INTEGER_PREFIX.match(str(raw))
    if match is None or not match.group(2):
        return 0
    return min(max(int(match.group(1) + match.group(2)), _INT64_MIN), _INT64_MAX)
