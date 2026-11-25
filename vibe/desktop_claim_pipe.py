"""Single-conversation transport for the native Desktop child. Neither stream is a log.

Only the parent gets the output descriptor. Each message is one bounded UTF-8 JSON
object followed by LF; tags, phase and fields are closed. No operation takes a path,
issuer, verifier, cookie, OTP or pairing key. One child owns at most one claim.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import queue
import secrets
import stat
import threading
import time

from core.caller_context import CALLER_CONTEXT_ENV_NAMES
from vibe import desktop_bound_observation, desktop_legacy_observation
from vibe.authorization import current_invocation_authority
from vibe.desktop_claim_admission import AdmissionUnavailable, HelperPermit, literal_origin
from vibe.personal_claim_client import ClaimError, text
from vibe.personal_claims import DesktopClaim

_FRAME = 4096
_OPEN_SECONDS = 15
_START_SECONDS = 45
# Even a nonsensical expiry from an issuer cannot leave an abandoned child running indefinitely.
_SESSION_SECONDS = 30 * 60
_ERRORS = frozenset(
    {
        "already_bound",
        "binding_unavailable",
        "claim_in_progress",
        "claim_unavailable",
        "control_plane_unreachable",
        "decision_changed",
        "expired",
        "fresh_claim_required",
        "invalid_backend",
        "invalid_claim_response",
        "invalid_device_name",
        "local_binding_changed",
        "local_binding_unavailable",
        "local_claim_unavailable",
        "local_instance_conflict",
        "local_runtime_upgrade_required",
        "name_taken",
        "pairing_recovery_required",
        "rate_limited",
        "subject_mismatch",
    }
)


class _Closed(Exception):
    pass


def _object(pairs):
    obj = dict(pairs)
    if len(pairs) != len(obj):
        raise _Closed()
    return obj


def _read_commands(fd, events):
    # The private fd is owned by this daemon thread. Never leave a reader blocked
    # on sys.stdin's buffered lock at interpreter shutdown. At most five legal
    # commands (open, begin, arm, observe_publication, recheck) on the longest path.
    try:
        with os.fdopen(fd, "rb") as stream:
            for _ in range(5):
                line = stream.readline(_FRAME + 1)
                if len(line) > _FRAME or not line.endswith(b"\n"):
                    break
                cmd = json.loads(line.decode("utf-8"), object_pairs_hook=_object)
                if not isinstance(cmd, dict) or type(cmd.get("v")) is not int or cmd.pop("v") != 1:
                    break
                events.put(("input", cmd))
                if cmd.get("op") == "close":
                    return
    except (OSError, ValueError, RecursionError, _Closed):
        pass
    events.put(("closed", None))


def _send(fd, event, **fields):
    data = json.dumps({"v": 1, "event": event, **fields}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(data) > _FRAME:
        raise _Closed()
    # Raw pipe writes may block (also on Windows) if the parent stops reading.
    # Only a private duplicate can outlive this two-second wait. The process is
    # single-purpose; no next conversation shares its descriptors or threads.
    result = queue.Queue(maxsize=1)
    owned = os.dup(fd)

    def write():
        try:
            with os.fdopen(owned, "wb", buffering=0) as stream:
                remaining = memoryview(data)
                while remaining:
                    count = stream.write(remaining)
                    if not count:
                        raise OSError()
                    remaining = remaining[count:]
            result.put(True)
        except OSError:
            result.put(False)

    threading.Thread(target=write, daemon=True).start()
    if not result.get(timeout=2):
        raise _Closed()


def _result(fd, view):
    # A completed local publication is not yet a fresh hosted Owner check.
    if view.get("state") == "bound":
        _send(fd, "result", outcome="publication_finished")
    elif view.get("state") == "declined":
        _send(fd, "result", outcome="declined")
    else:
        code = view.get("code")
        _send(
            fd,
            "result",
            outcome="error",
            code=code if isinstance(code, str) and code in _ERRORS else "local_claim_unavailable",
        )


def _observe(fd, root, root_id, command, deadline, publication=None):
    with contextlib.ExitStack() as pins:

        def sample(*, initial=False):
            return desktop_bound_observation.sample(
                root, root_id, pins, publication=publication, inspect_pending=initial and publication is None
            )

        try:
            first = sample(initial=True)
        except ClaimError as exc:
            if publication is not None or exc.code not in desktop_legacy_observation.CODES:
                raise
            if time.monotonic() >= deadline:
                raise _Closed()
            # This pipe ends here. No generation, credential, new claim or recheck.
            _send(fd, "result", outcome="error", code=exc.code)
            return 0
        if time.monotonic() >= deadline:
            raise _Closed()
        if first.instance_id is None:
            if publication is not None:
                raise ClaimError("local_binding_changed")
            _send(fd, "no_binding")
            return 0
        generation = secrets.token_hex(32)
        if publication is None:
            _send(fd, "binding", instance_id=first.instance_id, generation=generation)
        else:
            _send(
                fd, "publication", claim_id=publication.claim_id, instance_id=first.instance_id, generation=generation
            )
        recheck = command("recheck", "generation", "challenge")
        token, challenge = recheck["generation"], recheck["challenge"]
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not hmac.compare_digest(generation, token)
            or not isinstance(challenge, str)
            or len(challenge) != 32
            or any(c not in "0123456789abcdef" for c in challenge)
        ):
            raise _Closed()
        if sample() != first:
            raise ClaimError("local_binding_changed")
        if time.monotonic() >= deadline:
            raise _Closed()
        _send(fd, "unchanged", generation=generation, challenge=challenge)
        return 0


def _conversation(in_fd, out_fd):
    events = queue.Queue()
    threading.Thread(target=_read_commands, args=(in_fd, events), daemon=True).start()
    deadline = time.monotonic() + _OPEN_SECONDS
    permit = flow = worker = None

    def next_event():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Closed()
        tag, value = events.get(timeout=remaining)
        if tag == "closed" or (tag == "input" and value.get("op") == "close"):
            raise _Closed()
        return tag, value

    def command(op, *fields):
        tag, cmd = next_event()
        if tag != "input" or set(cmd) != {"op", *fields} or cmd["op"] != op:
            raise _Closed()
        return cmd

    try:
        tag, hello = next_event()
        inspect = tag == "input" and hello.get("op") == "inspect"
        if tag != "input" or not literal_origin(hello.get("origin")):
            raise _Closed()
        if inspect:
            if set(hello) != {"op", "origin"}:
                raise _Closed()
        elif (
            set(hello) != {"op", "origin", "subject", "locale"}
            or hello["op"] != "open"
            or not text(hello["subject"], 200)
            or hello["locale"] not in ("en", "zh")
        ):
            raise _Closed()
        permit = HelperPermit.register(hello["origin"])
        deadline = time.monotonic() + _OPEN_SECONDS
        _send(out_fd, "challenge", challenge=permit.challenge)
        begin = command("begin", "challenge")["challenge"]
        if (
            not isinstance(begin, str)
            or not begin.isascii()
            or not hmac.compare_digest(begin, permit.challenge)
            or not permit.consume()
        ):
            raise _Closed()
        home = permit.home
        permit = None
        if inspect:
            # Irreversibly read-only on this pipe. Never start a DesktopClaim, even
            # on a clean root; a caller cannot supply the ID or the binding digest.
            deadline = time.monotonic() + _START_SECONDS
            return _observe(out_fd, home.path, home.identity, command, deadline)
        flow = DesktopClaim(
            home=home,
            signed_in_subject=hello["subject"],
            locale=hello["locale"],
            publish_ticket=lambda ticket: events.put(("ticket", ticket)),
        )

        def run():
            try:
                view = flow.run()
            except Exception:
                view = {}
            events.put(("result", view))

        worker = threading.Thread(target=run, daemon=True)
        deadline = time.monotonic() + _START_SECONDS
        worker.start()
        tag, value = next_event()
        if tag == "result":
            _result(out_fd, value)
            return 0
        if tag != "ticket":
            raise _Closed()
        deadline = time.monotonic() + min(_SESSION_SECONDS, max(0, flow.ticket.deadline - time.time()))
        _send(
            out_fd,
            "ticket",
            **{
                k: value[k]
                for k in (
                    "claim_id",
                    "user_code",
                    "user_url",
                    "mode",
                    "device_name",
                    "expires_at",
                    "ordinary_decision_readback",
                )
            },
        )
        arm = command("arm", "claim_id", "kind", "hostname", "existing_instance_id")
        flow.arm(**{k: v for k, v in arm.items() if k != "op"})
        tag, value = next_event()
        if tag != "result":
            raise _Closed()
        _result(out_fd, value)
        if value.get("state") == "bound":
            # Old readers close after the notification. Only this armed pipe
            # can ask about its own operation; no caller-supplied correlation.
            deadline = min(deadline, time.monotonic() + _START_SECONDS)
            command("observe_publication")
            return _observe(out_fd, home.path, home.identity, command, deadline, flow.take_publication())
        return 0
    except ClaimError as exc:
        _result(out_fd, {"code": exc.code})
        return 1
    except (_Closed, AdmissionUnavailable, queue.Empty, OSError):
        return 1
    finally:
        if permit is not None:
            with contextlib.suppress(AdmissionUnavailable):
                permit.close()
        if flow is not None and worker is not None and worker.is_alive():
            try:
                flow.cancel()
            except ClaimError:
                # Acceptance already crossed the durable marker. Do not undo or
                # reattribute it; let the exact publication finish or recover.
                worker.join(max(0, min(_SESSION_SECONDS, deadline - time.monotonic())))
            else:
                worker.join(0.25)  # best effort decline; process death also releases the machine lock


def run_private_stdio() -> int:
    if current_invocation_authority() is not None or CALLER_CONTEXT_ENV_NAMES.intersection(os.environ):
        return 1
    if not all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (0, 1)):
        return 1
    # No ordinary print, traceback, library warning or child process inherits the
    # protocol output. Rust must also discard launcher stderr (including imports).
    logging.disable(logging.CRITICAL)  # this dedicated child has no diagnostic transcript, including file handlers
    source, sink = os.dup(0), os.dup(1)
    with open(os.devnull, "r+b", buffering=0) as null:
        for fd in (0, 1, 2):
            os.dup2(null.fileno(), fd)
    try:
        if os.name == "nt":
            import msvcrt

            for fd in (source, sink):
                msvcrt.setmode(fd, os.O_BINARY)
        return _conversation(source, sink)
    except Exception:
        return 1  # protocol errors and underlying exceptions never become a transcript
    finally:
        os.close(sink)
