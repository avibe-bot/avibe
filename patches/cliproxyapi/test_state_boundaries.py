"""Actual pre-write consumers with fake homes and task-only sentinels."""

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import fixture
import isolation
import verify
import wire_matrix


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    for name in (".avibe", ".vibe_remote", ".codex", ".claude"):
        (home / name).mkdir()
        (home / name / "sentinel").write_bytes(b"unchanged test-only user state")
    return home


@pytest.mark.parametrize("name", [".avibe", ".vibe_remote", ".codex", ".claude"])
@pytest.mark.parametrize("consumer", ["environment", "sandbox", "export", "verify", "wire", "engine-seam"])
@pytest.mark.parametrize("alias", [False, True])
def test_actual_callers_refuse_before_any_protected_setup_write(tmp_path, fake_home, monkeypatch, name, consumer, alias):
    root = fake_home / name / "must-not-create"
    if alias:
        root = tmp_path / "task-alias"
        root.symlink_to(fake_home / name / "must-not-create")
    before = fixture.source_digest(fake_home)
    if consumer == "environment":
        action = lambda: isolation.isolated_environment(root)
    elif consumer == "sandbox":
        action = lambda: isolation.sandbox_prefix(root)
    elif consumer == "export":
        action = lambda: fixture.export_fixture(tmp_path / "not-a-repository", root, {})
    elif consumer == "engine-seam":
        action = lambda: wire_matrix.FrozenEngine(None, root, None, None)
    elif consumer == "verify":
        monkeypatch.setattr(sys, "argv", ["verify.py", "build", "--state", str(root),
                                         "--source", str(tmp_path / "not-a-source")])
        action = verify.main
    else:
        monkeypatch.setattr(sys, "argv", ["wire_matrix.py", "--state", str(root), "--binary", "/absent",
                                         "--fixture", "/absent", "--fixture-sha256", "0" * 64])
        action = wire_matrix.main
    with pytest.raises(ValueError, match="protected user state"):
        action()
    assert fixture.source_digest(fake_home) == before
    assert not (fake_home / name / "must-not-create").exists()


def test_protected_apply_source_refuses_before_git_or_patch(tmp_path, fake_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "verify.py", "apply", "--state", str(tmp_path / "task-state"),
        "--source", str(fake_home / ".avibe" / "source"),
    ])
    with pytest.raises(ValueError, match="protected user state"):
        verify.main()
    assert not (tmp_path / "task-state").exists()


def test_environment_aliases_are_all_validated_before_first_mkdir(tmp_path, fake_home):
    root = tmp_path / "task-state"
    root.mkdir()
    (root / "tmp").symlink_to(fake_home / ".codex")
    with pytest.raises(ValueError, match="protected user state"):
        isolation.isolated_environment(root)
    assert sorted(path.name for path in root.iterdir()) == ["tmp"]
    assert (fake_home / ".codex/sentinel").read_bytes() == b"unchanged test-only user state"


@pytest.mark.parametrize("which", ["home", "ancestor", "root", "tmp", "users"])
def test_broad_write_roots_refuse(tmp_path, fake_home, which):
    root = {"home": fake_home, "ancestor": tmp_path, "root": Path("/"),
            "tmp": Path("/tmp"), "users": Path("/Users")}[which]
    with pytest.raises(ValueError, match="dedicated, narrow"):
        isolation.isolated_environment(root)


def test_ordinary_dedicated_temporary_state_still_works(tmp_path, fake_home):
    root = tmp_path / "allowed-task"
    cache = tmp_path / "allowed-cache"
    environment = isolation.isolated_environment(root, cache=cache)
    assert Path(environment["HOME"]) == root / "home"
    assert Path(environment["GOCACHE"]) == cache / "cache"
    assert environment["GOTOOLCHAIN"] == "local"
    assert set(path.name for path in root.iterdir()) == {"home", "tmp", "config", "data"}


