"""Activation-journal boundaries (hermetic files; not power-loss certification).

These tests protect an outcome existing install tests cannot see: a transaction
must not report durable completion after any write/flush/replace failure, and a
reader must distinguish absence from damaged recovery authority. Provenance may
survive channel history loss, but must never recreate that history.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from vibe.model_hub_runtime import journal
from vibe.model_hub_runtime.lifecycle import EngineGeneration, EngineJournal


def generation(root: Path) -> dict:
    return {
        "asset_release_tag": "model-hub-engine-v8.0.63-2",
        "runtime_version": "v8.0.63", "platform": "linux-amd64",
        "archive_sha256": "a" * 64, "binary_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
        "install_dir": str(root / "versions" / "candidate"),
        "bin_path": "cli-proxy-api", "origin": "bundled",
        "min_avibe_version": None, "envelope_sha256": None,
    }


def test_journal_round_trip_and_unavailable_is_not_empty(tmp_path):
    store = EngineJournal(tmp_path)
    assert store.load() is None
    value = store.empty()
    value["active"] = generation(tmp_path)
    value["health"] = "preflight_only"
    store.save(value)
    assert store.load() == value
    store.path.write_bytes(b"{")
    with pytest.raises(journal.JournalError, match="unreadable"):
        store.load()
    assert store.path.read_bytes() == b"{"


@pytest.mark.parametrize("raw", [b"null", b"[]", b'"document"', b'{}', b'{"schema_version":2}'])
def test_invalid_journal_never_becomes_fresh_state(tmp_path, raw):
    store = EngineJournal(tmp_path)
    store.path.write_bytes(raw)
    with pytest.raises(journal.JournalError):
        store.load()


@pytest.mark.parametrize("field,value", [
    ("install_dir", "/tmp/foreign-engine"), ("bin_path", "../foreign"),
    ("archive_sha256", "not-a-digest"), ("origin", "trusted"),
    ("envelope_sha256", "a" * 64), ("runtime_version", None),
])
def test_generation_record_cannot_escape_or_invent_origin(tmp_path, field, value):
    item = generation(tmp_path)
    item[field] = value
    with pytest.raises(journal.JournalError):
        EngineGeneration.read(item, tmp_path)


@pytest.mark.parametrize("boundary", ["file_flush", "replace", "parent_flush"])
def test_journal_publication_failure_is_not_completion(tmp_path, monkeypatch, boundary):
    path = tmp_path / "lifecycle-state.json"
    journal.write_document(path, {"revision": 1})
    original_fsync = journal.os.fsync
    original_replace = journal._replace
    replaced = False

    def replace(source, target):
        nonlocal replaced
        if boundary == "replace":
            raise OSError("injected replace failure")
        original_replace(source, target)
        replaced = True

    def fsync(descriptor):
        import stat

        directory = stat.S_ISDIR(journal.os.fstat(descriptor).st_mode)
        if (boundary == "file_flush" and not directory) or (boundary == "parent_flush" and replaced):
            raise OSError("injected flush failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(journal, "_replace", replace)
    monkeypatch.setattr(journal.os, "fsync", fsync)
    if boundary == "parent_flush" and journal.os.name == "nt":
        pytest.skip("Windows publication completion is MoveFileExW WRITE_THROUGH")
    with pytest.raises(journal.JournalError, match="unavailable"):
        journal.write_document(path, {"revision": 2})
    # A failed parent flush can leave the new bytes visible. That is NOT a
    # returned success, and must not be equated with a committed transaction.
    assert json.loads(path.read_bytes())["revision"] == (2 if replaced else 1)
    assert list(tmp_path.glob(".lifecycle-state.json-*")) == []


def test_provenance_is_exact_bytes_not_channel_history(tmp_path):
    store = EngineJournal(tmp_path)
    raw = b'{ "signed": "envelope bytes, not a local replay floor" }\n'
    digest = store.retain_bytes(raw, kind="envelope")
    assert digest == hashlib.sha256(raw).hexdigest()
    assert store.provenance(digest, kind="envelope") == raw
    assert not (tmp_path / "channel").exists()
    (tmp_path / "provenance" / f"{digest}.envelope.json").write_bytes(raw + b" ")
    with pytest.raises(journal.JournalError, match="invalid"):
        store.provenance(digest, kind="envelope")


def fixture_manifest(
    root: Path, *, version: str = "v8.0.63", build: int = 2, archive_format: str | None = None,
) -> bytes:
    import io
    import tarfile
    import zipfile

    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    root.mkdir()
    platform = EngineRuntimeManager(runtime_dir=root / "unused").host_platform()
    windows = platform.startswith("windows")
    bin_path = "cli-proxy-api.exe" if windows else "cli-proxy-api"
    # These are signed/digested byte fixtures, not native health-test engines.
    # Neither staging nor RecordingSupervisor may execute a version probe.
    binary = f"#!/bin/sh\necho 'CLIProxyAPI Version: {version.removeprefix('v')}'\n".encode()
    archive_format = archive_format or ("zip" if windows else "tar.gz")
    archive = root / ("engine.zip" if archive_format == "zip" else "engine.tar.gz")
    if archive_format == "zip":
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(bin_path, binary)
    else:
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(bin_path)
            info.size = len(binary)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(binary))
    return json.dumps({
        "schema_version": 1, "name": "cliproxyapi", "version": version,
        "release_tag": version, "asset_release_tag": f"model-hub-engine-{version}-{build}",
        "source_sha": "a" * 40, "license": "MIT", "assets": [{
            "platform": platform,
            "url": archive.as_uri(), "format": archive_format, "size_bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary).hexdigest(), "bin_path": bin_path,
        }],
    }).encode()


def test_no_controller_stages_without_selecting_or_writing_legacy_hint(tmp_path, monkeypatch):
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    raw = fixture_manifest(tmp_path / "bundle")
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(manager, "bundled_manifest_bytes", lambda: raw)
    result = manager.ensure()
    assert result["ok"], result
    assert result["state"] == "staged"
    assert result["reason"] == "waiting_for_controller"
    assert not (manager.runtime_dir / "current.json").exists()
    value = EngineJournal(manager.runtime_dir).load()
    assert value["active"] is None
    assert value["pending"] is None
    assert len(value["staged"]) == 1
    binary = Path(result["path"])
    assert binary.is_file()
    assert manager.resolve_binary() == binary
    assert manager.ensure()["changed"] is False
    assert not (manager.runtime_dir / "current.json").exists()


def test_required_manifest_ignores_staged_candidate_and_foreign_legacy_hint(tmp_path, monkeypatch):
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    bundled = fixture_manifest(tmp_path / "bundle", version="v8.0.30")
    candidate = fixture_manifest(tmp_path / "candidate")
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(manager, "bundled_manifest_bytes", lambda: bundled)
    baseline = manager.ensure()
    result = manager.stage_manifest(candidate, envelope=b"test-only provenance", minimum="3.2.6")
    assert result["ok"], result
    # Staging never leaks into ordinary status/repair/manifest resolution.
    assert manager.contract_manifest()["version"] == "v8.0.30"
    assert manager.status()["path"] == baseline["path"]
    store = EngineJournal(manager.runtime_dir)
    value = store.load()
    value.update(active=result["generation"], authorized_package=manager.package_identity(), health="preflight_only")
    store.save(value)
    # This deliberately writes a bundled hint that disagrees with committed C.
    (manager.runtime_dir / "current.json").write_text(json.dumps({"install_dir": baseline["install_dir"]}))
    assert manager.contract_manifest()["version"] == "v8.0.63"
    assert manager.status()["path"] == result["path"]
    repair = manager.ensure()
    assert repair["ok"] and repair["path"] == result["path"]
    assert json.loads((manager.runtime_dir / "current.json").read_text())["install_dir"] == baseline["install_dir"]
    clean = manager.clean(keep_previous=0)
    assert clean["ok"], clean
    assert Path(baseline["path"]).is_file() and Path(result["path"]).is_file()
    # Neither cleanup nor another ensure can heal a damaged lifecycle record by
    # falling back to the legacy pointer or silently selecting the packaged pin.
    store.path.write_bytes(b"damaged")
    assert not manager.ensure()["ok"]
    assert not manager.clean(keep_previous=0)["ok"]
    assert Path(baseline["path"]).is_file() and Path(result["path"]).is_file()


class RecordingSupervisor:
    """A process boundary fixture; journal/installer/channel remain real."""
    def __init__(self):
        self.lifecycle = None
        self.running = None
        self.events = []
        self.fail_tag = None

    def is_running(self):
        return self.running is not None

    def running_generation(self):
        return self.running

    def live_connection(self):
        return "test connection" if self.running is not None else None

    def with_engine_excluded(self, operation):
        assert self.running is None
        return operation(None)

    def recover_processes(self, witness):
        self.events.append(("recover", witness))
        self.running = None

    def stop_for_activation(self):
        if self.running is not None:
            state = self.lifecycle.journal.load()
            assert state["pending"] is not None or state["blocked"] is not None or not self.lifecycle.recovered
        self.events.append(("stop", self.running))
        self.running = None
        self.lifecycle.running_release = None

    def disable(self):
        self.events.append(("disable", self.running))


    def ensure_running(self):
        if not self.lifecycle.starting:
            return self.lifecycle.ensure_running()
        from core.process_isolation import fingerprint_process_marker, new_process_identity_marker
        from vibe.model_hub_runtime.supervisor import EngineUnavailableError

        target, _managed = self.lifecycle.startable()
        assert self.running is None
        self.lifecycle.before_launch(fingerprint_process_marker(new_process_identity_marker()))
        self.events.append(("start", target.asset_release_tag))
        if target.asset_release_tag == self.fail_tag:
            raise EngineUnavailableError("models.engine.health_failed")
        self.running = target.document()
        return "test connection"


def owner_fixture(tmp_path, monkeypatch, *, bundled_version="v8.0.30"):
    from vibe.model_hub_runtime.installer import EngineRuntimeManager
    from vibe.model_hub_runtime.lifecycle import EngineLifecycle

    bundled = fixture_manifest(tmp_path / "bundle", version=bundled_version)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    monkeypatch.setattr(manager, "bundled_manifest_bytes", lambda: bundled)
    asset = json.loads(bundled)["assets"][0]
    archive_name = "engine.zip" if asset["platform"].startswith("windows") else "engine.tar.gz"
    cached = manager.runtime_dir / "downloads" / f"{asset['sha256']}-{archive_name}"
    cached.parent.mkdir(parents=True)
    cached.write_bytes((tmp_path / "bundle" / archive_name).read_bytes())
    staged = manager.ensure()
    assert staged["ok"], staged
    supervisor = RecordingSupervisor()
    owner = EngineLifecycle(manager, supervisor)
    monkeypatch.setattr(owner, "preflight", lambda target: supervisor.events.append(("preflight", target.asset_release_tag)))
    owner.recover()
    return owner, supervisor, staged


def test_repeated_repairs_automatically_reclaim_retired_installations(tmp_path, monkeypatch):
    owner, _supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    clean = owner.manager.clean
    observed = []

    def settled_clean(**kwargs):
        value = owner.journal.load()
        result = clean(**kwargs)
        observed.append((value, result))
        return result

    monkeypatch.setattr(owner.manager, "clean", settled_clean)
    generations = [baseline["generation"]]
    for _ in range(5):
        result = owner.stage_required(force=True)
        assert result["ok"], result
        assert observed, "byte-only staging must restore automatic post-install retention"
        value, cleaned = observed[-1]
        assert cleaned["ok"], cleaned  # the installer fence was released
        assert result["generation"] in value["staged"]
        for role in (value["active"], value["previous"], *value["staged"]):
            if role is not None:
                assert Path(role["install_dir"]).exists()
        target = EngineGeneration.read(result["generation"], owner.manager.runtime_dir)
        owner.activate(target, reason="required")
        generations.append(target.document())
    # The next ordinary required-file stage cleans roles retired by the last
    # activation, without a user cleanup command or any execution change.
    assert owner.stage_required()["ok"]
    value = owner.journal.load()
    protected = {g["install_dir"] for g in (value["active"], value["previous"], baseline["generation"])}
    assert {g["install_dir"] for g in generations if Path(g["install_dir"]).exists()} == protected
    assert owner.status()["legacy"]["install_dir"] == baseline["generation"]["install_dir"]


@pytest.mark.parametrize("phase", ["preflight", "activation", "rollback"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "cause"])
def test_local_spawn_errors_never_quarantine_a_healthy_generation(tmp_path, monkeypatch, phase, wrapped):
    import errno

    from core.os_errors import local_os_errno
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)

    def environmental_failure():
        if wrapped:
            try:
                raise OSError(errno.EAGAIN, "fixture process resource shortage")
            except OSError as cause:
                raise EngineUnavailableError("models.engine.start_failed") from cause
        raise EngineUnavailableError("models.engine.start_failed", os_errno=errno.EAGAIN)

    if phase == "preflight" and wrapped:
        # Exercise the actual supervisor spawn wrapper, not just a fabricated
        # exception: it keeps EAGAIN in __cause__ with os_errno unset.
        from vibe.model_hub_runtime import lifecycle

        monkeypatch.setattr(owner, "preflight", type(owner).preflight.__get__(owner))

        def exhausted_spawn(command, **_kwargs):
            assert command[0] == str(Path(candidate.install_dir) / candidate.bin_path)
            raise OSError(errno.EAGAIN, "fixture process resource shortage")

        monkeypatch.setattr(lifecycle.subprocess, "Popen", exhausted_spawn)
    elif phase == "preflight":
        monkeypatch.setattr(owner, "preflight", lambda _target: environmental_failure())
    else:
        start = supervisor.ensure_running
        supervisor.fail_tag = candidate.asset_release_tag if phase == "rollback" else None

        def start_or_fail():
            target = getattr(owner._start_context, "generation", None)
            failed_tag = candidate.asset_release_tag if phase == "activation" else baseline["generation"]["asset_release_tag"]
            if target is not None and target.asset_release_tag == failed_tag:
                environmental_failure()
            return start()

        monkeypatch.setattr(supervisor, "ensure_running", start_or_fail)
    with pytest.raises(EngineUnavailableError) as failure:
        owner.activate(candidate)
    if phase != "rollback":
        assert local_os_errno(failure.value) == errno.EAGAIN
        if wrapped:
            assert failure.value.os_errno is None
    value = owner.journal.load()
    assert baseline["generation"]["asset_release_tag"] not in value["quarantine"]
    if phase == "rollback":
        assert value["quarantine"] == {candidate.asset_release_tag: "EngineUnavailableError"}
    else:
        assert value["quarantine"] == {}
    if phase == "preflight":
        assert owner.recovered and supervisor.is_running()
        assert candidate not in owner._preflight_passed
    elif phase == "activation":
        assert not owner.recovered and value["pending"] is not None
        owner.recover()
        assert owner.journal.load()["active"] == baseline["generation"]
    else:
        assert value["blocked"] == "engine_rollback_failed"
        assert not supervisor.is_running()


def test_concurrent_first_start_waits_for_the_owner_transaction(tmp_path, monkeypatch):
    owner, supervisor, _staged = owner_fixture(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    original = supervisor.ensure_running

    def blocked_start():
        nonlocal calls
        with calls_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(5)
        return original()

    monkeypatch.setattr(supervisor, "ensure_running", blocked_start)
    results = []
    errors = []

    def call():
        try:
            results.append(owner.ensure_running())
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.05)
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert not errors
    assert len(results) == 2
    assert supervisor.running == owner.journal.load()["active"]


def test_recovery_and_fenced_first_start_publish_health_before_return(tmp_path, monkeypatch):
    owner, supervisor, staged = owner_fixture(tmp_path, monkeypatch)
    assert owner.journal.load()["health"] == "preflight_only"
    assert not supervisor.is_running()
    assert supervisor.ensure_running() == "test connection"
    value = owner.journal.load()
    assert value["active"] == staged["generation"]
    assert value["pending"] is None and value["health"] == "confirmed"
    assert value["launch"]["generation"] == value["active"]
    assert owner.status()["running"] == value["active"]


@pytest.mark.parametrize("failure", ["intent", "launch", "commit"])
def test_activation_journal_failure_never_returns_applied_or_keeps_provisional_process(tmp_path, monkeypatch, failure):
    from vibe.model_hub_runtime import lifecycle

    owner, supervisor, _staged = owner_fixture(tmp_path, monkeypatch)
    actual = lifecycle.write_document

    def interrupted(path, value):
        fail = (
            (failure == "intent" and value["pending"] is not None and value["launch"] is None)
            or (failure == "launch" and value["launch"] is not None and value["pending"] is not None)
            or (failure == "commit" and value["health"] == "confirmed" and value["pending"] is None)
        )
        if fail:
            raise journal.JournalError("injected completion failure")
        actual(path, value)

    monkeypatch.setattr(lifecycle, "write_document", interrupted)
    with pytest.raises(journal.JournalError):
        owner.ensure_running()
    assert owner.recovered is False
    assert supervisor.running is None
    if failure == "intent":
        assert not any(kind == "start" for kind, _ in supervisor.events)
    assert owner.status()["state"] != "applied"


def signed_candidate(owner, tmp_path, *, build=2, version="v8.0.63"):
    from tests.engine_channel_support import LocalVerifier, Signer, manifest_bytes, release_entry, signed
    from vibe.model_hub_runtime.engine_channel import EngineChannel

    directory = tmp_path / f"candidate-{build}"
    # The owned signed-channel contract uses tar.gz on ALL six platforms,
    # including Windows. Do not put locally packaged zip bytes under that name.
    local = json.loads(fixture_manifest(directory, build=build, version=version, archive_format="tar.gz"))["assets"][0]
    raw = json.loads(manifest_bytes(build=build, version=version))
    host = owner.manager.host_platform()
    asset = next(item for item in raw["assets"] if item["platform"] == host)
    asset.update({key: local[key] for key in ("size_bytes", "sha256", "binary_sha256", "bin_path")})
    manifest = json.dumps(raw).encode()
    name = asset["url"].rsplit("/", 1)[1]
    cached = owner.manager.runtime_dir / "downloads" / f"{asset['sha256']}-{name}"
    cached.write_bytes((directory / "engine.tar.gz").read_bytes())
    signer = Signer()
    envelope = signed(signer, releases=[release_entry(manifest)])
    owner.manager.offline = False
    owner.channel = EngineChannel(
        owner.manager, roots=signer.roots(), verifier=LocalVerifier(), fetch=lambda: envelope,
        avibe_version="3.2.6",
    )
    decision = owner.check()
    assert decision.action.value == "adopt"
    generation = owner.stage_decision(decision)
    return generation, signer, manifest, envelope


@pytest.mark.parametrize("host", ["windows-amd64", "windows-arm64", "linux-amd64"])
def test_signed_candidate_archive_matches_channel_contract_on_every_host(tmp_path, monkeypatch, host):
    from core import managed_runtime

    runtime_host = {"windows-amd64": "win32-x64", "windows-arm64": "win32-arm64", "linux-amd64": "linux-x64"}[host]
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: runtime_host)
    monkeypatch.setattr("vibe.model_hub_runtime.installer.windows_native_arm64", lambda: False)
    owner, _supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    asset = next(item for item in json.loads(manifest)["assets"] if item["platform"] == host)
    assert asset["url"].endswith(".tar.gz")
    assert owner.manager._verified_manifest_binary(
        Path(candidate.install_dir), *owner._manifest(candidate),
    ) is not None


@pytest.mark.parametrize("phase", ["preflight", "start", "interrupted"])
@pytest.mark.parametrize("origin", ["bundled", "channel"])
def test_failed_same_release_repair_preserves_the_old_healthy_install(tmp_path, monkeypatch, phase, origin):
    from vibe.model_hub_runtime.lifecycle import EngineGeneration
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    if origin == "channel":
        active, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
        owner.activate(active)
    owner.ensure_running()
    old = owner.journal.load()["active"]
    result = owner.stage_required(force=True)
    assert result["ok"], result
    replacement = EngineGeneration.read(result["generation"], owner.manager.runtime_dir)
    assert replacement.asset_release_tag == old["asset_release_tag"]
    assert replacement.install_dir != old["install_dir"]
    if phase == "preflight":
        def fail_preflight(_generation):
            raise EngineUnavailableError("models.engine.health_failed")
        monkeypatch.setattr(owner, "preflight", fail_preflight)
        with pytest.raises(EngineUnavailableError):
            owner.activate(replacement, reason="required")
    elif phase == "start":
        original = supervisor.ensure_running

        def fail_replacement():
            target, _managed = owner.startable()
            if target == replacement:
                raise EngineUnavailableError("models.engine.health_failed")
            return original()

        monkeypatch.setattr(supervisor, "ensure_running", fail_replacement)
        with pytest.raises(EngineUnavailableError):
            owner.activate(replacement, reason="required")
    else:
        import uuid

        value = owner.journal.load()
        value["pending"] = {
            "id": uuid.uuid4().hex, "old": old, "candidate": replacement.document(),
            "run_intent": True, "reason": "required",
        }
        value["interrupted"][f"install:{replacement.install_dir}"] = 1
        owner._save(value)
        owner.recover()
    value = owner.journal.load()
    assert value["active"] == old and value["pending"] is None and value["blocked"] is None
    assert supervisor.running == old
    assert old["asset_release_tag"] not in value["quarantine"]
    assert f"install:{replacement.install_dir}" in value["quarantine"]
    owner.authorize(EngineGeneration.read(old, owner.manager.runtime_dir), value=value)
    with pytest.raises(journal.JournalError, match="quarantined"):
        owner.authorize(replacement, value=value)
    assert all(g["install_dir"] != replacement.install_dir for g in value["staged"])
    # Ordinary demand and required-file repair still name the settled old owner.
    assert owner.ensure_running() == "test connection"
    assert owner.stage_required()["ok"]
    owner.retry_required()
    with pytest.raises(journal.JournalError, match="quarantined"):
        owner.authorize(replacement, value=owner.journal.load())


def test_signed_channel_commit_is_separate_from_running_and_bundled_legacy(tmp_path, monkeypatch):
    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    before = owner.manager.contract_manifest()
    assert before["asset_release_tag"] == baseline["generation"]["asset_release_tag"]
    assert owner.running_release == baseline["generation"]["asset_release_tag"]
    result = owner.activate(candidate)
    assert result["committed"] == candidate.document()
    assert result["running"] == candidate.document()
    assert owner.running_release == candidate.asset_release_tag
    value = owner.journal.load()
    assert value["previous"] == baseline["generation"]
    assert value["health"] == "confirmed" and value["pending"] is None
    pointer = json.loads((owner.manager.runtime_dir / "current.json").read_bytes())
    assert pointer["install_dir"] == baseline["install_dir"]
    assert owner.manager.status()["path"] == str(Path(candidate.install_dir) / candidate.bin_path)
    repaired = owner.stage_required()
    assert repaired["generation"] == candidate.document()
    assert supervisor.running == candidate.document()


def test_failed_real_config_candidate_is_quarantined_before_healthy_rollback(tmp_path, monkeypatch):
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    supervisor.fail_tag = candidate.asset_release_tag
    with pytest.raises(EngineUnavailableError):
        owner.activate(candidate)
    value = owner.journal.load()
    assert value["active"] == baseline["generation"] and value["pending"] is None
    assert candidate.asset_release_tag in value["quarantine"]
    assert value["launch"]["generation"] == baseline["generation"]
    assert owner.running_release == baseline["generation"]["asset_release_tag"]
    assert owner.status()["state"] == "rolled_back"
    assert owner.check(refresh=False).target is None


def test_explicit_disable_clears_pending_withdrawal_run_intent(tmp_path, monkeypatch):
    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    owner.pause_withdrawn()
    assert owner.journal.load()["pending"]["run_intent"] is True

    owner.disable()

    assert owner.journal.load()["pending"]["run_intent"] is False
    assert supervisor.running is None


def test_lifecycle_provenance_does_not_reconstruct_lost_withdrawals_or_floor(tmp_path, monkeypatch):
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone

    owner, supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, signer, manifest, older = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    owner.ensure_running()
    # Signed 11 withdraws C and names a fully manifested compatible fallback.
    fallback = manifest_bytes(version="v8.0.30", build=1)
    withdrawn = signed(signer, sequence=11, releases=[release_entry(manifest), release_entry(fallback)], revoked=[
        tombstone(candidate.asset_release_tag, "model-hub-engine-v8.0.30-1"),
    ])
    owner.channel._fetch = lambda: withdrawn
    assert owner.channel.refresh().state == "accepted"
    with pytest.raises(journal.JournalError, match="withdrawn"):
        owner.authorize(candidate, value=owner.journal.load())
    owner.pause_withdrawn()
    assert supervisor.running is None
    assert owner.journal.load()["pending"]["run_intent"] is True
    owner.channel.store.state_path.unlink()
    owner.channel._fetch = lambda: older
    assert owner.channel.refresh().state == "accepted"
    assert owner.channel.store.load().sequence == 1
    # Retained exact provenance has no authority to restore 11 or its tombstone.
    owner.authorize(candidate, value=owner.journal.load())
    owner.recover()
    assert owner.journal.load()["active"] == candidate.document()
    assert owner.channel.store.load().sequence == 1
    assert not owner.channel.store.load().revoked
    assert supervisor.running == candidate.document()
    assert not owner.journal.load()["quarantine"]


def test_config_restart_cannot_adopt_available_candidate_and_preserves_previous(tmp_path, monkeypatch):
    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.restart_running()
    assert supervisor.running == baseline["generation"]
    assert owner.journal.load()["active"] == baseline["generation"]
    assert owner.journal.load()["previous"] is None
    owner.activate(candidate)
    previous = owner.journal.load()["previous"]
    owner.restart_running()
    assert supervisor.running == candidate.document()
    assert owner.journal.load()["previous"] == previous
    supervisor.stop_for_activation = lambda: setattr(supervisor, "running", None)
    supervisor.stop_for_activation()
    owner.restart_running()
    assert supervisor.running is None


def test_corrupt_committed_channel_recovery_and_required_repair_never_repin_to_bundle(tmp_path, monkeypatch):
    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    binary = Path(candidate.install_dir) / candidate.bin_path
    binary.write_bytes(b"corrupt")
    owner.recover()
    assert owner.journal.load()["active"] == candidate.document()
    assert owner.journal.load()["blocked"] == "engine_no_authorized_recovery_target"
    assert supervisor.running is None
    repair = owner.stage_required()
    assert repair["ok"], repair
    assert repair["generation"]["asset_release_tag"] == candidate.asset_release_tag
    assert repair["generation"]["origin"] == "channel"
    assert owner.journal.load()["active"] == candidate.document()
    assert Path(baseline["path"]).is_file()


@pytest.mark.parametrize("run_intent", [False, True])
def test_interrupted_activation_recovery_restores_explicit_run_intent_only(tmp_path, monkeypatch, run_intent):
    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    value = owner.journal.load()
    value["pending"] = {
        "id": "a" * 32, "old": baseline["generation"], "candidate": candidate.document(),
        "run_intent": run_intent, "reason": "update",
    }
    owner.journal.save(value)
    supervisor.running = candidate.document()  # process boundary fixture, not real crash certification
    owner.recover()
    value = owner.journal.load()
    assert value["active"] == baseline["generation"] and value["pending"] is None
    assert value["interrupted"][candidate.asset_release_tag] == 1
    assert supervisor.running == (baseline["generation"] if run_intent else None)
    assert value["health"] == ("confirmed" if run_intent else "preflight_only")


@pytest.mark.parametrize("boundary", ["intent", "launch", "commit"])
@pytest.mark.parametrize("visible", [False, True])
def test_every_transaction_write_failure_fences_later_actions_and_applied_status(tmp_path, monkeypatch, boundary, visible):
    from vibe.model_hub_runtime import lifecycle

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    actual = lifecycle.write_document

    def write(path, value):
        pending_c = value["pending"] and value["pending"]["candidate"] == candidate.document()
        launch_c = value["launch"] and value["launch"]["generation"] == candidate.document()
        fail = (
            (boundary == "intent" and pending_c and not launch_c)
            or (boundary == "launch" and pending_c and launch_c)
            or (boundary == "commit" and value["active"] == candidate.document() and value["pending"] is None)
        )
        if fail:
            if visible:
                actual(path, value)
            raise journal.JournalError("injected completion failure")
        actual(path, value)

    monkeypatch.setattr(lifecycle, "write_document", write)
    with pytest.raises(journal.JournalError):
        owner.activate(candidate)
    assert owner.recovered is False and owner._ready is False
    assert owner.status()["state"] != "applied" and owner.status().get("last_result") is None
    assert supervisor.running == (baseline["generation"] if boundary == "intent" else None)
    assert candidate.asset_release_tag not in owner.journal.load()["quarantine"]
    monkeypatch.setattr(lifecycle, "write_document", actual)
    owner.recover()
    committed_c = boundary == "commit" and visible
    assert owner.journal.load()["active"] == (candidate.document() if committed_c else baseline["generation"])
    # A durable new commit is retained, but a stopped generation still waits for
    # fenced first-start confirmation; an interrupted intent restores old run intent.
    if committed_c:
        assert supervisor.running is None
    elif boundary != "intent" or visible:
        assert supervisor.running == baseline["generation"]


def test_preflight_only_commit_confirmation_failure_rolls_back_before_admission(tmp_path, monkeypatch):
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    assert supervisor.running is None and owner.journal.load()["health"] == "preflight_only"
    supervisor.fail_tag = candidate.asset_release_tag
    with pytest.raises(EngineUnavailableError):
        owner.ensure_running()
    assert owner.journal.load()["active"] == baseline["generation"]
    assert supervisor.running == baseline["generation"]
    assert owner.running_release == baseline["generation"]["asset_release_tag"]
    assert owner.status()["state"] == "rolled_back"


def test_failed_candidate_cannot_restart_a_now_withdrawn_old_recovery_target(tmp_path, monkeypatch):
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    old_tag = baseline["generation"]["asset_release_tag"]
    withdrawn = signed(signer, sequence=2, releases=[
        release_entry(manifest), release_entry(manifest_bytes(version="v8.0.30", build=2)),
    ], revoked=[tombstone(old_tag, candidate.asset_release_tag)])
    owner.channel._fetch = lambda: withdrawn
    assert owner.channel.refresh().state == "accepted"
    supervisor.fail_tag = candidate.asset_release_tag
    start_count = sum(kind == "start" for kind, _tag in supervisor.events)
    with pytest.raises(EngineUnavailableError):
        owner.activate(candidate, reason="withdrawal")
    starts = [tag for kind, tag in supervisor.events if kind == "start"][start_count:]
    assert starts == [candidate.asset_release_tag]
    assert supervisor.running is None and owner.running_release is None
    assert owner.journal.load()["blocked"] == "engine_rollback_failed"
    assert owner.status()["state"] == "blocked"


def test_failed_legacy_projection_is_independent_of_a_committed_channel_activation(tmp_path, monkeypatch):
    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    write = owner.manager._write_current_pointer

    def fail(*_args, **_kwargs):
        raise PermissionError("injected compatibility projection failure")

    monkeypatch.setattr(owner.manager, "_write_current_pointer", fail)
    result = owner.activate(candidate)
    assert result["state"] == "applied" and result["committed"] == candidate.document()
    assert result["legacy"] == baseline["generation"]
    assert result["legacy_hint"]["state"] == "eligible"
    assert result["legacy_error"] == "legacy_hint_write_failed" and result["legacy_projection"] == "pending"
    assert supervisor.running is None  # never start B merely to supply a hint
    monkeypatch.setattr(owner.manager, "_write_current_pointer", write)
    owner.project_legacy(owner.journal.load())
    assert owner.status()["legacy_projection"] == "settled"
    assert owner.journal.load()["active"] == candidate.document()


def test_package_stale_hint_removal_failure_retention_and_actual_new_bundle(tmp_path, monkeypatch):
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone

    owner, _supervisor, old_bundle = owner_fixture(tmp_path, monkeypatch)
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    hint = owner.manager.runtime_dir / "current.json"
    old_hint = hint.read_bytes()
    newer_bundle = fixture_manifest(tmp_path / "new-bundle", version="v8.0.31")
    monkeypatch.setattr(owner.manager, "bundled_manifest_bytes", lambda: newer_bundle)
    unlink = Path.unlink

    def fail_hint_removal(path, *args, **kwargs):
        if path == hint:
            raise PermissionError("injected stale-hint removal failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_hint_removal)
    owner.project_legacy(owner.journal.load())
    status = owner.status()
    assert status["legacy"] is None and status["legacy_hint"]["state"] == "stale"
    assert status["legacy_hint"]["manifest_sha256"] == old_bundle["generation"]["manifest_sha256"]
    assert status["legacy_projection"] == "pending" and status["legacy_error"] == "legacy_hint_write_failed"
    assert hint.read_bytes() == old_hint
    assert owner.manager.required_generation() == candidate
    assert owner.manager.contract_manifest()["asset_release_tag"] == candidate.asset_release_tag
    monkeypatch.setattr(Path, "unlink", unlink)
    owner.project_legacy(owner.journal.load())
    assert owner.status()["legacy_hint"]["state"] == "absent"
    assert not hint.exists()
    # Projection did not install B_new. Materialize it explicitly now and only
    # then qualify it as the actual package's existing compatibility hint.
    staged_bundle = owner.manager.stage_manifest(newer_bundle)
    assert staged_bundle["ok"], staged_bundle
    owner.project_legacy(owner.journal.load())
    assert owner.status()["legacy"] == staged_bundle["generation"]
    assert owner.status()["legacy_hint"]["asset_release_tag"] == "model-hub-engine-v8.0.31-2"
    assert owner.journal.load()["active"] == candidate.document()
    recalled = signed(signer, sequence=11, releases=[
        release_entry(manifest), release_entry(manifest_bytes(version="v8.0.31", build=2)),
    ], revoked=[tombstone("model-hub-engine-v8.0.31-2", candidate.asset_release_tag)])
    owner.channel._fetch = lambda: recalled
    assert owner.channel.refresh().state == "accepted"
    owner.project_legacy(owner.journal.load())
    assert owner.status()["legacy_hint"]["state"] == "absent"
    assert owner.journal.load()["active"] == candidate.document()
    assert owner.manager.clean(keep_previous=0)["ok"]
    assert Path(staged_bundle["path"]).is_file() and (Path(candidate.install_dir) / candidate.bin_path).is_file()


def test_explicit_required_retry_cannot_clear_current_withdrawal_authority(tmp_path, monkeypatch):
    from core.model_hub_engine_channel import ResolutionAction
    from types import SimpleNamespace

    owner, _supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    tag = baseline["generation"]["asset_release_tag"]
    value = owner.journal.load()
    value["quarantine"][tag] = "startup_failed"
    owner._save(value)
    monkeypatch.setattr(owner.channel, "resolve", lambda **_kwargs: SimpleNamespace(
        action=ResolutionAction.BLOCKED, target=None, reason="withdrawn",
    ))
    with pytest.raises(journal.JournalError, match="withdrawn"):
        owner.retry_required()
    assert owner.journal.load()["quarantine"] == {tag: "startup_failed"}
    owner.check(refresh=False, retry=True)
    assert owner.journal.load()["quarantine"] == {tag: "startup_failed"}


@pytest.mark.parametrize("hint", ["canonical", "sibling"])
def test_retired_bundled_repair_siblings_are_reclaimed_but_one_eligible_hint_survives(tmp_path, monkeypatch, hint):
    from vibe.model_hub_runtime.lifecycle import EngineGeneration

    owner, _supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    generations = [baseline["generation"]]
    # Build a historical inventory for the explicit dry-run/cleanup contract;
    # automatic settlement is covered by the repeated-repairs test above.
    with monkeypatch.context() as setup:
        setup.setattr(owner.manager, "_clean_after_successful_install", lambda: None)
        for _ in range(4):
            result = owner.stage_required(force=True)
            assert result["ok"], result
            generation = EngineGeneration.read(result["generation"], owner.manager.runtime_dir)
            owner.activate(generation, reason="required")
            generations.append(generation.document())
    chosen = generations[0 if hint == "canonical" else 1]
    manifest, archive = owner._manifest(EngineGeneration.read(chosen, owner.manager.runtime_dir))
    owner.manager._write_current_pointer(Path(chosen["install_dir"]), manifest, archive)
    owner.project_legacy(owner.journal.load())
    assert owner.status()["legacy"]["install_dir"] == chosen["install_dir"]
    # Compatibility exposure must not make the non-authoritative hint an
    # execution/recovery selector, nor rewrite the committed generation.
    assert owner._existing_bundled().install_dir == generations[0]["install_dir"]
    assert owner._existing_bundled(compatibility=True).install_dir == chosen["install_dir"]
    assert owner.journal.load()["active"] == generations[-1]
    protected = {g["install_dir"] for g in [generations[-1], generations[-2], chosen]}
    dry = owner.manager.clean(keep_previous=0, dry_run=True)
    assert dry["ok"], dry
    assert all(Path(g["install_dir"]).exists() for g in generations)
    cleaned = owner.manager.clean(keep_previous=0)
    assert cleaned["ok"], cleaned
    assert {g["install_dir"] for g in generations if Path(g["install_dir"]).exists()} == protected
    assert owner._existing_bundled(compatibility=True).install_dir == chosen["install_dir"]
    assert owner.status()["legacy_hint"]["state"] == "eligible"


@pytest.mark.parametrize("role", ["staged", "pending_old", "pending_candidate", "launch", "preflight"])
def test_each_lifecycle_role_pins_a_bundled_sibling_only_until_retirement(tmp_path, monkeypatch, role):
    import uuid

    from core.process_isolation import fingerprint_process_marker, new_process_identity_marker
    from vibe.model_hub_runtime.lifecycle import EngineGeneration

    owner, _supervisor, _baseline = owner_fixture(tmp_path, monkeypatch)
    generations = []
    # Install the role-test inventory before enabling automatic retirement.
    with monkeypatch.context() as setup:
        setup.setattr(owner.manager, "_clean_after_successful_install", lambda: None)
        for _ in range(4):
            result = owner.stage_required(force=True)
            generation = EngineGeneration.read(result["generation"], owner.manager.runtime_dir)
            owner.activate(generation, reason="required")
            generations.append(generation.document())
    pinned = generations[1]
    value = owner.journal.load()
    fingerprint = fingerprint_process_marker(new_process_identity_marker())
    if role == "staged":
        value["staged"] = [pinned]
    elif role.startswith("pending_"):
        value["pending"] = {
            "id": uuid.uuid4().hex, "old": pinned if role == "pending_old" else value["active"],
            "candidate": pinned if role == "pending_candidate" else value["active"],
            "run_intent": False, "reason": "required",
        }
    elif role == "launch":
        value["launch"] = {"fingerprint": fingerprint, "generation": pinned}
    else:
        value["preflight"] = {
            "root": str(owner.manager.runtime_dir / "engine-preflight-owned"),
            "fingerprint": fingerprint, "generation": pinned,
        }
    owner._save(value)
    assert owner.stage_required()["ok"]
    assert Path(pinned["install_dir"]).exists()
    value.update(staged=[], pending=None, launch=None, preflight=None)
    owner._save(value)
    assert owner.stage_required()["ok"]
    assert not Path(pinned["install_dir"]).exists()
    assert Path(value["active"]["install_dir"]).exists()
    assert Path(value["previous"]["install_dir"]).exists()


def test_settled_and_superseded_stage_roles_retire_and_content_addressed_archives_are_reclaimed(tmp_path, monkeypatch):
    import os

    owner, _supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    assert owner.journal.load()["staged"] == []
    candidate, _signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    owner.activate(candidate)
    assert owner.journal.load()["staged"] == []
    assert owner.stage_required()["generation"] == candidate.document()
    assert owner.journal.load()["staged"] == []  # ordinary repair does not leak a role
    obsolete = owner.manager.stage_manifest(fixture_manifest(tmp_path / "obsolete", version="v8.0.40"))
    assert obsolete["ok"], obsolete
    digest = obsolete["generation"]["archive_sha256"]
    obsolete_cache = next((owner.manager.runtime_dir / "downloads").glob(f"{digest}-*"))
    # Byte-only stage is protected until the owner has superseded it. This is
    # not a user authorization to ever execute this unqualified fixture.
    assert owner.manager.clean(keep_previous=0)["ok"]
    assert Path(obsolete["path"]).exists()
    owner.check(refresh=False)
    assert owner.journal.load()["staged"] == []
    # First pass removes the unused install, but a young archive retains its
    # provenance so the later pass can reclaim its content-addressed filename.
    first = owner.manager.clean(keep_previous=0)
    assert first["ok"], first
    assert not Path(obsolete["path"]).exists() and obsolete_cache.exists()
    os.utime(obsolete_cache, (1, 1))
    second = owner.manager.clean(keep_previous=0)
    assert second["ok"], second
    assert not obsolete_cache.exists()
    assert Path(baseline["path"]).exists() and (Path(candidate.install_dir) / candidate.bin_path).exists()
    assert list((owner.manager.runtime_dir / "downloads").glob(f"{candidate.archive_sha256}-*"))


def test_withdrawal_learned_during_old_stop_is_rechecked_before_candidate_start(tmp_path, monkeypatch):
    from tests.engine_channel_support import manifest_bytes, release_entry, signed, tombstone

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    candidate, signer, manifest, _envelope = signed_candidate(owner, tmp_path)
    withdrawn = signed(signer, sequence=11, releases=[
        release_entry(manifest), release_entry(manifest_bytes(version="v8.0.30", build=1)),
    ], revoked=[tombstone(candidate.asset_release_tag, "model-hub-engine-v8.0.30-1")])
    stop = supervisor.stop_for_activation

    def stop_then_learn_withdrawal():
        stop()
        owner.channel._fetch = lambda: withdrawn
        assert owner.channel.refresh().state == "accepted"

    monkeypatch.setattr(supervisor, "stop_for_activation", stop_then_learn_withdrawal)
    with pytest.raises(journal.JournalError, match="engine_withdrawn"):
        owner.activate(candidate)
    assert ("start", candidate.asset_release_tag) not in supervisor.events
    assert candidate.asset_release_tag not in owner.journal.load()["quarantine"]
    assert not owner.recovered and supervisor.running is None
    owner.recover()
    assert owner.journal.load()["active"] == baseline["generation"]


def test_released_upstream_named_cache_stages_offline_without_rewriting_it(tmp_path, monkeypatch):
    import shutil

    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    raw = fixture_manifest(tmp_path / "bundle")
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    monkeypatch.setattr(manager, "bundled_manifest_bytes", lambda: raw)
    asset = json.loads(raw)["assets"][0]
    name = "engine.zip" if asset["platform"].startswith("windows") else "engine.tar.gz"
    legacy = manager.runtime_dir / "downloads" / name
    legacy.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "bundle" / name, legacy)
    before = legacy.read_bytes(), legacy.stat().st_mtime_ns
    monkeypatch.setattr("vibe.model_hub_runtime.installer.subprocess.run", lambda *_a, **_kw: pytest.fail("staging must not execute"))
    result = manager.ensure()
    assert result["ok"] and result["reason"] == "waiting_for_controller"
    assert (legacy.read_bytes(), legacy.stat().st_mtime_ns) == before
    assert not legacy.with_name(f"{asset['sha256']}-{name}").exists()
    assert not (manager.runtime_dir / "current.json").exists()


def test_required_reuse_does_not_reopen_a_durable_running_binary_for_writes(tmp_path, monkeypatch):
    from vibe.model_hub_runtime import journal

    owner, supervisor, baseline = owner_fixture(tmp_path, monkeypatch)
    owner.ensure_running()
    generation = baseline["generation"]
    binary = Path(generation["install_dir"]) / generation["bin_path"]
    flush = journal.flush_file

    def reject_running_write(path):
        if Path(path) == binary:
            raise PermissionError("Windows executable image is already mapped")
        flush(path)

    monkeypatch.setattr(journal, "flush_file", reject_running_write)
    result = owner.stage_required()
    assert result["ok"] and result["generation"] == generation
    assert supervisor.running == generation
    value = owner.journal.load()
    assert value["active"] == generation and value["staged"] == []
