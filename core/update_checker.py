"""
Automatic update checker and installer.

This module provides:
1. Periodic checking for new versions on PyPI
2. Slack notifications to workspace owner when updates are available
3. Automatic update installation when the system is idle
4. Managed local dependency reconciliation, such as askill
"""

import asyncio
import calendar
import json
import logging
import re
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from config import paths
from config.v2_config import UpdateConfig
from config.v2_settings import _infer_channel_platform, _infer_user_platform, _split_scoped_key
from modules.im import InlineButton, InlineKeyboard, MessageContext
from vibe.i18n import t as i18n_t
from vibe.upgrade import (
    PACKAGE_NAME,
    get_running_vibe_path,
    get_update_metadata_url,
    has_newer_version,
    select_latest_update_version,
)

if TYPE_CHECKING:
    from core.controller import Controller

logger = logging.getLogger(__name__)

# Action ID for the update button in Slack
UPDATE_BUTTON_ACTION_ID = "vibe_update_now"

# Minimum check interval to prevent tight loops (in minutes)
MIN_CHECK_INTERVAL_MINUTES = 1
MANAGED_DEPENDENCY_CHECK_INTERVAL_MINUTES = 60

# Grace period after sending an update notification before auto-update can proceed (in minutes).
# This gives admins time to read the notification and decide whether to update manually.
NOTIFICATION_GRACE_PERIOD_MINUTES = 10

GITHUB_RELEASE_TAG_BASE_URL = "https://github.com/avibe-bot/avibe/releases/tag"
GITHUB_RELEASE_API_BASE_URL = "https://api.github.com/repos/avibe-bot/avibe/releases/tags"
UPDATE_NOTIFICATION_POLICY_DEFAULT = "default"
UPDATE_NOTIFICATION_POLICY_NONE = "none"
UPDATE_NOTIFICATION_POLICY_MARKER_RE = re.compile(
    r"<!--\s*(?:avibe|vibe-remote):update-notification\s*=\s*(?P<policy>none|default)\s*-->",
    re.IGNORECASE,
)
_STABLE_PACKAGE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:[.-]?post\d+)?$")


def _fetch_pypi_version_sync() -> Dict[str, Any]:
    """Synchronous PyPI version fetch (to be run in thread)."""
    from vibe import __version__

    current = __version__
    result = {"current": current, "latest": None, "has_update": False, "error": None}

    try:
        url = get_update_metadata_url()
        req = urllib.request.Request(url, headers={"User-Agent": PACKAGE_NAME})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latest = select_latest_update_version(data, current)
            result["latest"] = latest

            if latest and latest != current:
                result["has_update"] = has_newer_version(latest, current)
    except Exception as e:
        result["error"] = str(e)

    return result


def _github_release_url(version: str) -> str:
    version_text = str(version).strip()
    tag = version_text if version_text.startswith(("v", "gh-v")) else f"v{version_text}"
    return f"{GITHUB_RELEASE_TAG_BASE_URL}/{urllib.parse.quote(tag, safe='')}"


def _github_release_tag(version: str) -> str:
    version_text = str(version).strip()
    return version_text if version_text.startswith(("v", "gh-v")) else f"v{version_text}"


def _github_release_api_url(version: str) -> str:
    tag = _github_release_tag(version)
    return f"{GITHUB_RELEASE_API_BASE_URL}/{urllib.parse.quote(tag, safe='')}"


def _parse_update_notification_policy(body: object) -> str:
    if not isinstance(body, str):
        return UPDATE_NOTIFICATION_POLICY_DEFAULT
    match = UPDATE_NOTIFICATION_POLICY_MARKER_RE.search(body)
    if not match:
        return UPDATE_NOTIFICATION_POLICY_DEFAULT
    return match.group("policy").lower()


def _fetch_update_notification_policy_sync(version: str) -> Dict[str, Any]:
    result = {"version": version, "policy": UPDATE_NOTIFICATION_POLICY_DEFAULT, "error": None}

    try:
        req = urllib.request.Request(
            _github_release_api_url(version),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": PACKAGE_NAME,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result["policy"] = _parse_update_notification_policy(data.get("body"))
    except Exception as e:
        result["error"] = str(e)

    return result


def _format_release_version_link(version: str, platform: str = "markdown") -> str:
    version_text = str(version).strip()
    url = _github_release_url(version_text)
    if platform == "slack":
        return f"<{url}|{version_text}>"
    if platform == "plain":
        return f"{version_text} ({url})"
    return f"[{version_text}]({url})"


@dataclass
class UpdateState:
    """Persistent state for update tracking."""

    notified_version: Optional[str] = None
    notified_at: Optional[str] = None
    last_check_at: Optional[str] = None
    last_activity_at: Optional[float] = None
    blocked_auto_update_version: Optional[str] = None
    blocked_auto_update_reason: Optional[str] = None
    blocked_auto_update_at: Optional[str] = None
    blocked_auto_update_current_version: Optional[str] = None

    @classmethod
    def load(cls) -> "UpdateState":
        path = cls._get_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                notified_version=data.get("notified_version"),
                notified_at=data.get("notified_at"),
                last_check_at=data.get("last_check_at"),
                last_activity_at=data.get("last_activity_at"),
                blocked_auto_update_version=data.get("blocked_auto_update_version"),
                blocked_auto_update_reason=data.get("blocked_auto_update_reason"),
                blocked_auto_update_at=data.get("blocked_auto_update_at"),
                blocked_auto_update_current_version=data.get("blocked_auto_update_current_version"),
            )
        except Exception as e:
            logger.warning(f"Failed to load update state: {e}")
            return cls()

    def save(self) -> None:
        """Save state atomically using temp file + rename."""
        try:
            path = self._get_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "notified_version": self.notified_version,
                "notified_at": self.notified_at,
                "last_check_at": self.last_check_at,
                "last_activity_at": self.last_activity_at,
                "blocked_auto_update_version": self.blocked_auto_update_version,
                "blocked_auto_update_reason": self.blocked_auto_update_reason,
                "blocked_auto_update_at": self.blocked_auto_update_at,
                "blocked_auto_update_current_version": self.blocked_auto_update_current_version,
            }
            # Atomic write: write to temp file, then rename
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
            ) as f:
                json.dump(data, f, indent=2)
                temp_path = Path(f.name)
            temp_path.replace(path)
        except Exception as e:
            logger.warning(f"Failed to save update state: {e}")

    @staticmethod
    def _get_path() -> Path:
        return paths.get_state_dir() / "update_state.json"


