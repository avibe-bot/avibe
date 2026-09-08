# Model Hub identifier boundary assessment

## Status and recommendation

Implemented locally after the recorded decision from orchestrator Session
`sesvmgbdub2gp` at integration commit
`d5adacdba42783f49dc0c40745fa25d5af8fcf7e`. The decision was read directly from
the integration worktree; the shared plan was not edited or cherry-picked.
The boundary inventory below records the diagnosed base; the implementation
record at the end describes the completed repair and its remaining gates.

- Inspected product SHA: `76e269c9a9dfd3a6286ce0da81b77316ca4be38a`.
- Lane: C; Session `sesm79v4dhm25`; Run `ddca032db570`.
- Assigned branch: `fix/model-hub-identifier-boundary`.
- Authority: `model-hub-permissive-boundaries.md`, shared invariant 4.
- Diagnosis evidence: 128 existing focused tests passed, plus synthetic
  reproductions described below. Implementation evidence: 524 focused Python
  tests and 64 UI tests passed. No live services, credentials, upstreams, or
  user state were exercised.

Remove the model-ID admission length ceiling, its UI/schema mirrors, and the
typeahead query ceiling. Preserve the existing whitespace spelling rule,
credential checks, exact Source/model pairing, and backend-specific admission
rules. Materialize discovery IDs and their supported-parameter declarations
losslessly at their selected JSON paths. Keep the default observation parser
budget for other callers and unrelated values.

Keep the usage key algorithm and persisted key reader unchanged. The ledger
already folds every identity longer than 200 characters, including a literal
that happens to spell another identity's folded key. Admission need not stop at
256 for that separation to work. No ledger migration, new key namespace, or
new numeric ID limit is necessary for valid nonblank Unicode identifiers.

There are two independently reproducible pre-existing exceptions to the
ledger's broad claims about every loadable string: long all-whitespace legacy
IDs, and unpaired UTF-16 surrogate code points. They are not caused by the
256-character limit. Preserve their evidence and do not silently rewrite
historical usage to guess an attribution; see the explicit scope recommendation
below.

## Complete boundary inventory

