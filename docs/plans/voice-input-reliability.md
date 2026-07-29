# Voice Input Reliability and Typeless Direction

## Product contract

Voice input is an input method, not an agent turn. It must:

1. preserve the recording until text is produced or the user discards it;
2. never submit the same recording to two transcription paths automatically;
3. never auto-send the resulting text;
4. return raw ASR text even when optional text cleanup is unavailable;
5. collect timing and failure-stage metadata without logging audio or transcript
   contents.

## Current request path

```text
MediaRecorder
  -> browser uploads multipart audio directly to avibe.bot
  -> qwen3-asr-flash
  -> transcript is appended to the composer draft
```

When the browser cannot obtain a cloud token, the compatibility path remains:

```text
MediaRecorder
  -> local /api/asr/transcribe
  -> paired-device /v1/audio/transcriptions
  -> qwen3-asr-flash
```

The compatibility path is selected before a cloud upload starts. A response or
timeout from the direct cloud request is final for that attempt; the user may
retry the retained recording explicitly.

## Reliability baseline

The first stabilization increment establishes these invariants:

- choose the recorder container from `MediaRecorder.isTypeSupported`;
- derive the uploaded filename from the actual MIME type, ignoring codec
  parameters when selecting the provider format;
- record speech at 32 kbps to reduce upload time and keep a 4:50 clip below the
  current request-size budget;
- stop at 4:50 because `qwen3-asr-flash` accepts at most five minutes;
- apply a 60-second total upstream deadline and a 65-second browser deadline;
- distinguish timeout, size, availability, empty-audio, and generic failures;
- retain a failed recording and expose an explicit retry action;
- log request size, MIME type, duration, provider stage, and attempt count, but
  never audio bytes, credentials, or transcript text.

### Initial service indicators

Measure these by request path, browser family, MIME type, duration bucket, and
release:

- transcription success rate;
- p50, p95, and p99 time from recording stop to text insertion;
- upstream fetch, timeout, HTTP, and empty-result failure rates;
- explicit retry success rate;
- recording-size and duration distributions.

Set production SLOs only after one week of representative measurements. The
first target should be at least 99.5% successful finalization for accepted
recordings, excluding user cancellation and permission denial.

## Streaming path

Batch upload cannot deliver Typeless-class latency. The next capability should
use the provider's realtime ASR model:

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

## Optional text cleanup

Cleanup is a second, separately budgeted stage:

```text
final raw transcript
  -> small text model
  -> cleaned text
  -> composer or active application
```

The cleanup model receives text only. Its prompt must preserve facts, names,
numbers, links, commands, and language while limiting edits to punctuation,
filler removal, obvious repetitions, and paragraph breaks. The raw transcript
is retained for undo and comparison.

Cleanup requirements:

- disabled, literal, and clean modes;
- hard deadline independent of ASR;
- raw-text fallback on timeout or any model error;
- deterministic settings and a versioned prompt;
- regression corpus covering Chinese, English, mixed language, code, URLs,
  names, and numeric dictation;
- no general Agent invocation and no tool access.

## Delivery sequence

1. Ship and observe the batch-path reliability baseline.
2. Add dashboards and alerts for success rate and latency by failure stage.
3. Prototype realtime ASR in the Web composer behind a feature flag.
4. Add the cleanup stage only after raw streaming ASR meets its latency and
   reliability targets.
5. Extract the shared capture/session client for the native system-wide input
   surface.

The batch route remains the fallback and long-tail compatibility path. Long
recordings beyond five minutes require either realtime streaming or the
provider's asynchronous file-transcription model; they must not be forced
through the current synchronous request.
