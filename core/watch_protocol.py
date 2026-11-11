"""Waiter protocol shared by the supervisor, lifecycle projections, the CLI and the Runtime spawn."""

DEFAULT_RETRY_EXIT_CODE = 75
# 64 is also EX_USAGE: it is a quiet cycle only together with the marker.
NO_EVENT_EXIT_CODE = 64
NO_EVENT_MARKER = "avibe-watch: no-event"

# The watch id, handed to every waiter through its environment. Waiters that keep
# per-watch state on disk use it as the owner of that state, so two identically
# configured watches cannot silently share one file.
WATCH_ID_ENV = "AVIBE_WATCH_ID"
# How many event reports of this watch have been durably queued as a follow-up, or
# empty for none. A waiter cannot see delivery for itself -- its output only reaches
# the service once the process has exited -- so a waiter that stages the cursors
# covering a reported event records this value alongside them, and a later cycle that
# reads a DIFFERENT value knows the report was delivered and the staged cursors are
# safe to promote. The value is opaque to the waiter, which only ever compares it.
LAST_DELIVERY_ENV = "AVIBE_WATCH_LAST_DELIVERY"
# Both describe one waiter cycle. A process Avibe owns must not inherit them from a
# waiter that started it: the service would hand them to every Agent shell, and each
# would take itself for that Watch.
WATCH_CYCLE_ENV_NAMES = frozenset({WATCH_ID_ENV, LAST_DELIVERY_ENV})