| Boundary and owner | Inspected behavior | Proposed treatment |
| --- | --- | --- |
| `identifiers.py`: `normalized_model_id` | `strip() or value`; no case folding, Unicode normalization, shortening, or length check. Preserves historical padding-only strings on load. | Preserve spelling semantics. |
| `identifiers.py`: `canonical_model_id` | Requires text, nonblank canonical text, and at most 256 Python characters. Does not itself scan for credentials. | Remove length admission. New admission should also require UTF-8-encodable Unicode, because that is what config, routing, and metering can carry. Keep credential policy with its existing callers. |
| `service.py`: `add_custom_model` | Canonical admission plus full credential scan; stores canonical ID; existing manual ID is an upsert, discovered ID is managed upstream. | Inherit length-free admission; preserve upsert, origin, and tier authority. |
| `service.py`: `create_source` inline `models` | Parses `ModelHubModelConfig`, then applies canonical admission and credential scans before observation/custody. | Inherit the same admission; preserve provenance, deduplication, and custody ordering. |
| `service.py`: `_apply_discovered_models` | Canonicalizes each discovered ID, rejects invalid/credential-bearing/canonical-duplicate batches, preserves manual and retired rows. Used by creation, refresh, source edits, OAuth, reauth, and import. | Inherit length-free admission, without partial inventory acceptance or truncation. |
| Runtime API-key discovery: `client.py::probe_models` and `_project_model_inventory` | Whole response spools with a 256 KiB memory threshold; projection has an absolute request deadline. Both `data` and `models` accept string rows and `{id: ...}` rows. Any selected ID elided by the JSON string budget fails the selected inventory. | Keep spool/deadline ownership; make the four ID paths lossless. |
| Runtime OAuth discovery: `adapter.py::_discovered_models` | Management JSON already materializes via `ijson`; extracts `id`, then `alias`, then `name`, or a string row. No ID length ceiling at this layer. | No implementation change; consume the common service admission and test parity. |
| Unsaved observation: `service.py::_validate_observation`, `_observation_payload` | Validates observation shape and scans the complete ID/parameter payload for credentials; publishes both `models` and `model_metadata`. No independent 256 limit. | Preserve full credential scan. Check IDs and parameter strings are representable before returning them through JSON response encoding. |
| Native-config import: `migration.py::_opencode_manual_models` and import commit | Trims nonblank IDs, scans credentials, constructs source model configs, and reaches shared discovery/application when observation succeeds. Manual import can survive failed discovery. Native bundled inventory also has no independent length ceiling. | Reuse canonical admission for newly imported manual IDs, preserving exact valid long IDs and the existing skip-invalid-entry behavior. Keep the copy-only lifecycle; no persisted import migration needed. |
| Catalog admission: `catalog_admission.py` | `backend_model_admission_error` uses canonical admission, preserves Claude prefix/builtin exceptions; `admissible_backend_model` also validates the credential-free persisted shape. | Inherit length-free admission; preserve prefixes, locked/native selectors, metadata validation, and origin rules. |
| Catalog producers: `service.py::agent_model_candidates`, `_apply_builtin_reconcile`; `models_dev_catalog.py::search_models_dev` | Builtin, provider, and models.dev proposals all use `admissible_backend_model`; a valid long source ID is currently omitted from candidates. | Automatically admit long proposals through that owner; test all producer classes without changing capability projection. |
| Catalog writes: `service.py::_parse_backend_catalog_models`, `set_agent_models` | New IDs use shared admission. OpenCode additionally reapplies admission to the entire parsed baseline/desired list. Claude/Codex allow edits of historical long IDs. | Keep new-ID checks at the common write boundary. Remove the redundant OpenCode whole-list admission check so new admission is not a legacy-read rule. |
| Persisted source/hop/model loading: `config/v2_config.py` | Source and hop leaves use `normalized_model_id`; backend model leaves trim and scan credentials without a length ceiling. Parent collections preserve their existing repairing-vs-admitting duplicate policies. | No tightening or new rejection of old source/hop/backend leaf shapes. |
| Persisted OpenCode aggregate: `ModelHubAgentSupplyConfig.from_payload` | Unlike other backends, reapplies `canonical_model_id` to route keys and catalog IDs; currently rejects a 257-character catalog ID on reload. | Replace admission reuse with explicit existing nonblank/canonical-spelling validation. Keep native protocol, route membership, and menu invariants. |
| Route writes/read/provenance: `service.py`, `resolver.py`, `provenance.py` | Route identities are exact `(source_id, model_id)` pairs. Existing targets retain load-time identities; only new targets use canonical admission. API-key passthrough, subscription inventory, retirement, menu membership, and guards remain distinct. | New long targets follow exactly the existing source policy. Preserve exact identity on route preview/save/reorder/restore and provenance lookup. |
| Runtime binding/invocation: `SourceBinding`, runtime `state.py`, `config.py`, `client.py::invoke` | Full IDs populate inventory and route-only IDs. YAML entries carry exact `name` and `alias`; invoke sends `source.prefix + "/" + model_id`. No length truncation found. | No implementation change; consuming tests must compare exact long/non-ASCII targets. Actual pinned-engine interpretation remains lane D's boundary. |
| Backend launch/overlay: `modules/agents/model_hub.py` | Full requested/target/runtime strings are carried into launch and runtime config; OpenCode provider prefix is separate from bare model identity. | Read-only consumer verification; do not edit lane B's projection code. |
| Web UI: `BackendModelEditorDialog.tsx`, `types.ts` | Add mode caps input at 256 UTF-16 units and refuses longer resolved IDs. Edit mode preserves full read-only legacy IDs. Source-detail manual input has no matching cap. | Remove the constant, HTML `maxLength`, and `tooLong` branch/copy. Preserve duplicate checks, trim, and read-only edits. |
| Typeahead: `service.py::models_dev_matches` | Independently rejects queries longer than 256 before calling search; the editor sends its full typed ID. Thus removing only the field cap produces a misleading lookup failure. | Remove this query-length check; preserve nonblank/text validation, search result behavior, cache, and fetched-catalog resource budget. |
| Web/API/IPC: `modelsApi.ts`, `ui_server.py`, `model_hub_client.py`, `rpc.py`, `internal_server.py` | IDs are passed as JSON strings, encoded query parameters, or an encoded path captured as `path:model_id`; RPC passes the values in JSON. No additional model-specific field cap found. | No endpoint redesign. Exercise encoded slashes/non-ASCII and long values in hermetic consumers; generic transport/body/URI limits are not an unlimited-transport guarantee. |
| Wire schemas/locales | `backend-model.schema.json` describes a 256 admission limit; `agent-supply.schema.json` actually caps OpenCode `menu.checked` items at 256; both locale bundles carry the editor error. | Update these exact mirrors; do not alter unrelated numeric/resource bounds. |
| Usage live write: `usage.py::record_many` | Converts live Source/model identities with `usage_ledger_key`, then normalizes stored-key rows. Both resolver and forwarded gateway calls report exact configured identities. | Preserve derivation direction and pair/day aggregation. |
| Usage read/labels: `_normalize_row`, `_keyed_identities`, `summary`, UI `UsageTab`/`usageProjection` | Reads stored keys with `persisted_ledger_key`; derives config identity keys separately for Source-scoped label joins. Deleted identities retain totals with missing labels. | Preserve keys, history, deleted-source semantics, retention, and presentation. |
| Diagnostic events and protocol observation | Event `model_id` stays whole while rendered human text is bounded at 200. Protocol observers retain finite machine evidence/presence and bounded private error copies, not model routing identities. | Leave harmless display/observation bounds intact. |

