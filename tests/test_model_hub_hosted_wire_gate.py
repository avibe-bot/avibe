"""The hosted wire gate and its A/B runner (``scripts/model_hub_hosted_wire_gate.py``)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from scripts import model_hub_hosted_wire_gate as gate
from tests.test_model_hub_engine_release_guard import _fake_download, _manifest

BASE_TAG = "model-hub-engine-v8.0.23-3"
PR_TAG = "model-hub-engine-v8.0.28-1"
DIFFERENCES = {
    "test_responses_xai_history_replays_in_real_shapes": ["upstream header set", "upstream request bytes"],
    "test_image_generation_is_never_injected_or_answered_by_the_engine": ["downstream bytes"],
}
SUMMARY = ("test_responses_xai_history_replays_in_real_shapes: upstream header set, upstream request bytes; "
           "test_image_generation_is_never_injected_or_answered_by_the_engine: downstream bytes")


def test_the_cases_mask_only_per_run_values() -> None:
    from tests.e2e.test_model_hub_engine_hosted_passthrough import _stable, _stable_header

    assert _stable_header("host", "127.0.0.1:43123") == "127.0.0.1:<per-run>"
    assert _stable_header("host", "api.openai.com") == "api.openai.com"
    assert _stable_header("session-id", "019a7f3e-9d1c") == "<per-run>"
    assert _stable_header("user-agent", "codex-tui/0.154.0") == "codex-tui/0.154.0"
    body = b'{"model":"m","prompt_cache_key":"019a7f3e","input":[ ]}'
    assert _stable(body) == b'{"model":"m","prompt_cache_key":"<per-run>","input":[ ]}'
    assert _stable(b'{"model":"m", "input":[ ]}') == b'{"model":"m", "input":[ ]}'
    with pytest.raises(AssertionError, match="one compact field"):
        _stable(b'{"prompt_cache_key": "019a7f3e"}')


def _recorded() -> dict:
    return {
        "upstream": {
            "path": "/v1/responses",
            "query": "",
            "headers": [["content-type", "application/json"], ["session-id", "<per-run>"]],
            "body": '{"model":"hosted-model","prompt_cache_key":"<per-run>"}',
        },
        "downstream": {"status": 200, "body": "event: response.completed\ndata: {}\n\n"},
    }


def test_identical_recordings_have_no_differences() -> None:
    assert gate.wire_differences({"case_a": _recorded()}, {"case_a": _recorded()}) == {}


@pytest.mark.parametrize(
    ("direction", "part", "label"),
    [
        ("upstream", "path", "upstream request path"),
        ("upstream", "query", "upstream request query"),
        ("upstream", "headers", "upstream header set"),
        ("upstream", "body", "upstream request bytes"),
        ("downstream", "status", "downstream status"),
        ("downstream", "body", "downstream bytes"),
    ],
)
def test_each_wire_part_is_compared(direction: str, part: str, label: str) -> None:
    changed = _recorded()
    changed[direction][part] = 201 if part == "status" else ["x"] if part == "headers" else "x"

    differences = gate.wire_differences(
        {"case_a": _recorded(), "case_b": _recorded()}, {"case_a": changed, "case_b": _recorded()},
    )

    assert differences == {"case_a": [label]}


def test_every_differing_part_of_a_case_is_named_in_wire_order() -> None:
    changed = _recorded()
    changed["downstream"]["body"] = "x"
    changed["upstream"]["headers"] = []

    assert gate.wire_differences({"case_a": _recorded()}, {"case_a": changed}) == {
        "case_a": ["upstream header set", "downstream bytes"],
    }


def test_a_case_recorded_against_one_engine_is_a_difference() -> None:
    assert gate.wire_differences({"case_a": _recorded()}, {"case_b": _recorded()}) == {
        "case_a": ["recorded against only one engine"], "case_b": ["recorded against only one engine"],
    }


@pytest.mark.parametrize("labels", [None, frozenset(), frozenset({gate.CHANGE_LABEL})])
def test_an_identical_passthrough_passes(labels) -> None:
    assert gate.wire_change_violation({}, labels) is None


@pytest.mark.parametrize("labels", [frozenset({gate.CHANGE_LABEL}), frozenset({"dependencies", gate.CHANGE_LABEL})])
def test_a_labeled_difference_passes(labels) -> None:
    assert gate.wire_change_violation(DIFFERENCES, labels) is None


@pytest.mark.parametrize(
    "labels", [None, frozenset(), frozenset({"dependencies"}), frozenset({gate.CHANGE_LABEL.upper()})],
)
def test_an_unlabeled_difference_fails_and_names_what_differs(labels) -> None:
    assert gate.wire_change_violation(DIFFERENCES, labels) == (
        f"the engine changes the hosted passthrough ({SUMMARY}). If that is intended, label the pull request "
        f"{gate.CHANGE_LABEL} and say in its body what changed and why"
    )


def _api(monkeypatch, response: bytes | Exception) -> list:
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    return requests


def test_labels_are_read_from_the_pull_request_as_it_is_now(monkeypatch) -> None:
    payload = {"number": 7, "labels": [{"name": gate.CHANGE_LABEL}, {"name": "dependencies"}]}
    requests = _api(monkeypatch, json.dumps(payload).encode())

    assert gate.pull_request_labels("avibe-bot/avibe", "7", "token-1") == {gate.CHANGE_LABEL, "dependencies"}
    [request] = requests
    assert request.full_url == "https://api.github.com/repos/avibe-bot/avibe/pulls/7"
    assert request.get_header("Authorization") == "Bearer token-1"

    requests.clear()
    assert gate.pull_request_labels("avibe-bot/avibe", "7", None) == {gate.CHANGE_LABEL, "dependencies"}
    assert requests[0].get_header("Authorization") is None


@pytest.mark.parametrize(
    "response",
    [
        urllib.error.HTTPError("https://api.github.com", 403, "rate limited", {}, None),
        urllib.error.URLError("unreachable"),
        TimeoutError("timed out"),
        b"not json",
        b'{"number": 7}',
        b'{"labels": "engine-hosted-wire-change"}',
        b'{"labels": [{"title": "engine-hosted-wire-change"}]}',
        b"[]",
    ],
)
def test_labels_that_cannot_be_read_count_as_none(monkeypatch, capsys, response) -> None:
    _api(monkeypatch, response)

    assert gate.pull_request_labels("avibe-bot/avibe", "7", "token-1") is None
    assert "cannot read the pull request's labels" in capsys.readouterr().out


def _repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: dict | bytes, pr: dict | bytes) -> None:
    manifest = tmp_path / "repo" / gate.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(pr if isinstance(pr, bytes) else json.dumps(pr).encode())
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(gate, "_git_show",
                        lambda revision, path: base if isinstance(base, bytes) else json.dumps(base).encode())


def _verdicts(monkeypatch, differences: dict | None, labels: frozenset[str] | None = None) -> dict:
    """Record the A/B and label reads ``main`` makes; ``differences`` None means it must not compare."""
    seen = {"ab": [], "labels": []}

    def compare(base, pr, work, assets, *, loopback_only):
        if differences is None:
            pytest.fail("ran the A/B")
        seen["ab"].append((json.loads(base), json.loads(pr), assets, loopback_only))
        return differences

    def read_labels(repository, number, token):
        seen["labels"].append((repository, number, token))
        return labels

    monkeypatch.setattr(gate, "hosted_wire_differences", compare)
    monkeypatch.setattr(gate, "pull_request_labels", read_labels)
    return seen


@pytest.mark.parametrize("pr", [{"asset_release_tag": BASE_TAG}, {"asset_release_tag": BASE_TAG, "note": "x"}])
def test_an_unchanged_engine_has_nothing_to_compare(tmp_path, monkeypatch, capsys, pr) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, pr)
    seen = _verdicts(monkeypatch, None)

    assert gate.main(["--base", "base", "--pull-request", "7"]) == 0
    assert seen == {"ab": [], "labels": []}
    assert "the engine is unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("pr", [b"{", b"[]", b'{"version": "v8.0.28"}'])
def test_a_manifest_without_an_engine_tag_fails(tmp_path, monkeypatch, capsys, pr) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, pr)
    _verdicts(monkeypatch, None)

    assert gate.main(["--base", "base"]) == 1
    assert "::error::hosted_wire gate: cannot read either manifest's asset_release_tag" in capsys.readouterr().out


def test_an_identical_passthrough_passes_without_reading_labels(tmp_path, monkeypatch, capsys) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG})
    seen = _verdicts(monkeypatch, {})

    assert gate.main(["--base", "base", "--pull-request", "7", "--head-assets", str(tmp_path / "build")]) == 0
    assert seen == {"ab": [({"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG}, tmp_path / "build", False)],
                    "labels": []}
    assert "hosted passthrough byte-identical" in capsys.readouterr().out


def test_the_loopback_only_flag_reaches_the_ab(tmp_path, monkeypatch) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG})
    seen = _verdicts(monkeypatch, {})

    assert gate.main(["--base", "base", "--loopback-only"]) == 0
    assert seen["ab"] == [({"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG}, None, True)]


@pytest.mark.parametrize(
    ("labels", "status"),
    [(frozenset({gate.CHANGE_LABEL}), 0), (frozenset({"dependencies"}), 1), (frozenset(), 1), (None, 1)],
)
def test_a_difference_is_judged_on_the_labels_read_when_the_gate_runs(
    tmp_path, monkeypatch, capsys, labels, status,
) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG})
    monkeypatch.setenv("GH_TOKEN", "token-1")
    seen = _verdicts(monkeypatch, DIFFERENCES, labels)

    assert gate.main(["--base", "base", "--pull-request", "7", "--repository", "avibe-bot/avibe"]) == status
    assert seen["labels"] == [("avibe-bot/avibe", "7", "token-1")]
    out = capsys.readouterr().out
    assert "hosted passthrough differs" in out
    assert "  test_image_generation_is_never_injected_or_answered_by_the_engine: downstream bytes" in out
    if status:
        assert f"::error file={gate.MANIFEST}::hosted_wire gate: the engine changes the hosted passthrough ({SUMMARY})" in out
    else:
        assert f"labeled {gate.CHANGE_LABEL}, so the difference is intended" in out


def test_without_a_pull_request_no_label_can_accept_a_difference(tmp_path, monkeypatch, capsys) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG})
    seen = _verdicts(monkeypatch, DIFFERENCES, frozenset({gate.CHANGE_LABEL}))

    assert gate.main(["--base", "base", "--head-assets", str(tmp_path / "build")]) == 1
    assert seen["labels"] == []
    out = capsys.readouterr().out
    assert "no pull request, so no label can accept the difference" in out
    assert f"::error file={gate.MANIFEST}::hosted_wire gate: the engine changes the hosted passthrough" in out


def test_a_gate_that_cannot_reach_a_verdict_fails(tmp_path, monkeypatch, capsys) -> None:
    _repository(tmp_path, monkeypatch, {"asset_release_tag": BASE_TAG}, {"asset_release_tag": PR_TAG})

    def unreachable(*_args, **_kwargs):
        raise gate.GateError("the PR engine is not the asset its manifest pins")

    monkeypatch.setattr(gate, "hosted_wire_differences", unreachable)
    monkeypatch.setattr(gate, "pull_request_labels", lambda *a: frozenset({gate.CHANGE_LABEL}))

    assert gate.main(["--base", "base", "--pull-request", "7"]) == 1
    assert "::error::hosted_wire gate: the PR engine is not the asset its manifest pins" in capsys.readouterr().out


def test_the_ab_takes_base_from_its_release_and_the_pr_engine_from_the_given_build(tmp_path, monkeypatch) -> None:
    runs = []

    def run_cases(side, manifest_bytes, platform, work, asset_dir=None, *, must_pass, loopback_only):
        runs.append((side, manifest_bytes, work, asset_dir, must_pass, loopback_only))
        changed = _recorded()
        if side == "PR":
            changed["downstream"]["body"] = "changed"
        return {"case_a": changed}

    monkeypatch.setattr(gate, "_run_cases", run_cases)

    differences = gate.hosted_wire_differences(b"base", b"pr", tmp_path, tmp_path / "build", loopback_only=True)

    assert differences == {"case_a": ["downstream bytes"]}
    # Only the engine being adopted must pass; base must record every case. Both run without network.
    assert runs == [("base", b"base", tmp_path / "base", None, False, True),
                    ("PR", b"pr", tmp_path / "head", tmp_path / "build", True, True)]


CASE_NAMES = ("test_one", "test_two")


def _fake_pytest(*, returncode: int = 0, outcomes: dict | None = None, recorded: tuple = CASE_NAMES):
    """Stand in for the case run: check the offline manifest, then write evidence and a JUnit report."""
    runs = []

    def run(args, *, cwd, env, check):
        manifest = json.loads(Path(env["VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH"]).read_text(encoding="utf-8"))
        [url] = [asset["url"] for asset in manifest["assets"] if asset["platform"] == "linux-amd64"]
        assert urlparse(url).scheme == "file" and Path(url2pathname(urlparse(url).path)).is_file()
        runs.append(args)
        for name in recorded:
            (Path(env[gate.EVIDENCE_DIR_ENV]) / f"{name}.json").write_text(json.dumps(_recorded()), encoding="utf-8")
        cases = "".join(
            f'<testcase name="{name}">{(outcomes or {}).get(name, "")}</testcase>' for name in CASE_NAMES
        )
        report = next(arg for arg in args if arg.startswith("--junitxml=")).removeprefix("--junitxml=")
        Path(report).write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
        return SimpleNamespace(returncode=returncode)

    return SimpleNamespace(run=run), runs


def _release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, dict, dict]:
    manifest_path, owned, built = _manifest(tmp_path)
    monkeypatch.setattr(gate.release_guard, "_download", _fake_download(owned))
    return manifest_path.read_bytes(), owned, built


@pytest.mark.parametrize("loopback_only", [False, True])
def test_the_cases_run_in_the_loopback_only_namespace_when_asked(tmp_path, monkeypatch, loopback_only) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    fake, runs = _fake_pytest()
    monkeypatch.setattr(gate, "subprocess", fake)

    gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True, loopback_only=loopback_only)

    [args] = runs
    pytest_at = args.index("pytest") - 2
    assert args[:pytest_at] == (["bash", gate.LOOPBACK_ONLY] if loopback_only else [])
    assert (gate.REPO_ROOT / gate.LOOPBACK_ONLY).is_file()


def test_a_published_engine_runs_the_cases_from_its_verified_archive(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    fake, runs = _fake_pytest()
    monkeypatch.setattr(gate, "subprocess", fake)

    cases = gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True)

    assert cases == {name: _recorded() for name in CASE_NAMES}
    [args] = runs
    assert args[-1] == gate.CASES and "e2e_model_hub" in args


@pytest.mark.parametrize(
    "fake_kwargs",
    [{"returncode": 1}, {"outcomes": {"test_two": "<skipped/>"}}, {"outcomes": {"test_two": "<failure/>"}},
     {"outcomes": {"test_two": "<error/>"}}],
)
def test_the_pr_engine_must_pass_every_case(tmp_path, monkeypatch, fake_kwargs) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "subprocess", _fake_pytest(**fake_kwargs)[0])

    with pytest.raises(gate.GateError, match="did not all pass against the PR engine"):
        gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True)


def test_a_case_that_fails_as_its_strict_xfail_says_counts_as_passing(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    xfailed = '<skipped type="pytest.xfail" message="known engine defect" />'
    monkeypatch.setattr(gate, "subprocess", _fake_pytest(outcomes={"test_two": xfailed})[0])

    cases = gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True)

    assert cases == {name: _recorded() for name in CASE_NAMES}


@pytest.mark.parametrize(
    "outcome",
    [
        # An engine that fixed the defect: strict xfail reports the pass as a failure.
        '<failure message="[XPASS(strict)] known engine defect">[XPASS(strict)] known engine defect</failure>',
        '<skipped type="pytest.skip" message="nope" />',
        "<skipped />",
        '<skipped type="pytest.xfail" /><error message="teardown" />',
    ],
)
def test_only_an_expected_failure_stands_in_for_a_pass(tmp_path, monkeypatch, outcome) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "subprocess", _fake_pytest(outcomes={"test_two": outcome})[0])

    with pytest.raises(gate.GateError, match="did not all pass against the PR engine"):
        gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True)


def test_a_base_engine_that_fails_a_case_is_compared_as_recorded(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    fake, _runs = _fake_pytest(returncode=1, outcomes={"test_two": "<failure/>"})
    monkeypatch.setattr(gate, "subprocess", fake)

    cases = gate._run_cases("base", manifest_bytes, "linux-amd64", tmp_path / "base", must_pass=False)

    assert cases == {name: _recorded() for name in CASE_NAMES}


@pytest.mark.parametrize("must_pass", [True, False])
def test_every_case_must_be_recorded(tmp_path, monkeypatch, must_pass) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    # A case skipped or broken before its exchange records nothing.
    fake, _runs = _fake_pytest(outcomes={"test_two": "<skipped/>"}, recorded=("test_one",))
    monkeypatch.setattr(gate, "subprocess", fake)

    with pytest.raises(gate.GateError, match=r"did not all pass|recorded \['test_one'\], expected \['test_one', 'test_two'\]"):
        gate._run_cases("base", manifest_bytes, "linux-amd64", tmp_path / "base", must_pass=must_pass)


def test_a_case_run_that_collects_nothing_fails(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)

    def collected_nothing(args, *, cwd, env, check):
        report = next(arg for arg in args if arg.startswith("--junitxml=")).removeprefix("--junitxml=")
        Path(report).write_text("<testsuites><testsuite/></testsuites>", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=collected_nothing))

    with pytest.raises(gate.GateError, match="did not all pass"):
        gate._run_cases("base", manifest_bytes, "linux-amd64", tmp_path / "base", must_pass=False)


def test_a_case_run_without_a_report_fails(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, _built = _release(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0)))

    with pytest.raises(gate.GateError, match="left no report"):
        gate._run_cases("base", manifest_bytes, "linux-amd64", tmp_path / "base", must_pass=False)


def test_an_engine_that_is_not_its_manifests_asset_never_runs(tmp_path, monkeypatch) -> None:
    manifest_bytes, owned, _built = _release(tmp_path, monkeypatch)
    url = next(url for url in owned if url.endswith("_linux_amd64.tar.gz"))
    monkeypatch.setattr(gate.release_guard, "_download", _fake_download({**owned, url: b"tampered"}))
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=lambda *a, **k: pytest.fail("ran the cases")))

    with pytest.raises(gate.GateError, match="the PR engine is not the asset its manifest pins"):
        gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", must_pass=True)
    with pytest.raises(gate.GateError, match="the PR manifest has no linux-riscv64 asset"):
        gate._run_cases("PR", manifest_bytes, "linux-riscv64", tmp_path / "riscv", must_pass=True)
    with pytest.raises(gate.GateError, match="the PR manifest is invalid"):
        gate._run_cases("PR", b'{"schema_version": 2}', "linux-amd64", tmp_path / "invalid", must_pass=True)


def _build_output(tmp_path: Path, manifest_bytes: bytes, built: dict) -> Path:
    output = tmp_path / "build"
    output.mkdir()
    (output / "model-hub-engine-manifest.json").write_bytes(manifest_bytes)
    for name, archive in built.items():
        (output / name).write_bytes(archive)
        (output / f"{name}.sha256").write_text(f"{gate.release_guard._sha256(output / name)}  {name}\n")
    return output


def test_a_local_candidate_runs_from_its_build_output_without_downloading(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, built = _release(tmp_path, monkeypatch)
    output = _build_output(tmp_path, manifest_bytes, built)
    monkeypatch.setattr(gate.release_guard, "_download", lambda *a, **k: pytest.fail("downloaded"))
    fake, runs = _fake_pytest()
    monkeypatch.setattr(gate, "subprocess", fake)

    assert gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", output, must_pass=True)
    assert len(runs) == 1


def test_a_local_candidate_must_be_exactly_what_its_manifest_pins(tmp_path, monkeypatch) -> None:
    manifest_bytes, _owned, built = _release(tmp_path, monkeypatch)
    output = _build_output(tmp_path, manifest_bytes, built)
    # A build from another manifest: the copy publication would upload differs.
    (output / "model-hub-engine-manifest.json").write_bytes(manifest_bytes + b"\n")
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=lambda *a, **k: pytest.fail("ran the cases")))

    with pytest.raises(gate.GateError, match="the PR engine is not the asset its manifest pins"):
        gate._run_cases("PR", manifest_bytes, "linux-amd64", tmp_path / "head", output, must_pass=True)
