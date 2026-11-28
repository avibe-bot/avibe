# Audio Attachment ASR Plan

## Background

Incoming file attachments are already normalized into `FileAttachment` objects by
each IM adapter, downloaded by the shared `MessageHandler._process_file_attachments`
pipeline, and then attached to `AgentRequest.files`. The final `[User Attachments]`
block is currently rendered inside each agent backend:

- Claude/OpenCode append text file lines in `_prepare_message_with_files`.
- Codex builds text input plus native `localImage` items for images.

That means the correct insertion point for audio transcription is not a single
IM adapter or a single agent backend. It should run after attachment download and
before `AgentRequest` is created, so every platform and backend inherits the same
behavior.

## Goal

When a user sends an audio file attachment:

1. Download and store the attachment exactly as today.
2. Start ASR for eligible audio files through the AVIBE backend's OpenAI
   compatible ASR endpoint.
3. Wait up to 60 seconds in the normal turn pipeline.
4. If ASR finishes successfully within the deadline, append the transcription to
   the user message while still passing the original attachment in
   `AgentRequest.files`.
5. If ASR times out or fails, preserve the current behavior: user text plus the
   existing attachment block only.
6. Expose the feature as a Web UI switch on the Messaging settings page.
7. Add a neighboring switch that controls whether successful transcripts are
   echoed back to the IM user; default echo should be enabled.
8. Use this feature as the forcing function to reorganize Messaging settings
   into clear groups instead of adding one more row to the current long panel.

## Non-Goals

- Do not implement speech recognition inside Vibe Remote.
- Do not call DashScope/Qwen directly from the local client.
- Do not change how agents read the downloaded attachment path.
- Do not add platform-specific audio behavior unless a platform cannot provide
  a usable file attachment.
- Do not show ASR failure details to the user or agent by default; failures are
  fallback conditions, not user-visible errors.
- Do not add a user-managed Qwen/DashScope API key to Vibe Remote. The local
  runtime should call AVIBE, and AVIBE owns the upstream model credential.

## Current Flow

```text
IM adapter
  -> MessageContext(files=[FileAttachment(...)] )
  -> MessageHandler._handle_turn()
  -> _process_file_attachments()
      saves ~/.vibe_remote/attachments/<channel_id>/<timestamp>_<name>
  -> _prepend_message_metadata()
  -> _append_attachment_errors()
  -> AgentRequest(message=..., files=processed_files)
  -> agent backend renders [User Attachments]
```

The ASR step should sit here:

```text
_process_file_attachments()
  -> _augment_message_with_audio_transcripts()
  -> _prepend_message_metadata()
  -> _append_attachment_errors()
```

This placement keeps ASR platform-agnostic and backend-agnostic.

## Proposed Design

### 1. Add a shared ASR service

Create a small core service, for example `core/audio_asr.py`, with three pieces:

- audio eligibility detection
- AVIBE OpenAI-compatible transcription client
- message augmentation formatting

The service should accept downloaded `FileAttachment` objects and return a list
of transcript results. It should not know about Slack, Telegram, Claude, Codex,
or OpenCode.

Suggested shape:

```python
@dataclass
class AudioTranscript:
    attachment_name: str
    local_path: str
    text: str
    duration_ms: int | None = None


class AudioAsrService:
    async def transcribe_attachments(
        self,
        attachments: list[FileAttachment],
        *,
        timeout_seconds: float = 60.0,
    ) -> list[AudioTranscript]:
        ...
```

`MessageHandler` owns the per-turn deadline and catches all ASR exceptions. On
timeout or error, it logs a concise warning and continues without transcripts.

### 2. Audio eligibility

Treat an attachment as audio if either:

- `mimetype` starts with `audio/`
- extension is one of the formats the AVIBE backend supports for the Qwen ASR
  model: `aac`, `flac`, `m4a`, `mp3`, `mp4`, `ogg`, `opus`, `wav`, `webm`

Important edge case: WeChat can include `voice_item.text` directly in extracted
message text, and Vibe Remote already reuses that official platform transcript.
Do not support WeChat `audio/silk` in the first ASR version. If a WeChat voice
message arrives without platform-provided text, keep the current attachment-only
fallback behavior instead of adding local or backend Silk transcoding.

### 3. AVIBE backend API contract

Use an OpenAI-compatible audio transcription contract. Preferred local client
request:

```http
POST {backend_url}/v1/audio/transcriptions
Content-Type: multipart/form-data

model=qwen3-asr-flash
file=@voice.m4a
response_format=json
```

Expected response:

```json
{
  "text": "transcribed speech..."
}
```

Do not put "openai" in the product route. The endpoint is OpenAI-compatible,
but the public path should be AVIBE-owned.

