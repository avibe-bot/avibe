# Managed Dependency Reliability

## Problem

Avibe installs local dependencies through several implementations. Show
Runtime, Git Runtime, tmux, avault, and script-based installers currently use
different retry behavior and return different failure details. Doctor cannot
give one consistent answer when a dependency is missing or unreachable.

## Design

- Introduce one dependency network layer for bounded retries, URL redaction,
  reachability probes, and HTTP/DNS/TLS/timeout/local-I/O classification.
- Retry only transient failures: timeouts, DNS/network resets, HTTP 408/425/429,
  and HTTP 5xx. Do not retry missing assets, certificate failures, checksum
  failures, permissions, disk exhaustion, or unsupported platforms.
- Reuse the network layer from Show Runtime, managed Git Runtime, tmux, and
  avault. Script installers use the same bounded curl retry policy.
- Keep the bootstrap installers aligned with the same three-attempt bound for
  downloading uv before the Python dependency layer exists.
- Present askill, avault, Git Runtime, Show Runtime, tmux, and Node.js in one Doctor
  dependency group with stable codes and explicit repair targets. Node.js is
  diagnosed but remains a manual system dependency.
- Keep bare `vibe doctor repair` non-networking. Dependency downloads remain
  explicit targets so diagnosis never silently changes the machine.

## Verification

- Contract tests for retryable and terminal error classes, attempt counts,
  Retry-After behavior, and redacted structured details.
- Installer tests for each managed dependency plus fast/deep Doctor coverage.
- Existing Show Runtime, Git Runtime, tmux, local dependency, CLI, and UI API
  suites remain green.
