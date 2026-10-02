"""Argument checks and result helpers shared by the coding tools."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from core.agent_core.messages import TextBlock
from core.agent_core.tools.base import ToolResult


class ToolInputError(ValueError):
    """Arguments the tool cannot act on; the message becomes the error result."""


def text_result(text: str, *, is_error: bool = False, details: Optional[Mapping[str, Any]] = None) -> ToolResult:
    return ToolResult(content=(TextBlock(text=text),), is_error=is_error, details=dict(details or {}))


def error_result(text: str, *, details: Optional[Mapping[str, Any]] = None) -> ToolResult:
    return text_result(text, is_error=True, details=details)


def str_arg(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string")
    return value


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
    """``5`` for 5.0 and ``0.5`` for 0.5, as JavaScript prints numbers in Pi's messages."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def os_error_text(error: OSError) -> str:
    return str(error) or type(error).__name__
