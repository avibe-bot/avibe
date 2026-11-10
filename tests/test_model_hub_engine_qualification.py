"""Engine candidate qualification (scripts/model_hub_engine_qualification.py).

What this protects: only a reviewed candidate record is qualified; a receipt
is complete only with every evidence class for those exact bytes; and the
finalizer believes GitHub about the producing run, never the receipt.
"""

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
    (directory / qualification.CANDIDATE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "asset_release_tag": tag,
                "min_avibe_version": minimum,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                **extra,
            }
        )
    )
    return directory


def _released(version: str) -> ReleasedAvibe:
    name = f"avibe_os-{version}-py3-none-any.whl"
    return ReleasedAvibe(
        version=version,
        tag=f"v{version}",
        wheel_name=name,
        wheel_url=f"https://github.com/avibe-bot/avibe/releases/download/v{version}/{name}",
        wheel_sha256=hashlib.sha256(version.encode()).hexdigest(),
        wheel_size=14_000_000,
    )


AVIBE = {"minimum": _released("3.2.6"), "current_stable": _released("3.2.7")}
PROVENANCE = {
    "tests_commit": "c" * 40, "dependencies_sha256": "d" * 64, "python": "Python 3.12.8",
    "node": "v22.23.4", "codex": "codex-cli 0.60.0", "opencode": "1.2.3",
}
BASELINES = {BASELINE: BASELINE_DIGEST}


def _evidence(candidate: qualification.Candidate) -> list[dict]:
    bound = {"manifest_sha256": candidate.manifest_sha256}
    documents = [{"class": "build", **bound, "assets": candidate.identity()["assets"], "reproduced": True}]
    for archive in candidate.spec.archives:
        documents.append(
            {
                "class": "platform_smoke", **bound, "platform": archive.platform,
                "archive_sha256": archive.sha256, "binary_sha256": archive.binary_sha256,
                "reported_version": candidate.spec.version, "health": "ok",
            }
        )
    documents.append(
        {
            "class": "hosted_wire", **bound, "baseline_tag": BASELINE, "baseline_manifest_sha256": BASELINE_DIGEST,
            "cases": ["web_search"], "differences": {},
        }
    )
    documents.append(
        {"class": "backward_state", **bound, "baseline_tag": BASELINE, "baseline_manifest_sha256": BASELINE_DIGEST,
         "result": "pass"}
    )
    for role, released in AVIBE.items():
        documents.append(
            {
                "class": "released_avibe_e2e", **bound, "role": role, "avibe": released.identity(),
                "imports_from_wheel": True, "selection": ["-m", "e2e_model_hub", "tests/e2e"],
                "provenance": dict(PROVENANCE),
                "junit": {"tests": 212, "failed": 0, "skipped": 0}, "scenario_evidence": "pass",
            }
        )
    documents.append({"class": "lifecycle", **bound, "result": "pass"})
    return documents


def _problems(candidate, evidence, **overrides) -> list[str]:
    return qualification.evidence_problems(candidate, evidence, **{"baselines": BASELINES, "avibe": AVIBE, **overrides})


# ----- the candidate record --------------------------------------------------


def test_a_candidate_record_names_exact_manifest_bytes_and_a_minimum_avibe(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path, hosted_wire_changes={BASELINE: "upstream renamed a header"}))

    assert candidate.asset_release_tag == TAG and candidate.min_avibe_version == "3.2.6"
    assert candidate.manifest_bytes == manifest_bytes(build=2)
    assert candidate.manifest_sha256 == hashlib.sha256(manifest_bytes(build=2)).hexdigest()
    assert len(candidate.identity()["assets"]) == 6
    assert candidate.hosted_wire_changes == {BASELINE: "upstream renamed a header"}


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
        pytest.param(
            lambda d: _relink(d),
            marks=pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs a privilege on Windows"),
        ),
    ],
)
def test_anything_but_exactly_one_consistent_record_is_refused(tmp_path, damage) -> None:
    directory = _record(tmp_path)
    damage(directory)
    target = directory if directory.exists() else directory.with_name("model-hub-engine-v8.0.63-9")

    with pytest.raises(QualificationError):
        qualification.load_candidate(target)


