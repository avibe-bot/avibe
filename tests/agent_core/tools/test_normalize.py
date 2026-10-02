"""C-7 section 6: the output normalizer, fed in arbitrary chunks as a job's log is read."""

from __future__ import annotations

import pytest

from core.agent_core.tools.normalize import OutputNormalizer


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ([b"\x1b[31mred\x1b[0m plain\n"], "red plain\n"),
        ([b"\x1b]0;window title\x07text\n"], "text\n"),
        ([b"10%\r55%\r100%\n"], "100%\n"),
        ([b"progress 100%\r\x1b[K\n"], "progress 100%\n"),
        ([b"done\r"], "done"),
        ([b"a\r\n", b"b\r", b"\nc"], "a\nb\nc"),
        ([b"\x1b[3", b"2mgreen\n"], "green\n"),
        ([b"\xe4\xb8", b"\xad\xe6\x96\x87\n"], "中文\n"),
        ([b"bad \xff byte\n"], "bad \ufffd byte\n"),
    ],
)
def test_chunks_normalize_to_the_final_screen_text(chunks, expected):
    normalizer = OutputNormalizer()

    out = "".join(normalizer.feed(chunk) for chunk in chunks) + normalizer.flush()

    assert out == expected
