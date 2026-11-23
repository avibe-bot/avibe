# Vibey LAN device plugin (candidate)

`avibe-device` is an optional process for one CoreS3 named **Vibey**. Its
conversation uses the identity and configured model of the existing
**littlevibey** Bot in a host-enforced chat-only path. Installing Avibe does
not install or launch this plugin. It does not run an Avachan Core service.

The host must include the `device.voice` private IPC interface from this
candidate. V1 supports macOS/Linux owner-only Unix IPC; it does not load the
Windows control descriptor or acquire its broad host bearer credential.
The plugin only calls the host's named voice operations, never reads host
configuration, and never receives cloud credentials. A native `libopus` library
is required. Python dependencies are declared in this package's `pyproject.toml`.

Build this directory as a normal Python wheel, then install that wheel in the
same environment as the candidate Avibe wheel. The package has its own
`avibe-device` entry and the host provides the lazy `vibe device` entry.
No package has been published by this change. Do not install the candidate into
a running agent environment merely to test it.

## Local lifecycle

Pairing and diagnostics work without cloud pairing. Pairing uses local host
authority and creates a **device-only** credential. It starts disabled and
writes the credential to the explicitly named, owner-only provisioning file,
not to stdout. The plugin's durable record contains only its hash.

```sh
avibe-device --state-dir /path/to/device-state pair \
  --device-id 'aa:bb:cc:dd:ee:ff' --name 'Vibey' \
  --websocket-url 'ws://192.168.1.10:8765/device/ws' \
  --provision-file /private/path/vibey-provision.json
avibe-device --state-dir /path/to/device-state status
avibe-device --state-dir /path/to/device-state doctor
avibe-device --state-dir /path/to/device-state enable
avibe-device --state-dir /path/to/device-state serve \
  --bind 192.168.1.10 --port 8765 \
  --provision-file /private/path/vibey-provision.json --provision-seconds 120
```

Replace the example device ID with the board's **Wi-Fi STA MAC**, exactly
lowercase and colon-separated. Use a concrete LAN address; wildcard/public binds
are refused. In the board's Wi-Fi setup page, set **Custom OTA URL** to
`http://192.168.1.10:8765/device/ota` before starting its connection check.
This firmware page writes `ota_url`; installing the plugin alone does not change
the board. Device configuration and flashing remain separate operator actions.
Pairing validates this local listener's capabilities before writing state:
use `ws://` with a concrete private IPv4 address and valid port. `wss://`, hostnames, IPv6,
wildcard/public addresses, credentials, query strings and fragments are refused.
This LAN plugin does not terminate TLS.

Pairing publishes the private provisioning file before committing the device
record. If writing fails or the process exits partway through, rerun the same
`pair` command after fixing the filesystem problem: the next state operation
cleans up an uncommitted attempt, or recognizes an already complete pair.
Existing unrelated files and pairings are never overwritten. State changes
and authentication reads share a process-safe lock, so concurrent enable and
revoke commands cannot restore a revoked token.

The explicit `serve --provision-file` option opens a 120-second window (at most
300 seconds) on the **same plugin port**. `POST /device/ota` checks `Device-Id`
against the currently enabled pairing and returns only
`{"websocket":{"url":"ws://192.168.1.10:8765/device/ws","token":"<device-only token>","version":2}}`.
The existing file must be an owner-private regular file whose token matches
this pairing and whose WebSocket address matches this listener. No MQTT,
activation or firmware-update response is served. No cloud or host credential
is read or returned. The board uses this response for its authenticated
WebSocket connection; no token needs to be pasted into its setup page.

Missing/other device IDs and disabled/revoked pairings are refused. A request
without Authorization requires the open bootstrap window; retries are allowed
within it because a response can be lost. Expiry closes credential delivery;
it does not revoke the paired WebSocket token or interrupt a conversation.
Stopping the plugin also closes the window, and a normal later `serve` without
the option leaves it closed.

After pairing, the firmware keeps its device token and sends
`Authorization: Bearer <device token>` on each OTA check, including after a
power cycle. Matching the enabled device ID and token returns only
`{"websocket":{"url":"ws://192.168.1.10:8765/device/ws","version":2}}`,
using the actual listener address. It **does not return the token**; the
firmware retains its existing one. This path works without a provisioning
file/window, including after the plugin restarts. An incorrect or empty
Authorization header is rejected without falling back to bootstrap. Five
failed bearer checks from one peer impose a 60-second discovery cooldown.
Revoke invalidates both WebSocket access and authenticated discovery.

Keep the private artifact only for an explicitly authorized fresh setup, such
as after clearing the board's saved token; after revoke, remove it and create a
fresh pair/file. Normal device power cycles no longer require reopening a window.
If the board still stores a revoked token, clear that credential through a
separately authorized device setup before initial bootstrap; an old Bearer
cannot use the new window.

This is trusted-LAN provisioning: a MAC address is **not proof of possession**
and another LAN peer that knows it could fetch the device token during the
explicit window. Keep the LAN trusted and the window short; this candidate does
not add TLS termination. The plugin uses its own port, so an existing Avachan
service need not be stopped. Reserve the host's LAN address or update both the
private provisioning file's URL and the board's Custom OTA URL when it changes.

