"""Argument checks and result helpers shared by the coding tools."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping, Optional

from core.agent_core.messages import TextBlock
from core.agent_core.tools.base import ToolResult
from core.agent_core.tools.text import model_text


class ToolInputError(ValueError):
    """Arguments the tool cannot act on; the message becomes the error result."""


def text_result(text: str, *, is_error: bool = False, details: Optional[Mapping[str, Any]] = None) -> ToolResult:
    return ToolResult(content=(TextBlock(text=text),), is_error=is_error, details=dict(details or {}))


def error_result(text: str, *, details: Optional[Mapping[str, Any]] = None) -> ToolResult:
    return text_result(text, is_error=True, details=details)


def str_arg(arguments: Mapping[str, Any], name: str) -> str:
    """A string argument, through the one sanitizer for model text (a lone surrogate becomes U+FFFD)."""
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string")
    return model_text(value)


def optional_number_arg(arguments: Mapping[str, Any], name: str) -> Optional[float]:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"{name} must be a number")
    try:
        number = float(value)  # an integer too large for a float overflows here
    except OverflowError:
        raise ToolInputError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ToolInputError(f"{name} must be a finite number")
    return number


def optional_line_arg(arguments: Mapping[str, Any], name: str) -> Optional[int]:
    """A JSON integer of at least 1 (an integral float such as ``3.0`` is accepted)."""
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolInputError(f"{name} must be an integer of at least 1")
    return value


def optional_bool_arg(arguments: Mapping[str, Any], name: str) -> bool:
    value = arguments.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ToolInputError(f"{name} must be a boolean")
    return value


def format_number(value: float) -> str:
    """JavaScript's ``String(number)``, as Pi prints numbers in its messages: ``5``, ``0.5``, ``0.00001``, ``1e-7``."""
    value = float(value)
    if value == 0 or not math.isfinite(value):
        return {0.0: "0", math.inf: "Infinity", -math.inf: "-Infinity"}.get(value, "NaN")
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, as JavaScript does; only the layout differs.
    _, digit_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(map(str, digit_tuple))
    point = exponent + len(digits)  # value = 0.<digits> * 10**point
    if len(digits) <= point <= 21:
        return sign + digits + "0" * (point - len(digits))
    if 0 < point <= 21:
        return f"{sign}{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    mantissa = digits[0] + (f".{digits[1:]}" if len(digits) > 1 else "")
    return f"{sign}{mantissa}e{'+' if point - 1 >= 0 else '-'}{abs(point - 1)}"
