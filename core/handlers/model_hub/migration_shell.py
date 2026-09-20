"""Inventory literal shell assignments without interpreting a startup script.

Only complete, top-level assignment lines are removable. This is an inventory
of stored values, not a model of shell execution or effective precedence.
Includes and ordinary commands are not followed. The small lexer exists to
keep quoted text, compound commands and here-documents out of the literal path.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
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
_OPERATORS = ("<<<", "<<-", "&&", "||", ";;", ";&", "<<", ">>", "<&", ">&", "<>", ">|", "|&", "((", "))")
_SEPARATORS = {";", ";;", ";&", "&", "&&", "||", "|", "|&", "(", ")", "((", "))"}
_DECLARATIONS = {"export", "readonly", "declare", "typeset", "local"}
_ZSH_DECLARATIONS = {
    # Flags, optional numeric options, implicit numeric attribute. The aliases
    # share bin_typeset, but not its entire option alphabet.
    "typeset": ("AHTUafghklmrtuxz", "EFLRZip", ""),
    "declare": ("AHTUafghklmrtuxz", "EFLRZip", ""),
    "local": ("AHTUahlrtux", "EFLRZip", ""),
    "export": ("HTUafhlrtu", "EFLRZip", ""),
    "readonly": ("AHTUafghlptux", "EFLRZi", ""),
    "integer": ("Hghlrtux", "LRZip", "i"),
    "float": ("Hghlrtux", "EFLRZp", "E"),
}
_COMMAND_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!", "time", "always"}
_ZSH_PROFILES = frozenset({".zshenv", ".zprofile", ".zshrc", ".zlogin"})
_DYNAMIC_STARTS = "$`~{*?["
# Src/options.c zshletters, plus bin_set's own -s sorting option. -A/-o
# consume values; -b/-c are not ordinary set options.
_ZSH_OPTION_FLAGS = "0123456789BCDEFGHIJKLMNOPQRSTUVWXYZadefghiklmnprstuvwxy"


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
    mode: int = 0o600

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
    fd_name: str | None = None

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
            fd_name = None
            if char in "<>" and tokens and text[index - 1:index] == "}":
                match = re.fullmatch(rf"\{{({_NAME})\}}", tokens[-1].raw)
                if match:
                    fd_name = match[1]
                    tokens.pop()
            if (
                char in "<>" and tokens and tokens[-1].raw.isascii()
                and tokens[-1].raw.isdigit() and text[index - 1:index].isdigit()
            ):
                # An adjacent IO_NUMBER is part of the redirection, not a
                # command word. Quoted numbers and whitespace do not qualify.
                tokens.pop()
            operator = next((op for op in _OPERATORS if text.startswith(op, index)), char)
            tokens.append(_Token(operator, operator, operator=True, fd_name=fd_name))
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
            if current and current[0].is_code("coproc") and token.raw in {"(", "(("}:
                current.append(token)  # Retain the named compound introducer.
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


class _WrittenCode:
    """One finite, deduplicated owner for written code, expansion text and argv.

    A child is a written operand/subexpression, never expansion output, an
    included file or a resolved function. No recursion/analysis cutoff turns
    recognized code into safe data. Role and local dialect are part of identity.
    """

    def __init__(self, names: frozenset[str]):
        self.names = names
        self.found: set[str] = set()
        self.pending: list[tuple[str, str | tuple[_Token, ...], str]] = []
        self.seen: set[tuple[str, str | tuple[_Token, ...], str]] = set()

    def add(self, role: str, body: str | tuple[_Token, ...], dialect: str) -> None:
        key = (role, body, dialect)
        if body and key not in self.seen:
            self.seen.add(key)
            self.pending.append(key)

    def embedded(self, token: _Token, dialect: str) -> None:
        for kind, body in token.expansions:
            if kind == "parameter":
                match = re.match(rf"^({_NAME}):?=", body)
                if match:
                    self.found.update({match[1]} & self.names)
                self.add("expansion", body, dialect)
            elif kind == "arithmetic":
                self.found.update(_arithmetic_writers(body, self.names))
            else:
                self.add("source", body, dialect)

    def run(self) -> set[str]:
        while self.pending and not self.found >= self.names:
            self.visit(*self.pending.pop())
        return self.found

    def visit(self, role: str, body: str | tuple[_Token, ...], dialect: str) -> None:
        if role == "argv":
            _argv_writers(list(body), self, dialect)
            return
        for statement in _statements(body):
            if role == "expansion":
                for token in statement.tokens:
                    self.embedded(token, dialect)
                continue
            for segment in _segments(statement.tokens):
                _command_writers(segment, self, dialect)
            if any(token.raw == "((" for token in statement.tokens):
                self.found.update(_arithmetic_writers(body[statement.start:statement.end], self.names))


@dataclass(frozen=True, repr=False)
class _UnknownOptions:
    options: dict[str, _Token | None]
    remaining: list[_Token]


def _options(
    arguments: list[_Token],
    flags: str = "",
    required: str = "",
    *,
    numeric: str = "",
    plus: bool = False,
    long: Mapping[str, tuple[str, str]] | None = None,
    stop_after: str = "",
    stop_unless: str = "",
) -> tuple[dict[str, _Token | None], list[_Token]] | _UnknownOptions | None:
    """Decode argument roles, not values; the last occurrence of an option wins.

    A required short argument may be attached, separate, or end a bundle.
    Zsh's optional numeric arguments consume only a digit-leading word/suffix.
    Long optional arguments are attached-only. Stop at the first operand/--.
    Invalid literal syntax cannot invoke the builtin; a dynamic word in option
    position instead leaves its role unknown. Required option arguments and
    words after an operand/-- never pass through that uncertainty boundary.
    """
    options: dict[str, _Token | None] = {}
    index = 0
    while index < len(arguments):
        token = arguments[index]
        argument = token.value
        if not token.literal and (not argument or argument[0] in _DYNAMIC_STARTS):
            return _UnknownOptions(options, arguments[index:])
        if argument == "--":
            return options, arguments[index + 1:]
        if plus and argument == "+":
            options["+"] = None
            return options, arguments[index + 1:]
        if len(argument) < 2 or argument[0] not in ("-+" if plus else "-"):
            return options, arguments[index:]
        if argument.startswith("--"):
            name, separator, value = argument[2:].partition("=")
            spec = (long or {}).get(name)
            if spec is None:
                return None if token.literal else _UnknownOptions(options, arguments[index:])
            flag, arity = spec
            operand = None
            if separator:
                if arity == "none":
                    return None
                operand = _Token(value, value, token.literal)
            elif arity == "required":
                index += 1
                if index >= len(arguments):
                    return None
                operand = arguments[index]
            options[flag] = operand
            index += 1
            if flag in stop_after and not options.keys() & set(stop_unless):
                return options, arguments[index:]
            continue
        position = 1
        while position < len(argument):
            flag = argument[position]
            position += 1
            operand = None
            if flag in required or flag in numeric:
                suffix = argument[position:]
                if suffix and (flag in required or suffix[0].isdigit()):
                    operand = _Token(suffix, suffix, token.literal)
                    position = len(argument)
                elif not suffix and index + 1 < len(arguments) and (
                    flag in required or re.match(r"[0-9]", arguments[index + 1].value)
                ):
                    index += 1
                    operand = arguments[index]
                elif flag in required:
                    return None
            elif flag not in flags:
                if not token.literal and flag in _DYNAMIC_STARTS:
                    return _UnknownOptions(options, arguments[index:])
                return None
            options.pop(flag, None)
            options.pop("+" + flag, None)
            options[("+" if argument[0] == "+" else "") + flag] = operand
            if flag in stop_after and (flag in required or flag in numeric) and not options.keys() & set(stop_unless):
                return options, arguments[index + 1:]
        index += 1
        # Zsh print changes to echo's option grammar after the complete -R
        # word, not halfway through a bundle such as -RvNAME or -Rf FORMAT.
        if options.keys() & set(stop_after) and not options.keys() & set(stop_unless):
            return options, arguments[index:]
    return options, []


def _option_cases(
    arguments: list[_Token], flags: str = "", required: str = "", *,
    numeric: str = "", plus: bool = False,
    long: Mapping[str, tuple[str, str]] | None = None, stop_after: str = "",
    stop_unless: str = "",
    writer_flags: str | tuple[str, ...] = "", state_flags: str = "", state_values: str = "",
    zero_values: str = "",
) -> Iterator[tuple[dict[str, _Token | None], list[_Token]]]:
    """Project an unknown option onto this builtin's existing argument roles.

    This is an existential safety check, not an expansion guess. An unknown
    word can end options, leave flags unchanged, enable a writer attribute, or
    consume one following word in a known option's argument role. Static data
    boundaries still terminate parsing. Retain only options the consumer
    actually inspects, not combinations of irrelevant prompts/fds/delimiters.
    Equivalent states are visited once.
    """
    def remember(options: dict[str, _Token | None], flag: str, value: _Token | None) -> None:
        base = flag.removeprefix("+") if flag != "+" else flag
        if base not in state_flags + state_values + zero_values:
            return
        options.pop(base, None)
        options.pop("+" + base, None)
        if base in zero_values:
            value = _Token("0", "0") if value is not None and re.fullmatch(r"0+(?:\.0*)?", value.value) else None
        elif base not in state_values:
            value = None
        options[flag] = value

    pending = [(arguments, {})]
    seen = set()
    while pending:
        remaining, prefix = pending.pop()
        key = (tuple(remaining), tuple(sorted(prefix.items())))
        if key in seen:
            continue
        seen.add(key)
        parsed = _options(
            remaining, flags, required, numeric=numeric, plus=plus,
            long=long, stop_after="" if prefix.keys() & set(stop_unless) else stop_after,
            stop_unless=stop_unless,
        )
        if parsed is None:
            continue
        current = parsed.options if isinstance(parsed, _UnknownOptions) else parsed[0]
        options = dict(prefix)
        for flag, value in current.items():
            remember(options, flag, value)
        if not isinstance(parsed, _UnknownOptions):
            yield options, parsed[1]
            continue
        token, *tail = parsed.remaining
        # A whole dynamic word can itself be an operand (e.g. printf's
        # format). A visibly option-prefixed word cannot provide a format.
        if not token.value.startswith(("-", "+") if plus else ("-",)):
            yield options, parsed.remaining
        yield options, tail  # The dynamic word could be --.
        pending.append((tail, options))
        if plus and not token.value.startswith("-"):
            # A dynamic +/- bundle may remove existing query attributes.
            options = {key: value for key, value in options.items() if key not in {"f", "F", "p", "+"}}
            pending.append((tail, options))
        for bundle in writer_flags:
            pending.append((tail, {**options, **dict.fromkeys(bundle)}))
        if tail:
            for flag in required + numeric:
                changed = dict(options)
                remember(changed, flag, tail[0])
                if flag in stop_after:
                    yield changed, tail[1:]
                else:
                    pending.append((tail[1:], changed))


def _destination(value: str, names: frozenset[str]) -> set[str]:
    """Inspect an actual lvalue, including explicit writes in an array index."""
    match = re.fullmatch(rf"({_NAME})(?:\[(.*)\])?", value, re.DOTALL)
    if not match:
        return set()
    found = {match[1]} & names
    if match[2] is not None:
        found.update(_arithmetic_writers(match[2], names))
    return found


def _assignment_writers(value: str, names: frozenset[str], *, arithmetic: bool = False) -> set[str]:
    match = _WRITER.match(value)
    if not match:
        return set()
    found = _destination(value[:match.end() - 1].removesuffix("+"), names)
    if arithmetic:
        found.update(_arithmetic_writers(value[match.end():], names))
    return found


def _array_reader_writers(
    arguments: list[_Token], analysis: _WrittenCode, dialect: str,
) -> set[str]:
    names = analysis.names
    found: set[str] = set()
    for options, operands in _option_cases(arguments, "t", "dnOsuCc", state_values="C"):
        found.update(_destination(operands[0].value if operands else "MAPFILE", names))
        callback = options.get("C")
        if callback is not None:
            # Only an explicit callback is code. Named functions are never followed.
            analysis.add("source", callback.value, dialect)
        if found >= names:
            break
    return found


def _read_writers(
    arguments: list[_Token], names: frozenset[str], dialect: str, *, getln: bool = False,
) -> set[str]:
    cases = (
        _option_cases(
            arguments, "ecnAlE" if getln else "rszpqAclneE", "" if getln else "du",
            numeric="" if getln else "kt", writer_flags="A", state_flags="eAkqz", state_values="t",
        )
        if dialect == "zsh" else _option_cases(
            arguments, "ersE", "adinNptu", writer_flags="t", state_values="a", zero_values="t",
        )
    )
    found: set[str] = set()
    for options, operands in cases:
        if dialect == "zsh":
            timeout = options.get("t")
            if timeout is not None:
                found.update(_arithmetic_writers(timeout.value, names))
            if "e" in options:
                continue  # Echo-only, unlike Bash's Readline -e.
            targets = [arg.value for arg in operands] or ["reply" if "A" in options else "REPLY"]
            targets[0] = targets[0].partition("?")[0] or ("reply" if "A" in options else "REPLY")
            if getln or options.keys() & {"A", "k", "q", "z"}:
                targets = targets[:1]
        else:
            timeout = options.get("t")
            if timeout is not None and re.fullmatch(r"0+(?:\.0*)?", timeout.value):
                continue  # Bash's -t 0 checks availability without assigning.
            array = options.get("a")
            targets = [array.value] if array is not None else [arg.value for arg in operands] or ["REPLY"]
        found.update(set().union(*(_destination(value, names) for value in targets)))
        if found >= names:
            break
    return found


def _declaration_writers(command: str, arguments: list[_Token], names: frozenset[str], dialect: str) -> set[str]:
    if dialect == "zsh":
        flags, numeric_options, default = _ZSH_DECLARATIONS[command]
        if default:
            arguments = [_Token("-" + default, "-" + default)] + arguments
        cases = _option_cases(
            arguments, flags, numeric=numeric_options, plus=True,
            writer_flags="iT", state_flags="fFpi+ET",
        )
    elif command in {"export", "readonly"}:
        cases = _option_cases(arguments, "afAnp", state_flags="fFp")
    else:
        cases = _option_cases(arguments, "aAfFgIilnprtux", plus=True, writer_flags="i", state_flags="fFpi+")
    found: set[str] = set()
    for options, operands in cases:
        if options.keys() & {"f", "+f"}:
            continue
        if dialect != "zsh" and options.keys() & {"F", "+F"}:
            continue
        if ("p" in options or "+" in options) and (dialect == "zsh" or command not in {"export", "readonly"}):
            continue
        numeric = "i" in options or (dialect == "zsh" and bool(options.keys() & {"E", "F"}))
        if dialect == "zsh" and numeric:
            found.update(arg.value for arg in operands if arg.value in names)
        if dialect == "zsh" and "T" in options and 2 <= len(operands) <= 3:
            for arg in operands[:2]:
                found.update(_destination(arg.value.partition("=")[0], names))
        # Attribute-only declarations are not newly stored credentials. An explicit
        # integer RHS or array subscript is an evaluation context, quoted or not.
        found.update(set().union(*(
            _assignment_writers(
                arg.value, names,
                arithmetic=numeric,
            )
            for arg in operands
        )))
        if found >= names:
            break
    return found


def _printf_slots(format: str, dialect: str) -> list[tuple[int, str]]:
    """Locate format arguments without rendering, expanding, or evaluating.

    Track *, precision, %% and format reuse so %s data is never a %n target.
    Zsh's explicit positional slots are syntax only; numeric fields are known
    arithmetic contexts in Zsh, but not in Bash/POSIX printf.
    """
    slots: list[tuple[int, str]] = []
    cursor = 0
    index = 0

    def argument(position: int, kind: str) -> int:
        nonlocal cursor
        explicit = re.match(r"([1-9][0-9]*)\$", format[position:]) if dialect == "zsh" else None
        slot = int(explicit[1]) - 1 if explicit else cursor
        if not explicit:
            cursor += 1
        slots.append((slot, kind))
        return position + len(explicit[0]) if explicit else position

    while index < len(format):
        if format[index] == "\\":
            index += 2
            continue
        if format[index] != "%":
            index += 1
            continue
        index += 1
        if index < len(format) and format[index] == "%":
            index += 1
            continue
        explicit = re.match(r"[1-9][0-9]*\$", format[index:]) if dialect == "zsh" else None
        value_position = index
        if explicit:
            index += len(explicit[0])
        while index < len(format) and format[index] in "#0- +'":
            index += 1
        for precision in (False, True):
            if precision:
                if index >= len(format) or format[index] != ".":
                    break
                index += 1
            if index < len(format) and format[index] == "*":
                index = argument(index + 1, "number")
            else:
                while index < len(format) and format[index].isdigit():
                    index += 1
        while index < len(format) and format[index] in "hjlLtz":
            index += 1
        if index >= len(format):
            break
        conversion = format[index]
        time_format = False
        if conversion == "(" and dialect != "zsh":
            end = _group_end(format, index, "(", ")")
            if format[end:end + 1] != "T":
                break
            index = end
            conversion = "T"
            time_format = True
        if conversion not in "csbqQndiouxXeEfFgGaA" and not time_format:
            break
        kind = "destination" if conversion == "n" else (
            "number" if conversion in "diouxXeEfFgGaAT" else "data"
        )
        argument(value_position if explicit else index, kind)
        index += 1
    return slots


def _printf_writers(arguments: list[_Token], names: frozenset[str], dialect: str) -> set[str]:
    found: set[str] = set()
    for options, operands in _option_cases(arguments, required="v", state_values="v"):
        found.update(_printf_operand_writers(options, operands, names, dialect))
        if found >= names:
            break
    return found


def _printf_operand_writers(
    options: dict[str, _Token | None], operands: list[_Token], names: frozenset[str], dialect: str,
) -> set[str]:
    if not operands:
        return set()
    destination = options.get("v")
    found = _destination(destination.value, names) if destination is not None else set()
    format, values = operands[0], operands[1:]
    if not format.literal:
        # A dynamic format might use %n or an existing Zsh numeric slot.
        # Inspect explicit writers only, never infer the format's value.
        found.update(arg.value for arg in values if arg.value in names)
        if dialect == "zsh":
            for arg in values:
                if arg.value[:1] not in {"'", '"'}:
                    found.update(_arithmetic_writers(arg.value, names))
        return found
    slots = _printf_slots(format.value, dialect)
    stride = max((slot + 1 for slot, _ in slots), default=0)
    if not stride:
        return found
    for start in range(0, len(values), stride):
        for slot, kind in slots:
            if start + slot >= len(values):
                continue
            value = values[start + slot].value
            if kind == "destination" and value in names:
                found.add(value)
            elif kind == "number" and dialect == "zsh" and value[:1] not in {"'", '"'}:
                found.update(_arithmetic_writers(value, names))
    return found


_ENV_LONG_OPTIONS = {
    "ignore-environment": ("i", "none"), "null": ("0", "none"), "debug": ("v", "none"),
    "unset": ("u", "required"), "chdir": ("C", "required"),
    "split-string": ("S", "required"), "argv0": ("a", "required"),
    "quoting-style": ("quoting", "required"),
    "block-signal": ("block", "optional"), "default-signal": ("default", "optional"),
    "ignore-signal": ("ignore", "optional"), "list-signal-handling": ("list", "none"),
}


def _env_split(text: str) -> list[_Token] | None:
    """The literal argv subset of GNU env -S, not shell tokenization.

    In particular, ; is data, # starts a comment only at a word boundary,
    and \\_ is an argument separator (a space inside double quotes). Preserve
    ${NAME} symbolically: it is never resolved from the process environment.
    """
    words: list[_Token] = []
    word: list[str] = []
    started = False
    literal = True
    quote: str | None = None
    index = 0

    def finish() -> None:
        nonlocal started, literal
        if started:
            value = "".join(word)
            words.append(_Token(value, value, literal))
        word.clear()
        started, literal = False, True

    while index < len(text):
        char = text[index]
        index += 1
        if char in "'\"" and (quote is None or char == quote):
            quote = char if quote is None else None
            started = True
            continue
        if quote is None and char in " \t\n\v\f\r":
            finish()
            continue
        if char == "#" and not started:
            break
        if char == "\\" and (quote != "'" or text[index:index + 1] in {"\\", "'"}):
            if index == len(text):
                return None
            char = text[index]
            index += 1
            if char == "_" and quote != '"':
                finish()
                continue
            if char == "c":
                if quote == '"':
                    return None
                break
            escapes = {"_": " ", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
            if char not in escapes and char not in "\"#$'\\":
                return None
            char = escapes.get(char, char)
        elif char == "$" and quote != "'":
            expansion = re.match(rf"\{{{_NAME}\}}", text[index:])
            if expansion is None:
                return None
            char += expansion[0]
            index += len(expansion[0])
            literal = False
        word.append(char)
        started = True
    if quote is not None:
        return None
    finish()
    return words


def _env_writers(arguments: list[_Token], names: frozenset[str]) -> set[str]:
    # Each split consumes a written operand in a known/possible -S role.
    # Only the finite literal argv grammar is inspected; expansion never
    # manufactures more source, and command arguments are not shell code.
    found: set[str] = set()
    pending = [arguments]
    seen = set()
    while pending:
        arguments = pending.pop()
        key = tuple(arguments)
        if key in seen:
            continue
        seen.add(key)
        for options, operands in _option_cases(
            arguments, "iv0", "uCSa", long=_ENV_LONG_OPTIONS, stop_after="S", state_values="S",
        ):
            split = options.get("S")
            if split is not None:
                values = _env_split(split.value)
                if values is not None:
                    pending.append(values + operands)
                continue
            for token in operands:
                if token.value == "-" and not found:
                    continue  # Historical env - (empty environment).
                name, separator, _ = token.value.partition("=")
                if not separator:
                    break  # Even a dynamic command name anchors command data.
                if name in names:
                    found.add(name)
            if found >= names:
                return found
    return found


def _command_writers(tokens: list[_Token], analysis: _WrittenCode, dialect: str) -> None:
    if len(tokens) == 1 and tokens[0].operator:
        return  # A statement separator is syntax, never a command name.
    names, found = analysis.names, analysis.found
    for token in tokens:
        analysis.embedded(token, dialect)
    words: list[_Token] = []
    redirect = False
    for index, token in enumerate(tokens):
        if token.operator and token.raw not in _SEPARATORS:
            target = tokens[index + 1] if index + 1 < len(tokens) else None
            if token.fd_name and target and not target.operator and not (
                token.raw in {"<&", ">&"} and target.literal and target.value == "-"
            ):
                found.update({token.fd_name} & names)
            redirect = True
        elif redirect:
            redirect = False
        else:
            words.append(token)
    while words and words[0].raw in {"if", "then", "elif", "else", "while", "until", "do", "!"}:
        words = words[1:]
    if not words:
        return
    if words[0].raw in {"for", "select"}:
        if len(words) > 1 and words[1].value in names:
            found.add(words[1].value)
        return
    if dialect != "zsh" and words[0].is_code("coproc") and len(words) > 1:
        compound = {"{", "(", "((", "if", "while", "until", "for", "select", "case", "[["}
        named = len(words) > 2 and words[2].raw in compound and re.fullmatch(_NAME, words[1].raw)
        target = words[1].value if named else "COPROC"
        found.update({target, target + "_PID"} & names)
        words = words[2 if named else 1:]
        analysis.add("source", " ".join(word.raw for word in words), dialect)
        return
    while words:
        match = _WRITER.match(words[0].raw.replace("\\\r\n", "").replace("\\\n", ""))
        if not match:
            break
        found.update(_assignment_writers(words[0].value, names))
        words = words[1:]
    # Wrappers select argv, not fresh shell source. In particular an assignment
    # word after `command` is a command name, not a prefix assignment.
    analysis.add("argv", tuple(words), dialect)


def _argv_writers(words: list[_Token], analysis: _WrittenCode, dialect: str) -> None:
    command = words[0].value
    if command in {"command", "builtin"}:
        for options, operands in _option_cases(
            words[1:], "pvV" if command == "command" else "", state_flags="vV",
        ):
            if not options.keys() & {"v", "V"}:
                analysis.add("argv", tuple(operands), dialect)
    elif dialect == "zsh" and words[0].raw in {"noglob", "-", "exec"}:
        if command != "exec":
            analysis.add("argv", tuple(words[1:]), dialect)
        else:
            for _, operands in _option_cases(words[1:], "cl", "a"):
                analysis.add("argv", tuple(operands), dialect)
    else:
        analysis.found.update(_builtin_writers(words, analysis, dialect))


def _trap_writers(arguments: list[_Token], analysis: _WrittenCode, dialect: str) -> None:
    if dialect == "zsh":
        # Zsh has only the optional --, not Bash's query flags.
        operands = arguments[1:] if arguments and arguments[0].value == "--" else arguments
        cases = [({}, operands)]
    else:
        cases = _option_cases(arguments, "lpP", state_flags="lpP")
    for options, operands in cases:
        if not options and len(operands) >= 2 and operands[0].value not in {"", "-"}:
            analysis.add("source", operands[0].value, dialect)


def _bind_source(text: str) -> str:
    """Readline's explicit -x delimiter grammar, not ordinary binding macros."""
    text = text.lstrip()
    if not text.startswith('"'):
        return ""
    end = _quoted_end(text, 0, '"')
    tail = text[end:]
    if not tail or tail[0] not in ": \t":
        return ""
    colon = tail.startswith(":")
    tail = tail[1:].lstrip()
    if tail.startswith('"'):
        end = _quoted_end(tail, 0, '"')
        return tail[1:end - 1] if tail[end - 1:end] == '"' else ""
    return tail if colon else ""


