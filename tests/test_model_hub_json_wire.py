from __future__ import annotations

import io
import json

import pytest

from core.handlers.model_hub.json_wire import (
    SelectiveJSONParser,
    project_json_reader,
    rewrite_json_strings,
)


@pytest.mark.parametrize(
    "payload",
    (
        b'{"wanted":1,}',
        b'{"ignored":[{"nested":1}],"wanted":2,}',
        b'{"ignored":[}',
        b'{"ignored":"bad\\q","wanted":3}',
        b'{"ignored":01,"wanted":4}',
        b'{"ignored":1e,"wanted":5}',
        b'{"ignored":"\xff","wanted":6}',
        b'{"ignored":[,,true],"wanted":7}',
        b'{"ignored":{"key" 1},"wanted":8}',
        b'{"ignored":[true false],"wanted":9}',
    ),
)
def test_selective_projection_does_not_accept_invalid_ignored_json(payload: bytes) -> None:
    assert not project_json_reader(io.BytesIO(payload), {(), ("wanted",)}, lambda *_args: None)


def test_selective_projection_skips_large_values_but_keeps_later_selected_facts() -> None:
    payload = json.dumps(
        {
            "ignored_string": "x" * (2 * 1024 * 1024),
            "ignored_tree": [[{"value": index}] for index in range(20_000)],
            "wanted": "kept",
        },
        separators=(",", ":"),
    ).encode()
    observed: list[object] = []

    valid = project_json_reader(
        io.BytesIO(payload),
        {(), ("wanted",)},
        lambda path, event, value: (
            observed.append(value)
            if path == ("wanted",) and event == "scalar"
            else None
        ),
    )

    assert valid is True
    assert observed == ["kept"]


def test_selective_projection_scans_large_ascii_strings_in_chunks() -> None:
    parser = SelectiveJSONParser({(), ("wanted",)}, lambda *_args: None)
    parser.feed(b'{"ignored":"')

    delegate = parser._string_decoder

    class CountingDecoder:
        calls = 0

        def decode(self, payload: bytes, final: bool = False) -> str:
            self.calls += 1
            return delegate.decode(payload, final=final)

        def getstate(self):
            return delegate.getstate()

        def reset(self) -> None:
            delegate.reset()

    decoder = CountingDecoder()
    parser._string_decoder = decoder
    parser.feed(b"x" * (2 * 1024 * 1024))
    parser.feed(b'","wanted":true}')

    assert parser.finish() is True
    assert decoder.calls < 10


def test_path_rewriter_changes_only_selected_string_values() -> None:
    payload = b'{"content":"alias","tool":{"name":"alias"},"other":{"name":"alias"}}'
    rewritten = rewrite_json_strings(
        io.BytesIO(payload),
        target_paths={("tool", "name")},
        replacements={"alias": "original"},
    )

    assert rewritten is not None
    try:
        assert json.load(rewritten) == {
            "content": "alias",
            "tool": {"name": "original"},
            "other": {"name": "alias"},
        }
    finally:
        rewritten.close()
