"""Pure-source receipt regressions; no engine process or network access."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from fixture import export_fixture, source_digest, verify_fixture
from isolation import STORAGE_ENV, isolated_environment, safe_git
import verify


def frozen_repository(tmp_path: Path) -> tuple[Path, dict]:
    repository = tmp_path / "repository"
    repository.mkdir()
    env = isolated_environment(tmp_path / "state")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(repository), *args], env=env, text=True,
        ).strip()

    safe_git(repository, "init")
    (repository / "fixture_model.py").write_text("MODEL = 'frozen-模型'\n")
    (repository / "unchanged.go").write_text("package unchanged\n")
    (repository / "facts").mkdir()
    (repository / "facts/config.json").write_text('{"model":"frozen-模型"}')
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "fixture")
    base = git("rev-parse", "HEAD")
    archive = subprocess.check_output(["/usr/bin/git", "-C", str(repository), "archive", "--format=tar", base], env=env)
    return repository, {
        "avibe_fixture_base": base,
        "avibe_fixture_tree": git("rev-parse", f"{base}^{{tree}}"),
        "avibe_fixture_archive_sha256": hashlib.sha256(archive).hexdigest(),
    }


def test_frozen_export_never_imports_dirty_or_untracked_checkout(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    (repository / "fixture_model.py").write_text("raise RuntimeError('dirty source executed')\n")
    (repository / "untracked.py").write_text("raise RuntimeError('untracked source executed')\n")
    exported, identity = export_fixture(repository, tmp_path, receipt)
    assert not (exported / "untracked.py").exists()
    assert identity["commit"] == receipt["avibe_fixture_base"]
    # A clean interpreter imports from the frozen export even with dirty cwd.
    result = subprocess.check_output(
        [sys.executable, "-I", "-B", "-c",
         "import sys; sys.path.insert(0,sys.argv[1]); import fixture_model; print(fixture_model.MODEL)",
         str(exported)], cwd=repository, env=isolated_environment(tmp_path / "child"), text=True,
    )
    assert result.strip() == "frozen-模型"
    verify_fixture(exported, identity["source_sha256"])


@pytest.mark.parametrize("mutation", ["content", "extra", "missing", "escape"])
def test_fixture_digest_rejects_changed_closure(tmp_path: Path, mutation: str) -> None:
    repository, receipt = frozen_repository(tmp_path)
    exported, identity = export_fixture(repository, tmp_path, receipt)
    if mutation == "content":
        (exported / "facts/config.json").write_text('{"model":"changed"}')
    elif mutation == "extra":
        (exported / "extra.py").write_text("VALUE = 1")
    elif mutation == "missing":
        (exported / "fixture_model.py").unlink()
    else:
        (exported / "outside.py").symlink_to(repository / "fixture_model.py")
    with pytest.raises((RuntimeError, ValueError)):
        verify_fixture(exported, identity["source_sha256"])


def test_wrong_archive_refuses_before_materializing_fixture(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    receipt["avibe_fixture_archive_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="archive"):
        export_fixture(repository, tmp_path, receipt)
    assert not list(tmp_path.glob("avibe-fixture-*"))


def test_fixture_source_digest_is_path_independent(tmp_path: Path) -> None:
    repository, receipt = frozen_repository(tmp_path)
    first, identity = export_fixture(repository, tmp_path, receipt)
    second, second_identity = export_fixture(repository, tmp_path, receipt)
    assert first != second
    assert second_identity == identity
    assert source_digest(first) == source_digest(second)


def test_single_link_digest_keeps_independent_encoding_and_internal_symlinks(tmp_path):
    root = tmp_path / "source"
    (root / "facts").mkdir(parents=True)
    (root / "facts/模型.txt").write_bytes(b"exact task-only bytes")
    (root / "internal-link").symlink_to("facts/模型.txt")
    expected = hashlib.sha256()
    # Independent named records preserve the preexisting sorted/length encoding.
    for record in ((b"facts", b"dir", b""),
                   ("facts/模型.txt".encode(), b"file", b"exact task-only bytes"),
                   (b"internal-link", b"link", "facts/模型.txt".encode())):
        for field in record:
            expected.update(len(field).to_bytes(8, "big"))
            expected.update(field)
    assert source_digest(root) == expected.hexdigest()
    verify_fixture(root, expected.hexdigest())


@pytest.mark.parametrize("alias_location", ["inside", "outside", "writable-state"])
def test_complete_source_digest_rejects_real_hardlink_aliases(tmp_path, alias_location):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "unchanged.go"
    target.write_bytes(b"normal unpatched source")
    expected = source_digest(root)
    parent = root if alias_location == "inside" else tmp_path / alias_location
    if parent != root:
        parent.mkdir()
    alias = parent / "alias"
    os.link(target, alias)
    with pytest.raises(ValueError, match="single-link regular"):
        verify_fixture(root, expected)
    assert target.read_bytes() == alias.read_bytes() == b"normal unpatched source"


@pytest.mark.parametrize("mutation", ["replacement", "hardlink", "fifo"])
def test_actual_digest_refuses_leaf_drift_at_consumed_open(tmp_path, monkeypatch, mutation):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "file"
    target.write_bytes(b"unchanged content")
    original_open, original_fstat = os.open, os.fstat
    opened = []

    def opening(path, flags, *args, **kwargs):
        assert path == target and flags & os.O_NONBLOCK
        if mutation == "hardlink":
            os.link(target, tmp_path / "alias")
        else:
            target.rename(tmp_path / "preserved")
            if mutation == "replacement":
                target.write_bytes(b"unchanged content")
            else:
                os.mkfifo(target)
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", opening)
    with pytest.raises(ValueError):
        source_digest(root)
    assert len(opened) == 1
    with pytest.raises(OSError):
        original_fstat(opened[0])


def apply_fixture(repository, receipt, tmp_path, monkeypatch):
    """Actual apply/status/candidate consumers, with a harmless exact-base patch."""
    recipe = tmp_path / "apply-recipe"
    recipe.mkdir()
    before = (repository / "fixture_model.py").read_bytes()
    after = b"MODEL = 'patched task-only text'\n"
    patch = ("diff --git a/fixture_model.py b/fixture_model.py\n"
             "--- a/fixture_model.py\n+++ b/fixture_model.py\n@@ -1 +1 @@\n"
             "-" + before.decode() + "+" + after.decode())
    (recipe / "native-intent.patch").write_text(patch)
    (recipe / "inputs.json").write_text(json.dumps({
        "source_sha": receipt["avibe_fixture_base"],
        "sha256": {"facts/config.json": hashlib.sha256((repository / "facts/config.json").read_bytes()).hexdigest()},
    }))
    (recipe / "patched-files.json").write_text(json.dumps({
        "fixture_model.py": hashlib.sha256(after).hexdigest(),
    }))
    monkeypatch.setattr(verify, "HERE", recipe)
    monkeypatch.setattr(sys, "argv", [
        "verify.py", "apply", "--source", str(repository), "--state", str(tmp_path / "apply-state"),
    ])
    return after


@pytest.mark.parametrize("name", ["facts/config.json", "fixture_model.py"])
def test_actual_verify_input_and_candidate_reads_reject_hardlinks(tmp_path, monkeypatch, name):
    repository, receipt = frozen_repository(tmp_path)
    expected = apply_fixture(repository, receipt, tmp_path, monkeypatch)
    verify.main()  # Actual task-only Git apply, before installing the alias.
    target = repository / name
    os.link(target, tmp_path / "state-alias")
    consumer = verify.verify_inputs if name == "facts/config.json" else verify.verify_candidate
    with pytest.raises(ValueError, match="single-link regular"):
        consumer(repository)
    assert (repository / "fixture_model.py").read_bytes() == expected


@pytest.mark.parametrize("name", ["facts/config.json", "fixture_model.py", "unchanged.go"])
def test_actual_apply_refuses_complete_hardlinked_source_before_patch_write(tmp_path, monkeypatch, name):
    repository, receipt = frozen_repository(tmp_path)
    apply_fixture(repository, receipt, tmp_path, monkeypatch)
    before = (repository / "fixture_model.py").read_bytes()
    os.link(repository / name, tmp_path / "writable-state-alias")
    original_git = verify.safe_git
    calls = []

    def git(source, *arguments):
        calls.append(arguments)
        assert arguments[0] != "apply", "Rejected closure reached patch effect."
        return original_git(source, *arguments)

    monkeypatch.setattr(verify, "safe_git", git)
    with pytest.raises(ValueError, match="single-link regular"):
        verify.main()
    assert (repository / "fixture_model.py").read_bytes() == before
    assert not (tmp_path / "apply-state").exists()
    assert not any(command[0] == "apply" for command in calls)


def git_poison(tmp_path, monkeypatch, source):
    """All poison destinations and marker scripts are task-owned, never user config."""
    marker = tmp_path / "unwanted-execution"
    helper = tmp_path / "unwanted-helper"
    helper.write_text("#!/bin/sh\nprintf unwanted >> " + shlex.quote(str(marker)) + "\nexit 73\n")
    helper.chmod(0o700)
    template = tmp_path / "hostile-template"
    (template / "hooks").mkdir(parents=True)
    for name in ("post-checkout", "post-index-change"):
        (template / "hooks" / name).write_bytes(helper.read_bytes())
        (template / "hooks" / name).chmod(0o700)
    configuration = tmp_path / "hostile-config"
    configuration.write_text(
        f'[init]\n\ttemplateDir = "{template}"\n[core]\n\thooksPath = "{template / "hooks"}"\n'
        f'\tfsmonitor = "{helper}"\n[filter "hostile"]\n\tclean = "{helper}"\n'
        f'\tsmudge = "{helper}"\n[diff "hostile"]\n\tcommand = "{helper}"\n\ttextconv = "{helper}"\n'
        f'[credential]\n\thelper = "!{helper}"\n',
    )
    if source in ("global", "system"):
        monkeypatch.setenv("GIT_CONFIG_" + source.upper(), str(configuration))
        if source == "system":
            monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    elif source == "count":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
        for index, (key, value) in enumerate((
                ("init.templateDir", str(template)), ("core.fsmonitor", str(helper)),
                ("filter.hostile.clean", str(helper)))):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    elif source == "parameters":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", " ".join(shlex.quote(value) for value in (
            f"init.templateDir={template}", f"core.hooksPath={template / 'hooks'}",
            f"filter.hostile.clean={helper}", f"core.fsmonitor={helper}",
        )))
    else:
        assert source == "environment"
        for name, value in {
            "GIT_TEMPLATE_DIR": str(template), "GIT_EXEC_PATH": str(tmp_path),
            "GIT_EXTERNAL_DIFF": str(helper), "GIT_SSH_COMMAND": str(helper),
            "GIT_ASKPASS": str(helper), "SSH_ASKPASS": str(helper),
            "GIT_DIR": str(tmp_path / "wrong-git-dir"), "GIT_WORK_TREE": str(tmp_path / "wrong-worktree"),
            "GIT_CONFIG": str(configuration), "GIT_CONFIG_SYSTEM": str(configuration),
            "GIT_OBJECT_DIRECTORY": str(tmp_path / "wrong-objects"), "GIT_INDEX_FILE": str(tmp_path / "wrong-index"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path / "wrong-alternates"),
        }.items():
            monkeypatch.setenv(name, value)
    return marker, helper, template


@pytest.mark.parametrize("source", ["global", "system", "count", "parameters", "environment"])
@pytest.mark.parametrize("consumer", ["checkout", "export", "apply"])
def test_actual_git_consumers_ignore_ambient_execution_authority(tmp_path, monkeypatch, source, consumer):
    repository, receipt = frozen_repository(tmp_path)
    expected = apply_fixture(repository, receipt, tmp_path, monkeypatch) if consumer == "apply" else None
    marker, helper, template = git_poison(tmp_path, monkeypatch, source)
    environment_before = dict(os.environ)
    fresh = tmp_path / "fresh-init"
    safe_git(fresh, "init")
    assert not (fresh / ".git/hooks").exists()
    # Even manually placed local hooks are disabled at their actual consumer.
    hooks = repository / ".git/hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-index-change"):
        (hooks / name).write_bytes(helper.read_bytes())
        (hooks / name).chmod(0o700)
    (repository / ".git/info").mkdir(exist_ok=True)
    (repository / ".git/info/attributes").write_text("fixture_model.py filter=hostile diff=hostile\n")
    template_before = source_digest(template)
    if consumer == "checkout":
        (repository / "fixture_model.py").unlink()
        safe_git(repository, "checkout", "--", "fixture_model.py")
        safe_git(repository, "checkout", "--detach", receipt["avibe_fixture_base"])
        assert (repository / "fixture_model.py").is_file()
    elif consumer == "export":
        (repository / "fixture_model.py").write_text("dirty, not archived\n")
        exported, identity = export_fixture(repository, tmp_path, receipt)
        assert identity["commit"] == receipt["avibe_fixture_base"]
        assert "frozen" in (exported / "fixture_model.py").read_text()
    else:
        verify.main()
        assert (repository / "fixture_model.py").read_bytes() == expected
    assert not marker.exists() and source_digest(template) == template_before
    assert dict(os.environ) == environment_before


@pytest.mark.parametrize("key", [
    "init.templateDir", "include.path", "includeIf.gitdir:/.path", "core.hooksPath",
    "core.fsmonitor", "core.worktree", "core.gitProxy", "core.sshCommand",
    "core.attributesFile", "core.excludesFile", "diff.external", "diff.hostile.command",
    "diff.hostile.textconv", "filter.hostile.clean", "filter.hostile.smudge", "filter.hostile.process",
    "credential.helper", "url.hostile.insteadOf", "remote.origin.uploadpack",
    "remote.origin.vcs", "remote.origin.proxy", "http.proxy", "submodule.recurse",
    "extensions.worktreeConfig",
])
@pytest.mark.parametrize("consumer", ["export", "apply"])
def test_actual_git_consumers_refuse_local_executable_or_redirecting_config(
        tmp_path, monkeypatch, key, consumer):
    repository, receipt = frozen_repository(tmp_path)
    if consumer == "apply":
        apply_fixture(repository, receipt, tmp_path, monkeypatch)
    marker, helper, _ = git_poison(tmp_path, monkeypatch, "global")
    # Fixture construction itself has a finite clean environment.
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    value = "true" if key in ("extensions.worktreeConfig", "submodule.recurse") else str(helper)
    subprocess.run(
        ["/usr/bin/git", "config", "--file", str(repository / ".git/config"), key, value],
        env=env, check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
    )
    before = source_digest(repository)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        if consumer == "export":
            export_fixture(repository, tmp_path, receipt)
        else:
            verify.main()
    assert not marker.exists() and source_digest(repository) == before
    assert not list(tmp_path.glob("avibe-fixture-*"))


@pytest.mark.parametrize("failure", ["dirty", "untracked", "base", "input", "candidate"])
def test_shared_git_boundary_preserves_actual_apply_identity_refusals(tmp_path, monkeypatch, failure):
    repository, receipt = frozen_repository(tmp_path)
    apply_fixture(repository, receipt, tmp_path, monkeypatch)
    if failure == "dirty":
        (repository / "fixture_model.py").write_text("preserve a peer edit\n")
    elif failure == "untracked":
        (repository / "peer").write_text("preserve unrelated work\n")
    elif failure == "input":
        (repository / "facts/config.json").write_text("changed input")
    elif failure == "base":
        record = json.loads((verify.HERE / "inputs.json").read_text())
        record["source_sha"] = "0" * 40
        (verify.HERE / "inputs.json").write_text(json.dumps(record))
    else:
        verify.main()
        (repository / "fixture_model.py").write_text("not the maintained candidate\n")
    before = source_digest(repository)
    with pytest.raises(RuntimeError):
        verify.verify_candidate(repository) if failure == "candidate" else verify.main()
    assert source_digest(repository) == before


def test_documented_fetch_uses_shared_command_local_boundary_without_network(tmp_path, monkeypatch):
    task = tmp_path / "task"
    (task / "recipe").mkdir(parents=True)
    repository = task / "source"
    safe_git(repository, "init")
    readme = (Path(__file__).parent / "README.md").read_text()
    block = next(block for block in re.findall(r"```sh\n(.*?)\n```", readme, re.S)
                 if '"fetch", "--depth=1"' in block)
    code = block.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    check_output = subprocess.check_output
    calls = []

    def command(argv, **kwargs):
        assert argv[0] == "/usr/bin/git" and kwargs["env"]["GIT_CONFIG_COUNT"] == "0"
        if "config" in argv:
            return check_output(argv, **kwargs)
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "protocol.allow=never" in argv and "protocol.https.allow=always" in argv
        calls.append(argv[argv.index(str(repository)) + 1:])
        return b""  # Sole finite process seam: never execute fetch/transport.

    monkeypatch.setattr(subprocess, "check_output", command)
    monkeypatch.setattr(sys, "argv", ["-", str(task)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(code, "<maintained fetch caller; transport forbidden>", "exec"), {})
    assert calls == [
        ["fetch", "--no-recurse-submodules", "--depth=1",
         "https://github.com/router-for-me/CLIProxyAPI.git", "2a6b87aca083a5bf498ac1f68a1b636c500d7aaa"],
        ["checkout", "--detach", "FETCH_HEAD"],
    ]


@pytest.mark.parametrize("entry", ["preparation", "fetch", "archive", "export", "apply", "storage", "parent", "phase"])
@pytest.mark.parametrize("shadow", ["cwd", "pythonpath", "user-site", "path"])
def test_documented_python_starts_isolated_before_recipe_import(tmp_path, entry, shadow):
    """Actual shell/interpreter startup; fake recipe stops before all work/sudo.

    HOST-only adaptation: the documented absolute Linux interpreter is replaced
    by this installed base interpreter; the public sudo prefix is not executed.
    Neither this test nor the body-consuming tests claim Linux custody evidence.
    """
    readme = (Path(__file__).parent / "README.md").read_text()
    blocks = re.findall(r"```sh\n(.*?)\n```", readme, re.S)
    task = tmp_path / "task"
    recipe = task / "recipe"
    recipe.mkdir(parents=True)
    cwd, pythonpath, home, binary = [tmp_path / name for name in ("cwd", "pythonpath", "home", "bin")]
    for path in (cwd, pythonpath, home, binary):
        path.mkdir()
    marker = tmp_path / "shadow-executed"
    poison = f"from pathlib import Path; Path({str(marker)!r}).write_text('shadow'); raise SystemExit(73)\n"
    env = {name: str(tmp_path / ("original-" + name)) for name in STORAGE_ENV}
    env.update(HOME=str(home), PATH="/usr/bin:/bin", TMPDIR=str(tmp_path),
               engine_task=str(task), recipe_source=str(recipe), fixture_root=str(task / "fixtures"),
               avibe_checkout=str(tmp_path / "never-opened-repository"), caller_storage_sha256="not-used")
    if shadow in ("cwd", "pythonpath"):
        directory = cwd if shadow == "cwd" else pythonpath
        for name in ("subprocess", "pathlib", "sitecustomize"):
            (directory / (name + ".py")).write_text(poison)
        if shadow == "pythonpath":
            env["PYTHONPATH"] = str(directory)
    elif shadow == "user-site":
        userbase = tmp_path / "usersite"
        env["PYTHONUSERBASE"] = str(userbase)
        # Ask the same trusted HOST interpreter only for its task-local site
        # location. -I prevents activation during this read-only calculation.
        result = subprocess.run(
            [sys._base_executable, "-I", "-B", "-c", "import site; print(site.getusersitepackages())"],
            env=env, capture_output=True, text=True, check=True, timeout=5,
        )
        user_site = Path(result.stdout.strip())
        assert user_site.is_relative_to(userbase)
        user_site.mkdir(parents=True)
        (user_site / "usercustomize.py").write_text(poison)
        (user_site / "startup.pth").write_text("import pathlib; pathlib.Path(" + repr(str(marker)) + ").touch()\n")
    else:
        fake = binary / "python3"
        fake.write_text("#!/bin/sh\nprintf shadow > " + shlex.quote(str(marker)) + "\nexit 74\n")
        fake.chmod(0o700)
        env["PATH"] = str(binary) + ":/usr/bin:/bin"
    # These finite fake import consumers record the unmodified original storage
    # and flags, then stop. The actual admission/Git/parent consumers are tested
    # separately above and in the state/namespace suites.
    fake_module = (
        "import json, os, sys\n"
        f"names = {STORAGE_ENV!r}\n"
        "def stop(*args, **kwargs):\n"
        "    print('STARTUP=' + json.dumps({'storage': {k: os.environ.get(k) for k in names},\n"
        "          'isolated': sys.flags.isolated, 'user_site': sys.flags.no_user_site,\n"
        "          'bytecode': sys.dont_write_bytecode}), file=sys.stderr, flush=True)\n"
        "    raise SystemExit(41)\n"
        "preparation_directories = safe_git = verify_go = export_fixture = storage_context = main = stop\n"
        "PHASES = {}\n"
    )
    for name in ("isolation", "execution_inputs", "fixture", "verify", "namespace", "budgets"):
        (recipe / (name + ".py")).write_text(fake_module)
    (recipe / "inputs.json").write_text("{}")
    (recipe / "prerequisites.json").write_text('{"linux_arm64_go": {}}')
    selected = {
        "preparation": blocks[0], "fetch": blocks[1], "archive": blocks[2],
        "export": next(block for block in blocks if "fixture_root=$(" in block),
        "apply": next(block for block in blocks if "import verify; verify.main()" in block),
        "storage": next(block for block in blocks if "caller_storage_sha256=$(" in block),
        "phase": next(block for block in blocks if "run_phase() {" in block),
    }
    parent = selected["storage"].split("/usr/bin/sudo -n ", 1)[1]
    selected["parent"] = parent
    block = selected[entry].replace("/usr/bin/python3", shlex.quote(sys._base_executable))
    # Every selected body must stop before sudo. The one parent startup case
    # omits only the privilege prefix and invokes a task-only fake namespace.
    result = subprocess.run(
        ["/bin/sh", "-c", block], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, close_fds=True, timeout=10,
    )
    assert result.returncode == (1 if entry in ("preparation", "export", "storage") else 41), result.stderr
    records = [json.loads(line.removeprefix("STARTUP=")) for line in result.stderr.splitlines()
               if line.startswith("STARTUP=")]
    assert records == [{"storage": {name: env[name] for name in STORAGE_ENV},
                        "isolated": 1, "user_site": 1, "bytecode": True}]
    assert not marker.exists()
    assert sorted(path.name for path in task.iterdir()) == ["recipe"]
    assert not list(recipe.glob("*.pyc")) and not (recipe / "__pycache__").exists()
