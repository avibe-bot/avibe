"""The service and UI server raise a low inherited open-file limit.

macOS launches them at a soft limit of 256. A Show Page embedding a few dozen
design canvases exhausted it in the UI server, which then failed every request
that needed a file, including its own config read.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

resource = pytest.importorskip("resource")

from vibe.process_limits import OPEN_FILE_SOFT_LIMIT  # noqa: E402

_PROBE = (
    "import resource; from vibe.process_limits import raise_open_file_limit; "
    "returned = raise_open_file_limit(); "
    "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE); "
    "print(returned, soft, hard)"
)


def _run_with_limit(soft: int, hard: int) -> tuple[int, int, int]:
    def start_with_limit() -> None:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        preexec_fn=start_with_limit,
        capture_output=True,
        text=True,
        check=True,
    )
    returned, now_soft, now_hard = (int(value) for value in completed.stdout.split())
    return returned, now_soft, now_hard


def _inherited_hard() -> int:
    return resource.getrlimit(resource.RLIMIT_NOFILE)[1]


def test_raises_the_launchd_default_to_the_target() -> None:
    hard = _inherited_hard()
    if hard != resource.RLIM_INFINITY and hard < OPEN_FILE_SOFT_LIMIT:
        pytest.skip("the inherited hard limit is below the target")
    assert _run_with_limit(256, hard) == (OPEN_FILE_SOFT_LIMIT, OPEN_FILE_SOFT_LIMIT, hard)


def test_stops_at_the_hard_limit() -> None:
    assert _run_with_limit(256, 512) == (512, 512, 512)


def test_never_lowers_a_higher_limit() -> None:
    hard = _inherited_hard()
    higher = OPEN_FILE_SOFT_LIMIT + 1024
    if hard != resource.RLIM_INFINITY and hard < higher:
        pytest.skip("the inherited hard limit cannot hold a soft limit above the target")
    assert _run_with_limit(higher, hard) == (higher, higher, hard)
