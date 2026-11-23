# Vibey LAN device plugin (candidate)

`avibe-device` is an optional process for one CoreS3 named **Vibey**. Its
conversation belongs to the existing **littlevibey** Bot. Installing Avibe does
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
  --device-id 'CORE-S3-MAC' --name 'Vibey' \
  --websocket-url 'ws://192.168.1.10:8765/device/ws' \
  --provision-file /private/path/vibey-provision.json
avibe-device --state-dir /path/to/device-state status
avibe-device --state-dir /path/to/device-state doctor
avibe-device --state-dir /path/to/device-state enable
avibe-device --state-dir /path/to/device-state serve --bind 192.168.1.10 --port 8765
```

Use a concrete LAN address; wildcard/public binds are refused. The pairing file
is provisioning material for an authorized device setup step, **not** an open
OTA endpoint. Installation alone cannot replace the board's existing OTA URL.
Changing device configuration/NVS or flashing is a separate operator action.
Keep the LAN trusted: this candidate does not add TLS termination.

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
ASR, the queued littlevibey Run, and TTS have a combined 55-second budget,
inside the firmware's processing watchdog. A missing cloud capability closes
only that conversation; pairing and ordinary Avibe chat remain available.
The next wake can retry after the dependency returns, without restarting Avibe.

The host proves the reply belongs uniquely to this request using the durable
Run, output provenance and accepted Turn inputs. A concurrent Web input,
unproven callback, canceled request or unrelated final is withheld. It never
reads the Bot's latest message. Up to two short complete sentences are spoken;
an answer without a suitable complete sentence is withheld. Full replies stay
in the Bot transcript. No extra summarizer/model is introduced.

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

`tests/test_device_voice.py` uses isolated HOME/state, real loopback WebSockets,
owner-only Unix IPC, real Opus/WAV conversion, real host Run admission and
durable output/delivery records. Only cloud responses and native model output
are synthetic. It exercises two contextual turns after one wake, spoken exit,
pair/revoke/disable, invalid/duplicate receipts, own-microphone input during
playback, disconnection, legacy negotiation, inactivity and repeated lifecycle
disposal. It also enqueues ordinary synthetic Bot chat after plugin shutdown.

Run the tests with the optional package's codec dependencies and native
`libopus` available. Codec cases skip when `opuslib-next` is absent; the host
provenance/admission tests still run in the ordinary suite. Human acoustics,
real synthesis, real littlevibey, device configuration, flashing and deployment
remain separate unperformed acceptance gates.