def _completion_writers(command: str, arguments: list[_Token], analysis: _WrittenCode, dialect: str) -> set[str]:
    found: set[str] = set()
    flags = "abcdefgjksuv" + ("prDEI" if command == "complete" else "")
    required = "oAGWPSXFC" + ("V" if command == "compgen" else "")
    parsed = _options(arguments, flags, required)
    if parsed is None:
        return found
    if not isinstance(parsed, _UnknownOptions):
        options = parsed[0]
        # These options are validated before query/registration in Bash.
        if (target := options.get("V")) is not None and not re.fullmatch(_NAME, target.value):
            return found
        if (function := options.get("F")) is not None and not re.fullmatch(_NAME, function.value):
            return found
        if (action := options.get("A")) is not None and action.literal and action.value not in {
            "alias", "arrayvar", "binding", "builtin", "command", "directory", "disabled", "enabled",
            "export", "file", "function", "group", "helptopic", "hostname", "job", "keyword",
            "running", "service", "setopt", "shopt", "signal", "stopped", "user", "variable",
        }:
            return found
        if (option := options.get("o")) is not None and option.literal and option.value not in {
            "bashdefault", "default", "dirnames", "filenames", "noquote", "nosort", "nospace", "plusdirs",
        }:
            return found
    # Independent roles must not retain a Cartesian product of callback,
    # word-list, target and unrelated option values under dynamic options.
    for flag, role in (("V", "target"), ("C", "source"), ("W", "expansion")):
        if flag == "V" and command == "complete":
            continue
        for options, operands in _option_cases(
            arguments, flags, required, state_flags="prDEI", state_values=flag,
            writer_flags="D" if command == "complete" else "",
        ):
            if command == "complete" and (
                options.keys() & {"p", "r"} or not (operands or options.keys() & {"D", "E", "I"})
            ):
                continue
            if (token := options.get(flag)) is not None:
                if role == "target":
                    found.update({token.value} & analysis.names)
                else:
                    analysis.add(role, token.value, dialect)
    return found


