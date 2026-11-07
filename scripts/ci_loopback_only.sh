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
  # A command that lost the caller's environment reads root's HOME and fails
  # in ways that look like test bugs, so a sudo that dropped it stops here.
  if [ "$HOME" != "${CI_LOOPBACK_ONLY_HOME:-}" ]; then
    echo "sudo did not keep the caller's environment (HOME is $HOME); refusing to run the command." >&2
    exit 1
  fi
  unset CI_LOOPBACK_ONLY_HOME
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

# sudo resets the environment, and the command needs all of the caller's. A
# bare --preserve-env is ignored by sudo-rs (Ubuntu 26.04's sudo), so every
# exported variable is named: both sudo implementations keep a named list, and
# the runner's NOPASSWD: ALL rule permits it. Only names reach argv; values,
# tokens included, stay in the environment. sudo still resets PATH
# (secure_path), so the caller's toolchain PATH is set again explicitly.
export CI_LOOPBACK_ONLY_HOME="$HOME"
exec sudo --preserve-env="$(compgen -e | paste -sd, -)" env "PATH=$PATH" unshare --net -- \
  bash "$0" --inside "$(id -u)" "$(id -g)" "$@"
