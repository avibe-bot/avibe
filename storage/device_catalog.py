"""One authorized snapshot backs both device rows and mutations; callers own the SQLite transaction."""

from sqlalchemy import select

from storage import device_functions as gates, device_health_registry as mobile_registry, device_registry
from storage.device_functions import DeviceError
from storage.mobile_store import MobileStore, mobile_store_path
from storage.models import device_function_settings as settings, device_health_links as health


def snapshot(conn, viewer, instance, owner_email, now, *, paths=None, health_identity=None):
    binding, subject, owner, caller = viewer
    gates.live_caller(conn, viewer, now)
    listed = device_registry.list_devices(binding, subject, owner=owner, caller=caller, now=now, paths=paths, conn=conn)
    rows = {row["id"]: row for row in listed}
    query = select(settings).where(settings.c.binding == binding)
    if not owner:
        query = query.where(settings.c.subject == subject)
    preferences = {r["device_id"]: r for r in conn.execute(query).mappings()}
    for key, row in rows.items():
        account = row["account"]
        row["group"] = (
            "unattributed"
            if not account
            else (
                "mine" if (account["subject"] == subject if subject else account["login_role"] == "owner") else "others"
            )
        )
        own = gates.owns(row, viewer, owner_email)
        pref = preferences.get(key, {})
        enrolled = row["capabilities"]["push_enabled"]
        functions = [
            {
                "kind": "notifications",
                "on": bool(enrolled and pref.get("notifications_on", 1)),
                "can_change": bool(own and enrolled),
            }
        ]
        if not row["push_only"] and (row["kind"] == "iphone_app" or pref.get("schedule_capable")):
            functions.append({"kind": "schedule", "on": bool(pref.get("schedule_on", 1)), "can_change": own})
        row["functions"] = functions
    active_ids = {}
    unlinked = []
    health_scope = health_identity if health_identity is not None else (
        {"id": instance, "owner": owner_email} if instance and owner_email else {}
    )
    if owner and health_scope and mobile_store_path().exists():
        store = MobileStore()
        with store.transaction() as mobile:
            for mid, func, legacy in mobile_registry.scoped_rows(mobile, health_scope["id"], health_scope["owner"], health_scope.get("owner_subject")):
                link = (
                    conn.execute(select(health).where(health.c.mobile_id == mid, health.c.binding == binding))
                    .mappings()
                    .first()
                )
                row = rows.get(link["device_id"]) if link else None
                if not row or row["push_only"] or not row["account"] or row["account"]["subject"] != link["subject"]:
                    unlinked.append(legacy)
                    continue
                if not gates.owns(row, viewer, owner_email):
                    unlinked.append(legacy)
                    continue
                ids = active_ids.setdefault(row["id"], [])
                ids.append(mid)
                existing = next((f for f in row["functions"] if f["kind"] == "health"), None)
                if not existing:
                    row["functions"].append({**func, "can_change": True})
                else:
                    existing["data"] = sorted(set(existing["data"]) | set(func["data"]))
                    if func.get("syncedAt", "") > existing.get("syncedAt", ""):
                        existing["syncedAt"] = func["syncedAt"]
    return rows, active_ids, unlinked


def change(conn, viewer, instance, owner_email, device, kind, on, now, *, health_identity=None):
    rows, active, unlinked = snapshot(conn, viewer, instance, owner_email, now, health_identity=health_identity)
    if kind == "unlinkedHealth":
        if not viewer[2] or not any(h["pairing_id"] == device for h in unlinked):
            raise DeviceError("device_not_found", 404)
        MobileStore().revoke(device_id=device, caller="devices")
        return {"ok": True}
    row = rows.get(device)
    if not row:
        raise DeviceError("device_not_found", 404)
    if not gates.owns(row, viewer, owner_email):
        raise DeviceError("device_access_forbidden", 403)
    feature = next((f for f in row["functions"] if f["kind"] == kind), None)
    if feature is None or not feature["can_change"]:
        raise DeviceError("device_enrollment_required", 409)
    if kind == "health":
        store = MobileStore()
        for mid in active[device]:
            store.revoke(device_id=mid, caller="devices")
        return {"function": {"kind": "health", "on": False, "data": [], "can_change": False}}
    gates.preference(conn, viewer[0], row["account"]["subject"], device, **{kind + "_on": int(on)})
    return {"function": {**feature, "on": on}}
