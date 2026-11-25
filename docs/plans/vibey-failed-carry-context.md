# Failed-turn carry inputs in Vibey model context

A delivery is admitted by the Turn that claims it. Seeing an earlier copy in a model request is not an acknowledgement. A failed Turn may hand an accepted Harness input to another Turn without losing the sender's run. The transcript and delivery history remain the account of what happened; only the model-facing projection changes.

* Each persisted failed carry supersedes its immediate predecessor occurrence, permanently. The new occurrence enters the model view only when materialized and consumed, at its canonical sequence. Text equality, terminal outcome alone, and the child's current queue state are not evidence of a carry.
* Resolve the projection in the same read transaction as the runtime rows, checkpoint and fork ancestry. A fork pins its immediate source's carry membership along with its sequence bound. No delivery is inserted, released, acknowledged, or modified by a context read.
* A model-generated summary can contain a carried input even without a pinned input reference. A checkpoint whose projection no longer matches its prefix must be bypassed; restore from an earlier clean checkpoint or the canonical rows. Old rows, tool-call/result pairings and replay refusals remain intact. An ordinary request can establish a new continuation witness after the projection changes.
* A mixed input is projected by occurrence and member provenance, not by removing matching strings. If a released, pre-upgrade batch cannot prove which surviving words or attachments belong to which member, retain the historical row rather than lose a sibling's input. Emit a count-only diagnostic once per affected row per process. Do not replay it or change its delivery state.

The sparse carry index is an additive SQLite change. Released writers already create and retain the carry rows; there is no replay, queue migration or transcript rewrite during upgrade. Downgrade removes only the index.
