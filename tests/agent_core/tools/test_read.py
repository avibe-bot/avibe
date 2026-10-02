"""C-7 ``read``: Pi's head truncation and continuation text, streamed; images through the sink."""

from __future__ import annotations

import asyncio
import os
import random
import time
import shlex
import struct
import zlib

import pytest

import core.agent_core.tools.read as read_module
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import ImageBlock, TextBlock
from core.agent_core.tools.read import ReadTool
from core.agent_core.tools.truncate import truncate_head
from tests.agent_core.tools.conftest import result_text


async def _read(make_ctx, **arguments):
    return await ReadTool().execute(arguments, make_ctx())


async def test_head_is_cut_at_the_line_limit_with_the_next_offset(tmp_path, make_ctx):
    (tmp_path / "log.txt").write_text("".join(f"line {i}\n" for i in range(1, 2501)))

    result = await _read(make_ctx, path="log.txt")

    text = result_text(result)
    assert not result.is_error
    assert text.startswith("line 1\nline 2\n")
    assert text.endswith("line 2000\n\n[Showing lines 1-2000 of 2501. Use offset=2001 to continue.]")


async def test_head_is_cut_at_the_byte_limit(tmp_path, make_ctx):
    # 1,000-byte lines: the first takes 1,000 bytes and each further one 1,001, so 51 fit in 51,200.
    (tmp_path / "wide.txt").write_text("".join(f"{i:04d}" + "x" * 996 + "\n" for i in range(100)))

    text = result_text(await _read(make_ctx, path="wide.txt"))

    assert text.endswith("\n\n[Showing lines 1-51 of 101 (50.0KB limit). Use offset=52 to continue.]")
    assert text.split("\n\n[")[0].splitlines()[-1].startswith("0050")


async def test_offset_and_limit_name_the_remaining_lines(tmp_path, make_ctx):
    (tmp_path / "ten.txt").write_text("\n".join(f"l{i}" for i in range(1, 11)))

    text = result_text(await _read(make_ctx, path="ten.txt", offset=3, limit=4))

    assert text == "l3\nl4\nl5\nl6\n\n[4 more lines in file. Use offset=7 to continue.]"


async def test_offset_beyond_the_end_is_an_error(tmp_path, make_ctx):
    (tmp_path / "two.txt").write_text("a\nb")

    result = await _read(make_ctx, path="two.txt", offset=5)

    assert result.is_error
    assert result_text(result) == "Offset 5 is beyond end of file (2 lines total)"


@pytest.mark.parametrize(("arguments", "message"), [({"offset": 0}, "offset"), ({"limit": 2.5}, "limit")])
async def test_offset_and_limit_are_integers_of_at_least_one(tmp_path, make_ctx, arguments, message):
    (tmp_path / "ten.txt").write_text("a\nb\n")

    result = await _read(make_ctx, path="ten.txt", **arguments)

    assert result.is_error
    assert result_text(result) == f"{message} must be an integer of at least 1"


@pytest.mark.parametrize(
    "spelling", ["big file.txt", "~/big file.txt", "file://{home}/big%20file.txt", "@big file.txt"]
)
async def test_an_over_long_first_line_points_at_bash_with_the_file_read_opened(tmp_path, make_ctx, spelling):
    """The suggested command names the resolved file, so it runs whatever spelling the model used."""
    home = os.path.expanduser("~")
    os.makedirs(home, exist_ok=True)
    target = os.path.join(home, "big file.txt")
    with open(target, "w") as handle:
        handle.write("short\n" + "x" * 61_440 + "\nafter\n")

    result = await ReadTool().execute({"path": spelling.format(home=home), "offset": 2}, make_ctx(cwd=home))
    text = result_text(result)

    assert text == (
        f"[Line 2 is 60.0KB, exceeds 50.0KB limit. Use bash: sed -n '2p' {shlex.quote(target)} | head -c 51200]"
    )


class _CancelledAfter(CancelToken):
    """A cancel that lands after the scan has read a few chunks."""

    def __init__(self, reads: int) -> None:
        super().__init__()
        self._reads_left = reads

    @property
    def cancelled(self) -> bool:
        self._reads_left -= 1
        return self._reads_left < 0


async def test_cancel_stops_a_long_scan(tmp_path, make_ctx, monkeypatch):
    monkeypatch.setattr(read_module, "_READ_CHUNK_BYTES", 1024)
    (tmp_path / "long.txt").write_bytes(b"x\n" * 500_000)

    result = await ReadTool().execute({"path": "long.txt"}, make_ctx(cancel=_CancelledAfter(reads=10)))

    assert result.is_error
    assert result_text(result) == "Operation aborted"


def _whole_file_read(data: bytes, start: int, stop):
    """Pi's algorithm on the whole file, as the oracle for the streamed reader."""
    lines = data.decode("utf-8", "replace").split("\n")
    # The "\r" of a CRLF break is not shown.
    lines = [line[:-1] if line.endswith("\r") and i < len(lines) - 1 else line for i, line in enumerate(lines)]
    return truncate_head("\n".join(lines[start:stop]), max_lines=5, max_bytes=20), len(lines)


