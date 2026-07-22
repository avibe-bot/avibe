"""Frozen synthetic-corpus loading and public-search result matching.

This module deliberately keeps fixture text in memory only. Its callers receive
stable ranks and booleans for reports, never message or query bodies.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .paths import workspace_root


@dataclass(frozen=True)
class CorpusMessage:
    session_key: str
    seq: int
    text: str
    occurred_offset_ms: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class CorpusExpectation:
    kind: str
    session_key: str
    seq_refs: tuple[int, ...]
    text_hint: str


@dataclass(frozen=True)
class CorpusQuery:
    query_id: str
    type: str
    query: str
    expect: CorpusExpectation | None
    forbid: tuple[str, ...]


@dataclass(frozen=True)
class Corpus:
    revision: str
    messages: tuple[CorpusMessage, ...]
    queries: tuple[CorpusQuery, ...]

    def query(self, query_id: str) -> CorpusQuery:
        for item in self.queries:
            if item.query_id == query_id:
                return item
        raise HarnessError("corpus_query_unknown")

    def message(self, session_key: str, seq: int) -> CorpusMessage:
        for item in self.messages:
            if item.session_key == session_key and item.seq == seq:
                return item
        raise HarnessError("corpus_message_unknown")

    def search_hints_for(self, message: CorpusMessage) -> tuple[str, ...]:
        hints: list[str] = []
        for query in self.queries:
            if query.expect is not None and (
                query.expect.session_key == message.session_key and message.seq in query.expect.seq_refs
            ):
                hints.append(query.expect.text_hint)
            if any(_contains(message.text, hint) for hint in query.forbid):
                hints.extend(query.forbid)
        return tuple(dict.fromkeys(hint for hint in hints if hint))


@dataclass(frozen=True)
class SearchItem:
    """One value-bearing item from the public ``/search`` response."""

    kind: str
    text: str
    rank: int
    identity: str = "unavailable"


@dataclass(frozen=True)
class QueryEvaluation:
    passed: bool
    expected_rank: int | None
    forbidden_rank: int | None


def load_corpus(workspace: Path | None = None) -> Corpus:
    """Load and validate the committed, predeclared synthetic corpus."""
    root = workspace or workspace_root()
    corpus_dir = root / "scripts" / "memory_poc" / "corpus"
    try:
        manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
        messages = tuple(_load_message(line) for line in _jsonl(corpus_dir / "sessions.jsonl"))
        queries = tuple(_load_query(line) for line in _jsonl(corpus_dir / "queries.jsonl"))
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise HarnessError("corpus_invalid") from exc
    revision = manifest.get("corpus_revision") if isinstance(manifest, dict) else None
    if not isinstance(revision, str) or not revision:
        raise HarnessError("corpus_invalid")
    if manifest.get("message_count") != len(messages) or manifest.get("query_count") != len(queries):
        raise HarnessError("corpus_count_mismatch")
    if len({(item.session_key, item.seq) for item in messages}) != len(messages):
        raise HarnessError("corpus_message_identity_duplicate")
    if len({item.query_id for item in queries}) != len(queries):
        raise HarnessError("corpus_query_identity_duplicate")
    return Corpus(revision=revision, messages=messages, queries=queries)


def flatten_search_response(value: Any, *, owner_id: str, top_k: int = 8) -> tuple[SearchItem, ...]:
    """Extract ranked episode and nested fact text from a public search body.

    EverOS 1.1.3 returns atomic facts nested under ranked episodes. Facts use
    their parent's rank because the public DTO has no independent top-level rank.
    Profiles are intentionally excluded from scoring: production reads use the
    episode/fact search surface and profile non-retrieval is accepted POC behavior.
    """
    if not isinstance(value, dict) or not isinstance(value.get("episodes"), list):
        return ()
    items: list[SearchItem] = []
    for index, episode in enumerate(value["episodes"][:top_k], start=1):
        if not isinstance(episode, dict) or episode.get("user_id") != owner_id:
            continue
        episode_text = _episode_text(episode)
        if episode_text:
            items.append(
                SearchItem(
                    kind="episode",
                    text=episode_text,
                    rank=index,
                    identity=_public_identity(episode.get("id")),
                )
            )
        facts = episode.get("atomic_facts")
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if isinstance(fact, dict) and isinstance(fact.get("content"), str) and fact["content"]:
                items.append(
                    SearchItem(
                        kind="atomic_fact",
                        text=fact["content"],
                        rank=index,
                        identity=_public_identity(fact.get("id")),
                    )
                )
    return tuple(items)


def evaluate_query(query: CorpusQuery, items: tuple[SearchItem, ...]) -> QueryEvaluation:
    """Apply the frozen CONTRACT matcher to public search results."""
    expected_rank: int | None = None
    if query.expect is not None:
        expected_rank = _first_rank(
            items,
            hint=query.expect.text_hint,
            kind=query.expect.kind,
        )
    forbidden_rank = _first_forbidden_rank(items, query.forbid)
    if query.type == "positive":
        return QueryEvaluation(
            passed=expected_rank is not None,
            expected_rank=expected_rank,
            forbidden_rank=forbidden_rank,
        )
    if query.type == "negative":
        return QueryEvaluation(
            passed=forbidden_rank is None,
            expected_rank=None,
            forbidden_rank=forbidden_rank,
        )
    if query.type == "temporal":
        return QueryEvaluation(
            passed=expected_rank is not None and (forbidden_rank is None or expected_rank < forbidden_rank),
            expected_rank=expected_rank,
            forbidden_rank=forbidden_rank,
        )
    raise HarnessError("corpus_query_type_invalid")


def _jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            raise ValueError("jsonl_row_invalid")
        rows.append(value)
    return tuple(rows)


def _load_message(value: dict[str, Any]) -> CorpusMessage:
    session_key = value["session_key"]
    seq = value["seq"]
    text = value["text"]
    offset = value["occurred_offset_ms"]
    tags = value["tags"]
    if (
        not isinstance(session_key, str)
        or not isinstance(seq, int)
        or isinstance(seq, bool)
        or not isinstance(text, str)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
    ):
        raise ValueError("message_invalid")
    return CorpusMessage(session_key=session_key, seq=seq, text=text, occurred_offset_ms=offset, tags=tuple(tags))


def _load_query(value: dict[str, Any]) -> CorpusQuery:
    query_id = value["query_id"]
    query_type = value["type"]
    query_text = value["query"]
    raw_expect = value.get("expect")
    raw_forbid = value.get("forbid", [])
    if (
        not isinstance(query_id, str)
        or query_type not in {"positive", "negative", "temporal"}
        or not isinstance(query_text, str)
        or not isinstance(raw_forbid, list)
    ):
        raise ValueError("query_invalid")
    expect = _load_expectation(raw_expect) if raw_expect is not None else None
    forbid: list[str] = []
    for item in raw_forbid:
        if not isinstance(item, dict) or not isinstance(item.get("text_hint"), str):
            raise ValueError("query_forbid_invalid")
        forbid.append(item["text_hint"])
    if query_type in {"positive", "temporal"} and expect is None:
        raise ValueError("query_expect_missing")
    if query_type == "negative" and not forbid:
        raise ValueError("query_forbid_missing")
    return CorpusQuery(query_id=query_id, type=query_type, query=query_text, expect=expect, forbid=tuple(forbid))


def _load_expectation(value: Any) -> CorpusExpectation:
    if not isinstance(value, dict):
        raise ValueError("query_expect_invalid")
    kind = value.get("kind")
    session_key = value.get("session_key")
    seq_refs = value.get("seq_refs")
    text_hint = value.get("text_hint")
    if (
        not isinstance(kind, str)
        or not isinstance(session_key, str)
        or not isinstance(seq_refs, list)
        or not seq_refs
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in seq_refs)
        or not isinstance(text_hint, str)
        or not text_hint
    ):
        raise ValueError("query_expect_invalid")
    return CorpusExpectation(kind=kind, session_key=session_key, seq_refs=tuple(seq_refs), text_hint=text_hint)


def _episode_text(episode: dict[str, Any]) -> str:
    values = [episode.get(field) for field in ("summary", "subject", "episode")]
    return "\n".join(value for value in values if isinstance(value, str) and value)


def _public_identity(value: object) -> str:
    """Keep a bounded opaque public id for evidence, never result prose."""
    if isinstance(value, str) and value and len(value) <= 128 and value.isascii():
        if all(character.isalnum() or character in "._-" for character in value):
            return value
    return "unavailable"


def _first_rank(items: tuple[SearchItem, ...], *, hint: str, kind: str | None = None) -> int | None:
    ranks = [item.rank for item in items if (kind is None or item.kind == kind) and _contains(item.text, hint)]
    return min(ranks) if ranks else None


def _first_forbidden_rank(items: tuple[SearchItem, ...], forbidden: tuple[str, ...]) -> int | None:
    ranks = [item.rank for item in items for hint in forbidden if _contains(item.text, hint)]
    return min(ranks) if ranks else None


def _contains(value: str, hint: str) -> bool:
    return _normalise(hint) in _normalise(value)


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()
