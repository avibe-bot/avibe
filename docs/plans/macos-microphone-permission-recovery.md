# Microphone permission recovery

## Problem and evidence

On macOS Desktop, a hold can say to allow a system prompt without a prompt appearing; subsequent holds or a retry after enabling the microphone in System Settings can remain unsuccessful. The first-request probe currently discards every `getUserMedia` rejection, emits no capture trace, and labels a permission query of `prompt` as an actual system prompt. The hold also treats the browser Permissions API's `denied` answer as authoritative before attempting capture. This is website state, not a fresh macOS TCC status read. Desktop has no mobile native voice permission bridge.

The installed rc25 bundle has `NSMicrophoneUsageDescription`, the audio-input entitlement, Developer ID signing, and hardened runtime. The investigation Mac is locked and Computer Use is unavailable. A real first-use TCC prompt and a denial-to-System-Settings recovery cannot be verified in this environment. Do not claim the complete reported macOS failure has been reproduced or that bundle metadata proves input works.

## Change contract

- Every failed first permission probe produces an accurate visible outcome and a privacy-safe capture trace; a failure is never retained as an instruction to answer a prompt.
- An explicit Desktop hold attempts capture instead of trusting stale website permission state. Browser and mobile native permission checks retain their behavior.
- A permission grant proves only that a stream opened. Existing capture traces, frame counts, and silence classification remain the authority on delivered audio.
- Distinguish refused permission, missing input, failure to open an input, and unsupported capture. State changes from obsolete requests cannot overwrite a later gesture.
- Keep all capture permissions and origin restrictions intact. No device selector or settings test UI, native permission expansion, installed application changes, TCC reset, release, or merge.

## Validation

1. Add failing hook regressions for first probe rejection, repeated attempts, stale Desktop browser denial, native denial, missing input, and obsolete asynchronous answers.
2. Run the actual recording pipeline with a generated non-sensitive signal in the existing isolated browser fixture. Keep only statistics; fixture recognition must not reach live services.
3. Verify shared hold surfaces render the new outcomes and Desktop recovery instructions.
4. Run focused UI suites, test typecheck, lint, and production build. Open an English PR and follow exact-head Codex review and CI.
5. Residual native acceptance: distinct signed test bundle, undetermined -> visible TCC prompt -> Allow -> nonzero input frames; Deny -> clear error; enable in System Settings -> explicit retry -> input frames; second hold; no input hardware. The test bundle must not use the production bundle identifier or start the production Runtime. Record signing and WebKit differences.

## Investigation artifact

`~/.avibe/scratch/macos-desktop-microphone-permission-investigation-20261111.md` holds the detailed evidence, ranked causes, reproduction limits, and the minimal data required from the affected Mac.

## Results

The five initial hook regressions failed on the baseline for swallowed probe errors and the Desktop website-permission gate. The repaired focused suites pass 163 UI tests and 17 telemetry contract tests. Five browser scenarios exercise the real AudioWorklet with generated 440 Hz input: two successful captures each delivered seven chunks, 516 render calls, no empty inputs, and a peak level of -17 dBFS. Refusal and device failures are injected in those browser scenarios; they do not exercise macOS TCC.

UI test and all 21 E2E typecheck suites, lint, production build, changed-file Ruff, and 22 existing Chromium voice-shortcut scenarios pass. Real undetermined-to-Allow and denial-to-System-Settings recovery remain manual gates because the host is locked. The identity-only shell verified a separate undetermined bundle but did not execute capture and is not Release-equivalent.

After enabling System Settings, an explicit Desktop hold reissues `getUserMedia`. If WebKit still rejects access from retained page/system state, the UI advises quitting and reopening Avibe. No automatic recording, forced navigation, or restart is added, and the native recovery mechanism has not been verified on the affected Mac.