## Why the ledger does not need a migration

For a canonical, nonblank, UTF-8-encodable identifier `x`, the existing rule is:

```text
K(x) = x                                      when len(x) <= 200
K(x) = x[:200] + "~" + SHA256(UTF8(x)).hexdigest() otherwise
R(K(x)) = K(x)                                 persisted-key read
```

The output populations are at most 200 characters and exactly 265 characters.
Admission at 256 is not what separates them. Take `x = "m" * 300`, and admit
`y = K(x)` as another literal model ID: `y` is 265 characters, so its live calls
are keyed by `K(y)`, not by `y`. A persisted row for `x` is read as `R(y) = y`.
These are intentionally different operations. Do not make live key derivation
self-idempotent, recognize hash-shaped live IDs as already keyed, fold at 256,
or extend the readable head.

The existing SHA-256 collision assumption remains; a finite digest is not a
mathematical proof of injectivity over arbitrary strings. No additional
structural collision is introduced by admitting a folded-looking literal.

Local history independently confirms the compatibility requirement:
`6a40b10e592d7b9ae8ab291769c8dbea47a7711e` raised admission from 200 to 256 and
introduced `USAGE_LEDGER_VERBATIM_MAX_LENGTH = 200` explicitly to keep previously
written usage keys stable. This proposal changes neither that algorithm nor
the stored-row reader, including its permissive treatment of historical keys.

Synthetic evidence used eight seeds spanning 200/201/256/257 characters, CJK
plus emoji, composed and decomposed accents, and 16,385 ASCII characters. The
set also included each seed's folded literal and its next folded literal:
22 distinct identities, 44 calls over two writer instances, 22 separate rows
after another reload, and exact full labels joined from config. All
`R(K(x)) == K(x)` assertions passed in that population.

## Discovery: required facts versus observations

`JSON_STRING_TOKEN_BYTES = 16 * 1024` currently budgets the JSON lexical token,
including escape sequences, rather than the decoded identity.

Measured against the inspected SHA:

| Synthetic input | Current outcome |
| --- | --- |
| 256 ASCII characters | Admission, source load, catalog admission, and inventory projection accept. |
| 257, 265, 4,097, or 16,384 ASCII characters | Source load and inventory projection preserve exact ID; new admission/catalog refuse. |
| 16,385 ASCII characters | Source load preserves ID; inventory projection fails instead of materializing it. |
| Same 3,000-character CJK ID, raw UTF-8 JSON | Exact projection succeeds; response is 9,022 bytes. |
| Same CJK ID, JSON Unicode escapes | Projection fails; response is 18,022 bytes. |
| `supported_parameters = ["reasoning"]` | Consumer chooses upstream reasoning tiers. |
| `["reasoning", <16,385-character unknown parameter>]` | Projection changes parameters to `None`; consumer loses upstream reasoning evidence. |
| `<16,385 spaces> + "reasoning"` as a parameter | Projection elides it even though the real tier consumer trims and recognizes it. |

