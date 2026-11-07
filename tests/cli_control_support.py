"""What ``in_process_cli_control`` lends its stand-in controller.

A test that needs a particular store (a file-backed one, or a stub) or a live
Session owner sets it here, usually with ``patch.object(lent, "task_store", store)``.
``None`` builds the store a real controller owns.
"""

from types import SimpleNamespace

lent = SimpleNamespace(
    task_store=None,
    watch_store=None,
    watch_runtime_store=None,
    session_turns=None,
)
