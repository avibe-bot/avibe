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

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[P^_][^\x1b]*\x1b\\"  # DCS, PM, APC ... ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other escape sequences
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
# An open line (no newline yet) keeps at most this many characters of its last segment.
_MAX_OPEN_CHARS = 64 * 1024


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

    def feed(self, data: bytes) -> str:
        return self._consume(self._decoder.decode(data))

    def flush(self) -> str:
        out = self._consume(self._decoder.decode(b"", final=True))
        if self._carry_cr:
            self._carry_cr = False
            self._open_segments("\r")
        tail = self._line()
        self._reset()
        return out + tail

    def peek(self) -> str:
        return self._line()

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
        out: list[str] = []
        lines = text.split("\n")
        for line in lines[:-1]:
            if line.endswith("\r"):
                line = line[:-1]
            self._open_segments(line)
            out.append(self._line() + "\n")
            self._reset()
        self._open_segments(lines[-1])
        return "".join(out)

    def _open_segments(self, text: str) -> None:
        segments = (self._cur + text).split("\r")
        if len(segments) > 1:
            # The first piece continues the current segment, so it keeps that segment's drop count.
            for index in range(len(segments) - 2, -1, -1):
                if strip_ansi(segments[index]):
                    self._prev = segments[index]
                    self._prev_dropped = self._cur_dropped if index == 0 else 0
                    break
            self._cur_dropped = 0
        self._cur = segments[-1]
        if len(self._cur) > _MAX_OPEN_CHARS:
            cut = len(self._cur) - _MAX_OPEN_CHARS
            self._cur_dropped += utf8_len(self._cur[:cut])
            self._cur = self._cur[cut:]

    def _line(self) -> str:
        for segment, dropped in ((self._cur, self._cur_dropped), (self._prev, self._prev_dropped)):
            visible = strip_ansi(segment)
            if visible:
                return f"[... {dropped} bytes omitted ...]{visible}" if dropped else visible
        return ""

    def _reset(self) -> None:
        self._prev, self._prev_dropped = "", 0
        self._cur, self._cur_dropped = "", 0


def normalize_output(data: bytes) -> str:
    normalizer = OutputNormalizer()
    return normalizer.feed(data) + normalizer.flush()
