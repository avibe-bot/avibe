"""WF-LOCAL-001: public-target policy is enforced before any executor egress.

The previous readers had no policy owner. These tables protect parser ambiguity,
all-answer checks, numeric pins, DNS64's embedded boundary, and DNS-free metadata.
"""
import socket

import pytest

from core.handlers.model_hub import fetch_policy as policy


@pytest.mark.parametrize("ip,code", [
    ("127.0.0.1", "fetch_url_blocked"), ("10.0.0.1", "fetch_url_blocked"),
    ("172.16.0.1", "fetch_url_blocked"), ("192.168.1.1", "fetch_url_blocked"),
    ("100.64.0.0", "fetch_url_blocked"), ("100.127.255.255", "fetch_url_blocked"),
    ("169.254.169.254", "fetch_url_blocked"), ("168.63.129.16", "fetch_url_blocked"),
    ("0.0.0.0", "fetch_url_blocked"), ("224.0.0.1", "fetch_url_blocked"),
    ("255.255.255.255", "fetch_url_blocked"), ("192.0.2.1", "fetch_url_blocked"),
    ("198.51.100.1", "fetch_url_blocked"), ("203.0.113.1", "fetch_url_blocked"),
    ("198.18.0.1", "fetch_dns_unverified"), ("198.19.255.255", "fetch_dns_unverified"),
    ("::", "fetch_url_blocked"), ("::1", "fetch_url_blocked"),
    ("::ffff:127.0.0.1", "fetch_url_blocked"), ("fe80::1", "fetch_url_blocked"),
    ("fc00::1", "fetch_url_blocked"), ("ff02::1", "fetch_url_blocked"),
    ("2001:db8::1", "fetch_url_blocked"), ("2001::1", "fetch_url_blocked"),
    ("2002:0808:0808::1", "fetch_url_blocked"), ("64:ff9b:1::808:808", "fetch_url_blocked"),
    ("64:ff9b::7f00:1", "fetch_url_blocked"), ("64:ff9b::c612:1", "fetch_dns_unverified"),
    ("::8.8.8.8", "fetch_url_blocked"), ("3fff::1", "fetch_url_blocked"),
    ("2001:4860::5efe:127.0.0.1", "fetch_url_blocked"),
    ("2001:4860::200:5efe:8.8.8.8", "fetch_url_blocked"),
])
def test_denied_address_table(ip, code):
    with pytest.raises(policy.FetchFailure) as raised:
        policy.checked_ip(ip)
    assert (raised.value.code, raised.value.terminal) == (code, True)


@pytest.mark.parametrize("ip,expected", [
    ("8.8.8.8", "8.8.8.8"), ("100.63.255.255", "100.63.255.255"),
    ("100.128.0.1", "100.128.0.1"), ("198.20.0.1", "198.20.0.1"),
    ("2606:4700:4700::1111", "2606:4700:4700::1111"),
    ("2001:4860:4860::8888", "2001:4860:4860::8888"),
    ("::ffff:8.8.8.8", "8.8.8.8"), ("64:ff9b::808:808", "64:ff9b::808:808"),
])
def test_public_address_table(ip, expected):
    assert policy.checked_ip(ip) == expected


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "https://user:pass@example.com/", "https://@example.com/",
    "https://localhost/", "https://localhost./", "http://metadata.google.internal/", "http://device.local/",
    "http://router.home.arpa/", "http://localhost.localdomain/",
    "http://localhost。/", "http://metadata.google.internal。/", "http://router.lan．/",
    "http://2130706433/", "http://127.1/", "http://0177.0.0.1/", "http://0x7f000001/", "http://0x7f.1/",
    "http://[fe80::1%25en0]/", "http://[::ffff:127.0.0.1]/", "http://example.com:0/",
    "http://example.com:65536/", "http://example.com:/", "http://example.com\\@8.8.8.8/",
    "http://example.com/\r\nHost:8.8.8.8", " http://example.com/", "http://example.com/\u200b",
    "http://example.com../", "http://%31%32%37.0.0.1/", "//example.com", "https:///example.com",
    "https://example.com/" + "a" * 8192,
])
def test_url_ambiguities_refused_before_dns(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("refused URL reached DNS"))
    with pytest.raises(policy.FetchFailure) as raised:
        policy.resolve_target(url)
    assert raised.value.terminal


def test_uts46_normalization_preserves_path_query_and_removes_fragment():
    address = policy.parse_url("https://BÜCHER.Example.:8443/a%2Fb?q=%2f&raw=a+b#gone")
    assert address.url == "https://xn--bcher-kva.example:8443/a%2Fb?q=%2f&raw=a+b"
    assert address.origin == ("https", "xn--bcher-kva.example", 8443)


@pytest.mark.parametrize("answers,expected", [
    (["8.8.8.8", "2001:4860::1"], ("8.8.8.8", "2001:4860::1")),
    (["8.8.8.8", "10.0.0.1"], "fetch_url_blocked"),
    (["8.8.8.8", "::ffff:127.0.0.1"], "fetch_url_blocked"),
    (["8.8.8.8", "198.18.0.1"], "fetch_dns_unverified"),
    ([], "fetch_dns_unverified"),
])
def test_entire_dns_set_is_checked(monkeypatch, answers, expected):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in answers])
    if isinstance(expected, str):
        with pytest.raises(policy.FetchFailure, match=expected):
            policy.resolve_target("https://example.com/")
    else:
        assert policy.resolve_target("https://example.com/").ips == expected


def test_dns_failure_and_metadata_boundary(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror()
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(policy.FetchFailure, match="fetch_dns_unverified"):
        policy.resolve_target("https://example.com/")
    assert policy.metadata_url("https://reported.example/final") == "https://reported.example/final"
    for value in ("http://127.0.0.1/", "https://user@public.example/", "file:///x", None):
        assert policy.metadata_url(value) is None
