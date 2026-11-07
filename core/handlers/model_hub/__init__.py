"""Model Hub configuration, resolution policy, and API services.

Identifiers and other leaf contracts must not load the Hub service or runtime.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import EngineAdapter
    from .service import (
        ModelHubError,
        ModelHubService,
        create_default_service,
        ensure_runtime_dependency,
        load_opencode_public_models,
        runtime_dependency_payload,
        search_capability_for_source,
    )

__all__ = [
    "EngineAdapter",
    "ModelHubError",
    "ModelHubService",
    "create_default_service",
    "ensure_runtime_dependency",
    "load_opencode_public_models",
    "runtime_dependency_payload",
    "search_capability_for_source",
]


def __getattr__(name: str):
    if name == "EngineAdapter":
        from .adapter import EngineAdapter

        return EngineAdapter
    if name in __all__:
        from . import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
