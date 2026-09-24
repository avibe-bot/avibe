#!/usr/bin/env python3
"""Build the metadata-only bridge required by released v3.1.0 updaters.

Release tooling only: never a core dependency or a feature implementation.
Keep shipping for every target release until old-updater support is retired.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile

from release_package_version import package_version_from_release_tag


def wheel_bytes(version: str) -> bytes:
    directory = f"avibe_memory-{version}.dist-info"
    members = {
        f"{directory}/METADATA": (
            "Metadata-Version: 2.1\nName: avibe-memory\n"
            f"Version: {version}\n"
            "Summary: Empty compatibility bridge for retired Avibe Memory updaters\n\n"
            "Memory was removed. Existing user data is left untouched.\n"
        ).encode(),
        f"{directory}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: avibe-retired-companion\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((f"{directory}/RECORD", "", ""))
    members[f"{directory}/RECORD"] = record.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--verify", action="store_true", help="Verify existing bytes without writing")
    args = parser.parse_args()
    version = package_version_from_release_tag(args.tag)
    path = args.output_dir / f"avibe_memory-{version}-py3-none-any.whl"
    expected = wheel_bytes(version)
    if args.verify or path.exists():
        if path.read_bytes() != expected:
            raise SystemExit(f"Inert bridge bytes differ: {path}; never overwrite a release artifact")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(expected)
    print(path)


if __name__ == "__main__":
    main()
