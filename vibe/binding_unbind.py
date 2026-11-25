"""Decisions for the original, authenticated hosted unbind response only."""
import hashlib
import http.client
import json
from uuid import UUID

from config import paths


def root_stamp() -> str:
    """Fence a moved/replaced home, even if all the credential files were copied."""
    root = paths.get_vibe_remote_dir().resolve(strict=True)
    state = paths.get_state_dir().resolve(strict=True)
    if not state.is_relative_to(root):
        raise ValueError('untrusted_state_root')
    identities = []
    for directory in (root, state):
        info = directory.stat()
        identities.append((str(directory), info.st_dev, info.st_ino))
    return hashlib.sha256(json.dumps(identities).encode()).hexdigest()


def valid_operation(operation) -> bool:
    try:
        value = UUID(operation)
        return value.version == 4 and str(value) == operation
    except (TypeError, ValueError, AttributeError):
        return False


class _DuplicateField(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField()
        result[key] = value
    return result


def classify_receipt(status: int, body: bytes, operation: str, *, accepted: bool) -> str:
    """Never infer a deletion from failure or an admission from HTTP status alone.

    The caller owns HTTPS provenance, the bound on transport reads, and the
    generation check. `accepted` means a matching 202 was already journaled.
    """
    if not valid_operation(operation):
        return 'repair'
    # Response table v2.2: what is not the issuer's own JSON proves nothing about
    # admission, so it is retried with the same operation and key. Rate limits and
    # redirects, and any reply without a JSON object (gateway pages, an old
    # service's HTML 404, a truncated body), are such answers.
    if status == 429 or 300 <= status < 400 or len(body) > 4096:
        return 'retry'
    try:
        data = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object)
    except _DuplicateField:
        return 'repair'  # Parsed JSON that names a field twice: ambiguous, never acted on or resent.
    except (UnicodeError, ValueError, RecursionError):
        return 'retry'
    if not isinstance(data, dict):
        return 'retry'
    if status == 503 and data == {'error': 'unbind_unavailable'}:
        return 'retry'
    # From here on the issuer answered with a JSON object; outside the contract it needs local attention.
    if status == 200 and data == {'status': 'unbound', 'operation_id': operation}:
        return 'unbound'
    if status == 202 and data == {'status': 'pending', 'operation_id': operation}:
        return 'pending'
    if not accepted:
        if status == 401 and data == {'error': 'unbind_not_applied'}:
            return 'invalid_credential'
        if status == 403 and data == {'error': 'unbind_ineligible', 'operation_id': operation}:
            return 'ineligible'
    return 'repair'


def _result_path():
    return paths.get_state_dir() / 'remote-unbind-result.json'


def clear_result() -> None:
    _result_path().unlink(missing_ok=True)


def save_result(generation: str, state: str, reason: str | None = None, *, operation: str | None = None) -> None:
    from config.atomic_io import write_atomic
    write_atomic(_result_path(), json.dumps({'generation': generation, 'state': state, 'reason': reason,
                                            'operation': operation}))


def device_state(config) -> dict | None:
    """Owner-only readback. Reading a marker never resumes it or contacts its issuer."""
    from vibe import binding_admission as gate, binding_reset as reset
    from vibe.personal_claim_client import ClaimError
    if config is None:
        return None
    if not gate.reset_in_progress():
        return last_result(config)
    try:
        record = reset._read()
        if record['schema'] != 2:
            return None
        reset._verify_stage(record, config)
    except (ClaimError, OSError, ValueError):
        return {'state': 'reset_repair_required'}
    return {'state': 'unbinding'}


def last_result(config, *, operation: str | None = None) -> dict | None:
    """A stale result is not evidence of anything about this binding."""
    from vibe.binding_reset import _stamp
    path = _result_path()
    if path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get('generation') != _stamp(config.remote_access.vibe_cloud):
        return None
    if operation is not None and (not valid_operation(operation) or data.get('operation') != operation):
        return None
    if data.get('state') == 'unbound' and data.get('reason') is None:
        return {'state': 'unbound'}
    if data.get('state') == 'not_unbound' and data.get('reason') in ('invalid_credential', 'ineligible'):
        return {'state': 'not_unbound', 'reason_code': data['reason']}
    return None


def send_original(config, operation: str, *, accepted: bool) -> str:
    """One attempt against the hosted issuer. Do not replay inside a transport fallback."""
    from vibe import remote_access

    cloud = config.remote_access.vibe_cloud
    if (cloud.backend_url.rstrip('/') != 'https://avibe.bot' or not cloud.instance_id or
            not cloud.instance_secret or not valid_operation(operation)):
        return 'repair'
    target, reason = remote_access._normalize_pairing_backend_url('https://avibe.bot')
    if target is None:
        return 'retry' if reason == 'pairing_backend_unresolvable' else 'repair'
    try:
        proxy = remote_access._validated_backend_proxy_url(target.base_url)
        if target.requires_proxy and not proxy:
            return 'retry'
        for conn in remote_access._validated_backend_connections(target, 8, proxy):
            try:
                try:
                    conn.connect()
                except (OSError, http.client.HTTPException):
                    continue  # No request sent on this connection.
                conn.request('POST', '/api/v1/paired-instance/unbind',
                             body=json.dumps({'operation_id': operation}).encode(), headers={
                                 'Host': target.host_header, 'Content-Type': 'application/json',
                                 'Accept': 'application/json', 'Cache-Control': 'no-store',
                                 'X-Vibe-Device-Secret': cloud.instance_secret})
                response = conn.getresponse()
                if ((response.getheader('Content-Type') or '').partition(';')[0].strip().lower() != 'application/json'
                        or (response.getheader('Age') or '0').strip() != '0'):
                    return 'retry'  # An intermediary or a cache answered, not the issuer.
                return classify_receipt(response.status, response.read(4097), operation, accepted=accepted)
            finally:
                conn.close()
    except (OSError, http.client.HTTPException):
        pass  # Ambiguous send; retry only through the original durable intent.
    return 'retry'
