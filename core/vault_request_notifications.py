"""IM notifications for Vault requests that require browser-only review."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from config.platform_registry import get_platform_descriptor, im_platform_ids, is_workbench_platform
from core.avibe_cloud import vault_request_url
from core.scheduled_tasks import ParsedSessionKey, ResolvedSessionIdTarget, resolve_session_id_target
from modules.im import MessageContext
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

REQUEST_TYPES = {"access", "sign", "provision"}
MAX_SECRET_NAMES = 3


def _payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _request_card(request: dict[str, Any]) -> dict[str, Any]:
    card = _payload(request.get("card"))
    if card:
        return card
    delivery = _payload(request.get("delivery"))
    return _payload(delivery.get("card"))


def _request_type(request: dict[str, Any]) -> str:
    request_type = str(request.get("request_type") or "").strip()
    if request_type:
        return request_type
    card = _request_card(request)
    if card.get("card_type") == "secure_input":
        return "provision"
    return str(card.get("request_type") or "").strip()


def _request_session_id(request: dict[str, Any]) -> str | None:
    for source in (_payload(request.get("requester")), _payload(request.get("delivery")), _request_card(request)):
        session_id = str(source.get("session_id") or "").strip()
        if session_id:
            return session_id
    return None


def _secret_names(request: dict[str, Any]) -> list[str]:
    card = _request_card(request)
    values: list[str] = []
    for raw in (request.get("secret_name"), card.get("secret_name")):
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    for key in ("secret_names", "protected_secret_names"):
        raw_names = card.get(key)
        if isinstance(raw_names, list):
            values.extend(str(name).strip() for name in raw_names if str(name).strip())
    return list(dict.fromkeys(values))


def _secret_summary(request: dict[str, Any], *, lang: str) -> str:
    names = _secret_names(request)
    if not names:
        return i18n_t("vault.requestNotification.secretSet", lang)
    if len(names) <= MAX_SECRET_NAMES:
        return ", ".join(names)
    return i18n_t(
        "vault.requestNotification.secretNamesMore",
        lang,
        names=", ".join(names[:MAX_SECRET_NAMES]),
        count=len(names) - MAX_SECRET_NAMES,
    )


def _resolve_target_context(controller: Any, target: ParsedSessionKey) -> dict[str, str]:
    settings_managers = getattr(controller, "platform_settings_managers", {}) or {}
    settings_manager = settings_managers.get(target.platform)
    if settings_manager is None:
        return {"user_id": target.scope_id if target.is_dm else "scheduled", "channel_id": target.scope_id}

    channel_id = target.scope_id
    user_id = "scheduled"
    if target.is_dm:
        user_id = target.scope_id
        bound_user = settings_manager.get_store().get_user(target.scope_id, platform=target.platform)
        dm_chat_id = getattr(bound_user, "dm_chat_id", "") if bound_user else ""
        if target.platform == "lark" and not dm_chat_id:
            raise ValueError(f"lark user {target.scope_id} is missing dm_chat_id binding")
        if dm_chat_id:
            channel_id = dm_chat_id

    return {"user_id": user_id, "channel_id": channel_id}


def _message_context(
    controller: Any,
    target: ResolvedSessionIdTarget,
    *,
    request_id: str,
) -> MessageContext:
    session_key = target.session_key
    target_context = _resolve_target_context(controller, session_key)
    return MessageContext(
        user_id=target_context["user_id"],
        channel_id=target_context["channel_id"],
        platform=session_key.platform,
        thread_id=session_key.thread_id,
        message_id=f"vault-request:{request_id}",
        platform_specific={
            "platform": session_key.platform,
            "is_dm": session_key.is_dm,
            "turn_source": "vault_request",
            "agent_session_id": target.session_id,
            "session_key_external": session_key.to_key(),
            "delivery_key_external": session_key.to_key(),
            "delivery_scope_session_key": session_key.session_scope,
            "suppress_delivery": bool(target.suppress_delivery),
            "agent_session_target": {
                "id": target.session_id,
                "agent_id": target.agent_id,
                "agent_name": target.agent_name,
                "agent_backend": target.agent_backend,
                "agent_variant": target.agent_variant,
                "model": target.model,
                "reasoning_effort": target.reasoning_effort,
                "native_session_id": target.native_session_id,
                "workdir": target.workdir,
                "session_anchor": target.session_anchor,
                "metadata": target.metadata or {},
                "suppress_delivery": target.suppress_delivery,
            },
        },
    )


def _lang(controller: Any) -> str:
    return str(getattr(getattr(controller, "config", None), "language", None) or "en")


def _format_notification(
    request: dict[str, Any],
    *,
    platform: str,
    session_id: str,
    link: str | None,
    lang: str,
) -> str:
    descriptor = get_platform_descriptor(platform)
    formatter = descriptor.create_formatter()
    request_type = _request_type(request)
    return formatter.format_vault_request_notification(
        title=i18n_t("vault.requestNotification.title", lang),
        request_label=i18n_t("vault.requestNotification.requestLabel", lang),
        request_value=i18n_t(f"vault.requestNotification.type.{request_type}", lang),
        secret_label=i18n_t("vault.requestNotification.secretLabel", lang),
        secret_value=_secret_summary(request, lang=lang),
        session_label=i18n_t("vault.requestNotification.sessionLabel", lang),
        session_id=session_id,
        action_label=i18n_t("vault.requestNotification.action", lang),
        action_url=link,
        no_link_text=i18n_t("vault.requestNotification.noPublicLink", lang),
        guidance=i18n_t("vault.requestNotification.guidance", lang),
    )


async def notify_vault_request_created(
    controller: Any,
    request: dict[str, Any],
    *,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Notify IM-originated sessions about a pending Vault request.

    The IM message contains only public request metadata and a web deep link.
    Approval, signing, and secure input stay browser-only.
    """

    if not isinstance(request, dict):
        return {"ok": False, "sent": False, "reason": "invalid_request"}
    request_id = str(request.get("id") or "").strip()
    request_type = _request_type(request)
    if not request_id or request_type not in REQUEST_TYPES:
        return {"ok": True, "sent": False, "reason": "unsupported_request"}
    if str(request.get("status") or "pending") != "pending":
        return {"ok": True, "sent": False, "reason": "not_pending"}
    session_id = _request_session_id(request)
    if not session_id:
        return {"ok": True, "sent": False, "reason": "no_session"}

    try:
        target = resolve_session_id_target(session_id, db_path=db_path)
    except Exception:
        logger.debug("vault request notification: session target unavailable for %s", session_id, exc_info=True)
        return {"ok": True, "sent": False, "reason": "session_unavailable"}

    platform = target.session_key.platform
    if is_workbench_platform(platform):
        return {"ok": True, "sent": False, "reason": "workbench_session"}
    if platform not in set(im_platform_ids()):
        return {"ok": True, "sent": False, "reason": "unsupported_platform"}
    if target.suppress_delivery:
        return {"ok": True, "sent": False, "reason": "delivery_suppressed"}

    try:
        context = _message_context(controller, target, request_id=request_id)
        text = _format_notification(
            request,
            platform=platform,
            session_id=session_id,
            link=vault_request_url(request_id, getattr(controller, "config", None)),
            lang=_lang(controller),
        )
        message_id = await controller.emit_agent_message(
            context,
            message_type="notify",
            text=text,
            parse_mode="markdown",
        )
    except Exception:
        logger.exception("failed to notify IM session for vault request %s", request_id)
        return {"ok": False, "sent": False, "reason": "send_failed"}

    return {"ok": True, "sent": message_id is not None, "message_id": message_id}
