"""Immutable handle descriptors for Controller-owned Computer Use scripts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


class ComputerScriptHandleError(RuntimeError):
    """A script handle descriptor is invalid or no longer names its target."""

    def __init__(self, code: str, *, params: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.params = dict(params or {})
        super().__init__(code)


@dataclass(frozen=True)
class AppHandleDescriptor:
    pid: int | None
    bundle_id: str | None
    name: str | None

    def matches(self, record: Mapping[str, Any]) -> bool:
        if record.get("pid") != self.pid:
            return False
        if self.bundle_id is not None:
            return record.get("bundle_id") == self.bundle_id
        return record.get("name") == self.name


@dataclass(frozen=True)
class WindowHandleDescriptor:
    pid: int
    window_id: int
    app: AppHandleDescriptor


HandleDescriptor = AppHandleDescriptor | WindowHandleDescriptor
DiscoveryCall = Callable[
    [str, Mapping[str, Any]],
    Awaitable[dict[str, Any]],
]

_GLOBAL_SCRIPT_TOOLS = frozenset(
    {
        "list_apps",
        "clipboard_read",
        "clipboard_write",
    }
)
# Window observations whose driver refuses a window_id that is closed or not
# owned by the requested pid (``window_id_not_found`` /
# ``window_owner_pid_mismatch``).
_DRIVER_VALIDATED_TOOLS = frozenset({"get_window_state"})
_APP_HANDLE_TOOLS = frozenset(
    {
        "launch_app",
        "kill_app",
        "list_windows",
    }
)
_WINDOW_HANDLE_TOOLS = frozenset(
    {
        "get_window_state",
        "zoom",
        "bring_to_front",
        "click",
        "double_click",
        "type_text",
        "press_key",
        "hotkey",
        "scroll",
        "drag",
        "set_value",
    }
)


def script_tool_requires_handle(tool: str) -> bool:
    """Return whether the shipped SDK must supply a handle for this tool."""

    return tool not in _GLOBAL_SCRIPT_TOOLS


def parse_handle_descriptor(value: Any) -> HandleDescriptor | None:
    """Parse the exact descriptor shape accepted from the shipped SDK."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ComputerScriptHandleError("invalid_computer_script_descriptor")
    kind = value.get("kind")
    if kind == "app":
        if set(value) != {"kind", "pid", "bundle_id", "name"}:
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        pid = value.get("pid")
        if pid is not None and (
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
        ):
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        bundle_id = _optional_nonempty_string(value.get("bundle_id"))
        name = _optional_nonempty_string(value.get("name"))
        if (bundle_id is None) == (name is None):
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        return AppHandleDescriptor(
            pid=pid,
            bundle_id=bundle_id,
            name=name,
        )
    if kind == "window":
        if set(value) != {"kind", "pid", "window_id", "app"}:
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        pid = value.get("pid")
        window_id = value.get("window_id")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(window_id, bool)
            or not isinstance(window_id, int)
            or window_id < 0
        ):
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        app = parse_handle_descriptor(value.get("app"))
        if not isinstance(app, AppHandleDescriptor) or app.pid != pid:
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        return WindowHandleDescriptor(pid=pid, window_id=window_id, app=app)
    raise ComputerScriptHandleError("invalid_computer_script_descriptor")


def bind_handle_to_request(
    descriptor: HandleDescriptor | None,
    tool: str,
    arguments: Mapping[str, Any],
) -> None:
    """Require the forwarded target to be exactly the supplied descriptor."""

    if descriptor is None:
        if script_tool_requires_handle(tool):
            raise ComputerScriptHandleError("computer_script_descriptor_required")
        return
    if isinstance(descriptor, AppHandleDescriptor):
        if tool not in _APP_HANDLE_TOOLS:
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        if tool == "launch_app":
            identity_key = "bundle_id" if descriptor.bundle_id is not None else "name"
            identity_value = descriptor.bundle_id or descriptor.name
            if arguments.get(identity_key) != identity_value:
                raise ComputerScriptHandleError("invalid_computer_script_descriptor")
            other_key = "name" if identity_key == "bundle_id" else "bundle_id"
            if arguments.get(other_key) is not None:
                raise ComputerScriptHandleError("invalid_computer_script_descriptor")
            return
        if arguments.get("pid") != descriptor.pid:
            raise ComputerScriptHandleError("invalid_computer_script_descriptor")
        return
    if tool not in _WINDOW_HANDLE_TOOLS:
        raise ComputerScriptHandleError("invalid_computer_script_descriptor")
    if (
        arguments.get("pid") != descriptor.pid
        or arguments.get("window_id") != descriptor.window_id
    ):
        raise ComputerScriptHandleError("invalid_computer_script_descriptor")


def handle_validated_by_driver(
    descriptor: HandleDescriptor | None,
    tool: str,
) -> bool:
    """Whether the driver itself refuses this call for a stale window handle.

    ``get_window_state`` refuses unless ``window_id`` is live and owned by
    ``pid``. macOS does not reuse window ids within a login session, so the
    exact pair cannot name a different window. The Controller therefore
    forwards this read-only observation without a discovery round trip and
    classifies a refusal afterwards with ``validate_handle_is_current``.
    """

    return isinstance(descriptor, WindowHandleDescriptor) and tool in _DRIVER_VALIDATED_TOOLS


async def validate_handle_is_current(
    descriptor: HandleDescriptor | None,
    tool: str,
    discover: DiscoveryCall,
) -> dict[str, Any] | None:
    """Validate one descriptor against current Controller discovery state.

    An app handle is checked against ``list_apps``. A window handle is checked
    with one pid-scoped ``list_windows``: the exact ``(pid, window_id)`` pair
    identifies the window for the whole login session, so the app list adds
    no identity and costs a full application scan.
    """

    if descriptor is None:
        return None
    if isinstance(descriptor, WindowHandleDescriptor):
        windows_result = await discover("list_windows", {"pid": descriptor.pid})
        if windows_result.get("isError") is True:
            return windows_result
        windows = _structured_list(windows_result, "windows")
        if not any(
            record.get("pid") == descriptor.pid
            and record.get("window_id") == descriptor.window_id
            for record in windows
        ):
            raise ComputerScriptHandleError(
                "stale_window_handle",
                params={
                    "pid": descriptor.pid,
                    "window_id": descriptor.window_id,
                },
            )
        return None

    if descriptor.pid is None:
        if tool == "launch_app":
            return None
        raise ComputerScriptHandleError("stale_app_handle")

    apps_result = await discover("list_apps", {})
    if apps_result.get("isError") is True:
        return apps_result
    apps = _structured_list(apps_result, "apps")
    if not any(descriptor.matches(record) for record in apps):
        raise ComputerScriptHandleError(
            "stale_app_handle",
            params={"pid": descriptor.pid},
        )
    return None


def _optional_nonempty_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ComputerScriptHandleError("invalid_computer_script_descriptor")
    return value


def _structured_list(result: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return []
    records = structured.get(field)
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, Mapping)]