def _jobs_writers(arguments: list[_Token], analysis: _WrittenCode, dialect: str) -> None:
    # -x is rejected if a display format was already set, but -xl is valid.
    form = False
    for token in arguments:
        if not token.literal or not token.value.startswith("-") or token.value == "--":
            break
        for flag in token.value[1:]:
            if flag == "x" and form:
                return
            form |= flag in "lpn"
    for options, operands in _option_cases(arguments, "lpnxrs", writer_flags="x", state_flags="x"):
        if "x" in options:
            analysis.add("argv", tuple(operands), dialect)


def _fc_writers(arguments: list[_Token], analysis: _WrittenCode, dialect: str) -> None:
    zsh = dialect == "zsh"
    for options, _ in _option_cases(
        arguments, "aAdDEfiIlLmnpPrRWs" if zsh else "lnrs",
        "et" if zsh else "e", state_flags="lspPRWA" if zsh else "ls", state_values="e",
    ):
        editor = options.get("e")
        if editor is not None and editor.value != "-" and not options.keys() & {"l", "s", "p", "P", "R", "W", "A"}:
            analysis.add("source", editor.value, dialect)


def _zsh_print_writers(arguments: list[_Token], names: frozenset[str]) -> set[str]:
    found: set[str] = set()
    for role in ("v", "f"):
        for options, operands in _option_cases(
            arguments, "abcDilmnNoOpPrRsSz", "CfuvxX",
            stop_after="R", stop_unless="f", state_values=role, state_flags="zsScCpuvf",
        ):
            special = options.keys() & {"z", "s", "S", "v"}
            if (
                len(special) > 1
                or (options.keys() & {"z", "s", "S"} and options.keys() & {"c", "C"})
                or (special and options.keys() & {"p", "u"})
            ):
                continue
            if (token := options.get(role)) is not None:
                if role == "v":
                    found.update(_destination(token.value, names))
                elif "S" not in options:
                    found.update(_printf_operand_writers({}, [token, *operands], names, "zsh"))
            if found >= names:
                return found
    return found


