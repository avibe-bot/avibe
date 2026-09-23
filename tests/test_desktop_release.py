from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts import desktop_release as release
from tests.test_desktop_runtime_bundle_builder import builder


ROOT = Path(__file__).resolve().parents[1]
TAG = "gh-v3.1.2rc1"
VERSION = "3.1.2-rc.1"
SOURCE = "a" * 40


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def step(name, job="package", file="desktop-package.yml"):
    return next(item for item in workflow(file)["jobs"][job]["steps"] if item.get("name") == name)


def assemble_assets(root, source=SOURCE):
    """Use the real Runtime archive writer and desktop producer, then merge downloads."""
    directory = root / "desktop-dist"
    directory.mkdir()
    for target, (system, arch, suffix) in release.TARGETS.items():
        work = root / target
        payload = work / "payload"
        payload.mkdir(parents=True)
        (payload / "说明.txt").write_text("Runtime fixture 中文", encoding="utf-8")
        runtime = work / "runtime"
        runtime.mkdir()
        archive = runtime / "runtime.zip"
        size, count, tree = builder.create_runtime_zip(payload, archive)
        manifest = {
            "schema_version": 2, "runtime_version": "3.1.2rc1", "os": system, "arch": arch,
            "archive": archive.name, "archive_sha256": release.digest(archive),
            "archive_size": archive.stat().st_size, "unpacked_size": size,
            "entry_count": count, "tree_sha256": tree,
        }
        (runtime / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        installer = work / ("native installer" + suffix)
        installer.write_bytes(f"installer bytes for {target}".encode())
        output = work / "output"
        release.record(version=VERSION, target=target, tag=TAG, source_sha=source,
                       installer=installer, runtime=runtime, output=output,
                       signing=release.signature(target).strip())
        for path in output.iterdir():
            shutil.copyfile(path, directory / path.name)
    return directory


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


@pytest.mark.parametrize("annotated", [False, True])
def test_real_tag_resolution_and_prepare_pin_runtime_before_build(tmp_path, monkeypatch, annotated):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "tagged source")
    source = git(tmp_path, "rev-parse", "HEAD")
    if annotated:
        git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "tag", "-a", TAG, "-m", "test only")
    else:
        git(tmp_path, "tag", TAG)
    monkeypatch.chdir(tmp_path)
    assert release.resolve(TAG) == {"tag": TAG, "source_sha": source, "version": VERSION,
                                    "package_version": "3.1.2rc1"}
    config, environment = tmp_path / "config.json", tmp_path / "env"
    release.prepare(VERSION, TAG, source, config, environment)
    assert environment.read_text().splitlines() == [
        "SETUPTOOLS_SCM_PRETEND_VERSION=3.1.2rc1",
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS=3.1.2rc1",
    ]
    override = json.loads(config.read_text())
    assert override["version"] == VERSION
    assert override["bundle"]["macOS"]["signingIdentity"] == "-"
    assert override["bundle"]["windows"] == {"certificateThumbprint": None, "signCommand": None}
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "different caller branch")
    with pytest.raises(ValueError, match="checkout/version"):
        release.prepare(VERSION, TAG, source, config, environment)
    with pytest.raises(ValueError, match="source SHA"):
        release.resolve(TAG, "b" * 40)


@pytest.mark.parametrize("tag", ["gh-v3.1.2", "gh-v3.1.2-rc1", "gh-v03.1.2rc1", "gh-v3.1.2rc01",
                                 "v3.1.2rc1", "gh-vnext", "gh-v3.1.2rc1\n", "--help", ""])
def test_malformed_test_tags_fail_before_git_or_publication(tag, monkeypatch):
    monkeypatch.setattr(release, "git_output", lambda *_: pytest.fail("invalid tag reached git"))
    with pytest.raises(ValueError, match="canonical"):
        release.resolve(tag)


