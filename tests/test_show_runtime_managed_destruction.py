"""Ownership must dominate every destructive Show Runtime filesystem effect.

The census deliberately derives calls from ``core.show_runtime``. Its known blind
spots are dynamic ``getattr`` dispatch, callables passed out of the module and
invoked elsewhere, and arbitrary subprocess semantics beyond the argv patterns
listed in ``_destructive_install_command``.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from core import show_runtime


_NOT_YET_MIGRATED_DESTRUCTIVE_SITES = (
    ("ShowRuntimeManager", "_admit_runtime_start", "Path.open:w"),
    ("ShowRuntimeManager", "_admit_runtime_start", "Path.open:w"),
    ("ShowRuntimeManager", "_remove_managed_runtime_tree_for_replacement", "shutil.rmtree"),
    ("ShowRuntimeManager", "_clean_locked", "shutil.rmtree"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.rename"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.rename"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.rename"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.unlink"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.unlink"),
    ("ShowRuntimeManager", "_clean_downloaded_archives", "os.unlink"),
    ("ShowRuntimeManager", "_clean_manifest_install_dirs", "shutil.rmtree"),
    ("ShowRuntimeManager", "_install_archive_runtime", "shutil.rmtree"),
    ("ShowRuntimeManager", "_install_manifest_runtime_locked", "shutil.rmtree"),
    ("ShowRuntimeManager", "_install_npm_runtime", "Path.open:w"),
    ("ShowRuntimeManager", "_install_npm_runtime", "Path.write_text"),
    ("ShowRuntimeManager", "_resolve_manifest_archive", "Path.replace"),
    ("ShowRuntimeManager", "_resolve_manifest_archive", "Path.unlink"),
    ("ShowRuntimeManager", "_resolve_manifest_archive", "Path.unlink"),
    ("ShowRuntimeManager", "_run_install_command", "Path.open:a"),
    ("<module>", "_safe_extract_tar", "TarFile.extractall"),
    ("<module>", "_safe_extract_tar", "TarFile.extractall"),
    ("ShowRuntimeManager", "_write_archive_manifest", "Path.write_text"),
    ("ShowRuntimeManager", "_write_current_manifest_pointer", "Path.write_text"),
    ("ShowRuntimeManager", "_write_manifest_install_metadata", "Path.write_text"),
    ("ShowRuntimeManager", "_copy_packaged_runtime_archive", "Path.open:wb"),
)


@dataclass(frozen=True)
class _Effect:
    owner: str
    function: str
    operation: str
    capability_dominated: bool
    lineno: int


def _call_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    def resolve(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            value = resolve(node.value)
            return f"{value}.{node.attr}" if value else node.attr
        return None

    return resolve(call.func)


def _mode(call: ast.Call, *, method: bool) -> str | None:
    value: ast.expr | None = None
    position = 0 if method else 1
    if len(call.args) > position:
        value = call.args[position]
    for keyword in call.keywords:
        if keyword.arg == "mode":
            value = keyword.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _literal_words(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        value.value
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )


def _destructive_install_command(call: ast.Call) -> bool:
    words = _literal_words(call)
    if "clone" in words or "fetch" in words or "init" in words:
        return True
    if "checkout" in words or "clean" in words or {"remote", "add"} <= set(words):
        return True
    return "ci" in words or ("run" in words and "build" in words)


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def _path_receiver(node: ast.expr, path_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in path_names or node.id.endswith(("_path", "_dir"))
    if isinstance(node, ast.Attribute):
        return node.attr.endswith(("_path", "_dir")) or _path_receiver(node.value, path_names)
    if isinstance(node, ast.Call):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        return name.endswith(("_path", "_dir")) or any(
            _path_receiver(arg, path_names) for arg in node.args if isinstance(arg, ast.expr)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_receiver(node.left, path_names)
    return False


def _module_destructive_effects() -> list[_Effect]:
    tree = ast.parse(inspect.getsource(show_runtime))
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"

    scopes: list[tuple[str, list[ast.stmt]]] = [("<module>", tree.body)]
    scopes.extend(
        (class_node.name, class_node.body)
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
    )
    effects: list[_Effect] = []
    for owner_name, statements in scopes:
        for function in (
            node
            for node in statements
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            local_aliases = dict(aliases)
            capability_names = {
                argument.arg
                for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
                if _annotation_name(argument.annotation) == "_ManagedBytesOwnership"
            }
            path_names = {
                argument.arg
                for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
                if _annotation_name(argument.annotation) == "Path"
            }
            capability_derived_names = set(capability_names)
            changed = True
            while changed:
                changed = False
                for node in ast.walk(function):
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    value = node.value
                    if value is None or not _path_receiver(value, path_names):
                        continue
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id not in path_names:
                            path_names.add(target.id)
                            changed = True
                    referenced_names = {
                        item.id for item in ast.walk(value) if isinstance(item, ast.Name)
                    }
                    if capability_derived_names & referenced_names:
                        for target in targets:
                            if isinstance(target, ast.Name) and target.id not in capability_derived_names:
                                capability_derived_names.add(target.id)
                                changed = True
            for node in ast.walk(function):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, (ast.Name, ast.Attribute)):
                    resolved = _call_name(ast.Call(func=node.value, args=[], keywords=[]), local_aliases)
                    if resolved:
                        local_aliases[target.id] = resolved
                elif (
                    isinstance(node.value, ast.Call)
                    and _call_name(node.value, local_aliases) == "functools.partial"
                    and node.value.args
                    and isinstance(node.value.args[0], (ast.Name, ast.Attribute))
                ):
                    resolved = _call_name(
                        ast.Call(func=node.value.args[0], args=[], keywords=[]),
                        local_aliases,
                    )
                    if resolved:
                        local_aliases[target.id] = resolved

            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                name = _call_name(call, local_aliases)
                if not name:
                    continue
                operation: str | None = None
                leaf = name.rsplit(".", 1)[-1]
                if name == "shutil.rmtree":
                    operation = name
                elif name in {"os.unlink", "os.rename", "os.replace"}:
                    operation = name
                elif (
                    leaf in {"unlink", "rename", "replace", "write_text", "write_bytes"}
                    and isinstance(call.func, ast.Attribute)
                    and _path_receiver(call.func.value, path_names)
                ):
                    operation = f"Path.{leaf}"
                elif leaf == "extractall":
                    operation = "TarFile.extractall"
                elif leaf == "open":
                    method = isinstance(call.func, ast.Attribute)
                    mode = _mode(call, method=method)
                    if mode and any(flag in mode for flag in "wax+"):
                        operation = f"Path.open:{mode}"
                elif leaf == "_run_install_command" and _destructive_install_command(call):
                    operation = "install-command:" + ":".join(_literal_words(call))
                if operation:
                    referenced_names = {
                        node.id for node in ast.walk(call) if isinstance(node, ast.Name)
                    }
                    effects.append(
                        _Effect(
                            owner_name,
                            function.name,
                            operation,
                            bool(capability_derived_names & referenced_names),
                            call.lineno,
                        )
                    )
    return effects


def test_every_destructive_effect_is_capability_dominated_or_explicit_debt() -> None:
    effects = _module_destructive_effects()
    debt = list(_NOT_YET_MIGRATED_DESTRUCTIVE_SITES)
    uncovered: list[tuple[str, str, str]] = []
    for effect in effects:
        key = (effect.owner, effect.function, effect.operation)
        if key in debt:
            debt.remove(key)
        elif not effect.capability_dominated:
            uncovered.append(key)

    assert uncovered == []
    assert debt == []

    source = inspect.getsource(show_runtime)
    assert "class _ManagedPublishedBytesOwner" in source
    manager_tree = ast.parse(textwrap.dedent(inspect.getsource(show_runtime.ShowRuntimeManager))).body[0]
    capability_functions = {
        function.name
        for function in manager_tree.body
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            _annotation_name(argument.annotation) == "_ManagedBytesOwnership"
            for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
        )
    }
    assert capability_functions == {
        "_clone_github_staging",
        "_build_github_staging",
        "_write_github_build_marker",
    }

    tree = ast.parse(inspect.getsource(show_runtime))
    scopes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    for effect in effects:
        if (effect.owner, effect.function, effect.operation) in _NOT_YET_MIGRATED_DESTRUCTIVE_SITES:
            continue
        function = next(
            node
            for node in scopes[effect.owner].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == effect.function
        )
        capability_names = {
            argument.arg
            for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
            if _annotation_name(argument.annotation) == "_ManagedBytesOwnership"
        }
        guarded = {
            node.value.id
            for guard in ast.walk(function)
            if isinstance(guard, ast.If)
            and guard.lineno < effect.lineno
            and any(isinstance(item, (ast.Raise, ast.Return)) for item in guard.body)
            for node in ast.walk(guard.test)
            if isinstance(node, ast.Attribute)
            and node.attr == "may_destroy"
            and isinstance(node.value, ast.Name)
        }
        guarded.update(
            argument.id
            for guard in ast.walk(function)
            if isinstance(guard, ast.If)
            and guard.lineno < effect.lineno
            and any(isinstance(item, (ast.Raise, ast.Return)) for item in guard.body)
            for call in ast.walk(guard.test)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "allows_destruction"
            for argument in call.args
            if isinstance(argument, ast.Name)
        )
        assert capability_names & guarded, (
            effect.owner,
            effect.function,
            effect.operation,
        )


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_host_git_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _legacy_github_checkout(tmp_path: Path) -> tuple[show_runtime.ShowRuntimeManager, Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    (upstream / "package.json").write_text('{"name":"runtime"}\n', encoding="utf-8")
    _git("add", "package.json", cwd=upstream)
    _git("commit", "-m", "initial", cwd=upstream)

    manager = show_runtime.ShowRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        runtime_source="github-source",
        github_repo=str(upstream),
        github_ref="main",
    )
    source_dir = manager._github_source_dir()
    _git("clone", str(upstream), str(source_dir), cwd=tmp_path)
    cli_path = source_dir / "packages" / "runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("old runtime\n", encoding="utf-8")
    return manager, source_dir, cli_path


def _recorded_github_checkout(
    tmp_path: Path,
) -> tuple[show_runtime._ManagedPublishedBytesOwner, Path, str, str]:
    owner = show_runtime._ManagedPublishedBytesOwner()
    repo = "https://example.invalid/avibe/show-runtime.git"
    ref = "main"
    checkout = tmp_path / "published"
    created = owner.create_staging(checkout, repo=repo, ref=ref)
    _git("init", "-b", ref, cwd=checkout)
    (checkout / "runtime.js").write_text("runtime\n", encoding="utf-8")
    _git("add", "runtime.js", cwd=checkout)
    _git("commit", "-m", "runtime", cwd=checkout)
    _git("remote", "add", "origin", repo, cwd=checkout)
    revision = _git("rev-parse", "HEAD", cwd=checkout)
    assert owner.record_github_checkout(
        created,
        git=["git"],
        repo=repo,
        ref=ref,
        revision=revision,
    )
    return owner, checkout, repo, ref


def test_legacy_checkout_without_creation_record_refuses_but_keeps_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager, source_dir, cli_path = _legacy_github_checkout(tmp_path)
    monkeypatch.setattr(show_runtime, "_resolve_node_command", lambda: ["/usr/bin/node"])
    real_resolve = show_runtime._resolve_command
    monkeypatch.setattr(
        show_runtime,
        "_resolve_command",
        lambda name: ["/usr/bin/npm"] if name == "npm" else real_resolve(name),
    )

    def fake_clone(staged: show_runtime._ManagedBytesOwnership, _git_command: list[str]) -> str:
        _git("init", "-b", "main", cwd=staged.path)
        _git("remote", "add", "origin", manager.github_repo, cwd=staged.path)
        return "0123456789abcdef"

    def fake_build(
        staged: show_runtime._ManagedBytesOwnership,
        _npm: list[str],
        node: list[str],
        revision: str,
    ) -> list[str]:
        staged_cli = staged.path / "packages" / "runtime" / "dist" / "cli.js"
        staged_cli.parent.mkdir(parents=True)
        staged_cli.write_text("new runtime\n", encoding="utf-8")
        manager._write_github_build_marker(staged, revision)
        return [*node, str(staged_cli)]

    monkeypatch.setattr(manager, "_clone_github_staging", fake_clone)
    monkeypatch.setattr(manager, "_build_github_staging", fake_build)

    result = manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_revision_unverified"
    assert result["status"]["install"]["state"] == "installed"
    assert result["status"]["command"] == ["/usr/bin/node", str(cli_path)]
    assert source_dir.exists()
    assert cli_path.read_text(encoding="utf-8") == "old runtime\n"


def test_github_ownership_record_is_written_before_publish() -> None:
    source = textwrap.dedent(inspect.getsource(show_runtime.ShowRuntimeManager._install_github_runtime))
    tree = ast.parse(source)
    calls = [
        node.func.attr
        for node in sorted(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
    ]
    assert calls.index("record_github_checkout") < calls.index("publish")


def test_checkout_record_schema_names_creation_inputs(tmp_path: Path) -> None:
    owner = show_runtime._ManagedPublishedBytesOwner()
    staged = owner.create_staging(tmp_path / "checkout", repo="example/repo", ref="main")
    record_path = staged.path / "avibe-managed-checkout.json"
    initial = json.loads(record_path.read_text(encoding="utf-8"))
    assert initial == {
        "schema_version": 1,
        "repo": "example/repo",
        "ref": "main",
        "revision": None,
        "origin": None,
        "device": staged._identity[0],
        "inode": staged._identity[1],
    }
    _git("init", "-b", "main", cwd=staged.path)
    _git("remote", "add", "origin", "example/repo", cwd=staged.path)
    record = owner.record_github_checkout(
        staged,
        git=["git"],
        repo="example/repo",
        ref="main",
        revision="0123456789abcdef",
    )

    assert record is True
    payload = json.loads(record_path.read_text())
    assert payload == {
        "schema_version": 1,
        "repo": "example/repo",
        "ref": "main",
        "revision": "0123456789abcdef",
        "origin": "example/repo",
        "device": staged._identity[0],
        "inode": staged._identity[1],
    }


def test_recorded_checkout_and_own_build_marker_are_safe_to_replace(tmp_path: Path) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    (checkout / ".avibe-runtime-build").write_text("built\n", encoding="utf-8")
    generated = (
        checkout / "node_modules" / "package" / "index.js",
        checkout / "packages" / "runtime" / "dist" / "cli.js",
    )
    for path in generated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")

    ownership = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.PROVEN_MANAGED
    assert ownership.may_destroy is True
    assert ownership.reason is None
    assert ownership.blocking_paths == ()


def test_globally_ignored_untracked_path_never_changes_ownership_or_safety(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    ignore_file = tmp_path / "global-ignore"
    ignore_file.write_text(".envrc\n", encoding="utf-8")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f"[core]\n\texcludesFile = {ignore_file}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    (checkout / ".envrc").write_text("export TOKEN=local\n", encoding="utf-8")

    ownership = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.PROVEN_MANAGED
    assert ownership.may_destroy is True
    assert ownership.reason is None
    assert ownership.blocking_paths == ()


def test_git_url_rewrite_uses_one_observed_origin_representation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    _git("commit", "--allow-empty", "-m", "initial", cwd=upstream)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f'[url "file://{upstream}"]\n\tinsteadOf = fixture:\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    owner = show_runtime._ManagedPublishedBytesOwner()
    checkout = tmp_path / "checkout"
    staged = owner.create_staging(checkout, repo="fixture:", ref="main")
    _git("init", "-b", "main", cwd=checkout)
    _git("remote", "add", "origin", "fixture:", cwd=checkout)
    _git("commit", "--allow-empty", "-m", "checkout", cwd=checkout)
    revision = _git("rev-parse", "HEAD", cwd=checkout)
    assert owner.record_github_checkout(
        staged,
        git=["git"],
        repo="fixture:",
        ref="main",
        revision=revision,
    )
    record_path = checkout / "avibe-managed-checkout.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    ownership = owner.inspect_github_checkout(
        checkout,
        git=["git"],
        repo="fixture:",
        ref="main",
    )

    assert record["origin"] == f"file://{upstream}"
    assert ownership.verdict is show_runtime._ManagedBytesVerdict.PROVEN_MANAGED
    assert ownership.may_destroy is True


def test_relative_local_repo_is_resolved_before_staging_changes_git_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    (upstream / "package.json").write_text('{"name":"runtime"}\n', encoding="utf-8")
    _git("add", "package.json", cwd=upstream)
    _git("commit", "-m", "runtime", cwd=upstream)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    manager = show_runtime.ShowRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        runtime_source="github-source",
        github_repo="../upstream",
        github_ref="main",
    )
    monkeypatch.setattr(show_runtime, "_resolve_node_command", lambda: ["/usr/bin/node"])
    real_resolve = show_runtime._resolve_command
    monkeypatch.setattr(
        show_runtime,
        "_resolve_command",
        lambda name: ["/usr/bin/npm"] if name == "npm" else real_resolve(name),
    )

    def fake_build(
        staged: show_runtime._ManagedBytesOwnership,
        _npm: list[str],
        node: list[str],
        revision: str,
    ) -> list[str]:
        cli = staged.path / "packages" / "runtime" / "dist" / "cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("runtime\n", encoding="utf-8")
        manager._write_github_build_marker(staged, revision)
        return [*node, str(cli)]

    monkeypatch.setattr(manager, "_build_github_staging", fake_build)

    attempt = manager._install_github_runtime(force=True)

    assert attempt.command
    source_dir = manager._github_source_dir()
    record = json.loads((source_dir / "avibe-managed-checkout.json").read_text(encoding="utf-8"))
    assert record["repo"] == "../upstream"
    assert record["origin"] == str(upstream.resolve())


@pytest.mark.parametrize(
    "change",
    [
        "modified",
        "staged",
        "deleted",
    ],
)
def test_tracked_content_changes_block_deletion_without_changing_ownership(
    tmp_path: Path,
    change: str,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    if change == "deleted":
        (checkout / "runtime.js").unlink()
    else:
        (checkout / "runtime.js").write_text("locally edited\n", encoding="utf-8")
        if change == "staged":
            _git("add", "runtime.js", cwd=checkout)

    ownership = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.PROVEN_MANAGED
    assert ownership.may_destroy is False
    assert ownership.reason == "runtime_github_source_dirty"
    assert ownership.blocking_paths == ("runtime.js",)


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ("record", "runtime_github_source_record_mismatch"),
        ("origin", "runtime_github_source_origin_changed"),
        ("revision", "runtime_github_source_revision_changed"),
    ],
)
def test_conflicting_checkout_evidence_proves_foreign(
    tmp_path: Path,
    change: str,
    expected_reason: str,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    if change == "record":
        record_path = checkout / "avibe-managed-checkout.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["ref"] = "other"
        record_path.write_text(json.dumps(record), encoding="utf-8")
    elif change == "origin":
        _git("remote", "set-url", "origin", "https://example.invalid/foreign.git", cwd=checkout)
    else:
        (checkout / "runtime.js").write_text("new revision\n", encoding="utf-8")
        _git("commit", "-am", "different revision", cwd=checkout)

    ownership = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.PROVEN_FOREIGN
    assert ownership.may_destroy is False
    assert ownership.reason == expected_reason


def test_unreadable_ownership_record_is_undetermined(tmp_path: Path) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    record_path = checkout / "avibe-managed-checkout.json"
    record_path.write_text("not json\n", encoding="utf-8")

    ownership = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.UNDETERMINED
    assert ownership.may_destroy is False
    assert ownership.reason == "runtime_github_source_ownership_unreadable"


def test_stage_with_unrecorded_git_origin_is_undetermined(tmp_path: Path) -> None:
    owner = show_runtime._ManagedPublishedBytesOwner()
    checkout = tmp_path / "checkout"
    staged = owner.create_staging(checkout, repo="example/repo", ref="main")
    _git("init", "-b", "main", cwd=checkout)
    _git("remote", "add", "origin", "example/repo", cwd=checkout)

    ownership = owner.inspect_github_checkout(
        checkout,
        git=["git"],
        repo="example/repo",
        ref="main",
    )

    assert ownership.verdict is show_runtime._ManagedBytesVerdict.UNDETERMINED
    assert ownership.may_destroy is False
    assert ownership.reason == "runtime_github_source_inspection_failed"


def test_next_install_reclaims_recorded_abandoned_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "main", cwd=upstream)
    (upstream / "package.json").write_text('{"name":"runtime"}\n', encoding="utf-8")
    _git("add", "package.json", cwd=upstream)
    _git("commit", "-m", "runtime", cwd=upstream)
    manager = show_runtime.ShowRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        runtime_source="github-source",
        github_repo=str(upstream),
        github_ref="main",
    )
    source_dir = manager._github_source_dir()
    staging_dir = manager._github_staging_dir(source_dir)
    abandoned = manager._published_bytes_owner.create_staging(
        staging_dir,
        repo=manager.github_repo,
        ref=manager.github_ref,
    )
    _git("init", "-b", "main", cwd=abandoned.path)
    _git("remote", "add", "origin", manager.github_repo, cwd=abandoned.path)
    assert manager._published_bytes_owner.record_github_checkout(
        abandoned,
        git=["git"],
        repo=manager.github_repo,
        ref=manager.github_ref,
        revision=None,
    )
    orphan_marker = abandoned.path / ".git" / "orphan-marker"
    orphan_marker.write_text("old stage\n", encoding="utf-8")
    monkeypatch.setattr(show_runtime, "_resolve_node_command", lambda: ["/usr/bin/node"])
    real_resolve = show_runtime._resolve_command
    monkeypatch.setattr(
        show_runtime,
        "_resolve_command",
        lambda name: ["/usr/bin/npm"] if name == "npm" else real_resolve(name),
    )

    def fake_build(
        staged: show_runtime._ManagedBytesOwnership,
        _npm: list[str],
        node: list[str],
        revision: str,
    ) -> list[str]:
        cli = staged.path / "packages" / "runtime" / "dist" / "cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("runtime\n", encoding="utf-8")
        manager._write_github_build_marker(staged, revision)
        return [*node, str(cli)]

    monkeypatch.setattr(manager, "_build_github_staging", fake_build)

    attempt = manager._install_github_runtime(force=True)

    assert attempt.command
    assert source_dir.exists()
    assert not staging_dir.exists()
    assert not orphan_marker.exists()


def test_unproven_abandoned_stage_is_published_to_status_with_its_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = show_runtime.ShowRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        runtime_source="github-source",
    )
    source_dir = manager._github_source_dir()
    staging_dir = manager._github_staging_dir(source_dir)
    staging_dir.mkdir(parents=True)
    (staging_dir / "avibe-managed-checkout.json").write_text("{\n", encoding="utf-8")
    monkeypatch.setattr(show_runtime, "_resolve_node_command", lambda: ["/usr/bin/node"])

    github_status = manager.status()["github_source"]

    assert github_status["path"] == str(staging_dir)
    assert github_status["ownership"] == "undetermined"
    assert github_status["destruction_safe"] is False
    assert github_status["reason"] == "runtime_github_source_ownership_unreadable"


def test_publish_refusal_never_deletes_managed_checkout_with_tracked_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    (checkout / "runtime.js").write_text("locally edited\n", encoding="utf-8")
    current = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)
    staged = owner.create_staging(
        tmp_path / "replacement",
        repo=repo,
        ref=ref,
    )
    monkeypatch.setattr(
        show_runtime.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(AssertionError("refusal must not delete")),
    )

    refusal = owner.publish(staged, checkout, current)

    assert refusal == "runtime_github_source_dirty"
    assert checkout.exists()
    assert staged.path.exists()


def test_publish_reports_but_does_not_gate_local_additions(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    _git("remote", "add", "backup", "https://example.invalid/backup.git", cwd=checkout)
    custom_hook = checkout / ".git" / "hooks" / "pre-commit"
    custom_hook.write_text("#!/bin/sh\n", encoding="utf-8")
    (checkout / ".DS_Store").write_text("finder\n", encoding="utf-8")
    sample_hooks = tuple((checkout / ".git" / "hooks").glob("*.sample"))
    assert len(sample_hooks) == 14

    current = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)
    staged = owner.create_staging(
        tmp_path / "replacement",
        repo=repo,
        ref=ref,
    )
    (staged.path / "replacement").write_text("complete\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger=show_runtime.__name__):
        refusal = owner.publish(staged, checkout, current)

    assert current.verdict is show_runtime._ManagedBytesVerdict.PROVEN_MANAGED
    assert current.may_destroy is True
    assert current.blocking_paths == ()
    assert refusal is None
    assert (checkout / "replacement").read_text(encoding="utf-8") == "complete\n"
    assert not (checkout / ".DS_Store").exists()
    assert "local additions" in caplog.text
    assert "checkout-local Git metadata" in caplog.text


def test_failed_publish_window_leaves_absence_instead_of_partial_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner, checkout, repo, ref = _recorded_github_checkout(tmp_path)
    current = owner.inspect_github_checkout(checkout, git=["git"], repo=repo, ref=ref)
    staged = owner.create_staging(
        tmp_path / "replacement",
        repo=repo,
        ref=ref,
    )
    (staged.path / "complete").write_text("new tree\n", encoding="utf-8")
    real_rename = Path.rename

    def interrupt_publish(path: Path, target: Path) -> Path:
        if path == staged.path:
            raise OSError("interrupted before rename")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", interrupt_publish)

    refusal = owner.publish(staged, checkout, current)

    assert refusal == "runtime_github_source_update_failed"
    assert not checkout.exists()
    assert staged.path.exists()
    assert (staged.path / "complete").read_text(encoding="utf-8") == "new tree\n"


def test_replaced_staging_directory_cannot_reach_destructive_install_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path / "runtime")
    staged = manager._published_bytes_owner.create_staging(
        tmp_path / "replacement",
        repo=manager.github_repo,
        ref=manager.github_ref,
    )
    moved = tmp_path / "original-stage"
    staged.path.rename(moved)
    staged.path.mkdir()
    victim = staged.path / "user-file"
    victim.write_text("keep\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        manager,
        "_run_install_command",
        lambda command, **_kwargs: commands.append(command) or True,
    )

    assert manager._clone_github_staging(staged, ["git"]) is None
    assert manager._build_github_staging(staged, ["npm"], ["node"], "revision") is None
    assert commands == []
    assert victim.read_text(encoding="utf-8") == "keep\n"
