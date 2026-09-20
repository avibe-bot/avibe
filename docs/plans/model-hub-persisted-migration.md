# Persisted native configuration migration

## Goal and scope

Migrate user-consented, persisted native authentication into Model Hub without
treating the controller's inherited environment or Hub launch overrides as
credentials to import or as blanket blockers. This fixes a reproduced regression
where all three already-Hub backends were disabled in the migration dialog.
No real credentials, native CLI, production-state testing, deployment, or restart
is authorized by this implementation task.

## Boundary contract

- Discovery reads persisted CLI configuration, existing supported native stores,
  and user shell startup files. Runtime authentication environment values are
  neither candidates nor credential suppliers. Environment variables which
  locate configuration directories remain path configuration, not credentials.
- Shell discovery never executes, sources, expands, or evaluates shell code.
  Support literal assignments in `.profile`, `.bash_profile`, `.bash_login`,
  `.bashrc`, `.zshenv`, `.zprofile`, `.zshrc`, and `.zlogin`. Quoted literal
  values, comments, Unicode, and CRLF must preserve their meaning and bytes.
  Dynamic credential expressions, control flow, command substitutions and
  ambiguous values cannot become guessed credentials; report a specific
  source and remediation, leaving all bytes untouched. Ordinary unrelated
  shell commands/includes are not credential candidates and are never
  followed. This is an inventory of direct persisted assignments, not an
  emulation of shell execution or a recursive scan of arbitrary scripts.
  The bounded writer surface is derived from the Bash 5.3 and Zsh 5.9 core
  command manuals/source, not from whichever branches already exist in the
  parser. It includes explicit named-output/declaration destinations, written
  code/expansion operands, and explicit target-bearing shell syntax (including
  named file-descriptor allocation and named Bash coprocs). Core aliases and
  command modifiers belong to that inventory too. A checked-in coverage ledger
  classifies every core command/alias and relevant syntax as a role handler,
  ordinary data/query, or an explicit exclusion, with primary source version/
  digest and independent positive/negative consuming cases. Tests must detect
  unclassified entries and missing role coverage.
  Classify argument roles, including attached/combined destination options,
  array destinations, documented default destinations and explicit arithmetic
  writes. Code, expansion-only text, destinations, data, queries and filenames
  are distinct roles. In particular, `source`/`.` take a filename and argv,
  not an embedded shell program; neither an assignment-shaped filename nor an
  argument is code, and the included file is never followed. Known callback
  bodies are inspected only as written; named functions and external content
  are not resolved. Output/query-only options do not write.
  Zsh numeric declarations (`integer`/`float`, `typeset -i/-E/-F`) can coerce
  an existing explicitly named value even without `=`; inventory those targets
  as dynamic, never as guessed numeric credentials. Ordinary attribute-only
  export/readonly declarations remain non-candidates. Written tied-declaration
  destinations are local evidence, not authority to follow a tie across
  commands. Written `emulate -c` bodies retain Zsh's core role inventory:
  emulation changes options, not the interpreter or builtin table. Do not
  reinterpret `emulate sh` as Bash/POSIX commands or simulate compatibility
  option state; it does not change the startup file's persistent dialect.
  Bash/POSIX startup paths and Zsh startup paths use their respective option
  semantics. `.profile` conservatively recognizes explicit Bash extensions
  as ambiguous without claiming that every POSIX shell executes them.
  Nested written code operands must retain the same role analysis: reaching
  an analysis limit cannot silently mean "no writer". Prefer a finite iterative
  traversal; any necessary limit must preserve an actionable uncertainty in
  the known code context, not manufacture credential values.
  This remains bounded syntax recognition, not execution, alias/nameref
  resolution, effective precedence, history replay, implicit shell-state
  simulation or arbitrary expansion. Module-provided commands and user-defined
  extensions are excluded; that does not exclude a core command's explicit
  named output such as `zmodload -P`. Unknown ordinary commands are not blocked
  merely because their data mentions a credential variable. The guarantee is
  inventory of persisted literals and refusal of explicit related writers
  within this fixed surface, not restoration of the effective environment or
  proof that an arbitrary startup script cannot regenerate a credential.
- File references to environment variables resolve only from unambiguous
  persisted literal assignments, never `os.environ`. Unresolved references
  describe a missing persistent value, not a permission failure.
