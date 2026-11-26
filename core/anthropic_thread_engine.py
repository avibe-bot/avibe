"""Local gateway metadata for measured Anthropic creates; never provider headers or JSON."""

import re

HEADER = "x-avibe-thread-engine"


def version(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9]+)?", value):
        return value
    return None


def launched_version(release: object) -> str | None:
    prefix = "model-hub-engine-v"
    return version(release[len(prefix) :]) if isinstance(release, str) and release.startswith(prefix) else None
