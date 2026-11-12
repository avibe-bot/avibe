#!/usr/bin/env python3
"""Keep the standalone shell installer's stdlib resolver byte-identical."""
from pathlib import Path
import argparse

ROOT = Path(__file__).resolve().parents[1]
START = "# BEGIN GENERATED RELEASE DEPENDENCIES\n"
END = "# END GENERATED RELEASE DEPENDENCIES\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "install.sh"
    text = path.read_text(encoding="utf-8")
    source = (ROOT / "vibe/release_dependencies.py").read_text(encoding="utf-8")
    if "# GENERATED_RELEASE_DEPENDENCIES\n" in text:
        expected = text.replace("# GENERATED_RELEASE_DEPENDENCIES\n", START + source + END)
    else:
        before, rest = text.split(START)
        _, after = rest.split(END)
        expected = before + START + source + END + after
    if args.check:
        if text != expected:
            raise SystemExit("Run python scripts/sync_install_dependencies.py")
    else:
        path.write_text(expected, encoding="utf-8")


if __name__ == "__main__":
    main()
