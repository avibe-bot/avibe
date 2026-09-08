"""Enforce Avibe's ownership of the model sent to a backend."""

from vibe.i18n import t


def require_agent_model(model: str | None, backend: str, language: str = "en") -> str:
    selected = str(model or "").strip()
    if not selected or (backend == "claude" and selected.lower() == "default"):
        raise ValueError(t("errors.agentModelRequired", language, backend=backend))
    return selected
