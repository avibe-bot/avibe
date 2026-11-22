"""Prove the normal release compiles and the authentication test peer cannot.

Run after `npm run build` in desktop/. No package is installed or published.
Honors CARGO_TARGET_DIR / CARGO_BUILD_JOBS so lanes can reuse their build cache.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GUARD = "app-lock-e2e authentication substitutes must never ship in release builds"
BASE = [
    "cargo", "check", "--manifest-path", str(ROOT / "Cargo.toml"),
    "-p", "avibe-desktop", "--lib", "--release",
]


def main() -> int:
    normal = subprocess.run(BASE, cwd=ROOT, check=False)
    if normal.returncode:
        return normal.returncode
    rejected = subprocess.run(
        [*BASE, "--features", "app-lock-e2e"], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if rejected.returncode == 0 or GUARD not in rejected.stdout:
        print(rejected.stdout, file=sys.stderr)
        print("FAIL: the release must reject the native authentication substitute", file=sys.stderr)
        return 1
    print("PASS: normal release compiles; release with app-lock-e2e is rejected by the explicit build gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
