#!/usr/bin/env bash
#
# Run a command, as the calling user, in a network namespace that has only loopback.
#
# The unit tests' in-process guard (tests/network_guard.py) sees only the
# pytest interpreter. A child process a test starts -- the vibe CLI, a worker
# interpreter, git, node, uv -- is outside it. In this namespace every
# descendant gets one answer: loopback works, and anything past it fails at
# once with "network unreachable".
#
# Linux only, with passwordless sudo, as on GitHub-hosted runners. Ubuntu
# restricts unprivileged user namespaces, and a user namespace would also run
# the command as a mapped root that file permissions do not stop, so the
# namespace is made as root and the command runs as the calling user again.
# Before it runs, a probe proves nothing past loopback answers.
#
# Usage: scripts/ci_loopback_only.sh <command> [args...]
set -euo pipefail

if [ "${1:-}" = "--inside" ]; then
  uid="$2"
  gid="$3"
  shift 3
  ip link set lo up
  # An address literal, so the probe tests the route, not only name resolution.
  if probe=$(curl -sS --max-time 5 -o /dev/null https://1.1.1.1 2>&1); then
    echo "This network namespace reaches past loopback; refusing to run the command in it." >&2
    exit 1
  fi
  echo "Loopback-only network namespace: a request past loopback fails with: $probe"
  exec setpriv --reuid="$uid" --regid="$gid" --init-groups -- "$@"
fi

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <command> [args...]" >&2
  exit 2
fi

# sudo resets PATH; the command needs the caller's toolchain on it.
exec sudo --preserve-env env "PATH=$PATH" unshare --net -- \
  bash "$0" --inside "$(id -u)" "$(id -g)" "$@"
