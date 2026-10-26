"""WF-LOCAL-003: extraction and local orchestration have no rendering side effects.

Tables distinguish target failures from policy/credential refusals; fake transports
are confined to this fixture, while the socket boundary is tested separately.
"""
import asyncio
import io
import json
import sys
import time

import pytest

from core.web_fetch_worker import worker, transport
from core.handlers.model_hub import local_fetch
from core.web_fetch_worker.extract import extract_page, inert_markdown
from core.web_fetch_worker.policy import CheckedTarget, FetchFailure, parse_url
from core.web_fetch_worker.transport import HopResponse


@pytest.mark.parametrize("body,mime,expected", [
    (b"# Page\n\nReadable text.", "text/markdown", "Readable text."),
    (b"plain text", "text/plain", "plain text"),
    (b'{"hello": [1,2]}', "application/json", '"hello"'),
    (b"{not json", "application/json", "{not json"),
    (b"<html><title>Heading</title><article><h1>Heading</h1><p>A complete readable article explains the test document.</p></article></html>", "text/html", "complete readable article"),
    ("<meta charset='gb18030'><article><p>这是网页的正文，编码由元数据提供。</p></article>".encode("gb18030"), "text/html", "这是网页的正文"),
])
def test_formats_are_read_without_network(body, mime, expected):
    page = extract_page(body, mime, "https://example.com/docs/page")
    assert expected in page.content
    assert page.content_type == "text/markdown"
    assert page.source_content_type == mime


@pytest.mark.parametrize("body,mime,code,terminal", [
    (b"", "text/plain", "fetch_target_unreadable", False),
    (b"  ", "application/json", "fetch_target_unreadable", False),
    (b"", "text/html", "fetch_target_unreadable", False),
    (b"  ", "text/html", "fetch_target_unreadable", False),
    (b"<!-- only a comment -->", "text/html", "fetch_target_unreadable", False),
    (b"PK...", "application/zip", "fetch_unsupported_content", False),
    (b"<html><script src='/app.js'></script><div id='app'></div></html>", "text/html", "fetch_js_shell", False),
    (b"<header><form><input type='password'></form></header><div id='root'></div><script src='/app.js'></script>", "text/html", "fetch_js_shell", False),
    (b"<form id='challenge-form'>Checking</form>", "text/html", "fetch_challenge", False),
    (b"<form><input type='password'></form>", "text/html", "fetch_refused", True),
    (b"Access denied", "text/plain", "fetch_challenge", False),
])
def test_unreadable_formats_fail_over_but_login_is_terminal(body, mime, code, terminal):
    with pytest.raises(FetchFailure) as raised:
        extract_page(body, mime, "https://example.com/")
    assert (raised.value.code, raised.value.terminal) == (code, terminal)


@pytest.mark.parametrize("body,mime,expected", [
    (b"caf\xe9", "text/plain; Charset=ISO-8859-1", "café"),
    (b"caf\xe9", "TEXT/PLAIN; CHARSET=ISO-8859-1", "café"),
    (b"<meta charset='ISO-8859-1'><article><p>A readable caf\xe9 article.</p></article>", "TEXT/HTML", "café"),
])
def test_charset_declarations_are_case_insensitive(body, mime, expected):
    page = extract_page(body, mime, "https://example.com/")
    assert expected in page.content and not page.charset_fallback


def test_markdown_html_keeps_visible_text_and_literal_code_without_active_markup():
    text = '''# Install

<Warning>
Rotate the key before continuing.
</Warning>

<details><summary>Reason</summary>The visible explanation.</details>

<script>never_execute()</script>

```html
<script>literal example</script>
```

Done.'''
    content = inert_markdown(text, "https://example.com/")
    for visible in ("Rotate the key", "Reason", "visible explanation", "<script>literal example</script>", "Done."):
        assert visible in content
    assert "<Warning>" not in content and "<details>" not in content and "never_execute" not in content


@pytest.mark.parametrize("hint", ["/bad path.md", "/bad\\path.md", "/bad\npath.md", "https://example.com:/bad"])
def test_malformed_optional_hint_keeps_readable_html(hint):
    markup = f'<link rel="alternate" type="text/markdown" href="{hint}"><article><p>The original article remains readable.</p></article>'
    page = extract_page(markup.encode(), "text/html", "https://example.com/")
    assert page.follow_url is None and "original article" in page.content