@pytest.mark.parametrize("root", ["/home/user/task", "/root/task", "/Users/user/task", "/tmp", "/var/tmp", "/"])
def test_privileged_domain_rejects_non_temporary_or_broad_roots(monkeypatch, root):
    # Pure virtual Linux path classification; never create/read those surfaces.
    monkeypatch.setattr(Path, "resolve", lambda path, **_kw: path)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/user"))
    with pytest.raises(ValueError):
        isolation.validate_temporary_root(Path(root))


@pytest.mark.parametrize("root", ["/tmp/allocated-task", "/var/tmp/allocated/task"])
def test_privileged_domain_accepts_narrow_linux_temporary_roots(monkeypatch, root):
    monkeypatch.setattr(Path, "resolve", lambda path, **_kw: path)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/user"))
    assert isolation.validate_temporary_root(Path(root)) == Path(root)


@pytest.mark.parametrize("target", ["/home/absent-task", "/root/absent-task", "/Users/absent-task"])
def test_symlinked_privileged_root_cannot_admit_a_hidden_surface(tmp_path, target):
    link = tmp_path / "looks-temporary"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        isolation.validate_temporary_root(link)
    assert link.is_symlink()


def test_privileged_validation_also_protects_invoking_users_aliased_state(tmp_path, monkeypatch):
    invoking_home, root_home = tmp_path / "invoking-home", tmp_path / "root-home"
    invoking_home.mkdir()
    root_home.mkdir()
    actual_data = tmp_path / "looks-like-temporary-data"
    actual_data.mkdir()
    (actual_data / "sentinel").write_bytes(b"invoking user's fake protected data")
    (invoking_home / ".avibe").symlink_to(actual_data)
    monkeypatch.setattr(isolation.os, "getuid", lambda: 0)
    monkeypatch.setattr(isolation.pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_dir=str(invoking_home if uid == 501 else root_home),
    ))
    context = isolation.StorageContext.capture(uid=501, environment={}).with_root_identity()
    with pytest.raises(ValueError, match="protected user state"):
        isolation.validate_state_root(actual_data / "evidence", owner_uid=501, context=context)
    assert list(actual_data.iterdir()) == [actual_data / "sentinel"]


@pytest.mark.parametrize("name", [".avibe", ".vibe_remote", ".codex", ".claude"])
def test_write_root_cannot_contain_protected_symlink_target(tmp_path, fake_home, name):
    root = tmp_path / "apparently-task-owned"
    target = root / "protected-target"
    target.mkdir(parents=True)
    (target / "sentinel").write_bytes(b"fake protected state must remain unchanged")
    protected = fake_home / name
    # Preserve the original fake home fixture while substituting only its alias.
    protected.rename(fake_home / (name + "-preserved"))
    protected.symlink_to(target)
    before = fixture.source_digest(root)
    with pytest.raises(ValueError, match="protected user state"):
        isolation.isolated_environment(root)
    assert fixture.source_digest(root) == before


