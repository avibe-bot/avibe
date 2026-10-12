# CLI health owned by detection (#2257)

## Contract

A native CLI detection observes the selected executable, not merely its directory entry. The observation contains `found`, `path`, `status` (`ok`, `broken`, `missing`, `unknown`), `version`, and a closed safe `reason` code. `found` retains its presence meaning; only `status == ok` certifies runnability. The version belongs to that same successful, bounded `--version` observation. Non-native command discovery (npm, shells, etc.) retains presence semantics and does not impersonate native backend health.

The native backend identity is explicit on the detection request, so custom/draft paths are classified using the right CLI's version contract. Explicit existing paths are tested even when they lack execute permission. A draft is never judged using the saved path. An explicit native path that is missing stays missing: another healthy PATH installation cannot certify it. Bare command discovery still uses the existing resolver. Generic non-backend tool discovery is unchanged.

One detection owner runs probes using the existing supervised command runner, with output limits, timeout and owned-tree teardown. HTTP consumers await it natively; synchronous CLI/installer workers use its synchronous adapter, not a new per-request event-loop bridge. Initial detections/read-backs are uncached: replacing an npm platform package can change health without changing the launcher's stat or version-cache key.

Backend-specific version parsing accepts actual CLI formats, not an arbitrary semver in a crash trace. Nonzero exit, timeout, spawn failure, truncated/invalid output cannot certify health. Raw stdout/stderr is never a card diagnostic. CLI health remains separate from auth, provider/model routing, controller application state and overall entry eligibility. Built-in Vibey is not a CLI backend.

## Consumers

- `/api/cli/detect`: native identity + selected path -> authoritative observation.
- `/api/backend/<name>/runtime`: optional selected path -> the same observation, plus existing lifecycle/update information. No separate `runnable` verdict.
- Welcome, AgentDetection, SettingsBackendsPage, useBackendRuntime, BackendLifecycleChip, final onboarding entryGate and CLI doctor consume `status`, not `found`/`installed` as readiness. Native UI detection obtains the runtime projection once and shares that observation with its lifecycle chip; subsequent explicit refreshes carry the selected path and discard stale completions. Human unsaved path edits cannot mutate a different saved installation. Existing connection/source/application gates remain required.
- Install/upgrade: exit zero is only a candidate success. Freshly detect the resulting executable before persisting its path or announcing success. A missing/broken result fails. Existing app-private candidate verification/ownership and install locks remain in force.

## Repair

A healthy CLI keeps its ordinary native update route, but success is bound to the actual executable selected before invocation—even when configured by a bare name. A missing, non-executable or still-broken selected result fails without probing or persisting another installation. An upstream relocation without operation-specific attribution is verification failure, not permission for basename fallback. Codex's pre-existing npm `NPM_CONFIG_PREFIX` policy is an explicitly declared destination: both fresh installs and upgrades verify its platform launcher, never an old healthy CLI elsewhere. An existing selected alias is retained only if it still names the same file at that declared destination. This is not a general relocation framework. A broken Claude/OpenCode CLI cannot be its own repair tool. Determine supported existing installation provenance without executing that CLI; use its package manager or native bootstrap independently, preserving source and resulting path. Unsupported/ambiguous sources return a safe actionable failure rather than silently migrating the install. App-private desktop installs retain their installer and activation boundary.

### Native OpenCode support/refusal ledger

Only the positively identified canonical `~/.opencode/bin/opencode` installation uses its vendor bootstrap. Its installer environment is a copy with that directory (including equivalent directory entries) removed from PATH. `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS` and exported Bash functions are removed from that child environment only. A non-executing `command -v opencode` guard refuses any residual executable alias, wrapper, function or second installation **before network activity**; the same guard prefixes the bootstrap stream in the non-login/non-rc consumer shell. The official installer receives `--no-modify-path`. No shell configuration changes, renamed/deleted old executable, source migration, wrapper inspection, or new native artifact downloader is involved.

A residual resolution conflict is an actionable refusal, not an attempted repair. Even after exit zero, the exact selected destination must pass fresh detection under the **normal launch environment**, not the masked installer environment. Missing target, wrong output and timeout remain failure even if another healthy installation exists. Install-job success carries that validated observation; the restart fingerprint reuses its path/version rather than inventing an independent after-version verdict. Already activated app-private generations are not removed if final read-back fails.

### Supervised probe launch contract

`core.command_runner.run_supervised_command(hide_console=True)` is opt-in. It hides the Windows supervisor and supplies private `AVIBE_SUPERVISOR_HIDE_CONSOLE=1` to `core.watch_worker`, which consumes the instruction, hides the actual Windows command and does not forward the flag to that command. Default callers retain existing console behavior. Shared supervision owns output limits, timeout/cancellation and descendant cleanup. Mocked Win32 flag assertions are not a claim of native Windows acceptance.

Not included: npm residue cleanup, general install-source migration, generic health service, auth/routing rewrites, deployment, unrelated runtime/VM changes. Preserve #2452 startup diagnosis and input retry behavior.

## Producer-to-consumer boundary ledger

| Boundary | Authority and invariant | Regression evidence |
| --- | --- | --- |
| Explicit selection | Detection observes that path; missing stays missing without another-install fallback. | HTTP draft/saved separation; explicit and bare upgrade matrices. |
| Bare selection / presence | Runnable discovery keeps its priority; if none works, existence discovery includes the same supported standard, PATH and npm-global sources without requiring execute permission. | Non-executable custom npm prefix outside standard HOME/PATH remains `found/broken` and repairs its original prefix. |
| Probe execution | Shared supervised runner and stable existing cwd; backend version shape and normal/private launch environment. | Real valid CLI while the owned service subprocess's old cwd has been deleted; bounded timeout/cancellation and output. |
| UI path / intent | A request captures its requested path. A missing observation retains that path, not an old render closure. Revision and human-intent guards reject late completions. | Saved-missing initial load, explicit retry, and edit-during-probe tests. |
| Install/update result | Existing upgrades verify their preselected executable. An explicitly declared Codex npm prefix verifies its own output; an alias survives only if it names that artifact. | Missing/nonexec/broken/same-path success plus old-healthy/new-target-missing npm controls. |
| Auth/source/application | CLI health is an independent conjunction, not an auth verdict or an application receipt. Built-in backends have no CLI requirement. | Guarded auth harness supplies deterministic command-observation inputs; real HTTP projection refuses broken CLI and recovers at the same selection while other gates can still refuse. |
| Setup consumption | Wizard forwards the native backend identity; final entry gate consumes health plus the existing independent gates. | Wizard/entry-gate UI tests and cataloged HTTP auth/setup flow; not a claim of the pre-existing partial real-browser-to-real-API journey. |

The first review correction stays within these boundaries. No new cache, source migration, cwd policy, scenario framework or permission model is introduced. CI fixtures follow the actual async producer signatures without relaxing process/host-effect guards. Capability-specific backend relations are classified explicitly rather than excluded from the catalog-universe audit.

## Evidence required

Controlled real subprocesses cover valid backend versions, nonzero Node crash-tail versions, exit-zero wrong output, non-executable/missing paths, bounded output, timeout/descendants, replacements and draft-vs-saved paths. Installer consumers cover false-success read-back, healthy source-preserving update, and broken repair without invoking the damaged CLI. API/UI consumers cover broken state, stale completions and unchanged auth/entry gates. Focused tests, UI build and exact-head review/CI complete the gate; native-platform gaps are reported explicitly.