def _zsh_test_writers(arguments: list[_Token], names: frozenset[str], bracket: bool) -> set[str]:
    if bracket:
        if not arguments or arguments[-1].value != "]":
            return set()
        arguments = arguments[:-1]
    # Only -t has a numeric-evaluation operand. Binary comparisons consume
    # their two data operands (including -eq's decimal input), not shell code.
    words = [arg.value for arg in arguments]
    found: set[str] = set()
    index = 0
    nesting = 0
    primary = True
    binary = {"=", "==", "!=", "<", ">", "-eq", "-ne", "-lt", "-le", "-gt", "-ge", "-nt", "-ot", "-ef"}
    while index < len(words):
        if not primary:
            if words[index] == ")" and nesting:
                nesting -= 1
            elif words[index] in {"-a", "-o"}:
                primary = True
            else:
                return set()
            index += 1
            continue
        if index + 2 < len(words) and words[index + 1] in binary:
            index += 3
        elif words[index] in {"!", "("}:
            nesting += words[index] == "("
            index += 1
            continue
        elif re.fullmatch(r"-[abcdefghkLnpOrRsStuvwxzUGNHSV]", words[index]) and index + 1 < len(words):
            if words[index] == "-t":
                found.update(_arithmetic_writers(words[index + 1], names))
            index += 2
        else:
            index += 1
        primary = False
    return found if not primary and not nesting else set()


