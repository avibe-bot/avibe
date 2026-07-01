# Agent Resource Governance MVP

## Background

Heavy agent work can saturate a tenant's CPU, memory, and file-backed cache/I/O
until Avibe's liveness path stops responding. Tenant-level Incus limits already
protect the host and neighboring tenants; the missing boundary is inside one
tenant between Avibe control-plane work and agent workload.

## Goal

First version: constrain the aggregate agent workload so Claude, Codex,
OpenCode, and their spawned shell/tool subprocesses cannot consume the whole
tenant budget. This is a best-effort Linux/cgroup v2 MVP. It must not break
non-Linux, non-systemd, or non-delegated installs; when Avibe cannot create a
real constrained child cgroup, it falls back to legacy process startup.

## Design

- Add a V2 runtime config block for resource governance, defaulting to `auto`.
- Create one shared `avibe-agents` cgroup under the current service cgroup.
- Set aggregate limits on that group:
  - `memory.high` as a soft throttle
  - `memory.max` as a hard agent-domain cap
  - lower `cpu.weight` / `io.weight`
  - `pids.max`
  - `memory.oom.group=1`
- Move backend runtime root processes into that shared group:
  - Codex `app-server`
  - OpenCode `serve`
  - Claude SDK-managed CLI process after connect
- Apply positive `oom_score_adj` to the backend root process so extreme pressure
  prefers killing agent work over Avibe.

## Non-goals

- Do not move Avibe itself into a protected cgroup in this PR.
- Do not hard-fail agent startup when cgroup setup is unavailable.
- Do not add UI controls yet; config can be file-driven.

## Follow-ups

- Add Avibe protected cgroup properties via service/scope startup.
- Add tenant-wide agent turn admission control.
- Add an Incus pressure regression that asserts `/health` remains responsive.
