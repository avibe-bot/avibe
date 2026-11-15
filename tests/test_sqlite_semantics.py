import json
import sqlite3

import pytest

from storage.sqlite_semantics import sqlite_cast_integer


@pytest.mark.parametrize(
    "raw",
    [
        "9" * 5000,
        "-" + "9" * 5000,
        "0" * 5000 + "3",
        "-" + "0" * 5000 + "3",
        "0" * 5000,
        " \t+" + "9" * 5000 + "suffix",
        "9223372036854775807",
        "9223372036854775808",
        "-9223372036854775808",
        "-9223372036854775809",
    ],
    ids=[
        "HFR-076-positive-overflow",
        "negative-overflow",
        "leading-zeroes",
        "negative-leading-zeroes",
        "all-zeroes",
        "signed-prefix-with-suffix",
        "int64-max",
        "above-int64-max",
        "int64-min",
        "below-int64-min",
    ],
)
def test_sqlite_cast_integer_long_numeric_prefix_matches_sqlite(raw: str) -> None:
    with sqlite3.connect(":memory:") as connection:
        expected: int = connection.execute(
            "SELECT CAST(json_extract(?, '$') AS INTEGER)", (json.dumps(raw),)
        ).fetchone()[0]

    assert sqlite_cast_integer(raw) == expected