def test_public_article_with_login_widget_and_xhtml_declaration_is_readable():
    text = b'''<?xml version="1.0" encoding="UTF-8"?><html><head><title>Public article</title></head>
    <body><header><form><input type="password">Sign in</form></header><article><h1>Public article</h1>
    <p>This complete readable article is available without authentication. Its header includes an unrelated login widget.</p>
    </article></body></html>'''
    for mime in ("text/html", "application/xhtml+xml"):
        assert "without authentication" in extract_page(text, mime, "https://example.com/").content


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown"])
def test_large_text_is_bounded_before_markdown_token_construction(mime):
    source = ("A readable paragraph with a [link](https://example.com/).\n\n" * 150000).encode()
    page = extract_page(source, mime, "https://example.com/")
    assert page.content_overflow and "readable paragraph" in page.content
    assert len(page.content) < 50000


def test_plain_text_preserves_literal_markup_and_numbered_lines():
    text = 'Usage: tool <input-file> <output-file>\nstd::vector<int> v;\n\n<div>literal</div>\n\n[1]: https://example.com/ref\n1. One\n2. Two'
    page = extract_page(text.encode(), "text/plain", "https://example.com/")
    assert text in page.content
    numbered = inert_markdown("7. Seven\n8. Eight", "https://example.com/")
    assert "7. Seven" in numbered and "8. Eight" in numbered


def test_pdf_text_is_quoted_not_interpreted_as_markup():
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 700 Td (tool <input-file> std::vector<int>) Tj ET")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    result = extract_page(output.getvalue(), "application/pdf", "https://example.com/")
    assert "[Page 1]" in result.content and "tool <input-file> std::vector<int>" in result.content


def test_inert_links_images_html_and_metadata():
    page = extract_page(b'''<html><title>A title</title><link rel="canonical" href="../canonical">
    <article><h1>A heading</h1><p>Readable article with <a href="../next">next page</a>
    and <a href="javascript:alert(1)">unsafe link</a>, and meaningful paragraph text.</p>
    <script>alert('secret')</script><img src="https://images.example/x"><form>unwanted</form></article></html>''',
                        "text/html", "https://example.com/docs/page")
    assert page.title == "A title"
    assert page.canonical_url == "https://example.com/canonical"
    assert "https://example.com/next" in page.content
    for unsafe in ("javascript:", "<script", "images.example", "unwanted", "secret"):
        assert unsafe not in page.content
    result = inert_markdown("[relative](../x) ![alt](https://img.example/x) <script>bad()</script> [ref][r]\n\n[r]: https://good.example/x", "https://example.com/docs/p")
    assert "https://example.com/x" in result and "https://good.example/x" in result
    assert "img.example" not in result and "bad()" not in result


def test_exact_challenge_text_inside_document_is_not_a_refusal():
    page = extract_page(b"This article discusses the message Access denied in a web application.", "text/plain", "https://example.com/")
    assert "Access denied" in page.content


def test_charset_fallback_alternate_and_refresh():
    assert extract_page(b"text \xff", "text/plain", "https://example.com/").charset_fallback
    for kind, markup in (("alternate", '<link rel="alternate" type="text/markdown" href="/page.md">'),
                         ("refresh", '<meta http-equiv="refresh" content="0; url=/next">')):
        page = extract_page((markup + "<article><p>Readable document for the alternative format.</p></article>").encode(),
                            "text/html", "https://example.com/")
        assert page.follow_kind == kind
        assert page.follow_url.startswith("https://example.com/")
    page = extract_page(b'<link rel="alternate" type="text/markdown" href="https://other.example/x"><p>Retained text.</p>',
                        "text/html", "https://example.com/")
    assert page.follow_url is None


def test_encrypted_and_textless_pdf_fail_over():
    from pypdf import PdfWriter
    for encrypted in (False, True):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        if encrypted:
            writer.encrypt("not-available")
        data = io.BytesIO()
        writer.write(data)
        with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
            extract_page(data.getvalue(), "application/pdf", "https://example.com/document")


