#!/usr/bin/env python3
"""Gate a TestFlight build on its exact source commit (ios-testflight.yml).

A commit may be built, signed and uploaded only when:

- it is named by its full 40-character SHA;
- it is on ``master`` (master's head, or an ancestor of it);
- its full master backstop is green, by the same rule a release tag uses
  (``scripts/ci_release_backstop.py``): a master ``lint`` run for exactly that
  SHA (``push`` or ``workflow_dispatch``) whose latest attempt concluded
  ``success`` with ``master-full-backstop`` green. A cancelled or unfinished
  run never counts; a run whose failed jobs were run again green counts;
- that run's ``ios-shell / simulator`` job, as last run, concluded ``success``:
  the backstop implies it, and the gate states it.

There is no exception path. The gate prints the run and attempt it trusted and
fails closed on anything it cannot read.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

try:
    from scripts import ci_release_backstop as backstop
except ImportError:  # run as `python scripts/testflight_source_gate.py`
    import ci_release_backstop as backstop

IOS_JOB = "ios-shell / simulator"
INPUT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class TestFlightSourceError(RuntimeError):
    """The commit may not be built for TestFlight."""


@dataclass(frozen=True)
class SourceProof:
    source_sha: str
    run_id: int
    run_attempt: int
    event: str
    ios_job_id: int

    def line(self) -> str:
        return (
            f"TestFlight source {self.source_sha} verified: it is on master; full master backstop "
            f"run {self.run_id} attempt {self.run_attempt} (event {self.event}) succeeded, "
            f"and its {IOS_JOB} job {self.ios_job_id} succeeded."
        )


def check_input(value: str) -> str:
    if not isinstance(value, str) or INPUT_RE.fullmatch(value.strip()) is None:
        raise TestFlightSourceError(
            f"source_sha must be a full 40-character commit SHA, got {value!r}"
        )
    return value.strip().lower()


def prove(api: backstop.GitHubApi, value: str) -> SourceProof:
    sha = check_input(value)
    try:
        backstop._require_on_master(api, sha)
    except backstop.ReleaseBackstopError as error:
        # The backstop words this for an RC; an unreadable or unknown commit keeps its reason.
        reason = "" if "is not on" in str(error) else f" ({error})"
        raise TestFlightSourceError(f"{sha} is not on master{reason}") from None
    try:
        proof = backstop.prove_backstop_sha(api, source_sha=sha)
        jobs = backstop._paged(api, f"actions/runs/{proof.run_id}/jobs", query={"filter": "latest"})
    except backstop.ReleaseBackstopError as error:
        raise TestFlightSourceError(str(error)) from None
    ios = [job for job in jobs if job.get("name") == IOS_JOB]
    if len(ios) != 1:
        raise TestFlightSourceError(
            f"run {proof.run_id} for {sha} has {len(ios)} {IOS_JOB!r} jobs; exactly one must be green"
        )
    job = ios[0]
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise TestFlightSourceError(
            f"run {proof.run_id} for {sha}: {IOS_JOB} is {job.get('status')}/{job.get('conclusion')}, not success"
        )
    job_id = job.get("id")
    return SourceProof(
        source_sha=sha,
        run_id=proof.run_id,
        run_attempt=proof.run_attempt,
        event=proof.event,
        ios_job_id=job_id if isinstance(job_id, int) else 0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--source-sha", default=os.environ.get("SOURCE_SHA", ""))
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    args = parser.parse_args(argv)
    try:
        sha = check_input(args.source_sha)
        api = backstop.GitHubApi(args.repository, args.token)
        proof = prove(api, sha)
    except (TestFlightSourceError, backstop.ReleaseBackstopError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    print(proof.line())
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"source_sha={proof.source_sha}\nrun_id={proof.run_id}\nrun_attempt={proof.run_attempt}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
