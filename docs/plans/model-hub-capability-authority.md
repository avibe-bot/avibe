# Backend capability and limit authority audit

Lane B, Session `seshneu2u9cbw`, base
`76e269c9a9dfd3a6286ce0da81b77316ca4be38a`.

## Contract

Unknown custom capability metadata must not suppress supplied images or
reasoning. Explicit modality lists, `supports_reasoning: false`, and effort
lists retain their meanings. Native rows keep their own metadata. Custom rows
must not acquire an unrelated model's context, tools, service tier, or default
effort. Persisted BackendModel shapes and Avibe-owned model selection remain
unchanged.

## Authority and ambiguity inventory at the shared base

- BackendModel stores optional context/output numbers, optional capability
  booleans, and empty-by-default modality/effort lists. `origin` identifies the
  creation path for the whole row; it cannot distinguish an imported field
  from a later user edit. No existing per-field limit provenance exists.
- The UI's candidate path leaves limits and capabilities unstated. Its manual
  editor visibly starts with text input and reasoning disabled. A models.dev
  selection fills the editor; saving commits that full row. The backend saves
  those values literally and built-in reconciliation only adds missing rows.
  Therefore a saved non-null limit should remain the selected model's
  description, without trying to reconstruct how it was authored.
- Route resolution reads metadata by the requested BackendModel ID, before
  mapping it to the upstream target. This is intentional: an alias owns its
  planning metadata. Codex's authenticated turn gateway retains that alias as
  the runtime model, so the projected catalog row matches. Claude launches the
  target but carries the selected row's limits. OpenCode overlays use the
  public alias and selected row's limits.
- Claude overwrites both `CLAUDE_CODE_MAX_*_TOKENS` environment
  variables, in both Hub and native CLI launches. Hub also masks them as if
  they were credentials. Separately, `claude_settings_for_launch` copies the
  catalog limits into the highest-priority launch settings, so changing only
  the subprocess environment would still lose explicit choices. Project/local
  settings are enabled, user settings are disabled for Hub transport ownership.
  Native CLI retains all setting sources. Parent environment, settings env, and
  the saved model row can conflict; there is no provenance for ranking two
  user-authored descriptions. Credential/connection masking is independent.
- Codex projects the saved context into `context_window` and
  `max_context_window`, removing the old catalog compaction limit so it can be
  derived from the new context. Custom rows without a limit inherit no context.
  Launch arguments do not inject a context override. Native CLI launches do
  not consume this projected catalog. The real consumer checks below cover
  `model_context_window` and `model_auto_compact_token_limit`. BackendModel output limits have no
  Codex projection; silently claiming support or inventing a new output cap
  would be incorrect.
- OpenCode projects saved limits into `limit.context` / `limit.output` when
  present and otherwise omits them. The managed plugin replaces complete Hub
  provider definitions to protect authentication, so a same-name native
  provider cannot remain a second owner. Ordinary native provider definitions
  are separate. No new merge of arbitrary native provider data is proposed.
- Unknown input metadata is converted into `["text"]` only for
  custom Codex rows. Codex 0.153.2's protocol schema itself defaults omitted
  input modalities to text and image. Explicit nonempty lists can narrow that
  default without adding persisted state. Original-resolution image detail is
  a separate provider feature and should not be enabled from absence.
- Unknown reasoning is converted into a `none`-only list and default.
  Explicit false and explicit lists must remain distinct from absence.
- The UI model picker currently returns an empty effort menu for an unknown
  row, and the manual editor starts with explicit reasoning false. These are
  separate visible-product policies; this lane will not change them without
  coordination.

## Approved repair

The orchestrator approved the limit rule in integration commit `dc2d5f7bc` and
the Codex consumer follow-up in `a3065a7d1`. The shared plan remains owned by
the orchestrator.

- Preserve the persisted schema and requested alias's metadata authority.
- Unknown custom Codex input omits `input_modalities`, invoking the native
  text/image compatibility default, while original-resolution detail remains
  disabled. A nonempty modality list still narrows the accepted inputs.
- Unknown custom reasoning has the schema-required empty supported list and
  no default effort. Actual turns can still supply an effort. Explicit false
  retains `none`, explicit lists retain their selected/default efforts, and
  known native rows retain their own defaults.
