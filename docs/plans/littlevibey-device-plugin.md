# Optional LAN voice device

## Change contract

An independently installed `avibe-device` process serves one explicitly paired
CoreS3. Avibe startup and ordinary IM/Web chat do not import or start it. The
plugin owns its device credential, connection, bounded audio buffers and
half-duplex conversation. The host owns model/cloud credentials. The device
never receives instance credentials, a model token, or host authority.

The host's private control channel adds `device.voice`: status, ASR/TTS,
open/close a device conversation, one synchronous chat request, and owner-only
transcript inspection. The host creates the conversation identity and enforces
`device_chat_only_v1`. Each request also has a fresh UUID. Voice is never admitted
as a normal Bot Run or Delivery. Removed candidate `submit`/`result` operations
fail closed; there is no compatibility fallback to Owner execution.

The existing `littlevibey` Bot supplies its identity, configured Vibey model and
role description. A separate, narrow host path calls the existing Model Hub and
provider protocol adapter directly. It never constructs the tool-capable Agent
loop, ToolSuite, MCP clients, native CLI processes, jobs or delegates. Its request
has no function tools, native hosted declarations or image generation routes.
Any returned tool-call event or final tool block is refused, without dispatch.
The prompt explains the product behavior; it is not the permission boundary.

Only plain user/assistant text from this device's current connection enters its
model context, bounded to the last eight exchanges. No shared Bot/Web messages,
filesystem instructions, opaque provider history or general session tail is
read. Concurrent ordinary Bot work retains its normal path and permissions.
Unsupported Bot/backend/model configuration or a model failure refuses voice;
there is no alternate Owner/native-backend route.

Durable owner-private JSON files under Avibe's `state/device-voice` retain full
voice answers and request status. They are transcripts, never executable jobs.
Restart does not restore a live conversation; duplicate IDs, prior connections
and old record schemas cannot be resumed. The host CLI's `device history` entry
lists these records or reads one explicitly. The interactive shared Bot Web
session is not populated with voice input that could later acquire its tools.

Voice can converse and suggest. Work must be sent as a new specific request in
signed-in Avibe Web. A tool attempt receives a fixed localized explanation of
that handoff. Neither a spoken confirmation nor a later unrelated Web message
upgrades the voice conversation. No approval system is introduced.

ASR uses the paired instance's existing endpoint and multipart WAV upload.
TTS uses `POST /v1/audio/speech`, exactly JSON `{"text":"..."}`, and returns
bounded 24 kHz mono PCM16 WAV. Cloud headers stay inside the host. The plugin
converts WAV to negotiated 16 kHz Opus/60 ms. Audio/model IPC calls are canceled
on disconnect using the actual FastAPI handler's existing lifetime monitor.

The frozen device contract is copied to `littlevibey-wire-v1.md`, with
`tests/fixtures/littlevibey-wire-v1.json` agreed with the firmware lane.
Avachan is a read-only reference, never a dependency. No wire fields changed for
the host chat restriction. Wake route revision 4 remains separate from voice v1.

The plugin uses scoped process/socket ownership and explicit capability
dependencies, without a generic plugin framework, bus, or Cordis dependency.
Pairing/diagnosis survives missing voice capabilities. V1 host IPC is Unix-only;
the plugin does not acquire the broad Windows host bearer descriptor.

### Explicit LAN provisioning response

The 2026-11-24 board preflight exposed a missing connection-discovery step.
The firmware performs `POST` with a nonempty system-info JSON body to its
configured `ota_url`, carrying `Device-Id` as the lowercase colon-separated
Wi-Fi STA MAC. The firmware lane confirmed `/device/ota` on the plugin port
and exposes Custom OTA URL for this board, with no third-party default address.

`serve --provision-file <private pair artifact> --provision-seconds 120` opens
an explicit process-local window (1–300 seconds). The file must match the
currently enabled device token and this listener's WebSocket address.
Only a matching Device-Id receives a `websocket` object with `url`, `token` and `version:2`;
there are no MQTT, activation or firmware fields, nor host/cloud credentials.
An absent/expired window or disabled/revoked pairing rejects the request.
Retries are allowed within the window; stop/restart never restores a window
implicitly. Ordinary WebSocket authorization remains unchanged.

The MAC identifies the paired device but is not a secret: this is an explicitly
opened trusted-LAN bootstrap window, not cryptographic device attestation.
No new durable plaintext credential or generalized OTA/update service is added.
The plugin keeps its own port, independent of any existing Avachan service.
Later board OTA checks require another explicit window. The frozen voice wire
does not change.

## Validation contract

Use real local WebSockets, Unix IPC through the production FastAPI server,
Opus/WAV conversion and provider protocol parsing. Only cloud/model responses
and Model Hub route selection are synthetic. Two contextual spoken turns after
one wake and spoken exit must succeed. The device must never resume capture
before matching software-drain evidence; disconnect/revoke/abort must not replay.

Exercise tool attempts (including final-only native hosted blocks), duplicate
requests, restart, unsupported configuration/old records, unrelated Web work,
private provisioning, repeated lifecycle, process crash, and real ASGI disconnect
cancellation for cloud audio and model work. Full answers stay in host transcripts;
only a short complete sentence projection is sent to TTS. Real acoustics, live
cloud/model/Bot use, deployment and flashing remain unperformed gates.

## Review inventory

First reviewed head `0a6f4a8449` had four findings in three root-cause classes:
authority propagation, live-request lifetime, and speech projection (two comments).
The coordinator's 2026-11-23 decision replaced voice Run admission with the
isolated chat-only path described above. The other fixes use actual IPC disconnect
monitoring, require sentence punctuation, and remove inline-code spans. This is
one findings-bearing reviewed head so far; subsequent reviews must track these
classes and the repository's architecture-rewrite circuit breaker.

## Candidate evidence after the chat-only decision

485 focused device, host, CLI, Bot, IPC/Vault and ordinary Vibey backend tests
passed; seven existing platform-specific cases skipped. Device codec tests ran
with real native libopus. The two-turn test uses the production FastAPI server
on a Unix socket and the real OpenAI Chat protocol parser; its second model
request contains the first question and answer but excludes unrelated Web work.
Seven adversarial function names, final-only function/native hosted blocks,
duplicate IDs and restart all leave no executable Run or external effect.
Model, ASR and TTS disconnect cancellation is tested at the actual ASGI boundary.
Ruff, whitespace checks and the optional wheel build are separate local gates.

The provisioning follow-up extends the real two-turn integration to obtain its
WebSocket URL/token through `POST /device/ota`, plus real HTTP refusal/retry/
expiry/disable/revoke and listener lifecycle cases. Private-file/token/URL
validation has isolated fixtures. The clean review of `795ab1c062` applies to
the prior candidate; this follow-up requires a new exact-head review and CI.