@pytest.fixture
def local_fixture(monkeypatch):
    calls, gates, events = [], [], []
    responses = {}
    def resolve(url):
        address = parse_url(url)  # Real syntax/literal policy, only DNS is fixture-owned.
        gates.append(address.url)
        return CheckedTarget(address, ("8.8.8.8",))
    def hop(target, headers, budget, timeout, on_sent):
        on_sent()
        calls.append((target.address.url, headers))
        item = responses[target.address.url]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(worker, "resolve_target", resolve)
    monkeypatch.setattr(transport, "native_available", lambda: (True, None))
    monkeypatch.setattr(transport, "curl_hop", hop)
    monkeypatch.setattr(transport, "httpx_hop", lambda *a: pytest.fail("sent target failure retried on degraded transport"))
    return responses, calls, gates, events


def test_cookie_jar_bounds_outgoing_header_and_validates_prefixes():
    jar = worker.OriginCookies()
    address = parse_url("https://example.com/doc")
    jar.receive(address, [("set-cookie", f"cookie{i}={'x' * 3900}; Path=/") for i in range(70)])
    assert len(jar.values) == 64
    assert len(jar.header(address).encode()) <= 48 * 1024
    jar = worker.OriginCookies()
    jar.receive(address, [("set-cookie", "__Host-bad=1; Path=/"),
                          ("set-cookie", "__Secure-bad=1"),
                          ("set-cookie", "good=1; Secure; Path=/")])
    assert jar.header(address) == "good=1"


def test_rfc_response_cookies_ignore_attributes_but_remain_origin_only():
    jar = worker.OriginCookies()
    address = parse_url("https://www.example.com/doc")
    jar.receive(address, [("set-cookie", 'prefs={"a":1}; Domain=.example.com; Path=/; Secure; HttpOnly; Priority=High; foo=bar'),
                          ("set-cookie", "partitioned=yes; Secure; Partitioned; Path=/")])
    assert jar.header(address) == 'prefs={"a":1}; partitioned=yes'
    assert jar.header(parse_url("https://other.example.com/doc")) == ""
    assert jar.header(parse_url("https://example.com/doc")) == ""


def test_redirect_cookie_scope_and_one_logical_send(local_fixture):
    responses, calls, gates, events = local_fixture
    responses.update({
        "https://example.com/start": HopResponse(302, [("location", "/next"), ("set-cookie", "visit=1; Path=/; Secure")], b""),
        "https://example.com/next": HopResponse(302, [("location", "https://other.example/final")], b""),
        "https://other.example/final": HopResponse(200, [("content-type", "text/markdown")], b"# Document\n\nUseful content."),
    })
    result = worker.fetch_document("https://example.com/start", "en", events.append)
    assert result["final_url"] == "https://other.example/final"
    assert calls[1][1]["Cookie"] == "visit=1" and "Cookie" not in calls[2][1]
    assert [e for e in events if e["event"] == "sent"] == [{"event": "sent"}]
    assert all(url in gates for url, _ in calls)
    events.clear()
    worker.fetch_document("https://other.example/final", "en", events.append)
    assert "Cookie" not in calls[-1][1]


def test_native_load_failure_selects_honest_transport_before_send(local_fixture, monkeypatch):
    responses, calls, gates, events = local_fixture
    monkeypatch.setattr(transport, "native_available", lambda: (False, "native_library_unavailable"))
    def degraded(target, headers, budget, timeout, on_sent):
        assert headers["User-Agent"].startswith("Avibe/")
        assert not any(key.lower().startswith(("sec-ch-", "sec-fetch-")) for key in headers)
        on_sent()
        return HopResponse(200, [("content-type", "text/plain")], b"Readable degraded result.")
    monkeypatch.setattr(transport, "httpx_hop", degraded)
    monkeypatch.setattr(transport, "curl_hop", lambda *a: pytest.fail("unavailable native library was used"))
    result = worker.fetch_document("https://example.com/", "en", events.append)
    assert result["transport_degraded"] and result["transport_degraded_reason"] == "native_library_unavailable"