def _rewrite(directory: Path, **fields) -> None:
    path = directory / qualification.CANDIDATE_FILE
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def _relink(directory: Path) -> None:
    manifest = directory / qualification.MANIFEST_FILE
    elsewhere = directory.parent / "elsewhere.json"
    elsewhere.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(elsewhere)


def test_no_candidate_is_recorded_before_one_is_reviewed() -> None:
    # The directory documents the record; it holds no engine of its own.
    assert sorted(path.name for path in qualification.CANDIDATES_DIR.iterdir()) == ["README.md"]


# ----- evidence --------------------------------------------------------------


def test_complete_evidence_for_the_exact_bytes_makes_a_receipt(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))

    receipt = qualification.build_receipt(
        candidate, _evidence(candidate), baselines=BASELINES, avibe=AVIBE,
        run_id=RUN_ID, run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )

    assert receipt["candidate"] == candidate.identity()
    assert receipt["workflow_path"] == ".github/workflows/model-hub-engine-qualification.yml"
    assert (receipt["run_id"], receipt["run_attempt"], receipt["head_sha"]) == (RUN_ID, ATTEMPT, HEAD)
    assert receipt["avibe"]["minimum"]["wheel"]["sha256"] == AVIBE["minimum"].wheel_sha256


def test_the_evidence_classes_without_a_producing_job_block_every_receipt_today(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    produced_today = [item for item in _evidence(candidate) if item["class"] not in {"backward_state", "lifecycle"}]

    problems = _problems(candidate, produced_today)

    assert [problem.split(":")[0] for problem in problems] == ["backward_state", "lifecycle"]
    with pytest.raises(QualificationError, match="backward_state.*lifecycle"):
        qualification.build_receipt(
            candidate, produced_today, baselines=BASELINES, avibe=AVIBE,
            run_id=RUN_ID, run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
        )


def _drop(kind: str, **match):
    def apply(evidence: list[dict]) -> None:
        evidence[:] = [
            item for item in evidence
            if not (item["class"] == kind and all(item.get(key) == value for key, value in match.items()))
        ]
    return apply


def _change(kind: str, match: dict, **fields):
    def apply(evidence: list[dict]) -> None:
        for item in evidence:
            if item["class"] == kind and all(item.get(key) == value for key, value in match.items()):
                item.update(fields)
    return apply


@pytest.mark.parametrize(
    ("mutate", "blocked"),
    [
        (_drop("build"), "build"),
        (_change("build", {}, reproduced=False), "build"),
        (_change("build", {}, assets=[]), "build"),
        (_change("build", {}, manifest_sha256="0" * 64), "build"),
        (_drop("platform_smoke", platform="windows-arm64"), "platform_smoke"),
        (_change("platform_smoke", {"platform": "linux-arm64"}, health="timeout"), "platform_smoke"),
        (_change("platform_smoke", {"platform": "darwin-x64"}, binary_sha256="0" * 64), "platform_smoke"),
        (_change("platform_smoke", {"platform": "darwin-arm64"}, reported_version="v8.0.62"), "platform_smoke"),
        (_drop("hosted_wire"), "hosted_wire"),
        (_change("hosted_wire", {}, baseline_manifest_sha256="0" * 64), "hosted_wire"),
        (_change("hosted_wire", {}, cases=[]), "hosted_wire"),
        (_change("hosted_wire", {}, differences={"web_search": ["upstream request bytes"]}), "hosted_wire"),
        (_drop("released_avibe_e2e", role="minimum"), "released_avibe_e2e"),
        (_drop("released_avibe_e2e", role="current_stable"), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, imports_from_wheel=False), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, junit={"tests": 212, "failed": 1, "skipped": 0}), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, junit={"tests": 0, "failed": 0, "skipped": 0}), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, selection=["tests/e2e/test_model_hub_engine_wire.py"]), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, scenario_evidence="fail"), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, provenance={**PROVENANCE, "dependencies_sha256": ""}), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "current_stable"}, provenance=None), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "current_stable"}, avibe=_released("3.2.6").identity()), "released_avibe_e2e"),
        (_change("released_avibe_e2e", {"role": "minimum"}, avibe={**_released("3.2.6").identity(), "wheel": {"name": "x", "sha256": "0" * 64, "size_bytes": 1}}), "released_avibe_e2e"),
        (_drop("backward_state"), "backward_state"),
        (_change("backward_state", {}, result="fail"), "backward_state"),
        (_drop("lifecycle"), "lifecycle"),
        (_change("lifecycle", {}, result="skipped"), "lifecycle"),
        (_change("lifecycle", {}, manifest_sha256="0" * 64), "lifecycle"),
    ],
)
def test_missing_failed_or_misbound_evidence_leaves_the_candidate_unqualified(tmp_path, mutate, blocked) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    evidence = copy.deepcopy(_evidence(candidate))
    assert _problems(candidate, evidence) == []

    mutate(evidence)

    assert {problem.split(":")[0] for problem in _problems(candidate, evidence)} == {blocked}


