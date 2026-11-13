#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <staging> <main-image> [debug-dylib]" >&2
  exit 2
fi

staging=$1
main_image=$2
debug_dylib=${3:-}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
checker="$script_dir/../../packaging/iroh-apple/check-private-apple-datapath.sh"

python3 "$script_dir/prepare_iroh_app.py" verify-product \
  --staging "$staging" \
  --product "$main_image"

if [[ -n "$debug_dylib" ]]; then
  [[ -f "$debug_dylib" ]] || {
    echo "configured iroh Debug dylib does not exist: $debug_dylib" >&2
    exit 1
  }
  bash "$checker" "$debug_dylib"
fi
