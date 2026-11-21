"""`vibe remote rebind`: bind this computer's remote access again, at a terminal.

This is the terminal adapter only. It reads the local binding state, explains
the browser path, and calls the two entry points the backend hands to an
interactive caller: ``binding_reset.reset_interactively`` and
``personal_claims.run_link``. Those functions own every admission check; this
module adds no authority and no alternate route to either of them.

Nothing here prints or logs an installation id, a device key, a callback
proof or the reset confirmation. The only remote values shown are the link to
open and the account and domain avibe.bot reports for the person to accept.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import sys
from textwrap import indent
from urllib.parse import urlsplit

COMMAND = "  vibe remote rebind"

TEXT = {
    "not_interactive": (
        "This command asks questions and changes which Avibe this computer is bound to.\n"
        "Run it by hand in a terminal on this computer; an SSH session works.\n"
        "It does not run from an Agent, a Task, a script or a pipe.\n"
        "Nothing changed."
    ),
    "heading": "Remote access on this computer",
    "line_domain": "  Domain:  {url}",
    "line_binding": "  Binding: {state}",
    "line_tunnel_running": "  Tunnel:  running",
    "line_tunnel_stopped": "  Tunnel:  stopped",
    "rejected_note": (
        "avibe.bot has refused this computer's binding at least twice in a row.\n"
        "Either this Avibe was deleted on avibe.bot, or its binding was replaced from\n"
        "another computer. This computer cannot tell which."
    ),
    "rejected_tunnel_note": "The tunnel is still running. That does not mean the binding is accepted.",
    "config_unreadable": (
        "Avibe could not read its settings file cleanly, and will not change the\n"
        "binding on top of that. Nothing changed.\n"
        "Run this, fix what it reports, then run vibe remote rebind again:\n"
        "  vibe doctor"
    ),
    "two_ways": "There are two ways to bind this computer again.",
    "way_browser_title": "1. Bind remote access again",
    "way_browser_body": (
        "For the Avibe this computer is bound to now. It is done in a browser that\n"
        "opens Avibe at this computer's own address, on the Devices page.\n"
        "On a server with no browser, forward the port from your own computer:\n"
        "    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>\n"
        "then open http://127.0.0.1:{port} there and go to Devices."
    ),
    "way_browser_effects": (
        "Binding again signs every remote device out. Pages open on this computer\n"
        "are not affected. It does not change the tunnel credential."
    ),
    # Product's sentences. Keep the wording: this one where binding again is
    # explained, ``copied_warning_reset`` where the reset is confirmed.
    "copied_warning": (
        "If someone may have copied this computer's Avibe data as a whole, delete this\n"
        "Avibe at avibe.bot, bind it again, then take your name back."
    ),
    "copied_warning_reset": (
        "If someone may have copied this computer's Avibe data as a whole, also delete\n"
        "the old Avibe at avibe.bot: until then, whoever holds the copy can still use it."
    ),
    "way_new_title": "2. Bind as a new computer",
    "damaged_note": (
        "The record of this computer's binding is damaged, so it cannot be bound again\n"
        "as it is. It can be bound as a new computer."
    ),
    "stale_attempt_note": (
        "The earlier attempt left a record that cannot be continued from this computer.\n"
        "Binding as a new computer clears it."
    ),
    "reset_title": "Bind as a new computer",
    "reset_intro": (
        "This computer forgets the Avibe it is bound to. After that it can be bound as a\n"
        "new computer, to any account."
    ),
    "reset_intro_unbound": (
        "This computer is not bound now. It forgets what is left on it from earlier\n"
        "bindings, and can then be bound as a new computer, to any account."
    ),
    "reset_lose_way_back": (
        "  - If this computer was bound to an Avibe before, it can no longer be bound\n"
        "    to that one again. That Avibe's domain and the people with access to it\n"
        "    do not move to the new binding."
    ),
    "reset_lose_heading": "You lose:",
    "reset_lose_access": "  - Remote access through {host} stops at once.",
    "reset_lose_devices": (
        "  - Every device signed in from elsewhere is signed out, and notifications to\n"
        "    those devices stop."
    ),
    "reset_lose_people": (
        "  - Everyone you gave access to loses it. The new binding starts with its\n"
        "    owner only."
    ),
    "reset_lose_links": "  - Every link already sent (Show Pages, files) stops working.",
    "reset_lose_health": "  - Health data already synced is deleted; connect Health again on the iPhone.",
    "reset_lose_name": (
        "  - {host} does not move with this computer.\n"
        "    How to take a name back is shown after this step."
    ),
    "current_domain": "the current domain",
    "reset_keep": (
        "You keep:\n"
        "  - Everything on this computer: sessions, files, Agents and settings.\n"
        "    Pages open on this computer keep working."
    ),
    "reset_unchanged": (
        "This does not change:\n"
        "  - The Avibe on avibe.bot is not deleted. If it still exists it keeps its\n"
        "    name, and it can no longer reach this computer. If it belongs to another\n"
        "    account, that account is not told.\n"
        "  - No credential is changed: not the tunnel credential, not model keys, not\n"
        "    the Vault."
    ),
    "reset_unchanged_unbound": (
        "This does not change:\n"
        "  - Nothing on avibe.bot is deleted. An Avibe this computer was bound to\n"
        "    before keeps its name, and its account is not told.\n"
        "  - No credential is changed: not the tunnel credential, not model keys, not\n"
        "    the Vault."
    ),
    "reset_irreversible": "This cannot be undone.",
    "reset_question": "Type yes to bind as a new computer. Press Enter to leave everything as it is: ",
    "reset_declined": "Nothing changed. This computer's binding is as it was.",
    "reset_done": "Done. This computer is no longer bound to any Avibe. Everything on it is as it was.",
    "reset_changed": (
        "This computer's binding changed while you were answering, so nothing was reset.\n"
        "To see where it stands now:\n" + COMMAND
    ),
    "busy": (
        "Another change to this computer's binding or domain is running. Nothing changed.\n"
        "When it has finished, run:\n" + COMMAND
    ),
    "reset_unfinished": (
        "The reset started and did not finish. Remote access on this computer stays off\n"
        "until it does. To continue it:\n" + COMMAND
    ),
    "reset_failed": (
        "The reset could not start. Nothing changed. To try again:\n" + COMMAND + "\n"
        "(code: {code})"
    ),
    "recovery_required": (
        "The last attempt may have completed on avibe.bot, but this computer has not\n"
        "finished saving it. Do not start another one.\n"
        "To continue that same attempt:\n" + COMMAND
    ),
    "link_intro": (
        "Bind this computer\n"
        "\n"
        "Open a link on any device, sign in to avibe.bot and name this computer's Avibe\n"
        "there. This terminal then shows the account and the domain and asks you to\n"
        "accept them. Nothing is bound before that."
    ),
    "link_show": (
        "Open this link:\n"
        "  {url}\n"
        "Waiting for it to be approved on avibe.bot. Ctrl+C stops."
    ),
    "link_confirm": (
        "avibe.bot reports that this request was approved by:\n"
        "  Account: {email}\n"
        "  Domain:  {host}\n"
        "This account becomes the owner of this computer's Avibe and can open\n"
        "everything in it from anywhere. If the account or the domain is not what you\n"
        "expect, answer no."
    ),
    "link_question": "Bind this computer to this account at this domain? [y/N] ",
    "bound": "Bound. This computer's Avibe is at https://{host}, owned by {email}.",
    "bound_sign_in": "Sign in at that address on your other devices.",
    "tunnel_not_running": "The tunnel is not running yet. Start it with:\n  vibe remote start",
    "link_declined": "Not bound. You answered no, so nothing was bound. To try again:\n" + COMMAND,
    "link_stopped": "Stopped. Nothing was bound; this computer is not bound. To continue:\n" + COMMAND,
    "link_stopped_after_accept": (
        "Stopped after you accepted. This computer is not bound. avibe.bot may have\n"
        "recorded the request. Run this and approve once more:\n" + COMMAND
    ),
    "link_expired": "The link expired before it was approved. Nothing was bound. For a new link:\n" + COMMAND,
    "already_bound": "This computer is already bound. Nothing changed.",
    "name_back": (
        "Taking the old name back\n"
        "\n"
        "A name that is given up is kept for the account that had it for 30 days;\n"
        "during that time nobody else can claim it.\n"
        "The old Avibe gives up its name when it is deleted on avibe.bot. Until then\n"
        "the name stays with it.\n"
        "To use {old} for this computer:\n"
        "delete the old Avibe on avibe.bot first. Then choose the name when avibe.bot\n"
        "asks you to name this computer's Avibe, or afterwards in Devices, under\n"
        "\"Change the domain\". avibe.bot decides whether the name can be taken; it is\n"
        "not guaranteed."
    ),
    "old_name": "the old name",
    "name_back_pointer": "Had another name before? See \"Taking the old name back\" in: vibe remote rebind --help",
    "refused_active": (
        "avibe.bot refused: that Avibe is connected to a computer that was active in\n"
        "the last 15 minutes. Nothing was bound.\n"
        "Go to that computer, or delete the Avibe on avibe.bot and bind again."
    ),
    "refused_loopback": (
        "avibe.bot refused: this computer has been bound before, and a computer like\n"
        "that is bound from a browser at its own address, not through a link.\n"
        "Nothing was bound.\n"
        "Use the browser on this computer, or from another computer through SSH port\n"
        "forwarding:\n"
        "    ssh -L {port}:127.0.0.1:{port} <user>@<this-server>\n"
        "then open http://127.0.0.1:{port} there, go to Devices and turn on remote\n"
        "access."
    ),
    "offer_new": "If you cannot do that, this computer can be bound as a new computer instead.",
    "unreachable": (
        "avibe.bot could not be reached, so nothing was bound. Check this computer's\n"
        "network, then run:\n" + COMMAND
    ),
    "rate_limited": (
        "avibe.bot asked this computer to wait, so nothing was bound. Wait {seconds}\n"
        "seconds, then run:\n" + COMMAND
    ),
    "reply_lost": (
        "The reply from avibe.bot was lost after you accepted, and it cannot be asked\n"
        "for again. This computer is not bound. Run this and approve once more:\n" + COMMAND
    ),
    "claim_in_progress": (
        "Another request to bind this computer is already open, in Avibe on this\n"
        "computer or in another terminal. Finish or cancel that one first.\n"
        "Nothing was bound here."
    ),
    "name_taken": (
        "That domain was taken before the binding finished. Nothing was bound.\n"
        "Run this and choose another:\n" + COMMAND
    ),
    "refused_unexplained": (
        "avibe.bot refused to bind this computer to that account and gave no reason it\n"
        "allows this computer to show. Nothing was bound.\n"
        "It may accept another account. To try again:\n" + COMMAND
    ),
    "binding_changed": (
        "This computer's binding changed while this was running. Nothing more was done\n"
        "here. To see where it stands now:\n" + COMMAND
    ),
    "link_failed": (
        "avibe.bot did not complete this request. Nothing was bound. To try again:\n" + COMMAND + "\n"
        "(code: {code})"
    ),
    "continue_attempt": (
        "An earlier attempt to bind this computer may have completed on avibe.bot.\n"
        "Continuing that same attempt now; no new one is started."
    ),
    "continue_finished": "Finished. This computer is bound: {url}",
    "continue_cleared": "That attempt did not complete and has been cleared. This computer is not bound.",
    "continue_unfinished": (
        "It has not finished: avibe.bot could not be reached or has not answered yet.\n"
        "Nothing new was started. To try again:\n" + COMMAND
    ),
    "continue_busy": (
        "Avibe is working on it right now. Nothing new was started. In a moment, run:\n" + COMMAND
    ),
    "reset_continue": (
        "A reset of this computer's binding was started and has not finished.\n"
        "Finishing it now."
    ),
    "reset_still_unfinished": (
        "It could not finish yet. Remote access on this computer stays off until it\n"
        "does. To try again:\n" + COMMAND
    ),
    "state_unknown": (
        "Avibe could not tell what state this computer's binding is in. Nothing changed.\n"
        "To look again:\n" + COMMAND + "\n"
        "(code: {code})"
    ),
    "repair_record_damaged": "The record of that reset is damaged.",
    "repair_binding_changed": "This computer's binding changed after that reset was started.",
    "repair_files_changed": "This computer's binding files changed after that reset was started.",
    "repair_mismatch": "The record of that reset no longer matches this computer.",
    "repair_note": (
        "A reset of this computer's binding was started and cannot be continued.\n"
        "{category}\n"
        "Avibe will not continue it by guessing. You can ask for a new reset of this\n"
        "computer as it is now; your earlier answer does not count for it."
    ),
    "repair_stuck": (
        "A reset of this computer's binding was started, and it can neither be finished\n"
        "nor replaced safely from here. Nothing more was changed.\n"
        "Remote access stays off. Sessions, files and settings on this computer are\n"
        "not affected.\n"
        "For help, see https://docs.avibe.bot/troubleshoot/common-issues or ask an\n"
        "Agent in Avibe to report this to Avibe."
    ),
}

STATE_LABELS = {
    "unpaired": "not bound",
    "bound": "bound",
    "binding_rejected": "refused by avibe.bot",
    "pairing_recovery_required": "an earlier attempt is unfinished",
    "resetting": "a reset is unfinished",
    "reset_repair_required": "a reset cannot finish",
    "damaged": "the binding record on this computer is damaged",
}

# Each link-step refusal maps to one sentence. Codes are the local allowlist
# ``personal_claims`` projects; an unknown one gets the generic sentence.
LINK_REFUSALS = {
    "active_pairing_recent": "refused_active",
    "control_plane_unreachable": "unreachable",
    "rate_limited": "rate_limited",
    "pairing_recovery_required": "recovery_required",
    "expired": "link_expired",
    "fresh_claim_required": "reply_lost",
    "claim_in_progress": "claim_in_progress",
    "operation_in_progress": "busy",
    "name_taken": "name_taken",
    "local_binding_changed": "binding_changed",
    "binding_unavailable": "refused_unexplained",
}
# After this proved refusal a computer with no browser cannot be bound as it is.
# The reset that may follow is a separate choice made at this terminal, never a
# permission the refusal grants: ``reset_interactively`` admits or refuses it on
# its own checks. An unexplained ``binding_unavailable`` gets no such offer: the
# installation identity exists from the first bind request, so this computer
# cannot tell an earlier binding from a request made seconds ago, and a reset
# would only lead back to the same refusal.
OFFER_NEW_AFTER = {
    "paired_installation_loopback_required": ("refused_loopback", "offer_new"),
}
DAMAGED_CODES = frozenset({
    "local_binding_unavailable", "local_instance_conflict", "local_identity_unavailable",
    "local_enrollment_unavailable",
})
# Why a started reset cannot be continued: a category only, never an identifier.
REPAIR_CATEGORIES = {
    "reset_record_damaged": "repair_record_damaged",
    "binding_changed": "repair_binding_changed",
    "files_changed": "repair_files_changed",
}

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class _Context:
    url: str
    host: str
    port: int


def _say(key: str, **values) -> None:
    print(TEXT[key].format(**values))


def _interactive() -> bool:
    from vibe.authorization import current_invocation_authority
    from vibe.cli import _cli_invocation_authority

    return (current_invocation_authority() is None and _cli_invocation_authority() is None
            and sys.stdin.isatty() and sys.stdout.isatty())


def _context() -> _Context:
    """Display values only: the saved domain and this computer's Avibe port."""
    try:
        from config.v2_config import V2Config

        config = V2Config.load()
        url = str(config.remote_access.vibe_cloud.public_url or "")
        port = int(config.ui.setup_port)
    except Exception:
        return _Context("", "", 5123)
    host = ""
    if url.isascii() and url.isprintable():
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            host = ""
    return _Context(url if host else "", host, port if 0 < port < 65536 else 5123)


