"""Package identities shared by the forward upgrade path."""

from packaging.version import Version


CORE_PACKAGE_NAME = "avibe-os"
LEGACY_CORE_PACKAGE_NAME = "vibe-remote"


__all__ = [
    "CORE_PACKAGE_NAME",
    "LEGACY_CORE_PACKAGE_NAME",
]