def _zsh_writers(command: str, arguments: list[_Token], analysis: _WrittenCode) -> set[str]:
    names = analysis.names
    found: set[str] = set()
    if command == "print":
        return _zsh_print_writers(arguments, names)
    if command == "set":
        for options, _ in _option_cases(
            arguments, _ZSH_OPTION_FLAGS, "Ao", plus=True, stop_after="A", state_values="A",
        ):
            target = options.get("A") or options.get("+A")
            if target is not None:
                found.update(_destination(target.value, names))
    elif command == "shift":
        for _, operands in _option_cases(arguments, "p"):
            if operands:
                found.update(_arithmetic_writers(operands[0].value, names))
                # The first word may be an array instead of a count. Inspect
                # written names, never look up the variable's runtime type.
                found.update(arg.value for arg in operands if arg.value in names)
    elif command in {"break", "continue", "return", "exit", "bye", "logout"}:
        if arguments and arguments[0].value == "--":
            arguments = arguments[1:]
        if len(arguments) == 1:
            found.update(_arithmetic_writers(arguments[0].value, names))
    elif command in {"test", "["}:
        found.update(_zsh_test_writers(arguments, names, command == "["))
    elif command == "emulate":
        for options, operands in _option_cases(arguments, "lLR", state_flags="lL"):
            if options.keys() & {"l", "L"} or not operands:
                continue
            for local, rest in _option_cases(
                operands[1:], _ZSH_OPTION_FLAGS, "co", state_values="c",
            ):
                if not rest and (body := local.get("c")) is not None:
                    # bin_emulate changes options then calls bin_eval; it does
                    # not replace Zsh's builtin table with the named shell's.
                    analysis.add("source", body.value, "zsh")
    elif command == "zmodload":
        for options, operands in _option_cases(
            arguments, "AFRILabcfdilmsue", "P", writer_flags=("FL",),
            state_flags="FRALleubcfpd", state_values="P",
        ):
            if (
                operands and "F" in options and options.keys() & {"l", "L"}
                and not options.keys() & {"e", "u", "b", "c", "f", "p", "A", "R"}
                and (target := options.get("P")) is not None
            ):
                found.update({target.value} & names)
    return found


