"""Bounded-memory JSON projection and path-aware string rewriting."""

from __future__ import annotations

import codecs
import json
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import BinaryIO, Final, Literal


JSONPath = tuple[str, ...]
JSONEvent = Literal["start_map", "start_array", "scalar", "nonempty"]
JSON_STRING_TOKEN_BYTES: Final = 16 * 1024
JSON_NUMBER_TOKEN_BYTES: Final = 128
JSON_IO_CHUNK_BYTES: Final = 64 * 1024
_JSON_STRING_SPECIAL: Final = re.compile(br'["\\\x00-\x1f\x80-\xff]')


@dataclass
class _PathNode:
    selected: bool = False
    children: dict[str, "_PathNode"] = field(default_factory=dict)


def _path_tree(paths: Iterable[JSONPath]) -> _PathNode:
    root = _PathNode()
    for path in paths:
        node = root
        for component in path:
            node = node.children.setdefault(component, _PathNode())
        node.selected = True
    return root


@dataclass
class _Container:
    kind: Literal["map", "array"]
    path: JSONPath
    node: _PathNode
    state: str
    key: str | None = None
    nonempty: bool = False


class SelectiveJSONParser:
    """Parse only selected paths while skipping every unrelated value in place.

    Strings and numbers are bounded lexical tokens. Every container retains only
    its grammar state and selected-path node, so unrelated shape costs O(depth)
    memory without weakening JSON validation.
    """

    def __init__(
        self,
        paths: Iterable[JSONPath],
        visitor: Callable[[JSONPath, JSONEvent, object | None], None],
    ) -> None:
        self._root = _path_tree(paths)
        self._visitor = visitor
        self._stack: list[_Container] = []
        self._discard_node = _PathNode()
        self._root_state = "value"
        self._invalid = False
        self._mode: Literal["normal", "string", "number", "literal"] = "normal"
        self._string = bytearray()
        self._string_too_long = False
        self._escaped = False
        self._unicode_escape_digits = 0
        self._string_decoder = codecs.getincrementaldecoder("utf-8")()
        self._number = bytearray()
        self._number_too_long = False
        self._number_state = ""
        self._literal = bytearray()

    @property
    def retained_bytes(self) -> int:
        return (
            len(self._string)
            + len(self._number)
            + len(self._literal)
            + sum(len(frame.key or "") for frame in self._stack)
        )

    @property
    def valid(self) -> bool:
        return (
            not self._invalid
            and self._root_state == "done"
            and not self._stack
            and self._mode == "normal"
        )

    @property
    def next_value_path(self) -> JSONPath | None:
        """Return the selected path of the next value, if one is expected."""

        if self._invalid or self._mode != "normal":
            return None
        if not self._stack:
            return () if self._root_state == "value" else None
        frame = self._stack[-1]
        if frame.kind == "map" and frame.state == "value" and frame.key is not None:
            child = frame.node.children.get(frame.key)
            return (*frame.path, frame.key) if child is not None else None
        if frame.kind == "array" and frame.state in {"value_or_end", "value"}:
            child = frame.node.children.get("*")
            return (*frame.path, "*") if child is not None else None
        return None

    def feed(self, chunk: bytes) -> None:
        offset = 0
        while offset < len(chunk) and not self._invalid:
            if self._mode == "string":
                offset = self._feed_string_chunk(chunk, offset)
                continue
            self.feed_byte(chunk[offset])
            offset += 1

    def feed_byte(self, byte: int) -> None:
        if self._invalid:
            return
        if self._mode == "string":
            self._feed_string(byte)
            return
        if self._mode == "number":
            if byte in b"0123456789+-.eE":
                if not self._advance_number(byte):
                    self._invalid = True
                    return
                if len(self._number) < JSON_NUMBER_TOKEN_BYTES:
                    self._number.append(byte)
                else:
                    self._number_too_long = True
                return
            self._finish_number()
            self.feed_byte(byte)
            return
        if self._mode == "literal":
            if 0x61 <= byte <= 0x7A:
                if len(self._literal) < len(b"false"):
                    self._literal.append(byte)
                else:
                    self._invalid = True
                return
            self._finish_literal()
            self.feed_byte(byte)
            return
        if byte in b" \t\r\n":
            return
        if byte == 0x22:
            self._mode = "string"
            self._string.clear()
            self._string_too_long = False
            self._escaped = False
            self._unicode_escape_digits = 0
            self._string_decoder.reset()
            return
        if byte in b"-0123456789":
            self._mode = "number"
            self._number.clear()
            self._number.append(byte)
            self._number_too_long = False
            self._number_state = "sign" if byte == 0x2D else "zero" if byte == 0x30 else "int"
            return
        if byte in b"tfn":
            self._mode = "literal"
            self._literal.clear()
            self._literal.append(byte)
            return
        punctuation = {
            0x7B: "start_map",
            0x7D: "end_map",
            0x5B: "start_array",
            0x5D: "end_array",
            0x3A: "colon",
            0x2C: "comma",
        }.get(byte)
        if punctuation is None:
            self._invalid = True
            return
        self._accept(punctuation, None)

    def finish(self) -> bool:
        if self._mode == "number":
            self._finish_number()
        elif self._mode == "literal":
            self._finish_literal()
        elif self._mode == "string":
            self._invalid = True
        return self.valid

    def _feed_string(self, byte: int) -> None:
        if self._unicode_escape_digits:
            if byte not in b"0123456789abcdefABCDEF":
                self._invalid = True
                return
            self._unicode_escape_digits -= 1
        elif self._escaped:
            if byte not in b'"\\/bfnrtu':
                self._invalid = True
                return
            self._escaped = False
            if byte == 0x75:
                self._unicode_escape_digits = 4
        elif byte == 0x5C:
            self._escaped = True
        elif byte == 0x22:
            try:
                self._string_decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._invalid = True
                return
            raw = bytes(self._string)
            too_long = self._string_too_long
            self._mode = "normal"
            self._string.clear()
            if too_long:
                self._accept("string", None)
                return
            try:
                value = json.loads(b'"' + raw + b'"')
            except (UnicodeDecodeError, ValueError):
                self._invalid = True
                return
            self._accept("string", value)
            return
        elif byte < 0x20:
            self._invalid = True
            return
        try:
            self._string_decoder.decode(bytes((byte,)), final=False)
        except UnicodeDecodeError:
            self._invalid = True
            return
        self._retain_string_bytes(bytes((byte,)))

    def _feed_string_chunk(self, chunk: bytes, offset: int) -> int:
        """Consume ordinary ASCII string runs without per-byte codec calls."""

        while offset < len(chunk) and self._mode == "string" and not self._invalid:
            decoder_pending = bool(self._string_decoder.getstate()[0])
            if self._escaped or self._unicode_escape_digits or decoder_pending:
                self._feed_string(chunk[offset])
                offset += 1
                continue

            special = _JSON_STRING_SPECIAL.search(chunk, offset)
            end = special.start() if special is not None else len(chunk)
            if end > offset:
                self._retain_string_bytes(chunk[offset:end])
                offset = end
            if special is None:
                break
            self._feed_string(chunk[offset])
            offset += 1
        return offset

    def _retain_string_bytes(self, payload: bytes) -> None:
        remaining = JSON_STRING_TOKEN_BYTES - len(self._string)
        if remaining > 0:
            self._string.extend(payload[:remaining])
        if len(payload) > remaining:
            self._string_too_long = True

    def _finish_number(self) -> None:
        raw = bytes(self._number)
        too_long = self._number_too_long
        self._mode = "normal"
        self._number.clear()
        if self._number_state not in {"zero", "int", "frac", "exp_int"}:
            self._invalid = True
            return
        if too_long:
            self._accept("scalar", None)
            return
        try:
            value = json.loads(raw)
        except ValueError:
            self._invalid = True
            return
        self._accept("scalar", value)

    def _advance_number(self, byte: int) -> bool:
        if self._number_state == "sign":
            if byte == 0x30:
                self._number_state = "zero"
                return True
            if byte in b"123456789":
                self._number_state = "int"
                return True
            return False
        if self._number_state == "zero":
            if byte == 0x2E:
                self._number_state = "dot"
                return True
            if byte in b"eE":
                self._number_state = "exp"
                return True
            return False
        if self._number_state == "int":
            if byte in b"0123456789":
                return True
            if byte == 0x2E:
                self._number_state = "dot"
                return True
            if byte in b"eE":
                self._number_state = "exp"
                return True
            return False
        if self._number_state == "dot":
            if byte in b"0123456789":
                self._number_state = "frac"
                return True
            return False
        if self._number_state == "frac":
            if byte in b"0123456789":
                return True
            if byte in b"eE":
                self._number_state = "exp"
                return True
            return False
        if self._number_state == "exp":
            if byte in b"+-":
                self._number_state = "exp_sign"
                return True
            if byte in b"0123456789":
                self._number_state = "exp_int"
                return True
            return False
        if self._number_state == "exp_sign":
            if byte in b"0123456789":
                self._number_state = "exp_int"
                return True
            return False
        if self._number_state == "exp_int":
            return byte in b"0123456789"
        return False

    def _finish_literal(self) -> None:
        raw = bytes(self._literal)
        self._mode = "normal"
        self._literal.clear()
        values = {b"true": True, b"false": False, b"null": None}
        if raw not in values:
            self._invalid = True
            return
        self._accept("scalar", values[raw])

    def _accept(self, token: str, value: object | None) -> None:
        if not self._stack:
            if self._root_state != "value":
                self._invalid = True
                return
            self._start_value((), self._root, token, value)
            return

        frame = self._stack[-1]
        if frame.kind == "map":
            self._accept_map(frame, token, value)
        else:
            self._accept_array(frame, token, value)

    def _accept_map(self, frame: _Container, token: str, value: object | None) -> None:
        if frame.state in {"key_or_end", "key"}:
            if token == "end_map" and frame.state == "key_or_end":
                self._end_container("map")
            elif token == "string":
                frame.key = value if isinstance(value, str) else None
                frame.state = "colon"
            else:
                self._invalid = True
            return
        if frame.state == "colon":
            if token != "colon":
                self._invalid = True
            else:
                frame.state = "value"
            return
        if frame.state == "value":
            key = frame.key
            child = frame.node.children.get(key) if key is not None else None
            self._mark_nonempty(frame)
            if child is None:
                self._skip_value(token)
            else:
                self._start_value((*frame.path, key), child, token, value)
            return
        if frame.state == "comma_or_end":
            if token == "comma":
                frame.state = "key"
                frame.key = None
            elif token == "end_map":
                self._end_container("map")
            else:
                self._invalid = True

    def _accept_array(self, frame: _Container, token: str, value: object | None) -> None:
        if frame.state in {"value_or_end", "value"}:
            if token == "end_array" and frame.state == "value_or_end":
                self._end_container("array")
                return
            self._mark_nonempty(frame)
            child = frame.node.children.get("*")
            if child is None:
                self._skip_value(token)
            else:
                self._start_value((*frame.path, "*"), child, token, value)
            return
        if frame.state == "comma_or_end":
            if token == "comma":
                frame.state = "value"
            elif token == "end_array":
                self._end_container("array")
            else:
                self._invalid = True

    def _start_value(
        self,
        path: JSONPath,
        node: _PathNode,
        token: str,
        value: object | None,
    ) -> None:
        if token == "start_map":
            if node.selected:
                self._visitor(path, "start_map", None)
            self._stack.append(_Container("map", path, node, "key_or_end"))
        elif token == "start_array":
            if node.selected:
                self._visitor(path, "start_array", None)
            self._stack.append(_Container("array", path, node, "value_or_end"))
        elif token in {"string", "scalar"}:
            if node.selected:
                self._visitor(path, "scalar", value)
            self._complete_value()
        else:
            self._invalid = True

    def _skip_value(self, token: str) -> None:
        self._start_value((), self._discard_node, token, None)

    def _mark_nonempty(self, frame: _Container) -> None:
        if not frame.nonempty:
            frame.nonempty = True
            if frame.node.selected:
                self._visitor(frame.path, "nonempty", True)

    def _end_container(self, expected: Literal["map", "array"]) -> None:
        if not self._stack or self._stack[-1].kind != expected:
            self._invalid = True
            return
        self._stack.pop()
        self._complete_value()

    def _complete_value(self) -> None:
        if not self._stack:
            self._root_state = "done"
            return
        parent = self._stack[-1]
        parent.state = "comma_or_end"


