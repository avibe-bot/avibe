"""Atomic account-work selection and audience-scoped, non-secret sign-out receipts."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import uuid

from sqlalchemy import and_, literal, or_, select

from storage.models import (
    remote_account_refs as refs, remote_account_revocations as jobs, remote_account_pairings as pairings,
    remote_device_sessions as sessions, remote_signout_receipts as receipts, remote_signout_items as items,
    remote_signout_receipt_roles as receipt_roles, state_meta,
)

RECEIPT_TTL = 7 * 86400
# A session the owner's own "sign out this device" ended, for one later sign-in to finish:
# see ``device_registry.record_session``. Kept beside the tombstone, never in it.
OWNER_SIGNED_OUT_PREFIX = 'remote_access.owner_signed_out.v1:'


class AccountSessionSignedOut(PermissionError):
    pass


class AccountSignInEnded(AccountSessionSignedOut):
    """This sign-in finished an owner's sign-out of the browser's old session: refused, and the
    browser's old cookie is cleared."""


def enroll(conn, *, sid, subject, binding, epoch, reference, expires_at):
    if not reference or not epoch:
        return
    digest = hashlib.sha256(json.dumps([epoch, subject, reference]).encode()).hexdigest()
    if conn.scalar(select(jobs.c.digest).where(jobs.c.digest == digest)):
        raise AccountSessionSignedOut('account_session_signed_out')
    conn.execute(refs.insert().values(sid=sid, cookie_binding=binding, epoch=epoch, digest=digest, reference=reference, expires_at=expires_at))


def revoke_late(conn, *, epoch, subject, reference, now):
    """Queue avibe.bot's sign-out of one exact account session, met at a later sign-in.

    Never restarts an existing job, whatever its state. With the pairing already refused there is
    nothing to send, but the digest is written so that the same session is still refused here.
    """
    digest = hashlib.sha256(json.dumps([epoch, subject, reference]).encode()).hexdigest()
    if conn.scalar(select(jobs.c.digest).where(jobs.c.digest == digest)):
        return
    values = dict(digest=digest, epoch=epoch, created_at=now, attempts=0, answers=0)
    if conn.scalar(select(pairings.c.refused).where(pairings.c.epoch == epoch)):
        conn.execute(jobs.insert().values(**values, reference=None, status='refused', reason='credential', next_at=None))
    else:
        conn.execute(jobs.insert().values(**values, reference=reference, status='pending', reason='queued', next_at=now))


def _job(conn, row, now):
    """Return the current immutable outcome or join/create exactly one dispatch."""
    if row.prior_status:
        return row.prior_status, row.prior_reason, None
    if conn.scalar(select(pairings.c.refused).where(pairings.c.epoch == row.epoch)):
        return 'refused', 'credential', None
    old = conn.execute(select(jobs.c.status, jobs.c.reason).where(jobs.c.digest == row.digest)).first()
    if old and old.status != 'unconfirmed':
        return old.status, old.reason, row.digest if old.status == 'pending' else None
    values = dict(epoch=row.epoch, reference=row.reference, status='pending', reason='queued',
                  created_at=now, next_at=now, attempts=0, answers=0)
    if old:  # A different explicit action targets a still-enrolled sibling; prior receipts remain terminal.
        conn.execute(jobs.update().where(jobs.c.digest == row.digest).values(**values))
    else:
        conn.execute(jobs.insert().values(digest=row.digest, **values))
    return 'pending', 'queued', row.digest


def receipt_group(viewer_subject, subject, login_role):
    """Whose device a receipt was, for this viewer: ``mine``, ``others`` or unknown (``None``).

    The same rule as the live device list, applied to what the registry knew
    when the device was signed out: a signed-in viewer's own devices are those
    of their account; for a viewer on this computer itself (no account) they
    are the devices that signed in as Owner. Unknown when the receipt names no
    account or was written before the role was recorded. Never from a name or a kind.
    """

    if not subject or not login_role:
        return None
    if viewer_subject:
        return 'mine' if subject == viewer_subject else 'others'
    return 'mine' if login_role == 'owner' else 'others'


