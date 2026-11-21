"""The fixed answers a managed name that is not the active one can get.

Rules E1-E8 of ``docs/plans/instance-address-change.md``. Nothing here reads
application state or the request body: a cleaning response is built from the
request's own cookie names and path, and tells a visitor only that this Avibe
has a new address.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re

from vibe.i18n import get_supported_languages, t
from vibe.instance_address import CLEARED_REPORT_PATH, RETIRED_HEADER, RETIRED_HEADER_VALUE

# Never ``"cookies"`` and never ``"*"``: either would clear cookies for the
# whole registered domain, signing the visitor out of avibe.bot and of their
# other instances.
CLEAR_SITE_DATA = '"cache", "storage"'
# For the report an open page posts after it has emptied its own storage: the
# HTTP cache is the one thing its script cannot reach, and clearing storage
# again would take the write fence it just left.
CLEAR_HTTP_CACHE = '"cache"'

_LOCAL_DATABASES = ("avibe-local-store", "avibe-local-snapshots")
# Every counter by which a tab of the old app knows that what it captured is
# void (``ui/src/lib/signOutGeneration.ts``, the local store's epoch, the boot
# session's generation). Clearing resets them; a tab still open would then
# write its data back. They are left at the time of the clearing, a value no
# tab can hold. The marker tells such a tab to ask the instance and, on a
# retired address, to clear itself and leave too (``addressRetired.ts``): the
# counters stop only the stores that have one.
WRITE_FENCE_KEYS = (
    "avibe.sign-out-generation.v1",
    "avibe.local-store.epoch.v1",
    "avibe.boot-session.generation.v1",
)
RETIRED_MARKER_KEY = "avibe.address-retired.v1"

# The fallback for a browser that ignores the header. It reports only when
# every step succeeded and the page was given a receipt.
CLEANING_SCRIPT = (
    "(async function(){var ok=true,m=document.querySelector('meta[name=\"avibe-address-receipt\"]'),r=m&&m.content;"
    "try{localStorage.clear();var f=String(Date.now());"
    + json.dumps([*WRITE_FENCE_KEYS, RETIRED_MARKER_KEY]) + ".forEach(function(k){localStorage.setItem(k,f)})}catch(e){ok=false}"
    "try{sessionStorage.clear()}catch(e){ok=false}"
    "try{var names=" + json.dumps(list(_LOCAL_DATABASES)) + ";"
    "if(indexedDB.databases){(await indexedDB.databases()).forEach(function(d){"
    "if(d&&d.name&&names.indexOf(d.name)<0)names.push(d.name)})}"
    "await Promise.all(names.map(function(n){return new Promise(function(res){"
    "var q=indexedDB.deleteDatabase(n);q.onsuccess=function(){res()};"
    "q.onerror=function(){ok=false;res()};q.onblocked=function(){ok=false;res()}})}))}catch(e){ok=false}"
    "try{if(self.caches){var keys=await caches.keys();"
    "await Promise.all(keys.map(function(k){return caches.delete(k)}))}}catch(e){ok=false}"
    "try{if(navigator.serviceWorker){var regs=await navigator.serviceWorker.getRegistrations();"
    "for(var i=0;i<regs.length;i++){try{var s=await regs[i].pushManager.getSubscription();"
    "if(s)await s.unsubscribe()}catch(e){}await regs[i].unregister()}}}catch(e){ok=false}"
    "try{document.cookie.split(';').forEach(function(c){var n=c.split('=')[0].trim();"
    "if(n){document.cookie=n+'=; Max-Age=0; Path=/';document.cookie=n+'=; Max-Age=0; Path=/; Secure'}})}catch(e){}"
    "if(ok&&r){try{await fetch(" + json.dumps(CLEARED_REPORT_PATH) + ",{method:'POST',body:r,"
    "credentials:'omit',cache:'no-store'})}catch(e){}}})();"
)

# Served in place of ``/push-sw.js``: a worker that wakes without any page
# (a queued push, an update check) removes what it can reach and then itself.
REPLACEMENT_WORKER = (
    "self.addEventListener('install',function(){self.skipWaiting()});"
    "self.addEventListener('activate',function(event){event.waitUntil((async function(){"
    "try{var s=await self.registration.pushManager.getSubscription();if(s)await s.unsubscribe()}catch(e){}"
    "try{var keys=await caches.keys();await Promise.all(keys.map(function(k){return caches.delete(k)}))}catch(e){}"
    "try{var names=" + json.dumps(list(_LOCAL_DATABASES)) + ";"
    "if(indexedDB.databases){(await indexedDB.databases()).forEach(function(d){"
    "if(d&&d.name&&names.indexOf(d.name)<0)names.push(d.name)})}"
    "names.forEach(function(n){try{indexedDB.deleteDatabase(n)}catch(e){}})}catch(e){}"
    "await self.registration.unregister()})())});"
    "self.addEventListener('push',function(){});"
)

_SCRIPT_HASH = base64.b64encode(hashlib.sha256(CLEANING_SCRIPT.encode("utf-8")).digest()).decode("ascii")
CONTENT_SECURITY_POLICY = (
    f"default-src 'none'; script-src 'sha256-{_SCRIPT_HASH}'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# Cookies this origin sets under a fixed name, with the path each was set on.
_FIXED_COOKIES = (
    ("__Host-vibe_remote_session", "/"),
    ("__Host-vibe_remote_oauth", "/"),
    ("__Host-vibe_oauth_device", "/"),
    ("vibe_csrf_token", "/"),
    ("avibe_claim_browser", "/remote/claim/callback"),
)
_COOKIE_NAME = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}\Z")
_COOKIE_PATH = re.compile(r"\A/[!-+\--:<-~]*\Z")  # visible ASCII without ';' and ','
_MAX_REQUEST_COOKIES = 32
_MAX_PATHS = 12
_MAX_PATH_BYTES = 256
_MAX_COOKIE_HEADERS = 160
# Edges cap a response's headers in total (Cloudflare: 128 KB); stay far below.
_MAX_COOKIE_HEADER_BYTES = 16 * 1024
_EXPIRED = "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure"


def _request_cookie_names(cookie_header: str) -> list[str]:
    names: list[str] = []
    for part in cookie_header.split(";"):
        name = part.split("=", 1)[0].strip()
        if name and name not in names and _COOKIE_NAME.fullmatch(name):
            names.append(name)
            if len(names) >= _MAX_REQUEST_COOKIES:
                break
    return names


def _path_prefixes(raw_path: str) -> list[str]:
    """Every path a cookie sent on this request can have been set on.

    A request does not say which path its cookies belong to, and some are set
    with a trailing slash, so both spellings of each directory are listed.
    """

    path = raw_path.split("?", 1)[0]
    prefixes = ["/"]
    if not path.startswith("/"):
        return prefixes
    current = ""
    for segment in [part for part in path.split("/") if part]:
        current = f"{current}/{segment}"
        if len(current) >= _MAX_PATH_BYTES:
            break  # a cookie this deep is left to its own expiry; the response must stay deliverable
        for candidate in (current, f"{current}/"):
            if _COOKIE_PATH.fullmatch(candidate) and candidate not in prefixes:
                prefixes.append(candidate)
        if len(prefixes) >= _MAX_PATHS:
            break
    return prefixes[:_MAX_PATHS]


def expiring_cookies(raw_path: str, cookie_header: str) -> list[str]:
    """``Set-Cookie`` values that expire, as host-only cookies, what this request shows.

    The fixed names, then every cookie the request itself carries at each
    directory prefix of its path. Without a ``Domain`` none of these can touch
    a cookie of the parent domain.
    """

    values = [f"{name}=; Path={path}; {_EXPIRED}" for name, path in _FIXED_COOKIES]
    seen = set(_FIXED_COOKIES)
    size = sum(len(value) for value in values)
    names = _request_cookie_names(cookie_header)
    for path in _path_prefixes(raw_path):
        for name in names:
            # A ``__Host-`` cookie exists only at ``Path=/``.
            if (name, path) in seen or (name.startswith("__Host-") and path != "/"):
                continue
            seen.add((name, path))
            value = f"{name}=; Path={path}; {_EXPIRED}"
            size += len(value)
            if len(values) >= _MAX_COOKIE_HEADERS or size > _MAX_COOKIE_HEADER_BYTES:
                return values
            values.append(value)
    return values


def cleanup_headers(
    raw_path: str, cookie_header: str, *, content_type: str | None, clear: str = CLEAR_SITE_DATA
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"clear-site-data", clear.encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-robots-tag", b"noindex"),
        (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
        (RETIRED_HEADER.lower().encode("ascii"), RETIRED_HEADER_VALUE.encode("ascii")),
    ]
    if content_type:
        headers.append((b"content-type", content_type.encode("ascii")))
    headers.extend((b"set-cookie", value.encode("ascii")) for value in expiring_cookies(raw_path, cookie_header))
    return headers


def response_language(accept_language: str) -> str:
    supported = set(get_supported_languages())
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if not tag:
            continue
        if tag in supported:
            return tag
        primary = tag.split("-")[0]
        if primary in supported:
            return primary
    return "en"


def cleaning_document(*, language: str, control_plane_origin: str, receipt: str | None) -> bytes:
    """One approved line and one link to the control plane. Never the new name."""

    line = html.escape(t("remote_access.address_retired.line", language))
    origin = control_plane_origin or "https://avibe.bot"
    label = html.escape(origin.split("://", 1)[-1])
    receipt_meta = (
        f'<meta name="avibe-address-receipt" content="{html.escape(receipt, quote=True)}">' if receipt else ""
    )
    return (
        f'<!doctype html><html lang="{html.escape(language, quote=True)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex">{receipt_meta}<title>Avibe</title></head>'
        f'<body><p>{line}</p><p><a href="{html.escape(origin, quote=True)}" rel="noreferrer">{label}</a></p>'
        f"<script>{CLEANING_SCRIPT}</script></body></html>"
    ).encode("utf-8")


REFUSAL_BODY = b'{"ok":false,"error":"remote_access_host_mismatch"}'
DEVICE_REFUSAL_BODY = b'{"ok":false,"code":"device_unauthorized","error":"device_unauthorized"}'