def test_every_baseline_and_both_avibe_releases_must_be_covered(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    evidence = _evidence(candidate)
    other = "model-hub-engine-v8.0.60-4"

    wider = _problems(candidate, evidence, baselines={**BASELINES, other: "1" * 64})
    assert sorted(problem.split(":")[0] for problem in wider) == ["backward_state", "hosted_wire"]
    assert all(other in problem for problem in wider)
    # The candidate itself is never its own baseline.
    assert _problems(candidate, evidence, baselines={**BASELINES, TAG: candidate.manifest_sha256}) == []
    # A newer stable Avibe than the one the evidence used, or a minimum that is not the candidate's.
    assert _problems(candidate, evidence, avibe={**AVIBE, "current_stable": _released("3.2.8")}) != []
    assert _problems(candidate, evidence, avibe={"minimum": _released("3.2.7"), "current_stable": _released("3.2.7")}) != []
    assert _problems(candidate, evidence, avibe={"minimum": AVIBE["minimum"]}) != []


def test_a_hosted_wire_difference_qualifies_only_with_a_reviewed_reason_in_the_record(tmp_path) -> None:
    reviewed = qualification.load_candidate(
        _record(tmp_path, hosted_wire_changes={BASELINE: "upstream now forwards the citation header"})
    )
    evidence = _evidence(reviewed)
    _change("hosted_wire", {}, differences={"web_search": ["upstream header set"]})(evidence)

    assert _problems(reviewed, evidence) == []


# ----- the producing run -----------------------------------------------------


class GitHub:
    """What GitHub says about the run, the default branch and Avibe releases."""

    def __init__(self) -> None:
        self.run = {
            "id": RUN_ID, "run_attempt": ATTEMPT, "path": ".github/workflows/model-hub-engine-qualification.yml",
            "event": "workflow_dispatch", "head_branch": "master", "status": "completed", "conclusion": "success",
            "head_sha": HEAD, "repository": {"full_name": "avibe-bot/avibe"},
            "head_repository": {"full_name": "avibe-bot/avibe"},
        }
        self.comparison = {"status": "ahead"}
        self.jobs = [{"name": name, "conclusion": "success"} for name in qualification.REQUIRED_JOBS]
        self.total_jobs: int | None = None
        self.releases = {version: self._release(version) for version in ("3.2.6", "3.2.7")}
        self.listed = [{"tag_name": "v3.2.7"}, {"tag_name": "model-hub-engine-v8.0.63-1"}, {"tag_name": "v3.2.6"}]

    @staticmethod
    def _release(version: str) -> dict:
        released = _released(version)
        return {
            "tag_name": released.tag, "draft": False, "prerelease": False,
            "assets": [
                {"name": released.wheel_name, "size": released.wheel_size,
                 "digest": f"sha256:{released.wheel_sha256}", "browser_download_url": released.wheel_url},
                {"name": f"avibe_os-{version}.tar.gz", "size": 1, "digest": "sha256:" + "0" * 64,
                 "browser_download_url": "https://example.invalid/sdist"},
            ],
        }

    def __call__(self, path: str):
        prefix = "/repos/avibe-bot/avibe"
        if path == f"{prefix}/actions/runs/{RUN_ID}":
            return self.run
        if path.startswith(f"{prefix}/compare/") and path.endswith("...master"):
            return self.comparison
        if path == f"{prefix}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs?per_page=100":
            total = len(self.jobs or []) if self.total_jobs is None else self.total_jobs
            return {"total_count": total, "jobs": self.jobs}
        if path == f"{prefix}/releases?per_page=100":
            return [{"draft": False, "prerelease": False, **item} for item in self.listed]
        if path.startswith(f"{prefix}/releases/tags/v"):
            return self.releases.get(path.rsplit("/v", 1)[1])
        return None


def _receipt(candidate) -> dict:
    return qualification.build_receipt(
        candidate, _evidence(candidate), baselines=BASELINES, avibe=AVIBE,
        run_id=RUN_ID, run_attempt=ATTEMPT, head_sha=HEAD, harness_sha256=HARNESS,
    )


def _verify(candidate, receipt, github, **overrides) -> None:
    qualification.verify_receipt(
        receipt, candidate=candidate, api=github, **{"harness_sha256": HARNESS, "baselines": BASELINES, **overrides}
    )


def test_a_receipt_is_accepted_when_github_confirms_its_run_and_nothing_has_moved_since(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))

    _verify(candidate, _receipt(candidate), GitHub())


