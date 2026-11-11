"""Model Hub untrusted rehearsal and trusted publication-boundary tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import pytest
import yaml

from core.model_hub_engine_manifest import EXPECTED_PLATFORMS
from scripts import model_hub_engine_qualification as qualification
from scripts import model_hub_hosted_wire_gate as gate
from scripts.model_hub_engine_qualification import QualificationError, ReleasedAvibe
from tests.engine_channel_support import manifest_bytes

ROOT = Path(__file__).resolve().parents[1]
TAG = "model-hub-engine-v8.0.63-2"
BASELINE = "model-hub-engine-v8.0.63-1"
BASELINE_DIGEST = hashlib.sha256(manifest_bytes(build=1)).hexdigest()
RUN_ID, ATTEMPT, HEAD = 9001, 2, "a" * 40
HARNESS = "h" * 64


def _record(root: Path, *, tag: str = TAG, minimum: str = "3.2.6", **extra) -> Path:
    directory = root / tag
    directory.mkdir(parents=True)
    manifest = manifest_bytes(build=int(tag.rsplit("-", 1)[1]))
    (directory / qualification.MANIFEST_FILE).write_bytes(manifest)
    (directory / qualification.CANDIDATE_FILE).write_text(json.dumps({
        "schema_version": 1, "asset_release_tag": tag, "min_avibe_version": minimum,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(), **extra,
    }))
    return directory


def _released(version: str) -> ReleasedAvibe:
    name = f"avibe_os-{version}-py3-none-any.whl"
    return ReleasedAvibe(version, f"v{version}", name,
                         f"https://github.com/avibe-bot/avibe/releases/download/v{version}/{name}",
                         hashlib.sha256(version.encode()).hexdigest(), 14_000_000)


AVIBE = {"minimum": _released("3.2.6"), "current_stable": _released("3.2.7")}
PROVENANCE = {
    "tests_commit": "c" * 40, "tests_sha256": "e" * 64, "dependencies_sha256": "d" * 64,
    "python": "Python 3.12.8", "node": "v22.23.4", "agent_clis": "cli-a 0.60.0; cli-b 1.2.3",
}
BASELINES = {BASELINE: BASELINE_DIGEST}


def _evidence(candidate: qualification.Candidate) -> list[dict]:
    bound = {"manifest_sha256": candidate.manifest_sha256}
    documents = [{"class": "build", **bound, "assets": candidate.identity()["assets"], "reproduced": True}]
    for archive in candidate.spec.archives:
        documents.append({"class": "platform_smoke", **bound, "platform": archive.platform,
                          "archive_sha256": archive.sha256, "binary_sha256": archive.binary_sha256,
                          "reported_version": candidate.spec.version, "health": "ok"})
    documents.append({"class": "hosted_wire", **bound, "baseline_tag": BASELINE,
                      "baseline_manifest_sha256": BASELINE_DIGEST, "cases": ["web_search"], "differences": {}})
    for role, released in AVIBE.items():
        documents.append({"class": "released_avibe_e2e", **bound, "role": role, "avibe": released.identity(),
                          "imports_from_wheel": True, "selection": ["-m", "e2e_model_hub", "tests/e2e"],
                          "provenance": dict(PROVENANCE), "junit": {"tests": 212, "failed": 0, "skipped": 0},
                          "scenario_evidence": "pass"})
    return documents


# ----- candidate record -------------------------------------------------------


def test_a_candidate_record_names_exact_manifest_bytes_and_a_minimum_avibe(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path, hosted_wire_changes={BASELINE: "upstream renamed a header"}))
    assert candidate.asset_release_tag == TAG and candidate.min_avibe_version == "3.2.6"
    assert candidate.manifest_bytes == manifest_bytes(build=2)
    assert candidate.manifest_sha256 == hashlib.sha256(manifest_bytes(build=2)).hexdigest()
    assert len(candidate.identity()["assets"]) == 6
    assert candidate.hosted_wire_changes == {BASELINE: "upstream renamed a header"}


def _rewrite(directory: Path, **fields) -> None:
    path = directory / qualification.CANDIDATE_FILE
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def _relink(directory: Path) -> None:
    manifest = directory / qualification.MANIFEST_FILE
    elsewhere = directory.parent / "elsewhere.json"
    elsewhere.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(elsewhere)


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: (d / "notes.txt").write_text("extra"),
        lambda d: (d / qualification.MANIFEST_FILE).unlink(),
        lambda d: (d / qualification.MANIFEST_FILE).write_bytes(manifest_bytes(build=2, salt="x")),
        lambda d: (d / qualification.MANIFEST_FILE).write_bytes(b"{}"),
        lambda d: d.rename(d.with_name("model-hub-engine-v8.0.63-9")),
        lambda d: _rewrite(d, min_avibe_version="3.2.5"),
        lambda d: _rewrite(d, min_avibe_version="3.2.6rc1"),
        lambda d: _rewrite(d, min_avibe_version="3.2"),
        lambda d: _rewrite(d, schema_version=2),
        lambda d: _rewrite(d, asset_release_tag="model-hub-engine-v8.0.63-3"),
        lambda d: _rewrite(d, skip_qualification=True),
        lambda d: _rewrite(d, hosted_wire_changes={BASELINE: ""}),
        pytest.param(_relink, marks=pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows")),
    ],
)
def test_anything_but_exactly_one_consistent_record_is_refused(tmp_path, damage) -> None:
    directory = _record(tmp_path)
    damage(directory)
    target = directory if directory.exists() else directory.with_name("model-hub-engine-v8.0.63-9")
    with pytest.raises(QualificationError):
        qualification.load_candidate(target)


def test_no_candidate_is_recorded_before_one_is_reviewed() -> None:
    assert sorted(path.name for path in qualification.CANDIDATES_DIR.iterdir()) == ["README.md"]


def _problems(candidate, evidence, **overrides) -> list[str]:
    return qualification.evidence_problems(candidate, evidence, **{"baselines": BASELINES, "avibe": AVIBE, **overrides})


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


# ----- candidate and untrusted observations ----------------------------------


def test_candidate_identity_includes_upstream_reference_and_exact_archives(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    identity = candidate.identity()
    assert identity["source"]["repository"] == "router-for-me/CLIProxyAPI"
    assert identity["source"]["source_sha"]
    assert len(identity["assets"]) == 6
    assert identity["manifest_sha256"] == hashlib.sha256(candidate.manifest_bytes).hexdigest()


def test_missing_future_producers_are_reported_as_observations_not_a_universal_gate(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    problems = _problems(candidate, _evidence(candidate))
    assert problems == []
    missing = [item for item in _evidence(candidate) if item["class"] != "hosted_wire"]
    problems = _problems(candidate, missing)
    assert any(problem.startswith("hosted_wire:") for problem in problems)
    assert not any(problem.startswith("backward_state:") for problem in problems)
    assert not any(problem.startswith("lifecycle:") for problem in problems)


def test_untrusted_rehearsal_keeps_failed_and_missing_runtime_results(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    evidence = _evidence(candidate)
    evidence[0]["reproduced"] = False
    evidence.append({"class": "lifecycle", "manifest_sha256": candidate.manifest_sha256, "result": "fail"})
    result = qualification.build_untrusted_rehearsal(
        candidate, evidence, baselines=BASELINES, avibe=AVIBE, run_id=RUN_ID,
        run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )
    assert result["result"] == "untrusted_rehearsal"
    assert any(item.startswith("build:") for item in result["observations"])
    assert any(item.startswith("lifecycle:") for item in result["observations"])
    assert result["candidate"]["source"]["source_sha"]


def test_collect_evidence_does_not_use_candidate_producer_claims(tmp_path) -> None:
    root = tmp_path / "evidence" / "untrusted-rehearsal-evidence-platform-smoke-linux-amd64"
    _write(root / "report.json", {"class": "platform_smoke", "platform": "linux-amd64", "producer": "trusted"})
    collected = qualification.collect_evidence(tmp_path / "evidence")
    assert collected[0]["source_artifact"].startswith("untrusted-rehearsal-evidence-")
    assert collected[0]["producer"] == "trusted"


def test_collect_evidence_rejects_symlinks_and_non_json_paths(tmp_path) -> None:
    root = tmp_path / "evidence" / "untrusted-rehearsal-evidence-build"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("not a report")
    with pytest.raises(QualificationError, match="non-JSON"):
        qualification.collect_evidence(tmp_path / "evidence")
    (root / "notes.txt").unlink()
    (root / "nested").mkdir()
    with pytest.raises(QualificationError, match="non-JSON"):
        qualification.collect_evidence(tmp_path / "evidence")


class GitHub:
    def __init__(self) -> None:
        self.run = {"id": RUN_ID, "run_attempt": ATTEMPT,
                    "path": qualification.QUALIFICATION_WORKFLOW_PATH, "event": "workflow_dispatch",
                    "head_branch": "master", "status": "completed", "conclusion": "failure",
                    "head_sha": HEAD, "repository": {"full_name": "avibe-bot/avibe"},
                    "head_repository": {"full_name": "avibe-bot/avibe"}}
        self.comparison = {"status": "ahead"}
        self.jobs = [{"name": "build", "status": "completed", "conclusion": "failure"},
                     {"name": "untrusted_rehearsal", "status": "completed", "conclusion": "success"}]
        self.total_jobs: int | None = None
        self.releases = {version: self._release(version) for version in ("3.2.6", "3.2.7")}
        self.listed = [{"tag_name": "v3.2.7"}, {"tag_name": "model-hub-engine-v8.0.63-1"}, {"tag_name": "v3.2.6"}]

    @staticmethod
    def _release(version: str) -> dict:
        released = _released(version)
        return {"tag_name": released.tag, "draft": False, "prerelease": False,
                "assets": [{"name": released.wheel_name, "size": released.wheel_size,
                             "digest": f"sha256:{released.wheel_sha256}", "browser_download_url": released.wheel_url}]}

    def __call__(self, path: str):
        prefix = "/repos/avibe-bot/avibe"
        if path == f"{prefix}/actions/runs/{RUN_ID}": return self.run
        if path.startswith(f"{prefix}/compare/") and path.endswith("...master"): return self.comparison
        if path == f"{prefix}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100":
            total = len(self.jobs) if self.total_jobs is None else self.total_jobs
            return {"total_count": total, "jobs": self.jobs}
        if path == f"{prefix}/releases?per_page=100":
            return [{"draft": False, "prerelease": False, **item} for item in self.listed]
        if path.startswith(f"{prefix}/releases/tags/v"):
            return self.releases.get(path.rsplit("/v", 1)[1])
        return None


def _result(candidate) -> dict:
    return qualification.build_untrusted_rehearsal(
        candidate, _evidence(candidate), baselines=BASELINES, avibe=AVIBE, run_id=RUN_ID,
        run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )


def test_untrusted_rehearsal_can_be_verified_as_context_but_does_not_authorize(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    result = _result(candidate)
    qualification.verify_untrusted_rehearsal(result, candidate=candidate, api=GitHub(),
                                             harness_sha256=HARNESS, baselines=BASELINES)


@pytest.mark.parametrize("forge", [
    lambda r: r.update(result="qualified"),
    lambda r: r.update(run_id=RUN_ID + 1),
    lambda r: r.update(harness_sha256="0" * 64),
    lambda r: r.update(baselines={}),
    lambda r: r["candidate"].update(manifest_sha256="0" * 64),
    lambda r: r["evidence"].append({"class": "build", "manifest_sha256": "0" * 64}),
])
def test_trusted_prepare_rejects_wrong_run_or_digest_context(tmp_path, forge) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    result = _result(candidate)
    forge(result)
    with pytest.raises(QualificationError):
        qualification.verify_untrusted_rehearsal(result, candidate=candidate, api=GitHub(),
                                                 harness_sha256=HARNESS, baselines=BASELINES)


# ----- released Avibe artifacts ----------------------------------------------

# ----- released Avibe artifacts ----------------------------------------------


def test_a_released_avibe_is_the_published_stable_wheel_with_githubs_digest() -> None:
    github = GitHub()

    assert qualification.resolve_released_avibe("3.2.6", github) == _released("3.2.6")
    assert qualification.current_stable_avibe(github) == "3.2.7"
    # A stable version is compared numerically, and only stable tags count.
    github.listed = [{"tag_name": "v3.2.9"}, {"tag_name": "v3.2.10"}, {"tag_name": "gh-v3.3.0rc1"}, {"tag_name": "v4.0.0", "draft": True}]
    assert qualification.current_stable_avibe(github) == "3.2.10"


@pytest.mark.parametrize(
    "alter",
    [
        lambda release: release.update(draft=True),
        lambda release: release.update(prerelease=True),
        lambda release: release.update(tag_name="v3.2.5"),
        lambda release: release["assets"].pop(0),
        lambda release: release["assets"].append(dict(release["assets"][0])),
        lambda release: release["assets"][0].pop("digest"),
        lambda release: release["assets"][0].update(digest="md5:abc"),
        lambda release: release["assets"][0].update(size=0),
        lambda release: release["assets"][0].update(browser_download_url="https://example.invalid/avibe.whl"),
    ],
)
def test_nothing_stands_in_for_an_immutable_released_wheel(alter) -> None:
    github = GitHub()
    alter(github.releases["3.2.6"])

    with pytest.raises(QualificationError):
        qualification.resolve_released_avibe("3.2.6", github)
    for version in ("3.2.6rc1", "3.2", "0.0.0.dev0"):
        with pytest.raises(QualificationError):
            qualification.resolve_released_avibe(version, github)


class WheelRun:
    """A finished released-wheel run on disk: wheel, environment, harness, import logs, report."""

    VERSION = "3.2.6"
    ENTRY = b"# the wheel's entry script\n"

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.wheel = root / f"avibe_os-{self.VERSION}-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(qualification.WHEEL_SERVICE_ENTRY, self.ENTRY)
        self.released = ReleasedAvibe(
            self.VERSION, f"v{self.VERSION}", self.wheel.name, "https://example.invalid",
            hashlib.sha256(self.wheel.read_bytes()).hexdigest(), self.wheel.stat().st_size,
        )
        self.environment = root / "venv"
        self.site = self.environment / "lib" / "python3.12" / "site-packages"
        self.site.mkdir(parents=True)
        self.harness = root / "harness"
        (self.harness / "tests" / "e2e").mkdir(parents=True)
        (self.harness / "tests" / "e2e" / "test_case.py").write_text("def test_case(): pass\n")
        (self.harness / "main.py").write_bytes(self.ENTRY)
        self.origins = root / "origins"
        self.origins.mkdir()
        self.junit = root / "junit.xml"
        self.junit.write_text(
            '<testsuite><testcase classname="tests.e2e.a" name="a"/>'
            '<testcase classname="tests.e2e.b" name="b"><skipped type="pytest.xfail"/></testcase>'
            '<testcase classname="tests.e2e.c" name="c"><skipped type="pytest.skip"/></testcase></testsuite>'
        )
        self.provenance = {**PROVENANCE, "tests_sha256": qualification.tree_digest(self.harness / "tests")}
        self.python = str(self.environment / "bin" / "python")
        self.log(101, [self.python, "-m", "pytest", "-m", "e2e_model_hub", "tests/e2e"],
                 {"vibe": "vibe/__init__.py", "vibe.api": "vibe/api.py", "core": "core/__init__.py"})
        # The harness starts the service by a relative path from its own directory.
        self.log(202, [self.python, "main.py", "/tmp/home"],
                 {"core": "core/__init__.py", "core.controller": "core/controller.py", "vibe": "vibe/__init__.py",
                  "config": "config/__init__.py"})
        self.log(303, [self.python, "-c", "from vibe.ui_server import run_ui_server"],
                 {"vibe": "vibe/__init__.py", "vibe.ui_server": "vibe/ui_server.py"})

    def log(self, pid: int, command: list[str], modules: dict, *, version: str | None = None,
            prefix: str | None = None, cwd: Path | None = None) -> None:
        """Write one process's import log. A relative origin is inside the wheel's environment."""
        lines: list[dict] = [{"process": {
            "pid": pid, "command": command, "cwd": str(cwd or self.harness), "prefix": prefix or str(self.environment),
            "distribution_version": self.VERSION if version is None else version,
        }}]
        for name, origin in modules.items():
            inside = isinstance(origin, str) and not Path(origin).is_absolute()
            lines.append({"module": name, "origin": str(self.site / origin) if inside else origin})
        (self.origins / f"{pid}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

    def evidence(self, candidate, **overrides):
        arguments = {
            "role": "minimum", "released": self.released, "wheel": self.wheel, "environment": self.environment,
            "harness": self.harness, "origins_dir": self.origins, "junit": self.junit,
            "scenario_evidence_passed": True, "provenance": self.provenance,
        }
        return qualification.released_e2e_evidence(candidate, **{**arguments, **overrides})

    def rehearse(self, **overrides):
        arguments = {
            "tag": "gh-v3.2.6rc3", "tag_commit": "c" * 40, "version": self.VERSION, "wheel": self.wheel,
            "wheel_sha256": self.released.wheel_sha256, "environment": self.environment, "harness": self.harness,
            "origins_dir": self.origins, "junit": self.junit, "provenance": self.provenance,
        }
        return qualification.rehearsal_record(**{**arguments, **overrides})


def test_released_e2e_evidence_binds_the_wheel_the_tests_every_process_and_the_report(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path / "records"))
    run = WheelRun(tmp_path / "run")

    evidence = run.evidence(candidate)

    assert evidence["junit"] == {"tests": 3, "failed": 0, "skipped": 1}
    assert evidence["avibe"] == run.released.identity() and evidence["imports_from_wheel"] is True
    assert evidence["origins"] == {"processes": 3, "services": 1, "product_modules": 9}
    assert evidence["provenance"] == run.provenance
    assert evidence["manifest_sha256"] == candidate.manifest_sha256
    with pytest.raises(QualificationError, match="must record"):
        run.evidence(candidate, provenance={"python": "Python 3.12.8"})


def _checkout(run: WheelRun) -> str:
    return str(run.root / "checkout")


SERVICE = {"core": "core/__init__.py", "vibe": "vibe/__init__.py"}


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        # A product module from a checkout: in the test runner, deep inside the service, or in a helper process.
        (lambda run: run.log(101, [run.python, "-m", "pytest"], {"vibe": f"{_checkout(run)}/vibe/__init__.py"}),
         "outside the released wheel"),
        (lambda run: run.log(202, [run.python, "main.py"],
                             {**SERVICE,
                              "core.handlers.model_hub.service": f"{_checkout(run)}/core/handlers/model_hub/service.py"}),
         "core.handlers.model_hub.service"),
        (lambda run: run.log(404, [run.python, "-c", "pass"], {"storage.db": f"{_checkout(run)}/storage/db.py"}),
         "outside the released wheel"),
        (lambda run: run.log(404, [run.python, "-c", "pass"], {"config": [f"{_checkout(run)}/config"]}),
         "outside the released wheel"),
        (lambda run: run.log(404, [run.python, "-c", "pass"], {"modules": []}), "outside the released wheel"),
        # Found inside the environment first, and again in a checkout later.
        (lambda run: (run.origins / "202.jsonl").open("a").write(
            json.dumps({"module": "core", "origin": f"{_checkout(run)}/core/__init__.py"}) + "\n"),
         "such as core from"),
        # Another Avibe than the released one, or another interpreter than the environment's.
        (lambda run: run.log(202, [run.python, "main.py"], SERVICE, version="0.0.0.dev0"), "ran Avibe '0.0.0.dev0'"),
        (lambda run: run.log(101, ["/usr/bin/python3", "-m", "pytest"], {"vibe": "vibe/__init__.py"},
                             prefix="/usr"), "not the released wheel's environment"),
        # The test runner or the real service left no log.
        (lambda run: (run.origins / "101.jsonl").unlink(), "test runner reported no import"),
        (lambda run: run.log(101, [run.python, "-m", "pip", "list"], {"vibe": "vibe/__init__.py"}),
         "test runner reported no import"),
        (lambda run: (run.origins / "202.jsonl").unlink(), "no spawned service reported"),
        # A ``main.py`` that is not the harness's: started elsewhere, or a checkout's by absolute path.
        (lambda run: run.log(202, [run.python, "main.py"], SERVICE, cwd=Path(_checkout(run))),
         "no spawned service reported"),
        (lambda run: run.log(202, [run.python, f"{_checkout(run)}/main.py"], SERVICE), "no spawned service reported"),
        (lambda run: run.log(202, [run.python, "-c", "import main"], SERVICE), "no spawned service reported"),
        (lambda run: run.log(202, [run.python, "main.py"], {}), "no spawned service reported"),
        # The entry script in the harness is not the wheel's, or the tests changed after they were recorded.
        (lambda run: (run.harness / "main.py").write_text("# the checkout's entry script\n"), "wheel's own entry script"),
        (lambda run: (run.harness / "tests/e2e/test_case.py").write_text("def test_case(): assert True\n"),
         "not the tests that were recorded"),
        (lambda run: run.wheel.write_bytes(run.wheel.read_bytes() + b"x"), "is not the"),
    ],
)
def test_a_run_that_did_not_provably_test_the_released_wheel_is_neither_evidence_nor_a_clean_rehearsal(
    tmp_path, alter, message
) -> None:
    """MH-ENGINE-CHANNEL-010: a run counts only if every process imported Avibe from the released wheel."""

    candidate = qualification.load_candidate(_record(tmp_path / "records"))
    run = WheelRun(tmp_path / "run")
    alter(run)

    with pytest.raises(QualificationError, match=message):
        run.evidence(candidate)
    _record_document, problems = run.rehearse()
    assert any(message in problem for problem in problems)


@pytest.mark.parametrize(
    "damage",
    [
        lambda log: log.write_text("not json\n"),
        # Two processes in one log: the second one's imports would be judged by the first one's environment.
        lambda log: log.write_text(log.read_text() + log.read_text().splitlines()[0] + "\n"),
        lambda log: log.write_text("".join(line + "\n" for line in log.read_text().splitlines()[1:])),
    ],
    ids=["not-json", "two-process-records", "no-process-record"],
)
def test_an_import_log_that_cannot_be_read_fails_the_run(tmp_path, damage) -> None:
    candidate = qualification.load_candidate(_record(tmp_path / "records"))
    run = WheelRun(tmp_path / "run")
    damage(run.origins / "101.jsonl")

    with pytest.raises(QualificationError, match="import log cannot be read"):
        run.evidence(candidate)
    with pytest.raises(QualificationError, match="import log cannot be read"):
        run.rehearse()


def test_a_rehearsal_is_described_in_full_and_can_never_be_read_as_evidence(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path / "records"))
    run = WheelRun(tmp_path / "run")
    run.junit.write_text(
        '<testsuite><testcase classname="tests.e2e.a" name="a"/>'
        '<testcase classname="tests.e2e.b" name="b"><failure message="main.py not found"/></testcase></testsuite>'
    )

    record, problems = run.rehearse()

    assert problems == [] and record["harness_problems"] == []
    assert record["class"] == "rehearsal"
    assert (record["tag"], record["tag_commit"]) == ("gh-v3.2.6rc3", "c" * 40)
    assert record["wheel"] == {"name": run.wheel.name, "sha256": run.released.wheel_sha256}
    assert record["harness_sha256"] == qualification.harness_digest()
    assert record["provenance"] == run.provenance
    assert record["junit"] == {"tests": 2, "failed": 1, "skipped": 0} and record["failing"] == ["tests.e2e.b::b"]
    # No trusted result can be built from it, alone or dressed up: it binds no candidate and names no role.
    assert "manifest_sha256" not in record and "role" not in record
    assert {problem.split(":")[0] for problem in _problems(candidate, [record])} == {
        "evidence", "build", "platform_smoke", "hosted_wire", "released_avibe_e2e",
    }
    disguised = {**record, "class": "released_avibe_e2e", "role": "minimum"}
    assert any(problem.startswith("released_avibe_e2e") for problem in _problems(candidate, [disguised]))


def test_the_rehearsal_command_fails_on_harness_problems_and_on_failing_tests_only_when_asked(tmp_path, capsys) -> None:
    run = WheelRun(tmp_path / "run")
    (tmp_path / "provenance.json").write_text(json.dumps(run.provenance))
    arguments = [
        "rehearsal-record", "--tag", "gh-v3.2.6rc3", "--tag-commit", "c" * 40, "--version", run.VERSION,
        "--wheel", str(run.wheel), "--wheel-sha256", run.released.wheel_sha256, "--environment", str(run.environment),
        "--harness", str(run.harness), "--origins-dir", str(run.origins), "--junit", str(run.junit),
        "--provenance", str(tmp_path / "provenance.json"), "--output", str(tmp_path / "record.json"),
    ]

    assert qualification.main(arguments) == 0 and qualification.main([*arguments, "--require-pass"]) == 0
    run.junit.write_text('<testsuite><testcase classname="t" name="x"><failure message="no"/></testcase></testsuite>')
    # The tag's own tests may fail; that is reported, and fails only the leg that must pass.
    assert qualification.main(arguments) == 0
    assert qualification.main([*arguments, "--require-pass"]) == 1
    assert json.loads((tmp_path / "record.json").read_text())["failing"] == ["t::x"]
    (run.origins / "202.jsonl").unlink()
    assert qualification.main(arguments) == 1
    capsys.readouterr()


class RecordedEnvironment:
    """A stand-in environment with the origin recorder installed, and a checkout beside it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.environment = root / "venv"
        self.site = self.environment / "lib" / "python3.12" / "site-packages"
        for package in ("vibe/handlers", "core", "avibe_os-3.2.6.dist-info"):
            (self.site / package).mkdir(parents=True)
        (self.site / "vibe/__init__.py").write_text("")
        (self.site / "vibe/handlers/__init__.py").write_text("")
        (self.site / "vibe/handlers/deep.py").write_text("VALUE = 7\n")
        (self.site / "core/__init__.py").write_text("")
        (self.site / "avibe_os-3.2.6.dist-info/METADATA").write_text(
            "Metadata-Version: 2.4\nName: avibe-os\nVersion: 3.2.6\n\nVersion: not a header\n"
        )
        self.origins = root / "origins"
        qualification.install_origin_recorder(self.environment, self.origins)
        # A checkout that shadows a product package once it is on the path.
        self.checkout = root / "checkout"
        (self.checkout / "core").mkdir(parents=True)
        (self.checkout / "core/__init__.py").write_text("")
        (self.checkout / "core/leak.py").write_text("")

    def run(self, code: str, *paths: Path, site: Path | None = None) -> str:
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-S", "-c", f"import site; site.addsitedir({str(site or self.site)!r}); {code}"],
            check=True, cwd=self.root, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": os.pathsep.join(map(str, paths))},
        ).stdout

    def processes(self) -> list[qualification.ProcessOrigins]:
        return qualification.read_origin_logs(self.origins)

    def problems(self) -> list[str]:
        return qualification.origin_problems(
            self.processes(), environment=self.environment, version="3.2.6", harness=self.root / "harness"
        )


