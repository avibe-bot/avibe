#!/usr/bin/env bash
# Network qualification of the avibe-direct-transport sidecar on a disposable
# Ubuntu host (a GitHub Actions runner). Creates two network namespaces, runs
# network_qualification.py inside one of them, and always deletes both.
#
#   run-network-qualification.sh --sidecar <binary> --peer-exe <test executable> \
#       --out <directory> [more network_qualification.py options]
#
# It changes kernel network state as root, so it refuses to run anywhere that is
# not GitHub Actions unless DTQ_ALLOW_PRIVILEGED=1 says the host is disposable.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "network qualification needs Linux network namespaces" >&2
  exit 2
fi
if [[ "${GITHUB_ACTIONS:-}" != "true" && "${DTQ_ALLOW_PRIVILEGED:-}" != "1" ]]; then
  echo "refusing to change network state: not GitHub Actions and DTQ_ALLOW_PRIVILEGED is not 1" >&2
  exit 2
fi
for tool in ip tcpdump python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "missing required tool: $tool (sudo apt-get install --yes iproute2 tcpdump python3)" >&2
    exit 2
  fi
done

if [[ "$(id -u)" -ne 0 ]]; then
  # The sidecar and the test executable run as the invoking user, not as root.
  exec sudo -n env "PATH=$PATH" "GITHUB_ACTIONS=${GITHUB_ACTIONS:-}" \
    "DTQ_ALLOW_PRIVILEGED=${DTQ_ALLOW_PRIVILEGED:-}" \
    "DTQ_RUN_AS=$(id -u):$(id -g)" bash "${BASH_SOURCE[0]}" "$@"
fi

out=""
arguments=("$@")
for ((index = 0; index < ${#arguments[@]}; index++)); do
  if [[ "${arguments[index]}" == "--out" ]]; then
    out="${arguments[index + 1]:-}"
  fi
done
[[ -n "$out" ]] || { echo "--out <directory> is required" >&2; exit 2; }

run_id="$(printf '%04x%04x' "$RANDOM" "$RANDOM")"
namespace="dtq-${run_id}"
sink="dtq-${run_id}-sink"

cleanup() {
  local status=$? name pid leftover=0
  trap - EXIT
  for name in "$namespace" "$sink"; do
    [[ -e "/run/netns/$name" ]] || continue
    # Only processes this run started can be inside a namespace named for it.
    for pid in $(ip netns pids "$name" 2>/dev/null || true); do
      kill -KILL "$pid" 2>/dev/null || true
    done
    # Deleting the namespace destroys its veth interfaces, addresses and routes.
    ip netns del "$name" 2>/dev/null || true
    if [[ -e "/run/netns/$name" ]]; then
      echo "CLEANUP FAILED: network namespace $name still exists" >&2
      leftover=1
    fi
  done
  if [[ -n "${DTQ_RUN_AS:-}" && -d "$out" ]]; then
    chown -R "$DTQ_RUN_AS" "$out" 2>/dev/null || true # let the workflow upload it
  fi
  if [[ "$leftover" -ne 0 && "$status" -eq 0 ]]; then
    status=70
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

ip netns add "$namespace"
ip netns add "$sink"

run_as=()
if [[ -n "${DTQ_RUN_AS:-}" ]]; then
  run_as=(--run-as "$DTQ_RUN_AS")
fi

# Not `exec`: the EXIT trap above must outlive the Python process.
ip netns exec "$namespace" env DTQ_INSIDE_NETNS=1 \
  python3 "$here/network_qualification.py" --sink-netns "$sink" "${run_as[@]}" "$@"
