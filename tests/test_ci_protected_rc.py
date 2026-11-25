"""Regression coverage for exact native evidence. No GitHub writes or real release."""

from __future__ import annotations

import subprocess

import pytest

from scripts import ci_protected_rc as policy, ci_release_backstop as gate

SHA = "a" * 40
PARENT = "b" * 40
JOB = "desktop-shell / macos-26"
TAG = "gh-v3.2.8rc99"
GREEN = {"status": "completed", "conclusion": "success"}
CANCELLED = {"status": "completed", "conclusion": "cancelled", "runner_id": 0, "runner_name": "", "steps": []}


class ParentApi:
    def get(self, path):
        assert path == f"commits/{SHA}"
        return {"parents": [{"sha": PARENT}]}


def prove(monkeypatch, *, job=None, waiver="", other=None, parent=GREEN, files=("docs/guide.md",)):
    wanted = {JOB, "migration-release-guard"}
    monkeypatch.setattr(gate, "_checked_range", lambda *a: wanted)
    monkeypatch.setattr(
        gate,
        "_named_jobs",
        lambda _, sha: (
            {JOB: parent}
            if sha == PARENT
            else {
                **({JOB: job} if job is not None else {}),
                "migration-release-guard": GREEN if other is None else other,
            }
        ),
    )
    monkeypatch.setattr(policy, "touched", lambda base, tip: set(files))
    gate._verify_protected(ParentApi(), TAG, SHA, waiver)


def test_exact_evidence_or_single_unassigned_exception(monkeypatch):
    prove(monkeypatch, job=GREEN)
    prove(monkeypatch, job=CANCELLED, waiver=f"{JOB} | no runner | approved for this RC")
    prove(monkeypatch, job=GREEN, waiver=f"{JOB} | no runner | previously approved for this RC")


@pytest.mark.parametrize(
    "job", [None, CANCELLED, {"status": "completed", "conclusion": "skipped"}, {"status": "in_progress"}]
)
def test_missing_red_or_skipped_blocks_by_default(monkeypatch, job):
    with pytest.raises(gate.ReleaseBackstopError):
        prove(monkeypatch, job=job)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"job": None},
        {"waiver": f"{JOB} | reason without approval"},
        {"waiver": f"{JOB} |  | quoted go"},
        {"job": {**CANCELLED, "conclusion": "failure"}},
        {"job": {**CANCELLED, "runner_id": 451}},
        {"job": {**CANCELLED, "steps": [{"name": "start"}]}},
        {"other": CANCELLED},
        {"parent": CANCELLED},
        {"files": ("desktop/Cargo.lock",)},
        {"files": (".github/workflows/lint.yml",)},
        {"files": ("scripts/ci_protected_rc.py",)},
        {"waiver": "wrong job | explicit go"},
        {"waiver": f"{JOB} | "},
        {"waiver": f"{JOB} | go\nextra"},
    ],
)
def test_override_does_not_cover_any_other_case(monkeypatch, kwargs):
    with pytest.raises(gate.ReleaseBackstopError):
        prove(monkeypatch, **({"job": CANCELLED, "waiver": f"{JOB} | no runner | quoted go"} | kwargs))


def test_range_records_reverted_and_renamed_files(monkeypatch, tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.org")
    (tmp_path / "original").write_text("initial")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "desktop").mkdir()
    git("mv", "original", "desktop/risk.rs")
    git("commit", "-qm", "move")
    git("mv", "desktop/risk.rs", "original")
    git("commit", "-qm", "revert")
    candidate = git("rev-parse", "HEAD")
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    assert policy.touched(base, candidate) == {"original", "desktop/risk.rs"}
    assert policy.DESKTOP <= policy.required_jobs({"desktop/risk.rs"})
    assert policy.required_jobs({"docs/guide.md"}) == set()
    assert policy.required_jobs({'CODE_OF_CONDUCT.md'}) == set()
    assert policy.required_jobs({'docs/INSTALL_FOR_AI.md'}) >= policy.INSTALL
    assert policy.required_jobs({'docs/INSTALL_FOR_AI_ZH.md'}) >= policy.IDENTITY
    assert policy.required_jobs({'desktop/scripts/build-runtime-bundle.py'}) >= policy.FETCH
    mac = 'install-upgrade-regression (installer, macos-15)'
    assert not policy.inputs_changed(mac, {'ios/AvibeTests/Foo.swift', 'ios/Avibe.xcodeproj/project.pbxproj'})
    assert policy.inputs_changed(mac, {'ios/AvibeTests/Foo.swift', 'scripts/check.py'})
    assert policy.required_jobs({"new-build-input"}) == policy.ALL