def test_the_recorder_logs_every_product_module_a_process_imports_from_inside_that_process(tmp_path) -> None:
    recorded = RecordedEnvironment(tmp_path)
    site, checkout = recorded.site, recorded.checkout

    # The child says which environment it runs in. Started with ``-S`` from a
    # virtual environment, that is not the environment these tests run in.
    child_prefix = recorded.run(
        "import vibe.handlers.deep, json, collections, sys; assert vibe.handlers.deep.VALUE == 7; print(sys.prefix)"
    ).strip()
    recorded.run("import core.leak", checkout)
    # A test that puts a checkout on the path and imports the package again.
    recorded.run(f"import core, sys; del sys.modules['core']; sys.path.insert(0, {str(checkout)!r}); import core.leak")

    processes = recorded.processes()
    (clean,) = [process for process in processes if "vibe" in process.modules]
    leaking, found_again = sorted(
        (process for process in processes if "core.leak" in process.modules), key=lambda process: len(process.modules["core"])
    )
    # Every product module, submodules included, and nothing from the standard library.
    assert dict(clean.modules) == {
        "vibe": (str(site / "vibe/__init__.py"),),
        "vibe.handlers": (str(site / "vibe/handlers/__init__.py"),),
        "vibe.handlers.deep": (str(site / "vibe/handlers/deep.py"),),
    }
    assert leaking.modules == {
        "core": (str(checkout / "core/__init__.py"),), "core.leak": (str(checkout / "core/leak.py"),),
    }
    # The second file a module was found in is recorded too, and judged.
    assert found_again.modules["core"] == (str(site / "core/__init__.py"), str(checkout / "core/__init__.py"))
    assert clean.command[1:3] == ("-S", "-c") and Path(clean.cwd).resolve() == tmp_path.resolve()
    assert clean.script is None and not clean.is_test_runner
    # The prefix is recorded from inside the process, so it is that process's own.
    assert child_prefix and clean.prefix == child_prefix
    # Read from the installed metadata's header, without the metadata library.
    assert {process.distribution_version for process in processes} == {"3.2.6"}
    problems = recorded.problems()
    assert len([problem for problem in problems if "imported 2 product modules from outside" in problem]) == 2
    assert not any("ran Avibe" in problem for problem in problems)


