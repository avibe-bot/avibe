"""Pairing-scoped durable intent for connection settings (not an authorization source).

All transitions, including foreign CLI/legacy writes, serialize with the config
file lock. The operation id is a writer fence, never a browser capability.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config, config_file_lock
from vibe.device_metadata import binding_key

FIELDS = {
    "protocol": "transport_protocol", "ipFamily": "edge_ip_version", "outbound": "edge_bind_address",
    "direct": "direct", "relay": "public_relay_trial", "autoRecovery": "auto_recovery", "profile": "optimization_profile",
}
AUTOMATIC = {"protocol": "auto", "ipFamily": "auto", "outbound": ""}
SWITCHES = {"direct", "relay"}
OUTAGE_SECONDS = 180
STALE_OPERATION_SECONDS = 150  # readiness (including protocol fallback), drain and bounded public probe
RESTORE_SECONDS = 60


class ConnectionError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


def validated_change(body: Any) -> tuple[str, str | bool]:
    if not isinstance(body, dict) or set(body) != {"setting", "to"} or not isinstance(body["setting"], str):
        raise ConnectionError("invalid_connection_change", 400)
    key, value = body["setting"], body["to"]
    if key in SWITCHES | {"autoRecovery"}:
        valid = type(value) is bool
    elif key == "outbound" and isinstance(value, str):
        try:
            return key, str(ipaddress.ip_address(value)) if value else ""
        except ValueError:
            valid = False
    else:
        valid = isinstance(value, str) and value in {
            "protocol": ("auto", "http2", "quic"), "ipFamily": ("auto", "4", "6"),
            "profile": ("stable", "balanced", "low_latency"),
        }.get(key, ())
    if not valid:
        raise ConnectionError("invalid_connection_change", 400)
    return key, value


@dataclass(frozen=True)
class Actor:
    binding: str
    local: bool = False  # direct host loopback only; delegated CLI is remote


def binding(config: V2Config) -> str:
    cloud = config.remote_access.vibe_cloud
    return binding_key(cloud.instance_id or "", cloud.session_secret or "")


def _relay_enrollment_path() -> Path:
    return paths.get_state_dir() / "public-relay-host-enrollment.json"


def _record_enrollment(pairing: str | None) -> None:
    write_atomic(_relay_enrollment_path(), json.dumps({"version": 1, "binding": pairing}))


def relay_pairing(config: V2Config) -> str | None:
    """Neither cookie rotation, master off/on, nor a hostname change is a re-pair."""
    cloud = config.remote_access.vibe_cloud
    if not cloud.instance_id or not cloud.instance_secret:
        return None
    return hmac.digest(cloud.instance_secret.encode(), b"relay-host-enrollment:v1:" + cloud.instance_id.encode(), "sha256").hex()


def enroll_from_host(config: V2Config) -> None:
    pairing = relay_pairing(config)
    if not pairing:
        raise ConnectionError("relay_pairing_required")
    _record_enrollment(pairing)


def adopt_host_enrollment() -> None:
    """First-start adoption only. A tombstone prevents guessing eligibility after re-pair."""
    with config_file_lock():
        if not _relay_enrollment_path().exists():
            config = V2Config.load()
            cloud = config.remote_access.vibe_cloud
            _record_enrollment(relay_pairing(config) if cloud.public_relay_trial is True else None)


def relay_enrolled(config: V2Config) -> bool:
    try:
        marker = json.loads(_relay_enrollment_path().read_text(encoding="utf-8"))
        pairing = relay_pairing(config)
        return (pairing is not None and isinstance(marker, dict) and marker.get("version") == 1
                and marker.get("binding") == pairing)
    except (OSError, UnicodeError, ValueError):
        return False


def value(config: V2Config, key: str) -> str | bool:
    return getattr(config.remote_access.vibe_cloud, FIELDS[key])


def _clock() -> float:
    import sys
    if sys.platform.startswith("linux"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    if sys.platform == "win32":
        import ctypes
        ticks = ctypes.windll.kernel32.GetTickCount64
        ticks.restype = ctypes.c_ulonglong
        return ticks() / 1000
    return time.monotonic()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PinFailureWindow:
    def __init__(self, clock: Callable[[], float] = _clock):
        self.clock = clock
        self.identity: tuple | None = None
        self.seconds = self.last_down = 0.0
        self.paused = False

    def observe(self, identity: tuple | None, *, disconnected: bool | None, max_gap: float) -> bool:
        """Only adjacent, confirmed down samples earn time. A short unknown pauses it."""
        tick = self.clock()
        gap = tick - self.last_down
        if self.identity != identity or not 0 <= gap <= max_gap or disconnected is False:
            self.identity = None
        if identity is None or disconnected is not True:
            self.paused = True
            return False
        if self.identity is None:
            self.seconds = 0.0
        elif not self.paused:
            self.seconds += gap
        self.identity, self.last_down, self.paused = identity, tick, False
        return self.seconds >= OUTAGE_SECONDS


class Journal:
    def __init__(self, clock: Callable[[], float] = _clock):
        self.clock = clock
        self.boot = secrets.token_hex(16)
        self.warned_unavailable = False

    @property
    def path(self) -> Path:
        return paths.get_state_dir() / "connection-settings.json"

    def load(self, config: V2Config) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get('version') != 1 or not {'operation', 'outcome', 'binding', 'returnedToAutomatic'} <= data.keys():
                raise ValueError('invalid journal')
            for slot in ('operation', 'debt', 'priorRepair'):
                op = data.get(slot)
                if op is not None and (not isinstance(op, dict) or not {'id', 'state', 'setting', 'from', 'to', 'boot', 'until', 'baseline'} <= op.keys()
                                       or op['setting'] not in FIELDS or op['state'] not in {'applying', 'verifying', 'restoring'}):
                    raise ValueError('invalid operation record')
            attempt = data.get('pinAttempt')
            if attempt is not None and (not isinstance(attempt, dict) or not all(
                    isinstance(attempt.get(k), str) and attempt[k] for k in ('id', 'boot'))):
                raise ValueError('invalid pin attempt ownership')
            prior = data.get('priorRepair')
            if prior is not None:
                current = data['operation']
                if (not current or current['state'] != 'applying' or prior['state'] != 'restoring'
                        or prior['setting'] not in AUTOMATIC or prior['setting'] != current['setting']
                        or prior['from'] != current['from']):
                    raise ValueError('invalid suspended repair')
            if data['binding'] == relay_pairing(config):
                self.warned_unavailable = False
                return data
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise ConnectionError("connection_state_unavailable", 503) from exc
        return {"version": 1, "binding": relay_pairing(config), "operation": None, "outcome": None, "returnedToAutomatic": None}

    def save(self, state: dict) -> None:
        write_atomic(self.path, json.dumps(state, separators=(",", ":")))

    def begin(self, config, key, new, *, baseline=False, cancel_pin=None) -> dict:
        state = self.load(config)
        old = state["operation"]
        if old and not (old["state"] == "restoring" and old["setting"] == key):
            raise ConnectionError("connection_change_busy")
        if state.get('pinAttempt') and state['pinAttempt']['id'] != cancel_pin:
            raise ConnectionError('connection_change_busy')
        op = {"id": secrets.token_hex(16), "setting": key, "from": value(config, key), "to": new,
              "state": "applying", "baseline": baseline, "boot": self.boot, "until": self.clock() + STALE_OPERATION_SECONDS}
        if cancel_pin:
            # Revoke the writer and reserve the edit in the same file replacement.
            # Keep the active-generation exception until the owner's edit takes over.
            route = state.get('pinRoute') or {}
            if (state.get('pinAttempt') or {}).get('id') != cancel_pin or (route.get('attempt') or {}).get('id') != cancel_pin:
                raise ConnectionError('connection_change_superseded')
            state.pop('pinAttempt')
            route['phase'] = 'owner_edit'
            op['retiringPin'] = cancel_pin
            op['until'] += 75  # two existing stop_pid waits after cancellation
        # Reserving a candidate is not a connector handover. Suspend the old
        # foreground repair; any already-backgrounded debt stays in its slot.
        if old:
            state['priorRepair'] = old
        prior = old or state.get('debt')
        if prior and prior['setting'] == key:
            # A failed handover can be on either side of promotion. Transfer
            # those exact fences, not authority over the currently active PID.
            for field in ('generation', 'candidateGeneration'):
                if prior.get(field):
                    op[field] = prior[field]
        state["operation"] = op
        self.save(state)
        return op

    def owned(self, state, op_id, phase=None):
        op = state["operation"]
        if not op or op["id"] != op_id or (phase and op["state"] != phase):
            raise ConnectionError("connection_change_superseded")
        return op

    def applying(self, config, op_id):
        op = self.owned(self.load(config), op_id, "applying")
        if (op["boot"] != self.boot or self.clock() >= op["until"]
                or value(config, op["setting"]) != op["from"]):
            raise ConnectionError("connection_change_superseded")
        return op

    def verifying(self, config, op_id, generation):
        state = self.load(config)
        op = self.owned(state, op_id, "applying")
        if 'candidateGeneration' in op and op['candidateGeneration'] != generation:
            raise ConnectionError('connection_change_superseded')
        op.pop('candidateGeneration', None)
        op.update(state="verifying", generation=generation)
        self._retire_previous_repair(state, op)
        self.save(state)

    @staticmethod
    def _retire_previous_repair(state, op):
        state.pop('priorRepair', None)
        if (state.get('debt') or {}).get('setting') == op['setting']:
            state.pop('debt')

    def finish(self, state, result, reason):
        op = state["operation"]
        if not op.get('preserveOutcome'):
            state["outcome"] = {k: op[k] for k in ("id", "setting", "from", "to")} | {"result": result, "reason": reason, "at": now()}
        if result == 'applied' and op['setting'] in AUTOMATIC:
            self._retire_previous_repair(state, op)  # includes an actual save with no connector
        prior = state.pop('priorRepair', None)
        if prior and result == 'failed':
            prior['preserveOutcome'] = True  # repair silently; the new request really failed
            state['operation'] = prior
        else:
            state['operation'] = None
        self.save(state)

    def restore(self, state, reason):
        op = state["operation"]
        if op["setting"] not in AUTOMATIC:
            raise ValueError("only tunnel pins are reversible")
        # The rollback now owns the same saved target and fenced generation.
        self._retire_previous_repair(state, op)
        op.update(state="restoring", reason=reason, until=self.clock() + RESTORE_SECONDS, boot=self.boot)
        self.save(state)

    def supersede(self, config: V2Config, fields: set[str]) -> None:
        if not self.path.exists():
            return
        state = self.load(config)
        dirty = False
        if state.get("pinAttempt") and fields & {FIELDS[k] for k in AUTOMATIC}:
            state.pop("pinAttempt")
            dirty = True
        def replaced(op):
            if not op or FIELDS[op['setting']] not in fields:
                return False
            # A page or legacy-form save which affirms the recovery target must
            # not forget the live connector still needs to catch up with disk.
            return op['state'] != 'restoring' or value(config, op['setting']) != op['from']

        if replaced(state.get('debt')):
            state.pop("debt")
            dirty = True
        if replaced(state['operation']):
            self.finish(state, "superseded", "superseded")
        elif dirty:
            self.save(state)

    @staticmethod
    def guard_restore_target(config, state, fields):
        for op in (state.get('operation'), state.get('debt')):
            if (op and op['state'] == 'restoring' and FIELDS[op['setting']] in fields
                    and value(config, op['setting']) != op['from']):
                raise ConnectionError('connection_change_busy')

    def owns_pin_replica(self, config, attempt_id: str) -> bool | None:
        """None means ownership is unknown: retry without touching either connector."""
        try:
            attempt = self.load(config).get('pinAttempt')
        except ConnectionError as exc:
            if exc.code != 'connection_state_unavailable':
                raise
            self._warn_unavailable()
            return None
        if attempt is None:
            return False
        return attempt['id'] == attempt_id and attempt['boot'] == self.boot

    def _warn_unavailable(self):
        if not self.warned_unavailable:
            self.warned_unavailable = True
            logging.getLogger(__name__).warning('Connection settings journal unavailable; leaving it untouched')

    def legacy_busy(self, config, fields, *, noop=False):
        """A damaged optional journal cannot disable ordinary tunnel supervision/settings."""
        try:
            state = self.load(config)
            self.guard_restore_target(config, state, fields)
            return not noop and bool(state.get('pinAttempt') or (state['operation'] and state['operation']['setting'] in AUTOMATIC))
        except ConnectionError as exc:
            if exc.code != 'connection_state_unavailable':
                return True
            self._warn_unavailable()
            return False

    def legacy_supersede(self, config, fields):
        try:
            self.supersede(config, fields)
        except (ConnectionError, OSError):
            self._warn_unavailable()

    def owns_connector_lane(self) -> bool:
        if not self.path.exists():
            return False
        with config_file_lock():
            return self.legacy_busy(V2Config.load(), set())

    def inspect(self, config):
        state = self.load(config)
        op = state["operation"]
        return {"operation": {k: op[k] for k in ("id", "setting", "from", "to", "state")} if op else None,
                "outcome": state["outcome"], "returnedToAutomatic": state["returnedToAutomatic"]}


journal = Journal()