@pytest.mark.parametrize("status,code,terminal", [
    (401, "fetch_refused", True), (403, "fetch_challenge", False),
    (404, "fetch_target_unreadable", False), (410, "fetch_target_unreadable", False),
    (429, "service_rate_limited", False), (503, "fetch_unavailable", False),
])
def test_local_http_outcome_table(local_fixture, status, code, terminal):
    responses, calls, gates, events = local_fixture
    responses["https://example.com/"] = HopResponse(status, [("retry-after", "3600")], b"not content")
    with pytest.raises(FetchFailure) as raised:
        worker.fetch_document("https://example.com/", "en", events.append)
    assert (raised.value.code, raised.value.terminal) == (code, terminal)
    assert len(calls) == 1
    if status in {429, 503}:
        assert raised.value.retry_after == "3600"


def test_503_explicit_challenge_has_no_transient_host_class(local_fixture):
    responses, calls, gates, events = local_fixture
    responses["https://example.com/"] = HopResponse(503, [("cf-mitigated", "challenge")], b"challenge")
    with pytest.raises(FetchFailure, match="fetch_challenge"):
        worker.fetch_document("https://example.com/", "en", events.append)


def test_redirect_to_private_terminates_without_second_connect(local_fixture):
    responses, calls, gates, events = local_fixture
    responses["https://example.com/"] = HopResponse(302, [("location", "http://169.254.169.254/latest")], b"")
    with pytest.raises(FetchFailure) as raised:
        worker.fetch_document("https://example.com/", "en", events.append)
    assert (raised.value.code, raised.value.terminal) == ("fetch_url_blocked", True)
    assert len(calls) == 1


@pytest.mark.parametrize("alternate_outcome", ["unreadable", "blocked", "success", "rate_limited"])
def test_one_markdown_alternate_retains_original_except_policy_refusal(local_fixture, monkeypatch, alternate_outcome):
    responses, calls, gates, events = local_fixture
    responses["https://example.com/"] = HopResponse(200, [("content-type", "text/html")],
        b'<title>Original title</title><link rel="canonical" href="/canonical"><link rel="alternate" type="text/markdown" href="/alternate"><article><p>Original readable HTML article text.</p></article>')
    responses["https://example.com/alternate"] = (HopResponse(200, [("content-type", "text/markdown")], b"Better Markdown body.")
                                               if alternate_outcome == "success" else HopResponse(404, [], b""))
    if alternate_outcome == "rate_limited":
        responses["https://example.com/alternate"] = HopResponse(429, [("retry-after", "3600")], b"")
    if alternate_outcome == "blocked":
        original = worker.resolve_target
        def resolve(url):
            if url.endswith("/alternate"):
                raise FetchFailure("fetch_dns_unverified", terminal=True)
            return original(url)
        monkeypatch.setattr(worker, "resolve_target", resolve)
        with pytest.raises(FetchFailure, match="fetch_dns_unverified"):
            worker.fetch_document("https://example.com/", "en", events.append)
        assert len(calls) == 1
    else:
        result = worker.fetch_document("https://example.com/", "en", events.append)
        assert ("Better Markdown" if alternate_outcome == "success" else "Original readable") in result["content"]
        if alternate_outcome == "rate_limited":
            assert result["cooldown_code"] == "service_rate_limited" and result["retry_after"] == "3600"
        assert result["title"] == "Original title" and result["canonical_url"] == "https://example.com/canonical"
        assert len(calls) == 2


@pytest.mark.parametrize("shape", ["loop", "missing_location", "hop_limit"])
def test_redirect_protocol_failure_is_target_scoped(local_fixture, shape):
    responses, _, _, events = local_fixture
    if shape == "loop":
        responses["https://example.com/"] = HopResponse(302, [("location", "/")], b"")
    elif shape == "missing_location":
        responses["https://example.com/"] = HopResponse(302, [], b"")
    else:
        for n in range(6):
            url = "https://example.com/" + (str(n) if n else "")
            responses[url] = HopResponse(302, [("location", f"/{n + 1}")], b"")
    with pytest.raises(FetchFailure, match="fetch_target_unreadable"):
        worker.fetch_document("https://example.com/", "en", events.append)


