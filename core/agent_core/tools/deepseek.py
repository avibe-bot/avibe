"""DeepSeek's standalone Search and Fetch APIs as local function tools.

The APIs are model-independent: ``POST {base}/v1/tools/search`` and
``POST {base}/v1/tools/fetch``. That makes them useful to a Vibey Turn on any
model, including a local model whose provider exposes no native web search, and
they cost one request rather than a model call.

The DeepSeek key is never read here when it lives in Avibe Vault: the request
goes through ``vibe vault fetch``, which attaches the credential at egress and
enforces the secret's ``allowed_hosts``. A key already present in the Turn's
environment is used directly, for a user who configured one that way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Any, Mapping, Optional, Sequence

import httpx
import psutil

from core.agent_core.tools.args import ToolInputError, error_result, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree

DEFAULT_API_BASE = "https://api.deepseek.com"
DEEPSEEK_TOOLS_ENV = "AVIBE_DEEPSEEK_SEARCH"
DEFAULT_VAULT_SECRET = "DEEPSEEK_API_KEY"
TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_QUERY_CHARS = 128
MAX_ORI_QUESTION_CHARS = 128
MAX_URL_CHARS = 8192
MAX_CONTENT_CHARS = 20_000
MAX_TOTAL_CONTENT_CHARS = 100_000
MAX_COUNT = 50
MAX_OUTPUT_CHARS = 60_000
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
)
logger = logging.getLogger(__name__)

SEARCH_DESCRIPTION = (
    "Search the live web through DeepSeek's standalone Search API. Use this when the current model has no native "
    "web search, or when its native search is unavailable. Returns ranked results with title, URL, snippet, "
    "summary, publication date, and authority. Supports query operators such as site:, lang:, after:, before:, "
    "quoted phrases, and -term."
)

FETCH_DESCRIPTION = (
    "Fetch one web page through DeepSeek's standalone Fetch API and return its title and content as Markdown."
)

SEARCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query, 1-128 characters."},
        "count": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "default": 5},
        "need_content": {"type": "boolean", "default": False},
        "ori_question": {
            "type": "string",
            "description": "The user's original question, 1-128 characters, to improve query understanding.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

FETCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "HTTP or HTTPS URL, up to 8192 characters."}},
    "required": ["url"],
    "additionalProperties": False,
}


def deepseek_tools_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether Vibey should offer the DeepSeek tools (opt-in)."""
    value = (os.environ if env is None else env).get(DEEPSEEK_TOOLS_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _clean(value: object) -> str:
    """Text safe to encode: a lone surrogate becomes U+FFFD, never a crash."""
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace").decode("utf-8")


def _redact(value: object, secret: Optional[str] = None) -> str:
    """Error text with a credential removed, even if the provider echoes it."""
    text = _clean(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _api_base(env: Mapping[str, str]) -> str:
    return (env.get("DEEPSEEK_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _vault_secret(env: Mapping[str, str]) -> str:
    return env.get("DEEPSEEK_VAULT_SECRET") or DEFAULT_VAULT_SECRET


def _error_from_body(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _redact(body.decode("utf-8", "replace").strip())[:500]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = _redact(error.get("message")).strip()
        code = _redact(error.get("code")).strip()
        if message:
            return f"{message} (code={code})" if code else message
    if isinstance(error, str):
        return _redact(error)
    return _redact(json.dumps(parsed, ensure_ascii=True))[:500]


def _http_error_message(status: int, body: bytes, secret: Optional[str] = None) -> str:
    detail = _redact(_error_from_body(body), secret) or "no error message"
    hints = {
        301: " Redirects are refused so the API key is never sent to another host.",
        302: " Redirects are refused so the API key is never sent to another host.",
        303: " Redirects are refused so the API key is never sent to another host.",
        307: " Redirects are refused so the API key is never sent to another host.",
        308: " Redirects are refused so the API key is never sent to another host.",
        401: " Check the DeepSeek API key.",
        402: " The DeepSeek account has insufficient balance.",
        403: " The request was rejected by the security policy.",
        404: " Fetch could not retrieve usable page content.",
        429: " The Search/Fetch QPS limit was exceeded; retry after a short wait.",
        500: " DeepSeek had an internal error; retry later.",
        504: " DeepSeek timed out downstream; retry later.",
    }
    return f"DeepSeek API HTTP {status}: {detail}.{hints.get(status, '')}".rstrip()


class _ToolError(ValueError):
    """A failure with a message safe to show to the model."""


async def _post_direct(url: str, payload: Mapping[str, Any], key: str) -> Any:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "avibe-deepseek-search/1.0",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=TIMEOUT_SECONDS) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = bytearray()
                    limit = 64 * 1024
                    async for chunk in response.aiter_bytes():
                        remaining = limit - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
                        if len(body) >= limit:
                            break
                    raise _ToolError(_http_error_message(response.status_code, bytes(body), key))
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _ToolError("DeepSeek API response exceeded the size limit")
    except httpx.TimeoutException:
        raise _ToolError(f"DeepSeek API timed out after {TIMEOUT_SECONDS:g}s") from None
    except httpx.HTTPError as error:
        raise _ToolError(f"DeepSeek API is unreachable: {type(error).__name__}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ToolError("DeepSeek API returned a response that is not JSON") from None


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                children = []
            for child in children:
                try:
                    child.kill()
                except psutil.Error:
                    pass
            signal_process_tree(process, KILL_SIGNAL, logger, "DeepSeek search")
        await process.wait()


async def _post_via_vault(url: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> Any:
    body = json.dumps(payload, ensure_ascii=True)
    command = [
        sys.executable,
        "-m",
        "vibe",
        "vault",
        "fetch",
        "--auth",
        _vault_secret(env),
        "--method",
        "POST",
        "--url",
        url,
        "--header",
        "Content-Type: application/json",
        "--data",
        body,
        "--no-approval-wait",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **dict(env)},
            **isolated_subprocess_kwargs(console=False),
        )
    except FileNotFoundError:
        raise _ToolError("the Avibe CLI is not available to run the Vault request") from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS + 15)
    except asyncio.TimeoutError:
        raise _ToolError(f"DeepSeek API timed out after {TIMEOUT_SECONDS:g}s") from None
    except asyncio.CancelledError:
        raise
    finally:
        await _stop(process)
    if process.returncode != 0:
        detail = _error_from_body(stdout) if stdout.strip() else ""
        if not detail:
            lines = stderr.decode("utf-8", "replace").strip().splitlines()
            detail = _clean(lines[-1]) if lines else "no error message"
        raise _ToolError(f"DeepSeek API via Vault failed: {detail}")
    if len(stdout) > MAX_RESPONSE_BYTES:
        raise _ToolError("DeepSeek API response exceeded the size limit")
    try:
        return json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ToolError("DeepSeek API returned a response that is not JSON") from None


async def _post(path: str, payload: Mapping[str, Any], env: Mapping[str, str]) -> Any:
    url = f"{_api_base(env)}{path}"
    key = env.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return await _post_direct(url, payload, key)
    return await _post_via_vault(url, payload, env)


def _search_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    query = _clean(str_arg(arguments, "query")).strip()
    if not query:
        raise ToolInputError("query must not be empty")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolInputError(f"query must be at most {MAX_QUERY_CHARS} characters")
    count = arguments.get("count", 5)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise ToolInputError(f"count must be an integer between 1 and {MAX_COUNT}")
    need_content = arguments.get("need_content", False)
    if not isinstance(need_content, bool):
        raise ToolInputError("need_content must be a boolean")
    payload: dict[str, Any] = {"query": query, "count": count, "need_content": need_content}
    original = arguments.get("ori_question")
    if original is not None:
        if not isinstance(original, str) or not original.strip():
            raise ToolInputError("ori_question must be a non-empty string when provided")
        original = _clean(original).strip()
        if len(original) > MAX_ORI_QUESTION_CHARS:
            raise ToolInputError(f"ori_question must be at most {MAX_ORI_QUESTION_CHARS} characters")
        payload["ori_question"] = original
    return payload


def _fetch_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    url = _clean(str_arg(arguments, "url")).strip()
    if not url:
        raise ToolInputError("url must not be empty")
    if len(url) > MAX_URL_CHARS:
        raise ToolInputError(f"url must be at most {MAX_URL_CHARS} characters")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolInputError("url must be an absolute HTTP or HTTPS URL with a host")
    if parsed.username or parsed.password:
        raise ToolInputError("url must not contain a username or password")
    return {"url": url}


def _search_results(body: Any) -> list[dict[str, Any]]:
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        raise _ToolError("DeepSeek Search returned no results array")
    normalized: list[dict[str, Any]] = []
    budget = MAX_TOTAL_CONTENT_CHARS
    for item in results:
        if not isinstance(item, dict):
            continue
        result = {
            key: item.get(key)
            for key in ("rerank_score", "title", "url", "snippet", "summary", "site_name", "date", "authority")
        }
        for key in ("title", "url", "snippet", "summary", "site_name", "date"):
            if isinstance(result.get(key), str):
                result[key] = _clean(result[key])
        content = _clean(item.get("content"))
        if content and budget > 0:
            allowed = min(MAX_CONTENT_CHARS, budget)
            result["content"] = content[:allowed]
            if len(content) > allowed:
                result["content_truncated"] = True
            budget -= len(result["content"])
        elif content:
            result["content_truncated"] = True
        normalized.append(result)
    return normalized


def _search_text(results: Sequence[Mapping[str, Any]]) -> str:
    if not results:
        return "No results found."
    blocks: list[str] = []
    header = f"Found {len(results)} result(s):"
    used = len(header)
    omitted = 0
    for index, item in enumerate(results, 1):
        lines = [
            f"\n{index}. {_clean(item.get('title')) or '(untitled)'}\n   URL: {_clean(item.get('url'))}"
        ]
        for key in ("snippet", "summary"):
            if item.get(key):
                lines.append(f"   {key.capitalize()}: {_clean(item[key])}")
        facts = [
            f"{key}={item[key]}"
            for key in ("site_name", "date", "authority", "rerank_score")
            if item.get(key) is not None
        ]
        if facts:
            lines.append(f"   {' · '.join(facts)}")
        if item.get("content"):
            suffix = " (truncated)" if item.get("content_truncated") else ""
            lines.append(f"   Content{suffix}:\n{_clean(item['content'])}")
        block = "\n".join(lines)
        if used + len(block) > MAX_OUTPUT_CHARS:
            omitted = len(results) - index + 1
            break
        blocks.append(block)
        used += len(block)
    body = "".join(blocks)
    suffix = f"\n\n[output truncated: {omitted} more result(s) omitted]" if omitted else ""
    text = f"{header}{body}{suffix}"
    if len(text) > MAX_OUTPUT_CHARS:
        marker = "\n\n[output truncated]"
        text = text[: max(0, MAX_OUTPUT_CHARS - len(marker))] + marker
    return text


class DeepSeekSearchTool:
    """DeepSeek's standalone Search API as one local function tool."""

    concurrent = True

    def __init__(self) -> None:
        self._spec = ToolSpec("deepseek_web_search", SEARCH_DESCRIPTION, SEARCH_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            payload = _search_arguments(arguments)
            body = await _post("/v1/tools/search", payload, ctx.env)
            return text_result(_search_text(_search_results(body)))
        except (ToolInputError, _ToolError) as error:
            return error_result(str(error))


class DeepSeekFetchTool:
    """DeepSeek's standalone Fetch API as one local function tool."""

    concurrent = True

    def __init__(self) -> None:
        self._spec = ToolSpec("deepseek_fetch", FETCH_DESCRIPTION, FETCH_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            payload = _fetch_arguments(arguments)
            body = await _post("/v1/tools/fetch", payload, ctx.env)
            if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not body["content"].strip():
                raise _ToolError("DeepSeek Fetch returned no page content")
            title = _clean(body.get("title")) or "(untitled)"
            content = _clean(body["content"])
            suffix = " (truncated)" if len(content) > MAX_CONTENT_CHARS else ""
            return text_result(f"# {title}\n\n{content[:MAX_CONTENT_CHARS]}{suffix}")
        except (ToolInputError, _ToolError) as error:
            return error_result(str(error))