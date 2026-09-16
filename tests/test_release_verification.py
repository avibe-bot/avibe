from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import shlex
import subprocess
import sys
import shutil
import threading

import pytest
import yaml

from tests.e2e.github_release_fixture import create_certificate, make_server


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ("release_ai.yml", "build-assets", "Checkout release verification", "${{ github.sha }}"),
    ("publish.yml", "build", "Checkout release automation", "${{ needs.resolve-tag.outputs.workflow_sha }}"),
)


@pytest.mark.parametrize("failure", [None, "missing-wheel", "missing-sdist", "mismatch", "unpublished"])
@pytest.mark.parametrize(
    ("tag", "version"),
    [("v3.1.0", "3.1.0"), ("v3.2.0rc1", "3.2.0rc1"),
     ("gh-v3.2.0-rc1", "3.2.0rc1"), ("gh-v3.2.0.dev01", "3.2.0.dev1")],
)
def test_public_companion_gate_checks_both_exact_assets_without_auth(tmp_path, failure, tag, version):
    workspace = tmp_path / "public release 中文"
    dist = workspace / "dist"
    scripts = workspace / "scripts"
    binaries = tmp_path / "bin"
    runtime_tmp = tmp_path / "runner temp"
    for directory in (dist, scripts, binaries, runtime_tmp):
        directory.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/release_package_version.py", scripts)
    assets = {
        f"avibe_memory-{version}-py3-none-any.whl": b"companion wheel",
        f"avibe_memory-{version}.tar.gz": b"companion sdist",
    }
    for name, data in assets.items():
        (dist / name).write_bytes(data)
    if failure in {"missing-wheel", "missing-sdist"}:
        suffix = ".whl" if failure == "missing-wheel" else ".tar.gz"
        next(dist.glob(f"*{suffix}")).unlink()
    (binaries / "python").symlink_to(sys.executable)
    curl = binaries / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert not any('authorization' in arg.lower() for arg in args)\n"
        "url = args[-1]\n"
        f"assert url.startswith('https://github.com/avibe-bot/avibe/releases/download/{tag}/')\n"
        "with pathlib.Path('fetches.jsonl').open('a') as log: log.write(json.dumps(url) + '\\n')\n"
        f"sys.exit(22) if {failure == 'unpublished'!r} else None\n"
        "asset = url.rsplit('/', 1)[1]\n"
        f"data = {assets!r}[asset]\n"
        f"data = b'wrong' if {failure == 'mismatch'!r} else data\n"
        "pathlib.Path(args[args.index('--output') + 1]).write_bytes(data)\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    command = _step(
        _job("publish.yml", "verify-avibe-memory-release"),
        "Verify public GitHub companion distributions",
    )["run"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runtime_tmp),
            "RELEASE_TAG": tag,
            "GITHUB_REPOSITORY": "avibe-bot/avibe",
        },
        capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    assert not list(runtime_tmp.iterdir())
    if failure is None:
        fetches = [json.loads(line) for line in (workspace / "fetches.jsonl").read_text().splitlines()]
        assert {url.rsplit("/", 1)[1] for url in fetches} == set(assets)
    for path in dist.iterdir():
        assert path.read_bytes() == assets[path.name]


