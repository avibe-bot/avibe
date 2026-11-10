#!/usr/bin/env bash
set -euo pipefail

expect_hit=0
if [[ ${1:-} == '--expect-hit' ]]; then
  expect_hit=1
  shift
fi
if [[ $# -lt 1 ]]; then
  echo "usage: $0 [--expect-hit] <xcframework-or-product> [...]" >&2
  exit 2
fi

hits=0
candidates=0

for root in "$@"; do
  [[ -e "$root" ]] || { echo "path does not exist: $root" >&2; exit 1; }
done

while IFS= read -r -d '' path; do
  description=$(file -b "$path")
  case "$description" in
    *'Mach-O '*|*'archive'*) ;;
    *) continue ;;
  esac
  candidates=$((candidates + 1))

  nm_output=$(nm -a "$path" 2>&1 || true)
  symbol_output=$(printf '%s\n' "$nm_output" | grep -E '(^|[[:space:]])_?(sendmsg_x|recvmsg_x)($|[[:space:]])' || true)
  string_output=$(strings -a "$path" 2>/dev/null | grep -E 'sendmsg_x|recvmsg_x' || true)

  if [[ -n "$symbol_output" ]]; then
    hits=$((hits + 1))
    echo "forbidden symbol hit in $path"
    printf '%s\n' "$symbol_output"
  fi
  if [[ -n "$string_output" ]]; then
    hits=$((hits + 1))
    echo "forbidden string hit in $path"
    printf '%s\n' "$string_output"
  fi
done < <(
  for root in "$@"; do
    if [[ -f "$root" ]]; then
      printf '%s\0' "$root"
    else
      find "$root" -type f -print0
    fi
  done
)

if [[ $candidates -eq 0 ]]; then
  echo "no Mach-O or archive candidates found under: $*" >&2
  exit 1
fi

if [[ $expect_hit -eq 1 ]]; then
  if [[ $hits -eq 0 ]]; then
    echo "positive control failed: forbidden private Apple datapath names were not found" >&2
    exit 1
  fi
  echo "positive control passed: found $hits forbidden symbol/string hit(s) in $candidates binary/archive candidate(s)"
  exit 0
fi

if [[ $hits -ne 0 ]]; then
  echo "private Apple datapath check failed: found $hits hit(s) in $candidates binary/archive candidate(s)" >&2
  exit 1
fi

echo "private Apple datapath check passed: scanned $candidates binary/archive candidate(s)"
