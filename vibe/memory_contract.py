"""Lightweight contracts shared by Memory's host-facing HTTP boundary."""


class MemoryStoreUnavailableError(RuntimeError):
    """The optional Memory store cannot currently serve a host request."""


class MemoryRuntimeCloseUnprovedError(RuntimeError):
    """Controller ownership remains fenced because runtime close was unproved."""