Neither identity nor supported-parameter evidence is harmless diagnostic text.
The narrow implementation is a default-empty `lossless_string_paths` option on
`SelectiveJSONParser` and `project_json_reader`, selected only by
`_project_model_inventory` for:

```text
data.*                         models.*                 (string-row IDs)
data.*.id                      models.*.id              (object-row IDs)
data.*.supported_parameters.*   models.*.supported_parameters.*
```

Decide retention at the start of a value token, using its already-selected
path; never treat an object member name or an unrelated nested string as an
identity. Retain the complete selected lexical string until decoding, and keep
all existing JSON grammar, UTF-8 validation, duplicate-member replacement,
array-scope, whole-document completion, and absolute-deadline behavior.

The result inherently costs memory proportional to the inventory facts that
the application must retain. This is not a claim of constant memory for a
materialized unbounded ID. Unrelated subtrees and diagnostic strings continue
to use the existing bounded parser behavior. Replacing the whole projection
with a full-object JSON load would unnecessarily retain unrelated upstream
payloads.

## Encoding and legacy exceptions

New admission should accept nonblank Unicode scalar text, trim the existing
outer whitespace, and retain every remaining code point exactly. Do not apply
NFC/NFKC, case folding, slash rewriting, digest substitution, or ellipsis to a
model identity. Python counts code points and browser `length` counts UTF-16
units: 129 emoji currently pass Python's 256 limit but exceed the UI's limit.
Removing both ceilings eliminates that disagreement.

The following exceptions were reproduced at the base SHA and must remain
visible in the orchestrator's scope decision:

1. **Unpaired surrogates.** A 200-character string ending in `\ud800` passes
   current admission but cannot be written as UTF-8; at 201 characters, usage
   hashing raises `UnicodeEncodeError` directly. This is a representability
   failure, not a meaningful model-name length. Prefer rejecting it at new
   admission and unsaved observation, while removing admission reuse from
   persisted OpenCode loading and whole-list catalog parsing. Do not silently
   replace it with U+FFFD, ignore it, or introduce a new spelling of old IDs.
   Repairing already persisted malformed strings is separate from admission.
2. **All-whitespace legacy IDs beyond 200 characters.** A source model of 201
   spaces still loads, as intended by the historical loader. Its live key is
   200 spaces plus `~<digest>`, but `persisted_ledger_key` strips that head to
   a 65-character key. A second legitimate literal ID equal to that shortened
   key shares its usage row. Actual reproduction: two different identities
   produced one row with two requests. The label join also derives a key
   different from the persisted row. Existing `MH-USAGE-006` covers only three
   spaces and misses this population.

Preferred scope: block newly admitted unencodable identities, preserve loading,
and leave the existing key format/read behavior intact in this repair.
Explicitly record the whitespace-only historical collision as a separate
attribution defect, not a passing invariant or a new limitation on legitimate
IDs. Blank IDs remain rejected on every new admission path.

This separation has a real compatibility reason: already merged historical
counters contain no fact that can distinguish a whitespace identity from the
65-character literal. Rekeying or assigning that total to either model can
invent attribution or split historical usage. If the orchestrator elects to
repair this pre-existing class now, it needs a separately recorded
ambiguous-history policy and expanded ledger ownership before implementation;
the lane should not guess. No owner escalation is required merely to choose
the narrow, reversible admission scope.

Synthetic credential-shaped text at both the head and tail of a 17 KB ID was
detected by the existing full-value credential scanner, and refused by both
source and backend config validators. These are fabricated fixtures, not keys.
Retain those scans after full materialization, including parameter metadata;
never validate only the readable ledger head or a shortened diagnostic copy.

## Concrete implementation ownership

Proposed product files, limited to these symbols/concerns:

