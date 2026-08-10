# Realtime Voice

The browser Realtime path is enabled by default and preserves the existing HTTP
dictation queue as its fallback. The default model is
`qwen3-asr-flash-realtime`.

The browser sends `start`, 250 ms PCM16 `audio`, and `finish` frames over the
`avibe-asr-v1.<cloud-token>` subprotocol. The cloud route authenticates the
existing `asr` capability, forwards audio to the configured DashScope realtime
endpoint, and returns non-authoritative previews followed by one final text.
The preview is transient editor state; only the server-cleaned final result is
committed to the draft. Provider credentials remain server-side. Handshake,
transport, protocol, timeout, and upstream failures activate the existing HTTP
recording fallback without changing legacy or dictation endpoint contracts.