- Claude catalog limits fill only absent subprocess variables, including
  preservation of explicitly empty strings. Only the two limit keys leave the
  credential tombstone set. Hub connection settings no longer promote
  catalog limits above native project/local settings. Credential masking and
  enabled settings sources are unchanged.
- Applying a saved Codex context now removes `max_context_window` instead of
  synthesizing an equal hard ceiling. The planning context remains and the
  obsolete native compaction threshold is removed as before. Without a saved
  context override, native model metadata remains unchanged.
- OpenCode needs no production change: the saved BackendModel remains the
  managed provider's authority. Alias/overlay tests confirm supplied limits
  and omitted metadata do not inherit an unrelated target row's values.

## Consumer evidence and limitations

- Codex 0.153.2 loaded a native context of 512,000 but originally reported only
  121,600 usable tokens with the projected 128,000 hard ceiling. The pinned
  source's `models-manager/src/model_info.rs::with_config_overrides` confirms
  the minimum operation. Removing the synthesized ceiling makes its actual
  token-usage consumer report 486,400 usable tokens, preserving 95% headroom.
  Smaller explicit windows and the catalog fallback also work.
- Two-turn tests feed real CLI responses with synthetic token usage. With the
  128,000 planning window, 150,000 tokens trigger compaction. With an explicit
  512,000 window and 300,000 threshold, 150,000 do not trigger compaction and
  350,000 do. The mock sees the extra compaction request and the app-server
  completes its compaction item and following turn.
- Actual Codex requests retain a valid inline PNG and non-ASCII text. Unstated
  effort adds no default; explicit `none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, and `max` reach the local endpoint. Negative image/reasoning choices
  and nonempty effort lists are exercised through the same consumer.
- Codex `ultra` is a native multi-agent mode: the pinned client's
  `reasoning_effort_for_request` maps it to a non-Ultra tier, falling back to
  medium for an empty ladder. This repair does not change that native
  interpretation or claim literal Ultra wire preservation. Native CLI tools
  may also have their own defaults independent of catalog projection.
- Claude SDK 0.2.93's bundled CLI preserves parent/project/local output limits
  in real Messages requests, for both Hub and native CLI paths. Its normal
  context planner does not consume `CLAUDE_CODE_MAX_CONTEXT_TOKENS` unless
  compaction is disabled. The repair preserves that value through SessionHandler
  into SDK options but does not claim to change native Claude context planning.
  Compaction is never disabled as a workaround.
- BackendModel output metadata has no Codex output-cap projection. No output
  cap, UI policy, schema, engine, gateway, credential, or routing-policy change
  is introduced. Engine-to-upstream preservation remains lane D's boundary.

All CLI tests use test-owned homes, allowlisted environments, synthetic tokens,
and loopback endpoints. External HTTP(S) is directed at a test-owned rejecting
proxy. On macOS, a separate OS sandbox denies non-loopback outbound sockets;
a representative forbidden connection was verified to return a permission
error. Claude nonessential traffic is explicitly disabled; Codex launches use
Avibe's existing analytics/update/host-feature disable settings.

## Validation results

- Baseline: 70 catalog/injection tests passed before the repair.
- Focused catalog, injection, and SessionHandler coverage: 201 passed.
- Related scenario/catalog/route/projection selection: 219 passed.
- Actual Codex/Claude consumers: 30 cases, including the three compaction
  boundaries above.
- Pinned Ruff 0.4.9 and Model Hub authority closure pass.
- These are implementation-head results, not evidence for a later PR head.
  The owner's 2026-09-09 independent-delivery decision assigns this lane its
  own PR, canonical CLI fixture registration, and durable review/CI Watch.
  MH-PROTOCOL-004 remains partial CLI-only evidence; MH-CLAUDE-LAUNCH-001 gains
  related launch-limit evidence without claiming managed-engine coverage.
  After normal landed-master integration, rerun focused tests, real isolated
  CLI consumers, authority/scenario validation, and pinned Ruff at the PR head.
  The orchestrator independently verifies all gates and executes any merge;
  no runtime update or publication is authorized.