def _job(name: str, conclusion: str | None):
    def apply(github: GitHub) -> None:
        github.jobs = [job for job in github.jobs if job["name"] != name]
        if conclusion is not None:
            github.jobs.append({"name": name, "conclusion": conclusion})
    return apply


@pytest.mark.parametrize(
    "alter",
    [
        # Another workflow, or the same file dispatched from somewhere that was not reviewed.
        lambda g: g.run.update(path=".github/workflows/lint.yml"),
        lambda g: g.run.update(event="pull_request"),
        lambda g: g.run.update(event="push"),
        lambda g: g.run.update(head_branch="feature/fake-qualification"),
        lambda g: g.run.update(head_repository={"full_name": "attacker/avibe"}),
        lambda g: g.run.update(repository={"full_name": "attacker/avibe"}),
        # A branch or tag named like the default branch whose commit is not on it.
        lambda g: g.comparison.update(status="diverged"),
        lambda g: g.comparison.update(status="behind"),
        lambda g: setattr(g, "comparison", None),
        # A run that did not finish green, another attempt, or another commit than the receipt claims.
        lambda g: g.run.update(conclusion="failure"),
        lambda g: g.run.update(conclusion="cancelled"),
        lambda g: g.run.update(status="in_progress", conclusion=None),
        lambda g: g.run.update(run_attempt=ATTEMPT + 1),
        lambda g: g.run.update(head_sha="b" * 40),
        lambda g: setattr(g, "run", None),
        # A green run in which a required job did not actually succeed.
        _job("platform-smoke (windows-arm64)", "skipped"),
        _job("released-avibe-e2e (minimum)", "skipped"),
        _job("hosted-wire", "failure"),
        _job("receipt", "cancelled"),
        _job("build", None),
        _job("an extra job", "failure"),
        lambda g: setattr(g, "total_jobs", 40),
        lambda g: setattr(g, "jobs", None),
        # The Avibe releases as GitHub serves them now.
        lambda g: g.releases["3.2.6"]["assets"][0].update(digest="sha256:" + "f" * 64),
        lambda g: g.releases["3.2.6"].update(draft=True),
        lambda g: g.releases["3.2.7"].update(prerelease=True),
        lambda g: g.listed.insert(0, {"tag_name": "v3.2.8"}),
        lambda g: g.releases.pop("3.2.6"),
    ],
)
def test_a_receipt_github_does_not_confirm_is_refused_whatever_it_says_about_itself(tmp_path, alter) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    github = GitHub()
    github.releases["3.2.8"] = github._release("3.2.8")
    alter(github)

    with pytest.raises(QualificationError):
        _verify(candidate, _receipt(candidate), github)


