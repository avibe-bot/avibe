"""One admission boundary for generated backend-model catalog rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from config.v2_config import ModelHubBackendModelConfig

from .identifiers import canonical_model_id
from .resolver import BackendName


def backend_model_admission_error(
    backend: BackendName | None,
    model_id: object,
    *,
    claude_builtin_ids: Iterable[str] = (),
) -> Literal["backend_model_id_invalid"] | None:
    """Return the request-boundary error for one generated model identity."""

    canonical = canonical_model_id(model_id)
    if canonical is None or canonical != model_id:
        return "backend_model_id_invalid"
    # Backend model IDs are routing identities, not vendor labels. In
    # particular, Claude Code may explicitly launch a Hub model such as
    # ``grok-4.7`` and the Gateway must preserve that exact spelling.
    return None


def admissible_backend_model(
    backend: BackendName | None,
    model_id: object,
    metadata: Mapping[str, Any],
    *,
    claude_builtin_ids: Iterable[str] = (),
) -> ModelHubBackendModelConfig | None:
    """Validate one producer row against identity and persisted-shape rules."""

    if (
        backend_model_admission_error(
            backend,
            model_id,
            claude_builtin_ids=claude_builtin_ids,
        )
        is not None
    ):
        return None
    try:
        model = ModelHubBackendModelConfig.from_payload({"id": model_id, **dict(metadata)})
    except (TypeError, ValueError):
        return None
    return model if model.id == model_id else None
