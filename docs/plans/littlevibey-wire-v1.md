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

The Vibey variant has an empty default `CONFIG_OTA_URL`. With no user-saved
Custom OTA URL, it creates no discovery HTTP client and initializes no
transport. Other boards keep their existing OTA default and MQTT behavior.
An already saved Custom OTA URL remains an explicit user setting; this
candidate does not erase it.

On this board the existing Wi-Fi setup page exposes **Custom OTA URL**.
From Idle, hold the screen for at least two seconds and release to enter
the existing setup AP (`Vibey-<suffix>`). Open its displayed address and fill
`http://<current-host-LAN-address>:8765/device/ota`. There is no compiled host
IP or automatic discovery service. Reserve the host's DHCP lease or update
the saved URL when its address changes. A short touch retains its existing
chat behavior; holds between 500 ms and two seconds do nothing.

The device sends **POST** with its existing nonempty system-information JSON.
`Device-Id` is the Wi-Fi STA MAC, six lowercase hexadecimal octets separated
by colons (synthetic example: `02:00:00:00:00:01`); `Client-Id` is the existing
board UUID. The plugin accepts POST `/device/ota`, not GET.
If NVS contains a device token, the firmware also sends
`Authorization: Bearer <device-token>` on this discovery request. Without a
stored token it omits Authorization entirely. This is the device credential
already used for its WebSocket, never a host/instance/cloud credential.

The optional plugin supports two authorization paths on this same endpoint:

- **Initial pairing, no Authorization header:** explicitly start
  `serve --provision-file <original-private-pair-file> --provision-seconds 120`
  (maximum 300 seconds). During that short window only the exact currently
  enabled paired Device-Id receives the complete device-scoped WebSocket
  object above, including the initial token. Retries tolerate a lost response.
  Without Authorization and outside the window, discovery is rejected.
- **Recovery with Authorization:** an exact Device-Id and a constant-time
  match of its Bearer against the currently enabled pair authorize discovery
  at any time, independently of the initial window. The response contains
  only the current advertised URL and version; it never echoes the token:

  ```json
  {"websocket":{"url":"ws://host.invalid:8765/device/ws","version":2}}
  ```

An empty, malformed or incorrect Authorization header never falls back to
the initial window, even while that window is open. Unknown, disabled,
revoked or mismatched pairs are rejected. Wrong-Bearer attempts have bounded
counting and short-term rate limiting. Neither credentials nor request bodies
belong in logs; recovery responses never contain tokens.

Both paths share the plugin's existing LAN port and `/device/ota` path. Its
listener address/port must match its advertised URL. The MAC is public
identity, not a password: initial bootstrap requires a trusted LAN and an
explicitly opened window. It is not ownership proof. Firmware HTTP header
debug dumps would include Authorization in the existing vendor client, so
this board's canonical configuration explicitly limits compiled logging to
INFO. Offline artifact validation checks the DEBUG request-header format
is absent; no vendor source is changed.

The firmware validates the `url` (`ws://` or `wss://`) against the existing
client's supported DNS/IPv4 authority syntax, bounded numeric port 1–65535,
and absence of whitespace/control characters before persisting the response.
IPv6/userinfo and fragments are unsupported by that client; a query needs
an explicit `/` path. The total URL is bounded to 2048 bytes. A literal colon
in a path requires an explicit authority port because of the client's parser.
Version is exactly 2. Initial pairing requires a nonempty device token.
A successful later response may omit `token` to retain the stored device
credential; an explicitly empty or malformed token is rejected, and omission
without a stored token is rejected. Failed discovery never authorizes reuse
of a persisted transport.
Responses containing `mqtt`, `activation`, `firmware` or `assets` are rejected
before writes. Missing/partial/invalid responses, HTTP errors, an unauthorized request
or unreachable URL leave the device in Idle without a protocol; it does not
fall back to old NVS MQTT or old WebSocket credentials, and does not spend
the legacy exponential retry period before exposing setup. No success chime
is played. Local image-health confirmation occurs after display/audio,
timers and callbacks initialize and before network startup, independently of
Wi-Fi and discovery availability. Neither a missing SSID nor an unconfigured
endpoint keeps an otherwise healthy image pending verification and causes
rollback to an older build's external-service default. These behaviors apply only to this
product; other boards retain their existing transport selection.

After initial pairing, subsequent firmware startups use authenticated
discovery without reopening the initial window. They still require a fresh
successful response; an unreachable plugin or rejected/revoked credential
leaves the device offline and cannot authorize old transports. Saving the
URL alone is not authorization. This response configures a transport; it is
not a firmware update.

The existing setup page preserves multiple networks: a successful save calls
`SsidManager::AddSsid`. A new SSID is inserted at the front without deleting
other entries, up to ten saved networks. The same SSID updates its password;
an eleventh distinct SSID evicts the last entry. This is verified from the
existing page handler and manager implementation; this candidate does not
change SSID storage or station-selection code.

Public access is a later target, not implemented or validated by this LAN
slice. The URL is user configured rather than compiled as a LAN-only route:
an explicit `https://<instance-domain>/device/ota` and a returned `wss://`
endpoint remain supported schemes through the existing certificate-bundle
transport. Later public discovery must authenticate with the stored
device-scoped token using the same Authorization mechanism, and may omit a
new token in its successful response. The Bearer mechanism is implemented
for this LAN slice; a public listener or public unauthenticated pairing route
is not provided. HTTPS/WSS certificate and clock readiness, public deployment,
reachability and revocation acceptance remain separate gates.

WebSocket headers are `Protocol-Version:2`, `Device-Id` (MAC), `Client-Id`
(board UUID), and optional `Authorization:Bearer <device token>`. Device
identity headers are untrusted claims, not owner authentication. Pairing must
use independently authorized host/instance authority and a device-only,
revocable credential; host/cloud credentials never go to the device.

Changing the actual device's OTA URL/NVS, replacing a live endpoint, provisioning,
serial access, and flashing are separate approval gates. Offline artifacts do
not alter current device settings. An unreachable old provisioning URL cannot
be repaired by merely installing the optional plugin.

### App lane acceptance (2026-11-24)

The optional plugin lane (`sesrng8hqsvbp`, PR #4221) accepts the two-path
`POST /device/ota` contract above. Bootstrap remains explicitly windowed;
authenticated recovery uses the currently enabled device pair and returns
only the actual listener URL/version, never a token. A present invalid
Authorization cannot use the bootstrap path. The LAN candidate serves
plaintext HTTP/WS on a concrete private IPv4 address; it does not advertise
WSS or IPv6 to this firmware. Failed Bearers have bounded per-peer accounting
and a 60-second cooldown after five failures. The voice JSON fixture and
capture/playback protocol remain unchanged.