async def test_streaming_matches_reading_the_whole_file(tmp_path, monkeypatch):
    # Tiny caps and chunks put every cap and chunk boundary inside a few bytes.
    monkeypatch.setattr(read_module, "MAX_LINES", 5)
    monkeypatch.setattr(read_module, "MAX_BYTES", 20)
    pieces = [b"", b"a", b"abc", b"x" * 30, "é".encode(), b"\xff", b"\n", b"\n\n", b"\r\n"]
    rng = random.Random(7)
    path = tmp_path / "f"
    for _ in range(300):
        monkeypatch.setattr(read_module, "_READ_CHUNK_BYTES", rng.choice([1, 2, 3, 7, 64]))
        data = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        path.write_bytes(data)
        start = rng.randint(0, 6)
        stop = None if rng.random() < 0.5 else start + rng.randint(1, 6)

        with open(path, "rb") as handle:
            lines, total, first_line_bytes = read_module._scan_lines(handle, start, stop)
        expected, expected_total = _whole_file_read(data, start, stop)

        assert total == expected_total, data
        if start >= total:
            continue
        got = truncate_head("\n".join(lines), max_lines=5, max_bytes=20)
        assert (got.content, got.truncated, got.truncated_by, got.first_line_exceeds_limit) == (
            expected.content,
            expected.truncated,
            expected.truncated_by,
            expected.first_line_exceeds_limit,
        ), (data, start, stop)
        if got.first_line_exceeds_limit:
            assert first_line_bytes == len(data.split(b"\n")[start])


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(*, animated: bool = False, padding: int = 0) -> bytes:
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if padding:
        ihdr += _chunk(b"tEXt", b"Comment\0" + b"x" * padding)
    actl = _chunk(b"acTL", struct.pack(">II", 1, 0)) if animated else b""
    idat = _chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + actl + idat + _chunk(b"IEND", b"")


async def test_an_image_is_stored_through_the_sink_and_attached(tmp_path, make_ctx):
    (tmp_path / "pic.png").write_bytes(_png())
    stored = []

    async def sink(data, mime_type, name):
        stored.append((data, mime_type, name))
        return "media_1"

    result = await ReadTool(image_sink=sink).execute({"path": "pic.png"}, make_ctx())

    assert stored == [(_png(), "image/png", "pic.png")]
    assert result.content == (
        TextBlock(text="Read image file [image/png]"),
        ImageBlock(mime_type="image/png", media_token="media_1", name="pic.png"),
    )


@pytest.mark.parametrize(
    ("name", "data", "cap", "expected"),
    [
        (
            "pic.bmp",
            struct.pack("<2sIHHIIiiHH", b"BM", 58, 0, 0, 54, 40, 1, 1, 1, 24) + bytes(28),
            None,
            "Read image file [image/bmp]\n[Image omitted: could not be converted to a supported inline image format.]",
        ),
        (
            # Recognized but not sendable as it is: omitted, never decoded as text.
            "anim.png",
            _png(animated=True),
            None,
            "Read image file [image/apng]\n[Image omitted: could not be converted to a supported inline image format.]",
        ),
        (
            # acTL behind a large ancillary chunk, past the sniffed prefix.
            "late.png",
            _png(animated=True, padding=10_000),
            None,
            "Read image file [image/apng]\n[Image omitted: could not be converted to a supported inline image format.]",
        ),
        (
            "pic.png",
            _png(),
            16,
            "Read image file [image/png]\n[Image omitted: the file is {size}B, over the 16B inline image limit. "
            "Images are not resized.]",
        ),
    ],
    ids=["bmp", "animated", "late-actl", "oversized"],
)
async def test_images_that_cannot_be_sent_as_they_are_are_omitted(
    tmp_path, make_ctx, monkeypatch, name, data, cap, expected
):
    (tmp_path / name).write_bytes(data)
    if cap is not None:
        monkeypatch.setattr(read_module, "MAX_INLINE_IMAGE_BYTES", cap)
    stored = []

    result = await ReadTool(image_sink=lambda *args: stored.append(args) or "never").execute({"path": name}, make_ctx())

    assert stored == []
    assert result.content == (TextBlock(text=expected.format(size=len(data))),)


async def test_read_takes_the_image_size_from_the_file_it_reads(tmp_path, make_ctx, monkeypatch):
    """The image grew after a check by path: the cap holds on the bytes actually read."""
    (tmp_path / "pic.png").write_bytes(_png() + b"\0" * 64)
    monkeypatch.setattr(read_module, "MAX_INLINE_IMAGE_BYTES", 40)
    monkeypatch.setattr(read_module.os.path, "getsize", lambda path: 10)  # what an earlier check by path saw
    stored = []

    result = await ReadTool(image_sink=lambda *args: stored.append(args) or "never").execute(
        {"path": "pic.png"}, make_ctx()
    )

    assert stored == []
    assert "[Image omitted: the file is" in result_text(result)


async def test_a_fifo_swapped_in_after_the_check_does_not_block_read(tmp_path, make_ctx, monkeypatch):
    os.mkfifo(tmp_path / "p")
    monkeypatch.setattr(read_module, "target_kind", lambda path: "regular")  # it was a file when checked
    task = asyncio.ensure_future(ReadTool().execute({"path": "p"}, make_ctx()))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except asyncio.TimeoutError:
        while not task.done():
            try:
                os.close(os.open(tmp_path / "p", os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            await asyncio.sleep(0.05)
        pytest.fail("read blocked on a FIFO")

    assert (result.is_error, result_text(result)) == (True, "Cannot read p: it is not a regular file.")


async def test_a_huge_png_shaped_file_is_classified_without_walking_it(tmp_path, make_ctx):
    """A sparse file of empty PNG chunks: image work is bounded by the cap, not by the file."""
    path = tmp_path / "huge.png"
    with open(path, "wb") as handle:
        handle.write(_png()[:33])  # signature and IHDR, then nothing but zero bytes
        handle.truncate(64 * 1024 * 1024)
    started = time.monotonic()

    result = await ReadTool().execute({"path": "huge.png"}, make_ctx())

    assert time.monotonic() - started < 1.0
    assert result_text(result).startswith("Read image file [image/png]\n[Image omitted: the file is 64.0MB")
