from __future__ import annotations

from memory_poc.corpus import SearchItem, evaluate_query, flatten_search_response, load_corpus


def test_corpus_loads_the_frozen_predeclared_inventory() -> None:
    corpus = load_corpus()

    assert corpus.revision == "2026-07-22.2"
    assert len(corpus.messages) == 31
    assert len(corpus.queries) == 50
    assert {query.type for query in corpus.queries} == {"positive", "negative", "temporal"}


def test_positive_match_requires_the_declared_kind_and_reports_rank() -> None:
    corpus = load_corpus()
    query = corpus.query("q004")
    items = (
        SearchItem(kind="atomic_fact", text="The user prefers TypeScript.", rank=1),
        SearchItem(kind="episode", text="The user prefers TypeScript.", rank=2),
    )

    outcome = evaluate_query(query, items)

    assert outcome.passed is True
    assert outcome.expected_rank == 2
    assert outcome.forbidden_rank is None


def test_temporal_match_rejects_a_superseded_value_that_outranks_the_correction() -> None:
    corpus = load_corpus()
    query = corpus.query("q035")
    items = (
        SearchItem(kind="episode", text="The user selected MongoDB.", rank=1),
        SearchItem(kind="episode", text="The user now uses PostgreSQL.", rank=2),
    )

    outcome = evaluate_query(query, items)

    assert outcome.passed is False
    assert outcome.expected_rank == 2
    assert outcome.forbidden_rank == 1


def test_negative_match_detects_nested_atomic_fact_leakage() -> None:
    corpus = load_corpus()
    query = corpus.query("q040")
    items = (SearchItem(kind="atomic_fact", text="用户养了一只猫。", rank=1),)

    outcome = evaluate_query(query, items)

    assert outcome.passed is False
    assert outcome.expected_rank is None
    assert outcome.forbidden_rank == 1


def test_public_search_items_retain_only_safe_opaque_identities() -> None:
    items = flatten_search_response(
        {
            "episodes": [
                {
                    "id": "episode-123",
                    "user_id": "owner",
                    "summary": "safe result text",
                    "atomic_facts": [
                        {"id": "fact-456", "content": "safe nested fact"},
                        {"id": "not safe identity!", "content": "ignored identity shape"},
                    ],
                }
            ]
        },
        owner_id="owner",
    )

    assert [(item.kind, item.identity) for item in items] == [
        ("episode", "episode-123"),
        ("atomic_fact", "fact-456"),
        ("atomic_fact", "unavailable"),
    ]