@pytest.mark.parametrize(
    "forge",
    [
        lambda r: r.update(run_id=RUN_ID + 1),
        lambda r: r.update(run_id=str(RUN_ID)),
        lambda r: r.update(schema_version=2),
        lambda r: r.update(workflow_path=".github/workflows/lint.yml"),
        lambda r: r.update(harness_sha256="0" * 64),
        lambda r: r["candidate"].update(min_avibe_version="3.2.7"),
        lambda r: r["candidate"].update(manifest_sha256="0" * 64),
        lambda r: r.update(evidence=[item for item in r["evidence"] if item["class"] != "lifecycle"]),
        lambda r: r.update(evidence="all green"),
    ],
)
def test_a_receipt_that_was_edited_or_fabricated_is_refused(tmp_path, forge) -> None:
    candidate = qualification.load_candidate(_record(tmp_path))
    receipt = _receipt(candidate)
    forge(receipt)

    with pytest.raises(QualificationError):
        _verify(candidate, receipt, GitHub())


def test_a_receipt_stops_qualifying_when_the_record_harness_or_baselines_change(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path / "reviewed"))
    receipt = _receipt(candidate)
    other = qualification.load_candidate(_record(tmp_path / "other", minimum="3.2.7"))

    with pytest.raises(QualificationError, match="other candidate bytes"):
        _verify(other, receipt, GitHub())
    with pytest.raises(QualificationError, match="different qualification harness"):
        _verify(candidate, receipt, GitHub(), harness_sha256="n" * 64)
    with pytest.raises(QualificationError, match="model-hub-engine-v8.0.60-4"):
        _verify(candidate, receipt, GitHub(), baselines={**BASELINES, "model-hub-engine-v8.0.60-4": "1" * 64})


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


def test_a_run_that_imported_avibe_from_a_checkout_is_not_released_artifact_evidence(tmp_path) -> None:
    released = _released("3.2.6")
    environment = tmp_path / "venv"
    site = environment / "lib" / "python3.12" / "site-packages"
    wheel_origins = {
        "modules": {name: str(site / name / "__init__.py") for name in qualification.PRODUCT_PACKAGES},
        "distribution_version": "3.2.6",
        "vibe_version": "3.2.6",
    }
    assert qualification.wheel_origin_problems(wheel_origins, released=released, environment=environment) == []

    for forged in (
        {**wheel_origins, "modules": {**wheel_origins["modules"], "vibe": str(tmp_path / "checkout/vibe/__init__.py")}},
        {**wheel_origins, "modules": {**wheel_origins["modules"], "core": "None"}},
        {**wheel_origins, "vibe_version": "0.0.0.dev0"},
        {**wheel_origins, "distribution_version": "3.2.7"},
        {"distribution_version": "3.2.6", "vibe_version": "3.2.6"},
        None,
    ):
        assert qualification.wheel_origin_problems(forged, released=released, environment=environment) != []


