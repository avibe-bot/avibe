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
        "This Avibe may have been deleted on avibe.bot, or bound again from another\n"
        "computer. This computer cannot tell why."
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
    "stale_attempt_note": "Binding as a new computer clears that record.",
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
        "avibe.bot refused: that Avibe is running on another computer that was active in\n"
        "the last 15 minutes. Nothing was bound.\n"
        "To move it to this computer: quit Avibe on that computer, wait 15 minutes and\n"
        "try again.\n"
        "If that computer is no longer yours to reach: delete that Avibe on avibe.bot and\n"
        "bind again; its access list is lost, and its name is kept for your account for\n"
        "30 days."
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
        "avibe.bot refused this request and gave no reason. Nothing was bound.\n"
        "To try again:\n" + COMMAND
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
    "continue_nothing_left": (
        "Nothing is left to continue: that work is no longer saved on this computer,\n"
        "and this command did not complete it. Where this computer stands now:"
    ),
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
    "attempt_refused": (
        "avibe.bot has refused an earlier attempt to bind this computer, for good.\n"
        "It is not tried again. It may still have taken effect on avibe.bot. If remote\n"
        "access stops working, bind again."
    ),
    "attempt_unusable": (
        "The record of that attempt cannot be used on this computer. It is not\n"
        "continued or retried, and Avibe will not guess whether it completed."
    ),
    # Why an earlier reset cannot be continued: a category, never an identifier.
    "repair_record_damaged": "its record on this computer is damaged.",
    "repair_binding_changed": "this computer's binding changed after it was started.",
    "repair_files_changed": "this computer's binding files changed after it was started.",
    "repair_mismatch": "its record no longer matches this computer.",
    "repair_note": (
        "A reset of this computer was started earlier and cannot be continued:\n"
        "{category}\n"
        "What follows is a new reset of this computer as it is now."
    ),
    # Product's text for the one state with two unfinished records (November 22).
    "keep_head": (
        "Two unfinished things are on this computer:\n"
        "  - a binding that was approved earlier and can still be completed"
    ),
    "keep_domain": "      Domain: {host}",
    "keep_domain_unknown": "      Its domain is shown when it has finished.",
    "keep_body": (
        "  - a reset that was started and cannot be continued: {category}\n"
        "\n"
        "Answer yes to complete the binding and drop the reset. Remote access may\n"
        "still be off afterwards; the command to turn it on is shown then.\n"
        "Anything else leaves both as they are, with remote access off."
    ),
    "keep_question": "Complete the binding and drop the reset? [y/N] ",
    "keep_declined": "Nothing changed. Both are as they were, with remote access off.",
    "keep_withdrawn": "The reset was dropped.",
    "keep_stopped": (
        "Stopped. Nothing was done: both are still there, with remote access off.\n"
        "To look again:\n" + COMMAND
    ),
    "repair_declined": (
        "Nothing changed. The earlier reset is still unfinished, and remote access on\n"
        "this computer stays off."
    ),
    "keep_changed": (
        "This computer changed while you were answering, so nothing was done.\n"
        "To look again:\n" + COMMAND
    ),
    "repair_stuck": (
        "A reset of this computer was started, and it can neither be finished nor\n"
        "replaced by a new one safely from here. Nothing more was changed: remote\n"
        "access stays off, and your data on this computer is not affected.\n"
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
    bound: bool = False


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
        cloud = config.remote_access.vibe_cloud
        url = str(cloud.public_url or "")
        port = int(config.ui.setup_port)
        bound = bool(cloud.instance_id and cloud.instance_secret)
    except Exception:
        return _Context("", "", 5123)
    host = ""
    if url.isascii() and url.isprintable():
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            host = ""
    return _Context(url if host else "", host, port if 0 < port < 65536 else 5123, bound)


def read_state() -> tuple[str, bool, str | None]:
    """The local binding state, whether the tunnel process runs, and a reason. Read-only."""
    from vibe.desktop_local_instance import LocalInstanceError
    from vibe.personal_claim_client import ClaimError
    from vibe.personal_claims import rebind_claims

    try:
        view = rebind_claims.overview()
    except LocalInstanceError as exc:
        # An unreadable settings file blocks every writer; other local damage is
        # what the explicit reset exists to clear.
        return ("config_unreadable" if exc.code == "local_binding_unavailable" else "damaged"), False, None
    except ClaimError:
        return "damaged", False, None
    except (OSError, ValueError):
        return "config_unreadable", False, None
    state = view.get("binding_state")
    reason = view.get("reason_code")
    if state not in STATE_LABELS:
        state = state if isinstance(state, str) and _CODE.fullmatch(state) else "unknown"
    return state, bool(view.get("connector_still_running")), reason if reason == "issuer_replay_refused" else None


def _print_state(state: str, tunnel: bool, ctx: _Context, reason: str | None = None) -> None:
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
    if reason == "issuer_replay_refused":
        # Said once, here. The refused request no longer holds the binding in
        # recovery, so what follows is the ordinary flow for the state shown.
        print()
        _say("attempt_refused")


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
    ``repair`` asks for a new reset in place of one whose record no longer
    matches. The backend decides before the question whether that is safe, and
    it never accepts this answer in place of completing a recoverable binding.
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
        elif code == "local_binding_changed" and (repair or not reset_in_progress()):
            _say("reset_changed")
        elif repair:
            # ``operation_in_progress`` before the question: the record is usable after all.
            _say("reset_still_unfinished" if code == "operation_in_progress" and not asked else "repair_stuck")
        elif asked and reset_in_progress():
            # Once the reset is sealed its marker owns the outcome, whatever stopped it.
            _say("reset_unfinished")
        elif code == "operation_in_progress":
            _say("reset_unfinished" if reset_in_progress() else "busy")
        else:
            _say("reset_failed", code=code if _CODE.fullmatch(code) else "unknown")
        return "stopped"

    try:
        result = binding_reset.reset_interactively(confirm=confirm, **({"repair": True} if repair else {}))
    except KeyboardInterrupt:
        print()
        # Ctrl+C at the question is a decline and never arrives here. Once asked,
        # the answer was yes: a reset is sealed (or, for a new reset in place of an
        # old one, being sealed) and the same command continues it.
        if asked and (repair or reset_in_progress()):
            _say("reset_unfinished")
            return "stopped"
        _say("repair_declined" if repair else "reset_declined")
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
    state = result.get("state") if isinstance(result, dict) else None
    if state == "unpaired":
        _say("reset_done")
        return "reset"
    if state == "declined":
        _say("repair_declined" if repair else "reset_declined")
        return "declined"
    # Sealed and not run to the end, whatever stopped it: the same command continues.
    _say("reset_unfinished")
    return "stopped"


def _resume(**confirmation) -> dict:
    """One bounded step of the operation that was already under way.

    ``binding_recovery.resume_interactively()`` advances only a protected
    operation that exists when called. It creates no claim and no reset, does
    not start the service, and reports ``idle`` when there was nothing to
    continue, which is not success.
    """
    from vibe import binding_recovery
    from vibe.binding_admission import OperationBusy
    from vibe.desktop_local_instance import LocalInstanceError
    from vibe.personal_claim_client import ClaimError

    try:
        view = binding_recovery.resume_interactively(**confirmation)
    except OperationBusy:
        return {"state": "operation_in_progress"}
    except ClaimError as exc:
        return {"state": exc.code}
    except LocalInstanceError:
        return {"state": "config_unreadable"}
    return view if isinstance(view, dict) else {"state": "idle"}


def _repair_category(reason_code: object) -> str:
    return TEXT[REPAIR_CATEGORIES.get(reason_code if isinstance(reason_code, str) else "", "repair_mismatch")]


def _offer_keep_binding(view: dict) -> dict | None:
    """A recoverable binding next to a reset that cannot continue: complete the binding?

    This is never the reset question. Declining leaves both records as they are.
    Returns the backend's view after a yes, or None when the person declined.
    The domain shown is handed back, so no other target can be confirmed.
    """
    host = view.get("hostname") if isinstance(view.get("hostname"), str) else None
    answered = False

    def confirm() -> bool:
        nonlocal answered
        print()
        _say("keep_head")
        if host:
            _say("keep_domain", host=host)
        else:
            _say("keep_domain_unknown")
        _say("keep_body", category=_repair_category(view.get("reason_code")))
        print()
        try:
            answer = input(TEXT["keep_question"])
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        answered = answer.strip().lower() in ("y", "yes")
        return answered

    try:
        result = _resume(confirm_pairing=confirm, **({"expected_hostname": host} if host else {}))
    except KeyboardInterrupt:
        print()
        result = {"state": "interrupted"}
    print()
    if not answered:
        _say("keep_declined")
        return None
    return result


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
        state = read_state()[0]
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


def _after_reset_offer(ctx: _Context, outcome: str, *, live_attempt: str = "recovery_required") -> int:
    """What follows the reset question: bind as new, or stop where the answer left it."""
    if outcome == "live_attempt":
        _say(live_attempt)
    if outcome not in ("reset", "nothing"):
        return 0 if outcome == "declined" else 1
    return _link(ctx, after_reset=outcome == "reset")


def _say_bound_now() -> None:
    _say("continue_finished", url=_context().url)
    if not read_state()[1]:  # Completing a binding does not turn remote access back on.
        _say("tunnel_not_running")


def _continue_unfinished(ctx: _Context, started_as: str) -> int:
    """An attempt or a reset was already under way: continue it, never start beside it."""
    if started_as != "reset_repair_required":
        print()
        _say("reset_continue" if started_as == "resetting" else "continue_attempt")
    view = _resume()
    if view.get("state") == "reset_repair_required" and view.get("next_action") == "confirm_resume_pairing":
        kept = _offer_keep_binding(view)
        if kept is None:
            return 0
        if kept.get("state") == "interrupted":
            # Stopped after the answer: the reset may already be dropped and the
            # binding under way. Say where the computer stands, not what was meant.
            now = read_state()[0]
            if now in ("bound", "binding_rejected"):
                _say("keep_withdrawn")
                _say_bound_now()
                return 0
            if now == "pairing_recovery_required":
                _say("keep_withdrawn")
                _say("continue_unfinished")
            else:
                _say("keep_stopped")
            return 1
        if kept.get("state") == "reset_repair_required":
            _say("keep_changed")
            return 1
        _say("keep_withdrawn")
        view = kept
    state, reason = view.get("state"), view.get("reason_code")
    if state == "idle":
        # Nothing was there to continue, which is not the same as it having
        # completed: say so, show where the computer stands and go on from there.
        _say("continue_nothing_left")
        print()
        return _from_state(_context(), *read_state(), may_continue=False)
    if state == "operation_in_progress":
        _say("continue_busy")
        return 1
    if state == "config_unreadable":
        _say("config_unreadable")
        return 1
    if state == "local_operator_required":
        _say("not_interactive")
        return 1
    if state == "resetting":
        _say("reset_still_unfinished")
        return 1
    if state == "reset_repair_required":
        return _after_reset_offer(ctx, _offer_reset(
            ctx, intro=TEXT["repair_note"].format(category=_repair_category(reason)),
            bound=_context().bound, repair=True))
    if state == "pairing_recovery_required":
        if reason == "issuer_replay_refused":
            print()
            _say("attempt_refused")
            if _context().bound:
                _print_two_ways(ctx)
                return _after_reset_offer(ctx, _offer_reset(ctx))
            return _link(ctx, after_reset=False)
        if reason == "pairing_record_unusable":
            _say("attempt_unusable")
            # Such a record is cleared only by the explicit reset, and a live one
            # still makes ``reset_interactively`` refuse before it asks.
            return _after_reset_offer(
                ctx, _offer_reset(ctx, intro=TEXT["stale_attempt_note"], bound=_context().bound),
                live_attempt="continue_unfinished")
        _say("continue_unfinished")
        return 1
    if state in ("bound", "binding_rejected"):
        _say_bound_now()
        return 0
    if state == "unpaired":
        _say("reset_done")
        return _link(ctx, after_reset=True)
    _say("state_unknown", code=state if isinstance(state, str) and _CODE.fullmatch(state) else "unknown")
    return 1


def _from_state(ctx: _Context, state: str, tunnel: bool, reason: str | None, *, may_continue: bool = True) -> int:
    """Print where this computer stands, then do what that state calls for."""
    _print_state(state, tunnel, ctx, reason)
    if state == "config_unreadable":
        print()
        _say("config_unreadable")
        return 1
    if state in ("resetting", "pairing_recovery_required", "reset_repair_required"):
        if may_continue:
            return _continue_unfinished(ctx, state)
        print()
        _say("continue_busy")  # It reappeared under another writer; never a second step here.
        return 1
    if state in ("bound", "binding_rejected"):
        _print_two_ways(ctx)
        return _after_reset_offer(ctx, _offer_reset(ctx))
    if state == "damaged":
        return _after_reset_offer(ctx, _offer_reset(ctx, intro=TEXT["damaged_note"], bound=ctx.bound))
    if state == "unpaired":
        return _link(ctx, after_reset=False)
    # A state this command does not know is not a reason to reset or to bind.
    print()
    _say("state_unknown", code=state if _CODE.fullmatch(state) else "unknown")
    return 1


def run() -> int:
    if not _interactive():
        print(TEXT["not_interactive"], file=sys.stderr)
        return 1
    return _from_state(_context(), *read_state())