- Saved API keys use the existing whitespace-normalization policy once at
  inventory construction. Proof, identity reuse and permanent custody consume
  that same value; exact raw shell assignments remain separate cleanup evidence.
- Every credential keeps its original target and authentication semantics.
  A static bearer token is not assumed to be an API-key header or an OAuth grant.
  Any unsupported transport must have an honest, actionable row-level reason.
  For custom Anthropic endpoints, an opt-in private `auth_scheme="bearer"`
  follows the immutable credential ref through transient proof, provisioning,
  discovery, probes, reuse, replacement, retarget and engine configuration.
  Absent metadata keeps legacy behavior; unknown schemes fail closed. Official
  Anthropic endpoint Bearer and tokens triggering the pinned engine's
  `sk-ant-oat` OAuth heuristic are not reinterpreted or imported.
  Conversely, saved Anthropic API-key-header credentials for custom endpoints
  cannot migrate: the pinned engine sends Bearer there. Migration refuses
  this mismatch before proof/reuse/custody and preserves the original files;
  it never treats accepting both headers as permission to change semantics.
  This migration-only check does not change legacy public Source behavior.
  Public Source/Binding/Record shapes stay unchanged. The shared pure validator
  is `validate_api_key_auth_scheme(vendor, protocol, base_url, secret,
  auth_scheme)` in `api_key_vendors.py`; `protocol=None` is only for transient
  credentials. Adapter provision/transient/matching gain an optional keyword;
  private `_require_proven_source_payload` forwards it separately from public
  source payloads. Existing calls omit that keyword when it is absent.
  The first released explicit Bearer implementation also binds the scheme to
  an opaque immutable `cred_auth_bearer_<32 random hex characters>` ref. Only
  runtime state decodes this reserved namespace; unknown/malformed tags refuse.
  Legacy API-key and OAuth ref generation is unchanged. Every full credential
  consumer verifies tagged-ref/metadata agreement; deleting metadata's scheme
  cannot downgrade a tagged credential. Valid pre-release plain-ref Bearer
  metadata remains readable; replacement/retarget creates a fresh tagged ref.
  Complete loss of such an unpublished untagged Bearer record is unrecoverable
  from its ref alone and is not a supported upgrade guarantee.
  Replacement's scheme accessor does not require the old secret. For safe
  missing or content-corrupt documents it may use the ref: published plain
  API-key refs mean legacy, tagged refs mean Bearer. Readable documents still
  require API-key kind and known consistent scheme; unsafe paths, permissions,
  symlinks and genuine I/O errors never fall back. The replacement key passes
  ordinary target/scheme validation and authenticated discovery before commit.
  Existing revocation journal operation `revoke_api_key_credential` and the
  same-named adapter method carry explicit API-key-only retirement intent.
  They remove only the unbound ref's safe private namespace, durably, without
  requiring its old secret or deleting any OAuth auth-name file. The generic
  OAuth cleanup sequence is unchanged; unreadable/invalid is not proof of
  absence and cannot discard a pending retirement.
- Existing backend custody, authenticated proof, source reuse, cooperative
  lease/drain, durable journal, no-op guards, and recovery remain authoritative.
  Shared shell assignments must not be removed for unconsented consumers.
  Do not replace the takeover state machine or add a parallel journal.
