"""Source ownership and invocation-local reuse for the live authority checker."""

import ast
from collections import Counter
import json
from pathlib import Path
import subprocess

import pytest

from scripts.check_model_hub_authorities import (
    AuthorityInput,
    _python_importers,
    _python_literal_annotation,
    _python_string_assignment,
    _python_test_literals,
    check,
)


_FIXTURE_VERSION = 1


@pytest.fixture
def source_repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _registry(root):
    registry = {
        "contract_version": _FIXTURE_VERSION,
        "input_generation": {"mode": "same_run_live_files"},
        "authority_table_discovery": {"files": []},
        "entries": [],
        "decision_tables": [],
        "ownership_checks": [{
            "id": "ownership", "kind": "python_importers_have_exactly_one_lane",
            "module": "pkg.owner", "lane_table_file": "lanes.md",
            "lane_table_heading": "Implementation lanes",
        }],
        "repo_absence_checks": [{
            "id": "retired", "term_parts": ["retired", "fixture"],
            "scope_globs": ["**/*.py", "**/*.md", "**/*.json", "**/*.ts", "**/*.tsx"],
        }],
    }
    _write(root, "lanes.md", "Implementation lanes\n| Lane | Files |\n| --- | --- |\n")
    _write(root, "docs/plans/model-hub-contracts/mirror-registry.json", json.dumps(registry))
    return registry


def test_discovery_preserves_live_source_ownership_and_ignores_artifacts(source_repo):
    root = source_repo
    artifacts = [".venv", "build", "dist", "node_modules", ".worktrees"]
    _write(root, ".gitignore", "\n".join(f"{directory}/" for directory in artifacts))
    source_names = {"root.py", "new_lane/new.py", "odd name\nconsumer.py", "build/tracked.py"}
    for name in source_names:
        _write(root, name, "from pkg.owner import value\n")
    subprocess.run(["git", "-C", str(root), "add", "-f", "build/tracked.py"], check=True)
    for directory in artifacts:
        _write(root, f"{directory}/copied.py", "from pkg.owner import value\n")
    _write(root, "invalid.py", "this is not Python !!!")
    deleted = _write(root, "deleted.py", "from pkg.owner import value\n")
    subprocess.run(["git", "-C", str(root), "add", "deleted.py"], check=True)
    deleted.unlink()

    source = AuthorityInput(root)
    assert {p.relative_to(root).as_posix() for p in source.glob("**/*.py")} == source_names | {"invalid.py"}
    assert _python_importers(source, {"module": "pkg.owner"}) == source_names
    assert _python_importers(source, {
        "module": "pkg.owner", "exclude_globs": ["new_lane/**"],
    }) == source_names - {"new_lane/new.py"}


def test_all_registry_globs_use_the_checkout_boundary(source_repo, monkeypatch):
    root = source_repo
    registry = _registry(root)
    registry["contract_version_closure"] = {"literal_globs": ["**/*.py"]}
    registry["schema_absence_checks"] = [{
        "id": "schema", "source_file": "normative.md", "start_marker": "Forbidden:",
        "scope_globs": ["**/*.schema.json"],
    }]
    registry["api_boundary_only_errors"] = [{
        "value": "boundary_only", "contract_file": "normative.md",
        "negative_test_file": "consumer.py", "negative_test": "test_boundary",
        "forbidden_ui_globs": ["**/*.tsx"],
    }]
    _write(root, "docs/plans/model-hub-contracts/mirror-registry.json", json.dumps(registry))
    _write(root, "normative.md", "Forbidden: `retired_field`\n\nboundary_only\n")
    _write(root, "input.schema.json", '{"properties": {"kept": {"type": "string"}}}')
    _write(root, "consumer.py", f"CONTRACT_VERSION = {_FIXTURE_VERSION}\ndef test_boundary():\n    assert 'boundary_only'\n")
    _write(root, ".gitignore", ".venv/\n")
    _write(root, ".venv/copy.py", "from pkg.owner import value\n")
    retired = "-".join(registry["repo_absence_checks"][0]["term_parts"])
    for suffix in ("py", "md", "json", "ts", "tsx"):
        _write(root, f".venv/copy.{suffix}", json.dumps(retired))

    original_glob = Path.glob

    def no_unbounded_root_glob(path, pattern):
        assert path != root, f"unbounded registry scan: {pattern}"
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", no_unbounded_root_glob)
    baseline = check(root)
    assert baseline["ok"], baseline["findings"]
    for suffix in ("py", "md", "json", "ts", "tsx"):
        _write(root, f"future_lane/live.{suffix}", json.dumps(retired))
    result = check(root)
    assert not result["ok"]
    assert {finding["file"] for finding in result["findings"]} == {
        f"future_lane/live.{suffix}" for suffix in ("py", "md", "json", "ts", "tsx")
    }
    assert result["input_fingerprint"] != baseline["input_fingerprint"]


