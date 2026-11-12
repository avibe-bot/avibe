"""Controller-owned Model Hub generation selection and recovery.

The journal is an engine transaction record, not a replica of channel history.
Provenance authenticates exactly one manifest; its old index is NEVER accepted
into ChannelStateStore or mined for historical withdrawals/sequence floors.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.os_errors import local_os_errno
from vibe.model_hub_runtime.journal import JournalError, read_document, write_document, write_durable

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineGeneration:
    asset_release_tag: str
    runtime_version: str
    platform: str
    archive_sha256: str
    binary_sha256: str
    manifest_sha256: str
    install_dir: str
    bin_path: str
    origin: str
    min_avibe_version: str | None = None
    envelope_sha256: str | None = None

    @classmethod
    def read(cls, value: object, runtime_dir: Path) -> EngineGeneration:
        try:
            if not isinstance(value, dict):
                raise ValueError("generation is not an object")
            generation = cls(**value)
            for name in ("asset_release_tag", "runtime_version", "platform", "install_dir", "bin_path"):
                if not isinstance(getattr(generation, name), str) or not getattr(generation, name):
                    raise ValueError(name)
            for name in ("archive_sha256", "binary_sha256", "manifest_sha256"):
                if not isinstance(getattr(generation, name), str) or not _SHA256.fullmatch(getattr(generation, name)):
                    raise ValueError(name)
            if generation.origin not in {"bundled", "channel"}:
                raise ValueError("origin")
            if generation.origin == "channel":
                if not isinstance(generation.min_avibe_version, str):
                    raise ValueError("minimum")
                if not isinstance(generation.envelope_sha256, str) or not _SHA256.fullmatch(generation.envelope_sha256):
                    raise ValueError("provenance")
            elif generation.envelope_sha256 is not None or generation.min_avibe_version is not None:
                raise ValueError("bundled provenance")
            install = Path(generation.install_dir)
            root = (runtime_dir / "versions").resolve()
            if not install.is_absolute() or root not in install.resolve().parents:
                raise ValueError("install outside managed versions")
            binary = install / generation.bin_path
            if Path(generation.bin_path).is_absolute() or install.resolve() not in binary.resolve().parents:
                raise ValueError("binary outside generation")
            return generation
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            raise JournalError("engine_journal_invalid_generation") from exc

    def document(self) -> dict[str, Any]:
        return asdict(self)


class EngineJournal:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / "lifecycle-state.json"

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "schema_version": 1, "revision": 0, "active": None, "previous": None,
            "health": None, "authorized_package": None, "pending": None,
            "staged": [], "quarantine": {}, "interrupted": {}, "launch": None,
            "preflight": None, "blocked": None,
        }

    def load(self) -> dict[str, Any] | None:
        value = read_document(self.path)
        if value is None:
            return None
        if set(value) != set(self.empty()) or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise JournalError("engine_journal_invalid")
        if type(value["revision"]) is not int or value["revision"] < 0:
            raise JournalError("engine_journal_invalid")
        for role in ("active", "previous"):
            if value[role] is not None:
                EngineGeneration.read(value[role], self.runtime_dir)
        if not isinstance(value["staged"], list):
            raise JournalError("engine_journal_invalid")
        for item in value["staged"]:
            EngineGeneration.read(item, self.runtime_dir)
        for field in ("quarantine", "interrupted"):
            if not isinstance(value[field], dict):
                raise JournalError("engine_journal_invalid")
        if any(not isinstance(tag, str) or not isinstance(reason, str) or not reason for tag, reason in value["quarantine"].items()):
            raise JournalError("engine_journal_invalid")
        if any(not isinstance(tag, str) or type(count) is not int or count < 0 for tag, count in value["interrupted"].items()):
            raise JournalError("engine_journal_invalid")
        if value["health"] not in {None, "preflight_only", "confirmed"}:
            raise JournalError("engine_journal_invalid")
        if value["authorized_package"] is not None and (
            not isinstance(value["authorized_package"], str) or not _SHA256.fullmatch(value["authorized_package"])
        ):
            raise JournalError("engine_journal_invalid")
        if value["blocked"] is not None and not isinstance(value["blocked"], str):
            raise JournalError("engine_journal_invalid")
        pending = value["pending"]
        if pending is not None:
            if not isinstance(pending, dict) or set(pending) != {"id", "old", "candidate", "run_intent", "reason"}:
                raise JournalError("engine_journal_invalid_intent")
            if not isinstance(pending["id"], str) or not _ID.fullmatch(pending["id"]):
                raise JournalError("engine_journal_invalid_intent")
            if type(pending["run_intent"]) is not bool or not isinstance(pending["reason"], str):
                raise JournalError("engine_journal_invalid_intent")
            for role in ("old", "candidate"):
                if pending[role] is not None:
                    EngineGeneration.read(pending[role], self.runtime_dir)
            if pending["candidate"] is None:
                raise JournalError("engine_journal_invalid_intent")
        launch = value["launch"]
        if launch is not None:
            if not isinstance(launch, dict) or set(launch) != {"fingerprint", "generation"}:
                raise JournalError("engine_journal_invalid_launch")
            from core.process_isolation import is_valid_worker_fingerprint

            if not is_valid_worker_fingerprint(launch["fingerprint"]):
                raise JournalError("engine_journal_invalid_launch")
            EngineGeneration.read(launch["generation"], self.runtime_dir)
        preflight = value["preflight"]
        if preflight is not None:
            from core.process_isolation import is_valid_worker_fingerprint

            if not isinstance(preflight, dict) or set(preflight) != {"root", "fingerprint", "generation"}:
                raise JournalError("engine_journal_invalid_preflight")
            if not isinstance(preflight["root"], str):
                raise JournalError("engine_journal_invalid_preflight")
            root = Path(preflight["root"])
            if not root.is_absolute() or root.parent.resolve() != self.runtime_dir.resolve() or (
                not root.name.startswith("engine-preflight-") or root.is_symlink()
            ) or not is_valid_worker_fingerprint(preflight["fingerprint"]):
                raise JournalError("engine_journal_invalid_preflight")
            EngineGeneration.read(preflight["generation"], self.runtime_dir)
        return value

    def save(self, value: dict[str, Any]) -> None:
        value["revision"] += 1
        write_document(self.path, value)

    def retain_bytes(self, raw: bytes, *, kind: str) -> str:
        if kind not in {"manifest", "envelope"}:
            raise ValueError("unknown provenance kind")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.runtime_dir / "provenance" / f"{digest}.{kind}.json"
        # Re-publish even identical visible bytes: visibility after an interrupted
        # write is not proof that its completion boundary was reached.
        write_durable(path, raw)
        return digest

    def provenance(self, digest: str, *, kind: str) -> bytes:
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or kind not in {"manifest", "envelope"}:
            raise JournalError("engine_provenance_invalid")
        try:
            raw = (self.runtime_dir / "provenance" / f"{digest}.{kind}.json").read_bytes()
        except OSError as exc:
            raise JournalError("engine_provenance_unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise JournalError("engine_provenance_invalid")
        return raw


class EngineLifecycleView:
    """Read-only cross-process snapshot; cached status never authorizes a start."""

    @staticmethod
    def read(runtime_dir: Path) -> dict[str, Any]:
        try:
            value = EngineJournal(runtime_dir).load()
            try:
                status = read_document(runtime_dir / "lifecycle-status.json") or {}
            except JournalError:
                status = {}
            return {
                **status, "committed": value["active"] if value else None,
                "previous": value["previous"] if value else None,
                "staged": value["staged"] if value else [],
                "pending": value["pending"] if value else None,
                "quarantine": value["quarantine"] if value else {},
                "running_last_observed": status.get("running"), "running": None,
                "state": "blocked" if value and value["blocked"] else status.get("state", "waiting_for_controller"),
                "reason": value["blocked"] if value and value["blocked"] else status.get("reason"),
            }
        except JournalError as exc:
            return {"state": "blocked", "reason": str(exc), "committed": None, "running": None}


class EngineLifecycle:
    """Synchronous owner; controller callers run all methods on a worker thread.

    The adapter owns async admission/drain. This owner holds the existing
    cross-process mutation fence only during local recovery/activation, never
    across a network fetch or an admission drain.
    """

    def __init__(self, manager, supervisor, *, channel=None) -> None:
        from vibe.model_hub_runtime.engine_channel import EngineChannel

        self.manager = manager
        self.supervisor = supervisor
        self.channel = channel or EngineChannel(manager)
        self.journal = EngineJournal(manager.runtime_dir)
        self.recovered = False
        self.running_release: str | None = None
        self.withdrawn_current = False
        self.on_withdrawal = None
        self._ready = False
        self._start_context = threading.local()
        self._status: dict[str, Any] = {"state": "recovering", "legacy": None}
        self._preflight_passed: set[EngineGeneration] = set()
        # Owner-local serialization must precede the supervisor lock and the
        # cross-process mutation fence. It closes the gap between a readiness
        # check and the activation/start transaction for concurrent callers.
        self._owner_lock = threading.RLock()
        # Recovery takes this before mutation, never mutation before this lock.
        self._preflight_lock = threading.RLock()
        supervisor.lifecycle = self

    @contextmanager
    def mutation(self):
        with self._owner_lock:
            lock = self.manager._acquire_mutation_lock()
            if lock is None:
                raise JournalError("engine_owner_busy")
            try:
                yield
            finally:
                self.manager._release_mutation_lock(lock)

    @staticmethod
    def _infrastructure_failure(error: BaseException) -> bool:
        from vibe.model_hub_runtime.supervisor import EngineUnavailableError

        return isinstance(error, JournalError) or local_os_errno(error) is not None or (
            isinstance(error, EngineUnavailableError)
            and error.reason in {"previous_engine_alive", "engine_untracked"}
        )

    def _load(self) -> dict[str, Any] | None:
        try:
            return self.journal.load()
        except JournalError:
            self.recovered = False
            self._ready = False
            self._status.update(state="recovering", last_result=None, reason="engine_journal_unavailable")
            raise

    def _save(self, value: dict[str, Any]) -> None:
        try:
            EngineJournal.save(self.journal, value)
        except Exception:
            self.recovered = False
            self._ready = False
            self._status.update(state="recovering", last_result=None, reason="engine_journal_unavailable")
            raise

    def _manifest(self, generation: EngineGeneration):
        raw = self.journal.provenance(generation.manifest_sha256, kind="manifest")
        manifest = self.manager._parse_manifest(raw, loaded_from="lifecycle:verified")
        archive = self.manager._manifest_archive_for_platform(manifest) if manifest else None
        if manifest is None or archive is None or (
            manifest.runtime_version != generation.runtime_version
            or str(manifest.payload.get("asset_release_tag") or manifest.runtime_version) != generation.asset_release_tag
            or archive.platform != generation.platform or archive.sha256 != generation.archive_sha256
            or archive.binary_sha256 != generation.binary_sha256 or archive.bin_path != generation.bin_path
        ):
            raise JournalError("engine_generation_identity_invalid")
        return manifest, archive

    @staticmethod
    def _quarantined(generation: dict[str, Any], value: dict[str, Any]) -> bool:
        return any(key in value["quarantine"] for key in (
            generation["asset_release_tag"], f"install:{generation['install_dir']}",
        ))

    @staticmethod
    def _failure_key(generation: EngineGeneration, value: dict[str, Any]) -> str:
        # A forced repair is a new immutable installation, not a new release.
        # Its failure says nothing about the same-release old working install.
        old = value["pending"]["old"] if value["pending"] else value["active"]
        if old and old["asset_release_tag"] == generation.asset_release_tag and old["install_dir"] != generation.install_dir:
            return f"install:{generation.install_dir}"
        return generation.asset_release_tag

    def _quarantine(self, generation: EngineGeneration, value: dict[str, Any], reason: str) -> None:
        value["quarantine"][self._failure_key(generation, value)] = reason
        value["staged"] = [g for g in value["staged"] if not self._quarantined(g, value)]

    def authorize(self, generation: EngineGeneration, *, value: dict[str, Any], verify_installed: bool = True) -> None:
        """Re-authorize one retained identity without reviving old channel memory."""
        from packaging.version import Version

        from core.model_hub_engine_channel import ResolutionAction, authenticate, avibe_version_qualifies

        manifest, archive = self._manifest(generation)
        if self._quarantined(generation.document(), value):
            raise JournalError("engine_quarantined")
        if generation.origin == "bundled":
            if self.journal.provenance(generation.manifest_sha256, kind="manifest") != self.manager.bundled_manifest_bytes():
                raise JournalError("engine_package_changed")
        else:
            raw = self.journal.provenance(generation.envelope_sha256, kind="envelope")
            _envelope, payload = authenticate(raw, roots=self.channel.roots, verifier=self.channel.verifier)
            release = payload.release(generation.asset_release_tag)
            version = avibe_version_qualifies(self.channel._avibe_version)
            if release is None or release.manifest_sha256 != manifest.digest or release.min_avibe_version != generation.min_avibe_version:
                raise JournalError("engine_minimum_or_provenance_invalid")
            if version is None or version < Version(release.min_avibe_version):
                raise JournalError("engine_minimum_not_met")
        # Only CURRENTLY AVAILABLE channel history decides withdrawal. In
        # particular, do not inspect the old provenance payload's tombstones.
        decision = self.channel.resolve(current=generation.asset_release_tag, allow_adoption=False)
        if decision.action in {ResolutionAction.WITHDRAW, ResolutionAction.BLOCKED}:
            raise JournalError("engine_withdrawn")
        if verify_installed and self.manager._verified_manifest_binary(Path(generation.install_dir), manifest, archive) is None:
            raise JournalError("engine_installed_integrity_failed")

    def _existing_bundled(self, *, compatibility: bool = False) -> EngineGeneration | None:
        raw = self.manager.bundled_manifest_bytes()
        manifest = self.manager._parse_manifest(raw, loaded_from="package:bundled")
        archive = self.manager._manifest_archive_for_platform(manifest) if manifest else None
        if not manifest or not archive:
            return None
        directory = self.manager._existing_bundled_install(manifest, archive, prefer_hint=compatibility)
        if directory is None:
            return None
        digest = self.journal.retain_bytes(raw, kind="manifest")
        return EngineGeneration.read({
            "asset_release_tag": str(manifest.payload.get("asset_release_tag") or manifest.runtime_version),
            "runtime_version": manifest.runtime_version, "platform": archive.platform,
            "archive_sha256": archive.sha256, "binary_sha256": archive.binary_sha256,
            "manifest_sha256": digest, "install_dir": str(directory), "bin_path": archive.bin_path,
            "origin": "bundled", "min_avibe_version": None, "envelope_sha256": None,
        }, self.manager.runtime_dir)

    def package_transition_required(self, value=None) -> bool:
        """A changed Avibe package can require its own baseline, not a candidate.

        Ordinary repair never makes this selection. A still-authorized channel
        generation survives a package change unless the new shipped pin is
        newer. Corrupt installed C remains repair of C, even on an upgrade.
        """
        from core.model_hub_engine_channel import ChannelError, release_revision

        value = self._load() if value is None else value
        if not value or not value["active"] or value["authorized_package"] == self.manager.package_identity():
            return False
        generation = EngineGeneration.read(value["active"], self.manager.runtime_dir)
        if generation.origin == "bundled":
            return generation.manifest_sha256 != hashlib.sha256(self.manager.bundled_manifest_bytes()).hexdigest()
        try:
            if release_revision(generation.asset_release_tag) < release_revision(self.channel.bundled_release_tag()):
                return True
        except ValueError:
            pass
        try:
            # Package convergence inspects compatibility, not the old process's
            # health verdict. Its quarantine cannot hide a newly required pin.
            inspected = {**value, "quarantine": {
                key: reason for key, reason in value["quarantine"].items()
                if key not in {generation.asset_release_tag, f"install:{generation.install_dir}"}
            }}
            self.authorize(generation, value=inspected, verify_installed=False)
        except JournalError as exc:
            return str(exc) == "engine_minimum_not_met"
        except ChannelError as exc:
            return exc.reason in {"uncommissioned", "key_unknown"}
        return False

    def _recovery_target(self, selected, *, value) -> EngineGeneration | None:
        from core.model_hub_engine_channel import ResolutionAction

        bundled = self._existing_bundled()
        if selected is None:
            candidates = [bundled]
        else:
            target = EngineGeneration.read(selected, self.manager.runtime_dir)
            decision = self.channel.resolve(current=target.asset_release_tag, allow_adoption=False)
            if decision.action is ResolutionAction.BLOCKED:
                return None
            if decision.action is ResolutionAction.WITHDRAW:
                permitted = decision.target.asset_release_tag if decision.target else None
                retained = [value["previous"], *value["staged"]]
                candidates = [EngineGeneration.read(g, self.manager.runtime_dir) for g in retained if g and g["asset_release_tag"] == permitted]
                if bundled is not None and bundled.asset_release_tag == permitted:
                    candidates.append(bundled)
            else:
                # Corrupt/missing C is repair of C, not permission to repin
                # execution to an older packaged B. Only a genuinely newer
                # shipped baseline changes this required identity below.
                # A changed package's required baseline is an owner transition,
                # independent of whether that baseline happens to be cached.
                candidates = [bundled] if self.package_transition_required(value) else [target]
        for candidate in candidates:
            if candidate is not None:
                try:
                    self.authorize(candidate, value=value)
                    return candidate
                except Exception:
                    continue
        return None

    def recover(self) -> dict[str, Any]:
        """Local only, before admission; a damaged journal is never initialized."""
        with self._preflight_lock, self.mutation():
            self.recovered = False
            self.running_release = None
            value = self._load() or self.journal.empty()
            self._recover_preflight(value)
            launch = value["launch"]
            self.supervisor.recover_processes(launch["fingerprint"] if launch else None)
            pending = value["pending"]
            selected = pending["old"] if pending else value["active"]
            if pending and pending["reason"] in {"update", "required", "confirmation", "package"}:
                candidate = EngineGeneration.read(pending["candidate"], self.manager.runtime_dir)
                key = self._failure_key(candidate, value)
                value["interrupted"][key] = value["interrupted"].get(key, 0) + 1
                if value["interrupted"][key] > 1:
                    self._quarantine(candidate, value, "activation_interrupted")
            target = self._recovery_target(selected, value=value)
            value.update(
                active=target.document() if target else selected, pending=None, launch=None,
                previous=selected if target is not None and selected is not None and selected != target.document() else value["previous"],
                authorized_package=self.manager.package_identity() if target is not None or selected is None else value["authorized_package"],
                health="preflight_only" if target else None,
                blocked="engine_no_authorized_recovery_target" if selected and target is None else None,
            )
            if target is not None:
                value["staged"] = [g for g in value["staged"] if g["asset_release_tag"] != target.asset_release_tag]
            value["staged"] = [g for g in value["staged"] if not self._quarantined(g, value)]
            self._save(value)
            self.recovered = True
            self._ready = not value["blocked"]
            if self._ready:
                self.withdrawn_current = False
            self._status["state"] = "blocked" if value["blocked"] else "idle"
            # Only an interrupted transaction's explicit run intent may resume.
            # A launch witness is ownership evidence, NOT a request to turn a
            # deliberately stopped engine back on.
            if pending and pending["run_intent"] and target is not None:
                self._activate_locked(target, value=value, run_intent=True, reason="recovery")
            self.project_legacy(self._load())
            self.publish_status()
        return self.status()

    def startable(self) -> tuple[EngineGeneration, dict[str, Any]]:
        """Pure resolver called under supervisor lock: no recovery or network."""
        generation = getattr(self._start_context, "generation", None)
        if not self.recovered or generation is None:
            raise JournalError("engine_start_owner_required")
        value = self._load()
        if value is None or value["blocked"]:
            raise JournalError("engine_recovery_pending")
        permitted = value["pending"]["candidate"] if value["pending"] else value["active"]
        if permitted != generation.document() or value["authorized_package"] != self.manager.package_identity():
            raise JournalError("engine_start_not_authorized")
        manifest, archive = self._manifest(generation)
        binary = self.manager._verified_manifest_binary(Path(generation.install_dir), manifest, archive)
        if binary is None:
            raise JournalError("engine_installed_integrity_failed")
        return generation, {
            "path": str(binary), "install_dir": generation.install_dir,
            "version": generation.runtime_version, "asset_release_tag": generation.asset_release_tag,
        }

    def before_launch(self, fingerprint: str) -> None:
        generation, _managed = self.startable()
        value = self._load()
        value["launch"] = {"fingerprint": fingerprint, "generation": generation.document()}
        self._save(value)

    @property
    def starting(self) -> bool:
        return getattr(self._start_context, "generation", None) is not None

    def _start(self, generation: EngineGeneration):
        # Recheck after the old stop/other transaction I/O, not merely before
        # intent. A withdrawal/minimum change learned meanwhile cannot become
        # a fresh provisional start of the now-unauthorized generation.
        value = self._load()
        if value is None:
            raise JournalError("engine_recovery_pending")
        self.authorize(generation, value=value)
        self._start_context.generation = generation
        try:
            connection = self.supervisor.ensure_running()
            if self._load()["pending"] is None:
                self.running_release = generation.asset_release_tag
            return connection
        finally:
            self._start_context.generation = None

    def ensure_running(self):
        """Fenced first-start confirmation. Never performs recovery/downloads."""
        with self._owner_lock:
            if not self.recovered or not self._ready:
                raise JournalError("engine_recovery_pending")
            connection = self.supervisor.live_connection() if hasattr(self.supervisor, "live_connection") else None
            if connection is not None:
                return connection
            with self.mutation():
                value = self._load()
                if value is None or value["active"] is None or value["blocked"]:
                    raise JournalError("engine_no_authorized_generation")
                if value["pending"] is not None:
                    raise JournalError("engine_recovery_pending")
                target = EngineGeneration.read(value["active"], self.manager.runtime_dir)
                self.authorize(target, value=value)
                if value["health"] == "preflight_only" or self.supervisor.is_running():
                    return self._activate_locked(target, value=value, run_intent=True, reason="confirmation")
                return self._start(target)

    def restart_running(self, *, only_lost: bool = False) -> None:
        """Config/credential repair may restart only the committed generation.

        Called before taking the supervisor lock. It is neither an updater nor
        rollback authority, and must preserve the previous activation target.
        """
        from vibe.model_hub_runtime.supervisor import EngineConfigLostError

        with self._owner_lock:
            if not self.recovered or not self._ready:
                raise JournalError("engine_recovery_pending")
            with self.mutation():
                if not self.supervisor.is_running():
                    self.supervisor.with_engine_excluded(lambda _client: None)
                    return
                if only_lost:
                    try:
                        if self.supervisor.live_connection() is not None:
                            return
                    except EngineConfigLostError:
                        pass
                value = self._load()
                if value is None or value["active"] is None or value["blocked"] or value["pending"]:
                    raise JournalError("engine_recovery_pending")
                generation = EngineGeneration.read(value["active"], self.manager.runtime_dir)
                self.authorize(generation, value=value)
                if self.supervisor.running_generation() != generation.document():
                    raise JournalError("engine_running_identity_changed")
                value["pending"] = {
                    "id": uuid.uuid4().hex, "old": generation.document(), "candidate": generation.document(),
                    "run_intent": True, "reason": "restart",
                }
                value["authorized_package"] = self.manager.package_identity()
                self._ready = False
                self._status.update(state="activating", last_result=None)
                self._save(value)
                try:
                    self.supervisor.stop_for_activation()
                    self._start(generation)
                    value = self._load()
                    value.update(pending=None, health="confirmed")
                    self._save(value)
                except Exception:
                    self.recovered = False
                    self.supervisor.stop_for_activation()
                    raise
                self.running_release = generation.asset_release_tag
                self._ready = True
                self._status.update(state="idle", last_result="restarted", reason=None)
                self.publish_status()

    def disable(self) -> None:
        """Stop without racing an owner transaction that captured run intent."""
        with self._owner_lock:
            with self.mutation():
                value = self._load()
                if value is not None and value["pending"] is not None:
                    # An explicit stop is a fresh user decision. Do not let a
                    # previously captured withdrawal intent restart a fallback
                    # after this transaction has completed.
                    value["pending"]["run_intent"] = False
                    self._save(value)
                self.supervisor.disable()

    def _recover_preflight(self, value: dict[str, Any]) -> None:
        from types import SimpleNamespace

        from vibe.model_hub_runtime.state import EngineStateStore
        from vibe.model_hub_runtime.supervisor import EngineSupervisor

        pending = value["preflight"]
        if pending is None:
            return
        isolated = EngineSupervisor(
            installer=SimpleNamespace(), state_store=EngineStateStore(Path(pending["root"])),
        )
        # The independent durable launch witness, not the presence/parseability
        # of a temporary process record, decides that its child is gone.
        isolated.recover_processes(pending["fingerprint"])
        root = Path(pending["root"])
        if root.exists():
            shutil.rmtree(root)
        value["preflight"] = None

    def preflight(self, generation: EngineGeneration) -> None:
        """Isolated empty config/auth; no user state or upstream request exists."""
        from vibe.model_hub_runtime.state import EngineStateStore
        from vibe.model_hub_runtime.supervisor import EngineSupervisor

        manifest, archive = self._manifest(generation)
        binary = self.manager._verified_manifest_binary(Path(generation.install_dir), manifest, archive)
        if binary is None:
            raise JournalError("engine_installed_integrity_failed")
        outer = self

        class IsolatedInstall:
            def status(self):
                return {"installed": True, "path": str(binary), "install_dir": generation.install_dir,
                        "version": generation.runtime_version}

            def contract_manifest(self):
                return outer.manager.contract_manifest()

        with self._preflight_lock, tempfile.TemporaryDirectory(prefix="engine-preflight-", dir=self.manager.runtime_dir) as root:
            def spawn_isolated(*args, env, **kwargs):
                from core.process_isolation import PROCESS_IDENTITY_ENV, fingerprint_process_marker

                with self.mutation():
                    value = self._load() or self.journal.empty()
                    value["preflight"] = {
                        "root": root, "generation": generation.document(),
                        "fingerprint": fingerprint_process_marker(env[PROCESS_IDENTITY_ENV]),
                    }
                    self._save(value)  # before spawn, independently of temporary records
                # This is isolation of normal CPA inputs, not a sandbox or a
                # qualification authority. Never inherit the owner's home or
                # credential/config locations even for an empty-config probe.
                environment = dict(env)
                environment.update({name: root for name in (
                    "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "AVIBE_HOME",
                    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                )})
                return subprocess.Popen(*args, env=environment, **kwargs)

            supervisor = EngineSupervisor(
                installer=IsolatedInstall(), state_store=EngineStateStore(Path(root)), process_factory=spawn_isolated,
            )
            try:
                supervisor.ensure_running()
                supervisor.reload_config_if_running([])
            finally:
                try:
                    supervisor.stop_for_activation()
                except Exception:
                    self.recovered = False
                    raise
                with self.mutation():
                    value = self._load() or self.journal.empty()
                    value["preflight"] = None
                    self._save(value)

    def prepare_candidate(self, generation: EngineGeneration) -> None:
        if generation in self._preflight_passed:
            return
        from vibe.model_hub_runtime.supervisor import EngineUnavailableError

        with self.mutation():
            value = self._load() or self.journal.empty()
            self.authorize(generation, value=value)
        try:
            self.preflight(generation)
        except EngineUnavailableError as exc:
            if not self._infrastructure_failure(exc):
                with self.mutation():
                    value = self._load() or self.journal.empty()
                    self._quarantine(generation, value, "preflight_failed")
                    self._save(value)
                    self._status.update(state="rolled_back", last_result="preflight_failed")
                    self.publish_status()
            raise
        self._preflight_passed.add(generation)

    def activate(self, generation: EngineGeneration, *, reason: str = "update") -> dict[str, Any]:
        if not self.recovered:
            raise JournalError("engine_recovery_pending")
        # Isolated work may overlap the OLD real-auth engine, never another
        # real-auth process. It occurs before the adapter's short swap fence.
        self.prepare_candidate(generation)
        with self.mutation():
            if not self.recovered:
                raise JournalError("engine_recovery_pending")
            value = self._load() or self.journal.empty()
            self.authorize(generation, value=value)
            if reason in {"update", "withdrawal"} and generation.origin == "channel":
                current = value["active"]["asset_release_tag"] if value["active"] else self.channel.bundled_release_tag()
                decision = self.channel.resolve(current=current, quarantined=frozenset(value["quarantine"]))
                if decision.target is None or decision.target.asset_release_tag != generation.asset_release_tag:
                    raise JournalError("engine_selection_changed")
            value["blocked"] = None
            run_intent = self.supervisor.is_running() or bool(
                value["pending"] and value["pending"]["reason"] == "withdrawal" and value["pending"]["run_intent"]
            )
            self._activate_locked(generation, value=value, run_intent=run_intent, reason=reason)
            self.project_legacy(self._load())
            self.publish_status()
        return self.status()

    def _activate_locked(self, generation: EngineGeneration, *, value, run_intent: bool, reason: str):
        retained_previous = value["previous"]
        old = value["previous"] if reason == "confirmation" else value["active"]
        value["pending"] = {
            "id": uuid.uuid4().hex, "old": old, "candidate": generation.document(),
            "run_intent": run_intent, "reason": reason,
        }
        value["authorized_package"] = self.manager.package_identity()
        # If this does not return, do not signal the old process.
        self._ready = False
        self._status.update(state="activating", last_result=None, reason=None)
        self._save(value)
        try:
            self.supervisor.stop_for_activation()
        except Exception:
            self.recovered = False
            raise
        connection = None
        try:
            if run_intent:
                connection = self._start(generation)
            else:
                # activate() already completed isolated preflight. A stopped
                # engine is selected without silently starting real-auth CPA.
                pass
        except Exception as failure:
            try:
                self.supervisor.stop_for_activation()
            except Exception:
                self.recovered = False
                raise
            if self._infrastructure_failure(failure):
                self.recovered = False
                raise
            value = self._load()
            self._quarantine(generation, value, type(failure).__name__)
            value["launch"] = None  # provisional process was positively stopped
            # Known failing candidates are persisted before any fallback start.
            self._save(value)
            self._status["state"] = "rolling_back"
            previous = EngineGeneration.read(old, self.manager.runtime_dir) if old else None
            try:
                if previous is None:
                    raise JournalError("engine_no_rollback_target")
                self.authorize(previous, value=value)
                value["pending"]["candidate"] = previous.document()
                self._save(value)
                if run_intent:
                    self._start(previous)
                    # _start/before_launch persist a fresh witness. Reload it
                    # before settlement so the stale rollback snapshot cannot
                    # erase ownership evidence for the survivor.
                    value = self._load()
                value.update(active=previous.document(), pending=None, health="confirmed" if run_intent else "preflight_only", blocked=None)
                self._save(value)
                self.running_release = previous.asset_release_tag if run_intent else None
                self._ready = True
                self._status.update(state="rolled_back", last_result="rolled_back")
                self.publish_status()
            except Exception as rollback_failure:
                try:
                    self.supervisor.stop_for_activation()
                except Exception:
                    self.recovered = False
                    raise
                if previous is not None and not self._infrastructure_failure(rollback_failure):
                    self._quarantine(previous, value, type(rollback_failure).__name__)
                value.update(pending=None, launch=None, blocked="engine_rollback_failed")
                self._save(value)
                self._status["state"] = "blocked"
            raise failure
        value = self._load()
        value.update(active=generation.document(), previous=retained_previous if reason == "recovery" else old, pending=None,
                     health="confirmed" if run_intent else "preflight_only", blocked=None)
        value["staged"] = [g for g in value["staged"] if g["asset_release_tag"] != generation.asset_release_tag]
        try:
            self._save(value)
        except Exception:
            # An ambiguous completion never leaks a provisional process.
            self.recovered = False
            self.supervisor.stop_for_activation()
            raise
        self.running_release = generation.asset_release_tag if run_intent else None
        self._ready = True
        self.withdrawn_current = False
        self._status.update(state="applied", last_result="applied")
        self.publish_status()
        return connection

    def _legacy_hint(self, bundled: EngineGeneration | None) -> dict[str, Any]:
        """Inspect compatibility exposure only; never feed it to selection."""
        try:
            pointer = read_document(self.manager.runtime_dir / "current.json")
        except JournalError:
            return {"state": "unreadable", "asset_release_tag": None}
        if pointer is None:
            return {"state": "absent", "asset_release_tag": None}
        eligible = bundled is not None and all(pointer.get(key) == expected for key, expected in {
            "install_dir": bundled.install_dir, "runtime_version": bundled.runtime_version,
            "platform": bundled.platform, "manifest_sha256": bundled.manifest_sha256,
            "archive_sha256": bundled.archive_sha256, "bin_path": bundled.bin_path,
        }.items())
        return {
            "state": "eligible" if eligible else "stale",
            "asset_release_tag": bundled.asset_release_tag if eligible else None,
            # These are explicitly untrusted retained-hint fields. In
            # particular, upstream version alone cannot identify B_old/B_new.
            "runtime_version": pointer.get("runtime_version"),
            "install_dir": pointer.get("install_dir"),
            "manifest_sha256": pointer.get("manifest_sha256"),
            "archive_sha256": pointer.get("archive_sha256"),
        }

    def project_legacy(self, value: dict[str, Any]) -> None:
        """Post-settlement bundled-only/absent hint, independent of execution."""
        pointer = self.manager.runtime_dir / "current.json"
        bundled = None
        error = None
        try:
            bundled = self._existing_bundled(compatibility=True)
            if bundled is not None:
                self.authorize(bundled, value=value)
        except Exception:
            # Unqualified is never advertised; inspection/provenance failure is
            # also not permission to fail an already committed activation.
            bundled = None
        try:
            if bundled is not None:
                manifest, archive = self._manifest(bundled)
                self.manager._write_current_pointer(Path(bundled.install_dir), manifest, archive)
            else:
                pointer.unlink(missing_ok=True)
        except Exception:
            error = "legacy_hint_write_failed"
        hint = self._legacy_hint(bundled)
        if hint["state"] in {"stale", "unreadable"}:
            error = error or "legacy_hint_stale"
        self._status.update(
            legacy=bundled.document() if bundled is not None and hint["state"] == "eligible" else None,
            legacy_hint=hint, legacy_error=error,
            legacy_projection="pending" if error else "settled",
        )

    def retry_required(self) -> None:
        """Explicit owner retry, never fresh-start or selection authority."""
        with self.mutation():
            value = self._load() or self.journal.empty()
            if value["active"] is None or self.package_transition_required(value):
                return  # the new package owns its baseline; never retry its stale predecessor
            generation = EngineGeneration.read(value["active"], self.manager.runtime_dir)
            for key in (generation.asset_release_tag, f"install:{generation.install_dir}"):
                value["quarantine"].pop(key, None)
                value["interrupted"].pop(key, None)
            # Validate every other authority BEFORE publishing the clearance.
            # Corrupt bytes are repairable; lost provenance/minimum/withdrawal
            # authorization is not. A failed sibling's quarantine stays intact.
            self.authorize(generation, value=value, verify_installed=False)
            self._save(value)
            self._preflight_passed.discard(generation)

    def check(self, *, refresh: bool = True, allow_adoption: bool = True, retry: bool = False):
        """Metadata-only operation: no engine start or selected-generation write."""
        from dataclasses import asdict
        from datetime import datetime, timezone

        channel_status = self.channel.refresh() if refresh else None
        value = self._load() or self.journal.empty()
        bundled = self.manager.bundled_manifest()
        current = value["active"]["asset_release_tag"] if value["active"] else (
            str(bundled.payload.get("asset_release_tag") or bundled.runtime_version) if bundled else "unknown"
        )
        decision = self.channel.resolve(
            current=current, quarantined=frozenset() if retry else frozenset(value["quarantine"]),
            allow_adoption=allow_adoption,
        )
        # Loop-owned admission checks this volatile flag before taking a fresh
        # lease, including the gap before this worker's completion is delivered.
        self.withdrawn_current = decision.action.value in {"withdraw", "blocked"}
        if self.withdrawn_current and self.on_withdrawal is not None:
            self.on_withdrawal()
        self._status.update(
            state="available" if decision.target else "blocked" if decision.action.value == "blocked" else "up_to_date",
            available=decision.target.asset_release_tag if decision.target else None,
            channel=asdict(channel_status) if channel_status else None,
            last_checked_at=datetime.now(timezone.utc).isoformat(), reason=decision.reason,
        )
        if retry and decision.target is None and decision.action.value == "keep":
            self.retry_required()
        if retry or value["staged"]:
            with self.mutation():
                value = self._load() or self.journal.empty()
                required_tags = {current, self.channel.bundled_release_tag()}
                if decision.target is not None:
                    required_tags.add(decision.target.asset_release_tag)
                retained_stages = [g for g in value["staged"] if g["asset_release_tag"] in required_tags]
                changed = retained_stages != value["staged"]
                value["staged"] = retained_stages
                if retry and decision.target is not None:
                    value["quarantine"].pop(decision.target.asset_release_tag, None)
                    value["interrupted"].pop(decision.target.asset_release_tag, None)
                    self._preflight_passed = {
                        g for g in self._preflight_passed if g.asset_release_tag != decision.target.asset_release_tag
                    }
                    changed = True
                if changed:
                    self._save(value)
        self.publish_status()
        return decision

    def stage_required(self, *, offline: bool = False, **kwargs) -> dict[str, Any]:
        """Repair authorized required bytes, never the available candidate."""
        value = self._load() or self.journal.empty()
        generation = EngineGeneration.read(value["active"], self.manager.runtime_dir) if value["active"] else None
        if generation is not None and generation.origin == "bundled" and (
            generation.manifest_sha256 != hashlib.sha256(self.manager.bundled_manifest_bytes()).hexdigest()
        ):
            # A new Avibe package owns its new bundled baseline. This is not a
            # channel C -> B repair; selection still requires owner preflight.
            generation = None
        if generation is not None:
            # Failure is a blocked repair of this exact identity, never an
            # implicit switch back to the packaged baseline.
            self.authorize(generation, value=value, verify_installed=False)
        raw = self.journal.provenance(generation.manifest_sha256, kind="manifest") if generation else self.manager.bundled_manifest_bytes()
        manager = self.manager.offline_copy() if offline else self.manager
        return manager.stage_manifest(raw, generation=generation, **kwargs)

    def stage_package_baseline(self) -> EngineGeneration:
        result = self.manager.stage_manifest(self.manager.bundled_manifest_bytes())
        if not result.get("ok"):
            raise JournalError(str(result.get("reason") or "engine_stage_failed"))
        return EngineGeneration.read(result["generation"], self.manager.runtime_dir)

    def stage_decision(self, decision) -> EngineGeneration | None:
        if decision.target is None:
            return None
        snapshot = self.channel.snapshot()
        release = snapshot.payload.release(decision.target.asset_release_tag) if snapshot else None
        if release != decision.target:
            raise JournalError("engine_selection_changed")
        stager = self.manager if decision.download_authorized else self.manager.offline_copy()
        result = stager.stage_manifest(
            release.manifest_bytes, envelope=snapshot.envelope.raw, minimum=release.min_avibe_version,
        )
        if not result.get("ok"):
            raise JournalError(str(result.get("reason") or "engine_stage_failed"))
        return EngineGeneration.read(result["generation"], self.manager.runtime_dir)

    def pause_withdrawn(self) -> None:
        """Stop recalled execution BEFORE potentially slow fallback staging.

        This intent carries only engine run intent, not withdrawal history. If
        channel/state.json is lost, provenance must not replay that history.
        """
        with self.mutation():
            value = self._load() or self.journal.empty()
            active = value["active"]
            run_intent = self.supervisor.is_running() or bool(
                value["pending"] and value["pending"]["reason"] == "withdrawal" and value["pending"]["run_intent"]
            )
            if active is not None:
                value["pending"] = {
                    "id": uuid.uuid4().hex, "old": active, "candidate": active,
                    "run_intent": run_intent, "reason": "withdrawal",
                }
            value["blocked"] = "engine_withdrawn"
            self._ready = False
            self._save(value)
            try:
                self.supervisor.stop_for_activation()
            except Exception:
                self.recovered = False
                raise
            self._status.update(state="blocked", reason="engine_withdrawn")
            self.project_legacy(value)
            self.publish_status()

    def block(self, reason: str) -> None:
        with self.mutation():
            value = self._load() or self.journal.empty()
            # A blocked stop is also preceded by a durable decision.
            self._ready = False
            value["blocked"] = reason
            self._save(value)
            try:
                self.supervisor.stop_for_activation()
            except Exception:
                self.recovered = False
                raise
            self._status.update(state="blocked", reason=reason)
            self.project_legacy(value)
            self.publish_status()

    def publish_status(self) -> None:
        from core.managed_runtime import write_json_atomic

        try:
            write_json_atomic(self.manager.runtime_dir / "lifecycle-status.json", self.status())
        except OSError:
            logger.warning("Could not publish Model Hub engine status snapshot", exc_info=True)

    def status(self) -> dict[str, Any]:
        try:
            value = self._load()
            return {**self._status, "committed": value["active"] if value else None,
                    "state": "blocked" if value and value["blocked"] else self._status.get("state"),
                    "reason": value["blocked"] if value and value["blocked"] else self._status.get("reason"),
                    "previous": value["previous"] if value else None,
                    "staged": value["staged"] if value else [],
                    "running": self.supervisor.running_generation() if self._ready and self.recovered and value and not value["pending"] else None,
                    "recovered": self.recovered}
        except JournalError as exc:
            return {**self._status, "state": "blocked", "reason": str(exc), "recovered": False}