def project_json_reader(
    reader: BinaryIO,
    paths: Iterable[JSONPath],
    visitor: Callable[[JSONPath, JSONEvent, object | None], None],
) -> bool:
    parser = SelectiveJSONParser(paths, visitor)
    while chunk := reader.read(JSON_IO_CHUNK_BYTES):
        parser.feed(chunk)
    return parser.finish()


class _JSONPathStringRewriter:
    def __init__(
        self,
        writer: BinaryIO,
        target_paths: Iterable[JSONPath],
        replacements: Mapping[str, str],
    ) -> None:
        self._writer = writer
        self._targets = frozenset(target_paths)
        self._replacements = replacements
        self._parser = SelectiveJSONParser(self._targets, lambda *_args: None)
        self._output = bytearray()
        self._candidate = bytearray()
        self._candidate_path: JSONPath | None = None
        self._candidate_escaped = False
        self._candidate_too_long = False

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._candidate_path is not None:
                self._candidate.append(byte)
                if self._candidate_escaped:
                    self._candidate_escaped = False
                elif byte == 0x5C:
                    self._candidate_escaped = True
                elif byte == 0x22:
                    self._finish_candidate()
                    continue
                if len(self._candidate) > JSON_STRING_TOKEN_BYTES + 2:
                    self._candidate_too_long = True
                    self._emit(bytes(self._candidate))
                    self._candidate.clear()
                    self._candidate_path = None
                self._parser.feed_byte(byte)
                continue

            path = self._parser.next_value_path
            if byte == 0x22 and path in self._targets:
                self._candidate_path = path
                self._candidate.clear()
                self._candidate.append(byte)
                self._candidate_escaped = False
                self._candidate_too_long = False
                self._parser.feed_byte(byte)
                continue
            self._emit(bytes((byte,)))
            self._parser.feed_byte(byte)

    def finish(self) -> bool:
        if self._candidate:
            self._emit(bytes(self._candidate))
            self._candidate.clear()
            self._candidate_path = None
        valid = self._parser.finish()
        self._flush()
        return valid

    def _finish_candidate(self) -> None:
        raw = bytes(self._candidate)
        self._parser.feed_byte(raw[-1])
        replacement: str | None = None
        if not self._candidate_too_long:
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                value = None
            if isinstance(value, str):
                replacement = self._replacements.get(value)
        if replacement is None:
            self._emit(raw)
        else:
            self._emit(json.dumps(replacement, ensure_ascii=False).encode("utf-8"))
        self._candidate.clear()
        self._candidate_path = None

    def _emit(self, payload: bytes) -> None:
        self._output.extend(payload)
        if len(self._output) >= JSON_IO_CHUNK_BYTES:
            self._flush()

    def _flush(self) -> None:
        if self._output:
            self._writer.write(self._output)
            self._output.clear()


def rewrite_json_strings(
    reader: BinaryIO,
    *,
    target_paths: Iterable[JSONPath],
    replacements: Mapping[str, str],
) -> BinaryIO | None:
    """Return a rewritten spool, or None when the source is not valid JSON."""

    if not replacements:
        return None
    output = tempfile.SpooledTemporaryFile(max_size=256 * 1024)
    rewriter = _JSONPathStringRewriter(output, target_paths, replacements)
    while chunk := reader.read(JSON_IO_CHUNK_BYTES):
        rewriter.feed(chunk)
    if not rewriter.finish():
        output.close()
        return None
    output.seek(0)
    return output