def test_released_e2e_evidence_binds_the_downloaded_wheel_the_origins_and_the_report(tmp_path) -> None:
    candidate = qualification.load_candidate(_record(tmp_path / "records"))
    wheel = tmp_path / "avibe_os-3.2.6-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    released = ReleasedAvibe("3.2.6", "v3.2.6", wheel.name, "https://example.invalid", hashlib.sha256(b"wheel bytes").hexdigest(), 11)
    environment = tmp_path / "venv"
    origins = {
        "modules": {name: str(environment / "site-packages" / name / "__init__.py") for name in qualification.PRODUCT_PACKAGES},
        "distribution_version": "3.2.6", "vibe_version": "3.2.6",
    }
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite><testcase name="a"/><testcase name="b"><skipped type="pytest.xfail"/></testcase>'
        '<testcase name="c"><skipped type="pytest.skip"/></testcase></testsuite>'
    )
    arguments = {"role": "minimum", "released": released, "wheel": wheel, "environment": environment,
                 "origins": origins, "junit": junit, "scenario_evidence_passed": True,
                 "provenance": {**PROVENANCE, "runner": "not recorded"}}

    evidence = qualification.released_e2e_evidence(candidate, **arguments)

    assert evidence["junit"] == {"tests": 3, "failed": 0, "skipped": 1}
    assert evidence["provenance"] == PROVENANCE
    with pytest.raises(QualificationError, match="must record"):
        qualification.released_e2e_evidence(candidate, **{**arguments, "provenance": {"python": "Python 3.12.8"}})
    assert evidence["avibe"] == released.identity() and evidence["imports_from_wheel"] is True
    assert evidence["manifest_sha256"] == candidate.manifest_sha256
    wheel.write_bytes(b"other bytes")
    with pytest.raises(QualificationError, match="not the wheel GitHub published"):
        qualification.released_e2e_evidence(candidate, **arguments)


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
    # Every script the qualification workflow runs, and the workflow itself, is in the digest.
    workflow = (ROOT / qualification.QUALIFICATION_WORKFLOW_PATH).read_text(encoding="utf-8")
    invoked = set(re.findall(r"scripts/[A-Za-z0-9_]+\.(?:py|sh)", workflow))
    # The scenario-evidence gate runs from the released tag's own copy, which
    # the receipt binds through that run's recorded tag commit.
    invoked.discard("scripts/ci_scenario_evidence.py")
    assert invoked and invoked <= set(qualification.HARNESS_FILES)
    assert all((ROOT / name).is_file() for name in qualification.HARNESS_FILES)


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


# ----- the workflow ----------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load((ROOT / qualification.QUALIFICATION_WORKFLOW_PATH).read_text(encoding="utf-8"))


def test_the_required_jobs_are_exactly_the_jobs_the_qualification_workflow_runs() -> None:
    names = []
    for job in _workflow()["jobs"].values():
        matrix = (job.get("strategy") or {}).get("matrix")
        legs = [{}] if not matrix else matrix.get("include") or [{"role": role} for role in matrix["role"]]
        for leg in legs:
            name = job["name"]
            for key, value in leg.items():
                name = name.replace("${{ matrix.%s }}" % key, value)
            names.append(name)

    assert sorted(names) == sorted(qualification.REQUIRED_JOBS)
    smoke = _workflow()["jobs"]["platform-smoke"]["strategy"]["matrix"]["include"]
    assert {leg["platform"] for leg in smoke} == EXPECTED_PLATFORMS


def test_qualification_is_secret_free_default_branch_only_and_never_publishes() -> None:
    workflow = _workflow()
    text = (ROOT / qualification.QUALIFICATION_WORKFLOW_PATH).read_text(encoding="utf-8")

    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job and "environment" not in job for job in workflow["jobs"].values())
    assert "secrets." not in text and "gh release" not in text and "s3api" not in text
    assert workflow["jobs"]["build"]["if"] == "github.repository == 'avibe-bot/avibe' && github.ref == 'refs/heads/master'"
    # Every other job follows the build, so none runs for another ref either.
    assert all("build" in job["needs"] for name, job in workflow["jobs"].items() if name != "build")
    # The receipt needs every evidence job, so a skipped or failed one leaves no receipt.
    assert set(workflow["jobs"]["receipt"]["needs"]) == set(workflow["jobs"]) - {"receipt"}
    assert "if" not in workflow["jobs"]["receipt"]