def read_state() -> tuple[str, bool]:
    """The local binding state and whether the tunnel process is running. Read-only."""
    from vibe.desktop_local_instance import LocalInstanceError
    from vibe.personal_claim_client import ClaimError
    from vibe.personal_claims import rebind_claims

    try:
        view = rebind_claims.overview()
    except LocalInstanceError as exc:
        # An unreadable settings file blocks every writer; other local damage is
        # what the explicit reset exists to clear.
        return ("config_unreadable" if exc.code == "local_binding_unavailable" else "damaged"), False
    except ClaimError:
        return "damaged", False
    except (OSError, ValueError):
        return "config_unreadable", False
    state = view.get("binding_state")
    return (state if state in STATE_LABELS else "damaged"), bool(view.get("connector_still_running"))


def _print_state(state: str, tunnel: bool, ctx: _Context) -> None:
    _say("heading")
    if ctx.url:
        _say("line_domain", url=ctx.url)
    if state in STATE_LABELS:
        _say("line_binding", state=STATE_LABELS[state])
    _say("line_tunnel_running" if tunnel else "line_tunnel_stopped")
    if state == "binding_rejected":
        print()
        _say("rejected_note")
        if tunnel:
            _say("rejected_tunnel_note")


def _print_two_ways(ctx: _Context) -> None:
    print()
    _say("two_ways")
    print()
    _say("way_browser_title")
    for key in ("way_browser_body", "way_browser_effects", "copied_warning"):
        print(indent(TEXT[key].format(port=ctx.port), "   "))
        print()
    _say("way_new_title")


