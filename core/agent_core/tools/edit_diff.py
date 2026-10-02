"""Matching and diff helpers for ``edit`` (C-7 section 4).

Ported from Pi ``packages/coding-agent/src/core/tools/edit-diff.ts`` (MIT,
Copyright (c) 2025 Mario Zechner): exact match first, then Pi's deterministic
normalized match with no similarity threshold, every edit matched against the
original, overlapping edits rejected. Avibe adds ``replaceAll`` (from Claude
Code and OpenCode): every occurrence in the tier that matched, each one a span
for the overlap check. Avibe departures from Pi: each edit matches in its own
tier, so one normalized edit no longer switches the whole batch to normalized
text; uniqueness is counted in the tier that matched; and the file is never
re-encoded: replacements are spliced into the original text, so its BOM, its
line breaks (mixed or not), and every byte outside a replaced span survive.
"""

from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

from core.agent_core.tools.text import shown

BOM = "\ufeff"

# JavaScript's String.prototype.trimEnd set, which Pi uses (Python's isspace differs).
_BREAK = re.compile(r"\r\n|\r|\n")
_TRAILING_SPACE = " \t\v\f\r\u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"
_SMART_SINGLE = re.compile("[\u2018\u2019\u201a\u201b]")
_SMART_DOUBLE = re.compile("[\u201c\u201d\u201e\u201f]")
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPECIAL_SPACES = re.compile("[\u00a0\u2002-\u200a\u202f\u205f\u3000]")


class EditError(ValueError):
    """An edit that cannot be applied; the message is the model-facing error."""


@dataclass(frozen=True)
class Edit:
    old_text: str
    new_text: str
    replace_all: bool = False


@dataclass(frozen=True)
class _Replacement:
    edit_index: int
    index: int
    length: int
    new_text: str


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_for_fuzzy_match(text: str) -> str:
    """NFKC, no trailing whitespace per line, ASCII quotes and dashes, plain spaces."""
    text = unicodedata.normalize("NFKC", text)
    # rstrip, not a "[...]+$" regex: the regex retries at every space of a run, which is quadratic.
    text = "\n".join(line.rstrip(_TRAILING_SPACE) for line in text.split("\n"))
    text = _SMART_SINGLE.sub("'", text)
    text = _SMART_DOUBLE.sub('"', text)
    text = _DASHES.sub("-", text)
    return _SPECIAL_SPACES.sub(" ", text)


def _find_all(content: str, needle: str) -> list[int]:
    found: list[int] = []
    if not needle:
        return found
    index = content.find(needle)
    while index != -1:
        found.append(index)
        index = content.find(needle, index + len(needle))
    return found


def _occurrences(content: str, normalized: Callable[[], str], old_text: str) -> tuple[bool, list[tuple[int, int]]]:
    """``(used_normalized, spans)``: the edit's occurrences in the first tier that has one.

    Exact spans are in the original text, normalized spans in the normalized text. Text that
    normalizes to nothing never matches in the normalized tier.
    """
    exact = _find_all(content, old_text)
    if exact:
        return False, [(index, len(old_text)) for index in exact]
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    return True, [(index, len(fuzzy_old)) for index in _find_all(normalized(), fuzzy_old)]


def _not_found(path: str, index: int, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"Could not find the exact text in {path}. The old text must match exactly including all whitespace "
            "and newlines."
        )
    return EditError(
        f"Could not find edits[{index}] in {path}. The oldText must match exactly including all whitespace and "
        "newlines."
    )


def _duplicate(path: str, index: int, total: int, occurrences: int) -> EditError:
    if total == 1:
        return EditError(
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. Please provide more "
            "context to make it unique."
        )
    return EditError(
        f"Found {occurrences} occurrences of edits[{index}] in {path}. Each oldText must be unique. Please provide "
        "more context to make it unique."
    )


def _empty(path: str, index: int, total: int) -> EditError:
    if total == 1:
        return EditError(f"oldText must not be empty in {path}.")
    return EditError(f"edits[{index}].oldText must not be empty in {path}.")


def _no_change(path: str, total: int) -> EditError:
    if total == 1:
        return EditError(
            f"No changes made to {path}. The replacement produced identical content. This might indicate an issue "
            "with special characters or the text not existing as expected."
        )
    return EditError(f"No changes made to {path}. The replacements produced identical content.")


