# Voice intent sample (draft only, not part of Avibe)

A minimal App that answers one question outside Avibe: can a third-party App start a Live Activity and the microphone from an App Shortcut without opening?

The variants are separate bundle IDs, built from the same sources:

| Variant | Intent | `UIBackgroundModes` |
| --- | --- | --- |
| A | `AudioRecordingIntent` | none (as Avibe 26.1) |
| B | `AudioRecordingIntent` | `audio` (as Avibe 37.1) |
| C | `AudioRecordingIntent` + `LiveActivityIntent` | `audio` (as Avibe 35.1) |

The helper is a navigation-like App. It plays a voice-prompt-style audio session in the background (`.playback`, `.voicePrompt`, `.duckOthers` + `.interruptSpokenAudioAndMixWithOthers`) and shows its own Live Activity.

Each press logs the following to `Caches/probe.log`:
- the App's state;
- the other-audio diagnostics;
- the Live Activity result;
- the activation result for `.record`, then for `.playAndRecord` + `.mixWithOthers` + `.duckOthers`;
- `record()`.

App Shortcuts run only for a Team-signed bundle, so CI signs with an Apple Development identity from `IOS_DEV_CERT_P12_BASE64` / `IOS_DEV_CERT_PASSWORD`. The Simulator's background-audio and ActivityKit rules may differ from a phone's.