| File | Lane C change |
| --- | --- |
| `core/handlers/model_hub/identifiers.py` | Delete admission length constant/check; representability at new admission; correct comments claiming 256 protects folded keys. Preserve both ledger algorithms byte-for-byte. |
| `core/handlers/model_hub/json_wire.py` | Default-empty selected-path lossless string retention, with unchanged default behavior. |
| `vibe/model_hub_runtime/client.py` | Opt inventory identity/parameter paths into lossless retention only. |
| `core/handlers/model_hub/service.py` | Observation ID/parameter representability; remove redundant OpenCode whole-list admission and typeahead query ceiling; update directly misleading identity-bound comments. |
| `core/handlers/model_hub/migration.py` | Reuse canonical admission in `_opencode_manual_models`, so a failed discovery cannot bypass the representability rule for newly imported IDs. |
| `config/v2_config.py` | OpenCode aggregate uses load-time spelling checks, not new admission; update directly misleading length-bound comments. |
| `ui/src/components/settings/models/BackendModelEditorDialog.tsx` | Remove add-mode ID length check and HTML cap. |
| `ui/src/components/settings/models/types.ts` | Remove `BACKEND_MODEL_ID_MAX_LENGTH`. |
| `ui/src/i18n/en.json`, `ui/src/i18n/zh.json` | Remove the unused model-editor `tooLong` entry only. |
| `docs/plans/model-hub-contracts/backend-model.schema.json` | Correct ID description. |
| `docs/plans/model-hub-contracts/agent-supply.schema.json` | Remove only the model-ID `menu.checked` maximum. |

Focused test ownership:

- `tests/test_model_hub_api.py`: all admission producers, credential refusal,
  candidate producers, catalog writes, API round trips, long-query typeahead.
- `tests/test_model_hub_routing_modes.py`: replace length refusals with
  source-policy assertions; include OpenCode in long catalog reload coverage.
- `tests/test_model_hub_provenance.py`: replace removed-constant fixtures with
  explicit historical lengths; preserve membership-only refusal.
- `tests/test_model_hub_usage.py`: retain `MH-USAGE-006/007/008` consuming
  assertions, add long valid Unicode/fold-closure/reload cases; remove
  dependency on the admission constant without changing expected ledger keys.
- `tests/test_model_hub_json_wire.py`, `tests/test_model_hub_runtime.py`:
  lossless selected paths versus bounded unrelated text; inventory shape and
  duplicate semantics; supported-parameter consumer evidence.
- `tests/scenarios/model_hub/test_model_hub_migration_scenarios.py`: existing
  native-provider import harness with long/invalid manual IDs and failed
  discovery, without changing import/custody authority.
- `ui/src/components/settings/models/BackendModelEditorDialog.test.tsx`:
  long paste/submit, Unicode, duplicate and existing-row behavior.

The existing scenario IDs cover loadability, usage separation, and label joins;
keep those test names and IDs stable. If integration needs a new scenario ID or
catalog entry, coordinate its allocation with the orchestrator. This diagnosis
does not edit the shared plan or scenario catalog.

Lane A owns gateway request envelopes and their locale keys; C does not edit
`turn_gateway.py`. Lane B owns capability/limit projection; shared
`service.py`, config, or API-test file changes must be integrated by disjoint
functions, with no edits to projection policy. Lane D owns the pinned engine,
runtime reasoning policy, build/release plan, and engine-wire evidence; C does
not edit engine config generation, manifest, or adapter execution. C's runtime
client change is restricted to inventory projection and uses the unchanged
discovery deadline.

The admission/load distinction and opt-in parser argument are local contracts.
They require no new dependency, schema revision, allocated key namespace,
cross-lane runtime flag, or engine release. Reverting them leaves existing
ledger bytes untouched; an older build's existing OpenCode long-ID loader
restriction still applies if new long catalog rows are taken back to it. Do
not describe reverting source code as a promise of downgrade compatibility for
newly supported data.

## Acceptance matrix after approval