class UpdateChecker:
    """Handles automatic update checking and installation."""

    def __init__(self, controller: "Controller", config: UpdateConfig):
        self.controller = controller
        self.config = config
        self.state = UpdateState.load()
        self._check_task: Optional[asyncio.Task] = None
        self._running = False
        self._upgrade_lock = asyncio.Lock()  # Prevent concurrent upgrades
        self._post_update_lock = asyncio.Lock()
        self._transport_ready_event = asyncio.Event()
        self._cached_owner_dm_channel: Optional[str] = None  # Cache DM channel ID (legacy fallback)

    def _lang(self) -> str:
        config = getattr(self.controller, "config", None)
        return str(getattr(config, "language", "en") or "en")

    def _t(self, key: str, **kwargs: Any) -> str:
        return i18n_t(key, self._lang(), **kwargs)

    def start(self) -> None:
        """Start the periodic update checker."""
        if self._running:
            return

        # Initialize last_activity_at if not set (for idle detection baseline)
        if not self.state.last_activity_at:
            self.state.last_activity_at = time.time()
            self.state.save()

        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info(
            f"Update checker started (interval={self.config.check_interval_minutes}min, "
            f"auto_update={self.config.auto_update}, idle_minutes={self.config.idle_minutes})"
        )

    def stop(self) -> Optional[asyncio.Task]:
        """Stop the periodic update checker and return the cancelled task, if any."""
        self._running = False
        task = self._check_task
        if task:
            task.cancel()
        return task

    async def wait_stopped(self, task: Optional[asyncio.Task] = None) -> None:
        """Wait for the periodic checker task to finish after cancellation."""
        task = task or self._check_task
        if not task:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._check_task is task:
                self._check_task = None

    def record_activity(self) -> None:
        """Record user activity (called when a Slack message is received)."""
        self.state.last_activity_at = time.time()
        self.state.save()  # save() has its own try/except, won't raise

    def notify_transport_ready(self, _platform: str) -> None:
        """Wake a deferred check when an IM transport becomes available."""
        if self._running:
            self._transport_ready_event.set()

    def _reload_config(self) -> None:
        """Reload UpdateConfig from config file (for hot-reload support)."""
        try:
            config_path = paths.get_config_path()
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                update_data = data.get("update") or {}
                # Backward compat: rename legacy "notify_slack" → "notify_admins"
                if "notify_slack" in update_data and "notify_admins" not in update_data:
                    update_data["notify_admins"] = update_data.pop("notify_slack")
                valid = {f.name for f in fields(UpdateConfig)}
                self.config = UpdateConfig(**{k: v for k, v in update_data.items() if k in valid})
        except Exception as e:
            logger.warning(f"Failed to reload update config: {e}")

    def _clear_blocked_auto_update(self) -> None:
        if (
            self.state.blocked_auto_update_version is None
            and self.state.blocked_auto_update_reason is None
            and self.state.blocked_auto_update_at is None
            and self.state.blocked_auto_update_current_version is None
        ):
            return
        self.state.blocked_auto_update_version = None
        self.state.blocked_auto_update_reason = None
        self.state.blocked_auto_update_at = None
        self.state.blocked_auto_update_current_version = None
        self.state.save()

    def _clear_blocked_auto_update_if_resolved(self, latest: Optional[str], *, has_update: bool) -> None:
        blocked = self.state.blocked_auto_update_version
        if not blocked:
            return
        if has_update and latest == blocked:
            return
        logger.info("Clearing blocked auto-update state for %s", blocked)
        self._clear_blocked_auto_update()

    def _block_auto_update(self, target_version: str, reason: str, *, current_version: Optional[str] = None) -> None:
        self.state.blocked_auto_update_version = target_version
        self.state.blocked_auto_update_reason = reason
        self.state.blocked_auto_update_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.state.blocked_auto_update_current_version = current_version
        self.state.save()

    def _blocked_auto_update_reason_for(self, target_version: str) -> Optional[str]:
        if self.state.blocked_auto_update_version != target_version:
            return None
        return self.state.blocked_auto_update_reason or "unknown"

    def _supports_unattended_self_update(self, current_version: str) -> tuple[bool, Optional[str]]:
        from vibe import runtime

        service_main_path = runtime.get_service_main_path()
        if service_main_path.name == "main.py":
            return False, f"service is running from source checkout at {service_main_path}"

        normalized_version = str(current_version or "").strip()
        if not _STABLE_PACKAGE_VERSION_RE.fullmatch(normalized_version):
            return False, f"running version {normalized_version or 'unknown'} is not a packaged stable release"

        if not get_running_vibe_path():
            return False, "current vibe executable path is unavailable"

        return True, None

    def _running_version_satisfies_target(self, running_version: str, target_version: str) -> bool:
        running = str(running_version or "").strip()
        target = str(target_version or "").strip()
        if not running or not target:
            return False
        return running == target or has_newer_version(running, target)

    async def _check_loop(self) -> None:
        """Main loop for periodic update checking."""
        # Initial delay to let the service fully start
        await asyncio.sleep(30)

        while self._running:
            self._transport_ready_event.clear()
            try:
                await self._do_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Update check failed: {e}", exc_info=True)

            # Reload config and get interval (with minimum bound to prevent tight loop)
            self._reload_config()
            configured_interval = (
                self.config.check_interval_minutes
                if self.config.check_interval_minutes > 0
                else MANAGED_DEPENDENCY_CHECK_INTERVAL_MINUTES
            )
            interval = max(configured_interval, MIN_CHECK_INTERVAL_MINUTES)

            # Even when product self-update checks are disabled, the loop stays
            # alive for managed local dependencies such as askill. Clamp to the
            # minimum so hot-reload can re-enable product checks too.
            try:
                await asyncio.wait_for(self._transport_ready_event.wait(), timeout=interval * 60)
            except asyncio.TimeoutError:
                pass

    async def _do_check(self) -> None:
        """Perform a single update check."""
        try:
            # Reload config for hot-reload support (e.g., user toggled auto_update in UI)
            self._reload_config()
            await self._reconcile_managed_dependencies()

            # Skip if disabled
            if self.config.check_interval_minutes <= 0:
                return

            # Fetch version info in a thread to avoid blocking the event loop
            version_info = await asyncio.to_thread(_fetch_pypi_version_sync)

            self.state.last_check_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.state.save()

            if version_info.get("error"):
                logger.warning(f"Failed to check for updates: {version_info['error']}")
                return

            self._clear_blocked_auto_update_if_resolved(
                version_info.get("latest") if version_info.get("has_update") else None,
                has_update=bool(version_info.get("has_update")),
            )

            if not version_info.get("has_update"):
                logger.debug(f"No update available (current={version_info['current']})")
                return

            latest = version_info["latest"]
            current = version_info["current"]
            logger.info(f"Update available: {current} -> {latest}")
            release_notifications_enabled: Optional[bool] = None
            unattended_supported, unattended_reason = self._supports_unattended_self_update(current)

            # Notification flow — failure must not block auto-update
            if self.config.notify_admins and self.state.notified_version != latest:
                release_notifications_enabled = await self._should_send_release_notifications(
                    latest,
                    cached=release_notifications_enabled,
                )
                if release_notifications_enabled:
                    configured_admin_ids = self._get_admin_user_ids()
                    admin_ids = self._active_admin_ids(configured_admin_ids)
                    platform = getattr(self.controller.config, "platform", "slack")
                    if admin_ids:
                        waiting_for_transport = self._notification_targets_waiting_for_transport(admin_ids, platform)
                    elif configured_admin_ids:
                        waiting_for_transport = False
                    else:
                        waiting_for_transport = self._notification_targets_waiting_for_transport([], platform)
                    if waiting_for_transport:
                        logger.info(
                            "Deferring update notification and auto-update for %s until an admin transport is ready",
                            latest,
                        )
                        return
                    delivered = False
                    try:
                        delivered = await self._send_update_notification(current, latest)
                    except Exception as e:
                        logger.error(f"Failed to send update notification: {e}", exc_info=True)
                    if delivered:
                        self.state.notified_version = latest
                        self.state.notified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    else:
                        logger.warning(
                            "Update notification for %s was not delivered; skipping grace period",
                            latest,
                        )
                    self.state.save()

            # Auto-update flow — respect a grace period after successful notification
            # so the admin has time to read the notification before auto-update kicks in.
            if self.config.auto_update and self._is_idle():
                blocked_reason = self._blocked_auto_update_reason_for(latest)
                if blocked_reason:
                    logger.warning(
                        "Skipping auto-update to %s; previous attempt is blocked: %s",
                        latest,
                        blocked_reason,
                    )
                    return
                if not unattended_supported:
                    logger.warning("Skipping unattended self-update for %s: %s", latest, unattended_reason)
                    return
                release_notifications = await self._should_send_release_notifications(
                    latest,
                    cached=release_notifications_enabled,
                )
                if release_notifications and self._within_notification_grace_period(latest):
                    logger.info("Within notification grace period, deferring auto-update")
                else:
                    logger.info("System is idle, performing auto-update...")
                    update_kwargs = {}
                    if not release_notifications:
                        update_kwargs["suppress_post_update_notification"] = True
                    result = await self._perform_update(latest, **update_kwargs)
                    if not result.get("ok"):
                        logger.warning(
                            "Auto-update to %s failed and will be retried on a later check: %s",
                            latest,
                            result.get("message") or "unknown",
                        )
                    elif not result.get("restarting"):
                        await self._send_post_update_failure_notification(
                            target_version=str(latest),
                            running_version=str(current),
                        )
                        self._block_auto_update(
                            latest,
                            "restart_not_scheduled",
                            current_version=current,
                        )
        except Exception as e:
            logger.error(f"Update check failed: {e}", exc_info=True)

    async def _reconcile_managed_dependencies(self) -> None:
        """Keep required local dependencies current on the shared update cadence."""
        try:
            from vibe import api

            result = await asyncio.to_thread(api.reconcile_askill_auto_update)
            if not result.get("ok"):
                logger.warning("askill managed dependency reconcile failed: %s", result.get("message") or result)
            elif result.get("skipped"):
                logger.debug("askill managed dependency reconcile skipped: %s", result.get("reason"))
            else:
                logger.info("askill managed dependency reconcile completed: %s", result.get("action") or "updated")
        except Exception as exc:  # noqa: BLE001
            logger.warning("askill managed dependency reconcile raised: %s", exc, exc_info=True)

    async def _get_version_info_async(self) -> Dict[str, Any]:
        """Get version info asynchronously."""
        return await asyncio.to_thread(_fetch_pypi_version_sync)

    async def _should_send_release_notifications(self, latest: str, cached: Optional[bool] = None) -> bool:
        """Return whether update notifications should be sent for the release."""
        if cached is not None:
            return cached

        policy_info = await asyncio.to_thread(_fetch_update_notification_policy_sync, latest)
        if policy_info.get("error"):
            logger.warning(
                "Failed to check update notification policy for %s: %s",
                latest,
                policy_info["error"],
            )
            return True

        policy = policy_info.get("policy") or UPDATE_NOTIFICATION_POLICY_DEFAULT
        enabled = policy != UPDATE_NOTIFICATION_POLICY_NONE
        if not enabled:
            logger.info("Update notifications suppressed by release metadata for %s", latest)
        return enabled

    def _is_idle(self) -> bool:
        """Check if the system is idle (no active sessions and no recent activity)."""
        # Check for active agent sessions
        if self._has_active_sessions():
            logger.debug("Not idle: has active sessions")
            return False

        # Check for recent activity
        # If no activity recorded yet, consider it NOT idle (just started)
        if not self.state.last_activity_at:
            logger.debug("Not idle: no activity recorded yet (service just started)")
            return False

        idle_seconds = time.time() - self.state.last_activity_at
        idle_minutes = idle_seconds / 60
        if idle_minutes < self.config.idle_minutes:
            logger.debug(f"Not idle: last activity {idle_minutes:.1f} minutes ago")
            return False

        return True

    def _within_notification_grace_period(self, target_version: str) -> bool:
        """Check if we're still within the grace period after sending an update notification.

        Returns True if a notification for *this version* was successfully delivered
        recently and we should defer auto-update to give the admin time to read it.
        """
        if not self.state.notified_at:
            return False
        # Only apply grace for the version we actually notified about
        if self.state.notified_version != target_version:
            return False
        try:
            notified_ts = calendar.timegm(time.strptime(self.state.notified_at, "%Y-%m-%dT%H:%M:%SZ"))
            elapsed_minutes = (time.time() - notified_ts) / 60
            return elapsed_minutes < NOTIFICATION_GRACE_PERIOD_MINUTES
        except (ValueError, OverflowError) as e:
            logger.warning(f"Failed to parse notified_at '{self.state.notified_at}': {e}")
            return False

    def _has_active_sessions(self) -> bool:
        """Check if any agent has active sessions."""
        try:
            # Check OpenCode active polls
            if hasattr(self.controller, "sessions"):
                active_polls = self.controller.sessions.get_all_active_polls()
                if active_polls:
                    return True

            # Check Claude sessions
            if hasattr(self.controller, "claude_sessions") and self.controller.claude_sessions:
                return True

            # Check Codex active processes
            if hasattr(self.controller, "agent_service"):
                codex = self.controller.agent_service.agents.get("codex")
                if codex and hasattr(codex, "active_processes") and codex.active_processes:
                    return True
        except Exception as e:
            logger.warning(f"Error checking active sessions: {e}")

        return False

    def _get_admin_user_ids(self) -> list:
        """Get admin user IDs from the settings store.

        Returns scoped admin user IDs across all enabled platforms.
        """
        try:
            if hasattr(self.controller, "settings_manager"):
                store = self.controller.settings_manager.get_store()
                if store:
                    return list(store.get_admins().keys())
        except Exception as e:
            logger.warning(f"Failed to get admin user IDs: {e}")
        return []

    def _get_im_client_for_platform(self, platform: str):
        im_clients = getattr(self.controller, "im_clients", None)
        if isinstance(im_clients, dict) and platform in im_clients:
            return im_clients[platform]
        return self.controller.im_client

    def _get_im_client_for_user(self, user_id: str):
        scoped_platform, raw_user_id = _split_scoped_key(str(user_id))
        platform = scoped_platform or _infer_user_platform(raw_user_id)
        return self._get_im_client_for_platform(platform), raw_user_id, platform

    @staticmethod
    def _user_platform(user_id: str) -> str:
        scoped_platform, raw_user_id = _split_scoped_key(str(user_id))
        return scoped_platform or _infer_user_platform(raw_user_id)

    def _is_transport_ready(self, platform: str) -> bool:
        checker = getattr(self.controller, "is_im_transport_ready", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(platform))
        except Exception as e:
            logger.warning("Failed to read %s transport readiness: %s", platform, e)
            return False

    def _admin_ids_for_platform(self, admin_ids: list, platform: str) -> list:
        return [uid for uid in admin_ids if self._user_platform(uid) == platform]

    def _active_admin_ids(self, admin_ids: Optional[list] = None) -> list:
        admin_ids = self._get_admin_user_ids() if admin_ids is None else admin_ids
        im_clients = getattr(self.controller, "im_clients", None)
        if isinstance(im_clients, dict) and im_clients:
            enabled_platforms = set(im_clients)
        else:
            enabled = getattr(getattr(self.controller, "config", None), "enabled_platforms", None)
            enabled_platforms = set(enabled()) if callable(enabled) else set()
        if not enabled_platforms:
            return admin_ids
        return [uid for uid in admin_ids if self._user_platform(uid) in enabled_platforms]

    def _notification_targets_waiting_for_transport(self, admin_ids: list, platform: str) -> bool:
        if admin_ids:
            return any(not self._is_transport_ready(self._user_platform(uid)) for uid in admin_ids)
        if platform == "discord" and not self._get_default_notification_channel_id("discord"):
            return False
        if platform in {"slack", "discord"}:
            return not self._is_transport_ready(platform)
        return False

    async def _get_workspace_owner_id(self) -> Optional[str]:
        """Get the Slack workspace primary owner's user ID.

        Legacy fallback: used only when no admins are configured.
        """
        try:
            im_client = self._get_im_client_for_platform("slack")
            if not im_client or not hasattr(im_client, "web_client"):
                return None

            # Paginate through users to handle large workspaces
            cursor = None
            while True:
                kwargs = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor

                response = await im_client.web_client.users_list(**kwargs)
                if not response.get("ok"):
                    return None

                for member in response.get("members", []):
                    if member.get("is_primary_owner"):
                        return member.get("id")

                # Check for next page
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

            # Second pass: fallback to any owner if no primary owner found
            cursor = None
            while True:
                kwargs = {"limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor

                response = await im_client.web_client.users_list(**kwargs)
                if not response.get("ok"):
                    return None

                for member in response.get("members", []):
                    if member.get("is_owner"):
                        return member.get("id")

                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        except Exception as e:
            logger.warning(f"Failed to get workspace owner: {e}")

        return None

    async def _open_dm_channel(self, user_id: str) -> Optional[str]:
        """Open a DM channel with a user and return the channel ID."""
        # Use cached channel if available
        if self._cached_owner_dm_channel:
            return self._cached_owner_dm_channel

        try:
            im_client = self._get_im_client_for_platform("slack")
            if not im_client or not hasattr(im_client, "web_client"):
                return None

            response = await im_client.web_client.conversations_open(users=[user_id])
            if response.get("ok"):
                channel_id = response.get("channel", {}).get("id")
                self._cached_owner_dm_channel = channel_id
                return channel_id
        except Exception as e:
            logger.warning(f"Failed to open DM channel with user {user_id}: {e}")

        return None

    async def _send_update_notification(self, current: str, latest: str) -> bool:
        """Send update notification to admin users, with platform-specific fallbacks."""
        platform = getattr(self.controller.config, "platform", "slack")
        configured_admin_ids = self._get_admin_user_ids()
        admin_ids = self._active_admin_ids(configured_admin_ids)

        if configured_admin_ids:
            # Send DM to each admin via the platform-agnostic send_dm method
            if not admin_ids or self._notification_targets_waiting_for_transport(admin_ids, platform):
                return False
            return await self._send_notification_to_admins(admin_ids, current, latest, platform)
        else:
            # Legacy fallback: no admins configured
            if self._notification_targets_waiting_for_transport([], platform):
                return False
            if platform == "slack":
                return await self._send_slack_notification_legacy(current, latest)
            elif platform == "discord":
                return await self._send_discord_notification_fallback(current, latest)
            else:
                logger.warning("Update notification skipped: no admins and unsupported platform %s", platform)
                return False

    async def _send_post_update_failure_notification(
        self,
        *,
        target_version: str,
        running_version: str,
        channel_id: Optional[str] = None,
        message_id: Optional[str] = None,
        platform: Optional[str] = None,
        admin_platform: Optional[str] = None,
    ) -> bool:
        """Notify admins that an installed update did not become the active runtime."""
        resolved_platform = platform or getattr(self.controller.config, "platform", "slack")
        if channel_id:
            resolved_platform = platform or _infer_channel_platform(channel_id)

        failure_text = self._t(
            "update.postUpdateVersionMismatch",
            target=target_version,
            current=running_version or "unknown",
        )

        try:
            im_client = self._get_im_client_for_platform(resolved_platform)
            if channel_id and message_id and resolved_platform == "slack":
                result = await im_client.web_client.chat_update(channel=channel_id, ts=message_id, text=failure_text)
                if self._delivery_succeeded(result):
                    logger.info("Updated original message with post-update failure notification")
                    return True
                return False
            if channel_id and message_id:
                context = MessageContext(user_id="system", channel_id=channel_id, platform=resolved_platform)
                result = await im_client.edit_message(context, message_id, text=failure_text)
                if self._delivery_succeeded(result):
                    logger.info("Updated %s message with post-update failure notification", resolved_platform)
                    return True
                return False

            configured_admin_ids = self._get_admin_user_ids()
            active_admin_ids = self._active_admin_ids(configured_admin_ids)
            admin_ids = (
                self._admin_ids_for_platform(active_admin_ids, admin_platform)
                if admin_platform
                else active_admin_ids
            )
            delivered = False
            if admin_ids:
                delivered = bool(
                    await self._send_admin_text(
                        admin_ids,
                        failure_text,
                        log_label="post-update failure notification",
                    )
                )
            elif not configured_admin_ids and resolved_platform == "slack":
                owner_id = await self._get_workspace_owner_id()
                if owner_id:
                    dm_channel = await self._open_dm_channel(owner_id)
                    if dm_channel:
                        result = await im_client.web_client.chat_postMessage(channel=dm_channel, text=failure_text)
                        delivered = self._delivery_succeeded(result)
                        if delivered:
                            logger.info("Sent post-update failure notification to %s", owner_id)
            elif not configured_admin_ids and resolved_platform == "discord":
                channel_id = self._get_default_notification_channel_id("discord")
                if channel_id:
                    context = MessageContext(user_id="system", channel_id=channel_id, platform="discord")
                    result = await im_client.send_message(context, failure_text)
                    delivered = self._delivery_succeeded(result)
                    if delivered:
                        logger.info("Sent post-update failure notification to Discord channel %s", channel_id)
            return delivered
        except Exception as e:
            logger.error("Failed to send post-update failure notification: %s", e)
            return False

    async def _send_admin_text(self, admin_ids: list, text: str, *, log_label: str) -> set[str]:
        delivered_platforms: set[str] = set()
        for uid in admin_ids:
            try:
                admin_client, raw_user_id, user_platform = self._get_im_client_for_user(uid)
                result = await admin_client.send_dm(raw_user_id, text)
                if self._delivery_succeeded(result):
                    delivered_platforms.add(user_platform)
                    logger.info("Sent %s to admin %s", log_label, uid)
            except Exception as e:
                logger.error("Failed to send %s to admin %s: %s", log_label, uid, e)
        return delivered_platforms

    @staticmethod
    def _delivery_succeeded(result: Any) -> bool:
        if result is None:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict) and "ok" in result:
            return bool(result["ok"])
        return True

    async def _send_notification_to_admins(self, admin_ids: list, current: str, latest: str, platform: str) -> bool:
        """Send update notification DM to each admin user."""
        delivered = False
        for uid in admin_ids:
            im_client, raw_user_id, user_platform = self._get_im_client_for_user(uid)
            try:
                if user_platform == "slack":
                    release_url = _github_release_url(latest)
                    latest_link = _format_release_version_link(latest, "slack")
                    text = self._t("update.availablePlain", current=current, latest=latest, releaseUrl=release_url)
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": self._t(
                                    "update.availableSlack",
                                    current=current,
                                    latestLink=latest_link,
                                ),
                            },
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Update Now", "emoji": True},
                                    "style": "primary",
                                    "action_id": UPDATE_BUTTON_ACTION_ID,
                                    "value": latest,
                                }
                            ],
                        },
                    ]
                    result = await im_client.send_dm(raw_user_id, text, blocks=blocks)
                else:
                    text = self._format_update_notification_text(current, latest, user_platform)
                    kwargs: dict[str, Any] = {}
                    if self._supports_admin_update_button(user_platform):
                        kwargs["keyboard"] = self._build_update_keyboard(latest)
                        kwargs["parse_mode"] = "markdown"
                    result = await im_client.send_dm(raw_user_id, text, **kwargs)

                if result:
                    delivered = True
                    logger.info(f"Sent update notification to admin {uid}")
                else:
                    logger.warning(f"Failed to send update notification to admin {uid}: send_dm returned None")
            except Exception as e:
                logger.error(f"Failed to send update notification to admin {uid}: {e}")
        return delivered

    def _supports_admin_update_button(self, platform: str) -> bool:
        """Return whether admin update DMs should include an update button."""
        return platform not in {"wechat", "unknown"}

    def _build_update_keyboard(self, latest: str) -> InlineKeyboard:
        return InlineKeyboard(buttons=[[InlineButton(text=self._t("update.actionUpdateNow"), callback_data=f"vibe_update_now:{latest}")]])

    def _format_update_notification_text(self, current: str, latest: str, platform: str) -> str:
        latest_link = _format_release_version_link(
            latest,
            "plain" if platform in {"wechat", "unknown"} else "markdown",
        )
        if platform == "discord":
            return self._t("update.availableDiscord", current=current, latestLink=latest_link)
        if platform in {"telegram", "lark"}:
            return self._t("update.availableMarkdown", current=current, latestLink=latest_link)
        return self._t("update.availableText", current=current, latestLink=latest_link)

    async def _send_slack_notification_legacy(self, current: str, latest: str) -> bool:
        """Legacy Slack notification: send to workspace owner when no admins configured."""
        owner_id = await self._get_workspace_owner_id()
        if not owner_id:
            logger.warning("Cannot send update notification: no admins and no workspace owner found")
            return False

        # Open DM channel first (required for sending messages to users)
        dm_channel = await self._open_dm_channel(owner_id)
        if not dm_channel:
            logger.warning(f"Cannot send update notification: failed to open DM with {owner_id}")
            return False

        try:
            im_client = self._get_im_client_for_platform("slack")
            release_url = _github_release_url(latest)
            latest_link = _format_release_version_link(latest, "slack")
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": self._t(
                            "update.availableSlack",
                            current=current,
                            latestLink=latest_link,
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": self._t("update.actionUpdateNow"), "emoji": True},
                            "style": "primary",
                            "action_id": UPDATE_BUTTON_ACTION_ID,
                            "value": latest,
                        }
                    ],
                },
            ]

            await im_client.web_client.chat_postMessage(
                channel=dm_channel,
                text=self._t("update.availablePlain", current=current, latest=latest, releaseUrl=release_url),
                blocks=blocks,
            )
            logger.info(f"Sent update notification to workspace owner {owner_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send update notification: {e}")
            return False

    async def _send_discord_notification_fallback(self, current: str, latest: str) -> bool:
        """Fallback Discord notification: send to first enabled channel when no admins configured."""
        channel_id = self._get_default_notification_channel_id()
        if not channel_id:
            logger.warning("Cannot send update notification: no admins and no enabled channel found")
            return False
        try:
            from modules.im import InlineButton, InlineKeyboard, MessageContext

            text = self._format_update_notification_text(current, latest, "discord")
            keyboard = InlineKeyboard(
                buttons=[[InlineButton(text="Update Now", callback_data=f"vibe_update_now:{latest}")]]
            )
            context = MessageContext(user_id="system", channel_id=channel_id, platform="discord")
            message_id = await self.controller.get_im_client_for_context(context).send_message_with_buttons(
                context, text, keyboard, parse_mode="markdown"
            )
            logger.info("Sent update notification to Discord channel %s", channel_id)
            return bool(message_id)
        except Exception as e:
            logger.error(f"Failed to send Discord update notification: {e}")
            return False

    def _get_default_notification_channel_id(self, platform: Optional[str] = None) -> Optional[str]:
        if not hasattr(self.controller, "settings_manager"):
            return None
        store = self.controller.settings_manager.get_store()
        if store is None:
            return None
        platform = platform or getattr(self.controller.config, "platform", "slack")
        for channel_id, settings in store.get_channels_for_platform(platform).items():
            if getattr(settings, "enabled", False):
                if platform == "discord" and not str(channel_id).isdigit():
                    continue
                return str(channel_id)
        return None

    async def handle_update_button_click(self, context: MessageContext, target_version: Optional[str] = None) -> None:
        """Handle update button click for non-Slack platforms."""
        im_client = self.controller.get_im_client_for_context(context)
        message_id = context.message_id
        if not message_id:
            await im_client.send_message(context, self._t("update.actionUnavailable"))
            return
        if self._upgrade_lock.locked():
            await im_client.edit_message(
                context,
                context.message_id,
                text=self._t("update.alreadyInProgress"),
            )
            return

        if not target_version:
            version_info = await self._get_version_info_async()
            if version_info.get("error"):
                await im_client.edit_message(context, message_id, text=self._t("update.checkFailed"))
                return
            if not version_info.get("has_update"):
                await im_client.edit_message(context, message_id, text=self._t("update.alreadyUpToDate"))
                return
            target_version = version_info.get("latest")
            if not target_version:
                await im_client.edit_message(
                    context, message_id, text=self._t("update.infoUnavailable")
                )
                return

        await im_client.edit_message(context, message_id, text=self._t("update.updating"))
        platform = context.platform or (context.platform_specific or {}).get("platform")
        result = await self._perform_update(
            target_version,
            channel_id=context.channel_id,
            message_id=message_id,
            platform=platform,
        )
        if not result.get("ok"):
            await im_client.edit_message(context, message_id, text=self._t("update.upgradeFailed"))
        elif not result.get("restarting"):
            await im_client.edit_message(context, message_id, text=self._t("update.restartRequired"))

    async def _perform_update(
        self,
        target_version: str,
        channel_id: Optional[str] = None,
        message_id: Optional[str] = None,
        platform: Optional[str] = None,
        suppress_post_update_notification: bool = False,
    ) -> Dict[str, Any]:
        """Perform the actual update and restart. Returns do_upgrade result dict."""
        # Prevent concurrent upgrades
        if self._upgrade_lock.locked():
            logger.warning("Upgrade already in progress, skipping")
            return {
                "ok": False,
                "message": "Upgrade already in progress",
                "output": None,
                "restarting": False,
            }

        async with self._upgrade_lock:
            logger.info(f"Starting auto-update to version {target_version}")

            # Run upgrade in thread to avoid blocking event loop
            from vibe.api import do_upgrade

            result = await asyncio.to_thread(do_upgrade, True)

            if result["ok"]:
                logger.info(f"Upgrade successful: {result['message']}")
                if result.get("restarting"):
                    # Write marker only if restart is scheduled
                    if suppress_post_update_notification:
                        logger.info("Post-update notification suppressed for %s", target_version)
                    self._write_update_marker(
                        target_version,
                        channel_id=channel_id,
                        message_id=message_id,
                        platform=platform,
                        suppress_success_notification=suppress_post_update_notification,
                    )
                else:
                    logger.warning("Upgrade completed without restart; manual restart required")
                return result
            else:
                logger.error(f"Upgrade failed: {result['message']}")
                if result.get("output"):
                    logger.error(f"Output: {result['output']}")
                self._remove_update_marker()
                return result

    def _write_update_marker(
        self,
        version: str,
        channel_id: Optional[str] = None,
        message_id: Optional[str] = None,
        platform: Optional[str] = None,
        suppress_success_notification: bool = False,
    ) -> None:
        """Write a marker file to trigger post-update notification."""
        try:
            data = {
                "version": version,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if suppress_success_notification:
                data["suppress_success_notification"] = True
            # Store message coordinates for updating the original message after restart
            if channel_id and message_id:
                data["channel_id"] = channel_id
                data["message_id"] = message_id
            if platform:
                data["platform"] = platform
            self._write_update_marker_payload(data)
        except Exception as e:
            logger.error(f"Failed to write update marker: {e}")

    @staticmethod
    def _write_update_marker_payload(data: dict[str, Any]) -> None:
        marker_path = paths.get_state_dir() / "pending_update_notification.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=marker_path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            temp_path = Path(f.name)
        temp_path.replace(marker_path)

    def _record_post_update_admin_progress(
        self,
        *,
        marker_path: Path,
        data: dict[str, Any],
        active_platforms: set[str],
        handled_platforms: set[str],
        delivered_platforms: set[str],
    ) -> bool:
        handled_platforms.update(delivered_platforms)
        if active_platforms.issubset(handled_platforms):
            marker_path.unlink(missing_ok=True)
            return True
        if delivered_platforms:
            data["handled_admin_platforms"] = sorted(handled_platforms)
            self._write_update_marker_payload(data)
        logger.info(
            "Post-update notification is waiting for admin transport(s): %s",
            ", ".join(sorted(active_platforms - handled_platforms)),
        )
        return False

    def _remove_update_marker(self) -> None:
        """Remove the update marker file."""
        try:
            marker_path = paths.get_state_dir() / "pending_update_notification.json"
            marker_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to remove update marker: {e}")

    async def check_and_send_post_update_notification(self, *, ready_platform: Optional[str] = None) -> bool:
        """Check for pending update notification and send it (called on startup)."""
        async with self._post_update_lock:
            return await self._check_and_send_post_update_notification(ready_platform=ready_platform)

    async def _check_and_send_post_update_notification(self, *, ready_platform: Optional[str] = None) -> bool:
        marker_path = paths.get_state_dir() / "pending_update_notification.json"
        if not marker_path.exists():
            return False

        try:
            from vibe import __version__

            data = json.loads(marker_path.read_text(encoding="utf-8"))
            channel_id = data.get("channel_id")
            message_id = data.get("message_id")
            platform = data.get("platform") or getattr(self.controller.config, "platform", "slack")
            if channel_id:
                platform = data.get("platform") or _infer_channel_platform(channel_id)
            elif ready_platform is not None:
                platform = ready_platform
            if channel_id and ready_platform is not None and ready_platform != platform:
                return False
            configured_admin_ids: list = []
            active_admin_ids: list = []
            active_admin_platforms: set[str] = set()
            handled_admin_platforms: set[str] = set()
            attempt_admin_ids: list = []
            if not channel_id:
                configured_admin_ids = self._get_admin_user_ids()
                active_admin_ids = self._active_admin_ids(configured_admin_ids)
                active_admin_platforms = {self._user_platform(uid) for uid in active_admin_ids}
                stored_handled_platforms = data.get("handled_admin_platforms", [])
                if not isinstance(stored_handled_platforms, list):
                    stored_handled_platforms = []
                handled_admin_platforms = {
                    str(value)
                    for value in stored_handled_platforms
                    if isinstance(value, str)
                } & active_admin_platforms
                pending_platforms = active_admin_platforms - handled_admin_platforms
                if ready_platform is not None:
                    pending_platforms &= {ready_platform}
                else:
                    pending_platforms = {
                        candidate for candidate in pending_platforms if self._is_transport_ready(candidate)
                    }
                attempt_admin_ids = [
                    uid for uid in active_admin_ids if self._user_platform(uid) in pending_platforms
                ]
                if not configured_admin_ids:
                    platform = data.get("platform") or getattr(self.controller.config, "platform", "slack")
                    if ready_platform is not None and ready_platform != platform:
                        return False
            # Use the target version from marker (more reliable than __version__ in edge cases)
            target_version = data.get("version", "unknown")
            running_version = str(__version__ or "").strip()
            if not self._running_version_satisfies_target(running_version, str(target_version)):
                logger.warning(
                    "Suppressing post-update success notification: expected %s but still running %s",
                    target_version,
                    running_version or "unknown",
                )
                self._block_auto_update(
                    str(target_version),
                    "post_update_version_mismatch",
                    current_version=running_version or None,
                )
                if not channel_id and configured_admin_ids:
                    failure_text = self._t(
                        "update.postUpdateVersionMismatch",
                        target=str(target_version),
                        current=running_version or "unknown",
                    )
                    delivered_platforms = await self._send_admin_text(
                        attempt_admin_ids,
                        failure_text,
                        log_label="post-update failure notification",
                    )
                    return self._record_post_update_admin_progress(
                        marker_path=marker_path,
                        data=data,
                        active_platforms=active_admin_platforms,
                        handled_platforms=handled_admin_platforms,
                        delivered_platforms=delivered_platforms,
                    )
                delivered = await self._send_post_update_failure_notification(
                    target_version=str(target_version),
                    running_version=running_version or "unknown",
                    channel_id=channel_id,
                    message_id=message_id,
                    platform=platform,
                    admin_platform=ready_platform if not channel_id else None,
                )
                if delivered:
                    marker_path.unlink(missing_ok=True)
                elif not channel_id and not configured_admin_ids:
                    no_target = platform not in {"slack", "discord"} or (
                        platform == "discord" and not self._get_default_notification_channel_id("discord")
                    )
                    if no_target:
                        logger.info("Discarding post-update marker with no viable %s notification target", platform)
                        marker_path.unlink(missing_ok=True)
                        return True
                return delivered

            if self.state.blocked_auto_update_version == target_version:
                self._clear_blocked_auto_update()
            if data.get("suppress_success_notification"):
                logger.info("Post-update success notification suppressed for %s", target_version)
                marker_path.unlink(missing_ok=True)
                return True
            im_client = self._get_im_client_for_platform(platform)
            success_text = self._t("update.updated", version=target_version)
            success_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": self._t("update.updatedSlack", version=target_version),
                    },
                }
            ]

            # If we have original message coordinates, update that message
            delivered = False
            if channel_id and message_id and platform == "slack":
                result = await im_client.web_client.chat_update(
                    channel=channel_id,
                    ts=message_id,
                    text=success_text,
                    blocks=success_blocks,
                )
                delivered = self._delivery_succeeded(result)
                if delivered:
                    logger.info("Updated original message with post-update notification")
            elif channel_id and message_id:
                try:
                    context = MessageContext(user_id="system", channel_id=channel_id, platform=platform)
                    result = await im_client.edit_message(context, message_id, text=success_text)
                    delivered = self._delivery_succeeded(result)
                    if delivered:
                        logger.info("Updated %s message with post-update notification", platform)
                except Exception as e:
                    logger.error("Failed to edit %s update message: %s", platform, e)
            elif configured_admin_ids:
                delivered_platforms = await self._send_admin_text(
                    attempt_admin_ids,
                    success_text,
                    log_label="post-update notification",
                )
                return self._record_post_update_admin_progress(
                    marker_path=marker_path,
                    data=data,
                    active_platforms=active_admin_platforms,
                    handled_platforms=handled_admin_platforms,
                    delivered_platforms=delivered_platforms,
                )
            else:
                # Legacy fallback: try workspace owner
                if platform == "slack":
                    owner_id = await self._get_workspace_owner_id()
                    if owner_id:
                        dm_channel = await self._open_dm_channel(owner_id)
                        if dm_channel:
                            result = await im_client.web_client.chat_postMessage(
                                channel=dm_channel,
                                text=success_text,
                                blocks=success_blocks,
                            )
                            delivered = self._delivery_succeeded(result)
                            if delivered:
                                logger.info(f"Sent post-update notification to {owner_id}")
                elif platform == "discord":
                    channel_id = self._get_default_notification_channel_id("discord")
                    if not channel_id:
                        logger.info("Discarding post-update marker with no viable Discord notification target")
                        marker_path.unlink(missing_ok=True)
                        return True
                    context = MessageContext(user_id="system", channel_id=channel_id, platform="discord")
                    result = await im_client.send_message(context, success_text)
                    delivered = self._delivery_succeeded(result)
                    if delivered:
                        logger.info("Sent post-update notification to Discord channel %s", channel_id)
                else:
                    logger.info("Discarding post-update marker with no viable %s notification target", platform)
                    marker_path.unlink(missing_ok=True)
                    return True
            if delivered:
                marker_path.unlink(missing_ok=True)
            else:
                logger.warning("Post-update notification for %s was not delivered; retaining marker", target_version)
            return delivered
        except Exception as e:
            logger.error(f"Failed to send post-update notification: {e}")
            return False


