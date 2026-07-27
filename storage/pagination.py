from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Iterable, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


@dataclass(frozen=True)
class PageRequest:
    page: int = 1
    limit: int = DEFAULT_PAGE_LIMIT

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


@dataclass(frozen=True)
class PageResult(Generic[T]):
    items: list[T]
    page: int
    limit: int
    has_more: bool

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.has_more else None


def make_page_request(
    *,
    page: int | None = None,
    limit: int | None = None,
    default_limit: int = DEFAULT_PAGE_LIMIT,
    max_limit: int = MAX_PAGE_LIMIT,
) -> PageRequest:
    resolved_page = page if page is not None else 1
    resolved_limit = limit if limit is not None else default_limit
    if resolved_page < 1:
        raise ValueError("page must be >= 1")
    if resolved_limit < 1:
        raise ValueError("limit must be >= 1")
    if resolved_limit > max_limit:
        raise ValueError(f"limit must be <= {max_limit}")
    return PageRequest(page=resolved_page, limit=resolved_limit)


def page_sequence(items: Sequence[T], page_request: PageRequest | None) -> PageResult[T]:
    if page_request is None:
        values = list(items)
        return PageResult(items=values, page=1, limit=len(values), has_more=False)
    start = page_request.offset
    limit = page_request.limit
    window = list(items[start : start + limit + 1])
    return PageResult(
        items=window[:limit],
        page=page_request.page,
        limit=page_request.limit,
        has_more=len(window) > limit,
    )


def page_limit_plus_one(page_request: PageRequest | None) -> int | None:
    if page_request is None:
        return None
    return page_request.limit + 1


def page_result_from_limit_plus_one(items: Iterable[T], page_request: PageRequest | None) -> PageResult[T]:
    values = list(items)
    if page_request is None:
        return PageResult(items=values, page=1, limit=len(values), has_more=False)
    return PageResult(
        items=values[: page_request.limit],
        page=page_request.page,
        limit=page_request.limit,
        has_more=len(values) > page_request.limit,
    )


def pagination_payload(result: PageResult[object], *, next_command: str | None = None) -> dict:
    payload = {
        "page": result.page,
        "limit": result.limit,
        "returned": len(result.items),
        "has_more": result.has_more,
        "next_page": result.next_page,
    }
    if next_command:
        payload["next_command"] = next_command
    return payload
