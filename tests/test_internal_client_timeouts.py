"""Transport deadlines must outlast the operations they wait on."""

import re
from pathlib import Path

from vibe.model_hub_client import MODEL_HUB_RPC_TIMEOUT_SECONDS
from vibe.runtime import SERVICE_SLOW_START_TIMEOUT_SECONDS


def test_dependency_poller_outlasts_every_install_transport() -> None:
    source = (Path(__file__).resolve().parents[1] / "ui/src/context/ApiContext.tsx").read_text()
    match = re.search(r"startAndPollDependencyInstall[\s\S]{0,600}?Date\.now\(\) \+ ([\d_]+)", source)
    assert match, "could not locate dependency install poll deadline"
    budget = int(match.group(1).replace("_", "")) / 1000
    assert SERVICE_SLOW_START_TIMEOUT_SECONDS + MODEL_HUB_RPC_TIMEOUT_SECONDS < budget
