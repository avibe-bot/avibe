"""Waiter exit protocol shared by the supervisor, lifecycle projections and CLI."""

DEFAULT_RETRY_EXIT_CODE = 75
# 64 is also EX_USAGE: it is a quiet cycle only together with the marker.
NO_EVENT_EXIT_CODE = 64
NO_EVENT_MARKER = "avibe-watch: no-event"
