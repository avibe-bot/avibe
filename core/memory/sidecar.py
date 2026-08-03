"""Child-only UDS launcher for the pinned EverOS ASGI factory."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from starlette.requests import Request

from core.memory.artifact import EVEROS_VERSION
from core.memory.everos import MemoryProviderFailure
from core.memory.modality import SUPPORTED_ATTACHMENT_EXTENSIONS
from core.memory.report import ProfileReportGenerator
from core.memory.types import MemoryProfile, MemoryProfileExplicitInfo, MemoryProfileTrait


_MAX_BODY_BYTES = 64 * 1024
_APP_ID = "avibe"
_PRINCIPAL_PATTERN = re.compile(r"u-[0-9a-f]{32}\Z")
_PROJECT_PATTERN = re.compile(r"p-[0-9a-f]{32}\Z")


def serve(uds: Path) -> None:
    if version("everos") != EVEROS_VERSION:
        raise RuntimeError("everos version mismatch")
    if uds.exists() or not uds.parent.is_dir():
        raise RuntimeError("invalid sidecar socket path")
    os.umask(0o077)

    from starlette.responses import JSONResponse
    import uvicorn

    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()
    attachments_root = Path(os.environ["AVIBE_MEMORY_ATTACHMENTS_ROOT"])

    @app.post("/avibe/v1/profile-report")
    async def profile_report(request: Request) -> Any:
        try:
            payload = await request.json()
        except (TypeError, ValueError):
            return JSONResponse(
                {"status": "failed", "error": "memory_provider_response_invalid"},
                status_code=200,
            )
        parsed = _profile_report_payload(payload)
        if parsed is None:
            return JSONResponse(
                {"status": "failed", "error": "memory_provider_response_invalid"},
                status_code=200,
            )
        profile, language = parsed
        generator = ProfileReportGenerator(
            base_url=os.environ.get("EVEROS_LLM__BASE_URL"),
            model=os.environ.get("EVEROS_LLM__MODEL"),
            api_key=os.environ.get("EVEROS_LLM__API_KEY"),
        )
        try:
            report = await generator.generate(profile, language)
        except MemoryProviderFailure as failure:
            return JSONResponse({"status": "failed", "error": failure.error}, status_code=200)
        except Exception:
            return JSONResponse(
                {"status": "failed", "error": "memory_processing_failed"},
                status_code=200,
            )
        return JSONResponse({"status": "ok", "report": report}, status_code=200)

    @app.middleware("http")
    async def guard(request: Any, call_next: Any) -> Any:
        body = await request.body()
        if _request_rejection(
            request.method,
            request.url.path,
            body,
            attachments_root=attachments_root,
        ) is not None:
            return JSONResponse({"detail": "memory_request_rejected"}, status_code=403)
        return await call_next(request)

    config = uvicorn.Config(
        app,
        uds=str(uds),
        access_log=False,
        log_level="warning",
        log_config=None,
        timeout_graceful_shutdown=1,
    )
    uvicorn.Server(config).run()


def _request_rejection(
    method: str,
    path: str,
    body: bytes,
    *,
    attachments_root: Path | None = None,
) -> str | None:
    if method == "GET" and path == "/health":
        return None
    if method != "POST" or path not in {
        "/api/v2/memory/add",
        "/api/v2/memory/flush",
        "/api/v2/memory/search",
        "/api/v2/memory/get",
        "/avibe/v1/profile-report",
    }:
        return "route"
    if len(body) > _MAX_BODY_BYTES:
        return "body"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "json"
    if not isinstance(payload, dict):
        return "shape"
    if path == "/api/v2/memory/add":
        return _validate_add(payload, attachments_root=attachments_root)
    if path == "/api/v2/memory/flush":
        return _validate_flush(payload)
    if path == "/api/v2/memory/search":
        return _validate_search(payload)
    if path == "/avibe/v1/profile-report":
        return None if _profile_report_payload(payload) is not None else "profile-report"
    return _validate_get(payload)


def _valid_scope(payload: dict[str, Any]) -> bool:
    project_id = payload.get("project_id")
    return (
        payload.get("app_id") == _APP_ID
        and isinstance(project_id, str)
        and _PROJECT_PATTERN.fullmatch(project_id) is not None
    )


def _exact_keys(payload: dict[str, Any], keys: set[str]) -> bool:
    return set(payload) == keys


def _validate_add(
    payload: dict[str, Any],
    *,
    attachments_root: Path | None,
) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id", "messages"}) or not _valid_scope(payload):
        return "add"
    messages = payload.get("messages")
    if not isinstance(payload.get("session_id"), str) or not isinstance(messages, list) or len(messages) != 1:
        return "add"
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"sender_id", "role", "timestamp", "content"}:
        return "add"
    if (
        not _valid_principal(message.get("sender_id"))
        or message.get("role") != "user"
        or not isinstance(message.get("timestamp"), int)
        or isinstance(message.get("timestamp"), bool)
    ):
        return "add"
    content = message.get("content")
    if isinstance(content, str):
        return None
    if not isinstance(content, list) or not 1 <= len(content) <= 9:
        return "add"
    for item in content:
        if not isinstance(item, dict):
            return "add"
        if item.get("type") == "text":
            if set(item) != {"type", "text"} or not isinstance(item.get("text"), str):
                return "add"
            continue
        if not _valid_workbench_attachment(item, attachments_root):
            return "add"
    return None


def _valid_workbench_attachment(item: dict[str, Any], attachments_root: Path | None) -> bool:
    if set(item) != {"type", "name", "uri", "ext"}:
        return False
    if item.get("type") not in {"image", "audio", "doc", "pdf", "html", "email"}:
        return False
    name = item.get("name")
    uri = item.get("uri")
    extension = item.get("ext")
    if (
        attachments_root is None
        or not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 512
        or not isinstance(uri, str)
        or not isinstance(extension, str)
        or not extension.isalnum()
        or len(extension) > 8
        or extension.lower() not in SUPPORTED_ATTACHMENT_EXTENSIONS
    ):
        return False
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return False
    try:
        root = attachments_root.resolve(strict=True)
        raw_path = Path(unquote(parsed.path))
        raw_info = raw_path.lstat()
        if stat.S_ISLNK(raw_info.st_mode):
            return False
        path = raw_path.resolve(strict=True)
        path.relative_to(root)
        info = path.lstat()
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _validate_flush(payload: dict[str, Any]) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id"}) or not _valid_scope(payload):
        return "flush"
    return None if isinstance(payload.get("session_id"), str) else "flush"


def _validate_search(payload: dict[str, Any]) -> str | None:
    keys = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    if not _exact_keys(payload, keys) or not _valid_scope(payload):
        return "search"
    if (
        not _valid_principal(payload.get("user_id"))
        or not isinstance(payload.get("query"), str)
        or payload.get("method") != "hybrid"
        or not isinstance(payload.get("top_k"), int)
        or isinstance(payload.get("top_k"), bool)
        or not 1 <= payload["top_k"] <= 20
        or payload.get("include_profile") is not True
        or payload.get("enable_llm_rerank") is not False
    ):
        return "search"
    return None


def _validate_get(payload: dict[str, Any]) -> str | None:
    keys = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    if not _exact_keys(payload, keys) or not _valid_scope(payload):
        return "get"
    if (
        not _valid_principal(payload.get("user_id"))
        or payload.get("memory_type") not in {"profile", "episode"}
        or payload.get("page") != 1
        or payload.get("page_size") != 20
        or payload.get("sort_by") != "timestamp"
        or payload.get("sort_order") != "desc"
    ):
        return "get"
    return None


def _profile_report_payload(
    payload: object,
) -> tuple[MemoryProfile, Literal["en", "zh"]] | None:
    """Parse the exact Avibe-owned profile-report body, never credentials."""

    if not isinstance(payload, dict) or not _exact_keys(payload, {"language", "profile"}):
        return None
    language = payload.get("language")
    if language not in {"en", "zh"}:
        return None
    profile_payload = payload.get("profile")
    if not isinstance(profile_payload, dict) or not _exact_keys(
        profile_payload,
        {"summary", "explicit_info", "implicit_traits", "updated_at"},
    ):
        return None

    summary = _optional_profile_text(profile_payload.get("summary"))
    if profile_payload.get("summary") is not None and summary is None:
        return None
    explicit_info = _profile_explicit_info(profile_payload.get("explicit_info"))
    implicit_traits = _profile_implicit_traits(profile_payload.get("implicit_traits"))
    updated_at = _optional_profile_timestamp(profile_payload.get("updated_at"))
    if profile_payload.get("updated_at") is not None and updated_at is None:
        return None
    if explicit_info is None or implicit_traits is None:
        return None
    if summary is None and not explicit_info and not implicit_traits and updated_at is None:
        return None
    return (
        MemoryProfile(
            summary=summary,
            explicit_info=explicit_info,
            implicit_traits=implicit_traits,
            updated_at=updated_at,
        ),
        language,
    )


def _profile_explicit_info(value: object) -> tuple[MemoryProfileExplicitInfo, ...] | None:
    if not isinstance(value, list) or len(value) > 200:
        return None
    entries: list[MemoryProfileExplicitInfo] = []
    for entry in value:
        if not isinstance(entry, dict) or not _exact_keys(entry, {"description", "category", "evidence"}):
            return None
        description = _optional_profile_text(entry.get("description"))
        category = _optional_profile_text(entry.get("category"))
        evidence = _optional_profile_text(entry.get("evidence"))
        if description is None:
            return None
        if (entry.get("category") is not None and category is None) or (
            entry.get("evidence") is not None and evidence is None
        ):
            return None
        entries.append(MemoryProfileExplicitInfo(description=description, category=category, evidence=evidence))
    return tuple(entries)


def _profile_implicit_traits(value: object) -> tuple[MemoryProfileTrait, ...] | None:
    if not isinstance(value, list) or len(value) > 200:
        return None
    entries: list[MemoryProfileTrait] = []
    for entry in value:
        if not isinstance(entry, dict) or not _exact_keys(
            entry,
            {"description", "trait", "basis", "evidence"},
        ):
            return None
        description = _optional_profile_text(entry.get("description"))
        trait = _optional_profile_text(entry.get("trait"))
        basis = _optional_profile_text(entry.get("basis"))
        evidence = _optional_profile_text(entry.get("evidence"))
        if description is None:
            return None
        if any(
            original is not None and normalized is None
            for original, normalized in (
                (entry.get("trait"), trait),
                (entry.get("basis"), basis),
                (entry.get("evidence"), evidence),
            )
        ):
            return None
        entries.append(
            MemoryProfileTrait(
                description=description,
                trait=trait,
                basis=basis,
                evidence=evidence,
            )
        )
    return tuple(entries)


def _optional_profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        return None
    if not text or len(encoded) > 64 * 1024:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t", "\r"} for character in text):
        return None
    return text


def _optional_profile_timestamp(value: object) -> str | None:
    text = _optional_profile_text(value)
    if text is None or len(text.encode("utf-8")) > 64:
        return None
    try:
        instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant):
        return None
    return text


def _valid_principal(value: object) -> bool:
    return isinstance(value, str) and _PRINCIPAL_PATTERN.fullmatch(value) is not None


def _processing_healthy_from_child_environment() -> bool:
    """Run fixed authenticated probes only inside the scrubbed owned child."""

    from core.memory.everos import EverOSPort

    provider = EverOSPort(
        Path("/nonexistent-memory-sidecar.sock"),
        llm_base_url=os.environ.get("EVEROS_LLM__BASE_URL"),
        llm_model=os.environ.get("EVEROS_LLM__MODEL"),
        llm_api_key=os.environ.get("EVEROS_LLM__API_KEY"),
        embedding_base_url=os.environ.get("EVEROS_EMBEDDING__BASE_URL"),
        embedding_model=os.environ.get("EVEROS_EMBEDDING__MODEL"),
        embedding_api_key=os.environ.get("EVEROS_EMBEDDING__API_KEY"),
    )
    return asyncio.run(provider.processing_healthy())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds")
    parser.add_argument("--probe-processing", action="store_true")
    args = parser.parse_args()
    if args.probe_processing:
        return 0 if _processing_healthy_from_child_environment() else 1
    if not args.uds:
        parser.error("--uds is required when serving")
    serve(Path(args.uds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
