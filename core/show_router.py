"""Load the Runtime-authored router for new Show Page workspaces."""

from __future__ import annotations

from importlib.resources import files


def default_show_router() -> str:
    return files("vibe").joinpath("show_router.tsx").read_text(encoding="utf-8")