@pytest.mark.parametrize("availability", ["transient-404", "permanent-404", "wrong-bytes"])
def test_public_gate_with_real_curl_retries_availability_but_never_integrity(tmp_path, availability):
    workspace = tmp_path / "published 中文"
    dist = workspace / "dist"
    scripts = workspace / "scripts"
    binaries = tmp_path / "bin"
    temporary = tmp_path / "runner temp"
    for directory in (dist, scripts, binaries, temporary):
        directory.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/release_package_version.py", scripts)
    assets = {
        "avibe_memory-3.1.0-py3-none-any.whl": b"exact wheel bytes",
        "avibe_memory-3.1.0.tar.gz": b"exact sdist bytes",
    }
    for name, data in assets.items():
        (dist / name).write_bytes(data)
    attempts = {}

    class ReleaseHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Host"] == "github.com"
            assert self.headers.get("Authorization") is None
            prefix = "/avibe-bot/avibe/releases/download/v3.1.0/"
            assert self.path.startswith(prefix)
            name = self.path.removeprefix(prefix)
            attempts[name] = attempts.get(name, 0) + 1
            missing = availability == "permanent-404" or (
                availability == "transient-404" and attempts[name] == 1
            )
            body = b"not yet public" if missing else assets[name]
            if availability == "wrong-bytes":
                body = b"incorrect successful download"
            self.send_response(404 if missing else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ca, cert, key = create_certificate(tmp_path / "TLS 中文")
    server = make_server(tmp_path, cert, key, handler=ReleaseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        actual_curl = shutil.which("curl")
        assert actual_curl
        curl = binaries / "curl"
        # Redirect only the test connection, never the requested URL or TLS
        # hostname. Execute actual curl with the workflow's unchanged options.
        curl.write_text(
            "#!/bin/sh\nexec " + shlex.join([
                actual_curl, "--disable", "--noproxy", "*", "--cacert", str(ca),
                "--connect-to", f"github.com:443:127.0.0.1:{server.server_port}",
            ]) + ' "$@"\n', encoding="utf-8",
        )
        curl.chmod(0o755)
        (binaries / "python").symlink_to(sys.executable)
        command = _step(_job("publish.yml", "verify-avibe-memory-release"),
                        "Verify public GitHub companion distributions")["run"]
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
            cwd=workspace,
            env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                 "RUNNER_TEMP": str(temporary), "RELEASE_TAG": "v3.1.0",
                 "GITHUB_REPOSITORY": "avibe-bot/avibe"},
            capture_output=True, text=True, timeout=45,
        )
        assert (result.returncode == 0) is (availability == "transient-404"), result.stderr
        if availability == "transient-404":
            assert attempts == {name: 2 for name in assets}
        else:
            assert attempts == {
                "avibe_memory-3.1.0-py3-none-any.whl": 6 if availability == "permanent-404" else 1,
            }
        assert {p.name: p.read_bytes() for p in dist.iterdir()} == assets
        assert not list(temporary.iterdir())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
@pytest.mark.parametrize("tag", ["v3.2.0-rc1", "v03.02.00"])
def test_noncanonical_official_tags_fail_before_artifact_construction(
    tmp_path, workflow_name, job_name, checkout_name, workflow_ref, tag,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/release_package_version.py", scripts)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)
    command = _step(_job(workflow_name, job_name), "Pin package version to release tag")["run"]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command + "\ntouch artifact-construction-reached"],
        cwd=tmp_path, env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                          "RELEASE_TAG": tag, "GITHUB_ENV": str(tmp_path / "github-env")},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "canonical spelling" in result.stderr
    assert not (tmp_path / "artifact-construction-reached").exists()
    assert not (tmp_path / "github-env").exists()