def _apply(content: str, replacements: list[_Replacement], offset: int = 0) -> str:
    """``content`` with disjoint ``replacements`` applied, in one pass (``replaceAll`` can make thousands)."""
    pieces: list[str] = []
    pos = 0
    for replacement in sorted(replacements, key=lambda r: r.index):
        start = replacement.index - offset
        pieces += (content[pos:start], replacement.new_text)
        pos = start + replacement.length
    pieces.append(content[pos:])
    return "".join(pieces)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def _overlap(path: str, first: int, second: int) -> EditError:
    return EditError(
        f"edits[{first}] and edits[{second}] overlap in {path}. Merge them into one edit or target disjoint regions."
    )


def _check_disjoint(path: str, replacements: list[_Replacement]) -> None:
    ordered = sorted(replacements, key=lambda r: r.index)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.index + previous.length > current.index:
            raise _overlap(path, previous.edit_index, current.edit_index)


class _Lines:
    """The file as lines with their own breaks, and an LF view whose offsets map back to the file.

    Matching happens in the view, as Pi matches LF-normalized text; replacements are spliced into
    the original, so a BOM, every line break, and every byte outside a replaced span stay as they were.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        prefix = len(BOM) if text.startswith(BOM) else 0
        self.contents: list[str] = []
        self.breaks: list[str] = []
        pos = prefix
        for match in _BREAK.finditer(text, prefix):
            self.contents.append(text[pos : match.start()])
            self.breaks.append(match.group())
            pos = match.end()
        self.contents.append(text[pos:])
        self.breaks.append("")
        self.first_break = next((brk for brk in self.breaks if brk), "\n")
        # What read showed (one U+FFFD per undecodable byte), one character for one, so offsets map back.
        self.view = shown("\n".join(self.contents))
        self.view_starts: list[int] = []
        self.starts: list[int] = []
        view_pos, pos = 0, prefix
        for content, brk in zip(self.contents, self.breaks):
            self.view_starts.append(view_pos)
            self.starts.append(pos)
            view_pos += len(content) + 1
            pos += len(content) + len(brk)

    def to_original(self, view_offset: int) -> int:
        line = bisect.bisect_right(self.view_starts, view_offset) - 1
        return self.starts[line] + (view_offset - self.view_starts[line])

    def line_end(self, line: int) -> int:
        """Original offset just past ``line`` and its break."""
        return self.starts[line + 1] if line + 1 < len(self.starts) else len(self.text)

    def break_for(self, lo: int) -> str:
        """The break new text uses for a span starting at ``lo``: the first break at or after it (inside the
        span, else the one after it), which is the break of ``lo``'s line, else the file's first."""
        return self.breaks[bisect.bisect_right(self.starts, lo) - 1] or self.first_break

    def with_breaks(self, lf_text: str, lo: int, own: Optional[list[str]] = None) -> str:
        """``lf_text`` with each ``\\n`` turned into ``own``'s next break, then the break of the span at ``lo``."""
        if "\n" not in lf_text:
            return lf_text
        pieces = lf_text.split("\n")
        fallback = self.break_for(lo)
        out = [pieces[0]]
        for index, piece in enumerate(pieces[1:]):
            brk = own[index] if own is not None and index < len(own) and own[index] else fallback
            out.append(brk + piece)
        return "".join(out)


def _exact_replacements(lines: _Lines, matches: list[_Replacement]) -> list[_Replacement]:
    out = []
    for match in matches:
        lo, hi = lines.to_original(match.index), lines.to_original(match.index + match.length)
        out.append(_Replacement(match.edit_index, lo, hi - lo, lines.with_breaks(match.new_text, lo)))
    return out


def _line_groups(lines: _Lines, normalized: str, matches: list[_Replacement]) -> list[_Replacement]:
    """Normalized-tier matches as whole-line replacements of the original.

    Normalization keeps every line break, so line ``i`` of the normalized view is line ``i`` of the
    file. Matches sharing lines form one group; its lines are rewritten from the normalized view with
    the group's matches applied (Pi's way of applying a normalized match), and each rewritten line
    keeps its own break.
    """
    normalized_starts = _line_starts(normalized)
    if len(normalized_starts) != len(lines.starts):
        raise EditError("Cannot preserve unchanged lines because the base content has a different line count.")

    def line_of(index: int) -> int:
        return bisect.bisect_right(normalized_starts, index) - 1

    groups: list[list] = []  # [first_line, last_line, members]
    for match in sorted(matches, key=lambda r: r.index):
        first, last = line_of(match.index), line_of(match.index + match.length - 1)
        if groups and first <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], last)
            groups[-1][2].append(match)
        else:
            groups.append([first, last, [match]])

    out: list[_Replacement] = []
    for first, last, members in groups:
        lo = normalized_starts[first]
        hi = normalized_starts[last + 1] if last + 1 < len(normalized_starts) else len(normalized)
        rewritten = _apply(normalized[lo:hi], members, lo)
        original_lo, original_hi = lines.starts[first], lines.line_end(last)
        text = lines.with_breaks(rewritten, original_lo, lines.breaks[first : last + 1])
        out.append(_Replacement(members[0].edit_index, original_lo, original_hi - original_lo, text))
    return out


