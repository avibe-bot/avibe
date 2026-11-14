"""Required queue API reads retry transport only, never the SQLite contract.

A real fake-gh process covers the formerly unguarded call boundary; a fake
clock keeps retries fast without removing the real subprocess/parser path.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from scripts import release_asset_download as downloads
from scripts import sqlite_predecessor_contract as contract
from tests.test_release_asset_download import retry_clock  # noqa: F401


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / ("gh.exe" if os.name == "nt" else "gh")
    if os.name == "nt":
        pytest.skip("POSIX fake executable; the required queue probe runs on Linux")
    config = tmp_path / "responses.json"
    calls = tmp_path / "calls.jsonl"
    executable.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent.parent
calls = root / "calls.jsonl"
number = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
responses = json.loads((root / "responses.json").read_text())
response = responses[min(number, len(responses) - 1)]
if sys.argv[1:3] == ["release", "download"]:
    target = pathlib.Path(sys.argv[sys.argv.index("--dir") + 1]) / sys.argv[sys.argv.index("--pattern") + 1]
    if target.exists() and "--clobber" not in sys.argv:
        print("asset already exists", file=sys.stderr)
        sys.exit(1)
    target.write_text(response.get("asset", "complete"))
print(response.get("stdout", ""), end="")
print(response.get("stderr", ""), end="", file=sys.stderr)
sys.exit(response.get("code", 0))
''')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])

    def configure(*responses):
        config.write_text(json.dumps(responses))
        return calls

    return configure


PREDECESSOR = {"tag_name": "v3.2.4", "published_at": "2026-11-01T00:00:00Z", "draft": False}
SUCCESS = {"stdout": json.dumps([[PREDECESSOR]])}


@pytest.mark.parametrize("message", [
    "gh: Service Unavailable (HTTP 503)",
    "Get https://api.github.com: unexpected EOF",
    "read tcp: connection reset by peer",
    "error connecting to api.github.com",
])
def test_predecessor_api_retries_real_gh_failure_then_uses_only_complete_output(fake_gh, retry_clock, message):
    calls = fake_gh({"code": 1, "stderr": message, "stdout": "partial response"}, SUCCESS)
    assert contract.previous_release("test/repo") == PREDECESSOR
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded == [["api", "repos/test/repo/releases?per_page=100", "--paginate", "--slurp"]] * 2
    assert retry_clock.sleeps == [1.5]


@pytest.mark.parametrize("message", [
    "gh: Bad credentials (HTTP 401)",
    "gh: Resource not accessible by integration (HTTP 403)",
    "gh: Not Found (HTTP 404)",
    "gh: Unprocessable Entity (HTTP 422)",
    "tls: failed to verify certificate: x509: certificate signed by unknown authority",
    "unknown flag: --bad-option",
])
def test_permanent_gh_errors_fail_immediately_with_the_original_reason(fake_gh, retry_clock, message):
    calls = fake_gh({"code": 1, "stderr": message, "stdout": "secondary rate limit"}, SUCCESS)
    with pytest.raises(RuntimeError, match="GitHub read failed") as error:
        contract.previous_release("test/repo")
    assert message in str(error.value)
    assert len(calls.read_text().splitlines()) == 1
    assert retry_clock.sleeps == []


def test_gh_transient_exhaustion_is_three_attempts(fake_gh, retry_clock):
    calls = fake_gh({"code": 1, "stderr": "gh: Bad Gateway (HTTP 502)"})
    with pytest.raises(RuntimeError, match="attempt 3/3"):
        contract.previous_release("test/repo")
    assert len(calls.read_text().splitlines()) == 3
    assert retry_clock.sleeps == [1.5, 2.5]


@pytest.mark.parametrize("body,error", [("not json", ValueError), ("[[]]", RuntimeError)])
def test_successful_transport_with_invalid_release_contract_is_never_retried(fake_gh, retry_clock, body, error):
    calls = fake_gh({"stdout": body}, SUCCESS)
    with pytest.raises(error):
        contract.previous_release("test/repo")
    assert len(calls.read_text().splitlines()) == 1 and retry_clock.sleeps == []


def test_gh_retry_uses_remaining_wall_budget(monkeypatch, retry_clock):
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        retry_clock.now += kwargs["timeout"]
        raise downloads.subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(downloads.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="30s"):
        contract.previous_release("test/repo")
    assert calls == [30.0] and retry_clock.now == 30.0 and retry_clock.sleeps == []


@pytest.mark.parametrize("status,message", [(429, "Too Many Requests"), (403, "You have exceeded a secondary rate limit")])
@pytest.mark.parametrize("retry_after,retries", [("0", True), ("1", True), ("60", False), (None, False)])
def test_gh_rate_limits_never_retry_ahead_of_server_or_beyond_budget(fake_gh, retry_clock, status, message, retry_after, retries):
    diagnostic = f"gh: {message} (HTTP {status})"
    if retry_after is not None:
        diagnostic += f"\nRetry-After: {retry_after}"
    calls = fake_gh({"code": 1, "stderr": diagnostic}, SUCCESS)
    if retries:
        assert contract.previous_release("test/repo") == PREDECESSOR
        assert len(calls.read_text().splitlines()) == 2
    else:
        with pytest.raises(RuntimeError, match="retry budget"):
            contract.previous_release("test/repo")
        assert len(calls.read_text().splitlines()) == 1 and retry_clock.sleeps == []


@pytest.mark.parametrize("failure", [None, "digest", "reader", "changed"])
def test_predecessor_download_replaces_partial_file_but_never_retries_contract(fake_gh, retry_clock, monkeypatch, failure):
    # This fake release/reader proves epoch 2. Pin its source proof too rather
    # than accidentally coupling download/retry behavior to the current build.
    monkeypatch.setattr("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", 2)
    content = b"complete wheel fixture"
    digest = "0" * 64 if failure == "digest" else hashlib.sha256(content).hexdigest()
    release = {**PREDECESSOR, "assets": [{"name": "avibe_os-3.2.4-py3-none-any.whl", "digest": f"sha256:{digest}"}]}
    final = {**release, "tag_name": "v3.2.5"} if failure == "changed" else release
    calls = fake_gh(
        {"stdout": json.dumps([[release]])},
        {"code": 1, "stderr": "read tcp: connection reset by peer", "asset": "partial"},
        {"asset": content.decode()},
        {"stdout": json.dumps([[final]])},
    )
    real_run = contract._run
    monkeypatch.setattr(contract, "_run", lambda *args, **kwargs: real_run(*args, **kwargs) if args[0] == "gh" else "")
    verified = []

    def verify(*args, **kwargs):
        verified.append(True)
        if failure == "reader":
            raise RuntimeError("reader contract mismatch")
        return 2

    monkeypatch.setattr(contract, "verify_predecessor", verify)
    if failure:
        with pytest.raises(RuntimeError, match={"digest": "checksum mismatch", "reader": "reader contract mismatch",
                                               "changed": "changed during verification"}[failure]):
            contract.main(["--repo", "test/repo"])
    else:
        assert contract.main(["--repo", "test/repo"]) == 0
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(recorded) == (3 if failure in ("digest", "reader") else 4)
    assert len(verified) == (0 if failure == "digest" else 1)
    assert recorded[1] == recorded[2] and "--clobber" in recorded[1]
    assert retry_clock.sleeps == [1.5]


def test_only_read_only_gh_operations_can_use_retries():
    with pytest.raises(ValueError, match="read-only"):
        downloads.run_gh_read("release", "create", "v1.0.0")
    with pytest.raises(ValueError, match="read-only"):
        downloads.run_gh_read("api", "repos/test/repo/issues", "--method", "POST")
