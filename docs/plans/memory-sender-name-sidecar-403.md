# Sender-name sidecar request contract

Follow-up to #1885, merged as `2f92bd009f13`. The installed
`3.0.15rc1+pr1885.fa13e5411` producer sends `messages[].sender_name`, but its
sidecar guard permits exactly the four older fields. New captures receive
HTTP 403 even though health and historical reads succeed. The mismatch is
also present in base commit `16ca51b64`.

## Change contract

Accept an optional nonblank string `sender_name` of at most 128 characters,
matching the producer's normalized name bound. Keep required fields, unknown
field rejection, identity checks, and content validation unchanged. Older
requests without names remain valid. Do not strip names or bypass the guard.

## Evidence

- Unit: valid Unicode names, length boundaries, user/Agent owner IDs, plain
  and structured content, malformed names, and scope-preservation cases.
- Contract/scenario: MEMORY-SEARCH-019 now runs actual automatic and explicit
  capture payloads through the real sidecar request guard, in both UI languages,
  before acknowledging them through a mocked transport. Local Web, Cloud Web,
  Slack, and Agent-owned captures retain identity and original text.
- Red/green: both scenario variants fail against the unchanged guard before
  applying the fix. All tests use test-owned state and mocked provider egress.
- Residual manual: deploy an approved normal package, then verify a new Web
  capture's add response and processed record. Deployment, host restart, and
  recovery/replay of previously rejected messages are not part of this PR.

## Scope

No credential changes, schema changes, old-memory rewriting, automatic replay,
or failure-history redesign. A healthy runtime alone is not a capture verdict.