async def handle_update_button_click(controller: "Controller", payload: Dict[str, Any]) -> None:
    """Handle the 'Update Now' button click from Slack.

    This function should return quickly to avoid Slack ack timeout.
    The actual update is performed in a background task.
    """
    channel_id = payload.get("channel", {}).get("id")
    message_id = payload.get("message", {}).get("ts")
    im_client = (
        controller._get_im_client_for_platform("slack")
        if hasattr(controller, "_get_im_client_for_platform")
        else controller.im_client
    )
    lang = str(getattr(getattr(controller, "config", None), "language", "en") or "en")

    # Check if upgrade is already in progress
    if hasattr(controller, "update_checker") and controller.update_checker._upgrade_lock.locked():
        try:
            await im_client.web_client.chat_update(
                channel=channel_id,
                ts=message_id,
                text=i18n_t("update.alreadyInProgressShort", lang),
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": i18n_t("update.alreadyInProgressWarning", lang)},
                    }
                ],
            )
        except Exception as e:
            logger.error(f"Failed to update message: {e}")
        return

    # Update message immediately to acknowledge the click
    try:
        await im_client.web_client.chat_update(
            channel=channel_id,
            ts=message_id,
            text=i18n_t("update.updating", lang),
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": i18n_t("update.updatingSlack", lang),
                    },
                }
            ],
        )
    except Exception as e:
        logger.error(f"Failed to acknowledge button click: {e}")
        return

    # Schedule the actual update in a background task to avoid blocking
    asyncio.create_task(_do_update_from_button(controller, channel_id, message_id))


