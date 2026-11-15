"""Bounded CLI file transport for the existing signing-context JSON protocol.

This module never opens storage, expands paths or logs caller/file contents.
"""
from __future__ import annotations

import json
import os
import stat
from typing import Any

from storage import vault_bitcoin


class SigningInputError(ValueError):
    pass


def _fail(message: str) -> None:
    raise SigningInputError(message)


def _preparse_context_json(raw: str) -> None:
    """Bound lexical work before json.loads builds a recursive object tree.

    This is only a bounded token pass. The standard decoder remains the grammar
    authority. Frames count values/own fields, not punctuation inside strings.
    """
    frames: list[list[Any]] = []  # opening character, member count, expecting key
    nodes = 0
    index = 0

    def value_node() -> None:
        nonlocal nodes
        if len(frames) > 16:
            _fail("signing context exceeds JSON depth 16")
        nodes += 1
        if nodes > 4096:
            _fail("signing context exceeds 4096 JSON nodes")
        if frames and frames[-1][0] == "[":
            frames[-1][1] += 1
            if frames[-1][1] > 2048:
                _fail("signing context array exceeds 2048 entries")

    while index < len(raw):
        char = raw[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == '"':
            is_key = bool(frames and frames[-1][0] == "{" and frames[-1][2])
            if is_key:
                if len(frames) > 16:
                    _fail("signing context exceeds JSON depth 16")
                frames[-1][1] += 1
                if frames[-1][1] > 128:
                    _fail("signing context object exceeds 128 fields")
                frames[-1][2] = False
            else:
                value_node()
            index += 1
            characters = 0
            while index < len(raw) and raw[index] != '"':
                if raw[index] == "\\":
                    index += 1
                    if index >= len(raw):
                        _fail("signing context must be valid JSON")
                    if raw[index] == "u":
                        index += 4  # grammar/hex/surrogate validity belongs to decoding
                characters += 1
                if characters > vault_bitcoin.MAX_FUNDING_HEX:
                    _fail("signing context string token exceeds its bound")
                index += 1
            if index >= len(raw):
                _fail("signing context must be valid JSON")
            index += 1
        elif char in "[{":
            value_node()
            frames.append([char, 0, char == "{"])
            index += 1
        elif char in "]}":
            if not frames or frames[-1][0] != (
                "[" if char == "]" else "{"
            ):
                _fail("signing context must be valid JSON")
            frames.pop()
            index += 1
        elif char in ",:":
            if char == "," and frames and frames[-1][0] == "{":
                frames[-1][2] = True
            index += 1
        else:
            value_node()
            start = index
            while index < len(raw) and raw[index] not in " \t\r\n,]}:":
                index += 1
                if index - start > 17:
                    _fail("signing context scalar token exceeds its bound")
            if index == start:
                _fail("signing context must be valid JSON")
    if frames:
        _fail("signing context must be valid JSON")


def decode_context_json(raw: str, *, bounded_file: bool = False) -> dict[str, Any]:
    if bounded_file:
        _preparse_context_json(raw)
    duplicate = False

    def object_pairs(pairs):
        nonlocal duplicate
        result = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        context = json.loads(raw, object_pairs_hook=object_pairs)
    except (ValueError, RecursionError) as exc:
        raise SigningInputError("signing context must be valid bounded JSON") from exc
    new_context = vault_bitcoin.needs_ecdsa_preflight(context)
    if bounded_file or new_context:
        if duplicate:
            _fail("signing context must not contain duplicate keys")
        if type(context) is not dict or not context:
            _fail("signing context must be a nonempty JSON object")
    if new_context:
        try:
            vault_bitcoin.preflight_ecdsa_context(context)
        except vault_bitcoin.BitcoinContextError as exc:
            raise SigningInputError(str(exc)) from exc
    # Existing literal contexts keep their existing API validation/precedence.
    # File contexts also receive the normal lossless/type-specific API checks.
    return context


def read_context_file(path: str) -> dict[str, Any]:
    if path == "-":
        _fail("signing context file must be a regular file, not stdin")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                _fail("signing context file must be a regular file")
            if metadata.st_size > vault_bitcoin.MAX_CONTEXT_BYTES:
                _fail("signing context file exceeds 2,162,688 bytes")
            data = bytearray()
            remaining = vault_bitcoin.MAX_CONTEXT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                data.extend(chunk)
                remaining -= len(chunk)
            if len(data) > vault_bitcoin.MAX_CONTEXT_BYTES:
                _fail("signing context file exceeds 2,162,688 bytes")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SigningInputError("signing context file could not be read as a regular file") from exc
    try:
        raw = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SigningInputError("signing context file must be strict UTF-8 without BOM") from exc
    if raw.startswith("\ufeff"):
        _fail("signing context file must be strict UTF-8 without BOM")
    return decode_context_json(raw, bounded_file=True)