- Consent binds the complete relevant file snapshots, including absent paths
  that could introduce a new grant. Cleanup removes only consented assignment
  lines, preserves unrelated bytes, and uses existing `NativeFileEdit` recovery.
  A changed file, new relevant layer, or ambiguous assignment fails closed
  before exposure. Already exposed grants never roll back to old credentials.
  Compare-only guards do not represent a native write or grant exposure, and
  rollback never restores their bytes. Recheck them after runtime start as well.
  Shell edits retain captured permission bits through journal serialization,
  forward/reverse writes and interrupted-write replay. The transaction writes
  bytes and mode on a temporary inode, rechecks the source and publishes them
  together; replay verifies the target and completes file/directory durability,
  never chmods a published path. Optional `before_mode` records actual captured
  evidence, not a legacy default. This is compare-before-write, not a claim of
  cross-process atomic CAS. Compare-only guards never claim permissions;
  Avibe-owned journal/state files remain owner-private.
  Atomic publication mechanics remain owned by `config.atomic_io.write_atomic`.
  Optional `mode` defaults to `0600`; optional `before_replace` runs after the
  temporary bytes/mode are durable, immediately before publication. The caller
  owns source validation and strict directory durability. Guard failure cleans
  only the unpublished temporary file. No-op/replay paths do not call this writer.
  Shared-reference guards cover configuration/profile consumers, not unrelated
  backend credential-store bytes. Credential snapshots stay backend-scoped.
  Completed receipts retain optional opaque `inventory_ids`, distinct from
  snapshot-bound consent IDs. Fresh consent for the exact restored old
  credential/target may clean it again only while the receipt's current Hub
  Source/ref ownership still matches; it must never reprovision an old OAuth
  grant. Legacy receipts remain readable: their original leaf IDs supply the
  same exact inventory proof, after fresh full-file consent and ownership
  validation. An unrelated or changed grant is not inferred to match.
  Matching applies per consented item, not to the whole selection, so adding
  another backend or restoring a subset cannot reprovision an old grant.
  The existing receipt Source bundle is retained and verified as a whole,
  without inventing a one-to-one item/Source mapping. A changed OAuth snapshot
  for a backend already covered by that receipt cannot prove new authorization:
  refuse cleanup/provisioning and direct the user to existing Hub reauthentication.
  An entirely Source-free receipt is an empty-only confirmation and does not
  assert OAuth custody for that batch. Mixed receipts cannot prove which opaque native
  container was empty: conservatively require Hub reauthentication there.
  A cleaned container revision alone does not establish absence of prior OAuth.
  Optional private `oauth_custody_backends: list[str]` preserves a monotonic
  reauthorization requirement across subsequent completed batches, including
  API-only and empty batches. It is bounded by the supported native OAuth
  backends (Claude and Codex), not
  a secret/grant history or another custody owner. The journal produces/merges
  and validates it; fresh-consent apply consumes it before proof/provision or
  cleanup. Exact last-bundle matches retain the existing cleanup-only path.
  Earlier-batch unmatched OAuth requires Hub reauthorization even if its old
  Source was removed or changed. An existing legacy receipt without the field
  has unknown overwritten history: conservatively mark all supported backends,
  including when that last receipt is Source-free. A genuinely new history
  writes an explicit empty list when no OAuth custody was transferred. Thus
  legacy upgrade can require an extra OAuth authorization, never speculative
  reprovisioning of a restored native refresh grant. Completion/recovery must
  carry this evidence monotonically across receipt-save/active-journal-forget
  interruptions and terminal needs-auth outcomes.
- Public scan adds `source_paths: list[str]` (display-safe file locators, never
  secret content). Existing payload fields remain compatible. Blocked rows
  use specific `settings.models.migration.blocked.*` keys; UI shows every
  distinct source/reason instead of only the first group warning. An empty
  inventory is not represented by a fake unselectable credential.
- Public `required_backends: list[str]` adds explicit shared-file consent.
  Empty/absent means the row's own backend. Connected backend groups consuming
  the same `(file, variable)` are selected together by UI; blockers propagate
  across that closure with their concrete source/reason. The server independently
  rejects partial consent before provisioning, and guards against a new consumer
  appearing during proof. This reuses backend custody rather than creating
  another transaction owner.
  Backend grouping is not proof that every reference was removed. Cleanup
  checks the planned native after-images: preserve endpoint-only shell values
  still referenced by another configuration; refuse removal of an authentication
  variable with surviving consumers, with a source-specific reason on its
  existing row. No fake credential is created for a base-only provider.
- Do not loosen actual Keychain permission errors or silently discard an
  unsupported persisted credential to make an entire backend selectable.

## Ownership and validation

Orchestrator owns scanner/planner/lifecycle integration and final delivery.
Independent lanes may own a pure shell parser, UI presentation, and read-only
credential transport audit only after this contract is committed.

Required evidence: fixture reproduction of populated inherited environment
with already-Hub agents; literal shell discovery/reference resolution;
dynamic/refusal cases without execution; exact cleanup/reverse/recovery and
concurrent edits; public/wrong-key rejection with native files preserved;
UI selection and source-specific instructions; focused Python and UI tests,
changed-file Ruff, UI build, then current-head automatic Codex review and CI.
Real-account OAuth/Keychain acceptance remains manual and unperformed.
