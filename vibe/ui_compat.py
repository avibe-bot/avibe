from __future__ import annotations

import contextlib
import functools
import inspect
import json
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response as StarletteResponse
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.datastructures import Headers, QueryParams, URL


TEST_REMOTE_ADDR_HEADER = "x-vibe-test-remote-addr"

_request_var: ContextVar["CompatRequest"] = ContextVar("ui_request")
_g_var: ContextVar[Any] = ContextVar("ui_g")


class _LocalProxy:
    def __init__(self, getter: Callable[[], Any]) -> None:
        object.__setattr__(self, "_getter", getter)

    def _get_current_object(self) -> Any:
        return object.__getattribute__(self, "_getter")()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_current_object(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._get_current_object(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._get_current_object(), name)


class CompatRequest:
    def __init__(self, request: FastAPIRequest, json_payload: Any = None) -> None:
        self._request = request
        self._json_payload = json_payload

    @property
    def method(self) -> str:
        return self._request.method

    @property
    def path(self) -> str:
        return self._request.url.path

    @property
    def args(self) -> QueryParams:
        return self._request.query_params

    @property
    def headers(self) -> Headers:
        return self._request.headers

    @property
    def cookies(self) -> dict[str, str]:
        return self._request.cookies

    @property
    def json(self) -> Any:
        return self._json_payload

    @property
    def host(self) -> str:
        return self._request.headers.get("host", "")

    @property
    def host_url(self) -> str:
        return str(URL(scope=self._request.scope).replace(path="/", query=""))

    @property
    def full_path(self) -> str:
        query = self.query_string.decode("latin-1")
        return f"{self.path}?{query}" if query else self.path

    @property
    def query_string(self) -> bytes:
        return self._request.scope.get("query_string", b"")

    @property
    def remote_addr(self) -> str | None:
        override = self._request.scope.get("vibe_remote_addr")
        if override:
            return str(override)
        client = self._request.client
        return client.host if client else None

    @property
    def is_secure(self) -> bool:
        return self._request.url.scheme == "https"


request = _LocalProxy(lambda: _request_var.get())
g = _LocalProxy(lambda: _g_var.get())


def jsonify(*args: Any, **kwargs: Any) -> JSONResponse:
    if args and kwargs:
        raise TypeError("jsonify() behavior with args and kwargs is unsupported")
    if kwargs:
        content = kwargs
    elif len(args) == 1:
        content = args[0]
    else:
        content = list(args)
    return JSONResponse(content)


def redirect(location: str, code: int = 302) -> RedirectResponse:
    return RedirectResponse(location, status_code=code)


class Response(StarletteResponse):
    def __init__(
        self,
        content: Any = None,
        status_code: int = 200,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
        mimetype: str | None = None,
        background: Any = None,
    ) -> None:
        super().__init__(
            content="" if content is None else content,
            status_code=status if status is not None else status_code,
            headers=headers,
            media_type=mimetype or media_type,
            background=background,
        )


def send_file(path: str | Path, mimetype: str | None = None) -> FileResponse:
    return FileResponse(path, media_type=mimetype)


def route_path_to_regex(path: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    token_re = re.compile(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>")
    for match in token_re.finditer(path):
        parts.append(re.escape(path[index : match.start()]))
        converter, name = match.groups()
        parts.append(rf"(?P<{name}>.*)" if converter == "path" else rf"(?P<{name}>[^/]+)")
        index = match.end()
    parts.append(re.escape(path[index:]))
    return re.compile(rf"^{''.join(parts)}$")


def route_path_to_fastapi(path: str) -> str:
    converted = re.sub(r"<path:([A-Za-z_][A-Za-z0-9_]*)>", r"{\1:path}", path)
    return re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", r"{\1}", converted)


def normalize_response(value: Any) -> Response:
    status_code: int | None = None
    headers: dict[str, str] | None = None
    body = value
    if isinstance(value, tuple):
        body = value[0]
        if len(value) > 1:
            if isinstance(value[1], (dict, list, tuple)):
                headers = dict(value[1])
            else:
                status_code = int(value[1])
        if len(value) > 2:
            headers = dict(value[2])
    if isinstance(body, StarletteResponse):
        if status_code is not None:
            body.status_code = status_code
        if headers:
            for key, val in headers.items():
                body.headers[key] = val
        return body
    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code or 200, headers=headers)
    return Response(
        content="" if body is None else str(body),
        status_code=status_code or 200,
        headers=headers,
    )


def _is_async_callable(obj: Any) -> bool:
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(obj.__call__)
    )


async def run_maybe_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if _is_async_callable(func):
        result = await func(*args, **kwargs)
    else:
        result = await run_in_threadpool(func, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class CompatHeaders:
    def __init__(self, headers: Any) -> None:
        self._headers = headers

    def getlist(self, key: str) -> list[str]:
        if hasattr(self._headers, "getlist"):
            return list(self._headers.getlist(key))
        if hasattr(self._headers, "get_list"):
            return list(self._headers.get_list(key))
        value = self._headers.get(key)
        return [] if value is None else [value]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._headers, name)

    def __getitem__(self, key: str) -> str:
        return self._headers[key]

    def __iter__(self):
        return iter(self._headers)

    def __contains__(self, key: str) -> bool:
        return key in self._headers

    def get(self, key: str, default: Any = None) -> Any:
        return self._headers.get(key, default)


class CompatTestResponse:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.headers = CompatHeaders(response.headers)

    def get_json(self) -> Any:
        return self._response.json()

    @property
    def is_json(self) -> bool:
        return "application/json" in self.headers.get("content-type", "").lower()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class CompatTestClient:
    def __init__(self, app: "CompatApp") -> None:
        self._client = TestClient(app)
        self._lock = threading.Lock()

    def set_cookie(
        self,
        key: str,
        value: str,
        *,
        domain: str = "testserver",
        path: str = "/",
    ) -> None:
        self._client.cookies.set(key, value, domain=domain, path=path)

    def get(self, url: str, **kwargs: Any) -> CompatTestResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> CompatTestResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> CompatTestResponse:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> CompatTestResponse:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> CompatTestResponse:
        return self.request("DELETE", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> CompatTestResponse:
        with self._lock:
            base_url = kwargs.pop("base_url", None)
            environ_base = kwargs.pop("environ_base", None) or {}
            request_url = url
            if base_url:
                request_url = _join_base_url(base_url, url)
            elif not url.startswith(("http://", "https://")):
                request_url = _join_base_url("http://127.0.0.1", url)
            headers = kwargs.pop("headers", None) or {}
            headers = dict(headers)
            if not base_url and "Origin" in headers:
                request_url = _join_base_url(headers["Origin"], url)
            remote_addr = environ_base.get("REMOTE_ADDR") or "127.0.0.1"
            headers[TEST_REMOTE_ADDR_HEADER] = str(remote_addr)
            request_url = _normalize_test_url_for_starlette(request_url, headers)
            kwargs.setdefault("follow_redirects", False)
            response = self._client.request(method, request_url, headers=headers, **kwargs)
            return CompatTestResponse(response)

    def websocket_connect(self, url: str, **kwargs: Any) -> Any:
        return self._client.websocket_connect(url, **kwargs)


def _join_base_url(base_url: str, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    base = base_url.rstrip("/")
    suffix = url if url.startswith("/") else f"/{url}"
    return f"{base}{suffix}"


def _normalize_test_url_for_starlette(url: str, headers: dict[str, str]) -> str:
    parsed = urlsplit(url)
    if parsed.hostname and ":" in parsed.hostname:
        headers.setdefault("Host", parsed.netloc)
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://testserver{parsed.path or '/'}{query}"
    return url


class CompatApp(FastAPI):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.pop("static_folder", None)
        super().__init__(*args, **kwargs)
        self._before_request_handlers: list[Callable[..., Any]] = []
        self._after_request_handlers: list[Callable[..., Any]] = []
        self._error_handlers: list[tuple[type[BaseException], Callable[..., Any]]] = []

    def add_event_handler(self, event_type: str, func: Callable[..., Any]) -> None:
        handler = getattr(super(), "add_event_handler", None)
        if callable(handler):
            handler(event_type, func)
            return
        self.router.add_event_handler(event_type, func)

    def route(
        self,
        path: str,
        methods: list[str] | tuple[str, ...] | None = None,
        defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        methods = tuple(methods or ("GET",))
        fastapi_path = route_path_to_fastapi(path)
        route_regex = route_path_to_regex(path)
        defaults = dict(defaults or {})

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            async def endpoint(starlette_request: FastAPIRequest) -> Response:
                return await self._dispatch_compat_route(
                    starlette_request,
                    func,
                    route_regex,
                    defaults,
                )

            endpoint.__name__ = f"{func.__name__}_compat_endpoint"
            endpoint.__doc__ = func.__doc__
            self.add_api_route(
                fastapi_path,
                endpoint,
                methods=list(methods),
                include_in_schema=kwargs.pop("include_in_schema", False),
                response_model=None,
            )
            return func

        return decorator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            query = scope.get("query_string", b"").decode("latin-1")
            remote_addr = _test_remote_addr_from_scope(scope)
            if "&amp;" in query or remote_addr:
                scope = dict(scope)
                scope["query_string"] = query.replace("&amp;", "&").encode("latin-1")
                if remote_addr:
                    scope["vibe_remote_addr"] = remote_addr
                scope["headers"] = [
                    (key, value)
                    for key, value in scope.get("headers", [])
                    if key != b"x-vibe-test-remote-addr"
                ]
        await super().__call__(scope, receive, send)

    def before_request(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self._before_request_handlers.append(func)
        return func

    def after_request(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self._after_request_handlers.append(func)
        return func

    def errorhandler(self, exc_class: type[BaseException]):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._error_handlers.append((exc_class, func))
            return func

        return decorator

    def test_client(self) -> CompatTestClient:
        return CompatTestClient(self)

    async def dispatch_native_request(
        self,
        starlette_request: FastAPIRequest,
        func: Callable[..., Any],
    ) -> Response:
        """Run a native FastAPI endpoint through the shared UI request hooks.

        New async FastAPI routes can avoid the legacy ``@app.route`` shim while
        still inheriting the remote-access, CSRF, and response hooks registered
        through ``before_request`` / ``after_request`` during the migration.
        """
        remote_addr = _test_remote_addr_from_scope(starlette_request.scope)
        if remote_addr:
            starlette_request.scope["vibe_remote_addr"] = remote_addr
        compat_request = CompatRequest(starlette_request)
        token_request = _request_var.set(compat_request)
        token_g = _g_var.set(type("CompatG", (), {})())
        try:
            response: Response | None = None
            try:
                for before in self._before_request_handlers:
                    result = await run_maybe_async(before)
                    if result is not None:
                        response = normalize_response(result)
                        break
                if response is None:
                    compat_request._json_payload = await _read_json_payload(starlette_request)
                    response = normalize_response(await run_maybe_async(func))
            except Exception as exc:
                response = await self._handle_compat_exception(exc)
            for after in reversed(self._after_request_handlers):
                response = normalize_response(await run_maybe_async(after, response))
            return response
        finally:
            _g_var.reset(token_g)
            _request_var.reset(token_request)

    @contextlib.contextmanager
    def test_request_context(
        self,
        path: str,
        *,
        method: str = "GET",
        base_url: str = "http://testserver",
        headers: dict[str, str] | None = None,
        json: Any = None,
    ):
        scope = _build_scope(method, _join_base_url(base_url, path), headers or {})
        compat_request = CompatRequest(FastAPIRequest(scope), json_payload=json)
        token_request = _request_var.set(compat_request)
        token_g = _g_var.set(type("CompatG", (), {})())
        try:
            yield compat_request
        finally:
            _g_var.reset(token_g)
            _request_var.reset(token_request)

    async def _dispatch_compat_route(
        self,
        starlette_request: FastAPIRequest,
        func: Callable[..., Any],
        route_regex: re.Pattern[str],
        defaults: dict[str, Any],
    ) -> Response:
        remote_addr = _test_remote_addr_from_scope(starlette_request.scope)
        if remote_addr:
            starlette_request.scope["vibe_remote_addr"] = remote_addr
        compat_request = CompatRequest(starlette_request)
        token_request = _request_var.set(compat_request)
        token_g = _g_var.set(type("CompatG", (), {})())
        try:
            response: Response | None = None
            try:
                for before in self._before_request_handlers:
                    result = await run_maybe_async(before)
                    if result is not None:
                        response = normalize_response(result)
                        break
                if response is None:
                    compat_request._json_payload = await _read_json_payload(starlette_request)
                    route_args = defaults | _extract_path_args(route_regex, compat_request.path)
                    response = normalize_response(await run_maybe_async(func, **route_args))
            except Exception as exc:
                response = await self._handle_compat_exception(exc)
            for after in reversed(self._after_request_handlers):
                response = normalize_response(await run_maybe_async(after, response))
            return response
        finally:
            _g_var.reset(token_g)
            _request_var.reset(token_request)

    async def _handle_compat_exception(self, exc: Exception) -> Response:
        for exc_class, handler in reversed(self._error_handlers):
            if isinstance(exc, exc_class):
                return normalize_response(await run_maybe_async(handler, exc))
        raise exc


def _extract_path_args(route_regex: re.Pattern[str], path: str) -> dict[str, str]:
    match = route_regex.match(path)
    return match.groupdict() if match else {}


def _test_remote_addr_from_scope(scope: Any) -> str | None:
    client = scope.get("client")
    if not isinstance(client, tuple) or client[:1] != ("testclient",):
        return None
    return next(
        (
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key == TEST_REMOTE_ADDR_HEADER.encode("latin-1")
        ),
        None,
    )


async def _read_json_payload(request: FastAPIRequest) -> Any:
    content_type = request.headers.get("content-type", "")
    if not _is_json_content_type(content_type):
        return None
    body = await request.body()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON")


def _is_json_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _build_scope(method: str, url: str, headers: dict[str, str]) -> dict[str, Any]:
    parsed = urlsplit(url)
    raw_headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()]
    if not any(key == b"host" for key, _ in raw_headers):
        raw_headers.append((b"host", parsed.netloc.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "scheme": parsed.scheme or "http",
        "path": parsed.path or "/",
        "raw_path": (parsed.path or "/").encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": raw_headers,
        "server": (parsed.hostname or "testserver", parsed.port or (443 if parsed.scheme == "https" else 80)),
        "client": ("testclient", 50000),
    }
