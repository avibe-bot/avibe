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

The explicit `serve --provision-file` option opens a 120-second window (at most
300 seconds) on the **same plugin port**. `POST /device/ota` checks `Device-Id`
against the currently enabled pairing and returns only
`{"websocket":{"url":"ws://192.168.1.10:8765/device/ws","token":"<device-only token>","version":2}}`.
The existing file must be an owner-private regular file whose token matches
this pairing and whose WebSocket address matches this listener. No MQTT,
activation or firmware-update response is served. No cloud or host credential
is read or returned. The board uses this response for its authenticated
WebSocket connection; no token needs to be pasted into its setup page.

Missing/other device IDs, disabled/revoked pairings, absent windows and expired
windows are refused. Retries during the window are allowed because a response
can be lost. Expiry closes credential delivery; it does not revoke the paired
WebSocket token or interrupt an established conversation. Stopping the plugin
also closes the window, and a normal later `serve` without the option leaves it
closed. A later board boot/OTA check needs a newly opened window: explicitly
start the plugin with the provisioning options again. Keep the private file
for that purpose; after revoke, remove it and create a fresh pair/file.

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

Adversarial model responses try terminal, files, Vault, MCP, delegation, background
and hosted tools without creating effects or Runs. Duplicate/restart requests,
old records and unsupported configurations fail closed. Real ASGI tests cover
cancellation of ASR/TTS and model work when the plugin disconnects.

Run codec tests with the optional Python dependencies and native `libopus`.
Codec cases skip when `opuslib-next` is absent; host chat/authority tests run in
the ordinary suite. No live keys, shared endpoint, real littlevibey conversation,
service restart, device provisioning, flashing or deployment is part of these tests.
