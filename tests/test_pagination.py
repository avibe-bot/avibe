from __future__ import annotations

import pytest

from storage.pagination import make_page_request, page_sequence, pagination_payload


def test_page_sequence_uses_limit_plus_one_for_has_more() -> None:
    request = make_page_request(page=2, limit=3)

    result = page_sequence(list(range(8)), request)
    payload = pagination_payload(result, next_command="vibe runs list --page 3 --limit 3")

    assert result.items == [3, 4, 5]
    assert result.has_more is True
    assert result.next_page == 3
    assert payload["returned"] == 3
    assert payload["next_command"] == "vibe runs list --page 3 --limit 3"


def test_make_page_request_validates_bounds() -> None:
    with pytest.raises(ValueError, match="page must be >= 1"):
        make_page_request(page=0)

    with pytest.raises(ValueError, match="limit must be <= 100"):
        make_page_request(limit=101)
