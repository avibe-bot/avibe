"""Pids that stand for no process in a test.

A small literal such as 1234 reads whatever process holds that pid on the
machine running the test: a desktop start that asked psutil for pid 1234's
environment was refused on the one CI runner where a root process held it.
Linux keeps every pid below 2**22 (PID_MAX_LIMIT) and macOS below 99999, so no
process, the test's own included, can hold a pid from ``fake_pid``: psutil
raises ``NoSuchProcess`` for it and every signal raises ``ProcessLookupError``,
on any Linux or macOS machine.
"""

PID_LIMIT = 2**22


def fake_pid(index: int = 0) -> int:
    """The ``index``-th pid no process can hold; distinct indexes give distinct pids.

    On Linux and macOS no process can hold the value, so the kernel answers "no
    such process". Windows is out of scope: its pids have no ceiling below this,
    and the guard is POSIX-only and these tests do not run there.
    """

    return PID_LIMIT + 1 + index