def _health_data_present() -> bool:
    try:
        from storage.mobile_store import mobile_store_path

        return os.path.lexists(mobile_store_path())
    except Exception:
        return True  # Never under-warn about a deletion.


def _print_reset_disclosure(ctx: _Context, intro: str | None, *, bound: bool) -> None:
    """Everything the reset discards and keeps, printed before the one question."""
    host = ctx.host or TEXT["current_domain"]
    if intro is not None:  # Otherwise the list of the two ways has just named it.
        print()
        print(intro)
        print()
        _say("reset_title")
    print()
    _say("reset_intro" if bound else "reset_intro_unbound")
    print()
    _say("reset_lose_heading")
    if bound:
        _say("reset_lose_access", host=host)
        _say("reset_lose_devices")
        _say("reset_lose_people")
        _say("reset_lose_links")
    else:
        _say("reset_lose_way_back")
    if _health_data_present():
        _say("reset_lose_health")
    if bound:
        _say("reset_lose_name", host=ctx.host or TEXT["current_domain"].capitalize())
    print()
    _say("reset_keep")
    print()
    _say("reset_unchanged" if bound else "reset_unchanged_unbound")
    print()
    _say("copied_warning_reset")
    print()
    _say("reset_irreversible")


def _offer_reset(ctx: _Context, *, intro: str | None = None, bound: bool = True, repair: bool = False) -> str:
    """Ask, then reset through the backend's interactive entry point.

    Returns ``reset`` (this computer is now unbound), ``declined``, ``nothing``
    (nothing on this computer to reset), ``live_attempt`` (an earlier attempt
    must be continued instead) or ``stopped`` (a sentence was printed).
    ``repair`` is the re-authorization of a reset whose record no longer
    matches: the backend replaces that record only after this same question.
    """
    from vibe import binding_reset
    from vibe.binding_admission import OperationBusy, reset_in_progress
    from vibe.desktop_local_instance import LocalInstanceError
    from vibe.personal_claim_client import ClaimError

    asked = False

    def confirm() -> bool:
        nonlocal asked
        asked = True
        _print_reset_disclosure(ctx, intro, bound=bound)
        try:
            answer = input(TEXT["reset_question"])
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer.strip().lower() == "yes"

    def failed(code: str) -> str:
        print()
        if code == "local_operator_required":
            _say("not_interactive")
        elif code == "config_unreadable":
            _say("config_unreadable")
        elif repair:
            # Refused before the question, or still not replaceable after it: say so
            # and stop. Anything else after the question left a reset to continue.
            _say("repair_stuck" if not asked or code == "reset_repair_required" else "reset_unfinished")
        elif asked and reset_in_progress():
            # Once the reset is sealed its marker owns the outcome, whatever stopped it.
            _say("reset_unfinished")
        elif code == "local_binding_changed":
            _say("reset_changed")
        elif code == "operation_in_progress":
            _say("reset_unfinished" if reset_in_progress() else "busy")
        else:
            _say("reset_failed", code=code if _CODE.fullmatch(code) else "unknown")
        return "stopped"

    try:
        result = binding_reset.reset_interactively(confirm=confirm)
    except KeyboardInterrupt:
        print()
        if asked and reset_in_progress():
            _say("reset_unfinished")
            return "stopped"
        _say("reset_declined")
        return "declined"
    except OperationBusy:
        return failed("operation_in_progress")
    except ClaimError as exc:
        if exc.code == "not_bound" and not asked:
            return "nothing"
        if exc.code == "pairing_recovery_required" and not asked:
            return "live_attempt"
        return failed(exc.code)
    except LocalInstanceError as exc:
        return failed("config_unreadable" if exc.code == "local_binding_unavailable" else exc.code)
    except Exception:
        # Exception text may carry local paths or record contents; print neither.
        return failed("local_failure")
    print()
    if result == {"state": "unpaired"}:
        _say("reset_done")
        return "reset"
    _say("reset_declined")
    return "declined"


