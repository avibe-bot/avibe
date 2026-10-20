"""Keep the previous Web UI build's hashed assets reachable after an upgrade.

Vite gives every JS/CSS chunk a content-hashed name, and an upgrade replaces
the packaged ``ui/dist`` wholesale. A page loaded before the upgrade still
holds the old build's chunk graph, so opening one of its lazy modules would
404 (``Failed to fetch dynamically imported module``), and the browser keeps
that failure until the page reloads.

Each build whose ``index.html`` the UI server serves is recorded here, and
``serve_static`` answers asset requests the current build cannot from this
archive. Only the ``KEEP_BUILDS`` most recently served builds are kept, so the
archive holds the current build plus the one before it, with unchanged chunks
stored once. Everything here is a cache: losing it only means an unrefreshed
page has to reload.

Layout under ``<runtime>/ui-assets/``:

- ``files/<name>``: one copy of each hashed asset, hard-linked to the current
  build's file when the filesystem allows
- ``builds/<id>.json``: the asset names of one build and when it was last
  recorded
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from config import paths

logger = logging.getLogger(__name__)

KEEP_BUILDS = 2

# One path segment of the characters Vite emits, including a leading
# underscore (``__vite-browser-external-<hash>.js``). An allow-list keeps
# separators and Windows drive or stream syntax (``D:name``) out of the path.
# A leading dot is reserved for this archive's in-progress files and excludes
# ``.`` and ``..``.
_ASSET_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_record_lock = threading.Lock()
_recorded_index_key: tuple[str, str] | None = None


def _archive_dir() -> Path:
    return paths.get_runtime_dir() / "ui-assets"


def find_archived_asset(name: str) -> Path | None:
    """Return the archived file for a flat ``/assets/<name>`` request, if any."""
    if not _ASSET_NAME.fullmatch(name):
        return None
    candidate = _archive_dir() / "files" / name
    return candidate if candidate.is_file() else None


def record_served_build(ui_dist: Path) -> None:
    """Record the build behind ``ui_dist/index.html`` before a page uses it.

    Cheap when the build is unchanged since the last call in this process.
    Never raises, and a failure is not retried until the build changes: it
    only costs unrefreshed pages of this build their fallback.
    """
    global _recorded_index_key
    try:
        index_html = (ui_dist / "index.html").read_bytes()
    except OSError:
        return
    # The entry page names its build's hashed entry chunks. File metadata is
    # not a reliable build identity: reproducible wheels pin timestamps.
    key = (str(ui_dist), hashlib.sha256(index_html).hexdigest())
    if key == _recorded_index_key:
        return
    # A concurrent request is already recording this build; serving the page
    # does not need to wait for it.
    if not _record_lock.acquire(blocking=False):
        return
    try:
        if key == _recorded_index_key:
            return
        _archive_build(ui_dist / "assets", _archive_dir())
    except Exception:
        logger.warning("Failed to archive Web UI assets for unrefreshed pages", exc_info=True)
    finally:
        _recorded_index_key = key
        _record_lock.release()


def _archive_build(assets_dir: Path, archive: Path) -> None:
    if not assets_dir.is_dir():
        return
    names = sorted(
        entry.name for entry in os.scandir(assets_dir) if entry.is_file() and _ASSET_NAME.fullmatch(entry.name)
    )
    if not names:
        return
    files_dir = archive / "files"
    builds_dir = archive / "builds"
    files_dir.mkdir(parents=True, exist_ok=True)
    builds_dir.mkdir(parents=True, exist_ok=True)

    for name in names:
        source = assets_dir / name
        target = files_dir / name
        exists = target.exists()
        # Names are content hashes, so an existing file is this file. An upgrade
        # still writes it anew, so re-link it to the current build's copy;
        # otherwise every shared chunk keeps a superseded install's copy alive.
        if exists and os.path.samefile(source, target):
            continue
        partial = files_dir / f".{name}.{os.getpid()}.partial"
        partial.unlink(missing_ok=True)
        try:
            os.link(source, partial)
        except OSError:
            if exists:
                continue
            shutil.copyfile(source, partial)
        os.replace(partial, target)

    build_id = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]
    manifest = builds_dir / f"{build_id}.json"
    partial_manifest = builds_dir / f".{build_id}.{os.getpid()}.partial"
    # File timestamps can be coarser than successive recordings, so the
    # recording order lives in the manifest itself.
    partial_manifest.write_text(json.dumps({"recorded_at": time.time_ns(), "assets": names}), encoding="utf-8")
    os.replace(partial_manifest, manifest)
    _prune(files_dir, builds_dir, current=manifest, current_assets=names)


def _prune(files_dir: Path, builds_dir: Path, *, current: Path, current_assets: list[str]) -> None:
    others: list[tuple[int, list[str], Path]] = []
    for entry in builds_dir.iterdir():
        if entry == current:
            continue
        try:
            if entry.name.startswith(".") or entry.suffix != ".json":
                raise ValueError("not a manifest")
            manifest = json.loads(entry.read_text(encoding="utf-8"))
            recorded_at, assets = int(manifest["recorded_at"]), manifest["assets"]
            if not isinstance(assets, list):
                raise TypeError("assets is not a list")
            others.append((recorded_at, assets, entry))
        except (OSError, ValueError, KeyError, TypeError):
            # Leftover partials and unreadable manifests protect nothing.
            entry.unlink(missing_ok=True)
    others.sort(key=lambda item: item[0], reverse=True)
    referenced = set(current_assets)
    for _, assets, _ in others[: KEEP_BUILDS - 1]:
        referenced.update(name for name in assets if isinstance(name, str))
    for _, _, entry in others[KEEP_BUILDS - 1 :]:
        entry.unlink(missing_ok=True)
    for entry in files_dir.iterdir():
        if entry.name not in referenced:
            entry.unlink(missing_ok=True)
