"""The managed tools every backend puts on an Agent command's PATH."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path


def prepend_managed_tools_to_path(
    env: MutableMapping[str, str],
    *,
    base_env: Mapping[str, str],
    working_dir: Path | str | None,
) -> bool:
    """Prepend verified Git, then the managed ripgrep, where the effective PATH lacks them.

    Each tool follows its own module's rules; this is the one composition every
    backend's command environment uses. Returns whether PATH changed.
    """

    from core import git_runtime, ripgrep_runtime

    git_changed = git_runtime.prepend_vendored_git_to_path(env, base_env=base_env, working_dir=working_dir)
    rg_changed = ripgrep_runtime.prepend_managed_ripgrep_to_path(env, base_env=base_env)
    return git_changed or rg_changed
