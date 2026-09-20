"""Citable web-search results, read from Codex's own recorded thread history.

The live ``item/completed`` notification is the cheap path to a thread's search
results, but it is not a complete one. A restarted process starts with an empty
handler; a resume sends ``excludeTurns: True``; ``thread/read`` sends
``includeTurns: False``; ``thread/fork`` returns only the new thread's id. None
of those replay the searches an earlier answer already cited, so a later answer
in the same conversation would lose its attribution. Codex has already written
the results down - every turn is appended to the thread's rollout file, and its
own ``state_5.sqlite`` index maps a thread id to that file - so recovering them
needs no extra search, page fetch, or model call.

Scope is the rollout *file*, deliberately, and not the ``thread_id`` recorded
inside each row. A forked thread's file opens with the parent's rows copied
verbatim, and those copies keep the *parent's* thread_id (measured on a local
fork: 909 inherited rows under the parent id ahead of 403 of the fork's own).
Reading the file therefore inherits exactly the history the fork carries, while
a thread whose file this is not stays invisible - which is what keeps the refs
apart, because a ref_id is only unique inside one thread (measured in a local
corpus: 1286 of 2466 distinct tokens appear in more than one thread's history).

Every failure here is a degradation, never an invention: an unreadable or
missing file logs what went wrong and yields no sources, which leaves the
citation to fall back to its unresolved label.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping

from core.citations import CitationSource
from modules.agents.native_sessions.codex import CodexNativeSessionProvider

logger = logging.getLogger(__name__)

# A thread id reaches the filesystem and a SQL lookup, so it is validated
# before either. Real ids are UUIDs; this also admits the shorter synthetic
# ids used in tests.
SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Rollout files reach hundreds of megabytes (397MB in one local sample), so the
# scan streams and parses only the lines that can possibly carry a search. The
# generic ``read_json_lines`` helper reads a whole file into memory and parses
# every row, which is right for the few-KB previews it was written for and
# wrong here.
# A completed web search has been recorded under two names. Codex moved the
# search tool into an extension namespace, so a current rollout (codex-cli
# 0.154.0) writes ``{"type": "Extension", "kind": "web.search"}`` where an older
# one wrote ``{"type": "WebSearch"}``; the live notification normalizes both to
# ``webSearch``. Both are present in real history - measured on the local corpus:
# 1023 old-form rows carrying results against 267 new-form ones - and every row
# the shipped binary writes from now on is the new form, so reading only one name
# loses attribution on one side of the rename or the other.
_WEB_SEARCH_TOKENS = ('"WebSearch"', '"webSearch"', '"web.search"')
_WEB_SEARCH_ITEM_TYPES = frozenset({"websearch"})
_WEB_SEARCH_EXTENSION_KIND = "web.search"


def is_web_search_item(item: Any) -> bool:
    """True when *item* is one completed web search, in any shape Codex writes."""
    if not isinstance(item, Mapping):
        return False
    if str(item.get("type") or "").lower() in _WEB_SEARCH_ITEM_TYPES:
        return True
    return str(item.get("kind") or "").lower() == _WEB_SEARCH_EXTENSION_KIND


def harvest_search_results(item: Mapping[str, Any]) -> list[CitationSource]:
    """Read one completed web search's citable results.

    Shared by the live notification and the recorded-history reader so the two
    readings of the same payload cannot drift apart. A result missing either a
    ref_id or a URL is skipped rather than repaired: there is nothing to link
    to, and a URL is never invented from a ref_id or from search order.
    """
    results = item.get("results")
    if not isinstance(results, list):
        return []
    harvested: list[CitationSource] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        ref_id = result.get("ref_id") or result.get("refId")
        url = result.get("url")
        if not isinstance(ref_id, str) or not ref_id:
            continue
        if not isinstance(url, str) or not url:
            continue
        title = result.get("title")
        harvested.append(
            CitationSource(
                ref_id=ref_id,
                title=title if isinstance(title, str) else "",
                url=url,
            )
        )
    return harvested


def thread_history_path(thread_id: str) -> Path | None:
    """Locate the rollout file Codex recorded for *thread_id*."""
    if not thread_id or not SAFE_THREAD_ID_RE.fullmatch(thread_id):
        logger.debug("Skipping Codex citation history for unusable thread id")
        return None
    try:
        return CodexNativeSessionProvider().rollout_path(thread_id)
    except Exception as exc:
        logger.warning("Could not locate Codex history for thread %s: %s", thread_id, exc)
        return None


@dataclass(frozen=True)
class HistoryFingerprint:
    """What a thread's rollout file looked like at one moment.

    A rollout file only ever grows, so identity plus length plus mtime is enough
    to tell "the file I already read" from "the file has more in it now". It is
    measured before *and* after a scan: a file Codex appended to mid-read was
    never read whole, and a partial read must not be remembered as a complete
    one.
    """

    path: str
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ThreadSearchRead:
    """One reading of a thread's recorded searches, and how much it proves.

    ``scanned`` says whether the file was walked at all; ``complete`` says the
    walk reached the end of a file that did not change underneath it. Only a
    complete scan licenses a caller to remember that a ref is *absent* - an
    unreadable, unindexed, or still-being-written history has learned nothing,
    and must stay re-readable rather than settle into permanent negative truth.
    """

    sources: dict[str, CitationSource] = field(default_factory=dict)
    fingerprint: HistoryFingerprint | None = None
    scanned: bool = False
    complete: bool = False


@dataclass
class ThreadSearchState:
    """What one process knows about a single thread's citable sources.

    One record rather than parallel maps, because the facts are one lifecycle:
    the sources seen so far, and the refs a *complete* read proved the recorded
    history does not define - each one carrying the fingerprint of the history
    that proved it. A history that grows invalidates the absences it justified,
    so an absence outliving its evidence would turn "not written down yet" into
    permanent negative truth. The proof is per ref and never shared, because a
    read is directed at the refs one message cites: a later scan looking for
    other refs walks the grown file without learning anything about this one, and
    a single "history I have already read" mark would let it vouch for an absence
    it never tested. Both maps are insertion-ordered so the holder can bound them
    least-recently-used; forgetting an entry costs a re-read, never a citation.
    """

    sources: "OrderedDict[str, CitationSource]" = field(default_factory=OrderedDict)
    absent: "OrderedDict[str, HistoryFingerprint]" = field(default_factory=OrderedDict)


def _fingerprint(path: Path) -> HistoryFingerprint:
    """Measure *path*, treating an unstattable file as an absent one."""
    try:
        stat = path.stat()
    except OSError:
        return HistoryFingerprint(path=str(path), exists=False, size=-1, mtime_ns=-1)
    return HistoryFingerprint(
        path=str(path), exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns
    )


def read_thread_search_sources(
    thread_id: str,
    *,
    wanted: Collection[str],
    unchanged: HistoryFingerprint | None = None,
) -> ThreadSearchRead:
    """Find the searches that define *wanted* in one thread's recorded history.

    Blocking: callers on an event loop should hand this to a worker thread. The
    read is directed by the refs a message actually cites, which is what bounds
    it - a rollout file reaches hundreds of megabytes and may define thousands of
    refs, but a message cites a handful, so nothing is kept that was not asked
    for and no bound can discard the very ref being resolved. A ref defined more
    than once resolves to its latest definition, the same last-wins rule the live
    cache applies, so a hydrated thread behaves like one that streamed.

    Pass *unchanged* to skip the walk when the file is still exactly the one that
    fingerprint describes; the result then reports ``scanned=False``.
    """
    if not wanted:
        return ThreadSearchRead(complete=True)

    path = thread_history_path(thread_id)
    if path is None:
        logger.debug("No Codex history file recorded for thread %s", thread_id)
        return ThreadSearchRead()

    before = _fingerprint(path)
    if unchanged is not None and unchanged == before:
        # Same file, same length, same mtime: a rescan could only find what the
        # scan behind that fingerprint already found.
        return ThreadSearchRead(fingerprint=before, complete=True)

    wanted_refs = frozenset(wanted)
    found: dict[str, CitationSource] = {}
    readable = True
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(token in line for token in _WEB_SEARCH_TOKENS):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # Codex may still be appending: the last line can be a
                    # partial write, and a row we cannot parse is not evidence
                    # about the rows around it.
                    continue
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload")
                item = payload.get("item") if isinstance(payload, dict) else None
                if not isinstance(item, dict):
                    continue
                if not is_web_search_item(item):
                    continue
                for source in harvest_search_results(item):
                    if source.ref_id in wanted_refs:
                        found[source.ref_id] = source
    except OSError as exc:
        # Say why the rest is missing; the unread refs degrade to the unresolved
        # label, and the incomplete read below keeps them re-readable.
        readable = False
        logger.warning("Could not read Codex citation history %s: %s", path, exc)

    after = _fingerprint(path)
    complete = readable and after == before
    logger.debug(
        "Recovered %d of %d requested source(s) from Codex history for thread %s (complete=%s)",
        len(found),
        len(wanted_refs),
        thread_id,
        complete,
    )
    return ThreadSearchRead(
        sources=found, fingerprint=after, scanned=True, complete=complete
    )
