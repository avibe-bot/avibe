# PR 3a implementation contract

Scope: Vibey only. Implements the Snapshot / replacement / Tiers / Pending notice / Persisted shape sections of `system-prompt-cache-tiers.md`; metrics follow after this PR merges.

Invariants:
- The cached system/tool prefix is a durable per-Session snapshot, compared by module id / instruction path. Unknown components are tier 2. Tier 1, a cold route, a route/model/Agent change, a checkpoint, or a 30-minute tier-2 deadline replaces the whole snapshot at Turn start only.
- Pending revisions and first-detected times persist separately; repeated edits do not extend a continuously pending tier-2 deadline. All optional-state failures degrade to fresh rendering, never block a Turn or recovery.
- Session identity travels in the input environment, never the Vibey system prompt. A fork copies its source snapshot at reservation. Native backend prompts and fork behavior remain unchanged.
- Pending names/revisions are send-on-change, without contents. Proven successful direct file writes are suppressed only for that exact revision; an outside edit remains visible.
- Filesystem work stays off the controller loop. Stored tools contain declarations, never routes' credentials. Live execution/refusal boundaries remain authoritative.

Primary validation boundaries:
- Pure snapshot decisions: each tier/free moment, deadline through restart, malformed snapshot and pending files, exact stable rendered bytes.
- Adapter integration: two Turns and an adapter restart, own versus external file revisions, pending/current environment deltas, real fork reservation and child/source prefix equality.
- Shared prompt regressions prove non-Vibey behavior unchanged; existing request-preview and checkpoint tests cover consumers.

No refresh UI/CLI, no database migration, no cache metrics in this PR, no deployment or service restart.
