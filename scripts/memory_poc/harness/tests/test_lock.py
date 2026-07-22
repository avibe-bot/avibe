from __future__ import annotations

import tomllib
from pathlib import Path


def test_lock_pins_the_reviewed_everos_wheel() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "uv.lock"
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = [item for item in payload["package"] if item["name"] == "everos"]

    assert len(packages) == 1
    assert packages[0]["version"] == "1.1.3"
    hashes = {item["hash"] for item in packages[0].get("wheels", [])}
    assert "sha256:f54086f9d4e52420eab70030dc8c92b76852c5b5e40d8f485226078f0f78fed0" in hashes