def test_short_attempt_dns_deadline_differs_from_real_dns_failure(local_fixture, monkeypatch):
    _, _, _, events = local_fixture
    original = worker.resolve_target
    def slow(url):
        time.sleep(.04)
        return original(url)
    monkeypatch.setattr(worker, "resolve_target", slow)
    with pytest.raises(FetchFailure) as raised:
        worker.fetch_document("https://example.com/", "en", events.append, remaining_seconds=.005)
    assert raised.value.code == "service_busy" and not raised.value.terminal and raised.value.cooldown_suppressed
    def unavailable(url):
        raise FetchFailure("fetch_dns_unverified", terminal=True)
    monkeypatch.setattr(worker, "resolve_target", unavailable)
    with pytest.raises(FetchFailure) as raised:
        worker.fetch_document("https://example.com/", "en", events.append)
    assert raised.value.code == "fetch_dns_unverified" and raised.value.terminal


def test_gate_worker_does_not_load_http_or_extraction_stack(tmp_path):
    """The mandatory gate works even when no local fetch stack can be loaded."""
    import subprocess
    from pathlib import Path

    code = '''import importlib.abc,sys
sys.path.insert(0,sys.argv.pop(1))
class PolicyOnly(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.rsplit(".",1)[-1] in {"transport","extract"} or fullname.split(".",1)[0] in {"httpx","httpcore","curl_cffi","lxml","trafilatura","pypdf"}:
   raise AssertionError("gate attempted unrelated import: " + fullname)
sys.meta_path.insert(0,PolicyOnly())
from core.web_fetch_worker.worker import main
main()
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve().parents[1])],
        input=json.dumps({"operation": "gate", "url": "https://8.8.8.8/"}) + "\n",
        capture_output=True, text=True, cwd=tmp_path, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"event": "result", "result": {"url": "https://8.8.8.8/"}}


async def test_gate_worker_and_initial_policy_failure_do_not_send():
    engine = local_fetch.LocalFetchEngine()
    assert await engine.check("https://8.8.8.8/", deadline=time.monotonic() + 5) == "https://8.8.8.8/"
    with pytest.raises(FetchFailure, match="fetch_url_blocked"):
        await engine.fetch("http://127.0.0.1/", deadline=time.monotonic() + 5)


async def test_host_lock_serializes_requests_and_scrubs_worker_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-owned-value-must-not-be-inherited")
    script = '''import sys,time,json,os
assert "OPENAI_API_KEY" not in os.environ
sys.stdin.readline()
print(json.dumps({"event":"host","host":"example.com"}),flush=True); sys.stdin.readline()
print(json.dumps({"event":"sent"}),flush=True); sys.stdin.readline()
time.sleep(.2)
print(json.dumps({"event":"result","result":{"content":"done"}}),flush=True)
'''
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine, starts = local_fetch.LocalFetchEngine(), []
    results = await asyncio.gather(*(engine.fetch("https://example.com/", deadline=time.monotonic() + 5,
                                                  on_sent=lambda: starts.append(time.monotonic())) for _ in range(2)))
    assert all(row["sent"] for row in results)
    assert len(starts) == 2 and starts[1] - starts[0] >= .18


@pytest.mark.parametrize("retained_html", [False, True])
async def test_local_retry_after_is_host_scoped_and_degraded_failure_is_recorded(monkeypatch, retained_html):
    script = '''import sys,json
from urllib.parse import urlsplit
request=json.loads(sys.stdin.readline())
for event in [{"event":"transport","transport_degraded":True,"transport_degraded_reason":"native_library_unavailable"},
              {"event":"host","host":urlsplit(request["url"]).hostname}, {"event":"sent"}]:
 print(json.dumps(event),flush=True); sys.stdin.readline()
