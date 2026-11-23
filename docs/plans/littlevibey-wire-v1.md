# Local Avibe voice protocol v1

Frozen integration shape: 2026-11-23. This is an optional, negotiated,
half-duplex extension for the `avachan-core-s3` candidate. No legacy Core service
is required. An old firmware or a missing/mismatched negotiation must be
rejected by the local plugin before accepting microphone audio.

## Negotiation and wake

Device hello adds numeric `features.avibe_voice: 1` alongside its existing
capabilities. The local server replies within the existing 10-second hello
timeout:

```json
{"type":"hello","transport":"websocket","session_id":"s1","audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60},"avibe_voice":{"version":1,"route_revision":4}}
```

The firmware enables this extension only for the matching version and its
compiled route revision. Missing negotiation retains the historical firmware
behavior for other servers; the new local plugin must refuse that behavior.
The extension suppresses automatic microphone streaming after wake and during
playback. It leaves capture endpoint detection to the plugin, not ESP-SR VAD.

The initial wake is the existing `listen/detect`, with the exact detected label,
stable ID, `protocol_version:1`, `route_revision:4`, `model_id:"mn7_cn"`,
`model_package_version:"esp-sr-2.4.7"`, event ID, sequence, and device uptime.
The three accepted IDs are `xiaowai-xiaowai`, `hi-xiaowai`, and
`xiaowai-tongxue`. They all route to `littlevibey`, never implicitly to main.

## One complete turn

Every control and acknowledgment below includes the current hello `session_id`.
Request and playback IDs are nonempty ASCII identifiers, at most 96 characters,
using letters, digits, `.`, `_`, `-`, or `:`. Each request ID is unique within
the connection. The firmware bounds request history to 128 turns; reconnect
with a fresh session afterward. No input or output is replayed after reconnect.

1. Server starts a fresh capture:

   ```json
   {"type":"listen","state":"start","session_id":"s1","request_id":"r1"}
   ```

2. Device enables fresh microphone processing, then acknowledges:

   ```json
   {"type":"listen","state":"start","session_id":"s1","request_id":"r1"}
   ```

   Only binary frames arriving after this acknowledgment belong to `r1`.
   The server performs local utterance endpoint detection with bounded capture,
   then closes the capture gate:

   ```json
   {"type":"listen","state":"stop","session_id":"s1","request_id":"r1"}
   ```

3. Device disables microphone processing, invalidates queued/in-flight encoded
   audio, and acknowledges:

   ```json
   {"type":"listen","state":"stop","session_id":"s1","request_id":"r1"}
   ```

   Frames received before this acknowledgment can only belong to `r1`; the
   server discards them once its utterance has closed. No new capture is
   permitted before the previous request's playback drains. This ordered
   start/stop gate supplies attribution for the unchanged binary v2 format.

4. Server begins that request's only playback:

   ```json
   {"type":"tts","state":"start","session_id":"s1","request_id":"r1","playback_id":"p1"}
   ```

   Device disables microphone and wake processing, resets old decoder output,
   enters Speaking, and acknowledges readiness:

   ```json
   {"type":"playback","state":"started","session_id":"s1","request_id":"r1","playback_id":"p1"}
   ```

   **Wait for `started` before sending any binary downlink.** JSON callbacks are
   scheduled onto the device main task; sending audio immediately after the
   start command races that scheduling. The plugin converts cloud WAV to
   16 kHz mono Opus, 60 ms per packet, and paces output at approximately real
   time. Do not burst an entire reply into the bounded decoder queue.

