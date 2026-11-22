"""The page a bound Avibe answers with at its old network address.

``docs/plans/bound-network-position-trust.md``: once an Avibe is bound to an
account, a device on the same network no longer gets the Workbench by opening
the computer's network address. A document navigation there gets this page.

It is the same bytes for every visitor and every install: nothing here reads
the request, the configuration, the connector or the pairing. It says only
that this address no longer opens Avibe and names the two ways on: the fixed
account page at avibe.bot, or Avibe on the computer that runs it. No script,
no external asset, no redirect, no link other than that fixed one.
"""

from __future__ import annotations

import base64
import hashlib
import html

from vibe.i18n import t

# The account's own list. Fixed: never an instance name, never built from the request.
ACCOUNT_URL = "https://avibe.bot/app"

# The look of the other server-rendered pages (``_render_oauth_error_html``).
_STYLE = (
    ":root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"
    '"Segoe UI",sans-serif;background:#f6f7f9;color:#172033}'
    "body{margin:0;min-height:100vh}"
    "main{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 18px;"
    "box-sizing:border-box}"
    "section{width:min(460px,100%);border:1px solid rgba(23,32,51,.12);border-radius:18px;"
    "background:rgba(255,255,255,.96);padding:clamp(28px,6vw,40px);box-shadow:0 24px 80px rgba(23,32,51,.10);"
    "box-sizing:border-box;text-align:center}"
    ".eyebrow{color:#526078;font-size:13px;font-weight:760;letter-spacing:.12em;text-transform:uppercase}"
    "h1{margin:14px 0 0;font-size:clamp(24px,6vw,32px);line-height:1.2;letter-spacing:0}"
    "p{margin:14px 0 0;line-height:1.65;color:#526078}"
    ".action{margin-top:26px}"
    ".action a{display:inline-block;height:44px;padding:0 24px;border-radius:12px;background:#0f172a;color:#fff;"
    "font:700 15px/44px Inter,ui-sans-serif,system-ui;text-decoration:none}"
    ".action a:hover{background:#1e293b}"
    ".local{margin-top:26px;padding-top:20px;border-top:1px solid rgba(23,32,51,.10);font-size:14px}"
)
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")

# The inline style is the only thing allowed to load, by its hash.
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}


def document(language: str) -> str:
    """The whole page in ``language``; the caller picks it from ``Accept-Language``."""

    def text(key: str) -> str:
        return html.escape(t(f"remote_access.bound_address.{key}", language))

    title = text("title")
    return (
        f'<!doctype html><html lang="{html.escape(language, quote=True)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{title}</title><style>{_STYLE}</style></head>"
        '<body><main><section><div class="eyebrow">Avibe</div>'
        f"<h1>{title}</h1><p>{text('body')}</p>"
        f'<div class="action"><a href="{ACCOUNT_URL}" rel="noreferrer">{text("account_action")}</a></div>'
        f'<p class="local">{text("local")}</p>'
        "</section></main></body></html>"
    )
