from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch


def _load_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "background-watch-hook"
        / "scripts"
        / "_github_wait_common.py"
    )
    spec = importlib.util.spec_from_file_location("_github_wait_common", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, payload, *, etag: str | None = None) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"ETag": etag} if etag else {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


def _not_modified() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/example",
        304,
        "Not Modified",
        hdrs=None,
        fp=None,
    )


def test_retry_initial_request_recovers_with_bounded_backoff() -> None:
    module = _load_module()
    attempts = 0

    def _operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("temporary network failure")
        return "ok"

    with patch.object(module.time, "sleep", return_value=None) as sleep:
        result = module.retry_initial_request(
            _operation,
            description="test request",
        )

    assert result.value == "ok"
    assert result.error is None
    assert attempts == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_retry_initial_request_does_not_retry_terminal_failure() -> None:
    module = _load_module()
    attempts = 0
    error = urllib.error.HTTPError(
        "https://api.github.com/example",
        404,
        "Not Found",
        hdrs=None,
        fp=None,
    )

    def _operation() -> None:
        nonlocal attempts
        attempts += 1
        raise error

    with patch.object(module.time, "sleep", return_value=None) as sleep:
        result = module.retry_initial_request(
            _operation,
            description="test request",
        )

    assert result.value is None
    assert result.error is not None
    assert result.error.retryable is False
    assert attempts == 1
    sleep.assert_not_called()


def test_unauthenticated_rate_limit_is_terminal() -> None:
    module = _load_module()
    error = urllib.error.HTTPError(
        "https://api.github.com/example",
        429,
        "Too Many Requests",
        hdrs=None,
        fp=None,
    )

    result = module.github_request(
        lambda: (_ for _ in ()).throw(error),
        unauthenticated=True,
    )

    assert result.error is not None
    assert result.error.retryable is False
    assert result.error.status_code == 429


def test_github_get_without_cache_sends_no_conditional_header() -> None:
    module = _load_module()
    requests: list[object] = []

    def _fake_urlopen(request, timeout=None):
        requests.append(request)
        return _FakeResponse([{"id": 1}], etag='W/"abc"')

    with patch.object(module.urllib.request, "urlopen", side_effect=_fake_urlopen):
        payload = module.github_get("https://api.github.com/example", "token")

    assert payload == [{"id": 1}]
    assert requests[0].get_header("If-none-match") is None


