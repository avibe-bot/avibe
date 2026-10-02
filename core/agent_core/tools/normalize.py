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

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b[P^_][^\x1b]*\x1b\\"  # DCS, PM, APC ... ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other escape sequences
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
)
# An escape sequence that may still be incomplete at the end of a chunk.
_HOLD_BACK_CHARS = 64
# An open line (no newline yet) is committed once its current segment grows past this.
_MAX_OPEN_CHARS = 64 * 1024


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x1b", "")


def _collapse(segments: list[str]) -> str:
    """The last segment that still shows something once escape sequences are gone."""
    for segment in reversed(segments):
        visible = strip_ansi(segment)
        if visible:
            return visible
    return ""


class OutputNormalizer:
    """Incremental normalizer: feed raw bytes, receive normalized text.

    ``feed`` returns only text that is final (complete lines, or an over-long
    open line); ``peek`` shows the open line as it would read now.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._carry_cr = False
        self._prev = ""  # last non-empty finished segment of the open line
        self._cur = ""  # segment after the last carriage return of the open line

    def feed(self, data: bytes) -> str:
        return self._consume(self._decoder.decode(data))

    def flush(self) -> str:
        out = self._consume(self._decoder.decode(b"", final=True))
        if self._carry_cr:
            self._carry_cr = False
            self._open_segments("\r")
        tail = _collapse([self._prev, self._cur])
        self._prev = self._cur = ""
        return out + tail

    def peek(self) -> str:
        return _collapse([self._prev, self._cur])

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
            out.append(_collapse([self._prev, self._cur]) + "\n")
            self._prev = self._cur = ""
        self._open_segments(lines[-1])
        if len(self._cur) > _MAX_OPEN_CHARS:
            out.append(self._commit_open())
        return "".join(out)

    def _open_segments(self, text: str) -> None:
        segments = (self._cur + text).split("\r")
        if len(segments) > 1:
            for segment in reversed(segments[:-1]):
                if strip_ansi(segment):
                    self._prev = segment
                    break
        self._cur = segments[-1]

    def _commit_open(self) -> str:
        # Bound memory on a line that never ends; keep a possibly incomplete escape.
        cut = len(self._cur)
        escape = self._cur.rfind("\x1b", max(0, cut - _HOLD_BACK_CHARS))
        if escape != -1 and not _ANSI_RE.match(self._cur, escape):
            cut = escape
        committed, self._cur = self._cur[:cut], self._cur[cut:]
        self._prev = ""
        return strip_ansi(committed)


def normalize_output(data: bytes) -> str:
    normalizer = OutputNormalizer()
    return normalizer.feed(data) + normalizer.flush()
