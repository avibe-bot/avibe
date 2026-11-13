#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 <staging> <main-image> <debug-dylib-or-empty> <enable-debug-dylib>" >&2
  exit 2
fi

staging=$1
main_image=$2
debug_dylib=$3
producer_enabled=$4
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
checker="$script_dir/../../packaging/iroh-apple/check-private-apple-datapath.sh"

[[ "$producer_enabled" == YES || "$producer_enabled" == NO ]] || {
  echo "unsupported ENABLE_DEBUG_DYLIB value: $producer_enabled" >&2
  exit 1
}

python3 "$script_dir/prepare_iroh_app.py" verify-product \
  --staging "$staging" \
  --product "$main_image"

if [[ "$producer_enabled" == YES ]]; then
  [[ -n "$debug_dylib" ]] || {
    echo "ENABLE_DEBUG_DYLIB=YES has no configured Debug dylib input" >&2
    exit 1
  }
  [[ -f "$debug_dylib" ]] || {
    echo "configured iroh Debug dylib does not exist: $debug_dylib" >&2
    exit 1
  }
  bash "$checker" "$debug_dylib"
elif [[ -n "$debug_dylib" ]]; then
  echo "ENABLE_DEBUG_DYLIB=NO has an incompatible Debug dylib input: $debug_dylib" >&2
  exit 1
fi
