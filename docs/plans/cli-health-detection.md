# CLI health owned by detection (#2257)

## Contract

A native CLI detection observes the selected executable, not merely its directory entry. The observation contains `found`, `path`, `status` (`ok`, `broken`, `missing`, `unknown`), `version`, and a closed safe `reason` code. `found` retains its presence meaning; only `status == ok` certifies runnability. The version belongs to that same successful, bounded `--version` observation. Non-native command discovery (npm, shells, etc.) retains presence semantics and does not impersonate native backend health.

The native backend identity is explicit on the detection request, so custom/draft paths are classified using the right CLI's version contract. Explicit existing paths are tested even when they lack execute permission. A draft is never judged using the saved path. Missing-path fallback, where the existing resolver supports it, must be returned as the observed path and used consistently by consumers.

One detection owner runs probes using the existing supervised command runner, with output limits, timeout and owned-tree teardown. HTTP consumers await it natively; synchronous CLI/installer workers use its synchronous adapter, not a new per-request event-loop bridge. Initial detections/read-backs are uncached: replacing an npm platform package can change health without changing the launcher's stat or version-cache key.

Backend-specific version parsing accepts actual CLI formats, not an arbitrary semver in a crash trace. Nonzero exit, timeout, spawn failure, truncated/invalid output cannot certify health. Raw stdout/stderr is never a card diagnostic. CLI health remains separate from auth, provider/model routing, controller application state and overall entry eligibility. Built-in Vibey is not a CLI backend.

## Consumers

- `/api/cli/detect`: native identity + selected path -> authoritative observation.
- `/api/backend/<name>/runtime`: optional selected path -> the same observation, plus existing lifecycle/update information. No separate `runnable` verdict.
- Welcome, AgentDetection, SettingsBackendsPage, useBackendRuntime and BackendLifecycleChip: consume `status`, not `found`/`installed` as readiness. Lifecycle version queries name the same path. Existing connection/source/application gates remain required.
- Install/upgrade: exit zero is only a candidate success. Freshly detect the resulting executable before persisting its path or announcing success. A missing/broken result fails. Existing app-private candidate verification/ownership and install locks remain in force.

## Repair

A healthy CLI keeps its ordinary native update route. A broken Claude/OpenCode CLI cannot be its own repair tool. Determine supported existing installation provenance without executing that CLI; use its package manager or native bootstrap independently, preserving source and resulting path. Unsupported/ambiguous sources return a safe actionable failure rather than silently migrating the install. App-private desktop installs retain their installer and activation boundary.

Not included: npm residue cleanup, general install-source migration, generic health service, auth/routing rewrites, deployment, unrelated runtime/VM changes. Preserve #2452 startup diagnosis and input retry behavior.

## Evidence required

Controlled real subprocesses cover valid backend versions, nonzero Node crash-tail versions, exit-zero wrong output, non-executable/missing paths, bounded output, timeout/descendants, replacements and draft-vs-saved paths. Installer consumers cover false-success read-back, healthy source-preserving update, and broken repair without invoking the damaged CLI. API/UI consumers cover broken state, stale completions and unchanged auth/entry gates. Focused tests, UI build and exact-head review/CI complete the gate; native-platform gaps are reported explicitly.
