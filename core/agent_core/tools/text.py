"""How file bytes become the text the model sees, shared by ``read`` and ``edit``.

Bytes are decoded with ``surrogateescape``, so every byte that is not UTF-8 survives as one escape
character and can be written back unchanged. The model sees each such byte as one U+FFFD. The
mapping is one character for one character, so ``edit`` can match what ``read`` showed and still
splice into the original bytes. (Pi, through the WHATWG decoder, shows one U+FFFD per invalid
sequence; Avibe shows one per invalid byte.)
"""

from __future__ import annotations

import re

_SHOWN = {code: "\ufffd" for code in range(0xDC80, 0xDD00)}
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def decode_file(data: bytes) -> str:
    """The file's text with undecodable bytes kept as surrogate escapes."""
    return data.decode("utf-8", "surrogateescape")


def shown(text: str) -> str:
    """``text`` as the model sees it: each surrogate escape becomes U+FFFD."""
    return text.translate(_SHOWN)


def encode_file(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def model_text(text: str) -> str:
    """Text from the model, writable as UTF-8: a lone surrogate (JSON allows one) becomes U+FFFD.

    The one sanitizer for model-supplied text, used by write's content and edit's oldText/newText.
    """
    return _LONE_SURROGATE.sub("\ufffd", text)
