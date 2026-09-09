"""Actual pre-write consumers with fake homes and task-only sentinels."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
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
    monkeypatch.setattr(fixture, "safe_git", lambda *_a, **_kw: pytest.fail("Git reached."))
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
    prefix = isolation.sandbox_prefix(tmp_path / "task", required_inputs=(tmp_path / "staged-input",))
    assert prefix[:2] == ["/usr/bin/sandbox-exec", "-p"]
    for boundary in (*context.homes, *context.protected):
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
    inner = {"mnt": "mnt:[11]", "net": "net:[12]", "pid": "pid:[13]", "ipc": "ipc:[14]"}
    proof = {
        "uid": uid, "gid": gid, "root": str(root), "output": str(output), "state": str(cache),
        "receipt": "one.json", "namespaces": inner,
        "outer_namespaces": {"mnt": "mnt:[1]", "net": "net:[2]", "pid": "pid:[3]", "ipc": "ipc:[4]"},
        "keyring_boundary": isolation.keyring_identity(),
        "storage_context": context.record(), "storage_sha256": context.fingerprint(),
    }
    marker = tmp_path / "parent-proof.json"
    marker.write_text(json.dumps(proof))
    marker.chmod(0o644)
    marker_inode = marker.stat().st_ino
    real_fstat, real_readlink, real_read_text = os.fstat, os.readlink, Path.read_text
    status = {**{name: "0" * 16 for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")},
              "NoNewPrivs": "1", "Seccomp": "2"}
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


@pytest.mark.parametrize("failure", [
    "missing-ipc", "same-ipc", "extra-namespace", "invalid-prefix", "missing-filter", "wrong-program",
    "wrong-abi", "extra-filter-field", "missing-seccomp", "seccomp-disabled", "seccomp-strict",
    "missing-capability", "invalid-capability",
])
def test_live_private_proof_requires_exact_ipc_filter_and_privilege_binding(private_storage, failure):
    state = private_storage
    if failure == "missing-ipc":
        state.proof["namespaces"].pop("ipc")
    elif failure == "same-ipc":
        state.proof["outer_namespaces"]["ipc"] = state.proof["namespaces"]["ipc"]
    elif failure == "extra-namespace":
        state.proof["namespaces"]["user"] = "user:[15]"
    elif failure == "invalid-prefix":
        state.proof["outer_namespaces"]["ipc"] = "mnt:[4]"
    elif failure == "missing-filter":
        state.proof.pop("keyring_boundary")
    elif failure in ("wrong-program", "wrong-abi", "extra-filter-field"):
        field = {"wrong-program": "program_sha256", "wrong-abi": "abi", "extra-filter-field": "installed"}[failure]
        state.proof["keyring_boundary"][field] = "untrusted"
    elif failure == "missing-seccomp":
        state.status.pop("Seccomp")
    elif failure in ("seccomp-disabled", "seccomp-strict"):
        state.status["Seccomp"] = "0" if failure == "seccomp-disabled" else "1"
    elif failure == "missing-capability":
        state.status.pop("CapEff")
    else:
        state.status["CapEff"] = "not-hex"
    state.marker.write_text(json.dumps(state.proof))
    before = fixture.source_digest(state.root)
    with pytest.raises(RuntimeError):
        isolation.namespace_receipt()
    assert fixture.source_digest(state.root) == before


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("grant", ["/usr/bin/sandbox-exec", "/dev/null", "/usr/lib/diagnostic",
                                  "/System/Library/Frameworks/Python.framework", "/Library/Frameworks/Python.framework"])
def test_macos_every_system_and_tool_grant_uses_same_storage_admission(
        tmp_path, original_storage, monkeypatch, variable, grant):
    """Synthetic path identities only; never read system configuration contents."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "resolve", lambda path, **_kw: path)
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    # Even a protected subtree strictly inside a system input must refuse.
    monkeypatch.setenv(variable, grant + "/configured-state")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("Policy refusal must precede execution."))
    with pytest.raises(ValueError):
        isolation.sandbox_prefix(tmp_path / "task", required_inputs=(Path(grant),))
    assert not (tmp_path / "task").exists()


@pytest.mark.parametrize("shape", ["home", "home-child", "parent", "protected", "alias", "home-alias",
                                  "broad-usr", "broad-system", "broad-library", "missing-inputs", "empty-inputs"])