@pytest.mark.parametrize("workflow_name", ["publish.yml", "release_ai.yml"])
@pytest.mark.parametrize(
    "state",
    ["empty", "identical", "partial", "core-mismatch", "memory-wheel-mismatch",
     "memory-sdist-mismatch", "runtime-mismatch", "missing-wheel", "empty-sdist", "read-failure"],
)
def test_upload_protects_all_existing_bytes_before_any_write(tmp_path, workflow_name, state):
    workspace = tmp_path / "release source 中文"
    dist = workspace / "dist"
    runtime = workspace / ("runtime-artifacts" if workflow_name == "publish.yml" else "dist")
    binaries = tmp_path / "bin"
    temporary = tmp_path / "temp"
    for path in {workspace, dist, runtime, binaries, temporary}:
        path.mkdir(parents=True, exist_ok=True)
    packages = {
        f"{package}-3.1.0{suffix}": f"{package}{suffix}".encode()
        for package in ("avibe_os", "avibe_memory")
        for suffix in ("-py3-none-any.whl", ".tar.gz")
    }
    if workflow_name == "publish.yml":
        # Preserve the optional legacy package upload path too.
        packages["vibe_remote-3.0.14-py3-none-any.whl"] = b"legacy shim"
    runtimes = {
        **{f"vibe-show-runtime-node-{platform}.tgz": platform.encode()
           for platform in ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64")},
        **{f"memory-runtime-1.2.3-{platform}.tar.gz": platform.encode()
           for platform in ("linux-x64", "linux-arm64", "darwin-arm64")},
        "show-runtime-manifest.json": b"show manifest",
        "memory-runtime-manifest.json": b"memory manifest",
    }
    for directory, assets in ((dist, packages), (runtime, runtimes)):
        for name, data in assets.items():
            (directory / name).write_bytes(data)
    all_assets = {**packages, **runtimes}
    existing = dict(all_assets) if state == "identical" else {}
    if state not in {"empty", "identical"}:
        existing = {name: all_assets[name] for name in (
            "avibe_os-3.1.0-py3-none-any.whl", "avibe_memory-3.1.0-py3-none-any.whl",
            "avibe_memory-3.1.0.tar.gz", "show-runtime-manifest.json",
        )}
    mismatch = {
        "core-mismatch": "avibe_os-3.1.0-py3-none-any.whl",
        "memory-wheel-mismatch": "avibe_memory-3.1.0-py3-none-any.whl",
        "memory-sdist-mismatch": "avibe_memory-3.1.0.tar.gz",
        "runtime-mismatch": "show-runtime-manifest.json",
    }.get(state)
    if mismatch:
        existing[mismatch] = b"already published different bytes"
    if state == "missing-wheel":
        (dist / "avibe_memory-3.1.0-py3-none-any.whl").unlink()
    if state == "empty-sdist":
        (dist / "avibe_memory-3.1.0.tar.gz").write_bytes(b"")

    # Only gh and the metadata helper are simulated; run the complete upload
    # shell, including real file comparisons, globs, temp cleanup and ordering.
    gh = binaries / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        f"existing = {existing!r}\n"
        "with pathlib.Path('events.jsonl').open('a') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "if args[1] == 'view':\n"
        f"    sys.exit(1) if {state == 'read-failure'!r} else None\n"
        "    print('\\n'.join(existing))\n"
        "elif args[1] == 'download':\n"
        "    name = args[args.index('--pattern') + 1]\n"
        "    (pathlib.Path(args[args.index('--dir') + 1]) / name).write_bytes(existing[name])\n"
        "elif args[1] == 'upload':\n"
        "    assert '--clobber' not in args\n"
        "    for path in args[args.index('--repo') + 2:]:\n"
        "        assert pathlib.Path(path).name not in existing\n"
        "        assert pathlib.Path(path).is_file()\n"
        "else: raise AssertionError(args)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    python = binaries / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    if workflow_name == "publish.yml":
        command = _step(_job(workflow_name, "build"), "Upload GitHub release assets")["run"]
        command = command.replace("${{ needs.resolve-tag.outputs.tag }}", "v3.1.0")
    else:
        command = _step(_job(workflow_name, "release"), "Create GitHub-only Release")["run"]
        command = command.replace("${{ steps.tag.outputs.tag }}", "gh-v3.1.0")
        command = command.replace("${{ steps.release_type.outputs.prerelease }}", "true")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace, env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                            "TMPDIR": str(temporary), "GITHUB_REPOSITORY": "avibe-bot/avibe"},
        capture_output=True, text=True, timeout=20,
    )
    success = state in {"empty", "identical", "partial"}
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    events = [json.loads(line) for line in (workspace / "events.jsonl").read_text().splitlines()]
    uploads = [event for event in events if event[1] == "upload"]
    if not success:
        assert not uploads
    else:
        uploaded = [Path(path).name for event in uploads for path in event[event.index("--repo") + 2:]]
        assert len(uploaded) == len(set(uploaded))
        assert set(uploaded) == set(all_assets) - set(existing)
        if uploads:
            first_upload = events.index(uploads[0])
            assert all(event[1] != "download" for event in events[first_upload:])
        kinds = [name in packages for name in uploaded]
        assert kinds == sorted(kinds), "Runtime uploads must complete before package uploads"
    assert not list(temporary.iterdir())

def _job(workflow_name: str, job_name: str) -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    return workflow["jobs"][job_name]


