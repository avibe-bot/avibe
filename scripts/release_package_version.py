#!/usr/bin/env python3
"""Resolve an Avibe release tag to the package version used for builds."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence


_VERSION_PATTERN = re.compile(
    r"^(?P<release>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:[.-]?(?P<phase>a|b|rc|dev)(?P<phase_number>[0-9]+))?"
    r"(?:\.post(?P<post_number>[0-9]+))?$"
)


def package_version_from_release_tag(tag: str) -> str:
    normalized = tag.strip()
    if normalized.startswith("gh-v"):
        version = normalized.removeprefix("gh-v")
    elif normalized.startswith("v"):
        version = normalized.removeprefix("v")
    else:
        raise ValueError("release tag must start with 'v' or 'gh-v'")

    match = _VERSION_PATTERN.fullmatch(version)
    if not match or (match["phase"] == "dev" and match["post_number"] is not None):
        raise ValueError(f"release tag does not contain a supported package version: {tag!r}")
    # This bounded tag grammar is normalized without third-party dependencies:
    # the helper also runs before build dependencies have been installed.
    # Keep the original tag for release URLs, but use Hatch/PEP 440 spelling
    # for distribution versions and filenames.
    result = ".".join(str(int(part)) for part in match["release"].split("."))
    if match["phase"]:
        separator = "." if match["phase"] == "dev" else ""
        result += f"{separator}{match['phase']}{int(match['phase_number'])}"
    if match["post_number"] is not None:
        result += f".post{int(match['post_number'])}"
    if normalized.startswith("v") and normalized != f"v{result}":
        raise ValueError(
            f"official release tag must use canonical spelling v{result}; "
            "index-installed core resolves its Memory companion by that exact tag"
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Release tag, for example gh-v3.0.10rc1")
    args = parser.parse_args(argv)
    try:
        version = package_version_from_release_tag(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