def test_macos_finite_input_refusals_are_before_effects(tmp_path, original_storage, monkeypatch, shape):
    monkeypatch.setattr(sys, "platform", "darwin")
    context = isolation.StorageContext.capture()
    task = tmp_path / "task"
    inputs = (tmp_path / "staged",)
    if shape == "home":
        inputs = (original_storage.home,)
    elif shape == "home-child":
        inputs = (original_storage.home / ".ssh",)
    elif shape == "parent":
        inputs = (tmp_path,)
    elif shape == "protected":
        inputs = (context.protected[0],)
    elif shape == "alias":
        alias = tmp_path / "alias"
        alias.symlink_to(original_storage.home / "Library/Application Support/Browser")
        inputs = (alias,)
    elif shape == "home-alias":
        task = original_storage.home / "looks-outside"
        task.symlink_to(tmp_path / "task")
    elif shape.startswith("broad-"):
        inputs = (Path({"broad-usr": "/usr", "broad-system": "/System", "broad-library": "/Library"}[shape]),)
    elif shape == "missing-inputs":
        inputs = None
    else:
        inputs = ()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("Refused policy executed."))
    with pytest.raises(ValueError):
        isolation.sandbox_prefix(task, required_inputs=inputs)
    assert not (tmp_path / "task").exists()


def test_macos_disjoint_unicode_inputs_reach_actual_argv_without_policy_widening(tmp_path, original_storage, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    task, inputs = tmp_path / "task-唯一", tmp_path / 'staged-"quoted"'
    inputs.mkdir()
    sentinel = inputs / "approved.txt"
    sentinel.write_bytes(b"synthetic approved input")
    for relative in (".ssh/key", ".aws/config", "Library/Application Support/Browser/profile"):
        path = original_storage.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic private sentinel")
    before = fixture.source_digest(original_storage.home)
    context = isolation.storage_context()
    calls = []
    def consume(command, **kwargs):
        assert command[:2] == ["/usr/bin/sandbox-exec", "-p"]
        policy = command[2]
        assert "(deny network*)" in policy and "(deny file-read*)" in policy and "(deny file-write*)" in policy
        assert "(allow file-read*)" not in policy and "(allow file-read-metadata)" not in policy
        for boundary in (*context.homes, *context.protected):
            assert f"(deny file-read* (subpath {json.dumps(str(boundary))}))" in policy
            assert f"(require-not (subpath {json.dumps(str(boundary))}))" in policy
        assert json.dumps(str(inputs)) in policy and json.dumps(str(task)) in policy
        assert command[3:] == ["/usr/bin/false"] and kwargs["close_fds"]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, "run", consume)
    subprocess.run([*isolation.sandbox_prefix(task, required_inputs=(inputs,)), "/usr/bin/false"], close_fds=True)
    assert len(calls) == 1 and not task.exists()
    assert fixture.source_digest(original_storage.home) == before
    assert sentinel.read_bytes() == b"synthetic approved input"


# Independent expected plan: changing only the planner cannot shrink coverage.
PREPARATION_PATHS = (
    "recipe", "source", "state", "downloads", "toolchain", "uv-toolchain", "venv", "fixtures",
    "state/home", "state/tmp", "state/config", "state/data", "state/cache",
    "state/go", "state/mod", "state/uv-cache",
)
PREPARATION_INTERNALS = (
    "downloads/go.tar.gz", "downloads/uv.tar.gz", "toolchain/bin/go", "uv-toolchain/uv",
    "venv/bin/python", "fixtures/avibe-fixture-existing/config", "state/home/.avibe",
)


def preparation_entry():
    readme = (Path(__file__).parent / "README.md").read_text()
    block = readme.split("```sh\n", 1)[1].split("\n```", 1)[0]
    body = block.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return block, compile(body, "<maintained README preparation entry>", "exec")


