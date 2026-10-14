"""Pinned Harbor driver: journal exact Compose ownership before environment start."""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from .remote_job import load, save


async def execute(root):
    if version("harbor") != "0.24.0":
        raise ValueError("this supervisor requires Harbor 0.24.0")
    from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
    from harbor.job import Job
    from harbor.models.job.config import JobConfig
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial

    config = JobConfig.model_validate(load(root / "config.json"))
    config.retry.max_retries = 0
    job = await Job.create(config)

    async def before_environment(event):
        # START is awaited before _prepare(), so this journal also covers the
        # network-only window. Use the pinned implementation's exact sanitizer
        # and verifier truncation/hash, not a prefix match on arbitrary labels.
        verifier = Trial._separate_verifier_session_id(SimpleNamespace(config=event.config), "trial")
        save(root / "ownership.json", {
            "schema_version": 1, "job_id": str(event.config.job_id),
            "trial_name": event.trial_name, "trial_id": str(event.trial_id),
            "projects": [_sanitize_docker_compose_project_name(f"{event.trial_name}__env"),
                         _sanitize_docker_compose_project_name(verifier)],
        })

    job.add_hook(TrialEvent.START, before_environment)
    await job.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        from harbor.utils.async_cleanup import with_async_cleanup
        asyncio.run(with_async_cleanup(execute(args.root)))
        return 0
    except Exception as exc:
        # No Rich local-variable dump and no upstream exception text. The host
        # log captures Harbor's own output through the supervisor's redactor.
        print(type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
