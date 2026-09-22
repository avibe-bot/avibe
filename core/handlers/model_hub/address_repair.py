"""Take a credential's address out of ids a release before this one stored.

The engine reaches one credential through the model field of one outbound
request and through no other field, so a call spells its target
``<source prefix>/<model>``. That prefix addresses a credential; it is not part
of what the model is called. Releases before this one persisted what the
engine's management API answered with — already addressed — so a stored id could
carry one, and then composition addressed it a second time and the call failed.

Discovery no longer writes one. This is the repair for files that already have
one, and it runs here rather than at the config load seam for two reasons the
load seam cannot answer:

- **Ownership must be proven, not guessed.** A config file records no prefix. A
  repair reading one can only recognise an address by its minted spelling, and
  that renames an upstream identity which happens to be spelled that way —
  ``x-ai/grok-4.6-latest`` and ``accounts/fireworks/models/…`` are the product's
  own counterexamples. Here the caller resolves each Source's credential first
  and passes the address it actually mints, so membership is the proof.
- **An id is a join key.** The same id names a Source's inventory row, a route
  hop, a route key, a backend menu row, a hidden-model entry, and an Agent's
  checked menu. Renaming one collection alone leaves the rest naming something
  gone, so every collection moves here, in one payload, computed before
  anything is written.

Pure: the caller owns resolving credentials and persisting the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .identifiers import (
    model_id_without_credential_address,
    model_id_without_credential_addresses,
)

# A repair costs one credential read per Source, so it is worth knowing first
# whether there is anything to repair. This recognises the minted shape only to
# decide that — nothing is renamed on its say-so.
_ADDRESSED_ID = re.compile(r"avibe-[0-9a-f]{24}/")


def payload_carries_credential_address(payload: Mapping[str, Any]) -> bool:
    """Report whether any stored id looks like it still carries an address."""

    return any(_ADDRESSED_ID.match(value) for value in _stored_model_ids(payload))


def _stored_model_ids(payload: Mapping[str, Any]) -> Iterator[str]:
    """Every id a repair would rewrite, in one place so neither can drift."""

    for source in payload.get("sources") or []:
        if not isinstance(source, dict):
            continue
        for model in source.get("models") or []:
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                yield model["id"]
    for agent in (payload.get("agents") or {}).values():
        if not isinstance(agent, dict):
            continue
        for model in agent.get("models") or []:
            if isinstance(model, dict) and isinstance(model.get("id"), str):
                yield model["id"]
        for value in agent.get("removed_model_ids") or []:
            if isinstance(value, str):
                yield value
        menu = agent.get("menu")
        if isinstance(menu, dict):
            for value in menu.get("checked") or []:
                if isinstance(value, str):
                    yield value
        for requested, route in (agent.get("routes") or {}).items():
            if isinstance(requested, str):
                yield requested
            if not isinstance(route, dict):
                continue
            for hop in route.get("hops") or []:
                if isinstance(hop, dict) and isinstance(hop.get("model_id"), str):
                    yield hop["model_id"]


@dataclass(frozen=True)
class AddressRepair:
    """What a repair would write, and how much of it there is to report."""

    payload: dict[str, Any]
    models: int = 0
    hops: int = 0
    routes: int = 0
    menu_entries: int = 0
    # The backends whose menu, routes, or hops moved. A running backend holds
    # the catalog it was started with, so the caller has to know which ones now
    # disagree with disk — reconciling the engine says nothing about them.
    backends: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.models or self.hops or self.routes or self.menu_entries)


def repair_credential_addresses(
    payload: Mapping[str, Any],
    addresses: Mapping[str, str],
) -> AddressRepair:
    """Rewrite every id carrying one of these Sources' own proven addresses.

    ``addresses`` maps a Source id to the address its credential is reached by.
    A Source absent from it is left alone entirely — an unresolvable credential
    proves nothing, and a guess is what this design exists to avoid.

    The payload is one a loaded config produced, so every collection in it has
    already passed the strict parse: ids are unique before the rename, and any
    collision seen here is one the rename itself created. Where a repaired id
    lands on a name already held, the holder stays — it is the row, hop, or
    route the product has been using, and the repaired one names no identity the
    holder does not already name.
    """

    known = frozenset(addresses.values())
    if not known:
        return AddressRepair(dict(payload))

    healed: dict[str, Any] = dict(payload)
    models_moved = 0
    if isinstance(payload.get("sources"), list):
        healed_sources: list[Any] = []
        for source in payload["sources"]:
            prefix = addresses.get(source.get("id")) if isinstance(source, dict) else None
            models = source.get("models") if isinstance(source, dict) else None
            if prefix is None or not isinstance(models, list):
                healed_sources.append(source)
                continue
            healed_models, moved = _healed_rows(models, frozenset({prefix}))
            models_moved += moved
            healed_sources.append({**source, "models": healed_models})
        healed["sources"] = healed_sources

    hops_moved = 0
    routes_moved = 0
    menu_entries_moved = 0
    if not isinstance(payload.get("agents"), dict):
        return AddressRepair(
            payload=healed,
            models=models_moved,
        )
    healed_agents: dict[str, Any] = {}
    moved_backends: list[str] = []
    for backend, agent in payload["agents"].items():
        if not isinstance(agent, dict):
            healed_agents[backend] = agent
            continue
        before = hops_moved + routes_moved + menu_entries_moved
        healed_agent = dict(agent)

        # A menu id, a route key, a hidden-model entry and a checked entry each
        # record no Source. They do not have to: an address names exactly one
        # credential, and a credential binds to exactly one Source.
        models = agent.get("models")
        if isinstance(models, list):
            healed_models, moved = _healed_rows(models, known)
            menu_entries_moved += moved
            healed_agent["models"] = healed_models

        removed = agent.get("removed_model_ids")
        if isinstance(removed, list):
            healed_removed, moved = _healed_ids(removed, known)
            menu_entries_moved += moved
            healed_agent["removed_model_ids"] = healed_removed

        menu = agent.get("menu")
        if isinstance(menu, dict) and isinstance(menu.get("checked"), list):
            healed_checked, moved = _healed_ids(menu["checked"], known)
            menu_entries_moved += moved
            healed_agent["menu"] = {**menu, "checked": healed_checked}

        routes = agent.get("routes")
        if isinstance(routes, dict):
            healed_routes: dict[str, Any] = {}
            # Which slots a key that needed no repair claimed, so a later one
            # cannot demote it just by arriving second.
            claimed_bare: set[str] = set()
            for requested, route in routes.items():
                healed_route = route
                if isinstance(route, dict) and isinstance(route.get("hops"), list):
                    healed_hops, moved = _healed_hops(route["hops"], addresses)
                    hops_moved += moved
                    healed_route = {**route, "hops": healed_hops}
                identity = (
                    model_id_without_credential_addresses(requested, known)
                    if isinstance(requested, str)
                    else requested
                )
                bare = identity == requested
                if identity != requested:
                    routes_moved += 1
                held = healed_routes.get(identity)
                if held is None:
                    healed_routes[identity] = healed_route
                    if bare:
                        claimed_bare.add(identity)
                    continue
                # Two keys naming one model. The route the Agent has been
                # resolving against is the one already spelled bare, so it is
                # the base and gains only hops it does not already have.
                if bare and identity not in claimed_bare:
                    healed_routes[identity] = {
                        **healed_route,
                        "hops": _merged_hops(healed_route, held),
                    }
                    claimed_bare.add(identity)
                    continue
                healed_routes[identity] = {
                    **held,
                    "hops": _merged_hops(held, healed_route),
                }
            healed_agent["routes"] = healed_routes

        healed_agents[backend] = healed_agent
        if hops_moved + routes_moved + menu_entries_moved > before and isinstance(
            backend, str
        ):
            moved_backends.append(backend)
    healed["agents"] = healed_agents

    return AddressRepair(
        payload=healed,
        models=models_moved,
        hops=hops_moved,
        routes=routes_moved,
        menu_entries=menu_entries_moved,
        backends=tuple(moved_backends),
    )


def _healed_rows(
    rows: list[Any],
    addresses: frozenset[str],
) -> tuple[list[Any], int]:
    """Rename each row's id, dropping one that lands on a name already held.

    The row already spelling the bare name wins over one repaired onto it: it
    is the row the product has been listing and metering, and the repaired one
    names no model it does not already name. Among two repaired rows meeting on
    one name, the first wins, which is the policy discovery itself applies.
    """

    held = {
        row["id"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and model_id_without_credential_addresses(row["id"], addresses) == row["id"]
    }
    healed: list[Any] = []
    moved: set[str] = set()
    count = 0
    for row in rows:
        row_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(row_id, str):
            healed.append(row)
            continue
        identity = model_id_without_credential_addresses(row_id, addresses)
        if identity == row_id:
            healed.append(row)
            continue
        count += 1
        if identity in held or identity in moved:
            continue
        moved.add(identity)
        healed.append({**row, "id": identity})
    return healed, count


def _healed_ids(
    values: list[Any],
    addresses: frozenset[str],
) -> tuple[list[Any], int]:
    """Rename each id in a flat list, keeping the first of any pair that meets."""

    healed: list[Any] = []
    seen: set[str] = set()
    count = 0
    for value in values:
        if not isinstance(value, str):
            healed.append(value)
            continue
        identity = model_id_without_credential_addresses(value, addresses)
        if identity != value:
            count += 1
        if identity in seen:
            continue
        seen.add(identity)
        healed.append(identity)
    return healed, count


def _healed_hops(
    hops: list[Any],
    addresses: Mapping[str, str],
) -> tuple[list[Any], int]:
    """Rename each hop against the address of the Source that hop names."""

    healed: list[Any] = []
    seen: set[tuple[str, str]] = set()
    count = 0
    for hop in hops:
        if not isinstance(hop, dict):
            healed.append(hop)
            continue
        source_id = hop.get("source_id")
        model_id = hop.get("model_id")
        prefix = addresses.get(source_id) if isinstance(source_id, str) else None
        if prefix is None or not isinstance(model_id, str):
            healed.append(hop)
            continue
        identity = model_id_without_credential_address(model_id, prefix)
        if identity != model_id:
            count += 1
        pair = (source_id, identity)
        if pair in seen:
            continue
        seen.add(pair)
        healed.append(hop if identity == model_id else {**hop, "model_id": identity})
    return healed, count


def _merged_hops(held: Any, incoming: Any) -> list[Any]:
    merged = list(held.get("hops") or []) if isinstance(held, dict) else []
    present = {
        (hop.get("source_id"), hop.get("model_id"))
        for hop in merged
        if isinstance(hop, dict)
    }
    for hop in (incoming.get("hops") or []) if isinstance(incoming, dict) else []:
        if isinstance(hop, dict) and (hop.get("source_id"), hop.get("model_id")) in present:
            continue
        merged.append(hop)
    return merged