| Population or boundary | Required result |
| --- | --- |
| 200/201, 256/257, 264/265/266, 4,097, 16,384/16,385-character IDs | Admit and preserve full identity where source/backend policy allows; no new fixed ceiling. |
| Two names sharing a head but differing arbitrarily far into the tail | Distinct routes, runtime targets, usage rows, and labels. |
| Literal `K(x)`, `K(K(x))`, and their source/model Cartesian pairs | Live key derivation remains separate from persisted key read; no structural collision. |
| CJK, supplementary-plane emoji, composed/decomposed accents, encoded slash | Same exact canonical ID through UI/API, config, runtime binding, provenance, usage, and reload; distinct accents remain distinct. |
| Raw UTF-8 versus JSON `\u` escapes, all inventory shapes | Same decoded ID and metadata, independent of lexical expansion. |
| Large unrelated string/tree beside inventory | Required IDs/parameters survive; ignored data and default parser diagnostics remain bounded. |
| Duplicate JSON members/IDs and padded equivalent identities | Preserve current last-member/first-complete-record semantics at the parser and canonical-batch rejection at service admission. |
| Long `supported_parameters` alongside or containing recognized reasoning evidence | Lossless materialization reaches the existing tier consumer; malformed non-string metadata still degrades as currently specified. |
| Empty/nontext/blank or newly supplied unpaired surrogate | Structured non-secret refusal before persistence/observation response encoding; native inventory import retains its existing skip-invalid-entry behavior. |
| Credential-shaped prefix/tail/metadata beyond former budgets | Full-value scan rejects; nothing leaks into config, API output, events, or ledger. |
| Source manual add, inline create, discovery/refresh/import, builtin/provider/models.dev proposals | Same ID spelling/admission owner, with existing per-surface authority intact. |
| Catalog reload/edit and route preview/save/restore for all three backends | Long existing IDs remain usable; no admission rule reintroduced into loading. |
| Unknown API-key target versus subscription target; retired model | Existing source eligibility and retirement policy unchanged. |
| Old ordinary/folded ledger rows followed by new calls and multiple reloads | Exact same key and aggregate; no silent row drop, double fold, duplicate counting, or label loss. |
| Deleted Source/model with retained usage | Historical totals remain, labels absent for the removed pair. |
| Long model pasted into editor | Submit whole ID, no `maxLength`/`tooLong`; lookup does not fail solely because query exceeds 256. |

No claim is made that in-process UI tests prove arbitrary URI sizes through a
real browser/proxy, that Python binding tests prove pinned-engine behavior, or
that removing an application field cap removes generic transport/resource
limits. Add hermetic HTTP/engine consumer evidence at integration where those
claims are needed; never infer it from a fake-only test.

## Diagnosis validation

All runs used the existing Python environment read-only, `python -B`, pytest
with its cache provider disabled, and the repository's per-test HOME/XDG/
Avibe/backend-store isolation. Synthetic ledger files lived in
`TemporaryDirectory` and were removed automatically. No product files had
changed during those diagnosis runs.

| Suite selection | Result |
| --- | --- |
| Usage: config-key equivalence, historical long reload, shared head, folded-literal closure, row reads, label joins | 23 passed; 77 deselected. |
| Runtime: `model_inventory` projection tests | 12 passed; 293 deselected across the collected selection. |
| Routing/provenance: legacy catalog/target, new-target admission, OpenCode loader | 66 passed; 122 deselected. |
| API: canonical manual/discovered/inline input, legacy load, secret/duplicate discovery, candidate and catalog length boundaries | 9 passed; 415 deselected. |
| Selective JSON parser and string rewriter | 18 passed. |

These are baseline tests, including tests that intentionally enforce today's
undesired length refusals. They prove the inspected consuming behavior, not
completion of the proposed repair. Synthetic probes additionally reproduced
the encoding-dependent inventory failure, lost parameter evidence, existing
OpenCode loader refusal, typeahead refusal before search, the two legacy
exceptions, full-length credential scanning, and the 22-identity ledger closure.

After implementation, run the corresponding focused tests and changed-Python
Ruff first, then relevant Model Hub/authority/scenario checks. UI changes require
the relevant Vitest cases, test type checking, and `npm run build`. The
orchestrator owns integrated validation, exact-current-commit Codex review, CI,
thread inventory, and the single PR. No push, PR, Watch, merge, release, deploy,
or local service restart is authorized for this lane.

## Implementation record

The implementation follows the approved ownership table without adding a
dependency, schema revision, runtime setting, or ledger migration:

- Removed the new-ID length cap, editor/type/schema mirrors, unused localized
  length error, and models.dev query cap. New identity admission requires
  nonblank UTF-8-representable canonical text; credential checks remain with
  the existing admission owners.
- OpenCode aggregate load and whole-list parsing no longer reuse new-ID
  admission. Native-protocol, spelling, route membership, Source eligibility,
  subscription inventory, and retirement rules remain in place.
