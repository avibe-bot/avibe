"""Pi's number and size formatting (JavaScript's ``toFixed`` and ``String(number)``)."""

from __future__ import annotations

import pytest

from core.agent_core.tools.args import format_number
from core.agent_core.tools.truncate import format_size


# Expected values are what Pi's formatSize prints under Node.
@pytest.mark.parametrize(
    ("size", "expected"),
    [(1023, "1023B"), (1280, "1.3KB"), (52_480, "51.3KB"), (1_205_863, "1.2MB")],
)
def test_sizes_round_half_up_as_to_fixed_does(size, expected):
    assert format_size(size) == expected


# Expected values are JavaScript's String(number).
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, "5"),
        (0.5, "0.5"),
        (2147483.647, "2147483.647"),
        (0.00001, "0.00001"),
        (1e-7, "1e-7"),
        (1e21, "1e+21"),
        (123456789012345680000.0, "123456789012345680000"),
    ],
)
def test_numbers_print_as_javascript_prints_them(value, expected):
    assert format_number(value) == expected
