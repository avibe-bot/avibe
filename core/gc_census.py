"""One-shot local watchdog diagnostic. Only static labels and aggregate counts leave this module."""

from __future__ import annotations

import gc
import json
import logging
import os
import sys
import time
import types
from collections import Counter, deque

# Never print type.__name__: dynamic types can have user-supplied names.
_TYPES = (
    list,
    tuple,
    dict,
    set,
    frozenset,
    type,
    deque,
    property,
    slice,
    staticmethod,
    classmethod,
    bytearray,
    memoryview,
    types.ModuleType,
    types.FunctionType,
    types.MethodType,
    types.BuiltinFunctionType,
    types.CellType,
    types.CodeType,
    types.FrameType,
    types.GeneratorType,
    types.CoroutineType,
    types.MappingProxyType,
    types.GetSetDescriptorType,
    types.MemberDescriptorType,
    types.MethodDescriptorType,
    types.WrapperDescriptorType,
    types.SimpleNamespace,
    BaseException,
)
_LABELS = {id(t): t.__name__ for t in _TYPES}
_PATHS = (
    ("dispatcher", "message_dispatcher"),
    ("gateway", "model_hub_turn_gateway"),
    ("model_hub", "model_hub_service"),
    ("turns", "session_turns"),
    ("config", "config"),
    ("agent_registry", "agent_service.agents"),
    ("vibey_runtimes", "agent_service.agents.@vibey._runtimes"),
    ("transcript_indexes", "agent_service.agents.@vibey.store"),
    ("transcript_locks", "agent_service.agents.@vibey.store._store._locks"),
    ("builtin_catalog", "model_hub_service._builtin_snapshot_cache"),
)


def _counts(controller):
    snapshot = gc.get_objects()
    counts = Counter(_LABELS.get(id(type(obj)), "other") for obj in snapshot)
    tracked = {id(obj) for obj in snapshot}
    # Reachability under each named root, NOT exclusive retainers. Follow only
    # ordinary containers and already materialized, audited application fields.
    # Never follow a controller backlink, a class, a module, or a function closure.
    owners = {}
    truncated = False
    # Expose the active Session -> run -> agent -> rows chain only. Do not walk
    # arbitrary instance dictionaries (notably host/controller backlinks).
    follow = {}
    for mod, name, member in (
        ("modules.agents.vibey.agent", "_SessionRuntime", "run"),
        ("modules.agents.vibey.agent", "_Run", "agent"),
        ("core.agent_core.agent.loop", "Agent", "_rows"),
    ):
        module = sys.modules.get(mod)
        if type(module) is types.ModuleType:
            cls = vars(module).get(name)
            if cls is not None:
                follow[id(cls)] = member
    for index, (_, field) in enumerate(_PATHS):
        root = controller
        for part in field.split("."):
            if part == "@vibey":
                root = root.get("vibey") if type(root) is dict else None
            else:
                try:
                    attrs = object.__getattribute__(root, "__dict__")
                except AttributeError:
                    root = None
                    break
                root = attrs.get(part) if type(attrs) is dict else None
            if root is None:
                break
        if root is controller:
            continue
        if root is None:
            continue
        mask = 1 << index
        stack = [root]
        edges = 0
        while stack:
            obj = stack.pop()
            key = id(obj)
            if key not in tracked or obj is controller or owners.get(key, 0) & mask:
                continue
            owners[key] = owners.get(key, 0) | mask
            if (
                type(obj) is dict
                or type(obj) is list
                or type(obj) is tuple
                or type(obj) is set
                or type(obj) is frozenset
            ):
                size = len(obj) * (2 if type(obj) is dict else 1)
                if size > 100000 - edges:
                    truncated = True
                    continue
                children = gc.get_referents(obj)
            elif id(type(obj)) in follow:
                children = [object.__getattribute__(obj, "__dict__").get(follow[id(type(obj))])]
            elif obj is root:
                # Fixed controller-owned root only; only traverse its already held
                # container referents, never arbitrary user objects/descriptors.
                children = [x for x in gc.get_referents(obj) if type(x) is dict or type(x) is list or type(x) is tuple]
            else:
                continue
            edges += len(children)
            stack.extend(children)
    bins = Counter(owners.values())
    return dict(
        total=len(snapshot),
        types=counts.most_common(30),
        owners={label: bins[1 << i] for i, (label, _) in enumerate(_PATHS)},
        shared=sum(n for mask, n in bins.items() if mask.bit_count() > 1),
        unattributed=len(snapshot) - len(owners),
        covered=len(owners),
        truncated=truncated,
    )


def configured(controller):
    if os.environ.get("AVIBE_DIAGNOSTIC_GC_CENSUS") != "1":
        return None

    def sample():
        start = time.perf_counter()
        report = _counts(controller)
        report["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
        logging.getLogger("core.loop_watchdog").warning("GC census: %s", json.dumps(report, separators=(",", ":")))

    return sample
