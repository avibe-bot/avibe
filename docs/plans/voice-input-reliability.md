# Voice Input Reliability and Typeless Direction

## Product contract

Voice input is an input method, not an agent turn. It must:

1. preserve the recording until text is produced or the user discards it: a
   failed request, a broken capture, or an empty transcript of a take that held
   speech never discards it;
2. never submit the same recording to two transcription paths at once; a
   request that got no answer may be repeated, on the same path or the other;
3. never auto-send the resulting text;
4. return raw ASR text even when optional text cleanup is unavailable;
5. support continuous dictation without an arbitrary total recording limit;
6. collect timing and failure-stage metadata without logging audio or transcript
   contents.

## Current request path

```text
getUserMedia
  -> AudioWorklet mono PCM capture on the audio rendering thread
  -> independently decodable 16 kHz WAV segments framed by sample count
  -> upload completed segments directly to avibe.bot while capture continues
  -> qwen3-asr-flash per segment
  -> receive an encrypted receipt for each completed segment
  -> submit the final segment, prior receipts, and bounded cursor context
  -> server validates order, joins overlap, and runs cleanup once
  -> replace the selection captured when recording started
```

The client never receives or joins intermediate transcript text. Encrypted
receipts bind the transcript to the instance, dictation id, sequence, and
overlap.
They let the server remain stateless between segment uploads without exposing
raw intermediate text to the client or requiring a transcript database. If the
recording stops exactly at a segment boundary, the client sends a small
finalize-only request containing the receipts and cursor context.

The local Workbench obtains its cloud token as a device principal; see
`docs/voice-realtime-prototype.md`. When the browser cannot obtain a cloud
token, the compatibility path remains:

```text
getUserMedia / AudioWorklet
  -> local /api/asr/transcribe
  -> paired-device /v1/voice/dictations
  -> the same receipt and finalization contract
```

The compatibility path is taken when no cloud token can be obtained, and also
when a dictation request to avibe.bot fails before any answer (a network error,
or a refused token that could not be replaced). Repeating a dictation request is
safe: segments are recognized statelessly into receipts, and the final request
only joins receipts and runs cleanup. A response or a timeout from the direct
cloud request is final for that attempt.

## Failure handling

Every dictation request is bounded and retried; the take's audio is kept until
its text lands or the user discards it.

- Each request has its own deadline, sized to what it carries: 15 seconds, plus
  the upload at 128 KB/s, plus the cleanup allowance (35 seconds) for the final
  request, capped at the full compatibility budget. A request on a dead path
  ends there instead of whenever the platform gives up.
- A request that fails for the network or the service (no answer, a timeout,
  `unavailable`, or a 5xx, 408, or 429 answer) is tried again after 1 and 3
  seconds. An empty, oversized, or rejected request is not.
- While the browser reports no network (`navigator.onLine === false`), a request
  fails at once instead of waiting for the platform's network timeout.
- Segments sent while recording retry on their own until capture stops. From
  then on finalization owns them: it sends every segment that still has no
  receipt, then the last segment, and starts no automatic attempt later than 10
  seconds after the release (or after the realtime result failed). The take is
  then left on its card for the user to retry, with its audio.
- A capture that breaks midway (the microphone track ends, the recorder fails to
  stop) keeps and transcribes what it recorded until then. Only a graph that
  never rendered a frame, which recorded silence, has nothing to keep.
- An empty transcript of a take whose microphone level showed at least 400 ms
  above about −40 dBFS is a recognition that failed: the take is kept and can be
  retried. An empty transcript of a quiet take is reported as no speech.
- A take with no live words whose loudness never holds three readings (about
  100 ms) at or above about −44 dBFS within a third of a second is silence. The
  readings are 32 ms RMS windows of the take's own recorded PCM, the tail the
  recorder flushes after the release included, so what is judged is exactly
  what would be transcribed. Such a take
  is not sent to recognition, which on near-silence can return its hotwords as
  text, and is reported as no speech. The gate is deliberately loose: speech 33
  dB under ordinary iPhone speech (−14 dBFS) still clears it, a room with nobody
  speaking measured −49 to −86 dBFS, and live preview words always override it.
  Without loudness readings nothing is gated. Each dropped take logs a
  `dictation_gated` reliability event with only its duration, the readings that
  reached the gate, and its loudest reading in dBFS, so a false block shows in
  the logs.
- A realtime socket that never becomes ready drops the cloud token it carried
  and mints a new one once, because a browser cannot tell a refused token from a
  failed network at the handshake.

## Reliability baseline

The first stabilization increment establishes these invariants:

- capture mono 16 kHz PCM on the audio rendering thread and frame WAV files by
  sample count so a blocked UI thread cannot create an oversized recording;
- emit a segment every minute because `qwen3-asr-flash` has a five-minute
  per-file limit, while imposing no total dictation limit;
- transcribe completed segments while capture continues, retain all segment
  audio until finalization, and return encrypted receipts rather than raw
  intermediate transcript text;
- apply independent 120-second ASR and 30-second cleanup deadlines, plus a
  5-second server-finalization allowance; the browser grants 193 seconds for
  token/CSRF acquisition, encoding/upload, and the complete server request,
  not for the complete dictation;
