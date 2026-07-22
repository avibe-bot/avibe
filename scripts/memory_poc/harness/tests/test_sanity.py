from __future__ import annotations

from memory_poc.sanity import _contains_atomic_fact, _contains_owned_items

OWNER_ID = "00000000-0000-4000-8000-000000000001"


def test_sanity_checks_the_pinned_hybrid_atomic_fact_shape() -> None:
    profile = {"profiles": [{"id": "profile-1", "user_id": OWNER_ID, "profile_data": {"language": "Python"}}]}
    episodes = {"episodes": [{"id": "episode-1", "user_id": OWNER_ID}]}
    search = {
        "episodes": [
            {
                "id": "episode-1",
                "user_id": OWNER_ID,
                "atomic_facts": [{"id": "fact-1", "content": "The owner uses Python.", "score": 0.9}],
            }
        ]
    }

    assert _contains_owned_items(profile, key="profiles", owner_id=OWNER_ID)
    assert _contains_owned_items(episodes, key="episodes", owner_id=OWNER_ID)
    assert _contains_atomic_fact(search, owner_id=OWNER_ID, fact_hint="Python")


def test_sanity_does_not_accept_legacy_kind_markers_or_another_owner() -> None:
    unsupported_shape = {"items": [{"kind": "atomic_fact", "content": "Python"}]}
    wrong_owner = {
        "episodes": [
            {
                "user_id": "different-owner",
                "atomic_facts": [{"id": "fact-1", "content": "Python", "score": 0.9}],
            }
        ]
    }

    assert not _contains_atomic_fact(unsupported_shape, owner_id=OWNER_ID, fact_hint="Python")
    assert not _contains_atomic_fact(wrong_owner, owner_id=OWNER_ID, fact_hint="Python")