def test_the_recorder_changes_nothing_else_about_the_process_it_runs_in(tmp_path) -> None:
    recorded = RecordedEnvironment(tmp_path)
    # The same start-up hook with nothing behind it but the one library the recorder uses.
    bare = tmp_path / "bare-site"
    bare.mkdir()
    (bare / "only_json.pth").write_text("import json\n")
    loaded = "import sys; print(' '.join(sorted(sys.modules)))"

    with_recorder = set(recorded.run(loaded).split())
    with_json_only = set(recorded.run(loaded, site=bare).split())

    # No metadata, path or threading library is loaded ahead of the product.
    assert with_recorder - with_json_only == {"avibe_qualification_origins"}


# What a process under test does to its own log. The log is found by this process's id.
_OWN_LOG = "import glob, os; (log,) = glob.glob(os.path.join({origins!r}, f'{{os.getpid()}}-*.jsonl')); "
# The log cannot be appended to, while the directory it is in can still be written.
_BLOCK_LOG = "os.rename(log, log + '.held'); os.mkdir(log); "
_UNBLOCK_LOG = "os.rmdir(log); os.rename(log + '.held', log); "
_MARKED = "glob.glob(os.path.join({origins!r}, '*.incomplete'))"


def test_a_log_that_falls_behind_is_marked_incomplete_until_it_catches_up(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: a log is evidence only if it holds every import of its process."""

    recorded = RecordedEnvironment(tmp_path)
    origins = str(recorded.origins)

    recorded.run(
        (_OWN_LOG + _BLOCK_LOG + "import vibe.handlers.deep; assert " + _MARKED + "; "
         + _UNBLOCK_LOG + "import core; assert not " + _MARKED).format(origins=origins)
    )

    # Nothing was lost and no import failed; the mark went with the last unwritten record.
    (process,) = recorded.processes()
    assert sorted(process.modules) == ["core", "vibe", "vibe.handlers", "vibe.handlers.deep"]
    assert not any("outside" in problem for problem in recorded.problems())


def test_a_log_that_never_catches_up_is_refused_although_what_it_holds_reads_as_complete(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: the last import of a process is not lost silently."""

    recorded = RecordedEnvironment(tmp_path)
    origins = str(recorded.origins)

    # The checkout's module is the last import, its record cannot be written, and the process ends normally.
    recorded.run(
        ("import vibe; " + _OWN_LOG + _BLOCK_LOG + "import core.leak; print(log)").format(origins=origins),
        recorded.checkout,
    )
    (log,) = [path for path in recorded.origins.iterdir() if path.is_dir() and path.suffix == ".jsonl"]
    log.rmdir()
    log.with_name(log.name + ".held").rename(log)

    # What is left parses as one process that imported only from the environment.
    assert [json.loads(line).get("module") for line in log.read_text().splitlines()] == [None, "vibe"]
    with pytest.raises(QualificationError, match="import log is incomplete: 1 processes could not record every import"):
        recorded.processes()
    with pytest.raises(QualificationError, match="import log is incomplete"):
        recorded.problems()


def test_an_import_that_can_be_neither_recorded_nor_marked_does_not_happen(tmp_path) -> None:
    recorded = RecordedEnvironment(tmp_path)
    origins, away = str(recorded.origins), f"{recorded.origins}.away"
    attempt = "\ntry:\n    import vibe\nexcept ImportError as refused:\n    print('refused', refused.name)\nelse:\n    print('imported')\n"

    # The log's directory is gone for a while: no write, and nowhere to leave a mark.
    during = recorded.run(
        f"import os, sys; os.rename({origins!r}, {away!r})" + attempt
        + f"assert 'vibe' not in sys.modules; os.rename({away!r}, {origins!r}); import vibe.handlers.deep"
    )
    # A process that cannot log from its first moment: the log directory is a file.
    blocked = tmp_path / "blocked"
    qualification.install_origin_recorder(recorded.environment, blocked)
    blocked.rmdir()
    blocked.write_text("")
    never = recorded.run("pass" + attempt)

    assert (during, never) == ("refused vibe\n", "refused vibe\n")
    # The first process's log is whole again: the refused import is in it only from when it went through.
    (process,) = recorded.processes()
    assert sorted(process.modules) == ["vibe", "vibe.handlers", "vibe.handlers.deep"]


@pytest.mark.parametrize("recorder_first", [False, True], ids=["after-the-suite-finder", "ahead-of-the-suite-finder"])
def test_the_recorder_and_the_suites_own_import_finder_each_handle_an_import_once(
    tmp_path, monkeypatch, recorder_first
) -> None:
    """MH-ENGINE-CHANNEL-010: the recorder runs inside the test runner, beside the suite's own finder.

    Both finders delegate to the rest of ``sys.meta_path``. The first released
    wheel rehearsal lost 128 cases to the two calling each other without end
    on ``vibe.models_dev_catalog``, which the suite stubs on import.
    """

    import importlib
    import importlib.machinery
    import importlib.util
    import sys

    from scripts import model_hub_engine_qualification_origins as shipped

    name = "vibe.models_dev_catalog"
    suite_finder = next(finder for finder in sys.meta_path if type(finder).__name__ == "_StubOnImport")
    # The recorder as an environment holds it: a copy beside its configuration, active once imported.
    (tmp_path / "recorder.py").write_bytes((qualification.REPO_ROOT / qualification.ORIGINS_MODULE).read_bytes())
    (tmp_path / shipped.CONFIG_NAME).write_text(json.dumps({"directory": str(tmp_path / "origins")}))
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    spec = importlib.util.spec_from_file_location("recorder_under_test", tmp_path / "recorder.py")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    recorder = sys.meta_path[0]
    assert type(recorder).__name__ == "_Recorder"
    if not recorder_first:
        # Where a ``.pth`` leaves it once pytest and the suite have put their finders in front.
        sys.meta_path.remove(recorder)
        sys.meta_path.insert(sys.meta_path.index(importlib.machinery.BuiltinImporter), recorder)
        assert sys.meta_path.index(suite_finder) < sys.meta_path.index(recorder)
    # The module is imported afresh, with one more stub waiting for it beside the suite's own.
    package = importlib.import_module("vibe")
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delattr(package, "models_dev_catalog", raising=False)
    stubbed: list = []
    monkeypatch.setitem(suite_finder.waiting, name, [*suite_finder.waiting.get(name, ()), stubbed.append])

    module = importlib.import_module(name)

    # The suite's finder still applied its stubs, once; the recorder saw where the module came from.
    assert stubbed == [module]
    (process,) = qualification.read_origin_logs(tmp_path / "origins")
    assert process.modules[name] == (str(qualification.REPO_ROOT / "vibe" / "models_dev_catalog.py"),)


def test_processes_are_told_apart_by_the_interpreters_own_command_line(tmp_path) -> None:
    run = WheelRun(tmp_path / "run")
    # The interpreter in an environment is a link to a base one; the command line names it either way.
    run.log(505, ["/opt/python/bin/python3.12", "-m", "pytest", "tests/e2e"], {"vibe": "vibe/__init__.py"})
    run.log(606, [run.python, str(run.environment / "bin" / "pytest"), "tests/e2e"], {"vibe": "vibe/__init__.py"})

    processes = qualification.read_origin_logs(run.origins)

    assert qualification.origin_problems(
        processes, environment=run.environment, version=run.VERSION, harness=run.harness
    ) == []
    assert [process.is_test_runner for process in processes] == [True, False, False, True, True]
    assert [process.script for process in processes][:3] == [None, (run.harness / "main.py").resolve(), None]


def test_a_harness_directory_holds_the_tags_tests_the_wheels_entry_script_and_no_product_package(tmp_path) -> None:
    source = tmp_path / "released-src"
    for name in ("tests/e2e", "scripts", *qualification.PRODUCT_PACKAGES):
        (source / name).mkdir(parents=True)
        (source / name / "module.py").write_text("")
    (source / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (source / "main.py").write_text("# the checkout's entry script\n")
    wheel = tmp_path / "avibe_os-3.2.6-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(qualification.WHEEL_SERVICE_ENTRY, "# the wheel's entry script\n")

    qualification.prepare_harness(source, wheel, tmp_path / "harness")

    harness = tmp_path / "harness"
    assert sorted(path.name for path in harness.iterdir()) == ["main.py", "pyproject.toml", "scripts", "tests"]
    assert (harness / "main.py").read_text() == "# the wheel's entry script\n"
    with pytest.raises(QualificationError, match="already exists"):
        qualification.prepare_harness(source, wheel, harness)
    with zipfile.ZipFile(tmp_path / "empty.whl", "w"):
        pass
    with pytest.raises(QualificationError, match="no service entry script"):
        qualification.prepare_harness(source, tmp_path / "empty.whl", tmp_path / "other")


def test_the_engine_a_released_wheel_bundles_is_read_from_the_wheel(tmp_path) -> None:
    wheel = tmp_path / "avibe_os-3.2.6-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(qualification.PACKAGED_MANIFEST, manifest_bytes(build=1))

    assert qualification.bundled_engine_manifest(wheel) == manifest_bytes(build=1)
    with zipfile.ZipFile(tmp_path / "empty.whl", "w"):
        pass
    with pytest.raises(QualificationError):
        qualification.bundled_engine_manifest(tmp_path / "empty.whl")


def test_the_harness_digest_changes_with_any_file_that_decides_a_qualification(tmp_path) -> None:
    for name in qualification.HARNESS_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(name)
    digests = {qualification.harness_digest(tmp_path)}

    for name in qualification.HARNESS_FILES:
        (tmp_path / name).write_text("changed")
        digests.add(qualification.harness_digest(tmp_path))

    assert len(digests) == len(qualification.HARNESS_FILES) + 1
    (tmp_path / qualification.QUALIFICATION_WORKFLOW_PATH).unlink()
    with pytest.raises(QualificationError, match="missing"):
        qualification.harness_digest(tmp_path)
    # Every script the qualification workflow and its shared action run, and both files, are in the digest.
    workflow = "".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in (qualification.QUALIFICATION_WORKFLOW_PATH, qualification.RELEASED_E2E_ACTION)
    )
    invoked = set(re.findall(r"scripts/[A-Za-z0-9_]+\.(?:py|sh)", workflow)) | {qualification.ORIGINS_MODULE}
    # The scenario-evidence gate runs from the released tag's own copy, which
    # the receipt binds through that run's recorded tag commit.
    invoked.discard("scripts/ci_scenario_evidence.py")
    assert invoked and invoked <= set(qualification.HARNESS_FILES)
    assert all((ROOT / name).is_file() for name in qualification.HARNESS_FILES)


def test_qualification_and_its_rehearsal_run_the_same_action_and_only_qualification_can_produce_evidence() -> None:
    action = ".github/actions/model-hub-released-e2e"
    qualify = _workflow()["jobs"]["released-avibe-e2e"]
    rehearsal_text = (ROOT / ".github/workflows/model-hub-engine-released-e2e-rehearsal.yml").read_text(encoding="utf-8")
    rehearsal_workflow = yaml.safe_load(rehearsal_text)
    rehearsal = rehearsal_workflow["jobs"]["rehearsal"]

    def shared(job: dict) -> dict:
        (step,) = [item for item in job["steps"] if item.get("uses") == f"./{action}"]
        return step["with"]

    # Qualification runs the release tag's own tests, with no alternative.
    assert shared(qualify)["tests-source"] == "released-src"
    (tag_checkout,) = [item for item in qualify["steps"] if item.get("with", {}).get("path") == "released-src"]
    assert tag_checkout["with"]["ref"] == "${{ steps.released.outputs.tag }}"
    assert "released-avibe " in json.dumps(qualify) and "evidence-released-e2e" in json.dumps(qualify)
    # The rehearsal is pinned to one published candidate, resolves nothing, and writes no evidence.
    assert shared(rehearsal)["tests-source"] == "${{ matrix.tests == 'tag' && 'released-src' || '.' }}"
    assert rehearsal["strategy"]["matrix"] == {"tests": ["tag", "head"]}
    scripts = "\n".join(step.get("run", "") for step in rehearsal["steps"])
    for forbidden in ("released-avibe ", "evidence-", "receipt", "model_hub_engine_channel.py"):
        assert forbidden not in scripts
    assert "secrets." not in rehearsal_text and rehearsal_workflow["permissions"] == {"contents": "read"}
    assert "rehearsal-record" in scripts and "args+=(--require-pass)" in scripts
    env = rehearsal_workflow["env"]
    assert env["RC_WHEEL"] == f"avibe_os-{env['RC_VERSION']}-py3-none-any.whl" and len(env["RC_WHEEL_SHA256"]) == 64
    # A release candidate is not a stable release, so the production resolver refuses this pin.
    with pytest.raises(QualificationError):
        qualification.resolve_released_avibe(env["RC_VERSION"], GitHub())
    assert qualification.RELEASED_E2E_ACTION == f"{action}/action.yml" and (ROOT / action / "action.yml").is_file()


def test_a_local_manifest_changes_only_where_one_platforms_verified_archive_is_read_from(tmp_path, monkeypatch) -> None:
    from core.model_hub_engine_manifest import parse_release_manifest

    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(manifest_bytes(build=2))
    verified: list[tuple[Path, Path]] = []
    spec = parse_release_manifest(manifest.read_bytes())

    class Guard:
        ReleaseGuardError = RuntimeError

        @staticmethod
        def verify_release_assets(manifest_path, asset_dir):
            verified.append((manifest_path, asset_dir))
            return spec

    monkeypatch.setattr(qualification, "_guard", lambda: Guard)

    qualification.write_local_manifest(manifest, tmp_path / "assets", "linux-amd64", tmp_path / "out" / "local.json")

    assert verified == [(manifest, tmp_path / "assets")]
    original, local = json.loads(manifest.read_text()), json.loads((tmp_path / "out" / "local.json").read_text())
    (changed,) = [index for index, (a, b) in enumerate(zip(original["assets"], local["assets"])) if a != b]
    assert local["assets"][changed]["platform"] == "linux-amd64"
    assert local["assets"][changed]["url"] == (tmp_path / "assets" / spec.archive_for("linux-amd64").name).resolve().as_uri()
    assert {**local["assets"][changed], "url": original["assets"][changed]["url"]} == original["assets"][changed]
    assert {key: value for key, value in local.items() if key != "assets"} == {
        key: value for key, value in original.items() if key != "assets"
    }
    with pytest.raises(QualificationError, match="no freebsd-amd64 asset"):
        qualification.write_local_manifest(manifest, tmp_path / "assets", "freebsd-amd64", tmp_path / "x.json")


# ----- the hosted-wire comparison --------------------------------------------


def test_an_explicit_hosted_wire_comparison_records_what_differs_without_judging_it(tmp_path, monkeypatch) -> None:
    base, head = manifest_bytes(build=1), manifest_bytes(build=2)
    exchange = {"upstream": {"path": "/v1", "query": "", "headers": [], "body": "a"}, "downstream": {"status": 200, "body": "b"}}
    changed = copy.deepcopy(exchange)
    changed["upstream"]["body"] = "c"
    monkeypatch.setattr(gate, "_recorded_cases", lambda *_args, **_kwargs: ({"web_search": exchange}, {"web_search": changed}))
    (tmp_path / "base.json").write_bytes(base)
    (tmp_path / "head.json").write_bytes(head)
    output = tmp_path / "evidence" / "hosted-wire.json"

    status = gate.main(["--base-manifest", str(tmp_path / "base.json"), "--head-manifest", str(tmp_path / "head.json"),
                        "--evidence-output", str(output)])

    assert status == 0
    assert json.loads(output.read_text()) == {
        "class": "hosted_wire",
        "manifest_sha256": hashlib.sha256(head).hexdigest(),
        "baseline_tag": BASELINE,
        "baseline_manifest_sha256": BASELINE_DIGEST,
        "cases": ["web_search"],
        "differences": {"web_search": ["upstream request bytes"]},
    }
    # The same engine on both sides, or a comparison that cannot reach a verdict, records nothing.
    assert gate.main(["--base-manifest", str(tmp_path / "head.json"), "--head-manifest", str(tmp_path / "head.json"),
                      "--evidence-output", str(tmp_path / "same.json")]) == 1
    assert not (tmp_path / "same.json").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--base-manifest", "base.json"],
        ["--base-manifest", "base.json", "--head-manifest", "head.json"],
        ["--base-manifest", "base.json", "--head-manifest", "head.json", "--evidence-output", "e.json", "--pull-request", "7"],
        ["--base", "HEAD", "--head-manifest", "head.json"],
        ["--base", "HEAD", "--base-manifest", "base.json"],
        [],
    ],
)
def test_the_two_hosted_wire_modes_do_not_mix(arguments) -> None:
    with pytest.raises(SystemExit):
        gate.main(arguments)