def _resume() -> dict:
    """One bounded step of the operation that was already under way.

    The contract is ``vibe.binding_recovery.resume_interactively()``: it only
    advances a protected operation that exists when called, creates no claim
    and no reset, and returns ``{'state': ...}`` with one of ``unpaired``,
    ``bound``, ``idle``, ``resetting``, ``pairing_recovery_required``,
    ``reset_repair_required`` (optionally with an allowlisted ``reason_code``)
    or ``operation_in_progress``.

    AV-13 STUB: until the backend lands that module this reports ``idle``,
    which the caller answers by re-reading the local state.
    """
    try:
        from vibe import binding_recovery
    except ImportError:
        return {"state": "idle"}
    from vibe.binding_admission import OperationBusy
    from vibe.desktop_local_instance import LocalInstanceError
    from vibe.personal_claim_client import ClaimError

    try:
        view = binding_recovery.resume_interactively()
    except OperationBusy:
        return {"state": "operation_in_progress"}
    except ClaimError as exc:
        return {"state": exc.code}
    except LocalInstanceError:
        return {"state": "config_unreadable"}
    return view if isinstance(view, dict) else {"state": "idle"}


def _link(ctx: _Context, *, after_reset: bool, may_offer_reset: bool = True) -> int:
    from vibe import personal_claims

    accepted = False

    def show(url: str) -> None:
        print()
        _say("link_show", url=url)

    def confirm(email: str, hostname: str) -> bool:
        nonlocal accepted
        print()
        _say("link_confirm", email=email, host=hostname)
        try:
            answer = input(TEXT["link_question"])
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        accepted = answer.strip().lower() in ("y", "yes")
        return accepted

    print()
    if after_reset:
        _say("name_back", old=ctx.host or TEXT["old_name"])
        print()
    _say("link_intro")
    try:
        view = personal_claims.run_link(show_link=show, confirm=confirm, locale="en")
    except KeyboardInterrupt:
        print()
        state, _ = read_state()
        if state == "pairing_recovery_required":
            _say("recovery_required")
        elif state in ("bound", "binding_rejected"):
            _say("continue_finished", url=_context().url)
            return 0
        else:
            _say("link_stopped_after_accept" if accepted else "link_stopped")
        return 1
    print()
    state, code = view.get("state"), str(view.get("code") or "")
    if state == "bound":
        _say("bound", host=view["hostname"], email=view["account"]["email"])
        _say("bound_sign_in" if read_state()[1] else "tunnel_not_running")
        if not after_reset:
            print()
            _say("name_back_pointer")
        return 0
    if state == "declined":
        _say("link_declined")
        return 1
    if code == "already_bound":
        _say("already_bound")
        return 0
    if code in LINK_REFUSALS:
        seconds = view.get("retry_after_seconds")
        _say(LINK_REFUSALS[code], seconds=seconds if type(seconds) is int and seconds > 0 else 60)
        return 1
    if code in OFFER_NEW_AFTER:
        refusal, offer = OFFER_NEW_AFTER[code]
        _say(refusal, port=ctx.port)
        intro = TEXT[offer]
    elif code in DAMAGED_CODES:
        intro = TEXT["damaged_note"]
    else:
        _say("link_failed", code=code if _CODE.fullmatch(code) else "unknown")
        return 1
    if not may_offer_reset:
        if code in DAMAGED_CODES:
            _say("link_failed", code=code)
        return 1
    outcome = _offer_reset(ctx, intro=intro, bound=False)
    if outcome == "reset":
        return _link(ctx, after_reset=True, may_offer_reset=False)
    if outcome == "live_attempt":
        _say("recovery_required")
    elif outcome == "nothing" and code in DAMAGED_CODES:
        _say("link_failed", code=code)
    return 1


