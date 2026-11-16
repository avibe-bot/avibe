"""Harness-only epoch-3 probe, executed against candidate OR installed wheel.

Loaded by absolute file path, never added to sys.path. All product imports below
resolve from the selected interpreter, including the actual released binary.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch


def run(action: str, home: Path) -> dict:
    from sqlalchemy import select

    from core import skill_observability as observations
    from core.agent_core.harness.cache_metrics import CacheTracker
    from core.agent_core.messages import AssistantMessage, Origin, TextBlock, UserMessage
    from storage import messages_service
    from storage import agent_events_service, resource_access_service as acl, skill_observability as skills, vault_service as vault
    from storage.agent_transcript import SQLiteTranscriptStore
    from storage.db import create_sqlite_engine
    from storage.models import agent_events, skill_catalogs, state_meta, vault_audit, vault_audit_access_snapshots, vault_secrets
    from storage.sessions_service import SQLiteSessionsService
    from storage.vault_crypto import Sealed
    from vibe.authorization import AuthorizationContext

    db = home / "state" / "vibe.sqlite"
    fixture_path = home / "epoch3.json"
    engine = create_sqlite_engine(db)
    sessions = SQLiteSessionsService(db)
    store = SQLiteTranscriptStore(engine)
    owner = AuthorizationContext(instance_role="owner")
    member = acl.ResourceUserContext(
        subject="reader", organization_id="org", organization_member_id="member",
        organization_role="member", instance_role="editor", is_remote=False,
        instance_access_source="organization_group",
    )
    message = AssistantMessage((TextBlock("cache contract"),), Origin("contract", "anthropic", "contract"), "stop")

    async def restored(session):
        runtime = await store.load_for_run(session)
        tracker = CacheTracker()
        tracker.restore((), (*runtime.cache_records, *await store.load_cache_attempts(session)))
        return list(tracker.latest.values())

    def raw_events(conn, ids):
        return [list(row) for row in conn.execute(select(agent_events.c.id, agent_events.c.content_json).where(
            agent_events.c.id.in_(ids)
        ).order_by(agent_events.c.id))]

    try:
        if action == "epoch3-seed":
            session = sessions.reserve_standalone_agent_session(
                agent_backend="vibey", session_anchor="EPOCH3_CONTRACT", workdir=str(home / "workspace"),
                authorization_context=owner,
            )
            with engine.begin() as conn:
                user = messages_service.append(conn, scope_id=None, session_id=session, platform="avibe",
                                               author="user", message_type="user", text="epoch3 contract")
            asyncio.run(store.consume_input(session, user["id"], UserMessage((TextBlock("epoch3 contract"),))))
            record = {
                "version": 1, "provider": "contract", "model": "contract", "route": "contract",
                "started_at": 100, "context_seq": 0, "prefix": [["static", 100, 0]], "input_tokens": 100,
            }
            now = datetime.now(timezone.utc)
            observation = observations.observation(
                "skill.catalog_result", observations.catalog_result([]), session_id=session,
            )
            observation["metadata"]["observed_at"] = skills.timestamp(now)
            second = deepcopy(observation)
            second["id"] += "-shared"
            with patch("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", 3):
                asyncio.run(store.append_response(session, message, final=True, cache=record))
                asyncio.run(store.append_audit(session, "attempt", {"version": 1, "cache": record, "cache_state": [record]}))
                with engine.begin() as conn:
                    for item in (observation, second):
                        assert skills.record(conn, item, enabled=lambda: True, now=now) == "accepted"
                    assert all("entries_ref" in json.loads(raw) for _, raw in raw_events(conn, [observation["id"], second["id"]]))
                    vault.create_secret(conn, name="EPOCH3_CONTRACT", sealed=Sealed("fixture-ct", "fixture-nonce", "fixture-wrap"))
                    resource = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == "EPOCH3_CONTRACT")).scalar_one()
                    acl.ensure_resource_policy(conn, resource_kind="vault_secret", resource_id=resource,
                        organization_id="org", owner_user_id="other", access_level="public", group_ids=[],
                        policy_revision=1, last_applied_control_plane_revision=1)
                    public = vault._build_audit_access_snapshot(conn, ["EPOCH3_CONTRACT"])
                    private = deepcopy(public)
                    private["resources"][0]["policy"]["access_level"] = "private"
                    for snapshot in (public, public, private):
                        vault.audit(conn, "proxied", secret_name="EPOCH3_CONTRACT", access_snapshot=snapshot,
                                    requester={"source": "predecessor-contract"}, delivery={"status": 200})
                    audits = [list(row) for row in conn.execute(select(vault_audit.c.id, vault_audit.c.delivery).where(
                        vault_audit.c.event == "proxied", vault_audit.c.secret_name == "EPOCH3_CONTRACT",
                    ).order_by(vault_audit.c.id))]
                    assert len(audits) == 3 and all("ref" in json.loads(raw)["_resource_access_snapshot"] for _, raw in audits)
                    assert len(conn.execute(select(vault_audit_access_snapshots)).all()) == 2
                    visible = [row["id"] for row in vault.list_audit(conn, user_context=member) if row["event"] == "proxied"]
                    assert len(visible) == 2
                    fixture = {"session": session, "record": record, "observation": observation, "second": second,
                               "now": now.isoformat(), "events": raw_events(conn, [observation["id"], second["id"]]),
                               "audits": audits, "visible": sorted(visible), "public": public}
                    fixture_path.write_text(json.dumps(fixture))
        fixture = json.loads(fixture_path.read_text())
        session, record = fixture["session"], fixture["record"]
        now = datetime.fromisoformat(fixture["now"])
        if action in ("epoch3-seed", "epoch3-read", "epoch3-exercise"):
            assert asyncio.run(restored(session)) == [record], "released cache reader lost normalized prefix state"
            with engine.connect() as conn:
                assert raw_events(conn, [fixture["observation"]["id"], fixture["second"]["id"]]) == fixture["events"]
                events = agent_events_service.list_session_events(conn, session_id=session)
                assert len([e for e in events if e["event_type"] == "skill.catalog_result"
                            and e["content"] == fixture["observation"]["content"]]) == 2
                assert sorted(row["id"] for row in vault.list_audit(conn, user_context=member) if row["event"] == "proxied") == fixture["visible"]
        if action == "epoch3-exercise":
            # Execute the old binary's real inline writer and duplicate receipt.
            with engine.begin() as conn:
                assert skills.record(conn, fixture["observation"], enabled=lambda: True, now=now) == "duplicate"
                assert raw_events(conn, [fixture["observation"]["id"], fixture["second"]["id"]]) == fixture["events"]
                inline = deepcopy(fixture["observation"])
                inline["id"] += "-old-writer"
                assert skills.record(conn, inline, enabled=lambda: True, now=now) == "accepted"
                # Old clear must trigger GC without a new table or Python helper.
                skills.clear(conn, now=now)
                assert conn.execute(select(skill_catalogs)).first() is None
                vault.audit(conn, "proxied", secret_name="EPOCH3_CONTRACT", access_snapshot=fixture["public"])
            newer = {**record, "started_at": 120}
            asyncio.run(store.append_response(session, message, final=True, cache=newer))
            assert asyncio.run(restored(session)) == [newer]
        if action == "epoch3-return":
            newer = {**record, "started_at": 120}
            assert asyncio.run(restored(session)) == [newer], "returning reader ignored newer bridge inline state"
            with engine.begin() as conn:
                assert conn.execute(select(skill_catalogs)).first() is None
                persisted = dict(conn.execute(select(vault_audit.c.id, vault_audit.c.delivery)).all())
                assert all(persisted.get(identifier) == raw for identifier, raw in fixture["audits"])
                assert set(fixture["visible"]).issubset(row["id"] for row in vault.list_audit(conn, user_context=member))
                high = conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == "vault_audit_snapshot_high_water")).scalar_one()
                assert int(high) >= 2
            with patch("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", 3):
                asyncio.run(store.append_audit(session, "attempt", {"version": 1, "cache": record, "cache_state": [newer]}))
                assert asyncio.run(restored(session)) == [newer]
                with engine.begin() as conn:
                    vault.audit(conn, "proxied", secret_name="EPOCH3_CONTRACT", access_snapshot=fixture["public"])
                    assert conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == "vault_audit_snapshot_high_water")).scalar_one() == high
        return {"epoch3_contract": "passed", "stage": action}
    finally:
        sessions.close()
        engine.dispose()
