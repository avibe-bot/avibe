"""Compatibility import for the canonical artifact-local Memory scrubber."""

from core.memory.secret_scrubber import (  # noqa: F401
    _scrub,
    install_error_scrubbers,
    scrub_from_environment,
    scrub_text,
)