Authentication should reuse the existing remote-access device credential
created by pairing:

- Pairing returns a long-lived `instance_secret`.
- Vibe Remote stores it in `remote_access.vibe_cloud.instance_secret`.
- AVIBE stores only its hash as `remoteAccessDevices.deviceSecretHash`.
- The existing `runtime-status` route validates it through
  `getInstanceByDeviceSecret()`, which also requires the device to be unrevoked
  and the instance to be active.

Recommended ASR auth:

```http
X-Vibe-Instance-Id: <instance_id>
X-Vibe-Device-Secret: <instance_secret>
```

The backend should hash `X-Vibe-Device-Secret`, call the same
`getInstanceByDeviceSecret()` store method, update `lastSeenAt` on success, and
require the latest runtime status to show `tunnel_running=true`. If no recent
runtime status exists or the tunnel is not running, reject ASR with a clear 403
or 409 error. This keeps ASR tied to an actively paired and running local Vibe
Remote.

Why this is the preferred long-term shape:

- It reuses the one credential already issued during pairing.
- It remains valid until the device is revoked, the instance is disabled, or the
  user re-pairs.
- It does not depend on browser OAuth sessions or expiring ID/access tokens.
- It keeps ASR scoped to paired Vibe Cloud devices without adding user-managed
  API keys.
- It avoids overloading the Cloudflare tunnel token. The tunnel token is for
  connectivity; the device secret is already the local-runtime API credential.

For consistency, a small AVIBE helper such as `requireDeviceRuntime()` should be
added and then both `runtime-status` and ASR can share the same credential
validation path. `runtime-status` may keep accepting `instance_secret` in JSON
for backward compatibility, while ASR should use headers because multipart
requests should not mix auth fields into the form body.

### 4. Configuration

Add a local config block instead of burying constants in `MessageHandler`:

```python
@dataclass
class AudioAsrConfig:
    enabled: bool = False
    echo_transcript: bool = True
    timeout_seconds: float = 60.0
    endpoint_path: str = "/v1/audio/transcriptions"
    model: str = "qwen3-asr-flash"
    max_file_bytes: int | None = None
```

Default endpoint base should come from
`config.remote_access.vibe_cloud.backend_url`, defaulting to `https://avibe.bot`.
If remote access is unpaired or `instance_secret` is unavailable, skip ASR and
preserve current behavior.

Default should be **off** until the AVIBE endpoint is available and verified.
The switch belongs under Messaging because it changes how inbound messages are
prepared for agents. It should not live under a specific backend because Claude,
Codex, and OpenCode all consume the same enriched `AgentRequest`.

Persist this block through V2 config load/save and include it in the UI config
payload. Suggested JSON shape:

```json
{
  "audio_asr": {
    "enabled": false,
    "echo_transcript": true,
    "timeout_seconds": 60,
    "endpoint_path": "/v1/audio/transcriptions",
    "model": "qwen3-asr-flash",
    "max_file_bytes": null
  }
}
```

### 5. Message formatting

Append transcripts to the user-authored message before current-time/user-info
metadata is prepended. Keep the original attachment list unchanged.

Recommended block:

```text
[Audio Transcripts]
- <filename>: <transcript text>
```

For a voice-only message, this block becomes the agent-visible content, and the
existing backend-specific attachment renderer still adds:

```text
[User Attachments]
- File: /.../voice.m4a (audio/mp4, 327763 bytes)
```

This satisfies the product requirement: transcript text plus the original file
attachment reaches the agent together.

If `audio_asr.echo_transcript` is enabled, send a concise IM reply immediately
after successful ASR. This echo is independent user feedback and should not wait
for the agent request to be accepted. Keep it factual and short:

```text
Voice transcript:
<transcript text>
```

For multiple audio attachments, echo one combined message with file labels. Do
not echo anything on timeout or failure.

### 6. Timeout and failure behavior

The 60-second timeout should wrap the whole ASR augmentation step. If there are
multiple audio files, run requests concurrently under the same deadline. Partial
success is useful: include transcripts that completed successfully before the
deadline and silently skip failed/timed-out files.

On failure:

- log file name, MIME type, status code/error class, and duration
- do not log audio contents or transcript text at warning/error level
- do not append an `[Attachment Download Errors]` entry for ASR failure
- still pass `processed_files` into `AgentRequest`

### 7. Web UI settings

The current Messaging page has one broad `Message Handling` panel. It mixes
different concepts:

- inbound processing: future audio transcription
- agent context metadata: current time, user info
- run feedback: ACK mode, duration display
- output formatting: quick-reply buttons, Slack link previews
- scope/routing navigation: allowed groups link

