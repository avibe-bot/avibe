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
from pathlib import Path
from typing import Any, Mapping

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
_WEB_SEARCH_TOKENS = ('"WebSearch"', '"webSearch"')
_WEB_SEARCH_ITEM_TYPE = "websearch"


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


def read_thread_search_sources(thread_id: str, *, limit: int) -> list[CitationSource]:
    """Web-search results Codex recorded for one thread, oldest first.

    Blocking: callers on an event loop should hand this to a worker thread. At
    most *limit* sources are kept, the most recent ones, and a ref_id seen twice
    resolves to its latest definition - the same last-wins rule the live cache
    applies, so a hydrated thread behaves like one that streamed.
    """
    path = thread_history_path(thread_id)
    if path is None:
        logger.debug("No Codex history file recorded for thread %s", thread_id)
        return []

    harvested: OrderedDict[str, CitationSource] = OrderedDict()
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
                if str(item.get("type") or "").lower() != _WEB_SEARCH_ITEM_TYPE:
                    continue
                for source in harvest_search_results(item):
                    harvested[source.ref_id] = source
                    harvested.move_to_end(source.ref_id)
                    while len(harvested) > limit:
                        harvested.popitem(last=False)
    except OSError as exc:
        # Keep whatever was already read and say why the rest is missing; the
        # unread refs degrade to the unresolved label.
        logger.warning("Could not read Codex citation history %s: %s", path, exc)

    logger.debug(
        "Recovered %d citable source(s) from Codex history for thread %s",
        len(harvested),
        thread_id,
    )
    return list(harvested.values())