def test_manual_prepare_retains_optional_signing_and_native_version(tmp_path):
    config, environment = tmp_path / "config.json", tmp_path / "env"
    release.prepare("2.0.0-beta.1+build.4", "", "", config, environment)
    assert json.loads(config.read_text()) == {"version": "2.0.0-beta.1+build.4"}
    assert not environment.exists()
    with pytest.raises(ValueError, match="SemVer"):
        release.prepare("2.0.0-01", "", "", config, environment)


def test_real_assembled_assets_pass_the_release_consumer(tmp_path):
    directory = assemble_assets(tmp_path)
    paths = release.verify(directory, TAG, SOURCE)
    assert len(paths) == len({path.name for path in paths}) == 15
    assert {path.suffix for path in paths} >= {".dmg", ".exe"}


@pytest.mark.parametrize("target", release.TARGETS)
def test_workflow_record_command_feeds_the_release_consumer(tmp_path, target):
    directory = assemble_assets(tmp_path)
    work = tmp_path / target
    scripts = work / "scripts"
    scripts.mkdir()
    for name in ("desktop_release.py", "release_package_version.py", "github_release.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    runtime = work / "desktop/src-tauri/resources/runtime"
    runtime.parent.mkdir(parents=True)
    shutil.copytree(work / "runtime", runtime)
    matrix = next(item for item in workflow("desktop-package.yml")["jobs"]["package"]["strategy"]["matrix"]["include"]
                  if item["target"] == target)
    installer = work / matrix["artifact_glob"].replace("*", "Avibe_fixture")
    installer.parent.mkdir(parents=True)
    shutil.copyfile(work / ("native installer" + release.TARGETS[target][2]), installer)
    binaries = work / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    (binaries / "git").write_text(f"#!/bin/sh\nprintf '%s\\n' '{SOURCE}'\n")
    (binaries / "git").chmod(0o755)
    command = step("Record artifact hashes")["run"].replace("${{ matrix.artifact_glob }}", matrix["artifact_glob"])
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=work,
                            env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                                 "AVIBE_DESKTOP_VERSION": VERSION, "RELEASE_TAG": TAG, "TARGET": target,
                                 "RUNNER_OS": "macOS" if "darwin" in target else "Windows",
                                 "signature_state": "app-adhoc"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    for path in (work / "desktop-package").iterdir():
        shutil.copyfile(path, directory / path.name)
    assert len(release.verify(directory, TAG, SOURCE)) == 15


@pytest.mark.parametrize("mutation", ["missing-target", "extra-file", "installer-bytes", "runtime-version",
                                     "runtime-target", "source-sha", "signature", "missing-hashes"])
def test_consumer_rejects_incomplete_or_mismatched_release(tmp_path, mutation):
    directory = assemble_assets(tmp_path)
    target = next(iter(release.TARGETS))
    names = release.asset_names(VERSION, target)
    if mutation == "missing-target":
        for name in names:
            (directory / name).unlink()
    elif mutation == "extra-file":
        (directory / "SIGNATURE").write_text("ambiguous metadata")
    elif mutation == "missing-hashes":
        (directory / names[4]).unlink()
    elif mutation == "installer-bytes":
        (directory / names[0]).write_bytes(b"changed installer")
    else:
        if mutation == "signature":
            (directory / names[3]).write_text("app-identity-signed\n")
        else:
            name = names[2] if mutation == "source-sha" else names[1]
            path = directory / name
            data = json.loads(path.read_text())
            data[{"source-sha": "source_sha", "runtime-version": "runtime_version",
                  "runtime-target": "arch"}[mutation]] = "incorrect"
            path.write_text(json.dumps(data))
        # Matching hashes alone cannot substitute for the semantic contract.
        (directory / names[4]).write_text("".join(
            f"{release.digest(directory / name)}  {name}\n" for name in sorted(names[:4])
        ))
    with pytest.raises(ValueError):
        release.verify(directory, TAG, SOURCE)


@pytest.mark.parametrize("mutation", ["archive", "version", "signing"])
def test_producer_rejects_incorrect_runtime_or_signing(tmp_path, mutation):
    assemble_assets(tmp_path)
    target = next(iter(release.TARGETS))
    work = tmp_path / target
    if mutation == "archive":
        (work / "runtime/runtime.zip").write_bytes(b"tampered archive")
    if mutation == "version":
        path = work / "runtime/runtime-manifest.json"
        data = json.loads(path.read_text())
        data["runtime_version"] = "0.1.0"
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        release.record(version=VERSION, target=target, tag=TAG, source_sha=SOURCE,
                       installer=work / "native installer.dmg", runtime=work / "runtime",
                       output=work / "rejected", signing="identity" if mutation == "signing"
                       else release.signature(target).strip())
    assert not (work / "rejected").exists()


def test_workflow_preserves_manual_path_and_isolates_test_signing():
    package = workflow("desktop-package.yml")
    triggers = package.get("on", package.get(True))
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert "secrets" not in triggers["workflow_call"]
    jobs = workflow("release_ai.yml")["jobs"]
    caller = jobs["desktop-packages"]
    assert caller["uses"] == "./.github/workflows/desktop-package.yml"
    assert "secrets" not in caller
    assert caller["with"]["source_sha"] == "${{ needs.resolve-desktop-release.outputs.source_sha }}"
    build = package["jobs"]["package"]
    steps = build["steps"]
    assert steps[0]["with"]["ref"] == "${{ inputs.source_sha || github.sha }}"
    assert {entry["target"] for entry in build["strategy"]["matrix"]["include"]} == set(release.TARGETS)
    assert steps.index(step("Prepare package version")) < steps.index(step("Build verified private Runtime"))
    credentials = step("Export Apple signing credentials when configured")
    assert credentials["if"] == "runner.os == 'macOS' && inputs.release_tag == ''"
    assert "codesign --force --deep --sign -" in step("Verify macOS app signature matches the signing path")["run"]
    assert "NotSigned" in step("Verify unsigned Windows installer")["run"]
    assert steps[-1]["uses"] == "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"


@pytest.mark.parametrize("configured", [False, True])
def test_manual_apple_export_still_consumes_complete_optional_credentials(tmp_path, configured):
    export = step("Export Apple signing credentials when configured")
    env = {**os.environ, **{key: "" for key in export["env"]},
           "GITHUB_ENV": str(tmp_path / "env"), "TMPDIR": str(tmp_path)}
    if configured:
        env.update({key: "fixture-value" for key in export["env"]})
    result = subprocess.run(["bash", "-e", "-c", export["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    exported = (tmp_path / "env").read_text()
    assert f"apple_signing={'enabled' if configured else 'absent'}" in exported
    if configured:
        key = next(line.split("=", 1)[1] for line in exported.splitlines() if line.startswith("APPLE_API_KEY_PATH="))
        assert Path(key).read_text() == "fixture-value"


@pytest.mark.parametrize("state", ["new", "draft-partial", "published-identical", "published-legacy",
                                  "changed-desktop", "changed-package", "upload-corrupt", "missing-local"])
def test_real_publication_shell_checks_assets_before_finalize(tmp_path, state):
    directory = assemble_assets(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("vibe-show-runtime-node-darwin-arm64.tgz", "show-runtime-manifest.json",
                 "avibe_os-3.1.2rc1.whl", "avibe_os-3.1.2rc1.tar.gz",
                 "avibe_memory-3.1.2rc1-py3-none-any.whl"):
        (dist / name).write_bytes(name.encode())
    remote = tmp_path / "remote"
    remote.mkdir()
    desktop_paths = sorted(directory.iterdir())
    if state in {"published-identical", "changed-desktop", "changed-package"}:
        for path in [*desktop_paths, *dist.iterdir()]:
            shutil.copyfile(path, remote / path.name)
    elif state == "draft-partial":
        shutil.copyfile(desktop_paths[0], remote / desktop_paths[0].name)
    elif state == "published-legacy":
        for path in dist.iterdir():
            shutil.copyfile(path, remote / path.name)
    if state == "changed-desktop":
        (remote / desktop_paths[0].name).write_bytes(b"different published bytes")
    if state == "changed-package":
        (remote / "avibe_os-3.1.2rc1.whl").write_bytes(b"different published wheel")
    if state == "missing-local":
        desktop_paths[0].unlink()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("desktop_release.py", "release_package_version.py", "github_release.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    (tmp_path / "release.md").write_text("# TEST release\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    gh = binaries / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, shutil, sys
args = sys.argv[1:]
root = pathlib.Path.cwd()
remote = root / 'remote'
with (root / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
state_path = root / 'state.json'
state = json.loads(state_path.read_text()) if state_path.exists() else None
if args[0] == 'api':
    if '--jq' in args:
        print('v1.0.0')
    elif '--paginate' in args:
        print(json.dumps([[state]] if state else [[]]))
    elif state:
        print(json.dumps(state))
    else:
        print('HTTP 404', file=sys.stderr)
        sys.exit(1)
elif args[1] == 'create':
    state = dict(tag_name=args[2], draft=True, prerelease=False, body='', html_url='https://example.invalid/release')
elif args[1] == 'edit':
    if '--notes-file' in args:
        state['body'] = pathlib.Path(args[args.index('--notes-file') + 1]).read_text()
    if '--draft=false' in args:
        state['draft'] = False
        state['prerelease'] = '--prerelease' in args
elif args[1] == 'view':
    names = sorted(path.name for path in remote.iterdir())
    print('\\n'.join(names) if '--jq' in args else json.dumps({'assets': [{'name': n} for n in names]}))
elif args[1] == 'download':
    name = args[args.index('--pattern') + 1]
    shutil.copyfile(remote / name, pathlib.Path(args[args.index('--dir') + 1]) / name)
elif args[1] == 'upload':
    assert '--clobber' not in args
    for item in args[args.index('--repo') + 2:]:
        path = pathlib.Path(item)
        assert not (remote / path.name).exists()
        shutil.copyfile(path, remote / path.name)
        if (root / 'corrupt').exists() and path.suffix == '.dmg':
            (remote / path.name).write_bytes(b'corrupt uploaded file')
else:
    raise AssertionError(args)
if state:
    state_path.write_text(json.dumps(state))
''')
    gh.chmod(0o755)
    if state != "new":
        (tmp_path / "state.json").write_text(json.dumps({
            "tag_name": TAG, "draft": not state.startswith("published"), "prerelease": True,
            "body": "prior notes", "html_url": "https://example.invalid/release",
        }))
    if state == "upload-corrupt":
        (tmp_path / "corrupt").touch()
    command = step("Create GitHub-only Release", "release", "release_ai.yml")["run"]
    command = command.replace("${{ steps.tag.outputs.tag }}", TAG)
    command = command.replace("${{ steps.release_type.outputs.prerelease }}", "true")
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=tmp_path,
                            env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                                 "SOURCE_SHA": SOURCE, "GITHUB_REPOSITORY": "avibe-bot/avibe"},
                            capture_output=True, text=True, timeout=30)
    success = state in {"new", "draft-partial", "published-identical"}
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    events_path = tmp_path / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
    finalizations = [args for args in events if "--draft=false" in args]
    assert bool(finalizations) == success
    if success:
        assert {path.name for path in remote.iterdir()} == {path.name for path in [*directory.iterdir(), *dist.iterdir()]}
        assert json.loads((tmp_path / "state.json").read_text())["draft"] is False
        if state == "published-identical":
            assert not any(args[:2] == ["release", "upload"] for args in events)
    elif state != "upload-corrupt":
        assert not any(args[:2] == ["release", "upload"] for args in events)
    else:
        assert json.loads((tmp_path / "state.json").read_text())["draft"] is True


def test_workflow_shell_syntax():
    for file in ("desktop-package.yml", "release_ai.yml"):
        for job in workflow(file)["jobs"].values():
            for item in job.get("steps", []):
                if "run" not in item or item.get("shell") == "pwsh":
                    continue
                command = re.sub(r"\$\{\{.*?\}\}", "fixture", item["run"])
                result = subprocess.run(["bash", "-n"], input=command, text=True, capture_output=True)
                assert result.returncode == 0, (file, item.get("name"), result.stderr)
