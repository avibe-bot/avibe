# Optional LAN voice device

## Change contract

An independently installed `avibe-device` process serves one explicitly paired
CoreS3. Avibe startup and ordinary IM/Web chat do not import or start it. The
plugin owns its device credential, connection, bounded audio buffers and
half-duplex conversation. The host owns cloud credentials and Bot admission.
Only the host calls ASR/TTS. The device never receives instance credentials.

The host's private control channel adds `device.voice` with five operations:
`status`, `transcribe`, `submit`, `result`, and `speech`. Submission targets the
existing `littlevibey` Bot, queued, with a device-origin instruction that voice
is not privileged-action approval. A UUID request ID is the Harness Run ID.
Results require the exact Run, terminal success, persisted output provenance,
and a completed Turn with exactly this input. Shared or uncertain output is
withheld. Full replies remain in the existing transcript; the plugin receives
only a bounded, deterministic sentence projection for synthesis.

ASR uses the paired instance's existing endpoint and multipart WAV upload.
TTS uses `POST /v1/audio/speech`, JSON `{"text":"..."}`, and returns bounded WAV
bytes. Cloud headers stay inside the host. The TTS candidate contract specifies
24 kHz mono PCM16; the plugin converts it to negotiated device Opus.

The accepted device contract is copied to `littlevibey-wire-v1.md`, with
`tests/fixtures/littlevibey-wire-v1.json` shared with the firmware lane.
Avachan is a read-only reference, never a dependency. Wake route revision 4 is
distinct from negotiated voice protocol v1.

The plugin uses scoped process/socket ownership and explicit capability
dependencies, without a generic plugin framework, bus, or Cordis dependency.
Local pairing/diagnosis survives missing voice capabilities. Unix-only host IPC
keeps broad Windows host credentials out of the optional plugin; unsupported
hosts fail explicitly. The process reads only its own pairing file.

The voice prompt identifies unverified device speech and is not an owner
approval. This interface accepts text for an existing Bot; it does not approve
tools or issue device MCP actions. Ordinary host approval policy still applies.

## Evidence required

Real local sockets, real Opus/WAV conversion, isolated persistent pairing state,
and synthetic cloud/Bot peers must complete two contextual turns after one
wake. Actual playback acknowledgement alone reopens capture. Disconnection,
revocation, interruption, duplicate and unrelated output cannot replay speech.
Missing plugin/codecs/cloud capability must fail locally and leave normal chat
independent. Human acoustics, live cloud, a real Bot, deployment and flashing
remain separate unperformed gates.

## Candidate validation, 2026-11-23

The focused host/CLI/Bot regression passed 427 tests; the final optional-device
suite passed 18 cases after adding private provisioning and pending-ASR abort
coverage. Real child-process crash, same-state duplicate process rejection,
repeat stop/start, and two spoken turns run only in isolated state. Changed-file
Ruff and optional wheel build passed. Ordinary CI skips codec-dependent tests
when the optional dependencies are absent; this local run installed those
dependencies into a disposable directory and used the existing native libopus.
The cloud lane's `f0082aed` candidate was inspected read-only for its exact
JSON, paired headers, MIME and 24 kHz PCM16 contract; no shared endpoint was used.

## Review inventory

Head `0a6f4a8449` received four findings in its first Codex review: device-origin
execution inherited local Owner authority; production IPC did not cancel audio
on disconnect; sentence projection accepted unfinished fragments; inline code
was retained. Root-cause classes are authority propagation, live-request lifetime,
and speech projection (two comments, one class). No class has yet recurred on a
second reviewed head.

The lifetime fix uses the existing FastAPI disconnect monitor only for ASR/TTS;
admitted Bot submissions remain independent mutations. Real ASGI tests cover
both audio actions. Projection now requires sentence terminators and removes
inline code including unterminated tails. The focused device/IPC/Vault run passed
61 tests with seven existing platform skips. The authority finding remains open
pending the coordinator's scope decision: prompt text is not an enforcement
boundary, and a fabricated cloud principal is not a valid substitute.