@pytest.fixture
def original_storage(tmp_path, fake_home, monkeypatch):
    for name in (*isolation.STORAGE_ENV, "SUDO_UID", "SUDO_GID"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(home=fake_home, parent=tmp_path / "configured-storage")


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("shape", ["equal", "descendant", "alias", "containing"])
@pytest.mark.parametrize("consumer", ["environment", "sandbox", "export", "verify", "wire", "engine-seam"])
def test_configured_storage_is_rejected_by_every_public_prewrite_consumer(
        tmp_path, original_storage, monkeypatch, variable, shape, consumer):
    boundary = original_storage.parent / variable
    boundary.mkdir(parents=True)
    (boundary / "sentinel").write_bytes(b"fake production storage")
    monkeypatch.setenv(variable, str(boundary))
    descendant = boundary / (".codex/evidence" if variable == "HOME" else "evidence")
    root = boundary if shape == "equal" else descendant
    if shape == "containing":
        root = original_storage.parent
    elif shape == "alias":
        root = tmp_path / "looks-like-task"
        root.symlink_to(descendant)
    before = fixture.source_digest(original_storage.parent)
    monkeypatch.setattr(fixture.subprocess, "check_output", lambda *_a, **_kw: pytest.fail("Git reached."))
    if consumer == "environment":
        action = lambda: isolation.isolated_environment(root)
    elif consumer == "sandbox":
        action = lambda: isolation.sandbox_prefix(root)
    elif consumer == "export":
        action = lambda: fixture.export_fixture(tmp_path / "absent-repository", root, {})
    elif consumer == "engine-seam":
        action = lambda: wire_matrix.FrozenEngine(None, root, None, None)
    elif consumer == "verify":
        monkeypatch.setattr(sys, "argv", ["verify.py", "build", "--state", str(root), "--source", "/absent"])
        action = verify.main
    else:
        monkeypatch.setattr(sys, "argv", [
            "wire_matrix.py", "--state", str(root), "--binary", "/absent",
            "--fixture", "/absent", "--fixture-sha256", "0" * 64,
        ])
        action = wire_matrix.main
    with pytest.raises(ValueError):
        action()
    assert fixture.source_digest(original_storage.parent) == before


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("shape", ["equal", "descendant", "alias", "containing"])
def test_configured_shared_cache_refuses_before_even_output_creation(
        tmp_path, original_storage, monkeypatch, variable, shape):
    boundary = original_storage.parent / variable
    boundary.mkdir(parents=True)
    monkeypatch.setenv(variable, str(boundary))
    cache = boundary if shape == "equal" else boundary / (".avibe/cache" if variable == "HOME" else "cache")
    if shape == "containing":
        cache = boundary.parent
    elif shape == "alias":
        alias = tmp_path / "cache-alias"
        alias.symlink_to(cache)
        cache = alias
    output = tmp_path / "exclusive-output"
    with pytest.raises(ValueError):
        isolation.isolated_environment(output, cache=cache)
    assert not output.exists()
    assert list(boundary.iterdir()) == []


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
def test_configured_alias_target_cannot_be_enclosed_by_a_write_root(tmp_path, original_storage, monkeypatch, variable):
    output = tmp_path / "apparently-owned"
    target = output / "protected-target"
    target.mkdir(parents=True)
    alias = tmp_path / "configured-alias"
    alias.symlink_to(target)
    monkeypatch.setenv(variable, str(alias))
    with pytest.raises(ValueError):
        isolation.isolated_environment(output)
    assert list(output.iterdir()) == [target]


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("invalid", ["relative", "~/not-expanded", "/tmp/../other"])
def test_invalid_configuration_refuses_before_any_setup(tmp_path, original_storage, monkeypatch, variable, invalid):
    monkeypatch.setenv(variable, invalid)
    root = tmp_path / "never-created"
    with pytest.raises(ValueError) as refused:
        isolation.isolated_environment(root)
    assert invalid not in str(refused.value)
    assert not root.exists()


@pytest.mark.parametrize("override_home", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_unset_empty_defaults_and_both_home_identities(tmp_path, original_storage, monkeypatch, override_home, empty):
    home = tmp_path / "effective-home" if override_home else original_storage.home
    if override_home:
        monkeypatch.setenv("HOME", str(home))
    if empty:
        for name in isolation.STORAGE_ENV[1:]:
            monkeypatch.setenv(name, "")
    context = isolation.storage_context()
    for protected_home in (original_storage.home, home):
        with pytest.raises(ValueError):
            context.validate(protected_home)
        for name in isolation.PRODUCT_DIRS:
            with pytest.raises(ValueError):
                context.validate(protected_home / name / "task")
    for default in isolation.XDG_DEFAULTS.values():
        with pytest.raises(ValueError):
            context.validate(home / default / "task")
    assert not any(str(path).startswith("/run/user/") for path in context.protected)
    # An ordinary dedicated directory outside protected locations still works.
    assert isolation.isolated_environment(tmp_path / "task")["GOENV"] == "off"


def test_invalid_path_values_do_not_leak_and_empty_home_uses_passwd(original_storage):
    for invalid in ("\0private", "/" + "x" * 4097):
        with pytest.raises(ValueError) as refused:
            isolation.StorageContext.capture(environment={"HOME": invalid})
        assert invalid not in str(refused.value)
    empty = isolation.StorageContext.capture(environment={"HOME": ""})
    unset = isolation.StorageContext.capture(environment={})
    assert empty.homes == unset.homes and empty.protected == unset.protected
    assert empty.fingerprint() != unset.fingerprint()  # Detect environment loss in transit.


def test_macos_read_policy_uses_all_original_configured_and_canonical_locations(tmp_path, original_storage, monkeypatch):
    for name in isolation.STORAGE_ENV:
        target = original_storage.parent / name
        target.mkdir(parents=True)
        alias = tmp_path / ("alias-" + name)
        alias.symlink_to(target)
        monkeypatch.setenv(name, str(alias))
    monkeypatch.setattr(sys, "platform", "darwin")
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: True if str(path) == "/usr/bin/sandbox-exec" else original_is_file(path))
    context = isolation.storage_context()
    prefix = isolation.sandbox_prefix(tmp_path / "task")
    assert prefix[:2] == ["/usr/bin/sandbox-exec", "-p"]
    for boundary in context.protected:
        assert f"(deny file-read* (subpath {json.dumps(str(boundary))}))" in prefix[2]


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
def test_actual_process_seam_refuses_protected_config_before_write_or_launch(
        tmp_path, original_storage, monkeypatch, variable):
    boundary = original_storage.parent / variable
    boundary.mkdir(parents=True)
    monkeypatch.setenv(variable, str(boundary))
    protected = boundary / (".avibe" if variable == "HOME" else "engine")
    engine = object.__new__(wire_matrix.FrozenEngine)
    engine.binary = tmp_path / "not-executed"
    engine.state = tmp_path / "safe-state"
    engine.store = SimpleNamespace(root=protected)
    monkeypatch.setitem(sys.modules, "config.atomic_io", SimpleNamespace(
        write_atomic=lambda *_a, **_kw: pytest.fail("Atomic config write reached."),
    ))
    monkeypatch.setattr(wire_matrix.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Process reached."))
    with pytest.raises(ValueError):
        engine.launch([str(engine.binary), "-config", str(protected / "config.yaml")], env={})
    assert not engine.state.exists() and list(boundary.iterdir()) == []


@pytest.fixture
def private_storage(tmp_path, original_storage, monkeypatch):
    original = original_storage.parent / "original-xdg"
    original.mkdir(parents=True)
    context = isolation.StorageContext.capture(environment={"XDG_CONFIG_HOME": str(original)})
    root = tmp_path / "task"
    output, cache = root / "runs/one.json", root / "shared-cache"
    output.mkdir(parents=True, mode=0o750)
    output.chmod(0o750)
    cache.mkdir()
    uid, gid = os.getuid(), os.getgid()
    inner = {"mnt": "mnt:[11]", "net": "net:[12]", "pid": "pid:[13]"}
    proof = {
        "uid": uid, "gid": gid, "root": str(root), "output": str(output), "state": str(cache),
        "receipt": "one.json", "namespaces": inner,
        "outer_namespaces": {"mnt": "mnt:[1]", "net": "net:[2]", "pid": "pid:[3]"},
        "storage_context": context.record(), "storage_sha256": context.fingerprint(),
    }
    marker = tmp_path / "parent-proof.json"
    marker.write_text(json.dumps(proof))
    marker.chmod(0o644)
    marker_inode = marker.stat().st_ino
    real_fstat, real_readlink, real_read_text = os.fstat, os.readlink, Path.read_text
    status = {**{name: "0" * 16 for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")},
              "NoNewPrivs": "1"}
    state = SimpleNamespace(root=root, output=output, cache=cache, original=original,
                            proof=proof, marker=marker, status=status, root_owned=True)

    def parent_owned(fd):
        info = real_fstat(fd)
        if info.st_ino == marker_inode and state.root_owned:
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    def read_status(path, *args, **kwargs):
        if str(path) == "/proc/self/status":
            return "\n".join(f"{name}: {value}" for name, value in status.items())
        return real_read_text(path, *args, **kwargs)

    def namespace_link(path, *args, **kwargs):
        if str(path).startswith("/proc/self/ns/"):
            return inner[Path(path).name]
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(isolation, "PROOF_PATH", marker)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "fstat", parent_owned)
    monkeypatch.setattr(os, "readlink", namespace_link)
    monkeypatch.setattr(Path, "read_text", read_status)
    # These are task-generated values, not the original invoking user's roots.
    monkeypatch.setenv("HOME", str(output / "home"))
    for name in isolation.STORAGE_ENV[1:]:
        monkeypatch.setenv(name, str(cache / name))
    return state


def test_valid_private_proof_keeps_generated_home_xdg_cache_usable(private_storage):
    state = private_storage
    environment = isolation.isolated_environment(state.output, cache=state.cache)
    assert environment["HOME"] == str(state.output / "home")
    assert environment["GOCACHE"] == str(state.cache / "cache")
    assert isolation.validate_state_root(state.output / "wire") == state.output / "wire"
    with pytest.raises(ValueError):
        isolation.validate_state_root(state.original / "must-not-create")
    public = json.dumps(isolation.namespace_receipt())
    assert str(state.original) not in public and "storage_context" not in public
    assert state.proof["storage_sha256"] in public


@pytest.mark.parametrize("failure", [
    "missing", "caller-owned", "symlink", "writable-proof", "linked-proof",
    "same-namespace", "missing-namespace", "wrong-uid", "capability", "privilege",
    "wrong-output", "output-mode", "cache-overlap", "context-hash", "empty-context",
])
def test_forged_or_incomplete_task_context_never_waives_production_admission(
        private_storage, failure):
    state = private_storage
    if failure == "missing":
        state.marker.unlink()
    elif failure == "caller-owned":
        state.root_owned = False
    elif failure == "symlink":
        real = state.marker.with_suffix(".original")
        state.marker.rename(real)
        state.marker.symlink_to(real)
    elif failure == "writable-proof":
        state.marker.chmod(0o666)
    elif failure == "linked-proof":
        os.link(state.marker, state.marker.with_suffix(".other"))
    elif failure == "same-namespace":
        state.proof["outer_namespaces"]["net"] = state.proof["namespaces"]["net"]
    elif failure == "missing-namespace":
        state.proof["outer_namespaces"].pop("pid")
    elif failure == "wrong-uid":
        state.proof["uid"] += 1
    elif failure == "capability":
        state.status["CapBnd"] = "1"
    elif failure == "privilege":
        state.status["NoNewPrivs"] = "0"
    elif failure == "wrong-output":
        state.proof["output"] = str(state.cache)
    elif failure == "output-mode":
        state.output.chmod(0o770)
    elif failure == "cache-overlap":
        state.proof["state"] = str(state.output)
    elif failure == "context-hash":
        state.proof["storage_sha256"] = "0" * 64
    else:
        state.proof["storage_context"] = {}
    if failure not in ("missing", "symlink"):
        state.marker.write_text(json.dumps(state.proof))
    before = fixture.source_digest(state.root)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        isolation.isolated_environment(state.output, cache=state.cache)
    assert fixture.source_digest(state.root) == before


def test_ambient_safe_root_claim_does_not_waive_real_storage(tmp_path, original_storage, monkeypatch):
    protected = original_storage.parent
    monkeypatch.setenv("XDG_CONFIG_HOME", str(protected))
    monkeypatch.setenv("AVIBE_STORAGE_CONTEXT", '{"protected": [], "task": "/tmp"}')
    with pytest.raises(ValueError):
        isolation.isolated_environment(protected / "output")
    assert not protected.exists()
