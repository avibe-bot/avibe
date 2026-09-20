"""Inventory literal shell assignments without interpreting a startup script.

Only complete, top-level assignment lines are removable. This is an inventory
of stored values, not a model of shell execution or effective precedence.
Includes and ordinary commands are not followed. The small lexer exists to
keep quoted text, compound commands and here-documents out of the literal path.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .migration_journal import NativeFileEdit


SHELL_PROFILE_NAMES = (
    ".profile", ".bash_profile", ".bash_login", ".bashrc",
    ".zshenv", ".zprofile", ".zshrc", ".zlogin",
)

_NAME = r"[a-zA-Z_][a-zA-Z_0-9]*"
_ASSIGNMENT = re.compile(rf"^({_NAME})=")
_WRITER = re.compile(rf"^({_NAME})(?:\[[^]]*\])?\+?=")
_OPERATORS = ("<<<", "<<-", "&&", "||", ";;", ";&", "<<", ">>", "<&", ">&", "|&", "((", "))")
_SEPARATORS = {";", ";;", ";&", "&", "&&", "||", "|", "|&", "(", ")", "((", "))"}
_DECLARATIONS = {"export", "readonly", "declare", "typeset", "local"}
_COMMAND_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!", "time", "always"}


@dataclass(frozen=True, repr=False)
class ShellAssignment:
    name: str
    value: str
    start: int
    end: int


@dataclass(frozen=True, repr=False)
class ShellIssue:
    names: tuple[str, ...]
    reason: str
    line: int | None


@dataclass(frozen=True, repr=False)
class ShellProfile:
    path: Path
    before: bytes | None
    assignments: tuple[ShellAssignment, ...]
    issues: tuple[ShellIssue, ...]

    @property
    def values(self) -> dict[str, str]:
        invalid = {name for issue in self.issues for name in issue.names}
        values: dict[str, str] = {}
        for assignment in self.assignments:
            if assignment.name in values and values[assignment.name] != assignment.value:
                invalid.add(assignment.name)
            values[assignment.name] = assignment.value
        return {name: value for name, value in values.items() if name not in invalid}


@dataclass(frozen=True, repr=False)
class _Token:
    raw: str
    value: str
    literal: bool = True
    operator: bool = False
    expansions: tuple[tuple[str, str], ...] = ()

    def is_code(self, word: str) -> bool:
        return self.raw == word


@dataclass(frozen=True, repr=False)
class _Statement:
    tokens: tuple[_Token, ...]
    start: int
    end: int
    line: int
    data: bool = False


def _quoted_end(text: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == quote:
            return index + 1
        if text[index] == "\\" and quote != "'":
            index += 1
        index += 1
    return len(text)


def _group_end(text: str, start: int, opening: str, closing: str) -> int:
    """Bound an expansion, never evaluate its contents."""
    depth = 1
    index = start + 1
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            index = _quoted_end(text, index, char)
            continue
        if char == "\\":
            index += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if not depth:
                return index + 1
        index += 1
    return len(text)


def _word(text: str, start: int) -> tuple[_Token, int]:
    value: list[str] = []
    expansions: list[tuple[str, str]] = []
    literal = True
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote is None and (char in " \t\n;&|()<>" or text.startswith("\r\n", index)):
            break
        if char == quote:
            quote = None
            index += 1
            continue
        if quote == "'":
            value.append(char)
            index += 1
            continue
        if char in "'\"" and quote is None:
            quote = char
            index += 1
            continue
        if char == "\\":
            if index + 1 == len(text):
                literal = False
                index += 1
                continue
            following = text[index + 1]
            if text.startswith("\\\r\n", index):
                index += 3
                continue
            if quote == '"' and following not in '$`"\\\n':
                value.append("\\")
                index += 1
                continue
            if following != "\n":
                value.append(following)
            index += 2
            continue
        if char == "$" and index + 1 < len(text) and text[index + 1] in "({":
            opening = text[index + 1]
            closing = ")" if opening == "(" else "}"
            end = _group_end(text, index + 1, opening, closing)
            closed = text[end - 1:end] == closing
            body = text[index + 2:end - 1 if closed else end]
            kind = "command" if opening == "(" else "parameter"
            if opening == "(" and body.startswith("("):
                kind = "arithmetic"
            expansions.append((kind, body))
            value.append(text[index:end])
            literal = False
            index = end
            continue
        if char == "`":
            end = _quoted_end(text, index, "`")
            expansions.append(("command", text[index + 1:end - 1]))
            value.append(text[index:end])
            literal = False
            index = end
            continue
        if char == "$" or (quote is None and char in "~{}*?[]"):
            literal = False
        value.append(char)
        index += 1
    return _Token(
        text[start:index], "".join(value), literal and quote is None,
        expansions=tuple(expansions),
    ), index


def _statements(text: str, *, heredocs: bool = True) -> Iterator[_Statement]:
    """Tokenize logical lines, isolating here-document bodies as data."""
    index = 0
    start = 0
    line = 1
    tokens: list[_Token] = []
    while index < len(text):
        char = text[index]
        if char in " \t" or text.startswith("\r\n", index):
            index += 1
            continue
        if char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        if char == "\n":
            index += 1
            yield _Statement(tuple(tokens), start, index, line)
            line += text.count("\n", start, index)
            start = index
            documents = [
                (token.value, tokens[pos - 1].raw == "<<-")
                for pos, token in enumerate(tokens)
                if pos and tokens[pos - 1].raw in {"<<", "<<-"} and not token.operator
            ] if heredocs else []
            tokens = []
            for delimiter, strip_tabs in documents:
                body_start, body_line = index, line
                while index < len(text):
                    end = text.find("\n", index)
                    end = len(text) if end < 0 else end + 1
                    candidate = text[index:end].removesuffix("\n").removesuffix("\r")
                    if (candidate.lstrip("\t") if strip_tabs else candidate) == delimiter:
                        break
                    index = end
                body = text[body_start:index]
                for statement in _statements(body, heredocs=False):
                    yield _Statement(
                        statement.tokens, body_start + statement.start,
                        body_start + statement.end, body_line + statement.line - 1, True,
                    )
                line += body.count("\n")
                if index < len(text):
                    line += text.count("\n", index, end)
                    index = end
                start = index
            continue
        if char in ";&|()<>":
            if (
                char in "<>" and tokens and tokens[-1].raw.isascii()
                and tokens[-1].raw.isdigit() and text[index - 1:index].isdigit()
            ):
                # An adjacent IO_NUMBER is part of the redirection, not a
                # command word. Quoted numbers and whitespace do not qualify.
                tokens.pop()
            operator = next((op for op in _OPERATORS if text.startswith(op, index)), char)
            tokens.append(_Token(operator, operator, operator=True))
            index += len(operator)
            continue
        token, index = _word(text, index)
        tokens.append(token)
    if start < len(text):
        yield _Statement(tuple(tokens), start, len(text), line)


def _segments(tokens: tuple[_Token, ...]) -> list[list[_Token]]:
    segments: list[list[_Token]] = []
    current: list[_Token] = []
    for token in tokens:
        if token.operator and token.raw in _SEPARATORS:
            if current:
                segments.append(current)
                current = []
            segments.append([token])
        elif not current and token.raw in {"{", "}"} | _COMMAND_PREFIXES:
            segments.append([token])
        elif current and current[0].raw == "function" and token.raw == "{":
            segments.extend([current, [token]])
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _arithmetic_writers(text: str, names: frozenset[str]) -> set[str]:
    # Arithmetic is only inspected inside a known arithmetic/evaluation context.
    return {
        name for name in names
        if re.search(
            rf"(?<![\w])(?:\+\+|--)\s*{re.escape(name)}\b"
            rf"|\b{re.escape(name)}\s*(?:\[[^]]*\]\s*)?(?:\+\+|--|(?:[+*/%&|^-]|<<|>>)?=(?!=))",
            text,
        )
    }


def _embedded_writers(token: _Token, names: frozenset[str], depth: int) -> set[str]:
    found: set[str] = set()
    for kind, body in token.expansions:
        if kind == "parameter":
            match = re.match(rf"^({_NAME}):?=", body)
            if match and match[1] in names:
                found.add(match[1])
            if depth < 12:
                # A fallback is data, but substitutions inside it can write.
                for statement in _statements(body):
                    for nested in statement.tokens:
                        found.update(_embedded_writers(nested, names, depth + 1))
            else:
                found.update(_arithmetic_writers(body, names))
        elif kind == "arithmetic":
            found.update(_arithmetic_writers(body, names))
        elif depth < 12:
            found.update(_written_names(body, names, depth + 1))
        else:
            # Beyond this bounded grammar, retain explicit writer evidence
            # rather than silently ignoring a deeply nested assignment.
            found.update(_arithmetic_writers(body, names))
    return found


def _command_writers(tokens: list[_Token], names: frozenset[str], depth: int) -> set[str]:
    found = set().union(*(_embedded_writers(token, names, depth) for token in tokens))
    words: list[_Token] = []
    redirect = False
    for token in tokens:
        if token.operator and token.raw not in _SEPARATORS:
            redirect = True
        elif redirect:
            redirect = False
        else:
            words.append(token)
    while words and words[0].raw in {"if", "then", "elif", "else", "while", "until", "do", "!"}:
        words = words[1:]
    if not words:
        return found
    if words[0].raw in {"for", "select"}:
        if len(words) > 1 and words[1].value in names:
            found.add(words[1].value)
        return found
    while words:
        match = _WRITER.match(words[0].raw.replace("\\\r\n", "").replace("\\\n", ""))
        if not match:
            break
        if match[1] in names:
            found.add(match[1])
        words = words[1:]
    while words and words[0].value in {"command", "builtin"}:
        words = words[1:]
        while words and words[0].value.startswith("-"):
            words = words[1:]
    if not words:
        return found
    command = words[0].value
    arguments = words[1:]
    if command in _DECLARATIONS:
        for argument in arguments:
            match = _WRITER.match(argument.value)
            if match and match[1] in names:
                found.add(match[1])
    elif command == "env":
        index = 0
        while index < len(arguments):
            argument = arguments[index].value
            if argument in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                index += 2
                continue
            if argument.startswith("-"):
                index += 1
                continue
            match = _WRITER.match(argument)
            if not match:
                break
            if match[1] in names:
                found.add(match[1])
            index += 1
    elif command == "read":
        index = 0
        while index < len(arguments):
            argument = arguments[index].value
            if argument in {"-p", "-u", "-t", "-n", "-N", "-d", "-i"}:
                index += 2
                continue
            if argument in names:
                found.add(argument)
            index += 1
    elif command == "unset":
        found.update(argument.value for argument in arguments if argument.value in names)
    elif command == "getopts" and len(arguments) > 1 and arguments[1].value in names:
        found.add(arguments[1].value)
    elif command == "printf":
        if len(arguments) > 1 and arguments[0].value == "-v" and arguments[1].value in names:
            found.add(arguments[1].value)
    elif command == "let":
        found.update(_arithmetic_writers(" ".join(arg.value for arg in arguments), names))
    elif command in {"eval", "source", "."}:
        # Do not follow includes, infer their contents, or turn variable
        # references into credentials. Only explicitly embedded writers count.
        body = " ".join(arg.value for arg in arguments)
        found.update(
            _written_names(body, names, depth + 1)
            if depth < 12 else _arithmetic_writers(body, names)
        )
    return found


def _written_names(text: str, names: frozenset[str], depth: int = 0) -> set[str]:
    found: set[str] = set()
    for statement in _statements(text):
        for segment in _segments(statement.tokens):
            found.update(_command_writers(segment, names, depth))
        if any(token.raw == "((" for token in statement.tokens):
            found.update(_arithmetic_writers(text[statement.start:statement.end], names))
    return found


def _parse(path: Path, before: bytes, names: frozenset[str]) -> ShellProfile:
    text = before.decode("utf-8")
    assignments: list[ShellAssignment] = []
    issues: list[ShellIssue] = []
    values: dict[str, str] = {}
    stack: list[str] = []
    pending_function = False
    continuation = False
    uncertain = False
    for statement in _statements(text):
        tokens = statement.tokens
        if not tokens:
            continue
        raw = text[statement.start:statement.end]
        words = tokens[1:] if tokens[0].is_code("export") else tokens
        match = _ASSIGNMENT.match(words[0].raw) if len(words) == 1 else None
        if (
            match and match[1] in names and words[0].literal
            and not (stack or pending_function or continuation or uncertain or statement.data)
            and "\n" not in raw.removesuffix("\n")
            and "\x00" not in raw
        ):
            name = match[1]
            value = words[0].value[len(name) + 1:]
            start = len(text[:statement.start].encode("utf-8"))
            end = start + len(raw.encode("utf-8"))
            assignments.append(ShellAssignment(name, value, start, end))
            if name in values and values[name] != value:
                issues.append(ShellIssue((name,), "ambiguous_shell", statement.line))
            values[name] = value
            continuation = False
            continue
        written: set[str] = set()
        arithmetic = "))" in stack or any(token.raw == "((" for token in tokens)
        segments = _segments(tokens)
        index = 0
        while index < len(segments):
            segment = segments[index]
            first = segment[0].raw
            written.update(_command_writers(segment, names, 0))
            if statement.data:
                index += 1
                continue
            if stack and stack[-1] == "]]":
                if any(token.raw == "]]" for token in segment):
                    stack.pop()
                index += 1
                continue
            if (
                len(segment) == 1 and re.fullmatch(_NAME, first)
                and index + 2 < len(segments)
                and [part[0].raw for part in segments[index + 1:index + 3]] == ["(", ")"]
            ):
                pending_function = True
                index += 3
                continue
            if first == "function":
                pending_function = True
            elif first in {"coproc", "repeat"}:
                # These shell-specific compound forms have no single portable
                # terminator. Their following assignments are unsupported.
                uncertain = True
            elif first in {"if", "for", "select", "while", "until", "case", "foreach", "{", "(", "((", "[["}:
                closer = {
                    "if": "fi", "for": "done", "select": "done", "while": "done",
                    "until": "done", "case": "esac", "foreach": "end",
                    "{": "}", "(": ")", "((": "))", "[[": "]]",
                }[first]
                stack.append(closer)
                if first == "[[" and any(token.raw == "]]" for token in segment[1:]):
                    stack.pop()
                pending_function = False
            elif first in {"fi", "done", "esac", "end", "}", ")", "))", "]]"}:
                if stack and stack[-1] == first:
                    stack.pop()
                elif first != ")" or not stack or stack[-1] != "esac":
                    uncertain = True
            index += 1
        if arithmetic:
            written.update(_arithmetic_writers(raw, names))
        if written:
            issues.append(ShellIssue(tuple(sorted(written)), "dynamic_shell", statement.line))
        if not statement.data:
            continuation = tokens[-1].raw in {"&&", "||", "|", "|&", "\\"}
            # Multiline expansions can contain arbitrary nested shell grammar.
            # Never resume accepting literals on a guessed closing delimiter.
            if any("\n" in token.raw and token.expansions for token in tokens):
                uncertain = True
    return ShellProfile(path, before, tuple(assignments), tuple(issues))


def read_shell_profiles(home: Path | None, names: frozenset[str]) -> tuple[ShellProfile, ...]:
    """Read only the eight fixed paths, retaining absent paths as consent guards."""
    from .migration_journal import TakeoverStateError, _read_regular

    root = Path.home() if home is None else home
    profiles: list[ShellProfile] = []
    for name in SHELL_PROFILE_NAMES:
        path = (root / name).absolute()
        try:
            before = _read_regular(path)
            profile = _parse(path, before, names) if before is not None else ShellProfile(path, None, (), ())
        except (OSError, UnicodeError, TakeoverStateError):
            profile = ShellProfile(path, None, (), (ShellIssue(tuple(sorted(names)), "unreadable", None),))
        profiles.append(profile)
    return tuple(profiles)


def cleanup_shell_profile(profile: ShellProfile, selected: Mapping[str, str]) -> NativeFileEdit:
    """Plan exact consented line removals, without rereading or writing a file."""
    from .migration_journal import NativeFileEdit, TakeoverStateError

    if any(set(issue.names).intersection(selected) for issue in profile.issues):
        raise TakeoverStateError("shell configuration requires manual migration")
    removals: list[ShellAssignment] = []
    for assignment in profile.assignments:
        if assignment.name not in selected:
            continue
        if assignment.value != selected[assignment.name]:
            raise TakeoverStateError("shell configuration value changed")
        removals.append(assignment)
    after = profile.before
    for assignment in sorted(removals, key=lambda item: item.start, reverse=True):
        if after is None or not 0 <= assignment.start < assignment.end <= len(after):
            raise TakeoverStateError("invalid shell configuration snapshot")
        after = after[:assignment.start] + after[assignment.end:]
    return NativeFileEdit(profile.path, profile.before, after)