def _builtin_writers(words: list[_Token], analysis: _WrittenCode, dialect: str) -> set[str]:
    names = analysis.names
    found: set[str] = set()
    command = words[0].value
    arguments = words[1:]
    if command in _DECLARATIONS or (dialect == "zsh" and command in {"integer", "float"}):
        found.update(_declaration_writers(command, arguments, names, dialect))
    elif command == "env":
        found.update(_env_writers(arguments, names))
    elif command == "read" or (dialect == "zsh" and command == "getln"):
        found.update(_read_writers(arguments, names, dialect, getln=command == "getln"))
    elif dialect != "zsh" and command in {"mapfile", "readarray"}:
        found.update(_array_reader_writers(arguments, analysis, dialect))
    elif command == "unset":
        for options, operands in _option_cases(arguments, "fmv" if dialect == "zsh" else "fnv", state_flags="f"):
            if "f" not in options:
                found.update(set().union(*(_destination(arg.value, names) for arg in operands)))
            if found >= names:
                break
    elif command == "getopts":
        for _, operands in _option_cases(arguments):
            if len(operands) >= 2:
                found.update({"OPTARG", "OPTIND", operands[1].value} & names)
            if found >= names:
                break
    elif command == "printf":
        found.update(_printf_writers(arguments, names, dialect))
    elif command == "let":
        found.update(_arithmetic_writers(" ".join(arg.value for arg in arguments), names))
    elif command == "eval":
        for _, operands in _option_cases(arguments):
            analysis.add("source", " ".join(arg.value for arg in operands), dialect)
    elif command == "trap":
        _trap_writers(arguments, analysis, dialect)
    elif command == "fc":
        _fc_writers(arguments, analysis, dialect)
    elif dialect != "zsh":
        if command == "wait":
            for options, _ in _option_cases(arguments, "fn", "p", state_values="p"):
                if (target := options.get("p")) is not None:
                    found.update(_destination(target.value, names))
        elif command == "bind":
            for options, _ in _option_cases(arguments, "lvpVPsSX", "fmqurx", state_flags="r", state_values="x"):
                if "r" not in options and (body := options.get("x")) is not None:
                    analysis.add("source", _bind_source(body.value), dialect)
        elif command in {"complete", "compgen"}:
            found.update(_completion_writers(command, arguments, analysis, dialect))
        elif command == "jobs":
            _jobs_writers(arguments, analysis, dialect)
    else:
        found.update(_zsh_writers(command, arguments, analysis))
    return found