def _repair(ctx: _Context, reason_code: object) -> str:
    """A started reset that cannot be continued: ask again for this computer as it is now."""
    category = TEXT[REPAIR_CATEGORIES.get(reason_code if isinstance(reason_code, str) else "", "repair_mismatch")]
    return _offer_reset(ctx, intro=TEXT["repair_note"].format(category=category), repair=True)


def run() -> int:
    if not _interactive():
        print(TEXT["not_interactive"], file=sys.stderr)
        return 1
    state, tunnel = read_state()
    ctx = _context()
    _print_state(state, tunnel, ctx)
    after_reset = False
    outcome = None
    if state in ("resetting", "pairing_recovery_required", "reset_repair_required"):
        unfinished = state
        if state != "reset_repair_required":
            print()
            _say("reset_continue" if state == "resetting" else "continue_attempt")
        resumed = _resume()
        state = resumed.get("state")
        if state == "idle":  # Nothing to continue any more: where does the computer stand now?
            state, _ = read_state()
        if state == "operation_in_progress":
            _say("continue_busy")
            return 1
        if state == "reset_repair_required":
            outcome = _repair(ctx, resumed.get("reason_code"))
        elif state == "resetting":
            _say("reset_still_unfinished")
            return 1
        elif state == "pairing_recovery_required":
            # A record the backend will not replay is cleared only by the explicit
            # reset; a live one makes ``reset_interactively`` refuse before asking.
            outcome = _offer_reset(ctx, intro=TEXT["stale_attempt_note"])
            if outcome == "live_attempt":
                _say("continue_unfinished")
        elif state in ("bound", "binding_rejected"):
            _say("continue_finished", url=_context().url)
            return 0
        elif state == "unpaired":
            if unfinished == "pairing_recovery_required":
                _say("continue_cleared")
            else:
                _say("reset_done")
                after_reset = True
    elif state in ("bound", "binding_rejected"):
        _print_two_ways(ctx)
        outcome = _offer_reset(ctx)
    elif state == "damaged":
        outcome = _offer_reset(ctx, intro=TEXT["damaged_note"])
    if state == "config_unreadable":
        print()
        _say("config_unreadable")
        return 1
    if outcome is not None:
        if outcome == "live_attempt" and state != "pairing_recovery_required":
            _say("recovery_required")
        if outcome not in ("reset", "nothing"):
            return 0 if outcome == "declined" else 1
        after_reset = outcome == "reset"
    elif state != "unpaired":
        # A state this command does not know is not a reason to start binding.
        _say("state_unknown", code=state if isinstance(state, str) and _CODE.fullmatch(state) else "unknown")
        return 1
    return _link(ctx, after_reset=after_reset)
