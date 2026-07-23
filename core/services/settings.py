"""Business API for V2 config + settings store.

C3 of the services-layer refactor (Plan 1 in
``docs/plans/workbench-dispatch-architecture.md`` §6.4). Two formerly-
duplicated entry points collapse into one named seam:

* CLI's ``vibe.cli._ensure_config`` — creates a default config on first
  use, then loads via ``V2Config.load(paths.get_config_path())``.
* UI server's bare ``V2Config.load()`` — relies on ``V2Config.load()``'s
  default-path lookup; doesn't seed a default on first run.

Same for ``SettingsStore``: CLI passes ``paths.get_settings_path()``
explicitly, UI server calls ``SettingsStore.get_instance()`` with no
args. Behavior overlaps but the entry shape diverges, so this module
makes both go through one function.

Both helpers are thin and side-effect free: they don't mutate process
state beyond what the underlying primitives already do (V2Config has no
caching; SettingsStore owns its own thread-safe singleton). Callers must
keep being explicit about reloads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from config import SettingsStore, paths
from config.platform_registry import WORKBENCH_PLATFORM_ID
from config.v2_config import (
    AgentsConfig,
    ClaudeConfig,
    CodexConfig,
    ModelHubConfig,
    OpenCodeConfig,
    PlatformsConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)

DefaultConfigFactory = Callable[[], V2Config]


def default_config() -> V2Config:
    """Build a fresh, un-completed default ``V2Config`` (never persisted here).

    Single source of truth for the "first run" config shape, shared by:

    * the CLI's seed-on-first-use path (``vibe.cli._ensure_config`` →
      ``load_config(default_factory=default_config)``), which persists the
      result, and
    * the read-side default for the Web UI's ``GET /api/config``
      (``load_config_or_default``), which keeps it in memory so a brand-new
      user can open the setup wizard — including the reused provider-config
      modal that calls ``getConfig()`` — before any config file exists.

    ``setup_completed`` stays ``False`` and no platform carries credentials,
    so ``setup_state()['needs_setup']`` is ``True``: a fresh default is never
    mistaken for a finished setup, and the wizard still shows. ``platforms``
    starts workbench-only (no external IM enabled) — see below.
    """

    return V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="", app_token=""),
        # Workbench-only first-run state. The always-on Avibe Workbench is the
        # sole inbound surface and no external IM is enabled yet, so ``enabled``
        # MUST start empty — overriding the PlatformsConfig ``["slack"]``
        # dataclass default. Otherwise a fresh install (or the wizard's "skip
        # chat platforms" path) would persist a setup-completed config with
        # Slack enabled and empty credentials — a phantom transport. ``primary``
        # anchors to the workbench; the user adds real IMs explicitly later.
        platforms=PlatformsConfig(enabled=[], primary=WORKBENCH_PLATFORM_ID),
        runtime=RuntimeConfig(default_cwd=str(Path.home() / "work")),
        agents=AgentsConfig(
            opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
            claude=ClaudeConfig(enabled=True, cli_path="claude"),
            codex=CodexConfig(enabled=False, cli_path="codex"),
        ),
        model_hub=ModelHubConfig.fresh(),
    )


def load_config(
    config_path: Optional[Path] = None,
    *,
    default_factory: Optional[DefaultConfigFactory] = None,
) -> V2Config:
    """Load the V2 config, optionally seeding a default file when missing.

    Behavior depends on ``default_factory``:

    * **None** (default, matches the UI server's bare ``V2Config.load()``
      behavior today): the file must exist on disk. Raises
      ``FileNotFoundError`` otherwise — this is what the UI server
      prefers for boot-time guards.
    * **Callable** (matches the CLI's ``_ensure_config`` behavior): if
      the file does not exist, ``default_factory()`` is invoked and the
      result is persisted via ``V2Config.save`` before the regular load
      proceeds. The CLI passes its own minimal-default factory here.

    Routing both callers through this entry point keeps the seeding
    contract centrally documented and prevents the two paths from
    drifting (e.g. one auto-creating a file the other expects to be
    absent).
    """

    target = config_path or paths.get_config_path()
    if not target.exists():
        if default_factory is None:
            raise FileNotFoundError(f"V2 config not found at {target}")
        default = default_factory()
        default.save(target)
    return V2Config.load(target)


def load_config_or_default(config_path: Optional[Path] = None) -> V2Config:
    """Load the V2 config, returning an in-memory default when it is missing.

    Unlike ``load_config(default_factory=...)``, this never writes the file:
    it is the read-side default for surfaces (notably the Web UI's
    ``GET /api/config``) that must serve a usable config to a brand-new user
    before any config has been persisted, without turning a read into a
    write or pre-empting the wizard's own first save.

    The default comes from :func:`default_config`, so ``setup_completed`` is
    ``False`` and ``setup_state()['needs_setup']`` is ``True`` — a fresh
    install is never reported as a completed setup. Once a config file
    exists, the on-disk value is returned unchanged.
    """

    target = config_path or paths.get_config_path()
    if not target.exists():
        return default_config()
    return V2Config.load(target)


def get_settings_store(settings_path: Optional[Path] = None) -> SettingsStore:
    """Return the process-wide ``SettingsStore`` singleton.

    Wraps ``SettingsStore.get_instance`` so callers don't need to know
    that the store is a singleton or how it picks the default path.
    Reload-from-disk happens inside ``get_instance``; we expose
    ``reload_settings_store`` for the rare case where a caller wants to
    force it.
    """

    return SettingsStore.get_instance(settings_path)


def reload_settings_store(settings_path: Optional[Path] = None) -> SettingsStore:
    """Force the settings store to re-read from disk.

    ``get_settings_store`` already calls ``maybe_reload`` on each access
    but only when the singleton already exists; the explicit reload here
    is for callers (mostly tests, post-write inspection) that want to
    guarantee they see the freshly-persisted state.
    """

    store = SettingsStore.get_instance(settings_path)
    store.maybe_reload()
    return store


def reset_settings_store() -> None:
    """Test-only: tear down the singleton.

    Re-exports ``SettingsStore.reset_instance`` under the services
    namespace so tests don't have to reach into ``config.v2_settings``
    directly.
    """

    SettingsStore.reset_instance()


__all__ = [
    "default_config",
    "load_config",
    "load_config_or_default",
    "get_settings_store",
    "reload_settings_store",
    "reset_settings_store",
]