- Added default-empty selected-path lossless string retention to the existing
  parser. Only inventory IDs and supported-parameter values opt in. Tests
  prove object keys, other selected strings, unselected strings, ignored
  trees, malformed JSON, duplicate scopes, and the discovery deadline retain
  their established behavior.
- Unsaved observations refuse unencodable IDs and parameters before response
  encoding, while scanning full credential-bearing values. Native manual
  imports reuse new-ID admission, including when inventory discovery fails.
- Compared the computational source of both ledger functions with
  `76e269c9a9dfd3a6286ce0da81b77316ca4be38a`: byte-for-byte unchanged.
  The entire `core/handlers/model_hub/usage.py` is unchanged. Only misleading
  identifier-module comments/docstrings were corrected.

### Consuming evidence

Final focused verification used the lane-local locked Python environment,
`python -B -m pytest -q -p no:cacheprovider`, and repository HOME/XDG/backend
store isolation. Runtime transport tests used a test-owned loopback HTTP
server, not an installed or pinned engine. Native-import tests used fabricated
credentials and verified the test-owned native tree's byte digest was unchanged.

| Final verification | Result |
| --- | --- |
| Complete JSON-wire, usage, routing-modes, provenance, and migration-scenario files | 384 passed. |
| API selection covering identity, catalog, producers, canonical/inline admission, observation/inventory, typeahead, and duplicate spelling | 108 passed; 351 deselected. |
| Runtime inventory/discovery, provisioning probe, and long-ID HTTP consumers | 32 passed; 270 deselected. |
| Editor, catalog dialog, and Source-create UI contract tests | 64 passed. |
| UI test type checking and production build | Passed; build reported dependency annotation/browser-crypto and chunk-size warnings. |
| Pinned Ruff 0.4.9 on all 13 changed Python files; whitespace diff check | Passed. |

The complete Source lifecycle preserves two long shared-head Unicode IDs,
composed/decomposed accents, inline and manual IDs, and an admitted
folded-looking literal through refresh, upsert, API output, runtime binding,
and reload. All three backend catalogs accept full long IDs; their legacy
catalogs remain editable/loadable, and malformed legacy surrogate identities
still load without being newly admitted.

The runtime consumer tests discover escaped long IDs over HTTP, reload
`SourceRecord`, serialize exact model names/aliases, and invoke every identity
plus an unlisted route-only identity through each of the three protocol
endpoints. Captured request bodies carry the exact prefixed IDs, and successful
outcomes retain the full Source/model pair. This is Avibe-to-mock-engine wire
evidence, not proof of a real engine's interpretation.

`MH-USAGE-007` now exercises 25 distinct valid identities (11 seeds plus two
keying generations), 75 recorded calls, repeated writer/read reloads, and exact
full labels. `MH-USAGE-008` also covers 28 Source/model label-join pairs
including long Unicode and folded-looking model literals after reload.
Existing scenario IDs/names remain stable; no registry entry was allocated.
The historical blanket wording of the registry must not be read as a proof
over malformed legacy text; the test documentation and this assessment
explicitly retain that exception.

### Remaining boundaries and integration handoff

- A percent-encoded 18,000-code-point Unicode path exceeded the test client's
  generic 65,536-character URL budget before reaching Avibe. The full ID
  succeeds through JSON body submission, service mutation, runtime HTTP bodies,
  and reload. Encoded path mutation is separately proven with 3,600 Unicode
  code points, and the real query route with 5,413 code points. No URI budget was
  raised or endpoint redesigned. Browser/proxy limits are still outside this
  field-admission repair.
- Long all-whitespace historical attribution and already persisted unpaired
  surrogates remain the documented pre-existing exceptions. No historical
  counter is rekeyed, split, dropped, or assigned a guessed owner by a migration.
  A future attribution policy must retain ambiguity where counters were
  already merged.
- Shared-file edits stay in the approved admission/load/observation/typeahead
  seams. No gateway envelope, `project_opencode_public_model`, launch
  capability policy, engine config-generation policy, engine pin, or shared
  scenario registry was changed. Integration must preserve peers' disjoint
  hunks in service/config/API-test/runtime-test files.
- The orchestrator owns broad integrated validation, real pinned-engine
  evidence, exact-head Codex review, CI, and the single PR. This lane only
  commits locally; no push, PR, Watch, release, deploy, or running-local change
  was performed.
