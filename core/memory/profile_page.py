"""Private, durable static-page artifacts derived from a Memory profile."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows
    _fcntl = None

from core.memory.store import is_principal_id, is_project_id
from core.memory.types import (
    MemoryProfilePageDescriptor,
    MemoryProfilePageSource,
    memory_profile_page_payload,
)


PROFILE_PAGE_SCHEMA_VERSION = 1
PROFILE_PAGE_PROMPT_CONTRACT_VERSION = 2
PROFILE_PAGE_MAX_HTML_BYTES = 128 * 1024
PROFILE_PAGE_MAX_CSS_BYTES = 64 * 1024
PROFILE_PAGE_MAX_MANIFEST_BYTES = 8 * 1024
PROFILE_PAGE_RETAINED_VERSIONS = 3
PROFILE_PAGE_STALE_TEMP_SECONDS = 5 * 60

_ARTIFACT_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_ASSET_NAMES = frozenset({"index.html", "styles.css"})
_FORBIDDEN_TAGS = frozenset(
    {
        "a",
        "animate",
        "animatemotion",
        "animatetransform",
        "area",
        "base",
        "button",
        "discard",
        "embed",
        "feimage",
        "form",
        "iframe",
        "image",
        "input",
        "object",
        "plaintext",
        "script",
        "select",
        "set",
        "style",
        "template",
        "textarea",
        "video",
        "audio",
        "source",
        "track",
        "use",
        "foreignobject",
        "xmp",
    }
)
_URL_ATTRIBUTES = frozenset(
    {
        "action",
        "archive",
        "background",
        "cite",
        "classid",
        "codebase",
        "data",
        "formaction",
        "href",
        "icon",
        "longdesc",
        "manifest",
        "poster",
        "profile",
        "src",
        "srcset",
        "usemap",
        "xlink:href",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HEAD_CONTENT_TAGS = frozenset({"link", "meta", "title"})
_FORBIDDEN_CSS_PATTERNS = (
    re.compile(r"@import\b", re.IGNORECASE),
    re.compile(r"url\s*\(", re.IGNORECASE),
    re.compile(r"expression\s*\(", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
    re.compile(r"(?:-moz-binding|behavior)\s*:", re.IGNORECASE),
    re.compile(r"(?:data|file|ftp|https?|javascript)\s*:", re.IGNORECASE),
)
_PROFILE_PAGE_PROCESS_LOCK = threading.Lock()


class ProfilePageValidationError(ValueError):
    """The model-authored page does not satisfy the static source contract."""


class ProfilePageStoreError(OSError):
    """The private artifact store could not safely publish or read state."""


class _ProfileHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctypes = 0
        self.counts: dict[str, int] = {}
        self.charset = 0
        self.viewport = 0
        self.stylesheet = 0
        self.page_marker = 0
        self.generated_markers: list[str] = []
        self.source_markers: list[str] = []
        self._time_stack: list[tuple[str | None, list[str]]] = []
        self._open_tags: list[str] = []
        self._document_sections: list[str] = []
        self._title_text: list[str] = []

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() != "doctype html":
            raise ProfilePageValidationError("invalid document declaration")
        self.doctypes += 1

    def handle_comment(self, data: str) -> None:
        del data
        raise ProfilePageValidationError("page comments are not allowed")

    def handle_pi(self, data: str) -> None:
        del data
        raise ProfilePageValidationError("processing instructions are not allowed")

    def unknown_decl(self, data: str) -> None:
        del data
        raise ProfilePageValidationError("unknown document declarations are not allowed")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        self._validate_start_context(normalized_tag)
        self.counts[normalized_tag] = self.counts.get(normalized_tag, 0) + 1
        if normalized_tag in _FORBIDDEN_TAGS:
            raise ProfilePageValidationError("active page content is not allowed")

        normalized_attrs: dict[str, str] = {}
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            if name in normalized_attrs:
                raise ProfilePageValidationError("duplicate HTML attribute")
            value = raw_value or ""
            normalized_attrs[name] = value
            if name.startswith("on") or name in {"contenteditable", "hidden", "inert", "ping"}:
                raise ProfilePageValidationError("interactive HTML attribute is not allowed")
            if name == "style":
                raise ProfilePageValidationError("inline styles are not allowed")
            if name == "http-equiv":
                raise ProfilePageValidationError("HTTP-equivalent metadata is not allowed")
            if name in _URL_ATTRIBUTES:
                self._validate_url_attribute(normalized_tag, name, value)

        if normalized_tag == "meta":
            if set(normalized_attrs) == {"charset"} and normalized_attrs["charset"].lower() == "utf-8":
                self.charset += 1
            elif (
                set(normalized_attrs) == {"name", "content"}
                and normalized_attrs["name"].lower() == "viewport"
                and "width=device-width"
                in {
                    part.strip().lower().replace(" ", "")
                    for part in normalized_attrs["content"].split(",")
                }
            ):
                self.viewport += 1
            else:
                raise ProfilePageValidationError("only fixed page metadata is allowed")
        elif normalized_tag == "link":
            if set(normalized_attrs) != {"rel", "href"}:
                raise ProfilePageValidationError("only the fixed stylesheet link is allowed")
            rel = {part.lower() for part in normalized_attrs["rel"].split()}
            if rel != {"stylesheet"} or normalized_attrs["href"] != "./styles.css":
                raise ProfilePageValidationError("only the fixed stylesheet link is allowed")
            self.stylesheet += 1
        elif normalized_tag == "main":
            if normalized_attrs.get("data-avibe-memory-profile-page") == str(PROFILE_PAGE_SCHEMA_VERSION):
                self.page_marker += 1
        elif normalized_tag == "time":
            marker: str | None = None
            if "data-avibe-generated-at" in normalized_attrs:
                marker = "generated"
                self.generated_markers.append(normalized_attrs.get("datetime", ""))
            elif "data-avibe-source-updated-at" in normalized_attrs:
                marker = "source"
                self.source_markers.append(normalized_attrs.get("datetime", ""))
            self._time_stack.append((marker, []))
        if normalized_tag in {"head", "body"}:
            self._document_sections.append(normalized_tag)
        if normalized_tag not in _VOID_TAGS:
            self._open_tags.append(normalized_tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if (
            normalized_tag in _VOID_TAGS
            or not self._open_tags
            or self._open_tags[-1] != normalized_tag
        ):
            raise ProfilePageValidationError("HTML elements must be explicitly nested")
        if normalized_tag == "time" and self._time_stack:
            marker, visible_text = self._time_stack.pop()
            if marker is not None and not "".join(visible_text).strip():
                raise ProfilePageValidationError("timestamp markers must be visible")
        self._open_tags.pop()

    def handle_data(self, data: str) -> None:
        if not self._open_tags and data.strip():
            raise ProfilePageValidationError("content outside the HTML document is not allowed")
        if "body" in self._open_tags and "main" not in self._open_tags and data.strip():
            raise ProfilePageValidationError("all body content must be inside main")
        if self._open_tags and self._open_tags[-1] == "title":
            self._title_text.append(data)
        if self._time_stack:
            self._time_stack[-1][1].append(data)

    def _validate_start_context(self, tag: str) -> None:
        if not self._open_tags:
            if tag != "html":
                raise ProfilePageValidationError("content outside the HTML document is not allowed")
            return
        if tag == "html":
            raise ProfilePageValidationError("nested HTML documents are not allowed")
        if tag in {"head", "body"}:
            if self._open_tags != ["html"]:
                raise ProfilePageValidationError("head and body must be direct HTML children")
            return
        if tag in _HEAD_CONTENT_TAGS:
            if self._open_tags != ["html", "head"]:
                raise ProfilePageValidationError("page metadata must be inside head")
            return
        if "head" in self._open_tags:
            raise ProfilePageValidationError("unsupported head content")
        if tag == "main":
            if self._open_tags != ["html", "body"]:
                raise ProfilePageValidationError("main must be the only body root")
            return
        if self._open_tags == ["html"]:
            raise ProfilePageValidationError("only head and body may be HTML children")
        if "body" in self._open_tags and "main" not in self._open_tags:
            raise ProfilePageValidationError("all body content must be inside main")

    @staticmethod
    def _validate_url_attribute(tag: str, name: str, value: str) -> None:
        if tag == "link" and name == "href" and value == "./styles.css":
            return
        if tag == "img" and name == "src" and re.fullmatch(
            r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+",
            value,
            flags=re.IGNORECASE,
        ):
            return
        raise ProfilePageValidationError("remote or navigable page content is not allowed")


def validate_profile_page_source(
    source: MemoryProfilePageSource,
    *,
    generated_at: str,
    source_profile_updated_at: str | None,
) -> tuple[bytes, bytes]:
    """Validate and encode one fixed two-file static page package."""

    if not isinstance(source, MemoryProfilePageSource):
        raise ProfilePageValidationError("invalid profile page source")
    html = _bounded_utf8(source.index_html, PROFILE_PAGE_MAX_HTML_BYTES, "index.html")
    css = _bounded_utf8(source.styles_css, PROFILE_PAGE_MAX_CSS_BYTES, "styles.css")
    if not source.index_html.lower().startswith("<!doctype html>"):
        raise ProfilePageValidationError("index.html must begin with its doctype")
    if not _valid_timestamp(generated_at) or (
        source_profile_updated_at is not None and not _valid_timestamp(source_profile_updated_at)
    ):
        raise ProfilePageValidationError("invalid page timestamp")
    if "\\" in source.styles_css or "/*" in source.styles_css or "*/" in source.styles_css:
        raise ProfilePageValidationError("CSS escapes and comments are not allowed")
    for pattern in _FORBIDDEN_CSS_PATTERNS:
        if pattern.search(source.styles_css):
            raise ProfilePageValidationError("unsafe stylesheet content")

    parser = _ProfileHTMLParser()
    try:
        parser.feed(source.index_html)
        parser.close()
    except ProfilePageValidationError:
        raise
    except Exception as error:
        raise ProfilePageValidationError("invalid HTML document") from error
    if parser.doctypes != 1:
        raise ProfilePageValidationError("one HTML doctype is required")
    if parser._open_tags or parser._time_stack:
        raise ProfilePageValidationError("HTML elements must be explicitly closed")
    if any(parser.counts.get(tag, 0) != 1 for tag in ("html", "head", "body", "title", "main")):
        raise ProfilePageValidationError("one complete HTML document is required")
    if parser._document_sections != ["head", "body"] or not "".join(parser._title_text).strip():
        raise ProfilePageValidationError("head, title, and body order is invalid")
    if parser.charset != 1 or parser.viewport != 1 or parser.stylesheet != 1 or parser.counts.get("link", 0) != 1:
        raise ProfilePageValidationError("required page metadata is missing")
    if parser.page_marker != 1:
        raise ProfilePageValidationError("profile page marker is missing")
    if parser.generated_markers != [generated_at]:
        raise ProfilePageValidationError("generated timestamp marker does not match")
    expected_source = [] if source_profile_updated_at is None else [source_profile_updated_at]
    if parser.source_markers != expected_source:
        raise ProfilePageValidationError("source timestamp marker does not match")
    return html, css


class MemoryProfilePageStore:
    """Publish and restore isolated immutable profile-page source packages."""

    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.abspath(os.fspath(root)))

    def publish(
        self,
        *,
        scope_key: bytes,
        principal_id: str,
        project_id: str,
        language: str,
        source: MemoryProfilePageSource,
        generated_at: str,
        source_profile_updated_at: str | None,
        source_profile_snapshot_id: str,
    ) -> MemoryProfilePageDescriptor:
        typed_language = _validated_scope(scope_key, principal_id, project_id, language)
        if not _SHA256_RE.fullmatch(source_profile_snapshot_id):
            raise ProfilePageValidationError("invalid profile snapshot id")
        html, css = validate_profile_page_source(
            source,
            generated_at=generated_at,
            source_profile_updated_at=source_profile_updated_at,
        )
        artifact_id = secrets.token_hex(16)
        published_at = _utc_now()
        content_digest = hashlib.sha256(html + bytes(1) + css).hexdigest()
        content_sha256 = f"sha256:{content_digest}"
        descriptor = MemoryProfilePageDescriptor(
            artifact_id=artifact_id,
            language=typed_language,
            generated_at=generated_at,
            published_at=published_at,
            source_profile_updated_at=source_profile_updated_at,
            source_profile_snapshot_id=source_profile_snapshot_id,
            prompt_contract_version=PROFILE_PAGE_PROMPT_CONTRACT_VERSION,
            content_sha256=content_sha256,
        )
        manifest = {
            "schema_version": PROFILE_PAGE_SCHEMA_VERSION,
            **memory_profile_page_payload(descriptor),
            "assets": {
                "index.html": f"sha256:{hashlib.sha256(html).hexdigest()}",
                "styles.css": f"sha256:{hashlib.sha256(css).hexdigest()}",
            },
        }
        manifest_bytes = _json_bytes(manifest)
        if len(manifest_bytes) > PROFILE_PAGE_MAX_MANIFEST_BYTES:
            raise ProfilePageValidationError("profile page manifest is too large")

        language_root, versions = self._prepare_scope_root(
            scope_key,
            principal_id,
            project_id,
            typed_language,
        )
        temporary = versions / f".tmp-{artifact_id}"
        final = versions / artifact_id
        pointer = language_root / f".current-{artifact_id}.tmp"
        current_replaced = False
        try:
            with _publication_lock(language_root):
                os.mkdir(temporary, 0o700)
                _write_private_file(temporary / "index.html", html)
                _write_private_file(temporary / "styles.css", css)
                _write_private_file(temporary / "manifest.json", manifest_bytes)
                _fsync_directory(temporary)
                os.replace(temporary, final)
                _fsync_directory(versions)
                _write_private_file(pointer, _json_bytes(memory_profile_page_payload(descriptor)))
                os.replace(pointer, language_root / "current.json")
                current_replaced = True
                _fsync_directory(language_root)
                self._prune_versions(versions, keep=artifact_id)
                self._prune_stale_temporary_files(language_root, versions)
        except Exception as error:
            _remove_tree_best_effort(temporary)
            _remove_tree_best_effort(pointer)
            if current_replaced:
                return descriptor
            _remove_tree_best_effort(final)
            raise ProfilePageStoreError("profile page publication failed") from error
        return descriptor

    def current(
        self,
        *,
        scope_key: bytes,
        principal_id: str,
        project_id: str,
        language: str,
    ) -> MemoryProfilePageDescriptor | None:
        typed_language = _validated_scope(scope_key, principal_id, project_id, language)
        language_root = self._language_root(scope_key, principal_id, project_id, typed_language)
        versions = language_root / "versions"
        if not self._private_directory_chain(language_root, versions):
            return None
        payload = _read_json_file(language_root / "current.json", PROFILE_PAGE_MAX_MANIFEST_BYTES)
        descriptor = _descriptor_from_payload(payload)
        if descriptor is None or descriptor.language != typed_language:
            return None
        version = versions / descriptor.artifact_id
        if not self._private_directory_chain(language_root, version):
            return None
        manifest = _read_json_file(
            version / "manifest.json",
            PROFILE_PAGE_MAX_MANIFEST_BYTES,
        )
        if not isinstance(manifest, dict) or manifest.get("schema_version") != PROFILE_PAGE_SCHEMA_VERSION:
            return None
        if any(manifest.get(key) != value for key, value in memory_profile_page_payload(descriptor).items()):
            return None
        html = self.read(
            scope_key=scope_key,
            principal_id=principal_id,
            project_id=project_id,
            language=typed_language,
            artifact_id=descriptor.artifact_id,
            asset_name="index.html",
        )
        css = self.read(
            scope_key=scope_key,
            principal_id=principal_id,
            project_id=project_id,
            language=typed_language,
            artifact_id=descriptor.artifact_id,
            asset_name="styles.css",
        )
        if html is None or css is None:
            return None
        content_digest = hashlib.sha256(html + bytes(1) + css).hexdigest()
        if descriptor.content_sha256 != f"sha256:{content_digest}":
            return None
        return descriptor

    def read(
        self,
        *,
        scope_key: bytes,
        principal_id: str,
        project_id: str,
        language: str,
        artifact_id: str,
        asset_name: str,
    ) -> bytes | None:
        typed_language = _validated_scope(scope_key, principal_id, project_id, language)
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id) or asset_name not in _ASSET_NAMES:
            return None
        language_root = self._language_root(scope_key, principal_id, project_id, typed_language)
        version = language_root / "versions" / artifact_id
        if not self._private_directory_chain(language_root, version):
            return None
        manifest = _read_json_file(version / "manifest.json", PROFILE_PAGE_MAX_MANIFEST_BYTES)
        if not isinstance(manifest, dict) or manifest.get("artifact_id") != artifact_id:
            return None
        assets = manifest.get("assets")
        expected_digest = assets.get(asset_name) if isinstance(assets, dict) else None
        if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(expected_digest):
            return None
        maximum = PROFILE_PAGE_MAX_HTML_BYTES if asset_name == "index.html" else PROFILE_PAGE_MAX_CSS_BYTES
        payload = _read_private_file(version / asset_name, maximum)
        if payload is None or f"sha256:{hashlib.sha256(payload).hexdigest()}" != expected_digest:
            return None
        return payload

    def clear_all(self) -> None:
        if not self._root.exists() and not self._root.is_symlink():
            return
        try:
            _remove_tree_no_follow(self._root)
        except Exception as error:
            raise ProfilePageStoreError("profile page cleanup failed") from error

    def _prepare_scope_root(
        self,
        scope_key: bytes,
        principal_id: str,
        project_id: str,
        language: Literal["en", "zh"],
    ) -> tuple[Path, Path]:
        _ensure_private_directory(self._root)
        scope_root = self._root / _scope_digest(scope_key, principal_id, project_id)
        _ensure_private_directory(scope_root)
        language_root = scope_root / language
        _ensure_private_directory(language_root)
        versions = language_root / "versions"
        _ensure_private_directory(versions)
        return language_root, versions

    def _language_root(
        self,
        scope_key: bytes,
        principal_id: str,
        project_id: str,
        language: Literal["en", "zh"],
    ) -> Path:
        return self._root / _scope_digest(scope_key, principal_id, project_id) / language

    def _private_directory_chain(self, language_root: Path, final: Path) -> bool:
        scope_root = language_root.parent
        versions = language_root / "versions"
        required = [self._root, scope_root, language_root, versions]
        if final != versions:
            required.append(final)
        return all(_is_private_directory(path) for path in required)

    @staticmethod
    def _prune_versions(versions: Path, *, keep: str) -> None:
        try:
            entries = [
                (entry.lstat().st_mtime_ns, entry)
                for entry in versions.iterdir()
                if entry.name != keep and _ARTIFACT_ID_RE.fullmatch(entry.name)
            ]
            entries.sort(key=lambda item: item[0], reverse=True)
            for _modified_at, entry in entries[PROFILE_PAGE_RETAINED_VERSIONS - 1 :]:
                _remove_tree_no_follow(entry)
        except Exception:
            return

    @staticmethod
    def _prune_stale_temporary_files(language_root: Path, versions: Path) -> None:
        cutoff_ns = time.time_ns() - PROFILE_PAGE_STALE_TEMP_SECONDS * 1_000_000_000
        try:
            entries = (
                *(
                    entry
                    for entry in language_root.iterdir()
                    if entry.name.startswith(".current-") and entry.name.endswith(".tmp")
                ),
                *(entry for entry in versions.iterdir() if entry.name.startswith(".tmp-")),
            )
            for entry in entries:
                if entry.lstat().st_mtime_ns <= cutoff_ns:
                    _remove_tree_no_follow(entry)
        except Exception:
            return


def _validated_scope(
    scope_key: bytes,
    principal_id: str,
    project_id: str,
    language: str,
) -> Literal["en", "zh"]:
    if (
        not isinstance(scope_key, bytes)
        or len(scope_key) < 16
        or not is_principal_id(principal_id)
        or not is_project_id(project_id)
        or language not in {"en", "zh"}
    ):
        raise ProfilePageValidationError("invalid profile page scope")
    return language


def _scope_digest(scope_key: bytes, principal_id: str, project_id: str) -> str:
    value = f"{principal_id}\0{project_id}".encode("ascii")
    return hmac.new(scope_key, value, hashlib.sha256).hexdigest()


def _bounded_utf8(value: object, maximum: int, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ProfilePageValidationError(f"{label} is empty")
    try:
        payload = value.encode("utf-8")
    except UnicodeError as error:
        raise ProfilePageValidationError(f"{label} is not UTF-8") from error
    if len(payload) > maximum or any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value
    ):
        raise ProfilePageValidationError(f"{label} is invalid")
    return payload


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _descriptor_from_payload(value: object) -> MemoryProfilePageDescriptor | None:
    keys = {
        "artifact_id",
        "language",
        "generated_at",
        "published_at",
        "source_profile_updated_at",
        "source_profile_snapshot_id",
        "prompt_contract_version",
        "content_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys:
        return None
    if (
        not isinstance(value.get("artifact_id"), str)
        or not _ARTIFACT_ID_RE.fullmatch(value["artifact_id"])
        or value.get("language") not in {"en", "zh"}
        or not _valid_timestamp(value.get("generated_at"))
        or not _valid_timestamp(value.get("published_at"))
        or (
            value.get("source_profile_updated_at") is not None
            and not _valid_timestamp(value.get("source_profile_updated_at"))
        )
        or not isinstance(value.get("source_profile_snapshot_id"), str)
        or not _SHA256_RE.fullmatch(value["source_profile_snapshot_id"])
        or value.get("prompt_contract_version") != PROFILE_PAGE_PROMPT_CONTRACT_VERSION
        or not isinstance(value.get("content_sha256"), str)
        or not _SHA256_RE.fullmatch(value["content_sha256"])
    ):
        return None
    return MemoryProfilePageDescriptor(**value)


def _ensure_private_directory(path: Path) -> None:
    _reject_symlinked_ancestors(path.parent)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        pass
    _reject_symlinked_ancestors(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProfilePageStoreError("unsafe profile page directory") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ProfilePageStoreError("unsafe profile page directory")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def _is_private_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _reject_symlinked_ancestors(path: Path) -> None:
    current = path
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode):
                raise ProfilePageStoreError("profile page path contains a symlink")
        if current == current.parent:
            return
        current = current.parent


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("profile page write failed")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_lock(language_root: Path) -> Iterator[None]:
    # Native Windows rejects Memory runtime installation, but this process lock
    # keeps direct store callers thread-safe without making module import fail.
    # POSIX adds a file lock for overlapping service processes during restarts.
    with _PROFILE_PAGE_PROCESS_LOCK:
        path = language_root / ".publish.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ProfilePageStoreError("unsafe profile page publication lock")
            os.fchmod(descriptor, 0o600)
            if _fcntl is not None:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX)
            yield
        finally:
            try:
                if _fcntl is not None:
                    _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _read_private_file(path: Path, maximum: int) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        return payload if len(payload) <= maximum else None
    finally:
        os.close(descriptor)


def _read_json_file(path: Path, maximum: int) -> object | None:
    payload = _read_private_file(path, maximum)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree_best_effort(path: Path) -> None:
    try:
        _remove_tree_no_follow(path)
    except Exception:
        return


def _remove_tree_no_follow(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    with os.scandir(path) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        _remove_tree_no_follow(child)
    os.rmdir(path)
