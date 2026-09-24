"""Release tooling must run from the released source, not from a dispatched branch."""

from __future__ import annotations

import re

from tests.test_desktop_release import workflow


TOOLING = (
    "scripts/desktop_release.py",
    "scripts/github_release.py",
    "scripts/release_package_version.py",
    "gh release",
)


def test_release_tooling_never_runs_from_an_unpinned_checkout():
    covered = set()
    for file in ("desktop-package.yml", "release_ai.yml"):
        jobs = workflow(file)["jobs"]
        for name, job in jobs.items():
            steps = job.get("steps", [])
            if not any(marker in item.get("run", "") for item in steps for marker in TOOLING):
                continue
            covered.add(name)
            for item in steps:
                if not str(item.get("uses", "")).startswith("actions/checkout@"):
                    continue
                ref = item.get("with", {}).get("ref", "")
                assert ref, (file, name, item.get("name"))
                for producer in re.findall(r"needs\.([\w-]+)\.outputs", ref):
                    if "if" not in jobs[producer]:
                        continue
                    # Skip propagation normally keeps a dependent off whenever its
                    # producer is gated out, but a job that opts out of it still runs
                    # and then reads that output as the empty string, which silently
                    # restores the unpinned default.
                    if re.search(r"\b(always|cancelled)\s*\(", str(job.get("if", ""))):
                        assert "||" in ref, (file, name, producer)
    assert covered == {"package", "resolve-desktop-release", "build-assets", "release"}
