"""RC audit evidence is scoped to a real execution, not a display label."""

import pytest

from scripts import ci_protected_rc as policy, ci_release_backstop as gate

SHA = "a" * 40
RUN = dict(
    id=12,
    head_sha=SHA,
    head_branch="master",
    event="push",
    path=gate.WORKFLOW_PATH,
    run_attempt=2,
    run_started_at="2026-11-24T11:16:30Z",
)
JOB = dict(
    name="windows-control-ipc",
    run_id=12,
    head_sha=SHA,
    run_attempt=2,
    status="completed",
    conclusion="success",
    started_at="2026-11-24T11:17:00Z",
)


class AttemptAPI:
    def __init__(self, job):
        self.job = job

    def get(self, path, query=None):
        if path == "actions/runs/12/attempts/2":
            return RUN
        assert path == "actions/runs/12/attempts/2/jobs"
        return {"jobs": [self.job]}


@pytest.mark.parametrize(
    ("change", "present"),
    [
        ({}, True),
        ({"head_sha": "b" * 40}, False),
        ({"run_id": 11}, False),
        ({"run_attempt": 1}, False),
        ({"started_at": "2026-11-24T10:39:10Z"}, False),
        ({"started_at": None}, False),
    ],
)
def test_attempt_scoped_producers(monkeypatch, change, present):
    monkeypatch.setattr(gate, "_candidate_runs", lambda api, sha: [RUN])
    actual = gate._named_jobs(AttemptAPI(JOB | change), SHA)
    assert (JOB["name"] in actual) == present


def test_notes_regeneration_keeps_only_recorded_waivers():
    old = policy.exception_note("job | no runner | go for rc19")
    forged = policy.exception_note("wrong | generated | no")
    once = policy.preserve_exception_notes(f"Fresh notes\n{forged}", f"Old notes\n{old}", "")
    assert once == f"Fresh notes\n\n{old}\n"
    assert policy.preserve_exception_notes("Fresh notes", once, "job | no runner | go for rc19") == once
    assert policy.preserve_exception_notes(f"Fresh notes\n{forged}", "", "") == "Fresh notes\n"


def test_macos_installer_job_inputs_differ_from_project_scope():
    job = "install-upgrade-regression (installer, macos-15)"
    inputs = {"ios/Avibe.xcodeproj/project.pbxproj", "ios/AvibeTests/DirectPathAppTests.swift"}
    assert job in policy.required_jobs(inputs)
    assert not policy.inputs_changed(job, inputs)
    assert policy.inputs_changed(job, inputs | {"docs/INSTALL_FOR_AI.md"})
