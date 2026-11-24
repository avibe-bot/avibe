"""Installation claim coordinator. HTTP callers cannot choose the mode's acceptance gate."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hmac
import json
import secrets
import socket
import threading
import time

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import config_file_lock
from storage.lock import MigrationFileLock, MigrationLockTimeout
from vibe.binding_admission import OperationBusy, admitted
from vibe import desktop_local_instance as local, remote_access
from vibe.personal_claim_client import (
    ClaimError, ClaimsClient, ConfirmedRefusal, OtherAccountRefusal, ConstrainedClaimsClient, Grant, Pending, Ticket,
    local_origin, loopback_origin, return_proof, text,
)


@dataclass(frozen=True, repr=False)
class _Source:
    installation: str
    revision: str
    device: str
    backend: str

    @classmethod
    def load(cls, on_bound):
        # Only an explicit start reaches this reconciliation. Do not retire a still-usable
        # predecessor when a new start, browser grant or mode gate might subsequently fail.
        replaceable = _recover_before_start(on_bound)
        with config_file_lock():
            config, pending = local._config(), local._pending()
            cloud = config.remote_access.vibe_cloud
            if pending != replaceable:
                raise ClaimError("local_binding_changed")
            if cloud.instance_id and cloud.instance_secret and cloud.backend_url:
                raise ClaimError("already_bound")
            if any((cloud.instance_id, cloud.instance_secret, cloud.tunnel_token, cloud.client_id, cloud.session_secret)):
                raise ClaimError("local_binding_unavailable")
            installation = local.ensure_installation_id(recover_from=(pending or {}).get("personal_claim"))
            return cls(installation, local.claim_revision(config, installation, pending), socket.gethostname(), cloud.backend_url)

    def client(self):
        return ClaimsClient(self.backend)

    def validate(self):
        with config_file_lock():
            config, identity, pending = local._config(), local._installation_id(), local._pending()
            if self.revision != local.claim_revision(config, identity, pending):
                raise ClaimError("local_binding_changed")


@dataclass(frozen=True, repr=False)
class _BoundSource(_Source):
    instance: str
    secret: str

    @classmethod
    def load(cls, on_bound):
        # Never reconcile an uncertain earlier redemption by starting another claim.
        with config_file_lock():
            config, pending = local._config(), local._pending()
            if pending is not None and not remote_access._issuer_replay_refused(pending):
                raise ClaimError("pairing_recovery_required")
            cloud = config.remote_access.vibe_cloud
            if not cloud.instance_id and not any((cloud.instance_secret, cloud.client_id, cloud.tunnel_token, cloud.session_secret)):
                raise ClaimError("not_bound")
            if not all((cloud.backend_url, cloud.instance_id, cloud.instance_secret)):
                raise ClaimError("local_binding_unavailable")
            installation = local.ensure_installation_id()
            return cls(installation, local.claim_revision(config, installation, pending), socket.gethostname(),
                       cloud.backend_url, cloud.instance_id, cloud.instance_secret)

    def client(self):
        return ConstrainedClaimsClient(self.backend, self.instance, self.secret)


def _recover_before_start(on_bound):
    """Return the *exact* still-prepared record an explicit new claim may supersede."""
    with config_file_lock():
        pending = local._pending()
        if not pending:
            return None
        if (pending.get("personal_claim") or {}).get("binding_disposition") is not None:
            raise ClaimError("pairing_recovery_required")
        phase = pending["phase"]
        if phase == "revoked":
            cloud = local._config().remote_access.vibe_cloud
            if any((cloud.instance_id, cloud.instance_secret, cloud.tunnel_token, cloud.client_id, cloud.session_secret)):
                raise ClaimError("local_binding_unavailable")
            if not remote_access.complete_pairing_revocation(pending["operation_id"]):
                raise ClaimError("pairing_recovery_required")
            return None
        if phase == "prepared":
            if pending.get("desktop_enrollment"):
                raise ClaimError("local_instance_conflict")
            if not pending.get("redeem_request"):
                return pending
            if not pending.get("personal_claim"):
                raise ClaimError("local_instance_conflict")
            # A missing correlation file need not make a saved request unreplayable.
            # Restore the same ID only if the claim still has its original binding source.
            identity = remote_access._pairing_identity_from_cloud(local._config().remote_access.vibe_cloud)
            if remote_access._pairing_identity_fingerprint(identity) == pending["source_fingerprint"]:
                local.ensure_installation_id(recover_from=pending["personal_claim"])
    if phase == "prepared":
        result = remote_access._retry_installation_pairing(pending)
        code, status = result.get("error"), result.get("status")
        cause = result.get("pairing", {}).get("cause")
        if (code == "local_binding_changed" or code == "pairing_redeem_indeterminate" and (
            (status == 409 and cause == "registration_indeterminate") or
            (status in (400, 401, 403, 404, 409, 410, 422) and cause == "pairing_request_failed")
        )):
            return pending  # Snapshot must still match both at start and at accepted handoff.
    else:
        result = remote_access._persist_pairing(None, device_name=pending["device_name"], operation_id=pending["operation_id"])
    if result.get("ok"):
        if on_bound:
            on_bound()
        raise ClaimError("already_bound")
    if result.get("error") in ("pairing_failure_retired", "pairing_key_expired"):
        return None
    raise ClaimError("pairing_recovery_required", result.get("retry_after_seconds", 5))


def _marker(constrained=False):
    return paths.get_state_dir() / ("rebind-claim-outcome.json" if constrained else "personal-claim-outcome.json")


def _outcome(constrained=False):
    """Nonsecret status only. A damaged file reads as absent and the next write replaces it."""
    try:
        saved = json.loads(_marker(constrained).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, RecursionError):
        return {}
    except OSError:
        raise ClaimError("local_claim_unavailable") from None
    bound = saved.get("bound", {}) if isinstance(saved, dict) else None
    if not isinstance(bound, dict) or (bound and not all(isinstance(bound.get(k), str) for k in ("claim_id", "generation"))):
        return {}
    return saved


def _record_attempt(ticket, *, constrained=False):
    with config_file_lock():
        saved = _outcome(constrained)
        saved.update(claim_id=ticket.id, expires_at=ticket.expiry)
        write_atomic(_marker(constrained), json.dumps(saved))


def _binding_stamp(identity):
    # Off is still bound. Connector preferences and browser-session rotation aren't a rebind.
    return remote_access._pairing_identity_fingerprint({k: identity[k] for k in ("backend_url", "instance_id", "instance_secret")})


def record_bound(claim_id, identity, *, disposition=None):
    """Called under the publication lock before retirement, including by legacy Resume."""
    saved = _outcome(disposition is not None)
    saved["bound"] = {"claim_id": claim_id, "generation": _binding_stamp(identity)}
    if disposition is not None:
        if disposition not in ("existing", "new"):
            raise ClaimError("invalid_claim_response")
        saved["bound"]["binding_disposition"] = disposition
    if saved.get("claim_id") == claim_id:
        saved.pop("claim_id")
        saved.pop("expires_at", None)
    write_atomic(_marker(disposition is not None), json.dumps(saved))


def redirect_origin_allows_auto_accept(origin: str, *, admitted_owner: bool) -> bool:
    """The redirect's local transport requirement, independent of the return proof.

    `admitted_owner` means the existing server-side local-owner admission (loopback or a
    private setup host) held for this request with no forwarding metadata; it never comes
    from the request body. The control-plane origin grammar alone is not authorization, so
    both must hold. Loopback and admitted private-network origins are treated alike.
    """
    return admitted_owner and local_origin(origin)


class _Flow:
    constrained = False
    def __init__(self, mode, locale, *, origin="", admitted_owner=False, expected=None, confirm=None, show=None, on_bound=None):
        self.on_bound = on_bound
        self.previous_expiry = None
        self.admitted_owner = admitted_owner
        self._cleanup_client = None
        self._decline_attempted = False
        self._declined_upstream = False
        self.mode, self.locale, self.origin = mode, locale, origin
        self.expected, self.confirm, self.show = expected, confirm, show
        self.lock = threading.Condition()
        self.ready = threading.Event()
        self.view = {"state": "starting"}
        self.ticket: Ticket | None = None
        self.first = None
        self._browser = self._proof = None
        self.returned = self.cancelled = self.accepting = False

    def update(self, state, **values):
        with self.lock:
            self.view = {"state": state, **values}
            if self.previous_expiry:
                self.view["previous_claim_expires_at"] = self.previous_expiry
            if self.ticket:
                self.view.update(claim_id=self.ticket.id, expires_at=self.ticket.expiry)
            self.lock.notify_all()

    def callback(self, origin, browser, state, *, token=None):
        with self.lock:
            if (not self.ticket or self.mode != "redirect" or self.origin != origin or self.cancelled or self.returned
                    or self.view["state"] not in ("pending", "waiting") or time.time() >= self.ticket.deadline
                    or not self._browser or not self._proof or not _matches(self._browser, browser) or not _matches(self._proof, state)):
                raise ClaimError("invalid_return")
            if self.constrained:
                if self._cleanup_client is None:
                    raise ClaimError("invalid_return")
                self._cleanup_client.confirm_return(self.ticket, token)
            elif token is not None:
                raise ClaimError("invalid_return")
            self._browser = self._proof = None
            self.returned = True
            self.lock.notify_all()

    def declined_by_return(self, origin, browser, state):
        """avibe.bot sent this browser back after the person cancelled there.

        The same browser nonce and state proof as a return; it can only end this claim.
        """
        with self.lock:
            if (not self.ticket or self.mode != "redirect" or self.origin != origin or self.cancelled or self.returned
                    or self.view["state"] not in ("pending", "waiting") or time.time() >= self.ticket.deadline
                    or not self._browser or not self._proof or not _matches(self._browser, browser) or not _matches(self._proof, state)):
                raise ClaimError("invalid_return")
            self._browser = self._proof = None
            self._declined_upstream = self._decline_attempted = True  # avibe.bot already declined it
            self.cancelled = True
            self.update("cancelling")
            self.lock.notify_all()

    def cancel(self):
        with self.lock:
            if self.accepting or self.view["state"] in ("bound", "pairing"):
                raise ClaimError("already_started")
            if self.view["state"] in ("error", "declined"):
                return dict(self.view)
            self.cancelled = True
            self._browser = self._proof = None
            self.update("cancelling")
            return dict(self.view)

    def _wait(self, delay, *, interrupt=True):
        with self.lock:
            until = time.monotonic() + min(delay, max(0, self.ticket.deadline - time.time()))
            self.lock.wait_for(lambda: interrupt and self.cancelled, timeout=max(0, until - time.monotonic()))
        self._check_time()

    def _check_time(self):
        if time.time() >= self.ticket.deadline:
            raise ClaimError("fresh_claim_required" if self.accepting else "expired")

    def _execute(self):
        from storage.importer import ensure_sqlite_state

        if self.locale not in ("en", "zh"):
            raise ClaimError("invalid_request")
        source = (_BoundSource if self.constrained else _Source).load(self.on_bound)
        self.previous_expiry = _outcome().get("expires_at") if not self.constrained else None
        if not text(source.device, 120):
            raise ClaimError("invalid_device_name")
        client = source.client()
        state = secrets.token_urlsafe(32) if self.mode == "redirect" else ""
        ticket = self.ticket = client.start(source.installation, source.device, self.mode, self.locale, origin=self.origin, state=state)
        self._cleanup_client = client
        self._check_time()
        with self.lock:
            self.update("pending")
            if self.mode == "redirect":
                self._browser = secrets.token_urlsafe(32)
                if self.constrained:
                    self.reset_browser = reset_browser_token(self._browser)
                self._proof = return_proof(state, ticket.id)
                self.first = ({"claim_id": ticket.id, "expires_at": ticket.expiry, "form": {
                    "method": "POST", "action": client.base + "/api/v1/claims/redirect-entry",
                    "fields": {"user_code": ticket.code, "redirect_state": state}, "referrer_policy": "strict-origin",
                }}, self._browser)
                if self.previous_expiry:
                    self.first[0]["previous_claim_expires_at"] = self.previous_expiry
            self.ready.set()
        del state
        if self.show is not None:
            self.show(ticket.url)
        if self.mode == "redirect":
            with self.lock:
                self.lock.wait_for(lambda: self.returned or self.cancelled, timeout=max(0, ticket.deadline - time.time()))
        else:
            self._wait(ticket.interval)
        grant = None
        backoff = ticket.interval
        mismatch = False
        while True:
            self._check_time()
            try:
                if self.cancelled:
                    if not self._declined_upstream:
                        # A transient failure below retries this same decline.
                        self._decline_attempted = True
                        client.decline(ticket)
                    self.update("declined", code="subject_mismatch" if mismatch else "declined")
                    return
                if grant is None:
                    try:
                        result = client.observe(ticket)
                    except ConfirmedRefusal as refusal:
                        if not self.constrained or not self.returned:
                            raise ClaimError("installation_unverified") from None
                        source.validate()
                        with self.lock:
                            if self.cancelled:
                                continue
                            self._decline_attempted = True  # Already terminal; there is no grant to decline.
                            self.refused_revision = source.revision
                            self.update("error", code="installation_unverified", reason_code=refusal.reason,
                                        **({"owner_hint": refusal.owner_hint} if refusal.owner_hint else {}))
                        return
                    except ClaimError as error:
                        if self.mode == "redirect" and not self.constrained:
                            if error.code in ("rate_limited", "control_plane_unreachable"):
                                self._wait(max(1, error.retry_after), interrupt=False)
                                raise ClaimError("invalid_return") from None
                            # Preserve terminal producer errors. In particular, an unavailable
                            # binding is not a bad return proof.
                            raise
                        raise
                    if self.mode == "redirect" and isinstance(result, Pending):
                        self._wait(result.delay, interrupt=False)
                        raise ClaimError("invalid_return")
                    if isinstance(result, Grant):
                        if self.mode == "redirect":
                            approved = self.returned and redirect_origin_allows_auto_accept(
                                self.origin, admitted_owner=self.admitted_owner,
                            )
                        elif self.expected is not None:
                            approved = self.expected == result.subject
                            mismatch = not approved
                        else:
                            approved = self.mode == "link" and self.confirm is not None and self.confirm(result.email, result.hostname) is True
                        if not approved:
                            self.cancelled = True
                            continue
                        source.validate()
                        if remote_access._pairing_local_write_preflight():
                            raise ClaimError("local_binding_unavailable")
                        remote_access._run_pending_deferred_context_migration()
                        ensure_sqlite_state()
                        with self.lock:
                            if self.cancelled:
                                continue
                            self._check_time()
                            # Write before sending: a crash from here until durable pairing handoff is ambiguous.
                            _record_attempt(ticket, constrained=self.constrained)
                            self.accepting, grant = True, result
                            self.update("accepting", account={"email": grant.email}, hostname=grant.hostname)
                        continue
                else:
                    source.validate()
                    result = client.accept(ticket, grant)
                    if not isinstance(result, Pending):
                        self.update("pairing")
                        paired = remote_access.pair_personal_claim(
                            key=result.key, backend_url=client.base, device=source.device, revision=source.revision,
                            installation=source.installation, subject=grant.subject, claim_id=ticket.id,
                            instance_id=result.instance_id, hostname=grant.hostname,
                            **({"binding_disposition": result.binding_disposition} if self.constrained else {}),
                        )
                        if not paired.get("ok"):
                            if paired.get("error") in ("local_binding_changed", "pairing_superseded_after_redeem"):
                                raise ClaimError("local_binding_changed")
                            raise ClaimError("pairing_recovery_required" if _journal_for(ticket.id) else "fresh_claim_required")
                        if self.on_bound:
                            self.on_bound()
                        self.update("bound", account={"email": grant.email}, hostname=grant.hostname,
                                    **({"binding_disposition": paired["binding_disposition"]} if self.constrained else {}))
                        return
                delay = max(ticket.interval, result.delay)
                backoff = ticket.interval
            except ClaimError as exc:
                if exc.code not in ("control_plane_unreachable", "rate_limited"):
                    raise
                delay = max(exc.retry_after, backoff)
                backoff = min(60, max(2, backoff * 2))
                self.update("waiting", code=exc.code, retry_after_seconds=delay)
            # Cancellation fences local consent immediately but may not bypass upstream backoff.
            self._wait(delay, interrupt=False)

    def run(self):
        try:
            with MigrationFileLock(paths.get_state_dir() / "personal-claim.lock", timeout_seconds=0), admitted('pairing'):
                try:
                    self._execute()
                finally:
                    # A failed/preempted gate is not permission to leave a grant armed.
                    if (not self.accepting and not self._decline_attempted and self._cleanup_client and self.ticket
                            and time.time() < self.ticket.deadline):
                        try:
                            self._cleanup_client.decline(self.ticket)
                        except ClaimError:
                            pass  # An unconfirmed cleanup never reports that the hold is free.
                    self._cleanup_client = None
        except (ClaimError, local.LocalInstanceError) as exc:
            self.update("error", code=exc.code, **({"retry_after_seconds": exc.retry_after} if getattr(exc, "retry_after", 0) else {}),
                        **({"confirming_account_email": exc.confirming_account_email}
                           if isinstance(exc, OtherAccountRefusal) else {}))
        except OperationBusy:
            self.update("error", code="operation_in_progress")
        except MigrationLockTimeout:
            self.update("error", code="claim_in_progress")
        except Exception:
            # Request bodies, exception locals and underlying HTTP detail are not diagnostics.
            self.update("error", code="fresh_claim_required" if self.accepting else "local_claim_unavailable")
        finally:
            self._browser = self._proof = None
            self.confirm = self.show = None
            self.ready.set()
        return dict(self.view)


def _matches(expected, actual):
    return isinstance(actual, str) and actual.isascii() and hmac.compare_digest(expected, actual)


def _journal_for(claim_id):
    with config_file_lock():
        record = local._pending()
        return record if record and record.get("personal_claim", {}).get("claim_id") == claim_id else None


def recover_pairing(claim_id: str) -> dict:
    """Retry only the existing guarded pairing request, not the one-shot claim acceptance."""
    with MigrationFileLock(paths.get_state_dir() / "personal-claim.lock", timeout_seconds=0):
        record = _journal_for(claim_id)
        if not record or "binding_disposition" in record["personal_claim"]:
            raise ClaimError("fresh_claim_required")
        if record["phase"] == "prepared":
            result = remote_access._retry_installation_pairing(record)
        else:
            result = remote_access._persist_pairing(None, device_name=record["device_name"], operation_id=record["operation_id"])
        if not result.get("ok"):
            return {"state": "error", "code": "pairing_recovery_required" if _journal_for(claim_id) else "fresh_claim_required"}
        return {"state": "bound", "claim_id": claim_id}


def run_link(*, show_link: Callable[[str], None], expected_subject: str | None = None,
             confirm: Callable[[str, str], bool] | None = None, locale="en") -> dict:
    """Supply either a predeclared stable subject or a trusted interactive confirmation adapter."""
    if ((expected_subject is None) == (confirm is None) or expected_subject is not None and not text(expected_subject, 200)
            or confirm is not None and not callable(confirm) or not callable(show_link)):
        return {"state": "error", "code": "link_confirmation_required"}
    return _Flow("link", locale, expected=expected_subject, confirm=confirm, show=show_link).run()


def run_desktop(*, signed_in_subject: str, show_link: Callable[[str], None], locale="en") -> dict:
    if not text(signed_in_subject, 200) or not callable(show_link):
        return {"state": "error", "code": "invalid_request"}
    return _Flow("desktop", locale, expected=signed_in_subject, show=show_link).run()


def reset_browser_token(callback_nonce):
    return hmac.digest(callback_nonce.encode('ascii'), b'avibe-rebind-reset-browser:v1', 'sha256').hex()


_RESET_REASONS = frozenset({'other_account', 'predecessor_unrecoverable'})


class _RebindFlow(_Flow):
    constrained = True
    reset_browser = None
    reset_confirmation = None
    reset_expiry = 0.

    def cancel(self):
        with self.lock:
            result = super().cancel()
            self.reset_browser = self.reset_confirmation = None
            return result

    def _execute(self):
        if self.mode != "redirect" or not self.admitted_owner or not loopback_origin(self.origin):
            raise ClaimError("local_operator_required")
        super()._execute()


class RedirectClaims:
    flow_type = _Flow

    def __init__(self):
        self._lock = threading.Lock()
        self._flow = None

    def start(self, origin: str, locale: str, *, admitted_owner: bool, on_bound=None):
        if not redirect_origin_allows_auto_accept(origin, admitted_owner=admitted_owner):
            raise ClaimError("link_required")
        with self._lock:
            if self._flow and self._flow.view["state"] not in ("bound", "error", "declined"):
                with self._flow.lock:
                    ticket = self._flow.ticket
                    raise ClaimError("claim_in_progress", 0 if ticket else 1,
                                     claim_id=ticket.id if ticket else None, expires_at=ticket.expiry if ticket else None)
            flow = self._flow = self.flow_type("redirect", locale, origin=origin, admitted_owner=admitted_owner, on_bound=on_bound)
            threading.Thread(target=flow.run, name="personal-claim", daemon=True).start()
        flow.ready.wait()
        with flow.lock:
            if flow.first is None:
                raise ClaimError(flow.view.get("code", "local_claim_unavailable"), flow.view.get("retry_after_seconds", 0))
            first, flow.first = flow.first, None
            return first

    def recognizes(self, claim_id):
        with self._lock:
            return bool(self._flow and self._flow.ticket and self._flow.ticket.id == claim_id)

    def _get(self, claim_id):
        with self._lock:
            f = self._flow
            if not f or not f.ticket or f.ticket.id != claim_id:
                raise ClaimError("claim_unavailable")
            return f

    def state(self, claim_id):
        try:
            f = self._get(claim_id)
        except ClaimError:
            view = None
        else:
            with f.lock:
                view = dict(f.view)
            if view["state"] not in ("bound", "error"):
                return view
        with config_file_lock():
            record = _journal_for(claim_id)
            if record and ("binding_disposition" in record["personal_claim"]) == self.flow_type.constrained:
                code = 'issuer_replay_refused' if remote_access._issuer_replay_refused(record) else 'pairing_recovery_required'
                return {"state": "error", "code": code}
            saved = _outcome(self.flow_type.constrained)
            bound = saved.get("bound", {})
            if bound.get("claim_id") == claim_id:
                identity = remote_access._pairing_identity_from_cloud(local._config().remote_access.vibe_cloud)
                if _binding_stamp(identity) != bound["generation"]:
                    return {"state": "error", "code": "local_binding_changed"}
                if view and view["state"] == "bound":
                    return view
                return {"state": "bound", **({"binding_disposition": bound["binding_disposition"]}
                        if self.flow_type.constrained and bound.get("binding_disposition") in ("new", "existing") else {})}
            if saved.get("claim_id") == claim_id and (view is None or view.get("code") == "pairing_recovery_required"):
                return {"state": "error", "code": "fresh_claim_required", "expires_at": saved.get("expires_at")}
        if view is not None:
            return {"state": "error", "code": "local_binding_changed"} if view["state"] == "bound" else view
        raise ClaimError("claim_unavailable")

    def cancel(self, claim_id):
        return self._get(claim_id).cancel()

    def callback_fields(self, claim_id, fields, cookies, origin, *, direct):
        # The live machine-side flow, not the URL, selects cookie and proof grammar.
        flow = self._get(claim_id)
        expected = {"claim_id", "state"}
        cookie_name = "avibe_claim_browser"
        if flow.constrained:
            if not direct:
                raise ClaimError("invalid_return")
            expected.add("callback_token")
            cookie_name = "avibe_rebind_browser"
        data = dict(fields)
        nonces = [value for name, value in cookies if name == cookie_name]
        if data.get("outcome") == "declined":
            # Cancelled at avibe.bot: the same shape for both kinds, never a callback token.
            expected = {"claim_id", "state", "outcome"}
        if len(fields) != len(expected) or set(data) != expected or data["claim_id"] != claim_id or len(nonces) != 1:
            raise ClaimError("invalid_return")
        if "outcome" in data:
            flow.declined_by_return(origin, nonces[0], data["state"])
        else:
            flow.callback(origin, nonces[0], data["state"], token=data.get("callback_token"))
        return cookie_name

    def callback(self, claim_id, origin, browser, state):
        self._get(claim_id).callback(origin, browser, state)


class RebindClaims(RedirectClaims):
    flow_type = _RebindFlow

    def state(self, claim_id):
        view = super().state(claim_id)
        if view.get("reason_code"):
            flow = self._get(claim_id)
            with config_file_lock():
                revision = local.claim_revision(local._config(), local._installation_id(), local._pending())
            if flow.refused_revision != revision:
                return {"state": "error", "code": "local_binding_changed"}
            if time.time() >= flow.ticket.deadline:
                return {"state": "error", "code": "expired"}
        return view

    def reset_request(self, claim_id, origin, browser, *, confirmation=None):
        """Preview never seals. Confirm requires the same browser, generation and unspent nonce."""
        from datetime import datetime, timezone
        from storage.mobile_store import MobileStore, mobile_store_path
        from vibe import binding_reset

        try:
            with admitted('reset'):
                # Serialize superseding start/cancel with the validation + seal, not the cleanup.
                with self._lock:
                    flow = self._flow
                    if flow is None or not flow.ticket or flow.ticket.id != claim_id:
                        raise ClaimError('reset_not_eligible')
                    with flow.lock, config_file_lock():
                        now = time.time()
                        if (not flow.returned or flow.accepting or flow.cancelled or origin != flow.origin
                                or not flow.reset_browser or not _matches(flow.reset_browser, browser)
                                or flow.view.get('code') != 'installation_unverified'
                                or flow.view.get('reason_code') not in _RESET_REASONS or now >= flow.ticket.deadline):
                            raise ClaimError('reset_not_eligible')
                        cfg = local._config()
                        if local.claim_revision(cfg, local._installation_id(), local._pending()) != flow.refused_revision:
                            raise ClaimError('local_binding_changed')
                        if confirmation is None:
                            connected = mobile_store_path().is_file() and MobileStore(existing_only=True).active_device() is not None
                            flow.reset_expiry = min(now + 120, flow.ticket.deadline)
                            flow.reset_confirmation = secrets.token_urlsafe(32)
                            return {'confirmation_id': flow.reset_confirmation, 'health_connected': connected,
                                    'expires_at': datetime.fromtimestamp(flow.reset_expiry, timezone.utc).isoformat()}
                        if not flow.reset_confirmation or not _matches(flow.reset_confirmation, confirmation):
                            raise ClaimError('reset_not_eligible')
                        if now >= flow.reset_expiry:
                            raise ClaimError('confirmation_expired')
                        binding_reset._seal(cfg)
                        flow.reset_confirmation = flow.reset_browser = None
                try:
                    return binding_reset.resume()
                except Exception:
                    return {'state': 'resetting'}  # The sealed marker retains exclusive ownership.
        except OperationBusy:
            raise ClaimError('operation_in_progress') from None

    def overview(self):
        from vibe import binding_health, binding_reset, instance_address
        from vibe.binding_admission import reset_in_progress

        # Read only: no credential probe, identity initialization, reconciliation,
        # connector status cleanup or grant observation on a page refresh.
        with config_file_lock():
            if reset_in_progress():
                try:
                    binding_reset._verify_stage(binding_reset._read(), local._config())
                    state = 'resetting'
                except (ClaimError, OSError, ValueError):
                    state = 'reset_repair_required'
                return {'binding_state': state, 'can_start': False, 'unavailable_reason': state,
                        'next_action': None, 'reason_code': None, 'attempt': None,
                        'connector_still_running': remote_access._is_cloudflared_pid(remote_access._read_pid())}
            cfg, pending = local._config(), local._pending()
            cloud = cfg.remote_access.vibe_cloud
            bound = bool(cloud.instance_id and cloud.instance_secret and cloud.backend_url)
            refused = remote_access._issuer_replay_refused(pending)
            recovering = pending is not None and not refused
            state = ("pairing_recovery_required" if recovering else
                     "binding_rejected" if bound and binding_health.rejected_at(cfg) is not None else
                     "bound" if bound else "unpaired")
            if not bound and any((cloud.instance_id, cloud.instance_secret, cloud.client_id, cloud.tunnel_token, cloud.session_secret)):
                raise ClaimError("local_binding_unavailable")
            local._installation_id()  # A damaged correlation file is not an unpaired computer.
        with self._lock:
            flow = self._flow
            claim_id = flow.ticket.id if flow and flow.ticket else None
        attempt = self.state(claim_id) if claim_id else None
        active = flow and (not attempt or attempt["state"] not in ("error", "bound", "declined"))
        unavailable = ("pairing_recovery_required" if recovering else "not_bound" if not bound else
                       "operation_in_progress" if active or instance_address._journal(instance_address._binding_from_config(cfg)) is not None else None)
        eligible = (unavailable is None and (attempt or {}).get('reason_code') in _RESET_REASONS
                    and flow and flow.reset_browser is not None)
        return {"binding_state": state, "connector_still_running": remote_access._is_cloudflared_pid(remote_access._read_pid()),
                "can_start": unavailable is None, "unavailable_reason": unavailable, "attempt": attempt,
                "next_action": "confirm_as_new" if eligible else "rebind" if unavailable is None else None,
                "reason_code": (attempt or {}).get("reason_code") or ('issuer_replay_refused' if refused else None)}


redirect_claims = RedirectClaims()
rebind_claims = RebindClaims()


def callback_service(claim_id):
    services = [svc for svc in (redirect_claims, rebind_claims) if svc.recognizes(claim_id)]
    if len(services) != 1:
        raise ClaimError("claim_unavailable")
    return services[0]
