# Codex Prompt Delivery

## Contract

Avibe-owned instructions must enter model-visible developer messages independently
of model-owned collaboration text. A complete native baseline must survive
context reconstruction. Changed prompts on loaded threads use a tagged injected
snapshot without a replacement declaration. Tags delimit Avibe content; they do
not guarantee rule revocation, history cleanup, or latest-version retention. Unchanged
instructions must not accumulate on normal turns or process restart. Prompt
delivery must not change model routing or reasoning effort, and ambiguous native
mutations must retain at-most-once recovery.

## Cause and Delivery

Codex 0.153.2 prefers a model catalog's collaboration instructions over
`collaborationMode.settings.developer_instructions`. A successful
`collaborationMode/list` probe therefore proves API support, not prompt delivery.
Both quick-reply and session-title rules can be present in the turn parameters
while absent from the model request.

Avibe sets native `developerInstructions` at thread creation, fork, and resume,
preserving independently configured native instructions. A fresh thread records
its initial fingerprint without also injecting a copy. Resume and fork preserve
the last delivered fingerprint because restored history may still contain an
older baseline, even when the new native configuration was accepted.

Avibe uses `thread/inject_items` with explicit developer messages for changes on
loaded threads. Each new message contains the current Avibe content between
`<avibe_runtime_instructions>` tags, with no explanatory preamble. Prior messages
remain in native history. Removing a rule or catalog entry from the new snapshot
does not explicitly revoke its old occurrence; this is an intentional wording
choice, not an implemented replacement API. No user or project history is rewritten.
The existing durable `fallback` marker name and write-ahead states
remain unchanged. Legacy `collaboration` threads migrate through the existing
pending-clear path, even when their model and prompt fingerprint are unchanged.
Fingerprints cover the rendered snapshot, including its envelope.
Legacy raw-prompt hashes and earlier prose-bearing envelopes therefore migrate
once through resume or fork, while
unchanged current snapshots remain deduplicated across restarts.
Model and reasoning overrides remain top-level turn parameters.

All backends use the shared block builder to place Agent custom instructions
after Avibe guidance, capabilities, Agent/Skill catalogs, and session-title rules.
Only the Codex closing tag follows that final content block in a live overlay.
The source registry, debug export, and Studio use this same order. Empty custom
instructions add no block or separator; nonempty text is preserved verbatim after
a top-level `# Agent` heading and its boundary newlines, so an unheaded custom
prompt cannot inherit the preceding Avibe subsection. Other source blocks retain
their bytes and relative order.

Protocol validation rejections (`-32600`, `-32601`, `-32602`) restore the durable
pre-injection marker and fail the turn before model dispatch, allowing a later
attempt after API compatibility is repaired. Transport failures and internal
server errors remain ambiguous and retain the write-ahead marker. If restoring
the marker itself fails, the turn fails and that unresolved marker is retained.

`features.retain_client_developer_messages=true` makes these messages eligible
for budget-limited retention during Codex remote compaction v2. It does not pin
them: recent user history can exhaust the 64K retained-message budget and evict
an older injected snapshot. Native configuration, not that budgeted history,
provides the reconstructed baseline. This replaces the collaboration-based delivery
described in the earlier managed-Skills implementation plan; Skill discovery and
prompt composition are unchanged.

## Evidence and Limits

- Unit tests cover native start/resume/fork, one render per dispatch, configured
  instruction preservation, injection deduplication, changed instructions, model
  routing, legacy migration, persistence failures, ambiguous RPCs, and recovery.
- `tests/test_codex_prompt_delivery_contract.py` drives the real Codex executable
  through Avibe's agent and transport into a loopback Responses server. It uses
  the actual quick-reply and session-title templates and a model catalog that
  overrides collaboration text. It covers new and legacy threads, prompt changes,
  process restart, remote compaction v2, and automatic compaction with retained
  history exceeding the 64K budget. It explicitly demonstrates both native
  baseline survival and newer-overlay eviction.
- Run the native contract with `CODEX_PROMPT_CONTRACT_BINARY` set to the selected
  executable and pytest targeting that file. It is opt-in, requires no model
  credentials, and was verified with Codex 0.153.2. Ordinary CI runs the unit
  contract without installing a native backend.
- Neither the retention flag nor this hybrid path guarantees the latest prompt
  after compaction. A loaded thread retains its last native baseline; an injected
  update does not change it. Cold resume can promote new native configuration,
  but restored history remains unchanged until context reconstruction. That
  reconstruction can also retain an injected duplicate. Older compaction and
  third-party local summarization have additional limitations. See
  [the native-baseline contract](codex-native-prompt-baseline.md).
- This verifies transport delivery, not whether a real model follows every rule.
  Live Workbench button rendering and automatic title changes require the local
  Incus integration pass; no running developer service is restarted by this fix.

No existing scenario catalog owns prompt delivery. This change adds a native
contract rather than assigning an unrelated capability's scenario ID.

## Earlier Review Scope Decision

Head `22a948af70` had one finding: historical prompt snapshots lacked supersession.
Head `d96c18a115` had two findings: legacy-marker migration in that same class,
and definitive RPC rejections being treated as ambiguous mutations. The repeated
class triggered the orchestrator circuit breaker before the next edit. Inspection
covered marker production, resume, fork, cached recovery, and the native consumer
test. The smallest complete decision is to fingerprint actual snapshot bytes and
restore prior state only for protocol-level rejection. No new thread lifecycle,
marker schema, storage migration, backend routing, or broad retry policy is needed.

The September 10, 2026 owner-requested removal of the replacement declaration
supersedes only that wording requirement. Rendered-byte fingerprints and
at-most-once recovery remain necessary and unchanged. The new format applies on
future delivery; previously stored snapshots are not edited or removed.
