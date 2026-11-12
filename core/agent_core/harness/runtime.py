"""Pure retained-row closure for a checkpoint's derived runtime capsule.

A capsule is not another transcript: entries are references to original canonical
rows. The storage owner validates a full prefix once, persists these references
and scalar facts, and subsequently supplies this closure plus its new suffix.
Historical projection and arbitrary historical cuts still use the full store.

The bound is the live semantic closure, not a fixed row count. A legacy memo or
an anchor preceding the first checkpoint can legitimately retain a large prefix.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.harness.context import _valid_usage
from core.agent_core.harness.projection import ProjectionError, _index_results, _ordered, context_view
from core.agent_core.harness.store import ContextEntry


@dataclass(frozen=True)
class RunTranscript:
    """Canonical runtime rows and non-model facts of their certified full prefix.

    ``cache_records`` holds response records only. The caller restores the running
    Session's audit records separately: a fork inherits responses, not its parent's
    audit-only requests. ``entries`` always retains the actual prefix head, so an
    incremental fold can distinguish old retained rows from newly appended rows.
    """

    entries: tuple[ContextEntry, ...]
    response_clocks: Mapping[tuple[str, str], float] = field(default_factory=dict)
    cache_records: tuple[Mapping[str, Any], ...] = ()
    change_seqs: Mapping[str, int] = field(default_factory=dict)


def is_anchor_response(row: ContextEntry) -> bool:
    """Whether this row has exactly the request/usage facts last_anchor accepts."""
    return row.kind == "response" and row.payload.get("request") is not None and _valid_usage(row.message)


def compact_runtime(
    rows: Sequence[ContextEntry], *, prior: RunTranscript | None = None, preserve_anchor: bool = True
) -> RunTranscript:
    """Retain exactly the dependencies required by current runtime consumers.

    ``rows`` is a complete canonical prefix, or a previously certified closure
    followed by its canonical suffix. Validate before selecting: a bad old edit
    or orphan result must not become valid merely because its row was discarded.
    Missing cross-boundary dependencies raise the ordinary ``ProjectionError``;
    storage must recover them from canonical history and repair its capsule.

    This function does not mutate rows, rewrite messages, or include scalar facts
    in model context. The returned closure preserves current projection, the
    latest usage anchor's pre-response projection, all pending calls, and the
    input rows a subsequent memo can offer. ``preserve_anchor=False`` derives the
    base reference set for storage: it may replace the exact set ONLY when the
    loaded suffix contains a new eligible response after this checkpoint. Until
    then the old anchor's separate reference set remains necessary.
    """
    ordered = _ordered(rows)
    view = context_view(ordered)
    results, pending = _index_results(ordered)
    clocks = dict(prior.response_clocks) if prior is not None else {}
    changes = dict(prior.change_seqs) if prior is not None else {}
    tracker = CacheTracker()
    previous_head = max((row.context_seq for row in prior.entries), default=0) if prior is not None else 0
    if prior is not None:
        previous_checkpoint = next((row for row in reversed(prior.entries) if row.kind == "compaction"), None)
        for row in ordered:
            if row.context_seq <= previous_head or row.kind != "compaction":
                continue
            if previous_checkpoint is not None:
                previous = previous_checkpoint.payload
                if (
                    row.payload["first_kept_seq"] < previous["first_kept_seq"]
                    or row.payload["summarized_to_seq"] < previous["summarized_to_seq"]
                    or ("memo" in previous and "memo" not in row.payload)
                ):
                    # These historical shapes can be valid, but may resurrect
                    # rows the prior certificate deliberately omitted. Only the
                    # canonical store can supply those rows; projection alone
                    # cannot detect their absence.
                    raise ProjectionError(f"checkpoint {row.row_id} requires canonical prefix rehydration")
            previous_checkpoint = row
        tracker.restore((), prior.cache_records)
    # Do not replay retained OLD rows over the prefix's winner on equal clocks.
    # CacheTracker deliberately chooses the later row when started_at ties.
    tracker.restore([row for row in ordered if row.context_seq > previous_head], ())
    for row in ordered:
        if row.kind == "response" and row.created_at is not None:
            origin = row.message.origin
            key = (origin.provider, origin.model)
            clocks[key] = max(clocks.get(key, row.created_at), row.created_at)
        if row.kind in {"compaction", "context_edit"}:
            changes[row.kind] = max(changes.get(row.kind, 0), row.context_seq)

    checkpoint = view.compaction
    if checkpoint is None:
        kept = ordered
    else:
        floor = checkpoint.payload["first_kept_seq"]
        pins = set(checkpoint.payload["kept_inputs"])
        # Match last_anchor's predicate, including failed/zero-usage responses.
        anchor = next((unit.lead for unit in reversed(view.units) if is_anchor_response(unit.lead)), None)
        if preserve_anchor and anchor is not None and anchor.context_seq < checkpoint.context_seq:
            before = next(
                (row for row in reversed(ordered)
                 if row.kind == "compaction" and row.context_seq < anchor.context_seq),
                None,
            )
            if before is None:
                floor = 0
            else:
                floor = min(floor, before.payload["first_kept_seq"])
                pins.update(before.payload["kept_inputs"])

        selected = {
            row.context_seq for row in ordered
            if row.context_seq >= floor and row.kind != "context_edit"
        }
        selected.update(pins)
        # A rendered checkpoint's state strings are not the hook's agent_state.
        state = next((row for row in reversed(ordered) if row.kind == "agent_state"), None)
        if state is not None:
            selected.add(state.context_seq)
        selected.update(row.context_seq for row in ordered if row.kind == "replay_refusal")
        selected.update(owner.context_seq for owner, _ in pending.values())

        # Mirror _memo_bounds: v4's first v5 consolidation offers ALL older
        # inputs; v5 offers unsummarized inputs and releases previously pinned
        # inputs, even if either is before first_kept_seq.
        boundary = checkpoint.payload["summarized_to_seq"] if "memo" in checkpoint.payload else 0
        selected.update(
            row.context_seq for row in ordered
            if row.kind == "input" and (row.context_seq > boundary or row.context_seq in pins)
        )

        by_id = {row.row_id: row for row in ordered}
        # Keep the true head even when it is an edit of a summarized result.
        # This also makes incremental scalar-cache folding unambiguous.
        head = ordered[-1]
        selected.add(head.context_seq)
        if head.kind == "context_edit":
            selected.add(by_id[head.payload["target_event_id"]].context_seq)

        batches: dict[int, list[int]] = {}
        owners: dict[int, int] = {}
        for (owner_seq, _), result in results.items():
            batches.setdefault(owner_seq, []).append(result.context_seq)
            owners[result.context_seq] = owner_seq
        # Closing one result includes its owning RESPONSE and every sibling
        # result, not any other instance that happens to reuse its call id.
        for seq in tuple(selected):
            if seq in owners:
                selected.add(owners[seq])
        for seq in tuple(selected):
            selected.update(batches.get(seq, ()))
        selected.update(
            row.context_seq for row in ordered
            if row.kind == "context_edit"
            and by_id[row.payload["target_event_id"]].context_seq in selected
        )
        kept = [row for row in ordered if row.context_seq in selected]

    return RunTranscript(
        tuple(kept),
        clocks,
        tuple(deepcopy(dict(tracker.latest[key])) for key in sorted(tracker.latest)),
        changes,
    )