def setup_snapshot(root):
    """Record fake task paths without following links into any other tree."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
            info = path.lstat()
            content = os.readlink(path) if stat.S_ISLNK(info.st_mode) else (
                path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            )
            result[str(path.relative_to(root))] = (info.st_mode, info.st_ino, info.st_mtime_ns, content)
    return result


def run_preparation(task, monkeypatch):
    _, body = preparation_entry()
    monkeypatch.setattr(sys, "argv", ["-", str(Path(__file__).parent), str(task)])
    # Adapt only the temporary-path domain for a macOS pure run. The original
    # caller context, complete shared storage validator and README body are real.
    monkeypatch.setattr(isolation, "validate_temporary_root", isolation.validate_state_root)
    exec(body, {})


def assert_preparation_refuses_without_side_effects(task, all_fake_state, monkeypatch):
    before = setup_snapshot(all_fake_state)
    with monkeypatch.context() as guard:
        def forbidden(*_args, **_kwargs):
            pytest.fail("Preparation reached a write/copy/export/subprocess before complete admission.")
        guard.setattr(Path, "mkdir", forbidden)
        guard.setattr(shutil, "copytree", forbidden)
        guard.setattr(fixture, "export_fixture", forbidden)
        for name in ("run", "Popen", "check_output"):
            guard.setattr(subprocess, name, forbidden)
        with pytest.raises((ValueError, FileNotFoundError)):
            run_preparation(task, guard)
    assert setup_snapshot(all_fake_state) == before


@pytest.mark.parametrize("destination", (*PREPARATION_PATHS, *PREPARATION_INTERNALS))
@pytest.mark.parametrize("authority", ["configured", "default"])
def test_documented_preparation_checks_every_destination_before_first_write(
        tmp_path, original_storage, monkeypatch, destination, authority):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    protected = (original_storage.parent / "xdg" if authority == "configured"
                 else original_storage.home / ".config")
    protected.mkdir(parents=True)
    (protected / "sentinel").write_bytes(b"preserved fake configuration")
    if authority == "configured":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(protected))
    target = task / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(protected)
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("destination,variable", [
    ("source", "XDG_CONFIG_HOME"), ("state", "XDG_DATA_HOME"),
])
def test_documented_preparation_closes_both_exact_root_reproductions(
        tmp_path, original_storage, monkeypatch, destination, variable):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    protected = original_storage.parent
    protected.mkdir()
    (protected / "sentinel").write_bytes(b"fake production data; never real user data")
    monkeypatch.setenv(variable, str(protected))
    (task / destination).symlink_to(protected)
    # The original root diagnosis reached actual Git init or mkdir through
    # precisely these layouts. The maintained entry now reaches neither.
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)
    assert not (protected / ".git").exists()
    assert not (protected / "home").exists() and not (protected / "tmp").exists()


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("destination", ["root", "source", "state", "state/cache"])
@pytest.mark.parametrize("shape", ["equal", "descendant", "containing", "configured-alias"])
def test_documented_preparation_retains_all_original_storage_authorities(
        tmp_path, original_storage, monkeypatch, variable, destination, shape):
    protected = original_storage.parent / "storage"
    protected.mkdir(parents=True)
    (protected / "sentinel").write_bytes(b"fake protected storage")
    configured = protected
    if shape == "configured-alias":
        configured = tmp_path / "configured-alias"
        configured.symlink_to(protected)
    monkeypatch.setenv(variable, str(configured))
    target = protected
    if shape == "descendant":
        target /= ".avibe/never-created" if variable == "HOME" else "never-created"
    elif shape == "containing":
        target = protected.parent
    if destination == "root":
        task = target
    else:
        task = tmp_path / "allocated-task"
        task.mkdir(mode=0o700)
        link = task / destination
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("home_override", [False, True])
@pytest.mark.parametrize("default", (*isolation.PRODUCT_DIRS, *isolation.XDG_DEFAULTS.values()))
def test_documented_preparation_default_product_and_xdg_locations(
        tmp_path, original_storage, monkeypatch, home_override, default):
    home = tmp_path / "effective-home" if home_override else original_storage.home
    if home_override:
        monkeypatch.setenv("HOME", str(home))
    protected = home / default
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "setup-sentinel").write_bytes(b"preserved default storage")
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    (task / "source").symlink_to(protected)
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("destination", (*PREPARATION_PATHS, *PREPARATION_INTERNALS))
@pytest.mark.parametrize("existing", ["directory", "file", "internal-alias", "dangling-alias"])
def test_documented_preparation_preserves_all_preexisting_destinations(
        tmp_path, original_storage, monkeypatch, destination, existing):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    path = task / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    if existing == "directory":
        path.mkdir()
    elif existing == "file":
        path.write_bytes(b"earlier evidence")
    else:
        target = task / "preserved"
        if existing == "internal-alias":
            target.mkdir()
            (target / "sentinel").write_bytes(b"earlier evidence")
        path.symlink_to(target)
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("existing", [False, True])
def test_documented_preparation_never_ignores_protected_storage_inside_proposed_task(
        tmp_path, original_storage, monkeypatch, variable, existing):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    protected = task / "downloads/live-user-storage"
    if existing:
        protected.mkdir(parents=True)
        (protected / "sentinel").write_bytes(b"fake user storage is not task output")
    monkeypatch.setenv(variable, str(protected))
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("variable", isolation.STORAGE_ENV)
@pytest.mark.parametrize("invalid", ["relative/storage", "/tmp/../storage"])
def test_documented_preparation_invalid_context_never_reaches_setup(
        tmp_path, original_storage, monkeypatch, variable, invalid):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    monkeypatch.setenv(variable, invalid)
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


@pytest.mark.parametrize("failure", ["mode", "owner", "root-alias", "missing", "unknown-evidence"])
def test_documented_preparation_requires_exclusive_canonical_allocation(
        tmp_path, original_storage, monkeypatch, failure):
    task = tmp_path / "allocated-task"
    if failure != "missing":
        task.mkdir(mode=0o700)
    if failure == "mode":
        task.chmod(0o755)
    elif failure == "owner":
        real_lstat = Path.lstat

        def wrong_owner(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == task:
                values = list(info)
                values[4] += 1
                return os.stat_result(values)
            return info
        monkeypatch.setattr(Path, "lstat", wrong_owner)
    elif failure == "root-alias":
        alias = tmp_path / "root-alias"
        alias.symlink_to(task)
        task = alias
    elif failure == "unknown-evidence":
        (task / "unlisted-earlier-result").write_bytes(b"preserve even unrelated evidence")
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


def test_documented_preparation_fresh_task_runs_actual_copy_git_and_directory_consumers(
        tmp_path, original_storage, monkeypatch):
    task = tmp_path / "allocated-task"
    task.mkdir(mode=0o700)
    events = []
    planner = isolation.preparation_directories
    mkdir, copytree, run = Path.mkdir, shutil.copytree, subprocess.run

    def plan(root):
        result = planner(root)
        assert tuple(str(path.relative_to(task)) for path in result) == PREPARATION_PATHS
        assert list(task.iterdir()) == []
        events.append("complete-admission")
        return result

    def make(path, *args, **kwargs):
        events.append("mkdir")
        return mkdir(path, *args, **kwargs)

    def copy(*args, **kwargs):
        events.append("copy")
        return copytree(*args, **kwargs)

    def execute(command, **kwargs):
        assert command[0] == "/usr/bin/git"
        assert command[-3:] == ["init", "--template=", str(task / "source")]
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        events.append("git-init")
        return run(command, **kwargs)

    monkeypatch.setattr(isolation, "preparation_directories", plan)
    monkeypatch.setattr(Path, "mkdir", make)
    monkeypatch.setattr(shutil, "copytree", copy)
    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("HOME", str(original_storage.home))
    run_preparation(task, monkeypatch)
    assert events[0] == "complete-admission"
    assert set(events) == {"complete-admission", "mkdir", "copy", "git-init"}
    assert (task / "source/.git").is_dir()
    assert all((task / name).is_dir() for name in PREPARATION_PATHS)
    assert not list((task / "recipe").rglob("*.pyc"))
    assert (task / "recipe/isolation.py").read_bytes() == Path(isolation.__file__).read_bytes()
    assert all(not list((task / name).iterdir()) for name in (
        "downloads", "toolchain", "uv-toolchain", "venv", "fixtures", "state/uv-cache",
    ))  # No downloads, extraction, dependency installation or fixture export ran.
    assert_preparation_refuses_without_side_effects(task, tmp_path, monkeypatch)


def test_documented_preparation_shell_stops_before_any_later_instruction_on_refusal(tmp_path):
    block, _ = preparation_entry()
    # Actual isolated interpreter startup; an absent inspected recipe refuses
    # before the preparation import. HOST Python path adaptation only.
    block = block.replace("/usr/bin/python3", sys.executable)
    result = subprocess.run(
        ["/bin/sh", "-c", block + "\nexit 99\n"],
        env={"PATH": "/usr/bin:/bin", "recipe_source": str(tmp_path / "absent-recipe"),
             "engine_task": str(tmp_path / "absent-task")},
        stdin=subprocess.DEVNULL, capture_output=True, close_fds=True, timeout=5,
    )
    assert result.returncode == 1
    assert not (tmp_path / "absent-task").exists()
