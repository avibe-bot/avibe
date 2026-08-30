from __future__ import annotations

import io
import json

import pytest

from core.handlers.model_hub.json_wire import project_json_reader, rewrite_json_strings


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