- distinguish timeout, size, availability, empty-audio, and generic failures;
- retain the complete recording and expose an explicit retry action that
  resubmits only segments without a receipt, through whichever path works then;
- keep pending and retryable batches keyed by chat session so ordinary
  navigation cannot discard audio;
- log request size, MIME type, duration, provider stage, and attempt count, but
  never audio bytes, credentials, or transcript text.

### Initial service indicators

Measure these by request path, browser family, MIME type, total dictation
duration, segment duration, and release:

- transcription success rate;
- p50, p95, and p99 time from recording stop to text insertion;
- segment backlog at recording stop;
- upstream fetch, timeout, HTTP, and empty-result failure rates;
- explicit retry success rate;
- recording-size and duration distributions.

Set production SLOs only after one week of representative measurements. The
first target should be at least 99.5% successful finalization for accepted
recordings, excluding user cancellation and permission denial.

## Streaming path

Segmented batch upload removes the long-dictation limit and overlaps inference
with capture, but it cannot deliver Typeless-class partial-result latency. The
next capability should use the provider's realtime ASR model:

```text
capture client
  -> ephemeral ASR session token
  -> authenticated WebSocket
  -> 20-40 ms mono PCM or Opus frames
  -> partial transcript events
  -> final raw transcript
```

The composer should render partial text separately from the persisted draft.
Only a final transcript is appended to the draft. A dropped WebSocket may resume
from the last acknowledged audio sequence while the client still holds its
bounded local audio buffer.

Acceptance targets for the streaming path:

- p95 first partial transcript under 800 ms on a healthy connection;
- p95 final transcript under 1.5 seconds after the user stops speaking;
- no lost final audio when the stop action races the last frame;
- raw transcript remains usable when cleanup fails.

## System-wide input

System-wide dictation should reuse the streaming protocol through a native
capture client, not automate the Web composer:

- global hotkey or press-and-hold capture;
- active-application text insertion through the operating system accessibility
  API, with clipboard insertion as an explicit fallback;
- local recording indicator and cancel action;
- the same ephemeral cloud credential and privacy-safe telemetry as Web;
- raw audio kept only in a bounded in-memory retry buffer unless the user opts
  into history.

Browser extension support may follow for browser-only users, but it should use
the same capture/session library and insertion contract.

## Text cleanup and insertion

Cleanup is a separately budgeted server-side stage within finalization:

```text
final raw transcript
  -> qwen3.7-plus-2026-05-26 by default, with thinking disabled
  -> cleaned text
  -> composer or active application
```

The cleanup model receives text only. Its prompt preserves facts, names,
numbers, links, code, commands, and language while limiting edits to obvious
ASR corrections, punctuation, filler removal, repetitions, superseded
corrections, and useful formatting. Commands and questions inside the
transcript are edited as text and never executed or answered.

The browser sends each non-final segment as multipart form data:

```http
POST /api/cloud/voice/dictations
Authorization: Bearer <cloud token with asr scope>
Content-Type: multipart/form-data

file=<audio>
dictation_id=<opaque client id>
sequence=0
overlap_ms=0
final=false
```

The response is `{ "receipt": "<encrypted receipt>", "sequence": 0 }`.
The final request uses the same endpoint and adds all prior `receipt` fields,
`before` (up to 500 UTF-16 code units), and `after` (up to 200). A successful
final response is `{ "text": "cleaned transcript", "cleanup": "success" }`.
When cleanup fails, the server returns the assembled raw ASR text with
`"cleanup": "fallback"`.

A bare legacy request to `/api/cloud/audio/transcriptions` containing only
`file` is transcribed and cleaned in the same request, with empty cursor
context. This gives old clients cleanup without changing their request shape.
The paired-device `/v1/audio/transcriptions` route keeps bare file requests raw
for IM audio attachments; the local composer uses the independent
`/v1/voice/dictations` route for the combined cleanup path.

The composer captures its serialized draft and selection on the microphone
button's `pointerdown`, before focus moves to the button. Keyboard activation
captures synchronously before microphone permission is awaited. The draft is
read-only during recording and finalization. Final text replaces the captured
selection only when the current serialized draft still exactly matches the
snapshot; otherwise the text remains available through recovery controls and is
never silently appended elsewhere.

Cleanup requirements:

- hard deadline independent of ASR;
- raw-text fallback on timeout or any model error;
- deterministic settings and a versioned prompt;
- regression corpus covering Chinese, English, mixed language, code, URLs,
  names, and numeric dictation;
- no general Agent invocation and no tool access.
- no standalone browser cleanup request after transcription finalization.

## Delivery sequence

1. Ship and observe the continuous segmented-batch reliability baseline.
2. Add dashboards and alerts for success rate and latency by failure stage.
3. Prototype realtime ASR in the Web composer behind a feature flag.
4. Observe cleanup latency, fallback rate, and edit quality before adding
   user-facing literal/clean modes.
5. Extract the shared capture/session client for the native system-wide input
   surface.

The segmented batch route remains the fallback and long-tail compatibility path.
Imported audio files that are already long recordings should use the provider's
asynchronous file-transcription model rather than being decoded and split in the
browser.