print(json.dumps({"event":"failure","error_code":"service_rate_limited","retry_after":"3600","terminal":False}),flush=True)
'''
    if retained_html:
        script = script.replace('{"event":"failure","error_code":"service_rate_limited","retry_after":"3600","terminal":False}',
                                '{"event":"result","result":{"content":"retained HTML","cooldown_code":"service_rate_limited","retry_after":"3600"}}')
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine, sent = local_fetch.LocalFetchEngine(), []
    async def fetch(host):
        return await engine.fetch(f"https://{host}/", deadline=time.monotonic() + 5, on_sent=lambda: sent.append(host))
    first, second, other = await fetch("example.com"), await fetch("example.com"), await fetch("other.example")
    assert first["transport_degraded"] and first["transport_degraded_reason"] == "native_library_unavailable"
    if retained_html:
        assert first["content"] == "retained HTML" and "error_code" not in first and "cooldown_code" not in first
    assert engine._cooldowns["example.com"][0] >= time.time() + 3590
    assert second["skipped"] == "cooling" and not second["sent"]
    assert other["sent"] and sent == ["example.com", "other.example"]


@pytest.mark.parametrize("retained_html", [False, True])
async def test_queued_redirect_observes_rate_limit_before_host_is_released(monkeypatch, retained_html):
    """A queued redirect cannot slip between worker cleanup and health publication."""
    limited_result = ({"event": "result", "result": {"content": "retained HTML", "cooldown_code": "service_rate_limited", "retry_after": "3600"}}
                      if retained_html else {"event": "failure", "error_code": "service_rate_limited", "retry_after": "3600", "terminal": False})
    script = '''import sys,json
from urllib.parse import urlsplit
request=json.loads(sys.stdin.readline())
host=urlsplit(request["url"]).hostname
for event in [{"event":"host","host":host}, {"event":"sent"}]:
 print(json.dumps(event),flush=True); sys.stdin.readline()
if host == "limited.example":
 print(json.dumps(LIMITED_RESULT),flush=True)
else:
 print(json.dumps({"event":"host","host":"limited.example"}),flush=True)
 if sys.stdin.readline():
  print(json.dumps({"event":"result","result":{"content":"bypassed Retry-After"}}),flush=True)
'''.replace("LIMITED_RESULT", repr(limited_result))
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine = local_fetch.LocalFetchEngine()
    started, queued = asyncio.Event(), asyncio.Event()
    change_host = engine._change_host
    async def observe_redirect(host, state):
        if host == "limited.example" and state["held_host"] == "other.example":
            queued.set()
        await change_host(host, state)
    monkeypatch.setattr(engine, "_change_host", observe_redirect)
    async def first_sent():
        started.set()
        await queued.wait()
    first = asyncio.create_task(engine.fetch("https://limited.example/", deadline=time.monotonic() + 5, on_sent=first_sent))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        second = asyncio.create_task(engine.fetch("https://other.example/", deadline=time.monotonic() + 5))
        limited, redirected = await asyncio.gather(first, second)
        assert limited.get("content") == "retained HTML" if retained_html else limited["error_code"] == "service_rate_limited"
        assert redirected.get("skipped") == "cooling" and "content" not in redirected
        assert engine._cooldowns["limited.example"][0] >= time.time() + 3590
        assert not engine._hosts and engine._workers["fetch"] == 0
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


async def test_busy_host_does_not_starve_policy_gate_or_other_hosts(monkeypatch):
    script = '''import sys,time,json
from urllib.parse import urlsplit
r=json.loads(sys.stdin.readline())
if r["operation"] == "gate":
 print(json.dumps({"event":"result","result":{"url":r["url"]}}),flush=True); sys.exit()
host=urlsplit(r["url"]).hostname
print(json.dumps({"event":"host","host":host}),flush=True); sys.stdin.readline()
print(json.dumps({"event":"sent"}),flush=True); sys.stdin.readline()
if host == "example.com": time.sleep(60)
print(json.dumps({"event":"result","result":{"content":"other host"}}),flush=True)
'''
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine, started = local_fetch.LocalFetchEngine(), asyncio.Event()
    spawned, spawn = [], asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        spawned.append(1)
        return await spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    tasks = [asyncio.create_task(engine.fetch("https://example.com/", deadline=time.monotonic() + 10, on_sent=started.set)) for _ in range(40)]
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await engine.check("https://other.example/", deadline=time.monotonic() + 2) == "https://other.example/"
        other = await engine.fetch("https://other.example/", deadline=time.monotonic() + 2)
        assert other["sent"] and other["content"] == "other host"
        queued = await engine.fetch("https://example.com/", deadline=time.monotonic() + .3)
        assert queued["error_code"] == "service_busy" and not queued["sent"]
        assert not engine._cooldowns
        assert len(spawned) <= 4  # One same-host worker, independent gate and other host; not 40 parked workers.
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_parent_memory_limit_kills_worker_and_keeps_known_alternate_fallback(monkeypatch):
    script = '''import sys,time,json
