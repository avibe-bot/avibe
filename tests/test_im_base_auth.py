import asyncio
from types import SimpleNamespace

from modules.im import MessageContext
from modules.im.base import BaseIMClient, BaseIMConfig


class _Cfg(BaseIMConfig):
    def validate(self) -> None:
        return None


class _IM(BaseIMClient):
    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        return ""

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        return ""

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        return True

    async def answer_callback(self, callback_id, text=None, show_alert=False):
        return True

    def register_handlers(self):
        return None

    def run(self):
        return None

    async def get_user_info(self, user_id):
        return {}

    async def get_channel_info(self, channel_id):
        return {}

    def format_markdown(self, text):
        return text


class _Store:
    def __init__(self):
        self.settings = SimpleNamespace(channels={})

    def maybe_reload(self):
        return None

    def is_bound_user(self, user_id: str) -> bool:
        return False

    def has_any_admin(self) -> bool:
        return False

    def is_admin(self, user_id: str) -> bool:
        return False


class _SettingsManager:
    def __init__(self, *, bound: bool):
        self._bound = bound
        self._store = _Store()

    def is_bound_user(self, user_id: str) -> bool:
        return self._bound

    def get_store(self):
        return self._store


def test_extract_command_action():
    assert BaseIMClient.extract_command_action("/settings") == "settings"
    assert BaseIMClient.extract_command_action("/setcwd /tmp") == "set_cwd"
    assert BaseIMClient.extract_command_action("/set_cwd /tmp") == "set_cwd"
    assert BaseIMClient.extract_command_action("bind code") == ""
    assert BaseIMClient.extract_command_action("bind code", allow_plain_bind=True) == "bind"
    assert BaseIMClient.extract_command_action("hello") == ""
    assert BaseIMClient.extract_command_action("") == ""


def test_parse_text_command():
    assert BaseIMClient.parse_text_command("/settings") == ("settings", "")
    assert BaseIMClient.parse_text_command("/setcwd /tmp") == ("set_cwd", "/tmp")
    assert BaseIMClient.parse_text_command("/set_cwd /tmp") == ("set_cwd", "/tmp")
    assert BaseIMClient.parse_text_command("bind code") is None
    assert BaseIMClient.parse_text_command("bind code", allow_plain_bind=True) == ("bind", "code")
    assert BaseIMClient.parse_text_command("bind vr-aB3X9kLmN0", allow_plain_bind=True) == (
        "bind",
        "vr-aB3X9kLmN0",
    )
    assert BaseIMClient.parse_text_command("/bind vr-aB3X9kLmN0") == ("bind", "vr-aB3X9kLmN0")
    assert BaseIMClient.parse_text_command("hello") is None
    assert BaseIMClient.parse_text_command("/") is None


def test_check_authorization_uses_text_when_action_missing():
    im = _IM(_Cfg())
    manager = _SettingsManager(bound=False)
    result = im.check_authorization(
        user_id="U1",
        channel_id="D1",
        is_dm=True,
        text="bind code",
        settings_manager=manager,
    )
    assert result.allowed is True


def test_dispatch_text_command_executes_handler():
    im = _IM(_Cfg())
    received = {}

    async def _handler(context, args):
        received["user"] = context.user_id
        received["args"] = args

    im.on_command_callbacks = {"start": _handler}
    context = MessageContext(user_id="U1", channel_id="C1")

    handled = asyncio.run(im.dispatch_text_command(context, "/start now"))

    assert handled is True
    assert received == {"user": "U1", "args": "now"}


def test_dispatch_text_command_executes_plain_bind_when_enabled():
    im = _IM(_Cfg())
    received = {}

    async def _handler(context, args):
        received["user"] = context.user_id
        received["args"] = args

    im.on_command_callbacks = {"bind": _handler}
    context = MessageContext(user_id="U1", channel_id="D1", platform_specific={"is_dm": True})

    handled = asyncio.run(im.dispatch_text_command(context, "bind code", allow_plain_bind=True))

    assert handled is True
    assert received == {"user": "U1", "args": "code"}


def test_dispatch_text_command_ignores_plain_bind_when_disabled():
    im = _IM(_Cfg())
    im.on_command_callbacks = {"bind": lambda *_args: None}
    context = MessageContext(user_id="U1", channel_id="D1")

    handled = asyncio.run(im.dispatch_text_command(context, "bind code", allow_plain_bind=False))

    assert handled is False


def test_dispatch_text_command_returns_false_for_unknown():
    im = _IM(_Cfg())
    context = MessageContext(user_id="U1", channel_id="C1")
    handled = asyncio.run(im.dispatch_text_command(context, "/unknown"))
    assert handled is False