def _step(job: dict, name: str) -> dict:
    step, = [step for step in job["steps"] if step.get("name") == name]
    return step


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
def test_release_verification_is_workflow_owned_after_tagged_artifacts_are_built(
    workflow_name: str, job_name: str, checkout_name: str, workflow_ref: str,
) -> None:
    job = _job(workflow_name, job_name)
    steps = job["steps"]
    source = _step(job, "Checkout")
    checkout = _step(job, checkout_name)
    build = _step(job, "Build Python distributions")
    verify = _step(job, "Verify distribution package matrix")

    expected_source_ref = (
        "${{ github.event.inputs.tag || github.ref }}" if workflow_name == "release_ai.yml"
        else "${{ needs.resolve-tag.outputs.tag }}"
    )
    assert source["with"]["ref"] == expected_source_ref
    assert checkout["uses"] == source["uses"]
    assert checkout["with"]["ref"] == workflow_ref
    assert checkout["with"]["path"] == "release-automation"
    assert checkout["with"]["sparse-checkout-cone-mode"] is False
    expected_files = {"tests/test_memory_distribution.py"}
    if workflow_name == "publish.yml":
        expected_files.add("scripts/github_release.py")
        expected_files.add("scripts/release_package_version.py")
    assert set(checkout["with"]["sparse-checkout"].splitlines()) == expected_files
    assert steps.index(source) < steps.index(build) < steps.index(checkout) < steps.index(verify)
    assert all(not step.get("if") and not step.get("continue-on-error") for step in (checkout, verify))
    commands = [
        line for line in verify["run"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands == [
        "cp release-automation/tests/test_memory_distribution.py tests/test_memory_distribution.py",
        'AVIBE_CORE_WHEEL="$(ls dist/avibe_os-*.whl)" \\',
        'AVIBE_MEMORY_WHEEL="$(ls dist/avibe_memory-*.whl)" \\',
        "  pytest tests/test_memory_distribution.py -v",
    ]
    for later in steps[steps.index(checkout) + 1:]:
        assert "python -m build" not in later.get("run", "")
    upload_name = "Upload release artifacts" if workflow_name == "release_ai.yml" else "Upload GitHub release assets"
    assert steps.index(verify) < steps.index(_step(job, upload_name))


@pytest.mark.parametrize(("workflow_name", "job_name", "checkout_name", "workflow_ref"), WORKFLOWS)
@pytest.mark.parametrize("failure", [None, "missing-verifier", "test-failure"])
def test_exact_matrix_command_uses_new_verifier_without_changing_artifacts(
    tmp_path: Path, workflow_name: str, job_name: str, checkout_name: str, workflow_ref: str,
    failure: str | None,
) -> None:
    workspace = tmp_path / "tagged source 中文"
    tests = workspace / "tests"
    automation_tests = workspace / "release-automation" / "tests"
    dist = workspace / "dist"
    for directory in (tests, automation_tests, dist):
        directory.mkdir(parents=True, exist_ok=True)
    original = b"# original tagged verification\n"
    corrected = b"# corrected workflow-owned verification\n"
    (tests / "test_memory_distribution.py").write_bytes(original)
    if failure != "missing-verifier":
        (automation_tests / "test_memory_distribution.py").write_bytes(corrected)
    artifacts = {
        "avibe_os-3.1.0-py3-none-any.whl": b"core built from immutable source",
        "avibe_memory-3.1.0-py3-none-any.whl": b"companion built from immutable source",
        "avibe_os-3.1.0.tar.gz": b"core sdist",
        "avibe_memory-3.1.0.tar.gz": b"companion sdist",
    }
    for name, data in artifacts.items():
        (dist / name).write_bytes(data)
    source_file = workspace / "source.py"
    source_file.write_bytes(b"# unchanged runtime code\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    pytest_stub = binaries / "pytest"
    pytest_stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path('invocation.json').write_text(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'core': os.environ['AVIBE_CORE_WHEEL'],\n"
        "    'memory': os.environ['AVIBE_MEMORY_WHEEL'],\n"
        "    'test': pathlib.Path(sys.argv[1]).read_text(),\n"
        "}))\n"
        f"sys.exit({1 if failure == 'test-failure' else 0})\n",
        encoding="utf-8",
    )
    pytest_stub.chmod(0o755)
    command = _step(_job(workflow_name, job_name), "Verify distribution package matrix")["run"]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=workspace,
        env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in dist.iterdir()} == artifacts
    assert source_file.read_bytes() == b"# unchanged runtime code\n"
    receipt = workspace / "invocation.json"
    if failure == "missing-verifier":
        assert not receipt.exists()
        assert (tests / "test_memory_distribution.py").read_bytes() == original
    else:
        assert json.loads(receipt.read_text()) == {
            "args": ["tests/test_memory_distribution.py", "-v"],
            "core": "dist/avibe_os-3.1.0-py3-none-any.whl",
            "memory": "dist/avibe_memory-3.1.0-py3-none-any.whl",
            "test": corrected.decode(),
        }
