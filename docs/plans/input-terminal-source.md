# Human input terminal provenance

## Change contract

Every human input delivered to an Agent carries one execution-only source line
next to the existing voice line. Canonical text, read/unread and notification
behavior stay unchanged. Harness input keeps its existing provenance.

Wire format: `[Source: iphone_app|mac_app|browser|im:<platform>|unknown]`.
Known Web terminals append `; surface=main|mini|notification_reply|ask_vibey`.
A terminal inferred only from a client hint appends `; evidence=hint`.
All values are enums, never user-provided prose. Existing voice text is unchanged.

At Web admission, resolve the authenticated subject, binding and session against
`remote_device_sessions`; copy only its kind. This takes precedence over the
`X-Avibe-Input-Source` client hint. Missing/invalid hints and absent/unsupported
records yield `unknown`. No UA inference, device name, version, device identifier,
IP, token or email is added. Surface is a bounded client presentation hint.
The registry itself stores presentation descriptions, not hardware attestations;
source labels confer no authorization.

Store the resolved facts as message metadata, just as voice uses `input=voice`.
Hydration and `MessageHandler.prepare_input_metadata` feed `AgentInputMetadata`,
which all backends render. Queued messages with distinct labels cannot coalesce.
IM sources use the admitted transport platform, regardless of message metadata.

Mini means the desktop pet window. Ask Vibey marks the draft it inserts into;
its next successful send consumes the marker, using the sending terminal's facts.
The entrance marker is a per-tab draft hint, retained across route remounts and
refused sends. Copying draft text to another tab/device does not transfer the
entrance marker; the receiving terminal still identifies itself normally.

## Validation

Exercise actual admission, durable delivery hydration and backend rendering for
all terminal and surface cases; prove registry precedence, unknown fallback,
voice coexistence, enum-only output and mixed-source queue boundaries. UI tests
cover the emitted header and Mini/notification/Ask Vibey payloads. Run focused
pytest/Vitest, Ruff, UI typecheck and production build. No Rust or packaging.

Review and merge belong to the orchestrator; do not post an `@codex review`
request or merge from this implementation lane.