def test_new_checks_observe_tracked_edits_and_new_importers(source_repo):
    root = source_repo
    _registry(root)
    tracked = _write(root, "consumer.py", "value = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "consumer.py"], check=True)
    baseline = check(root)
    assert baseline["ok"], baseline["findings"]

    tracked.write_text("from pkg.owner import value\n", encoding="utf-8")
    _write(root, "new_lane/untracked.py", "from pkg.owner import value\n")
    changed = check(root)
    assert not changed["ok"]
    assert changed["input_fingerprint"] != baseline["input_fingerprint"]
    assert changed["findings"] == [
        {"kind": "ownership_cardinality", "domain": "ownership", "path": name, "owners": 0}
        for name in ("consumer.py", "new_lane/untracked.py")
    ]


def test_reuses_first_read_and_consumer_tree_only_within_one_input(source_repo, monkeypatch):
    root = source_repo
    original_text = (
        "from pkg.owner import value\n"
        "VERSION = 'first'\n"
        "class Input:\n    reason: Literal['first']\n"
        "def test_example():\n    assert value == 'first'\n"
    )
    path = _write(root, "consumer.py", original_text)
    _write(root, "bulk.py", "from pkg.owner import value\n")
    reads, parses = Counter(), Counter()
    original_read, original_parse = Path.read_bytes, ast.parse

    def read(file):
        reads[file] += 1
        return original_read(file)

    def parse(payload, filename="<unknown>", *args, **kwargs):
        parses[filename] += 1
        return original_parse(payload, filename, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(ast, "parse", parse)
    source = AuthorityInput(root)
    assignment = {"file": "consumer.py", "name": "VERSION"}
    assert source.text("consumer.py") == original_text
    assert _python_string_assignment(source, assignment) == {"first"}
    assert _python_literal_annotation(source, {
        "file": "consumer.py", "class": "Input", "field": "reason",
    }) == {"first"}
    assert _python_test_literals(source, "consumer.py", "test_example") == {"first"}
    assert _python_importers(source, {"module": "pkg.owner"}) == {"consumer.py", "bulk.py"}
    assert reads[path] == parses["consumer.py"] == 1
    assert set(source._trees) == {"consumer.py"}
    fingerprint = source.fingerprint()

    path.write_text(original_text.replace("first", "second"), encoding="utf-8")
    assert source.text("consumer.py") == original_text
    assert _python_string_assignment(source, assignment) == {"first"}
    assert source.fingerprint() == fingerprint
    fresh = AuthorityInput(root)
    assert _python_string_assignment(fresh, assignment) == {"second"}
    assert reads[path] == parses["consumer.py"] == 2
    assert fresh.fingerprint() != fingerprint


def test_json_mutations_do_not_change_invocation_input(source_repo):
    _write(source_repo, "input.json", '{"values": [1]}')
    source = AuthorityInput(source_repo)
    payload = source.json("input.json")
    fingerprint = source.fingerprint()
    payload["values"].append(2)
    assert source.json("input.json") == {"values": [1]}
    assert source.fingerprint() == fingerprint


def test_source_discovery_is_reused_only_within_one_input(source_repo, monkeypatch):
    original_git = AuthorityInput._git
    queries = []

    def git(source, *args):
        queries.append(args)
        return original_git(source, *args)

    monkeypatch.setattr(AuthorityInput, "_git", git)
    source = AuthorityInput(source_repo)
    assert source.glob("**/*.py") == ()
    new_path = _write(source_repo, "new.py", "value = 1\n")
    assert source.glob("**/*.py") == ()
    assert sum(args[0] == "ls-files" for args in queries) == 1
    assert AuthorityInput(source_repo).glob("**/*.py") == (new_path,)
    assert sum(args[0] == "ls-files" for args in queries) == 2


def test_git_enumeration_failure_never_becomes_a_passing_check(source_repo, monkeypatch):
    _registry(source_repo)

    def unavailable(*args):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(AuthorityInput, "_git", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        check(source_repo)


def test_source_root_must_be_the_checkout_not_a_parent_repository_subdirectory(source_repo):
    child = source_repo / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="checkout root"):
        AuthorityInput(child).glob("**/*.py")


def test_archive_without_git_cannot_silently_pass(tmp_path):
    _registry(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        check(tmp_path)


@pytest.mark.parametrize("relative", ["../outside.py", "escape.py"])
def test_authority_inputs_cannot_escape_checkout(source_repo, relative):
    outside = _write(source_repo.parent, "outside.py", "value = 1\n")
    (source_repo / "escape.py").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes checkout"):
        AuthorityInput(source_repo).bytes(relative)
    with pytest.raises(ValueError, match="escapes checkout"):
        AuthorityInput(source_repo).glob("**/*.py")
