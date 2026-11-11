#!/usr/bin/env bash
set -euo pipefail
[[ $# == 2 ]] || { echo "usage: $0 <cargo-tree.txt> <cargo-metadata.json>" >&2; exit 2; }
exec python3 "$(dirname "$0")/check_features.py" "$1" "$2"