def test_get_token_prefers_gh_token_like_github_cli(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("GH_TOKEN", "preferred")
    monkeypatch.setenv("GITHUB_TOKEN", "stale")
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    assert module.get_token() == "preferred"


def test_github_get_revalidates_with_etag_and_reuses_the_cached_body() -> None:
    module = _load_module()
    cache = module.ResponseCache()
    requests: list[object] = []
    calls = 0

    def _fake_urlopen(request, timeout=None):
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            return _FakeResponse([{"id": 1}], etag='W/"abc"')
        raise _not_modified()

    with patch.object(module.urllib.request, "urlopen", side_effect=_fake_urlopen):
        first = module.github_get("https://api.github.com/example", "token", cache=cache)
        second = module.github_get("https://api.github.com/example", "token", cache=cache)

    assert first == [{"id": 1}]
    # A 304 carries no body, so the caller has to get the previous one back.
    assert second == [{"id": 1}]
    assert requests[1].get_header("If-none-match") == 'W/"abc"'
    assert (cache.downloaded, cache.revalidated) == (1, 1)
    assert "1 revalidated as 304" in cache.summary()


def test_github_get_caches_each_url_separately() -> None:
    module = _load_module()
    cache = module.ResponseCache()

    def _fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/two"):
            return _FakeResponse([{"id": 2}], etag='W/"two"')
        return _FakeResponse([{"id": 1}], etag='W/"one"')

    with patch.object(module.urllib.request, "urlopen", side_effect=_fake_urlopen):
        module.github_get("https://api.github.com/one", "token", cache=cache)
        module.github_get("https://api.github.com/two", "token", cache=cache)

    assert cache.etag_for("https://api.github.com/one") == 'W/"one"'
    assert cache.etag_for("https://api.github.com/two") == 'W/"two"'


def test_github_get_reraises_not_modified_without_a_cached_body() -> None:
    module = _load_module()
    cache = module.ResponseCache()

    with patch.object(module.urllib.request, "urlopen", side_effect=_not_modified()):
        try:
            module.github_get("https://api.github.com/example", "token", cache=cache)
        except urllib.error.HTTPError as err:
            assert err.code == 304
        else:  # pragma: no cover - the call must not succeed
            raise AssertionError("expected the 304 to propagate")

    assert cache.revalidated == 0


def test_github_get_does_not_cache_a_response_without_an_etag() -> None:
    module = _load_module()
    cache = module.ResponseCache()

    with patch.object(
        module.urllib.request,
        "urlopen",
        side_effect=lambda request, timeout=None: _FakeResponse([{"id": 1}]),
    ):
        module.github_get("https://api.github.com/example", "token", cache=cache)

    assert cache.etag_for("https://api.github.com/example") is None


def test_list_paginated_passes_the_cache_to_every_page() -> None:
    module = _load_module()
    cache = module.ResponseCache()
    seen: list[object] = []

    def _fake_github_get(url, token, *, cache=None):
        seen.append(cache)
        return [{"id": 1}]

    with patch.object(module, "github_get", side_effect=_fake_github_get):
        module.list_paginated("https://api.github.com/example", "token", cache=cache)

    assert seen == [cache]


def test_since_param_rewinds_past_the_newest_timestamp() -> None:
    module = _load_module()
    items = [
        {"id": 1, "created_at": "2026-08-04T06:47:10Z", "updated_at": "2026-08-04T06:47:10Z"},
        {"id": 2, "created_at": "2026-08-04T06:47:12Z", "updated_at": "2026-08-04T06:47:12Z"},
    ]

    # Two comments can share the newest second, so the filter has to start before it.
    assert module.since_param(items) == "2026-08-04T06:47:10Z"


def test_since_param_prefers_the_edited_timestamp() -> None:
    module = _load_module()
    items = [{"id": 1, "created_at": "2026-08-04T06:00:00Z", "updated_at": "2026-08-04T09:00:02Z"}]

    assert module.since_param(items) == "2026-08-04T09:00:00Z"


def test_since_param_is_none_without_usable_timestamps() -> None:
    module = _load_module()

    assert module.since_param([]) is None
    assert module.since_param([{"id": 1}]) is None
    assert module.since_param([{"id": 1, "created_at": "not-a-timestamp"}]) is None


def test_later_since_never_rewinds_an_existing_filter() -> None:
    module = _load_module()
    previous = "2026-08-04T12:00:00Z"

    # An empty page means nothing new arrived, not that the cursor should reset.
    assert module.later_since(previous, []) == previous
    assert (
        module.later_since(previous, [{"id": 1, "created_at": "2026-08-01T00:00:00Z"}]) == previous
    )
    assert (
        module.later_since(previous, [{"id": 2, "created_at": "2026-08-04T13:00:02Z"}])
        == "2026-08-04T13:00:00Z"
    )


def test_later_since_starts_from_nothing() -> None:
    module = _load_module()

    assert module.later_since(None, [{"id": 1, "created_at": "2026-08-04T13:00:02Z"}]) == (
        "2026-08-04T13:00:00Z"
    )


def test_response_cache_summary_reports_an_idle_run() -> None:
    module = _load_module()
    cache = module.ResponseCache()

    assert "no GitHub requests" in cache.summary()


def test_get_authenticated_login_still_works_uncached() -> None:
    module = _load_module()

    with patch.object(module, "github_get", return_value={"login": "qiqi"}):
        assert module.get_authenticated_login("token") == "qiqi"
