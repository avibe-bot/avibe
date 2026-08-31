"""Package identities shared by the forward upgrade path."""

from packaging.version import Version


CORE_PACKAGE_NAME = "avibe-os"
LEGACY_CORE_PACKAGE_NAME = "vibe-remote"
MEMORY_PACKAGE_NAME = "avibe-memory"
MEMORY_SPLIT_MIN_VERSION = Version("3.0.14.dev0")


__all__ = [
    "CORE_PACKAGE_NAME",
    "LEGACY_CORE_PACKAGE_NAME",
    "MEMORY_PACKAGE_NAME",
    "MEMORY_SPLIT_MIN_VERSION",
]