Adding ASR as just another row would make the page harder to scan. The page
should become a small set of grouped panels, reusing existing settings
primitives (`SettingsPanel`, `SettingsRow`, `ToggleSwitch`, `CompactField`,
`CompactSelect`) rather than introducing a new visual system.

Recommended grouping:

1. **Input Enrichment**
   - Audio transcription switch.
   - Echo transcription switch, enabled only when audio transcription is on.
   - Optional future status/meta row: only show "Requires Vibe Cloud pairing"
     when remote access credentials are missing and the switch is disabled or
     ineffective.
   - This group owns features that transform inbound user content before it
     reaches an agent.

2. **Agent Context**
   - Include current time.
   - Include user info.
   - These rows both prepend metadata to messages sent to agents, so they
     belong together.

3. **Work Feedback**
   - ACK mode.
   - Show duration.
   - These settings affect how Vibe Remote signals progress and handles a
     running turn. (The former OpenCode error retry limit row was later removed
     along with auto-`'continue'` retries.)

4. **Reply Experience**
   - Quick-reply buttons.
   - Slack link preview suppression, shown only when Slack is enabled.
   - These settings affect outbound bot messages rather than agent input.

5. **Groups and Routing**
   - Existing "Manage channels/groups" link.
   - Keep this as a navigation row or a compact panel at the bottom. It should
     not be mixed with message transformation settings.

The autosave chip can remain in the page header. Each row should still autosave
immediately; no new Save button is needed.

Audio transcription switch behavior:

- Label: "Audio transcription" / "语音转文字"
- Description: "Transcribe supported audio attachments before sending the turn
  to the agent." / "在把消息交给 Agent 前，先转写支持的语音附件。"
- If unpaired with Vibe Cloud, keep the switch clickable only if a manual ASR
  backend is later introduced. For the first version, prefer disabling the
  switch with a short hint that Vibe Cloud pairing is required.
- Do not expose model id or endpoint path in the normal UI. Those are product
  defaults, not day-to-day user controls.

Echo transcription switch behavior:

- Label: "Echo transcript" / "回显转写结果"
- Description: "Send the recognized text back to the chat when transcription
  succeeds." / "语音识别成功后，把转写文本也发回当前对话。"
- Default: on.
- Disabled when audio transcription is off.

### 8. Tests

Add focused unit tests around the shared handler/service boundary:

- non-audio attachments do not invoke ASR
- audio attachment success appends `[Audio Transcripts]` and preserves files
- audio ASR timeout falls back to the original message and files
- audio ASR error falls back to the original message and files
- multiple audio attachments can include partial successful transcripts
- unpaired/missing backend credentials skips ASR
- WeChat voice with existing extracted text is not duplicated by ASR unless a
  real audio attachment is also present
- echo enabled sends a transcript reply on success
- echo disabled does not send transcript replies

Keep tests at the `MessageHandler` level where possible, with a fake ASR service
injected through the controller. Add lower-level client tests for HTTP payload
shape if the AVIBE endpoint contract is finalized.

Add UI-focused tests only if the existing UI test setup already covers settings
pages cheaply. Otherwise, rely on `npm run build` plus manual screenshot review
for this narrow settings layout change.

## Implementation Steps

1. Add `AudioAsrConfig` to V2 config load/save.
2. Add `core/audio_asr.py` with audio detection, HTTP client, and transcript
   block rendering helpers.
3. Wire `AudioAsrService` in `core/controller.py`.
4. Call the service from `MessageHandler._handle_turn` after attachments are
   downloaded and before message metadata is prepended.
5. Add `audio_asr` to the Web UI config payload and `SettingsMessagingPage`
   save patch.
6. Reorganize `SettingsMessagingPage` into grouped panels and add the audio
   transcription and echo switches under Input Enrichment.
7. Add English and Chinese i18n strings for the new group labels, descriptions,
   and Vibe Cloud pairing hint.
8. Add focused tests in `tests/test_message_handler_attachments.py` or a new
   `tests/test_audio_asr.py`.
9. Run targeted Python tests, `ruff check` on changed Python files, and
   `npm run build` for the UI.
10. Once the AVIBE backend endpoint exists, add one integration/manual check
   using an m4a/ogg fixture against staging or production.

## Final Decisions

- AVIBE route: `/v1/audio/transcriptions`.
- Auth: reuse paired device identity with `X-Vibe-Instance-Id` and
  `X-Vibe-Device-Secret`.
- Availability: only paired Vibe Cloud users; backend also checks recent runtime
  status and requires `tunnel_running=true`.
- WeChat Silk: not supported in the first version; rely on WeChat's official
  voice transcript when present.
- Echo: enabled by default and sent immediately after ASR success.