sys.stdin.readline()
print(json.dumps({"event":"sent"}),flush=True); sys.stdin.readline()
print(json.dumps({"event":"fallback","result":{"content":"original HTML","final_url":"https://example.com/"}}),flush=True); sys.stdin.readline()
time.sleep(60)
'''
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine = local_fetch.LocalFetchEngine()
    result = await engine.fetch("https://example.com/", deadline=time.monotonic() + .5)
    assert result["sent"] and result["content"] == "original HTML" and "error_code" not in result
    # Separate actual RSS-supervision case: lower only this fixture's threshold.
    monkeypatch.setattr(local_fetch, "_MEMORY_LIMIT", 1)
    monkeypatch.setattr(local_fetch, "_WORKER", "import sys,time; sys.stdin.readline(); time.sleep(60)")
    started = time.monotonic()
    result = await engine.fetch("https://example.com/", deadline=time.monotonic() + 5)
    assert result["error_code"] == "fetch_unavailable" and time.monotonic() - started < 3


@pytest.mark.parametrize("sent", [False, True])
@pytest.mark.parametrize("phase", ["policy", "fetch"])
async def test_attempt_expiry_during_revalidation_is_not_a_dns_refusal(monkeypatch, sent, phase):
    script = '''import sys,time,json
sys.stdin.readline()
for event in EVENTS:
 print(json.dumps(event),flush=True); sys.stdin.readline()
time.sleep(60)
'''.replace("EVENTS", repr([{"event": "host", "host": "example.com"}] +
                        ([{"event": "sent"}] if sent else []) + [{"event": "phase", "phase": phase}]))
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    engine = local_fetch.LocalFetchEngine()
    result = await engine.fetch("https://example.com/", deadline=time.monotonic() + .3)
    code = "service_busy" if not sent and phase == "policy" else "fetch_timeout"
    assert result["error_code"] == code and not result["terminal"]
    assert result["sent"] == sent and not engine._cooldowns
    if code == "service_busy":
        assert result["skipped"] == "unavailable"
    with pytest.raises(FetchFailure, match="fetch_dns_unverified"):
        await engine.check("https://example.com/", deadline=time.monotonic() + .3)


@pytest.mark.parametrize("remaining,elapsed,sent,timeout_type,suppressed", [
    (30, 5, False, "connect", False),
    (1, 1, False, "connect", True),
    (30, 30, True, "read", True),
])
def test_hop_timeout_only_cools_when_it_had_its_own_connect_budget(
    local_fixture, monkeypatch, remaining, elapsed, sent, timeout_type, suppressed
):
    import httpcore
    from types import SimpleNamespace
    _, _, _, events = local_fixture
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))

    def timed_out(target, headers, budget, timeout, on_sent):
        if sent:
            on_sent()
        clock.now += elapsed
        raise (httpcore.ReadTimeout if timeout_type == "read" else httpcore.ConnectTimeout)()

    monkeypatch.setattr(transport, "curl_hop", timed_out)
    with pytest.raises(FetchFailure) as raised:
        worker.fetch_document("https://example.com/", "en", events.append, remaining_seconds=remaining)
    assert raised.value.code == "fetch_timeout" and raised.value.cooldown_suppressed == suppressed

async def test_stop_during_success_cleanup_is_rethrown_after_reap(monkeypatch):
    script = 'import sys; sys.stdin.readline(); print(\'{"event":"result","result":{"content":"page"}}\',flush=True)'
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    cleaning, release = asyncio.Event(), asyncio.Event()
    async def monitor(process):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise
    monkeypatch.setattr(local_fetch.LocalFetchEngine, "_monitor", staticmethod(monitor))
    task = asyncio.create_task(local_fetch.LocalFetchEngine().fetch("https://example.com/", deadline=time.monotonic() + 5))
    await asyncio.wait_for(cleaning.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("spawn_delay", [0, 0.4], ids=["normal-start", "slow-start"])
async def test_active_worker_cap_and_cancellation_safe_host_release(monkeypatch, spawn_delay):
    script = '''import sys,time,json
