"""Read-only, conservative diagnosis for a previously refused first schedule claim."""

import json

from sqlalchemy import and_, literal, or_, select

from storage.models import (
    device_function_settings as prefs,
    device_session_extensions as ext,
    remote_device_sessions as sessions,
    state_meta,
)
from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX
from storage.workbench_sessions_service import get_active_session


def all_eligible_off(conn, operation, now):
    from core.web_push_notifications import (
        WEB_PUSH_DISPOSITION_REVOKED,
        _evaluate_record_authorization,
        _load_notification_config,
    )
    from vibe.remote_access import device_binding

    binding = operation["schedule_probe_binding"]
    config, failed = _load_notification_config()
    if (
        failed
        or not config
        or config.remote_access.vibe_cloud.instance_id != operation["instance_id"]
        or device_binding(config) != binding
    ):
        return False
    join = and_(
        prefs.c.binding == sessions.c.binding,
        prefs.c.subject == sessions.c.subject,
        prefs.c.device_id == sessions.c.device_id,
    )
    candidates = conn.execute(
        select(sessions.c.subject, ext.c.schedule_principal, prefs.c.schedule_on)
        .select_from(sessions.outerjoin(prefs, join).outerjoin(ext, ext.c.session_id == sessions.c.session_id))
        .where(
            sessions.c.binding == binding,
            sessions.c.expires_at > now,
            sessions.c.cookie_exp > now,
            or_(sessions.c.kind == "iphone_app", prefs.c.schedule_capable == 1),
            ~select(state_meta.c.key)
            .where(state_meta.c.key == literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id)
            .exists(),
        )
    )
    found = False
    for subject, raw, enabled in candidates:
        if not raw:
            if enabled != 0:  # Default on; there is no proof that this phone is ineligible.
                return False
            continue
        record = json.loads(raw)
        decision = _evaluate_record_authorization(config, "remote:" + subject, record, allow_sync_retry=False)
        if not decision.authorized or not decision.context:
            if enabled != 0 and decision.disposition != WEB_PUSH_DISPOSITION_REVOKED:
                return False
            continue
        if not decision.context.has_role("editor"):
            continue
        try:
            get_active_session(conn, operation["session_id"], authorization_context=decision.context)
        except LookupError:
            continue
        if enabled != 0:
            return False
        found = True
    return found
