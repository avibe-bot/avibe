# Model Hub engine channel: minimum publishable plan

**Status: accepted design, not approval to merge or enable the channel.** The channel owner and independent backend reviewer agreed on this plan after [#4239](https://github.com/avibe-bot/avibe/pull/4239). Its final review passed five of six checks. The remaining check blocks an ordinary promotion: even a successful rehearsal can be rejected because the validator expects a different workflow digest from the one that produced the artifact.

## What publishes an update

Merging one manifest to `master` authorizes the engine update. The manifest names the minimum Avibe version and fixes the bytes to sign, including the sequence and timestamp. Changed bytes require a new sequence. Before merge, validate the designated rehearsal run, attempt and artifacts. After merge, publish the current ledger snapshot; do not reconstruct authorization from the PR or old runs.

Support one reviewed rehearsal revision at a time. First freeze the complete qualification workflow and tools in commit **H**. A later trusted caller pins H, so H never has to contain its own pin. The producer runs H's qualification logic. The validator independently computes the expected digest from H, including the workflow, and checks the hosted run record to establish that the designated attempt actually executed H. An artifact's own version claim is not evidence.

Verify the digests of installable packages and compatible rollback targets. A release in the merged `master` manifest stays listed unchanged or is explicitly withdrawn with a permanent withdrawal record and a named fallback, even if its upload never completed. Preflight checks the base manifest; the writer independently checks the authenticated origin. A broken withdrawn package need not be downloadable to withdraw it. If a previous fallback is later withdrawn, retain its immutable metadata without fetching it: new withdrawals must name an eligible live fallback, and clients whose recorded fallback is withdrawn block rather than follow a chain. Signing stays separate from upload. The writer conditionally updates the origin and verifies origin and public readback. Identical retries must not rewrite the origin. No new credential, service or manual signing step is needed.

## When it is publishable

- A real hosted rehearsal artifact, with its original bytes, passes the pinned preflight CLI without mocking the verifier or its expected digest.
- A different workflow revision, a wrong attempt and a tampered artifact are each rejected for the intended reason.
- With a throwaway signing key and a simulated origin, a client adopts an eligible release and rolls back after a withdrawal. Report this separately from what the hosted run proves.

## Release boundary

Implementation starts in a new PR **after the 3.2.8 complete RC is published**. Review once at the end against the evidence above. The 3.2.8 complete RC and stable ship with an empty trust root; the root is planned for the first RC after stable. The bundled CPA v8.1.2 update is independent. This plan does not authorize production key generation, uploads or channel enablement.