from urllib.parse import urlsplit
request=json.loads(sys.stdin.readline())
print(json.dumps({"event":"host","host":urlsplit(request["url"]).hostname}),flush=True); sys.stdin.readline()
print('{"event":"sent"}',flush=True); sys.stdin.readline()
time.sleep(60)
'''
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    children, spawn, spawn_attempts = [], asyncio.create_subprocess_exec, []
    reaping, release = asyncio.Event(), asyncio.Event()
    async def capture(*args, **kwargs):
        spawn_attempts.append(1)
        if spawn_delay:
            await asyncio.sleep(spawn_delay)
        child = await spawn(*args, **kwargs)
        children.append(child)
        if len(children) == 1:
            wait = child.wait
            async def delayed_reap():
                reaping.set()
                await release.wait()
                return await wait()
            monkeypatch.setattr(child, "wait", delayed_reap)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    engine, began = local_fetch.LocalFetchEngine(), asyncio.Event()
    first = asyncio.create_task(engine.fetch("https://example.com/", deadline=time.monotonic() + 10, on_sent=began.set))
    await asyncio.wait_for(began.wait(), 5)
    first.cancel()
    await asyncio.wait_for(reaping.wait(), 5)
    first.cancel()  # Stop arriving during timeout/Stop cleanup must not strand admission.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert children[0].returncode is not None
    began.clear()
    again = asyncio.create_task(engine.fetch("https://example.com/", deadline=time.monotonic() + 10, on_sent=began.set))
    try:
        await asyncio.wait_for(began.wait(), 2)
    finally:
        again.cancel()
        await asyncio.gather(again, return_exceptions=True)
    children.clear()
    spawn_attempts.clear()
    # Distinct hosts still have one aggregate process cap.
    sent, ready = 0, asyncio.Event()
    worker_cap = local_fetch._WORKERS
    def mark_sent():
        nonlocal sent
        sent += 1
        if sent == worker_cap:
            ready.set()
    tasks = [asyncio.create_task(engine.fetch(f"https://host{i}.example/", deadline=time.monotonic() + 10,
                                             on_sent=mark_sent)) for i in range(20)]
    try:
        await asyncio.wait_for(ready.wait(), 10)
        assert sent == worker_cap
        assert len(spawn_attempts) == len(children) == worker_cap == 8
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(child.returncode is not None for child in children)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inherited rlimits")
def test_worker_respects_stricter_inherited_kernel_limits():
    import subprocess
    from pathlib import Path
    code = '''import resource,sys,json
sys.path.insert(0,sys.argv[1])
from core.web_fetch_worker.worker import resource_limits
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
resource.setrlimit(resource.RLIMIT_FSIZE,(65536,65536))
resource_limits()
print(json.dumps([resource.getrlimit(resource.RLIMIT_CPU),resource.getrlimit(resource.RLIMIT_FSIZE)]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve().parents[1])],
                            capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout) == [[20, 20], [65536, 65536]]


async def test_timeout_and_stop_kill_and_reap_worker(monkeypatch):
    # A test-owned worker simulates a parser that cannot yield to cancellation.
    # The real parent must terminate it; no production network override exists.
    script = "import sys,time; sys.stdin.readline(); print('{\"event\":\"sent\"}',flush=True); sys.stdin.readline(); time.sleep(60)"
    monkeypatch.setattr(local_fetch, "_WORKER", script)
    children = []
    spawn = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    engine = local_fetch.LocalFetchEngine()
    sent = []
    result = await engine.fetch("https://example.com/", deadline=time.monotonic() + 0.5, on_sent=lambda: sent.append(1))
    assert result["error_code"] == "fetch_timeout" and result["sent"]
    assert sent == [1] and children[0].returncode is not None
    called = asyncio.Event()
    task = asyncio.create_task(engine.fetch("https://example.com/", deadline=time.monotonic() + 30, on_sent=called.set))
    await asyncio.wait_for(called.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[1].returncode is not None
