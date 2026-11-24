"""What a person reads at a bound Avibe's old network address, and what a visitor cannot learn from it."""

from __future__ import annotations

import base64
import hashlib
import html
import re

import pytest

from config.v2_config import V2Config
from vibe import ui_server

LAN = "http://192.168.2.3:15131"
LAN_PEER = {"REMOTE_ADDR": "192.168.2.4"}

SAID = {
    "zh": (
        "这个地址不再打开 Avibe",
        "这个 Avibe 已绑定账号，其他设备要先登录。在 avibe.bot 登录后，从你的列表里打开它。",
        "去 avibe.bot",
        "也可以在运行 Avibe 的那台电脑上直接打开（把这个地址里的 IP 换成 localhost），远程访问的开关也在那里。",
    ),
    "en": (
        "This address no longer opens Avibe",
        "This Avibe is bound to an account, so other devices sign in first. "
        "Sign in at avibe.bot, then open it from your list.",
        "Go to avibe.bot",
        "Or open it on the computer that runs Avibe (replace the IP in this address with localhost); "
        "the switch for remote access is there too.",
    ),
}


@pytest.fixture
def bound(monkeypatch, tmp_path):
    """A saved binding on an install whose setup host is a private address; the connector is off."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config.default()
    config.ui.setup_host = "192.168.2.3"
    cloud = config.remote_access.vibe_cloud
    cloud.instance_id = "fixture-instance"
    cloud.instance_secret = "fixture-secret"
    cloud.session_secret = "fixture-cookie-key"
    cloud.public_url = "https://fixture.example.test"
    config.save()
    return config


def open_old_address(path="/", accept_language=""):
    headers = {"Accept": "text/html"}
    if accept_language:
        headers["Accept-Language"] = accept_language
    return ui_server.app.test_client().get(path, base_url=LAN, environ_base=LAN_PEER, headers=headers)


@pytest.mark.parametrize(
    ("accept_language", "lang"), [("zh-CN,zh;q=0.9,en;q=0.8", "zh"), ("en-US,en;q=0.9", "en"), ("", "en"), ("fr", "en")]
)
def test_the_page_says_the_agreed_sentences_in_the_readers_language(bound, accept_language, lang):
    page = open_old_address("/sessions/abc", accept_language).text
    title, body, action, local = SAID[lang]
    assert f'<html lang="{lang}">' in page
    assert html.unescape(re.search(r"<title>(.*?)</title>", page).group(1)) == title
    assert html.unescape(re.search(r"<h1>(.*?)</h1>", page).group(1)) == title
    assert [html.unescape(text) for text in re.findall(r"<p[^>]*>(.*?)</p>", page)] == [body, local]
    assert re.findall(r"<a [^>]*>(.*?)</a>", page) == [action]


def test_a_visitor_learns_nothing_from_it(bound):
    """One fixed link, nothing that runs or loads, and nothing of this install or of the request."""
    answer = open_old_address("/settings/devices?claim=secret-claim#frag", "zh")
    page = answer.text
    # The only address on the page is the fixed account page.
    assert re.findall(r"""(?:href|src|action|content)\s*=\s*["']?(https?:[^"' >]+)""", page) == ["https://avibe.bot/app"]
    assert re.findall(r"https?://[^\s\"'<]+", page) == ["https://avibe.bot/app"]
    for forbidden in ("<script", "<link", "<img", "<iframe", "<form", "http-equiv", "url(", "@import"):
        assert forbidden not in page, forbidden
    assert not re.search(r"\son\w+\s*=", page)
    for private in ("fixture", "192.168", "15131", "secret-claim", "settings/devices", "example.test"):
        assert private not in page, private
    assert "Set-Cookie" not in answer.headers and "Location" not in answer.headers
    # Only the page's own style may load, by its hash.
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode("utf-8")).digest()).decode("ascii")
    assert answer.headers["Content-Security-Policy"] == (
        f"default-src 'none'; style-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert answer.headers["Cache-Control"] == "no-store" and answer.headers["Referrer-Policy"] == "no-referrer"

    # The same bytes at any path, and whether remote access is on or off.
    assert open_old_address("/", "zh").content == answer.content
    bound.remote_access.vibe_cloud.enabled = True
    bound.save()
    assert open_old_address("/", "zh").content == answer.content


PROXY = {"REMOTE_ADDR": "192.168.2.9"}
NAVIGATION = {"Accept": "text/html", "Accept-Language": "zh-CN", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def through_proxy(path, forwarded_host, headers=NAVIGATION):
    """What the operator's reverse proxy sends on for a browser that asked for ``forwarded_host``."""
    forwarded = {**headers, "X-Forwarded-Host": forwarded_host, "X-Forwarded-Proto": "https"}
    return ui_server.app.test_client().get(path, base_url=LAN, environ_base=PROXY, headers=forwarded)


def test_a_declared_proxy_and_domain_still_open_avibe_and_never_this_page(bound, monkeypatch):
    """The operator wrote down both the proxy's address and the public origin: the exception to the rule."""
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "192.168.2.9")
    bound.ui.trusted_public_origins = ["https://home.example.test"]
    bound.save()

    page = through_proxy("/sessions/abc", "home.example.test")
    assert SAID["zh"][0] not in html.unescape(page.text) and SAID["en"][0] not in html.unescape(page.text)
    session = through_proxy("/api/session", "home.example.test", {"Accept": "application/json"})
    assert session.status_code == 200 and session.get_json()["instance_role"] == "owner"


@pytest.mark.parametrize(
    ("declared_proxy", "declared_origins", "asked_for", "shows_the_page"),
    [
        # The proxy is trusted, but nobody declared a public origin: it forwards the saved private address.
        ("192.168.2.9", [], "192.168.2.3:15131", True),
        # The origin is declared, but the request does not come from a declared proxy.
        ("", ["https://home.example.test"], "home.example.test", True),
        # A name nobody declared is told nothing, not even this page.
        ("192.168.2.9", ["https://other.example.test"], "home.example.test", False),
    ],
    ids=["proxy-without-origin", "origin-without-proxy", "undeclared-name"],
)
def test_an_incomplete_proxy_declaration_never_opens_avibe(
    bound, monkeypatch, declared_proxy, declared_origins, asked_for, shows_the_page
):
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, declared_proxy)
    bound.ui.trusted_public_origins = declared_origins
    bound.save()

    refusal = {"ok": False, "error": "remote_access_host_mismatch"}
    session = through_proxy("/api/session", asked_for, {"Accept": "application/json"})
    assert session.status_code == 503 and session.get_json() == refusal
    answer = through_proxy("/sessions/abc?claim=secret-claim", asked_for)
    if not shows_the_page:
        assert answer.status_code == 503 and answer.get_json() == refusal
        return
    assert answer.status_code == 200 and answer.content == open_old_address("/", "zh-CN").content
    for private in ("home.example.test", "secret-claim", "fixture", "192.168"):
        assert private not in answer.text, private
    assert "Set-Cookie" not in answer.headers and "Location" not in answer.headers
