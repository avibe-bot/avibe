"""The desktop build caches: one trusted writer, exact reads, and a read-only keep-alive.

A release restores what the master warm-up saved. These tests hold the three
properties that make that safe and worth having: only the warm-up writes, a
build uses an entry only on an exact key hit, and the keep-alive can refresh an
entry without being able to change one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MAC_TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin")
RUST_PATHS = ("desktop/target/release/.fingerprint", "desktop/target/release/build", "desktop/target/release/deps")


def workflow(name):
    document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
    document["on"] = document.pop(True) if True in document else document["on"]
    return document


def package_step(name):
    return next(item for item in workflow("desktop-package.yml")["jobs"]["package"]["steps"] if item.get("name") == name)


def keepalive_steps():
    return workflow("desktop-cache-keepalive.yml")["jobs"]["touch"]["steps"]


def keepalive_step(name):
    return next(item for item in keepalive_steps() if item.get("name") == name)


def paths(item):
    return item["with"]["path"].split()


def run_step(script, tmp_path, *, gh, env):
    """Run a workflow step's shell text against a recorded `gh`, as the runner's bash would."""
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    calls = tmp_path / "gh-calls.jsonl"
    (binaries / "gh").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(args) + '\\n')\n"
        "answers = json.loads(os.environ['GH_ANSWERS'])\n"
        "sys.stdout.write(answers.get(args[args.index('--json') + 1], {}).get(args[args.index('--key') + 1], ''))\n",
        encoding="utf-8",
    )
    (binaries / "gh").chmod(0o755)
    (binaries / "sleep").write_text("#!/bin/sh\n", encoding="utf-8")
    (binaries / "sleep").chmod(0o755)
    output, summary = tmp_path / "output", tmp_path / "summary"
    result = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True, text=True, timeout=30,
        env={"PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}", "GITHUB_REPOSITORY": "avibe-bot/avibe",
             "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary), "GH_ANSWERS": json.dumps(gh), **env},
    )
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return result.stdout, (output.read_text() if output.exists() else ""), (summary.read_text() if summary.exists() else ""), recorded


def test_only_the_master_warmup_saves_and_every_build_restores_an_exact_key():
    """A release must never write a cache and never build on an entry for other inputs.

    A save step that a tagged or manual run can reach lets signed-build output
    into the entries later releases read; a prefix fallback lets a release
    compile against another lockfile's output.
    """
    steps = workflow("desktop-package.yml")["jobs"]["package"]["steps"]
    caches = [item for item in steps if str(item.get("uses", "")).startswith("actions/cache")]
    saves = [item for item in caches if item["uses"].startswith("actions/cache/save@")]
    restores = [item for item in caches if item["uses"].startswith("actions/cache/restore@")]
    assert {item["name"] for item in saves} == {"Save prepared Cua Driver", "Save Rust build cache"}
    for item in saves:
        assert "!inputs.release_tag" in item["if"] and "env.DESKTOP_CACHE_WARMUP == 'true'" in item["if"]
    assert all("restore-keys" not in item["with"] for item in restores)
    # The warm-up is a master push or the default-branch schedule, so its entries are master's.
    triggers = workflow("desktop-package.yml")["on"]
    assert triggers["push"]["branches"] == ["master"] and "tags" not in triggers["push"]
    warmup = workflow("desktop-package.yml")["jobs"]["package"]["env"]["DESKTOP_CACHE_WARMUP"]
    assert warmup == "${{ !inputs.release_tag && (github.event_name == 'push' || github.event_name == 'schedule') }}"
    # Restore and save name the same paths, or the saved entry is not the one a build looks up.
    for restore, save in (("Restore Rust build cache", "Save Rust build cache"),
                          ("Restore prepared Cua Driver", "Save prepared Cua Driver")):
        assert paths(package_step(restore)) == paths(package_step(save))
        assert package_step(save)["with"]["key"] == f"${{{{ steps.{package_step(restore)['id']}.outputs.cache-primary-key }}}}"


def test_an_inexact_rust_cache_match_is_removed_before_anything_compiles(tmp_path):
    """The cache service can answer a key with an entry whose key only starts with it."""
    steps = workflow("desktop-package.yml")["jobs"]["package"]["steps"]
    restore, discard = package_step("Restore Rust build cache"), package_step("Discard an inexact Rust cache match")
    assert steps.index(discard) == steps.index(restore) + 1
    assert discard["if"] == ("steps.rust-cache.outputs.cache-hit != 'true' "
                             "&& steps.rust-cache.outputs.cache-matched-key != ''")
    assert set(RUST_PATHS) <= set(paths(restore))
    for path in RUST_PATHS:
        (tmp_path / path).mkdir(parents=True)
        (tmp_path / path / "stale").write_text("x")
    subprocess.run(["bash", "-e", "-c", discard["run"]], cwd=tmp_path, check=True, timeout=10)
    assert not any((tmp_path / path).exists() for path in RUST_PATHS)


@pytest.mark.parametrize(("tag", "runner_os", "rust", "driver", "warned"), [
    ("gh-v3.2.6rc7", "macOS", "", "true", ["Rust build"]),
    ("v3.2.6", "macOS", "true", "false", ["prepared Cua Driver"]),
    ("v3.2.6", "macOS", "true", "true", []),
    ("gh-v3.2.6rc7", "Windows", "", "", ["Rust build"]),
    # The warm-up is the writer: its own miss is how an entry gets saved.
    ("", "macOS", "", "", []),
])
def test_a_release_cache_miss_is_a_warning_on_the_run(tmp_path, tag, runner_os, rust, driver, warned):
    report = package_step("Report build cache state")
    stdout, _, summary, _ = run_step(report["run"], tmp_path, gh={}, env={
        "RELEASE_TAG": tag, "TARGET": "x86_64-apple-darwin", "RUNNER_OS": runner_os,
        "RUST_CACHE_HIT": rust, "DRIVER_CACHE_HIT": driver,
    })
    warnings = [line for line in stdout.splitlines() if line.startswith("::warning ")]
    assert len(warnings) == len(warned)
    for line, name in zip(warnings, warned):
        assert f"no {name} cache for {tag}" in line
    assert ("| prepared Cua Driver |" in summary) == (runner_os == "macOS")
    steps = workflow("desktop-package.yml")["jobs"]["package"]["steps"]
    assert steps.index(package_step("Restore prepared Cua Driver")) < steps.index(report)
    assert report["env"]["RUST_CACHE_HIT"] == "${{ steps.rust-cache.outputs.cache-hit }}"
    assert report["env"]["DRIVER_CACHE_HIT"] == "${{ steps.cua-driver-cache.outputs.cache-hit }}"


def test_keepalive_can_read_master_entries_and_change_nothing():
    """The keep-alive runs on every landing, so it must be unable to alter release input."""
    document = workflow("desktop-cache-keepalive.yml")
    assert set(document["on"]) == {"push", "workflow_dispatch"}
    assert document["on"]["push"] == {"branches": ["master"]}
    assert document["permissions"] == {"actions": "read"}
    assert document["concurrency"] == {"group": "desktop-cache-keepalive", "cancel-in-progress": False}
    assert set(document["jobs"]) == {"touch"}
    job = document["jobs"]["touch"]
    assert job["if"] == "github.ref == 'refs/heads/master'"
    assert job["runs-on"] == "ubuntu-latest" and "environment" not in job and "permissions" not in job
    text = (ROOT / ".github/workflows/desktop-cache-keepalive.yml").read_text(encoding="utf-8")
    assert "secrets." not in text and "uses: ./" not in text
    pin = package_step("Restore Rust build cache")["uses"]
    actions = [item for item in job["steps"] if "uses" in item]
    assert len(actions) == 4
    for item in actions:
        # No checkout and no save: the same pinned restore action, asked only whether the entry exists.
        assert item["uses"] == pin and item["with"]["lookup-only"] is True
        assert set(item["with"]) == {"lookup-only", "path", "key"}
        assert item["with"]["key"] == f"${{{{ steps.keys.outputs.{item['id']} }}}}"
        assert item["if"] == f"steps.keys.outputs.{item['id']} != ''"


@pytest.mark.parametrize("target", MAC_TARGETS)
def test_keepalive_looks_up_exactly_what_the_warmup_saves(target):
    """A cache entry is matched by key and by a hash of its path list; a different list finds nothing."""
    arch = target.split("-")[0]
    driver = keepalive_step(f"Look up the prepared Cua Driver ({target})")
    rust = keepalive_step(f"Look up the Rust build cache ({target})")
    assert (driver["id"], rust["id"]) == (f"driver_{arch}", f"rust_{arch}")
    saved_driver = package_step("Save prepared Cua Driver")["with"]["path"].replace("${{ matrix.target }}", target)
    assert paths(driver) == saved_driver.split()
    assert paths(rust) == paths(package_step("Save Rust build cache"))
    # The prefixes the keep-alive lists are the fixed heads of the keys the warm-up computes.
    find = keepalive_step("Find the newest master entry of each macOS cache")["run"]
    for name, restore, os_name in ((f"driver_{arch}", "Restore prepared Cua Driver", "macOS"),
                                   (f"rust_{arch}", "Restore Rust build cache", "macOS")):
        prefix = re.search(rf"find_entry {name} (\S+)\n", find).group(1)
        key = (package_step(restore)["with"]["key"]
               .replace("${{ runner.os }}", os_name).replace("${{ matrix.target }}", target))
        assert key.startswith(prefix + "${{") and target in prefix


VALID = "cua-driver-x86_64-apple-darwin-97fbebad46494f21-default-target-" + "ac0b" * 16


@pytest.mark.parametrize(("listed", "key", "seen", "refused"), [
    (f"{VALID}\t2026-11-11T06:06:07.81906Z\n", VALID, "2026-11-11T06:06:07.81906Z", False),
    ("", "", "", False),
    # Shell text, an output-file injection, and a key from another target's namespace.
    ("cua-driver-x86_64-apple-darwin-$(touch pwned)\t2026-11-11T06:06:07Z\n", "", "", True),
    ("cua-driver-x86_64-apple-darwin-a\\nrust_x86_64=evil\t2026-11-11T06:06:07Z\n", "", "", True),
    ("cua-driver-aarch64-apple-darwin-97fbebad46494f21\t2026-11-11T06:06:07Z\n", "", "", True),
    (f"{VALID}\t$(touch pwned)\n", "", "", True),
])
def test_keepalive_passes_on_only_a_plain_key_from_the_target_prefix(tmp_path, listed, key, seen, refused):
    """A cache key is data another run chose; it may reach an action input and nothing else."""
    find = keepalive_step("Find the newest master entry of each macOS cache")
    stdout, output, _, calls = run_step(
        find["run"], tmp_path, env={},
        gh={"key,lastAccessedAt": {"cua-driver-x86_64-apple-darwin-": listed}},
    )
    outputs = dict(line.split("=", 1) for line in output.splitlines())
    assert outputs == {
        "driver_aarch64": "", "driver_aarch64_seen": "", "driver_x86_64": key, "driver_x86_64_seen": seen,
        "rust_aarch64": "", "rust_aarch64_seen": "", "rust_x86_64": "", "rust_x86_64_seen": "",
    }
    assert ("::warning title=Desktop cache key refused::" in stdout) == refused
    assert not (tmp_path / "pwned").exists()
    assert [call[call.index("--key") + 1] for call in calls] == [
        "cua-driver-aarch64-apple-darwin-", "cua-driver-x86_64-apple-darwin-",
        "macOS-aarch64-apple-darwin-desktop-package-", "macOS-x86_64-apple-darwin-desktop-package-",
    ]
    for call in calls:
        # The newest entry of the default branch only: another ref's entry is never release input.
        assert call[:2] == ["cache", "list"] and call[call.index("--ref") + 1] == "refs/heads/master"
        assert call[call.index("--sort") + 1] == "created_at" and call[call.index("--order") + 1] == "desc"
        assert call[call.index("--limit") + 1] == "1"


def test_keepalive_reports_every_entry_a_release_could_not_rely_on(tmp_path):
    """Missing, unrestorable, and not-refreshed entries are each a warning before a release meets them."""
    report = keepalive_step("Report what a release would restore")
    kept, stale = VALID, VALID.replace("x86_64", "aarch64")
    entries = "\n".join([
        "prepared Cua Driver|aarch64-apple-darwin|" + stale + "|2026-11-11T06:06:07Z|true",
        "prepared Cua Driver|x86_64-apple-darwin|" + kept + "|2026-11-11T06:06:07Z|true",
        "Rust build|aarch64-apple-darwin|||",
        "Rust build|x86_64-apple-darwin|macOS-x86_64-apple-darwin-desktop-package-1|2026-11-11T06:06:07Z|",
    ]) + "\n"
    stdout, _, summary, _ = run_step(report["run"], tmp_path, env={"ENTRIES": entries}, gh={"lastAccessedAt": {
        kept: "2026-11-11T08:00:00Z", stale: "2026-11-11T06:06:07Z",
    }})
    warnings = [line.split("::")[1] for line in stdout.splitlines() if line.startswith("::warning ")]
    assert warnings == ["warning title=Desktop cache not refreshed", "warning title=Desktop cache missing",
                        "warning title=Desktop cache unreadable"]
    states = [row.split(" | ")[2] for row in summary.splitlines()[2:]]
    assert states == ["not-refreshed", "kept", "missing", "unreadable"]
    # Every lookup's result and the access time it started from reach the report.
    for line in report["env"]["ENTRIES"].splitlines():
        _, target, key, seen, hit = line.split("|")
        name = re.fullmatch(r"\$\{\{ steps\.keys\.outputs\.(\w+) \}\}", key).group(1)
        assert name.endswith(target.split("-")[0])
        assert seen == f"${{{{ steps.keys.outputs.{name}_seen }}}}"
        assert hit == f"${{{{ steps.{name}.outputs.cache-hit }}}}"
    assert len(report["env"]["ENTRIES"].splitlines()) == 4