def apply_edits(text: str, edits: list[Edit], path: str) -> tuple[str, str, str]:
    """Apply every edit to the file's ``text``; return the new text and the LF views before and after.

    Each edit matches in its own tier against the original, in the file's LF view. Exact edits replace
    exactly the text they matched; a normalized edit rewrites the whole lines it touches from the
    normalized view. Uniqueness, ``replaceAll``, overlap, and application are all per edit in original
    coordinates, and replacements are spliced into the original, so nothing an edit did not match
    changes: not a byte, not a line break. (Pi matches a whole batch in normalized text as soon as one
    edit needs it, and re-encodes every line break in the file.)
    """
    edits = [Edit(normalize_to_lf(e.old_text), normalize_to_lf(e.new_text), e.replace_all) for e in edits]
    total = len(edits)
    for index, edit in enumerate(edits):
        if not edit.old_text:
            raise _empty(path, index, total)

    lines = _Lines(text)
    cache: list[str] = []

    def normalized() -> str:
        if not cache:
            cache.append(normalize_for_fuzzy_match(lines.view))
        return cache[0]

    exact: list[_Replacement] = []
    fuzzy: list[_Replacement] = []
    for index, edit in enumerate(edits):
        used_normalized, spans = _occurrences(lines.view, normalized, edit.old_text)
        if not spans:
            raise _not_found(path, index, total)
        if not edit.replace_all and len(spans) > 1:
            raise _duplicate(path, index, total, len(spans))
        chosen = spans if edit.replace_all else spans[:1]
        (fuzzy if used_normalized else exact).extend(
            _Replacement(index, at, length, edit.new_text) for at, length in chosen
        )

    _check_disjoint(path, fuzzy)  # in normalized coordinates, before they are grouped into lines
    replacements = _exact_replacements(lines, exact) + (_line_groups(lines, normalized(), fuzzy) if fuzzy else [])
    _check_disjoint(path, replacements)
    new_text = _apply(text, replacements)
    if new_text == text:
        raise _no_change(path, total)
    return new_text, lines.view, _Lines(new_text).view


#: The display diff runs difflib only on the changed middle, and skips it when either side is longer.
DIFF_MAX_LINES = 1000


class _FixedOpcodes(difflib.SequenceMatcher):
    """difflib's hunk grouping over opcodes computed elsewhere."""

    def __init__(self, a: list[str], b: list[str], opcodes: list[tuple[str, int, int, int, int]]) -> None:
        super().__init__(None, a, b)
        self._fixed = opcodes

    def get_opcodes(self) -> list[tuple[str, int, int, int, int]]:  # type: ignore[override]
        return self._fixed


def display_diff(path: str, old: str, new: str, context: int = 4) -> Optional[tuple[str, Optional[int], str]]:
    """``(diff, first_changed_line, patch)`` for display, or ``None`` when the change is too large to show.

    The common leading and trailing lines are trimmed in linear time, and difflib (whose worst case
    is quadratic, for example on repetitive lines) sees only the changed middle, at most
    ``DIFF_MAX_LINES`` lines on each side.
    """
    a, b = _lines_with_breaks(old), _lines_with_breaks(new)
    shortest = min(len(a), len(b))
    prefix = 0
    while prefix < shortest and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while suffix < shortest - prefix and a[len(a) - 1 - suffix] == b[len(b) - 1 - suffix]:
        suffix += 1
    a_end, b_end = len(a) - suffix, len(b) - suffix
    if a_end - prefix > DIFF_MAX_LINES or b_end - prefix > DIFF_MAX_LINES:
        return None
    middle = difflib.SequenceMatcher(None, a[prefix:a_end], b[prefix:b_end]).get_opcodes()
    opcodes = [("equal", 0, prefix, 0, prefix)] if prefix else []
    opcodes += [(tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix) for tag, i1, i2, j1, j2 in middle]
    if suffix:
        opcodes.append(("equal", a_end, len(a), b_end, len(b)))
    diff, first_changed_line = _render_diff(a, b, opcodes, context)
    return diff, first_changed_line, _unified_patch(path, a, b, opcodes, context)