def _written_names(text: str, names: frozenset[str], *, dialect: str = "bash") -> set[str]:
    analysis = _WrittenCode(names)
    analysis.add("source", text, dialect)
    return analysis.run()


def _parse(path: Path, before: bytes, names: frozenset[str]) -> ShellProfile:
    text = before.decode("utf-8")
    # Only fixed-path provenance selects grammar, never $SHELL or live state.
    # POSIX .profile also recognizes explicit Bash extension writers as unsafe;
    # it does not claim those extensions would execute in every POSIX shell.
    dialect = "zsh" if path.name in _ZSH_PROFILES else "posix" if path.name == ".profile" else "bash"
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
        written = _written_names(raw, names, dialect=dialect)
        arithmetic = "))" in stack or any(token.raw == "((" for token in tokens)
        segments = _segments(tokens)
        index = 0
        while index < len(segments):
            segment = segments[index]
            first = segment[0].raw
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
    from .migration_journal import NativeFileEdit, TakeoverStateError

    root = Path.home() if home is None else home
    profiles: list[ShellProfile] = []
    for name in SHELL_PROFILE_NAMES:
        path = (root / name).absolute()
        try:
            snapshot = NativeFileEdit.plan(path, None)
            before = snapshot.before
            profile = _parse(path, before, names) if before is not None else ShellProfile(path, None, (), ())
            profile = replace(profile, mode=snapshot.mode)
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
    return NativeFileEdit(
        profile.path, profile.before, after, profile.mode,
        profile.mode if profile.before is not None and profile.before != after else None,
    )
