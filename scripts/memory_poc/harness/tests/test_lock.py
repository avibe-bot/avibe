from __future__ import annotations

import tomllib
from pathlib import Path

from memory_poc.lock_check import active_lock_packages


def test_lock_pins_the_reviewed_everos_wheel() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = [item for item in payload["package"] if item["name"] == "everos"]

    assert len(packages) == 1
    assert packages[0]["version"] == "1.1.3"
    hashes = {item["hash"] for item in packages[0].get("wheels", [])}
    assert "sha256:f54086f9d4e52420eab70030dc8c92b76852c5b5e40d8f485226078f0f78fed0" in hashes


def test_lock_version_index_contains_the_harness_and_provider() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    versions = active_lock_packages(lock_path)

    assert versions["everos"] == "1.1.3"
    assert versions["memory-poc-harness"] == "0.1.0"


def test_active_lock_closure_includes_the_requested_provider_extra_and_dev_group() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"

    packages = active_lock_packages(lock_path)

    assert packages["memory-poc-harness"] == "0.1.0"
    assert packages["everos"] == "1.1.3"
    assert packages["uvicorn"] == "0.51.0"
    assert packages["httptools"] == "0.8.0"
    assert packages["pytest"] == "9.0.3"
