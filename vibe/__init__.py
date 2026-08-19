"""avibe - local-first Agent OS runtime."""

#: The version a build-less tree reports: a source checkout, an editable
#: install, or a regression environment built with a pretend version. It names no
#: published release, so anything that has to INSTALL a specific version has to
#: treat it as "unknown" instead of as a target -- see
#: `vibe.upgrade.rollback_target_version`.
UNKNOWN_VERSION = "0.0.0.dev0"

try:
    from vibe._version import __version__
except ImportError:
    __version__ = UNKNOWN_VERSION  # Fallback for editable installs without build