def test_released_context_skips_bundled_manifests_before_deserialization(tmp_path) -> None:
    released = tmp_path / "released"
    _write(released / "minimum.json", AVIBE["minimum"].__dict__)
    _write(released / "current_stable.json", AVIBE["current_stable"].__dict__)
    _write(released / "bundled-manifest-model-hub-engine-v8.0.63-1.json", {"not": "a released Avibe record"})
    loaded = qualification._load_released_directory(released)
    assert set(loaded) == set(AVIBE)

    candidate_dir = _record(tmp_path / "records")
    candidate = qualification.load_candidate(candidate_dir)
    evidence_dir = tmp_path / "evidence"
    for index, document in enumerate(_evidence(candidate)):
        if document["class"] == "build":
            artifact_dir = evidence_dir / "untrusted-rehearsal-evidence-build"
        elif document["class"] == "platform_smoke":
            artifact_dir = evidence_dir / f"untrusted-rehearsal-evidence-platform-smoke-{document['platform']}"
        elif document["class"] == "hosted_wire":
            artifact_dir = evidence_dir / "untrusted-rehearsal-evidence-hosted-wire"
        else:
            artifact_dir = evidence_dir / f"untrusted-rehearsal-evidence-released-avibe-e2e-{document['role']}"
        _write(artifact_dir / f"{index}.json", document)
    released_dir = tmp_path / "released"
    for role, released in AVIBE.items():
        _write(released_dir / f"{role}.json", released.__dict__)
    baselines = tmp_path / "baselines.json"
    baselines.write_text(json.dumps(BASELINES))
    output = tmp_path / "untrusted_rehearsal.json"
    assert qualification.main([
        "untrusted-rehearsal", "--candidate-dir", str(candidate_dir),
        "--evidence-dir", str(evidence_dir), "--released-dir", str(released_dir),
        "--baselines", str(baselines), "--run-id", str(RUN_ID), "--run-attempt", str(ATTEMPT),
        "--head-sha", HEAD, "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text())["result"] == "untrusted_rehearsal"


def test_missing_failed_or_misbound_evidence_leaves_the_candidate_unqualified(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: concrete failures remain observations, never approval."""
    candidate = qualification.load_candidate(_record(tmp_path))
    evidence = _evidence(candidate)
    evidence[0]["manifest_sha256"] = "0" * 64
    result = qualification.build_untrusted_rehearsal(
        candidate, evidence, baselines=BASELINES, avibe=AVIBE, run_id=RUN_ID,
        run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )
    assert result["result"] == "untrusted_rehearsal"
    assert any(problem.startswith("build:") for problem in result["observations"])


def test_future_backward_state_and_lifecycle_reports_are_not_universal_receipt_blocks(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    produced_today = [item for item in _evidence(candidate) if item["class"] not in {"backward_state", "lifecycle"}]
    assert _problems(candidate, produced_today) == []
    result = qualification.build_untrusted_rehearsal(
        candidate, produced_today, baselines=BASELINES, avibe=AVIBE,
        run_id=RUN_ID, run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )
    assert result["result"] == "untrusted_rehearsal"
    assert not any(problem.startswith(("backward_state:", "lifecycle:")) for problem in result["observations"])


def test_a_run_identity_mismatch_is_refused_even_when_the_untrusted_result_claims_success(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: GitHub run identity is checked independently of claims."""
    candidate = qualification.load_candidate(_record(tmp_path))
    result = _result(candidate)
    result["run_id"] = RUN_ID + 1
    with pytest.raises(QualificationError):
        qualification.verify_untrusted_rehearsal(result, candidate=candidate, api=GitHub(),
                                                 harness_sha256=HARNESS, baselines=BASELINES)


def test_a_receipt_github_does_not_confirm_is_refused_whatever_it_says_about_itself(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: catalog anchor for the renamed run-identity test."""
    test_a_run_identity_mismatch_is_refused_even_when_the_untrusted_result_claims_success(tmp_path)


def test_no_job_can_supply_the_evidence_classes_that_have_no_producer(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: catalog anchor for untrusted future observations."""
    test_untrusted_reports_do_not_gain_authority_from_artifact_or_job_names(tmp_path)


def test_untrusted_reports_do_not_gain_authority_from_artifact_or_job_names(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: absent future producers remain truthful observations."""
    candidate = qualification.load_candidate(_record(tmp_path))
    result = qualification.build_untrusted_rehearsal(
        candidate, [{"class": "lifecycle", "manifest_sha256": candidate.manifest_sha256, "result": "pass"}],
        baselines=BASELINES, avibe=AVIBE, run_id=RUN_ID, run_attempt=ATTEMPT,
        head_sha=HEAD, harness_sha256=HARNESS,
    )
    assert result["result"] == "untrusted_rehearsal"


def test_an_artifact_holding_evidence_its_job_does_not_produce_stops_the_receipt(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: catalog anchor for untrusted artifact parsing."""
    test_an_artifact_holding_untrusted_reports_is_parsed_as_data(tmp_path)


def test_an_artifact_holding_untrusted_reports_is_parsed_as_data(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: uploaded reports are parsed as untrusted data."""
    root = tmp_path / "evidence" / "untrusted-rehearsal-evidence-build"
    _write(root / "report.json", {"class": "lifecycle", "result": "pass"})
    assert qualification.collect_evidence(tmp_path / "evidence")[0]["class"] == "lifecycle"


def test_an_assembled_receipt_with_every_class_is_refused_when_it_is_made_and_when_it_is_checked(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: catalog anchor for the removed authority receipt."""
    test_an_assembled_authority_result_is_rejected_as_an_untrusted_rehearsal(tmp_path)


def test_an_assembled_authority_result_is_rejected_as_an_untrusted_rehearsal(tmp_path) -> None:
    """MH-ENGINE-CHANNEL-010: a rehearsal result never becomes an authority receipt."""
    candidate = qualification.load_candidate(_record(tmp_path))
    result = _result(candidate)
    result["result"] = "receipt"
    with pytest.raises(QualificationError):
        qualification.verify_untrusted_rehearsal(result, candidate=candidate, api=GitHub(),
                                                 harness_sha256=HARNESS, baselines=BASELINES)


def _workflow(path: str = ".github/workflows/model-hub-engine-qualification.yml") -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_candidate_workflow_is_untrusted_and_has_no_authoritative_receipt() -> None:
    workflow = _workflow()
    text = (ROOT / qualification.QUALIFICATION_WORKFLOW_PATH).read_text(encoding="utf-8")
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "model-hub-engine-untrusted-rehearsal" in text
    assert "untrusted_rehearsal" in text
    assert "qualification-receipt" not in text
    assert "EVIDENCE_PRODUCERS" not in text
    assert "secrets." not in text and "gh release" not in text and "s3api" not in text
    assert workflow["jobs"]["untrusted-rehearsal"]["if"] == "always()"
    assert workflow["jobs"]["untrusted-rehearsal"]["needs"] == ["build", "platform-smoke", "released-avibe-e2e", "hosted-wire"]
    hosted_wire_steps = workflow["jobs"]["hosted-wire"]["steps"]
    assert workflow["jobs"]["hosted-wire"]["if"] == "always()"
    released_record = next(step for step in workflow["jobs"]["released-avibe-e2e"]["steps"] if step.get("name") == "Upload the released Avibe record")
    assert released_record["if"] == "always()"
    baseline_upload = next(step for step in hosted_wire_steps if step.get("name") == "Upload the baseline list")
    assert baseline_upload["if"] == "always()"
    assert all(
        any(step.get("with", {}).get("persist-credentials") is False for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
        for job in workflow["jobs"].values()
    )


def test_trusted_channel_prepare_reads_untrusted_artifacts_and_uploads_new_trusted_namespace() -> None:
    workflow = _workflow(".github/workflows/model-hub-engine-channel.yml")
    text = (ROOT / ".github/workflows/model-hub-engine-channel.yml").read_text(encoding="utf-8")
    assert "rehearsal_run_id" in workflow[True]["workflow_dispatch"]["inputs"]
    assert workflow["jobs"]["prepare"]["name"] == "trusted prepare"
    assert "verify-untrusted-rehearsal" in text
    assert "model-hub-engine-untrusted-rehearsal" in text
    assert "model-hub-engine-trusted-assets" in text
    assert "model-hub-engine-untrusted-assets" in text
    assert "rehearsal-baselines" in text
    assert "Runtime pass/fail assertions above are rehearsal data" in text
    assert "REHEARSAL_RUN_ID: ${{ inputs.rehearsal_run_id }}" in text
    assert "persist-credentials: false" in text
    assert "secrets." not in json.dumps(workflow["jobs"]["prepare"])


def test_harness_digest_covers_the_candidate_workflow_and_shared_action() -> None:
    for name in qualification.HARNESS_FILES:
        (ROOT / name).is_file()
    workflow = (ROOT / qualification.QUALIFICATION_WORKFLOW_PATH).read_text(encoding="utf-8")
    invoked = set(re.findall(r"scripts/[A-Za-z0-9_]+\.(?:py|sh)", workflow)) | {qualification.ORIGINS_MODULE}
    invoked.discard("scripts/ci_scenario_evidence.py")
    assert invoked <= set(qualification.HARNESS_FILES)


def test_qualification_and_rehearsal_use_the_same_released_wheel_action() -> None:
    action = ".github/actions/model-hub-released-e2e"
    qualify = _workflow()["jobs"]["released-avibe-e2e"]
    rehearsal = _workflow(".github/workflows/model-hub-engine-released-e2e-rehearsal.yml")["jobs"]["rehearsal"]

    def shared(job: dict) -> dict:
        (step,) = [item for item in job["steps"] if item.get("uses") == f"./{action}"]
        return step["with"]

    assert shared(qualify)["tests-source"] == "released-src"
    assert shared(rehearsal)["tests-source"] == "${{ matrix.tests == 'tag' && 'released-src' || '.' }}"
    assert rehearsal["strategy"]["matrix"] == {"tests": ["tag", "head"]}
    scripts = "\n".join(step.get("run", "") for step in rehearsal["steps"])
    assert "rehearsal-record" in scripts and "--require-pass" in scripts
    assert "receipt" not in scripts and "evidence-" not in scripts