5. After the last binary frame, server seals normal output:

   ```json
   {"type":"tts","state":"stop","session_id":"s1","request_id":"r1","playback_id":"p1"}
   ```

   Stop means **end of stream**, not interruption or successful rendering.
   No more binary packets are allowed for that playback. The firmware waits
   for decode/playback queues and in-flight work to empty, then allows an
   additional 80 ms device tail guard before sending:

   ```json
   {"type":"playback","state":"drained","session_id":"s1","request_id":"r1","playback_id":"p1","boundary":"queues_empty_after_output_write","tail_guard_ms":80}
   ```

   This proves successful decode/output submission and empty software queues,
   plus an elapsed guard exceeding the configured CoreS3 I2S DMA ring
   (6 descriptors × 240 frames / 24 kHz = 60 ms). It does **not** prove audible
   sound, analog amplifier behavior, acoustic echo decay, or human recognition.
   No drain acknowledgment is emitted for an empty, failed, aborted, replaced,
   disconnected, or timed-out playback.

6. The plugin may apply an additional acoustic settling interval, then sends
   the same capture-start shape with fresh `request_id:"r2"`. No new wake word
   is needed. The device never automatically reopens capture at TTS stop or
   drain. Request `r2` starts only through explicit control after `p1` drains.

## Audio, interruption, and cleanup

The binary protocol remains v2: network-order `!HHIII`, followed by exactly
`payload_size` bytes of one Opus packet. Fields are version=2, type=0,
reserved=0, timestamp, payload size. The timestamp is the historical playback
reference echo, not a request ID or a drain acknowledgment. In half duplex it
must not be used to infer completion. Device uplink is 16 kHz mono/60 ms.
The physical codec operates at 24 kHz; existing resampling remains in use.

There is no local VAD utterance-start/stop event in this extension. The plugin
owns silence detection and sends capture stop. Capture is bounded by a device
30-second watchdog; wait/processing/playback are also bounded. A watchdog,
malformed or mismatched negotiated control, queue overflow, decode/output
failure, or unexpected binary downlink fails closed by disconnecting, without
`drained`. No follow-up may open on an estimated timeout.

For explicit interruption/cancellation, plugin disable/revoke, “先不聊了”,
inactivity, or error, close the WebSocket and discard request/playback state.
The device cancels queued output, disables voice processing, and returns to
Idle with local wake detection. This is a v1 half-duplex interaction; arbitrary
speech interruption during output is outside its acceptance claim.

Do not send `tts/sentence_start` or `stt` text to this candidate: historical
handlers log their text. The local plugin keeps full text in Avibe and does
not need those display messages. Do not forward model/tool commands from the
voice conversation to device MCP.

## Pairing and provisioning

The existing provisioning response is:

```json
{"websocket":{"url":"ws://host.invalid/device/ws","token":"DEVICE_SCOPED_EXAMPLE","version":2}}
```

The firmware consumes the response from its configured OTA check URL and
persists the websocket settings in NVS. Omit `mqtt`, `activation`, `firmware`,
and asset-download fields. MQTT wins protocol selection if both transport
objects are present. This response configures a transport; it is not a
firmware update or device ownership proof.

WebSocket headers are `Protocol-Version:2`, `Device-Id` (MAC), `Client-Id`
(board UUID), and optional `Authorization:Bearer <device token>`. Device
identity headers are untrusted claims, not owner authentication. Pairing must
use independently authorized host/instance authority and a device-only,
revocable credential; host/cloud credentials never go to the device.

Changing the actual device's OTA URL/NVS, replacing a live endpoint, provisioning,
serial access, and flashing are separate approval gates. Offline artifacts do
not alter current device settings. An unreachable old provisioning URL cannot
be repaired by merely installing the optional plugin.


## App lane acceptance

The avibe-app lane (`sesrng8hqsvbp`, 2026-11-23) accepts this frozen v1 wire.
Its shared subset fixture is `tests/fixtures/littlevibey-wire-v1.json` in the
`feat/littlevibey-device-plugin` worktree. The firmware lane confirmed the field
shapes and corrected the wake session/event IDs; those corrections are included.
The app's synthetic integration uses real WebSocket v2 framing, Opus conversion,
and request/playback gates. This agreement does not claim real-device or acoustic
validation, and `drained` retains only the software-boundary meaning above.
