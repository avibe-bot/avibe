# Session request preview

## Change contract

Add the Owner-only `vibe debug request preview --session-id ID [--context-seq N]` command over private controller IPC; do not expose a Web/API forwarding route. Existing CLI invocation authority, including remote Owner Agent callers, must be preserved and revalidated by the controller. Missing/malformed remote authority must not become local Owner authority.

The preview reconstructs a committed transcript prefix using current Agent/system/tools/Model Hub configuration. Production and preview share Agent construction, model/effort override semantics, and route description. A preview cannot dispatch work, resolve/activate a gateway credential, call a provider, execute a tool, consume input, settle a call, record an observation, or write Session state. Only owner-private diagnostic artifacts may be created. Context cut precedes projection. Report context transitions, pending tools, reconstruction limitations, and unbound authentication explicitly.

Output is a private artifact directory (POSIX 0700, files 0600; equivalent Windows owner-only ACLs). Standard output contains only bounded structural metadata and artifact paths, never the prompt, canonical history, opaque/signed provider content, or credentials. Body bytes use the merged Agent.dry_tick contract and its production provider wire encoder. No real-session probes or replay, no model-error root-cause changes, deployment, or release.

## Usage

```sh
vibe debug request preview --session-id SESSION_ID
vibe debug request preview --session-id SESSION_ID --context-seq 12
```

Requires a running local controller and an existing, unarchived Vibey Session. Both standalone host CLI and existing remote Owner Agent CLI invocations are supported; Viewer, Editor and Member invocations are refused. This command does not start a Session or consume its queue.

The command prints a JSON summary and paths below Avibe's configured state directory (`<state>/request-previews/preview-*/`). `manifest.json` describes the effective context cut, current-code reconstruction, unbound authentication, budget and readiness. A `ready` result also contains `request.body.json`: complete UTF-8 JSON bytes prepared by the production provider encoder, including private conversation and potentially opaque provider history. Keep that directory private and remove it manually when the diagnostic task is complete. No automatic retention or upload is added by this command.

`transition_required` means the loop would first clear results, checkpoint or relieve context; no body is exported. `not_model_ready` with `missing_tool_results: true` means a committed tool call has no result at that cut; the preview does not synthesize a completed execution. These are successful diagnostic results, not permission to send. Authentication, gateway turn routing and retries are deliberately not activated. Missing/disabled supply or unsupported Sessions fail safely without exposing a raw exception.

## Validation

- Owner-boundary tests reject lower-role and malformed IPC authority before reading a Session, while permitting the existing local and remote Owner paths.
- Adapter-boundary tests load real SQLite transcript/fork prefixes, compare production construction, fail on DML/session/run/tool/provider/observation/credential activation, and check explicit-null settings and blocked statuses.
- Router tests compare description with the activated production launch facts while proving description never allocates credentials or emits transitions.
- Artifact/CLI tests check permissions, no overwrite/symlink acceptance, safe output, and parser/admission classification.

Existing core dry-tick tests own request bytes and context-transition selection; integration tests exercise the new host/IPC/CLI boundaries instead of duplicating those algorithms.

## Known by design

- This is current-configuration reconstruction, not historical transport capture or a prediction of a running Turn's volatile retry state.
- Queued/unconsumed inputs are excluded; no fake input or environment block is appended.
- Pending client tool outcomes and required context transitions produce metadata only, not a sendable HTTP body.
- Authentication and gateway route activation remain unbound. The output must not be sent as-is.
- Private IPC is an OS-user trust boundary. Explicit caller provenance is mandatory in this new endpoint; remote snapshots are revalidated, never replaced by daemon authority. An explicit local declaration represents the existing standalone host Owner path, not remote anonymous fallback.
- PR #3103 independently adds model-request artifact capture and `list|show|export` capabilities. Debug replay with preview is a separate follow-up; reconcile parser changes only after #3103 merges.
