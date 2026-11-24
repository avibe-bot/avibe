"""The page a failed sign-in lands on: the signed-out case has its own text, the rest keep theirs."""

from __future__ import annotations

import html
import re
import urllib.parse

import pytest

from tests.test_remote_access_vibe_cloud import _config, _session_claims
from vibe import remote_access, ui_server
from vibe.i18n import t

ORIGIN = "https://alex.avibe.bot"

SIGNED_OUT = {
    "zh": ("这台设备已被退出", "请过几分钟再登录。一直不行的话，先在 avibe.bot 退出登录，再回来登录。"),
    "en": ("This device was signed out",
           "Please sign in again in a few minutes. If it still does not work, sign out at avibe.bot first, "
           "then come back and sign in."),
}
OTHER_CODES = ["invalid_oauth_state", "oauth_exchange_failed", "remote_pairing_mismatch", "oauth_time_mismatch",
               "invalid_oauth_nonce"]


@pytest.fixture
def install():
    config = _config()
    config.save()
    return config


def shown(page):
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S).group(1).strip()
    return html.unescape(title), html.unescape(heading), html.unescape(page)


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_a_signed_out_device_is_told_so_in_its_language(lang):
    title, body = SIGNED_OUT[lang]
    page_title, heading, text = shown(ui_server._render_oauth_error_html("account_session_signed_out", retry_href="/", lang=lang))
    assert page_title == heading == title
    assert body in text
    # The generic sentence is gone, the way to try again is not.
    assert t("remote_access.oauth_error.default_body", lang) not in text
    assert t("remote_access.oauth_error.sign_in_again", lang) in text


@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("code", [*OTHER_CODES, "some_other_failure", ""])
def test_every_other_failure_keeps_its_page(lang, code):
    key = code if code in OTHER_CODES else "default"
    page_title, heading, text = shown(ui_server._render_oauth_error_html(code, retry_href="/", lang=lang))
    assert page_title == heading == t(f"remote_access.oauth_error.{key}_title", lang)
    assert t(f"remote_access.oauth_error.{key}_body", lang) in text
    for sentence in SIGNED_OUT[lang]:
        assert sentence not in text


def test_only_the_signed_out_case_was_added():
    assert ui_server._OAUTH_ERROR_PAGE_CODES == {*OTHER_CODES, "account_session_signed_out"}


@pytest.mark.parametrize(("accept", "lang"), [("zh-CN,zh;q=0.9", "zh"), ("en-US", "en"), ("", "en")])
def test_the_callback_answers_a_signed_out_device_with_that_page(install, monkeypatch, accept, lang):
    from storage.device_account_signout import AccountSessionSignedOut

    monkeypatch.setattr(ui_server, "_auth_rate_limited", lambda: False)
    browser = ui_server.app.test_client()
    started = browser.get("/auth/login?next=/sessions", base_url=ORIGIN)
    assert started.status_code == 302
    asked = urllib.parse.parse_qs(urllib.parse.urlsplit(started.headers["Location"]).query)
    monkeypatch.setattr(remote_access, "exchange_oauth_code", lambda *_a, **_k: {
        "claims": {"nonce": asked["nonce"][0], "email": "owner@test.invalid", "sub": "owner"},
        "session_claims": _session_claims(install, role="owner"),
    })

    def refused(*_args, **_kwargs):
        # What the registry answers for an account session that was signed out here.
        raise AccountSessionSignedOut("account_session_signed_out")

    monkeypatch.setattr(remote_access, "make_session_cookie", refused)
    answer = browser.get("/auth/callback?code=fixture&state=" + urllib.parse.quote(asked["state"][0]), base_url=ORIGIN,
                         headers={"Accept-Language": accept} if accept else {})
    title, body = SIGNED_OUT[lang]
    page_title, heading, text = shown(answer.text)
    assert answer.status_code == 403 and answer.headers["Cache-Control"] == "no-store"
    assert page_title == heading == title and body in text
    assert 'href="/sessions"' in answer.text
    assert not any(remote_access.SESSION_COOKIE_NAME + "=" in cookie and "Max-Age=0" not in cookie
                   for cookie in answer.headers.getlist("Set-Cookie"))