def stage(conn, *, binding, actor, delegate, caller, keeper_device, target, selection, push_devices, now, receipt_scope=None):
    """Called under the same BEGIN IMMEDIATE as the local tombstone writes."""
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    joined = sessions.outerjoin(refs, refs.c.sid == sessions.c.session_id)
    # The device list's own test for a session it shows (``device_registry.list_devices``).
    listed = and_(sessions.c.cookie_exp > now, ~select(state_meta.c.key).where(
        state_meta.c.key == literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id).exists())
    rows = conn.execute(select(sessions.c.device_id, sessions.c.subject, sessions.c.kind, sessions.c.display_name,
                               sessions.c.login_role, listed.label('listed'),
                               refs.c.status.label('prior_status'), refs.c.reason.label('prior_reason'),
                               refs.c.reference, refs.c.digest, refs.c.epoch).select_from(joined).where(selection)
                        .order_by(sessions.c.last_active_at.desc(), sessions.c.session_id)).all()
    kept, unknown_keeper = set(), False
    if caller and target != keeper_device:
        keeper = select(refs.c.digest).select_from(joined).where(sessions.c.binding == binding,
                      sessions.c.subject == actor, sessions.c.device_id == keeper_device,
                      sessions.c.cookie_exp > now, sessions.c.expires_at > now)
        if target is not None:
            keeper = keeper.where(sessions.c.session_id == caller)
        for digest, in conn.execute(keeper):
            if digest is None:
                unknown_keeper = target is None
            else:
                kept.add(digest)
    outcomes, seen = defaultdict(Counter), defaultdict(set)
    # A stopped push registration carries (kind, name) and, when its account was
    # proven, the role that account signed in with.
    labels = {key: tuple(label[:2]) for key, label in push_devices.items()}
    proven = {key: (label[2] if len(label) > 2 else None) for key, label in push_devices.items()}
    shown, newest = {}, {}
    cache = {}
    for row in rows:
        key = row.device_id, row.subject
        if key not in outcomes:
            labels[key] = row.kind, row.display_name
        # Newest first, as the list orders them.
        newest.setdefault(key, row.login_role)
        if row.listed:
            shown.setdefault(key, row.login_role)
        if row.digest and row.digest in seen[key]:
            continue
        seen[key].add(row.digest)
        if not delegate or (actor is not None and actor != row.subject):
            reason = 'not_eligible'
        elif row.digest is None:
            reason = 'no_reference'
        elif unknown_keeper:
            reason = 'keeper_unknown'
        elif row.digest in kept:
            reason = 'shared_sign_in'
        else:
            if row.digest not in cache:
                cache[row.digest] = _job(conn, row, now)
            outcomes[key][cache[row.digest]] += 1
            continue
        outcomes[key][('not_applicable', reason, None)] += 1
    for key in push_devices:
        if key not in outcomes:
            outcomes[key][('not_applicable', 'no_reference', None)] = 1
    parents, children, recorded, output = [], [], [], []
    for key, counts in outcomes.items():
        device, subject = key
        kind, name = labels[key]
        op = uuid.uuid4().hex
        expiry = None if any(job for _, _, job in counts) else now + RECEIPT_TTL
        # The role the list went by: the newest session it showed; with none, the
        # proven notification registration it showed in its place; and for a device
        # it did not show at all, the newest session being ended.
        role = (shown.get(key) or proven.get(key) or newest.get(key)) if subject else None
        parents.append(dict(id=op, binding=receipt_scope or binding, subject=subject, device_id=device, kind=kind, display_name=name,
                            created_at=now, expires_at=expiry))
        if role:
            recorded.append(dict(receipt=op, login_role=role))
        aggregate = Counter()
        for (status, reason, job), count in counts.items():
            aggregate[status, reason] += count
            children.append(dict(receipt=op, status=status, reason=reason, job=job, count=count))
        output.append(_public(op, device, kind, name, aggregate, receipt_group(actor, subject, role)))
    if parents:
        conn.execute(receipts.insert(), parents)
        conn.execute(items.insert(), children)
    if recorded:
        conn.execute(receipt_roles.insert(), recorded)
    return output


def _public(op, device, kind, name, counts, group):
    return dict(operation_id=op, device_id=device, kind=kind, display_name=name, group=group, local_status='ended',
                account_results=[dict(status=s, reason=r, count=n) for (s, r), n in sorted(counts.items())])


def recent(conn, binding, subject, owner, now):
    query = select(receipts.c.id, receipts.c.device_id, receipts.c.kind, receipts.c.display_name,
                   receipts.c.subject, receipt_roles.c.login_role).select_from(
        receipts.outerjoin(receipt_roles, receipt_roles.c.receipt == receipts.c.id)).where(
        receipts.c.binding == binding, or_(receipts.c.expires_at.is_(None), receipts.c.expires_at > now))
    if not owner:
        query = query.where(receipts.c.subject == subject)
    parents = conn.execute(query.order_by(receipts.c.created_at.desc(), receipts.c.id.desc()).limit(100)).all()
    groups = defaultdict(Counter)
    if parents:
        for op, status, reason, n in conn.execute(select(items.c.receipt, items.c.status, items.c.reason, items.c.count)
                                                  .where(items.c.receipt.in_([p.id for p in parents]))):
            groups[op][status, reason] += n
    # ``subject`` is the viewer's account, or None for a viewer on this computer itself.
    return [_public(p.id, p.device_id, p.kind, p.display_name, groups[p.id],
                    receipt_group(subject, p.subject, p.login_role)) for p in parents]
