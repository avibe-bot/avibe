from __future__ import annotations

from types import SimpleNamespace

import config.platform_registry as platform_registry
from config.platform_registry import (
    WORKBENCH_PLATFORM_ID,
    PlatformCapabilities,
    PlatformDescriptor,
    get_platform_descriptor,
    im_platform_ids,
    is_workbench_platform,
)
from config.v2_config import PlatformsConfig, SlackConfig, V2Config
from modules.im.avibe import AvibeBot
from modules.im.factory import IMFactory
from modules.im.multi import MultiIMClient
from modules.im.slack import SlackBot


def test_platform_catalog_exposes_capability_flags() -> None:
    catalog = {item["id"]: item for item in platform_registry.platform_catalog_payload()}

    assert catalog["slack"]["capabilities"]["supports_threads"] is True
    assert catalog["telegram"]["capabilities"]["supports_threads"] is False
    assert catalog["wechat"]["capabilities"]["supports_buttons"] is False
    assert catalog["wechat"]["capabilities"]["supports_channels"] is False
    assert catalog["slack"]["capabilities"]["supports_typing_indicator"] is True
    assert catalog["slack"]["capabilities"]["typing_indicator_best_effort"] is True
    assert catalog["telegram"]["capabilities"]["supports_typing_indicator"] is True
    assert catalog["telegram"]["capabilities"]["typing_indicator_requires_clear"] is False
    assert catalog["wechat"]["capabilities"]["supports_typing_indicator"] is True
    assert catalog["wechat"]["capabilities"]["typing_indicator_requires_clear"] is True
    assert catalog["wechat"]["capabilities"]["force_preferred_processing_indicator"] is True
    assert catalog["wechat"]["capabilities"]["supports_toolcall_delivery"] is False
    assert catalog["slack"]["capabilities"]["supports_toolcall_delivery"] is True
    assert catalog["lark"]["capabilities"]["supports_typing_indicator"] is False
    assert catalog["lark"]["capabilities"]["preferred_processing_indicator"] == "reaction"
    assert catalog["telegram"]["capabilities"]["supports_message_indicator_delete"] is True


def test_credential_readiness_comes_from_platform_descriptor() -> None:
    config = SimpleNamespace(lark=SimpleNamespace(app_id="cli_a", app_secret="secret"))

    assert get_platform_descriptor("lark").has_credentials(config) is True

    config.lark.app_secret = ""
    assert get_platform_descriptor("lark").has_credentials(config) is False


def test_descriptors_resolve_config_classes() -> None:
    config = get_platform_descriptor("telegram").create_config({"bot_token": "123456:test-token"})

    assert config.bot_token == "123456:test-token"


def test_registry_addition_drives_platform_validation_and_readiness(monkeypatch) -> None:
    descriptor = PlatformDescriptor(
        id="mockchat",
        config_key="mockchat",
        config_module="config.v2_config",
        config_class="SlackConfig",
        client_module="unused",
        client_class="Unused",
        formatter_module="unused",
        formatter_class="Unused",
        credential_fields=("token",),
        capabilities=PlatformCapabilities(
            supports_channels=True,
            supports_threads=False,
            supports_buttons=False,
            supports_quick_replies=False,
            supports_message_editing=False,
        ),
    )
    monkeypatch.setitem(platform_registry.PLATFORM_REGISTRY, descriptor.id, descriptor)

    platforms = PlatformsConfig(enabled=["mockchat"], primary="mockchat")
    platforms.validate()

    fake_config = SimpleNamespace(
        mode="self_host",
        platforms=platforms,
        mockchat=SimpleNamespace(token="configured"),
    )
    fake_config.enabled_platforms = V2Config.enabled_platforms.__get__(fake_config)
    fake_config.platform_has_credentials = V2Config.platform_has_credentials.__get__(fake_config)
    fake_config.configured_platforms = V2Config.configured_platforms.__get__(fake_config)

    assert fake_config.configured_platforms() == ["mockchat"]
    assert "mockchat" in IMFactory.get_supported_platforms()


def test_is_workbench_platform_identifies_avibe() -> None:
    assert WORKBENCH_PLATFORM_ID == "avibe"
    assert is_workbench_platform("avibe") is True
    assert is_workbench_platform("slack") is False


def test_im_platform_ids_excludes_workbench() -> None:
    ids = im_platform_ids()

    assert "avibe" not in ids
    assert {"slack", "discord", "telegram", "lark", "wechat"} <= set(ids)


def test_catalog_payload_exposes_kind_distinction() -> None:
    catalog = {item["id"]: item for item in platform_registry.platform_catalog_payload()}

    assert catalog["avibe"]["kind"] == "workbench"
    assert catalog["slack"]["kind"] == "im"
    assert catalog["discord"]["kind"] == "im"


def _config_with_enabled(enabled: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        platform="slack",
        slack=SlackConfig(bot_token="xoxb-test"),
        enabled_platforms=lambda: list(enabled),
    )


def test_create_clients_skips_workbench_when_only_avibe_enabled() -> None:
    # The controller wires the in-process workbench itself; the IM factory must
    # never try to build an "avibe" client (it has no AppCompatConfig and would
    # raise "Avibe configuration not found").
    clients = IMFactory.create_clients(_config_with_enabled(["avibe"]))

    assert clients == {}
    assert "avibe" not in clients


def test_create_clients_builds_real_platforms_but_skips_workbench() -> None:
    clients = IMFactory.create_clients(_config_with_enabled(["slack", "avibe"]))

    assert list(clients.keys()) == ["slack"]
    assert isinstance(clients["slack"], SlackBot)
    assert not any(isinstance(client, AvibeBot) for client in clients.values())


def test_create_client_returns_avibe_for_workbench_only_empty_enabled() -> None:
    # Workbench-only config: no external IM platform is enabled, so the IM
    # runtime wrapper is empty and idles until stop.
    config = _config_with_enabled([])
    config.platforms = SimpleNamespace(enabled=[], primary="avibe")

    client = IMFactory.create_client(config)

    assert isinstance(client, MultiIMClient)
    assert client.clients == {}
    assert client.primary_platform == "avibe"


def test_create_client_returns_avibe_for_legacy_avibe_only_enabled() -> None:
    # Older configs persisted the workbench-only state as ``["avibe"]`` instead
    # of an empty list. ``create_clients`` skips the workbench either way, so the
    # singular helper must still resolve to an empty runtime wrapper.
    client = IMFactory.create_client(_config_with_enabled(["avibe"]))

    assert isinstance(client, MultiIMClient)
    assert client.clients == {}
    assert client.primary_platform == "avibe"


def test_create_client_returns_single_real_client_when_one_platform_enabled() -> None:
    # The runtime always uses the wrapper now, even for a lone enabled platform,
    # so hot reconcile has one uniform add/remove surface.
    config = _config_with_enabled(["slack"])
    config.platforms = SimpleNamespace(enabled=["slack"], primary="slack")

    client = IMFactory.create_client(config)

    assert isinstance(client, MultiIMClient)
    assert isinstance(client.clients["slack"], SlackBot)
    assert client.primary_platform == "slack"
