"""Server-only caller for the paired issuer's exact-session revoke contract."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
from urllib.parse import quote, urlsplit

from vibe import remote_access

_REF = re.compile(r"asr1_[A-Za-z0-9_-]{43}\Z", re.ASCII)


def verified_reference(claims):
    value = claims.get("avibe_account_session_ref")
    if (claims.get("vibe_instance_access_source") == "owner"
            and claims.get("vibe_instance_role") == "owner"
            and isinstance(value, str) and _REF.fullmatch(value)):
        return value
    return None


def pairing_epoch(config):
    """Never retarget work to a replacement secret, cookie binding, backend or instance."""
    if config is None:
        return None
    cloud = config.remote_access.vibe_cloud
    if not cloud.instance_secret or not cloud.session_secret or not cloud.instance_id:
        return None
    identity = [cloud.backend_url.rstrip('/'), cloud.instance_id, cloud.instance_secret, cloud.session_secret]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def dispatch(credentials, reference):
    """Exactly one request, no redirects, no unbounded bodies/errors, no target in URL."""
    base, instance, secret = credentials
    if not _REF.fullmatch(reference) or instance in {'.', '..'}:
        return 400
    try:
        target, error = remote_access._normalize_pairing_backend_url(base)
        if target is None:
            return None if error == 'pairing_backend_unresolvable' else 400
        path = (urlsplit(target.base_url).path.rstrip('/') + '/api/v1/instances/' + quote(instance, safe='')
                + '/account-sessions/revoke')
        proxy = remote_access._validated_backend_proxy_url(target.base_url)
        if target.requires_proxy and not proxy:
            return None
        conn = remote_access._validated_backend_connection(target.connect_hosts[0], target, 5.0, proxy)
        try:
            conn.request('POST', path, body=json.dumps({'account_session_ref': reference}).encode(),
                         headers={'Host': target.host_header, 'Content-Type': 'application/json',
                                  'X-Vibe-Device-Secret': secret})
            return conn.getresponse().status
        finally:
            conn.close()
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        return None
