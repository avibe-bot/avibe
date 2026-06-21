"""Factory for creating IM platform clients"""

import logging

from .base import BaseIMClient
from .multi import MultiIMClient

logger = logging.getLogger(__name__)


class IMFactory:
    """Factory class to create the appropriate IM client based on platform"""

    @staticmethod
    def create_client(config) -> BaseIMClient:
        clients = IMFactory.create_clients(config)
        configured_primary = getattr(getattr(config, "platforms", None), "primary", getattr(config, "platform", "slack"))
        primary = configured_primary if configured_primary in clients else (next(iter(clients)) if clients else "avibe")
        return MultiIMClient(clients, primary_platform=primary)

    @staticmethod
    def create_clients(config) -> dict[str, BaseIMClient]:
        """Create and return the appropriate IM client based on configuration

        Args:
            config: Application configuration

        Returns:
            Instance of platform-specific IM client

        Raises:
            ValueError: If platform is not supported
        """
        from config.platform_registry import get_platform_descriptor, is_workbench_platform

        enabled_platforms = list(getattr(config, "enabled_platforms", lambda: [getattr(config, "platform", "slack")])())
        clients: dict[str, BaseIMClient] = {}
        for platform in enabled_platforms:
            # The in-process workbench (avibe) is never an IM transport: it has no
            # remote credentials and is wired separately by the controller. Skip it
            # here so an explicitly-enabled "avibe" can't crash create_client with
            # "Avibe configuration not found".
            if is_workbench_platform(platform):
                continue
            descriptor = get_platform_descriptor(platform)
            logger.info("Creating %s client", platform)
            clients[platform] = descriptor.create_client(config)
        return clients

    @staticmethod
    def get_supported_platforms() -> list[str]:
        """Get list of supported platforms

        Returns:
            List of supported platform names
        """
        from config.platform_registry import supported_platform_ids

        return supported_platform_ids()

    @staticmethod
    def validate_platform_config(config) -> None:
        """Validate platform configuration before creating client

        Args:
            config: Application configuration

        Raises:
            ValueError: If configuration is invalid
        """
        from config.platform_registry import get_platform_descriptor, is_workbench_platform

        for platform in getattr(config, "enabled_platforms", lambda: [getattr(config, "platform", "slack")])():
            # The in-process workbench (avibe) has no remote credentials to
            # validate and is wired separately by the controller — skip it,
            # mirroring create_clients.
            if is_workbench_platform(platform):
                continue
            descriptor = get_platform_descriptor(platform)
            platform_config = descriptor.get_config(config)
            if platform_config is None:
                raise ValueError(f"Missing configuration for platform: {platform}")
            platform_config.validate()
