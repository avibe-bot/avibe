"""Handler modules for organizing controller functionality.

Keep leaf contracts importable without initializing the controller's handlers
and Agent SDKs. The public handler exports remain available on demand.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseHandler
    from .command_handlers import CommandHandlers
    from .session_handler import SessionHandler
    from .settings_handler import SettingsHandler
    from .message_handler import MessageHandler

_EXPORTS = {
    "BaseHandler": ".base",
    "CommandHandlers": ".command_handlers",
    "SessionHandler": ".session_handler",
    "SettingsHandler": ".settings_handler",
    "MessageHandler": ".message_handler",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