In a second local shell, `stop` or `disable` ends the listener and its current
connection within the bounded shutdown path while preserving pairing and alias.
`revoke` also removes the pairing and invalidates the device token. SIGINT or
SIGTERM disposes the running process without changing its enabled preference.
After `enable`, explicitly run `serve` again. There is no hidden daemon,
autostart integration, automatic host restart, subscription, or durable speech
outbox. `status` describes the configured pairing; `doctor` checks the codec
and host capability separately. TTS availability is authoritative only when
the cloud accepts a synthesis request, not inferred from an ASR-ready flag.
Each listener is bound to a lifecycle generation. Even an immediate
`stop`/`disable` followed by `enable` invalidates the old listener, connections
and provisioning window; enabling only changes the preference for a new serve.
Lifecycle commands reach the running listener through its private control
socket. It cancels and joins all connection and HTTP writer tasks, cleans up
its host conversation, then commits the new generation. A stopped listener
cannot initiate later work. If the listener does not acknowledge within 12
seconds, the CLI verifies its PID and creation time, terminates only that plugin
process, and confirms exit before changing state. The state directory and
control socket are owner-only; keep the directory path short enough for the
platform Unix socket limit (104 bytes on macOS).

## Conversation and failure behavior

The firmware must negotiate local voice v1 and wake route revision 4, as in
[`littlevibey-wire-v1.md`](../../docs/plans/littlevibey-wire-v1.md).
The accepted wake IDs are `xiaowai-xiaowai`, `hi-xiaowai`, and
`xiaowai-tongxue`. Older firmware is refused.

One wake starts capture. The host-controlled capture gate supplies each
utterance a fresh UUID. The plugin performs bounded energy endpoint detection,
sends `listen/stop`, and waits for the board to acknowledge capture is closed.
ASR, isolated littlevibey text generation, and TTS have a combined 55-second budget,
inside the firmware's processing watchdog. A missing cloud capability closes
only that conversation; pairing and ordinary Avibe chat remain available.
The next wake can retry after the dependency returns, without restarting Avibe.

The host mints a conversation identity and binds each synchronous answer to
its request ID. Context contains only this device's current conversation,
bounded to the last eight exchanges. Shared Web/IM history is not read or
updated. Full replies remain in owner-private Avibe voice transcripts. Inspect
those locally with `vibe device history`, then
`vibe device history --conversation-id <id>`.

This surface can only chat and suggest. It uses the Bot's configured model
through existing protocol adapters, with no executable Agent loop, tools,
native hosted declarations, MCP, terminal/files, Vault, delegation or jobs.
Tool-call responses are rejected at the host boundary. A work request must be
sent as a new, specific request in signed-in Avibe Web; saying “confirm” to the
device grants no authority. Normal Web turns keep their existing permissions.

Up to two short complete sentences are spoken. Inline code and unfinished
fragments are removed; an answer without a suitable complete sentence is withheld.
No extra summarizer is introduced. Transcripts are never resumed as work after
a restart, and duplicate or previous-connection requests are rejected.

Cloud PCM16 mono WAV at 24 kHz is resampled and encoded to Opus at 16 kHz,
60 ms per packet. Audio begins only after matching `playback/started`, and is
paced at real time, with a 40-second maximum to leave watchdog margin. Sending the last packet is not playback completion.
Only matching `playback/drained` with the negotiated boundary and tail guard
allows the next capture, after an additional 200 ms settling interval.
This is half duplex. It does not support spoken barge-in.

“先不聊了”, configured inactivity (default 20 seconds, maximum 25), a socket
loss, malformed control, overflow, failed dependency or mismatched receipt
closes the connection and discards pending speech. A new connection begins
idle with new identities; there is no replay. The initial VAD threshold and
settling interval require later acoustic validation on a real device.

## Validation

`tests/test_device_voice.py` and `tests/test_device_chat.py` use isolated HOME/state,
real loopback WebSockets, the production FastAPI Unix IPC server, real Opus/WAV
conversion, and real model protocol parsing. Cloud/model responses and Model Hub
route selection are synthetic. They exercise two contextual turns after one wake,
spoken exit, unrelated Web admission, private pair/revoke/disable, invalid/duplicate
receipts, own-mic input during playback, disconnection, legacy negotiation,
inactivity and repeated lifecycle disposal. An owned child-process crash leaves
ordinary host chat available.

The two-turn test first obtains its WebSocket URL/token through the real local
OTA response. Separate HTTP tests cover exact device identity, retries, expiry,
disable/revoke, listener stop/restart with no reopened window, private-file
validation and absence of extra response fields or leaked credentials.
Authenticated rediscovery survives window expiry and plugin restart without
returning a token; tests also cover old-token rejection, no invalid-bearer
fallback, bounded cooldown and an untrusted Host header.

`tests/test_device_pairing.py` exercises concurrent mutations across processes,
write/flush/fsync failures, process death at each pairing publication boundary,
and retry without manual state repair. A paused real serve process verifies
that stop/disable followed immediately by enable cannot revive an old scope.
`tests/test_device_scope.py` adds real WebSocket interleavings across handshake,
wake, host operations and playback using production Host and a real Unix HTTP
endpoint. Lost open responses and library writer tasks are explicit cases.
`tests/device_transport_probe.py` additionally checks writer cancellation and
state publication on supported Python 3.10/3.11 interpreters.

Adversarial model responses try terminal, files, Vault, MCP, delegation, background
and hosted tools without creating effects or Runs. Duplicate/restart requests,
old records and unsupported configurations fail closed. Real ASGI tests cover
cancellation of ASR/TTS and model work when the plugin disconnects.

Run codec tests with the optional Python dependencies and native `libopus`.
Codec cases skip when `opuslib-next` is absent; host chat/authority tests run in
the ordinary suite. No live keys, shared endpoint, real littlevibey conversation,
service restart, device provisioning, flashing or deployment is part of these tests.
