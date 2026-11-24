"""Classify one locked local snapshot, never authorize a retry, replacement or new claim."""
from __future__ import annotations

from dataclasses import fields
import json
import math
import re
from uuid import UUID

from config.v2_config import V2Config, VibeCloudRemoteAccessConfig
from vibe import desktop_redeem_evidence as evidence, remote_access as pairing

PRESERVE = "pairing_recovery_required"
CODES = frozenset({"legacy_pairing_wait", "legacy_pairing_no_request", "legacy_pairing_ended",
                   "legacy_pairing_expired", "legacy_pairing_publication", "legacy_pairing_incompatible"})
_BASE = {"schema_version", "operation_id", "phase", "source_fingerprint", "source_instance_id", "source_backend_url",
         "backend_url", "device_name", "session_secret", "prepared_at", "desktop_enrollment"}
_REQUEST = {"request_fingerprint", "redeem_request"}
_WIRE = {"pairing_key", "redeem_request_id", "device_name", "local_version", "origin_service"}
_OBSERVED = {"desktop_reply", "desktop_dispatch", "desktop_failure"}
_SEALED = {"pairing", "target_identity", "target_fingerprint", "redeemed_at"}
# Publication bookkeeping the current writer seals with the receipt (#4133).
_PUBLICATION = {"source_was_bound", "connection_file"}


def _unique(pairs):
    result = dict(pairs)
    if len(pairs) != len(result):
        raise ValueError
    return result


def _decode(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique)


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _installation(data):
    if data is None:
        return None
    # Only call damage a mismatch when the released reader also cannot replay.
    # A duplicate field or a future shape that it *would* accept is not that proof.
    try:
        obj = _decode(data)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if (obj.keys() - {"schema_version", "installation_id"} or "schema_version" in obj
            and (type(obj["schema_version"]) is not int or obj["schema_version"] != 1)):
        raise ValueError
    if obj.get("schema_version") != 1 or not _uuid(obj.get("installation_id")):
        return None
    return obj["installation_id"]


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _hex(value, n):
    return isinstance(value, str) and len(value) == n and re.fullmatch("[a-f0-9]+", value) is not None


def _known_request(record):
    req = record.get("redeem_request")
    if (not isinstance(req, dict) or set(req) != _WIRE or req["device_name"] != record["device_name"]
            or not record["backend_url"]):
        return False
    if "desktop_dispatch" in record and not _hex(record["desktop_dispatch"], 32):
        return False
    if "desktop_failure" in record and not isinstance(record["desktop_failure"], str):
        return False
    if "desktop_reply" in record:
        reply = record["desktop_reply"]
        return (isinstance(reply, dict) and set(reply) == {"kind", "observed_dispatch", "stamp"}
                and reply["kind"] in ("ended_unknown", "expired_unconsumed")
                and _hex(reply["observed_dispatch"], 32) and _hex(reply["stamp"], 64)
                and "desktop_dispatch" in record)
    return True


def classify(pending, config, installation, *, recent):
    """Closed advisory code only. Never return record bytes, identifiers or a generation token."""
    record, err = pairing._validate_pending_pairing_record(_decode(pending))
    if err or record is None or not isinstance(enrollment := record.get("desktop_enrollment"), dict):
        return PRESERVE
    if (not _BASE <= record.keys() or set(enrollment) != {"account_id", "installation_id"}
            or not _uuid(enrollment["installation_id"]) or type(record["schema_version"]) is not int
            or not _hex(record["operation_id"], 32) or not _hex(record["source_fingerprint"], 64)
            or not _timestamp(record["prepared_at"]) or record["source_instance_id"] != ""
            or not all(isinstance(record[k], str) and record[k] for k in ("source_backend_url", "device_name", "session_secret"))
            or not isinstance(record["backend_url"], str)):
        return PRESERVE
    raw = _decode(config)
    remote = raw["remote_access"]
    cloud = remote["vibe_cloud"]
    if (not isinstance(remote, dict) or remote.keys() - {"provider", "vibe_cloud"}
            or not isinstance(cloud, dict) or cloud.keys() - {f.name for f in fields(VibeCloudRemoteAccessConfig)}):
        return PRESERVE
    identity = pairing._pairing_identity_from_cloud(V2Config.from_payload(raw).remote_access.vibe_cloud)
    # The normal loader may default/normalize an invalid field. That is not evidence of emptiness.
    if any(type(cloud[k]) is not type(v) or cloud[k] != v for k, v in identity.items() if k in cloud):
        return PRESERVE
    fingerprint = pairing._pairing_identity_fingerprint(identity)
    unbound = not any(v for k, v in identity.items() if k != "backend_url")
    same_install = _installation(installation) == enrollment["installation_id"]
    same_source = record["source_fingerprint"] == fingerprint
    phase = record["phase"]
    if phase in ("redeemed", "applied"):
        allowed = _BASE | _REQUEST | _OBSERVED | _SEALED | _PUBLICATION | ({"applied_at"} if phase == "applied" else set())
        saved = record["pairing"]
        if (record.keys() - allowed or not _known_request(record) or not _timestamp(record.get("redeemed_at"))
                or type(record.get("source_was_bound", False)) is not bool
                or not isinstance(record.get("connection_file"), (str, type(None)))
                or phase == "applied" and not _timestamp(record.get("applied_at"))
                or set(saved) - {*pairing._PAIRING_REQUIRED_FIELDS, *pairing._PAIRING_SEAL_EXTRA_FIELDS, "session_secret"}
                or saved["session_secret"] != record["session_secret"] or saved["backend_url"] != record["backend_url"]
                or record["target_identity"] != pairing._pairing_target_identity(saved, saved["session_secret"])):
            return PRESERVE
        if same_install and (fingerprint == record["target_fingerprint"] or phase == "redeemed" and same_source and unbound):
            return "legacy_pairing_publication"
        return "legacy_pairing_incompatible"
    if phase != "prepared" or not unbound or not evidence.prepared(record):
        return PRESERVE
    if "redeem_request" not in record:
        if record.keys() != _BASE or record["backend_url"] != "":
            return PRESERVE
        return "legacy_pairing_wait" if recent else "legacy_pairing_no_request"
    if record.keys() - (_BASE | _REQUEST | _OBSERVED) or not _known_request(record):
        return PRESERVE
    if recent:
        return "legacy_pairing_wait"
    reply = evidence.disposition(record)
    if reply == "expired_unconsumed":
        return "legacy_pairing_expired"
    if reply == "ended_unknown" or not same_install or not same_source:
        return "legacy_pairing_ended"
    return "legacy_pairing_wait"