def _lines_with_breaks(text: str) -> list[str]:
    """Lines split at "\n" only, each keeping its "\n", as jsdiff's ``diffLines`` sees them.

    ``str.splitlines`` also breaks at \f, \v, \x1c-\x1e, \x85, U+2028 and U+2029, which are not line
    breaks for ``read``, ``edit``, or a patch.
    """
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


def _unified_range(start: int, stop: int) -> str:
    """jsdiff's hunk range: always ``start,count``, and an empty range starts at the line before."""
    length = stop - start
    return f"{start + 1 if length else start},{length}"


def _patch_line(sign: str, line: str) -> str:
    # Only a file's last line can lack "\n"; jsdiff marks it the way GNU diff does.
    return sign + line if line.endswith("\n") else f"{sign}{line}\n\\ No newline at end of file\n"


def _unified_patch(path: str, a: list[str], b: list[str], opcodes: list, context: int) -> str:
    """Pi's ``generateUnifiedPatch`` (jsdiff ``createTwoFilesPatch`` with file headers only), from the opcodes."""
    groups = list(_FixedOpcodes(a, b, opcodes).get_grouped_opcodes(context))
    if not groups:
        return ""
    out = [f"--- {path}\n", f"+++ {path}\n"]
    for group in groups:
        first, last = group[0], group[-1]
        out.append(f"@@ -{_unified_range(first[1], last[2])} +{_unified_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(_patch_line(" ", line) for line in a[i1:i2])
                continue
            out.extend(_patch_line("-", line) for line in a[i1:i2])
            out.extend(_patch_line("+", line) for line in b[j1:j2])
    return "".join(out)


def _split_count(lines: list[str]) -> int:
    return len(lines) + (0 if lines and not lines[-1].endswith("\n") else 1)


def _render_diff(a: list[str], b: list[str], opcodes: list, context: int) -> tuple[str, Optional[int]]:
    """Pi's display diff: numbered ``+``/``-``/context lines and the first changed line of the new file."""
    # Pi's width: the digits of the larger ``split("\n")`` count, which has one more entry after a final "\n".
    width = len(str(max(_split_count(a), _split_count(b))))
    a = [line[:-1] if line.endswith("\n") else line for line in a]
    b = [line[:-1] if line.endswith("\n") else line for line in b]
    parts: list[tuple[str, list[str]]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            parts.append((" ", a[i1:i2]))
            continue
        if i2 > i1:
            parts.append(("-", a[i1:i2]))
        if j2 > j1:
            parts.append(("+", b[j1:j2]))

    out: list[str] = []
    old_no = new_no = 1
    last_was_change = False
    first_changed: Optional[int] = None
    blank = " " * width
    for position, (kind, raw) in enumerate(parts):
        if kind != " ":
            if first_changed is None:
                first_changed = new_no
            for line in raw:
                if kind == "+":
                    out.append(f"+{str(new_no).rjust(width)} {line}")
                    new_no += 1
                else:
                    out.append(f"-{str(old_no).rjust(width)} {line}")
                    old_no += 1
            last_was_change = True
            continue

        trailing = position < len(parts) - 1 and parts[position + 1][0] != " "
        if last_was_change and trailing and len(raw) > context * 2:
            shown_head, skipped, shown_tail = raw[:context], len(raw) - 2 * context, raw[-context:]
        elif last_was_change and trailing:
            shown_head, skipped, shown_tail = raw, 0, []
        elif last_was_change:
            shown_head, skipped, shown_tail = raw[:context], max(0, len(raw) - context), []
        elif trailing:
            skipped = max(0, len(raw) - context)
            shown_head, shown_tail = [], raw[skipped:]
        else:
            shown_head, skipped, shown_tail = [], len(raw), []
            old_no += skipped
            new_no += skipped
            last_was_change = False
            continue
        for line in shown_head:
            out.append(f" {str(old_no).rjust(width)} {line}")
            old_no += 1
            new_no += 1
        if skipped:
            out.append(f" {blank} ...")
            old_no += skipped
            new_no += skipped
        for line in shown_tail:
            out.append(f" {str(old_no).rjust(width)} {line}")
            old_no += 1
            new_no += 1
        last_was_change = False
    return "\n".join(out), first_changed
