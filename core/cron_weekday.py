"""Detect cron weekday fields whose meaning differs between APScheduler and crontab(5).

Avibe parses cron with ``CronTrigger.from_crontab``, which numbers weekdays
0=Mon through 6=Sun. crontab(5) numbers them 0=Sun through 6=Sat, so the same
digit names a different day. Weekday names (``mon`` ... ``sun``) mean the same
day under both conventions, and a step after a named range (``mon-fri/2``) is a
count rather than a day, so only digits in a day position are ambiguous.
"""

from __future__ import annotations

import re
from typing import Optional

_APSCHEDULER_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CRONTAB_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DIGITS = re.compile(r"\d+")


def ambiguous_weekday_field(cron: str) -> Optional[str]:
    """Return the day-of-week field when it names days by number, else ``None``."""

    fields = cron.split()
    if len(fields) != 5:
        return None
    weekday = fields[4]
    for item in weekday.split(","):
        base, _, step = item.partition("/")
        if any(char.isdigit() for char in base):
            return weekday
        # ``*/2`` counts from day 0, which is Monday here and Sunday in crontab(5).
        if base == "*" and step:
            return weekday
    return None


def _rename(field: str, names: tuple[str, ...]) -> Optional[str]:
    if "*" in field:
        return None
    try:
        return _DIGITS.sub(lambda match: names[int(match.group())], field)
    except IndexError:
        return None


def weekday_readings(field: str) -> tuple[Optional[str], Optional[str]]:
    """``(apscheduler_reading, crontab_reading)`` of a numeric field, in weekday names."""

    return _rename(field, _APSCHEDULER_NAMES), _rename(field, _CRONTAB_NAMES)
