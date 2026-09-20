"""avibe - local-first Agent OS runtime."""

#: The version a build-less tree reports: a source checkout, an editable
#: install, or a regression environment built with a pretend version. It names no
#: published release, so update comparisons must treat it as "unknown" rather
#: than as an index-served release.
UNKNOWN_VERSION = "0.0.0.dev0"

try:
    from vibe._version import __version__
except ImportError:
    __version__ = UNKNOWN_VERSION  # Fallback for editable installs without build
