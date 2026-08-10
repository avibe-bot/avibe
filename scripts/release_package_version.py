#!/usr/bin/env python3
"""Resolve an Avibe release tag to the package version used for builds."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence


_VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:(?:[.-]?(?:a|b|rc|dev))[0-9]+)?"
    r"(?:\.post[0-9]+)?$"
)


def package_version_from_release_tag(tag: str) -> str:
    normalized = tag.strip()
    if normalized.startswith("gh-v"):
        version = normalized.removeprefix("gh-v")
    elif normalized.startswith("v"):
        version = normalized.removeprefix("v")
    else:
        raise ValueError("release tag must start with 'v' or 'gh-v'")

    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"release tag does not contain a supported package version: {tag!r}")
    return version


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