async def _do_update_from_button(controller: "Controller", channel_id: str, message_id: str) -> None:
    """Background task to perform update after button click."""
    try:
        if not hasattr(controller, "update_checker"):
            return

        update_checker = controller.update_checker
        im_client = (
            controller._get_im_client_for_platform("slack")
            if hasattr(controller, "_get_im_client_for_platform")
            else controller.im_client
        )
        lang = str(getattr(getattr(controller, "config", None), "language", "en") or "en")

        # Check for updates
        version_info = await update_checker._get_version_info_async()

        if version_info.get("has_update"):
            # Perform the update
            result = await update_checker._perform_update(
                version_info["latest"],
                channel_id=channel_id,
                message_id=message_id,
                platform="slack",
            )
            if not result.get("ok"):
                # Update failed, show error
                await im_client.web_client.chat_update(
                    channel=channel_id,
                    ts=message_id,
                    text=i18n_t("update.upgradeFailedShort", lang),
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": i18n_t("update.upgradeFailedSlack", lang),
                            },
                        }
                    ],
                )
            elif not result.get("restarting"):
                # Upgrade succeeded but restart not scheduled
                await im_client.web_client.chat_update(
                    channel=channel_id,
                    ts=message_id,
                    text=i18n_t("update.restartRequiredShort", lang),
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": i18n_t("update.restartRequiredSlack", lang),
                            },
                        }
                    ],
                )
        else:
            # No update available
            await im_client.web_client.chat_update(
                channel=channel_id,
                ts=message_id,
                text=i18n_t("update.alreadyUpToDate", lang),
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": i18n_t("update.latestVersionSlack", lang)},
                    }
                ],
            )
    except Exception as e:
        logger.error(f"Failed to perform update from button click: {e}", exc_info=True)


from modules.im import MessageContext
