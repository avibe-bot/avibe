"""Read-only, Delivery-addressed slices of one consumed Harness input."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from core.agent_core.messages import ImageBlock, TextBlock, UserMessage
from core.agent_input import harness_hint, harness_source
from storage.models import message_deliveries, messages


@dataclass(frozen=True)
class RenderedInput(UserMessage):
    """Render bookkeeping only: ordinary model/provider serialization sees just text.

    Env and fork notices prepend blocks, preserving its bookkeeping until
    consumption commits it alongside the canonical model input.
    """

    header: str = ""
    handoff_chars: int = 0
    body_text_block: bool = True
    # (uploaded token, index in the canonical images OR exact rendered file line)
    files: tuple[tuple[str, int | str], ...] = ()


def member_record(payload: dict[str, Any]) -> dict[str, Any]:
    from storage.message_deliveries import scheduled_delivery_provenance

    provenance = scheduled_delivery_provenance(payload) or {}
    spec = provenance.get("platform_specific") or {}
    return {
        "text": payload["dispatch_text"],
        "source": harness_source(spec),
        "hint": harness_hint(spec, receiver=payload["session_id"]),
        "tokens": [
            a["token"]
            for a in payload["content"].get("attachments") or ()
            if isinstance(a, dict) and isinstance(a.get("token"), str)
        ],
    }


def _legacy_parts(rendered: str, display: str, count: int, sep: str):
    """Locate inserted section breaks using the *independent* display join.

    A literal break in a body remains a break in the display, while an inserted
    one becomes a single newline. Keep only uniquely proved boundaries.
    """
    length = len(display) + (count - 1) * (len(sep) - 1)
    if not length or len(rendered) < length:
        return None
    body = rendered[-length:]
    chunks = body.split(sep)
    if len(chunks) < count or not display.startswith(chunks[0]):
        return None
    # At a given break the number of inserted breaks already seen determines
    # the display offset. Two routes to one offset are permanently ambiguous.
    states = {0: ()}
    offset = len(chunks[0])
    for index, chunk in enumerate(chunks[1:], start=1):
        following = {}
        for used, cuts in states.items():
            pos = offset - used * (len(sep) - 1)
            for step, delimiter in ((0, sep), (1, "\n")):
                total = used + step
                if total > count - 1 or total + len(chunks) - index - 1 < count - 1:
                    continue
                if display.startswith(delimiter, pos) and display.startswith(chunk, pos + len(delimiter)):
                    following[total] = None if total in following or cuts is None else (*cuts, offset) if step else cuts
        states = following
        if not states:
            return None
        offset += len(sep) + len(chunk)
    cuts = states.get(count - 1)
    if cuts is None:
        return None
    starts = (0, *(cut + len(sep) for cut in cuts))
    parts = [body[start:end] for start, end in zip(starts, (*cuts, len(body)))]
    return parts if all(part.strip() for part in parts) else None


def _handoff_from_header(header: str) -> str:
    # Only pass a header already isolated from the member body. A handoff quotes
    # conversation literally; a quoted end-marker need not be its boundary.
    end = "\n[End of Avibe handoff]"
    if header.startswith("[Avibe handoff]\n") and end + "\n\n" in header:
        return header[: header.rindex(end + "\n\n") + len(end)]
    return ""


def host_only_input(
    original: UserMessage, model_payload: dict, *, legacy_dispatch: str | None = None
) -> UserMessage | None:
    """A fully removed occurrence's host-authored backend handoff, if present.

    The receipt names a range of canonical text; the preceding blocks are Env/fork
    context. Never parse a delimiter in a member's words to identify a handoff.
    """
    render = model_payload.get("carry_render") or {}
    blocks = [b for b in original.content if isinstance(b, TextBlock)]
    if not blocks or not blocks[-1].text:
        return None
    text = blocks[-1].text
    header = render.get("header", "")
    if not header and legacy_dispatch and text.endswith(legacy_dispatch):
        # Released singleton initial inputs have no render receipt. Only look
        # before the exact Turn dispatch; the body itself can forge every marker.
        header = text.removesuffix(legacy_dispatch)
    size = render.get("handoff_chars", 0)
    handoff = text[:size] if size else _handoff_from_header(header)
    if not handoff or not blocks[-1].text.startswith(handoff + "\n\n"):
        return None
    return UserMessage((*blocks[:-1], TextBlock(handoff)))


def _legacy(original: UserMessage, content: dict, meta: dict, ids: list[str], session: str, digests: dict):
    """Only split the released renderer's ordered batch, never search for text to remove.

    Check both independent joins (display uses newline, scheduled dispatch uses a
    section break). A delimiter in a body, lost boundaries or unowned media makes
    this row opaque. Never assign the first sender's metadata to another member.
    """
    if meta.get("merged_delivery_ids") != ids or content.get("attachments"):
        return None
    if any(isinstance(block, ImageBlock) for block in original.content):
        return None
    for block in original.content[:-1]:
        value = block.text or ""
        if not (
            (value.startswith("<environment>\n") and value.endswith("\n</environment>"))
            or (value.startswith("<fork>\n") and value.endswith("\n</fork>"))
        ):
            return None
    sources = meta.get("merged_harness_sources")
    if sources is None:
        sources = [{} for _ in ids]
    if not isinstance(sources, list) or len(sources) != len(ids) or not all(isinstance(s, dict) for s in sources):
        return None
    rendered = original.content[-1].text or ""
    display = content.get("text") or ""
    if not isinstance(display, str):
        return None
    # Try the scheduled section renderer first; plain and recovered batches
    # use a newline, which is separable only for exactly one line per member.
    sep = "\n\n---\n\n"
    parts = _legacy_parts(rendered, display, len(ids), sep)
    if parts is None:
        sep = "\n"
        parts = display.split(sep)
    if len(parts) != len(ids) or not all(p.strip() for p in parts) or not rendered.endswith(sep.join(parts)):
        return None
    if any(hashlib.sha256(part.encode()).hexdigest() != digests.get(i) for i, part in zip(ids, parts)):
        return None  # Newline counts cannot distinguish a multiline sibling plus an empty member.
    header = rendered.removesuffix(sep.join(parts))
    handoff = _handoff_from_header(header)
    # Merely ending with the display text isn't sufficient evidence: a custom
    # dispatch prompt could add an instruction just before that text.
    from storage.message_deliveries import TURN_CARRY_LINES

    labels = [harness_source(s) for s in sources]
    merged_label = labels[0] if len(set(labels)) == 1 else "; ".join(labels)
    allowed = {f"[{harness_hint(s, receiver=session)}]" for s in sources}
    allowed.update(f"[{line}]" for line in TURN_CARRY_LINES.values())
    allowed.add(f"[Harness: {merged_label}]")
    if any(
        line not in allowed and not (line.startswith("[Now: ") and line.endswith("]"))
        for line in header.removeprefix(handoff + "\n\n").splitlines()
    ):
        return None
    return (
        [
            dict(text=t, source=harness_source(s), hint=harness_hint(s, receiver=session), tokens=[])
            for t, s in zip(parts, sources)
        ],
        header,
        sep,
    )


def surviving_input(
    conn: Connection,
    message_id: str,
    ordered_member_ids: Sequence[str],
    removed_ids: Collection[str],
    original: UserMessage,
) -> UserMessage | None:
    """Project a partially surviving batch without mutating its Message or Delivery history."""
    ids = list(ordered_member_ids)
    if not set(ids).intersection(removed_ids):
        return original
    if all(i in removed_ids for i in ids):
        raise ValueError("surviving_input requires a surviving Delivery")
    row = (
        conn.execute(
            select(messages.c.content_json, messages.c.metadata_json, messages.c.session_id).where(
                messages.c.id == message_id
            )
        )
        .mappings()
        .one()
    )
    content = json.loads(row["content_json"])
    meta = json.loads(row["metadata_json"])
    render = meta.get("carry_render") or {}
    histories = conn.execute(
        select(
            message_deliveries.c.id, message_deliveries.c.delivery_history_json, message_deliveries.c.dispatch_sha256
        ).where(message_deliveries.c.message_id == message_id)
    )
    members, envelopes, digests = {}, {}, {}
    for i, history, digest in histories:
        digests[i] = digest
        for event in json.loads(history)["events"]:
            if "carry_member" in event:
                members[i] = event["carry_member"]
            elif "carry_envelope" in event:
                envelopes[i] = event["carry_envelope"]
    if members:
        records = [members[i] for i in ids]
        header = render.get("header", "")
        sep = "\n\n---\n\n" if meta.get("merged_harness_sources") else "\n"
    else:
        legacy = _legacy(original, content, meta, ids, row["session_id"], digests)
        if legacy is None:
            # A released acceptance can still hold each original envelope. This is
            # positive per-Delivery evidence even when the text contains separators.
            if not all(i in envelopes for i in ids) or content.get("attachments"):
                return None
            from storage.message_deliveries import delivery_payload

            records = [
                member_record(
                    delivery_payload(
                        {
                            "id": i,
                            "session_id": row["session_id"],
                            "snapshot_json": envelopes[i][0],
                            "dispatch_text": envelopes[i][1],
                        }
                    )
                )
                for i in ids
            ]
            sep = "\n\n---\n\n" if meta.get("merged_harness_sources") else "\n"
            body = sep.join(record["text"] for record in records if record["text"].strip())
            last = original.content[-1]
            if not isinstance(last, TextBlock) or last.text is None or not body or not last.text.endswith(body):
                return None
            header = last.text.removesuffix(body)
        else:
            records, header, sep = legacy

    # Order and multiplicity matter: even the same upload may appear twice and
    # yield two distinct snapshot results. Never resolve a token or open a file on read.
    files = defaultdict(deque)
    for token, outcome in render.get("files", []):
        files[token].append(outcome)
    images = [b for b in original.content if isinstance(b, ImageBlock)]
    kept_images, listed, survivors = [], [], []
    for i, record in zip(ids, records):
        if i not in removed_ids:
            survivors.append(record)
        for token in record["tokens"]:
            if files[token]:
                outcome = files[token].popleft()
                if i not in removed_ids:
                    if isinstance(outcome, int):
                        kept_images.append(images[outcome])
                    else:
                        listed.append(outcome)
    labels = [s["source"] for s in survivors]
    label = labels[0] if len(set(labels)) == 1 else "; ".join(labels)
    from storage.message_deliveries import TURN_CARRY_LINES

    hints = {f"[{s['hint']}]" for s in survivors if s["hint"]}
    carry = TURN_CARRY_LINES.get(str(meta.get("turn_carry") or ""))
    size = render.get("handoff_chars", 0)
    handoff = header[:size] if size else _handoff_from_header(header)
    if handoff:
        header = header.removeprefix(handoff + "\n\n")
    lines = [
        *([handoff + "\n"] if handoff else []),
        f"[Harness: {label}]",
        *(
            line
            for line in header.splitlines()
            if line in hints or (carry and line == f"[{carry}]") or line.startswith("[Now: ")
        ),
    ]
    body = sep.join(s["text"] for s in survivors if s["text"].strip())
    if listed:
        body = "\n".join(([body, ""] if body else []) + ["[User Attachments]", *listed])
    # The renderer has one text block followed by images; any earlier text
    # blocks are context (Env/fork), not the leader's content.
    text_blocks = [b for b in original.content if isinstance(b, TextBlock)]
    prefix = text_blocks[:-1] if render.get("body_text_block", True) else text_blocks
    return UserMessage((*prefix, TextBlock("\n".join([*lines, body])), *kept_images))
