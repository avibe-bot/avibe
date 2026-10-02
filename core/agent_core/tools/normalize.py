"""The output normalizer for command output (C-7 section 6).

Every command output the model sees passes through here before the caps:
decode UTF-8 with replacement, strip ANSI escape sequences, and collapse
carriage-return redraws to the final line state. ``pipe`` output rarely needs
more than the decode; the future ``pty`` backend needs all of it.

A redrawn line keeps its last non-empty segment: ``"10%\\r55%\\r100%\\n"``
becomes ``"100%\\n"``, and ``"done\\r"`` at the end stays ``"done"``. ``"\\r\\n"``
is a line break, never a redraw.
"""

from __future__ import annotations

import codecs
import re

from core.agent_core.tools.truncate import utf8_len

# No sequence spans a line break or a carriage return, so stripping a run of whole lines at once equals
# stripping each redraw segment of each line on its own.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b\n\r]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[P^_][^\x1b\n\r]*\x1b\\"  # DCS, PM, APC ... ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other escape sequences
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
# An open line (no newline yet) keeps at most this many characters of its last segment.
_MAX_OPEN_CHARS = 64 * 1024
# A line longer than the bound, found from line starts only, so the search is linear.
_LONG_LINE = re.compile(r"^[^\n]{%d}" % (_MAX_OPEN_CHARS + 1), re.MULTILINE)
# A redrawn line keeps its last segment that shows something: drop the empty segments at its end,
# then everything up to its last carriage return.
_TRAILING_RETURNS = re.compile(r"\r+$", re.MULTILINE)
_BEFORE_LAST_RETURN = re.compile(r"^[^\n]*\r", re.MULTILINE)


def _bounded(segment: str, dropped: int) -> tuple[str, int]:
    """``segment``'s last ``_MAX_OPEN_CHARS`` characters, and the UTF-8 bytes dropped from its start."""
    if len(segment) <= _MAX_OPEN_CHARS:
        return segment, dropped
    cut = len(segment) - _MAX_OPEN_CHARS
    return segment[cut:], dropped + utf8_len(segment[:cut])


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x1b", "")


class OutputNormalizer:
    """Incremental normalizer: feed raw bytes, receive normalized text.

    ``feed`` returns only complete lines, which a later carriage return can no
    longer redraw; ``peek`` shows the open line as it would read now. An open
    line keeps at most its last ``_MAX_OPEN_CHARS`` characters, so memory stays
    bounded; if such a line is final, it starts with a marker saying how many
    bytes were dropped.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry_cr = False
        # The open line: its last segment that still shows something, and the segment after the
        # last carriage return, each with the UTF-8 bytes dropped from its start.
        self._prev, self._prev_dropped = "", 0
        self._cur, self._cur_dropped = "", 0
        # UTF-8 bytes of the last finished line, counting what was dropped from its start.
        self._finished_line_bytes = 0

    def feed(self, data: bytes) -> str:
        return self._consume(self._decoder.decode(data))

    def flush(self) -> str:
        out = self._consume(self._decoder.decode(b"", final=True))
        if self._carry_cr:
            self._carry_cr = False
            self._open_segments("\r")
        tail, size = self._line_and_size()
        if tail:
            self._finished_line_bytes = size
        self._reset()
        return out + tail

    def peek(self) -> str:
        return self._line()

    def last_line_bytes(self) -> int:
        """UTF-8 bytes of the last line: the open one if it shows anything, else the last finished one.

        Bytes dropped from the start of a long line count (before ANSI stripping), so the size is the
        line's, not the kept part's.
        """
        _, size = self._line_and_size()
        return size or self._finished_line_bytes

    def _consume(self, text: str) -> str:
        if self._carry_cr:
            text = "\r" + text
            self._carry_cr = False
        if not text:
            return ""
        if text.endswith("\r"):
            # Possibly the first half of "\r\n"; decide with the next chunk.
            self._carry_cr = True
            text = text[:-1]
        last = text.rfind("\n")
        if last == -1:
            self._open_segments(text)
            return ""
        first = text.find("\n")
        out: list[str] = []
        self._finish_line(text[:first], out)  # completes the open line
        if first != last:
            middle = text[first + 1 : last].replace("\r\n", "\n")
            if not _LONG_LINE.search(middle):
                # No line over the bound: each line becomes its last visible segment in a few passes over the
                # whole run, without a Python step per line, so a flood costs what it reads (redraws included).
                visible = strip_ansi(middle)
                if "\r" in visible:
                    visible = _BEFORE_LAST_RETURN.sub("", _TRAILING_RETURNS.sub("", visible))
                out.append(visible + "\n")
                self._finished_line_bytes = utf8_len(visible[visible.rfind("\n") + 1 :])
            else:
                for line in middle.split("\n"):
                    self._finish_line(line, out)
        self._open_segments(text[last + 1 :])
        return "".join(out)

    def _finish_line(self, line: str, out: list[str]) -> None:
        if line.endswith("\r"):
            line = line[:-1]
        self._open_segments(line)
        text, self._finished_line_bytes = self._line_and_size()
        out.append(text + "\n")
        self._reset()

    def _open_segments(self, text: str) -> None:
        segments = (self._cur + text).split("\r")
        if len(segments) > 1:
            # The first piece continues the current segment, so it keeps that segment's drop count.
            for index in range(len(segments) - 2, -1, -1):
                if strip_ansi(segments[index]):
                    self._prev, self._prev_dropped = _bounded(segments[index], self._cur_dropped if index == 0 else 0)
                    break
            self._cur_dropped = 0
        self._cur, self._cur_dropped = _bounded(segments[-1], self._cur_dropped)

    def _line(self) -> str:
        return self._line_and_size()[0]

    def _line_and_size(self) -> tuple[str, int]:
        for segment, dropped in ((self._cur, self._cur_dropped), (self._prev, self._prev_dropped)):
            visible = strip_ansi(segment)
            if visible:
                text = f"[... {dropped} bytes omitted ...]{visible}" if dropped else visible
                return text, dropped + utf8_len(visible)
        return "", 0

    def _reset(self) -> None:
        self._prev, self._prev_dropped = "", 0
        self._cur, self._cur_dropped = "", 0


def normalize_output(data: bytes) -> str:
    normalizer = OutputNormalizer()
    return normalizer.feed(data) + normalizer.flush()
