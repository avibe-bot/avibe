# Shared system schedule guidance

## Contract

Personal alarms and reminders select the system scheduling path. Requests for
an Agent to perform work later select Harness. This rule reaches every backend
through the existing shared prompt and built-in skill catalog.

The skill uses `vibe schedule create/status/cancel` for iPhone and Computer Use
for Mac. It does not add native capabilities or change authorization. A source
label is presentation, not device or same-host attestation. Without trusted
same-Mac evidence, the user may explicitly choose the Mac running the instance.
Without enforceable selection among multiple iPhones, creation waits for a
resolved execution path.

An operation keeps its identity through retries and cancellation. Only a native
`scheduled` receipt or verified Mac saved state permits a success claim.
Offline, denied and unsupported outcomes never silently change kind, device,
or become a Task.

## Old and new behavior

The shared prompt previously routed all future work toward Harness. It now
first routes system alarms and personal reminders to `use-system-schedule`.
Harness guidance also points there to avoid taking ownership when loaded
first. Skill discovery, packaging and backend orchestration are reused.

This change does not claim exact iPhone routing or both native kinds on every
installed client. Device binding and iOS 26 reminder creation are independent
changes; this skill remains usable with existing clients.

## Evidence and boundaries

- Publish the real built-in catalog into test-owned storage, compose every
  registered backend's prompt and load the skill through the real resolver.
- Parse shipped examples with the real CLI, preserving create/status/cancel
  operation identity. Existing Harness parsing tests cover the adjacent path.
- Use bounded model decision exercises to check intent and target ambiguity.
  These are routing evidence, not real native or Computer Use effects.
- Physical-device creation, signed iOS installation and desktop interaction
  require separate acceptance. No installation or production writes occur
  while validating this guidance.
