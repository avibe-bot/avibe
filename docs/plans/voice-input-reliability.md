# Voice Input Reliability and Typeless Direction

## Product contract

Voice input is an input method, not an agent turn. It must:

1. preserve the recording until text is produced or the user discards it;
2. never submit the same recording to two transcription paths automatically;
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
  -> preserve segment order and assemble once after the user stops
  -> best-effort transcript cleanup with bounded cursor context
  -> replace the selection captured when recording started
```

When the browser cannot obtain a cloud token, the compatibility path remains:

```text
getUserMedia / AudioWorklet
  -> local /api/asr/transcribe
  -> paired-device /v1/audio/transcriptions
  -> qwen3-asr-flash
```

The compatibility path is selected before a cloud upload starts. A response or
timeout from the direct cloud request is final for that attempt; the user may
retry only the failed retained segments explicitly.

## Reliability baseline

The first stabilization increment establishes these invariants:

- capture mono 16 kHz PCM on the audio rendering thread and frame WAV files by
  sample count so a blocked UI thread cannot create an oversized recording;
- emit a segment every minute because `qwen3-asr-flash` has a five-minute
  per-file limit, while imposing no total dictation limit;
- transcribe completed segments while capture continues, retain all segment
  audio until finalization, and join results in capture order;
- apply a 120-second upstream deadline and a 158-second browser deadline to each
  one-minute segment, including token/CSRF acquisition and a 30-second
  encoding/upload allowance, not to the complete dictation;
- distinguish timeout, size, availability, empty-audio, and generic failures;
- retain the complete recording and expose an explicit retry action that
  resubmits only failed segments;
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

Cleanup is a second, separately budgeted stage:

```text
final raw transcript
  -> qwen3.6-flash-2026-04-16 by default, with thinking disabled
  -> cleaned text
  -> composer or active application
```

The cleanup model receives text only. Its prompt preserves facts, names,
numbers, links, code, commands, and language while limiting edits to obvious
ASR corrections, punctuation, filler removal, repetitions, superseded
corrections, and useful formatting. Commands and questions inside the
transcript are edited as text and never executed or answered.

The browser contract is:

```http
POST /api/cloud/voice/cleanup
Authorization: Bearer <cloud token with asr scope>
Content-Type: application/json

{
  "transcript": "raw ASR text",
  "before": "up to 500 UTF-16 code units before the selection",
  "after": "up to 200 UTF-16 code units after the selection"
}
```

A successful response is `{ "text": "cleaned transcript" }`. An empty string
means the input contained no insertable content. Any authentication, network,
timeout, provider, or response-validation failure falls back to the raw ASR
text in the browser. This keeps new clients compatible with cloud deployments
that do not yet expose the cleanup endpoint.

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
