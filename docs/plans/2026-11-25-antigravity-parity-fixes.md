# Antigravity parity corrections

Owner-authorized backend lane for closed PR #4252. Compare with current Claude/Codex behavior; keep native managed execution and Avibe ownership of Skills, updates, routing and recovery. No real agy runs or service restarts. UX/docs is a separate writer on the same branch; rebase before pushing.

Each item is a separate commit with a regression at its owning boundary:

- A2/F9 correction: retain the askill `antigravity` target. askill installs the canonical copy in `.agents/skills` and links agent directories to it; Avibe reads that canonical copy. The Codex remapping was incorrect and is reverted. Verify discovery through the canonical copy while preserving Antigravity attribution.
- A4: Emit one completed/failed tool row, with readable paths, commands, bounded output and edit previews. Stream fixtures previously expected duplicate status rows.
- A5: Script installs participate in idle updates, settled release selection, version readback and failure restoration. Never restore agy's native background updater. State installer pinning limits explicitly.
- A6: Map an unavailable effort to the shared succession policy's offered value/default, report the effective choice, and permit models without an effort option. Previously unsupported selections failed the Turn.
  CLI model overrides clear an incompatible inherited effort with a warning, using the same route check as Session updates; explicitly requested incompatible efforts remain refused. Persist the cleared effort on both the reserved Session and queued Run, retain the Agent default, and cover Antigravity's live menu and Vibey's authoritative Hub menu through the CLI boundary.
- A7: Bundle the owner's agy 1.5.17 menu; preserve known empty effort lists and never guess efforts for unobserved live models.
- A8: Route incomplete results through standard failure/Retry; flush partial narration before RPC/transport failures. Previous tests accepted a failed text row without recovery affordances.
- A9: Managed live execution selects an advertised allow-once permission; malformed requests error cleanly. Passive clients do not acquire execution policy.
- A10: Summary fork creation visibly discloses summary-only history through localized Notice output; metadata alone was invisible.
- A11: A complete newest native end_turn wins a concurrent Stop and keeps the completed reply, including steered chains.

Final validation includes Antigravity, ACP, steering, registration, Session CLI and changed Skills/updater suites; changed-file Ruff. The PM owns real-provider probes after installation. No reopening or merging this PR.

Script-update guarantee: Avibe keeps the existing release settle window and idle reservation. Because `agy update` cannot pin its download, a healthy script executable is saved before updating and fresh readback must equal the selected settled version. Overshoot, broken readback or a failed command restores and verifies the previous executable before returning. Restoration failure reports failure and retains the saved binary for fix-forward. Managed agy processes continue disabling native automatic updates. Fresh/broken script bootstrap cannot promise a pinned release; it retains its explicit unsupported-version refusal rather than bypassing the settle rule.

Runtime reuse correction: consecutive Harness deliveries changed `AVIBE_RUN_ID` in the supposedly session-stable caller environment and forced `_specs_compatible` to replace agy, even though rules, cwd, auth, PATH, managed Skills and epoch matched. Drop Run/source in the shared fixed-environment projection used by Claude and Antigravity; the full context used by mutable Codex/OpenCode environments is unchanged. Retain session/native identity and trusted remote authority, and keep delivery provenance in normal per-Turn Harness metadata. A fake-peer regression checks one client/native session across distinct Runs and a callback, with run labels in the corresponding prompt; genuine launch-input changes still replace the runtime.
