"""Bounded page extraction, used only in a resource-limited disposable process."""
from __future__ import annotations

import codecs
import io
import json
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .fetch_policy import FetchFailure, metadata_url, parse_url


@dataclass
class Page:
    content: str
    content_type: str
    source_content_type: str
    title: str | None = None
    canonical_url: str | None = None
    follow_url: str | None = None
    follow_kind: str | None = None
    charset_fallback: bool = False
    content_overflow: bool = False

    def payload(self) -> dict:
        return asdict(self)


def _decode(data: bytes, content_type: str) -> tuple[str, bool]:
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode("utf-32", errors="replace"), False
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace"), False
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace"), False
    encodings = re.findall(r'charset\s*=\s*[\"\']?([a-zA-Z0-9_.-]+)', content_type, flags=re.I)
    # Only a bounded HTML prefix may declare a meta charset.
    if "html" in content_type.lower():
        for tag in re.findall(rb"<meta\b[^>]{0,1024}>", data[:8192], flags=re.I):
            encodings += re.findall(r'charset\s*=\s*[\"\']?([a-zA-Z0-9_.-]+)', tag.decode("ascii", "ignore"), flags=re.I)
    for encoding in encodings:
        try:
            codec = codecs.lookup(encoding)
            # Refuse non-text transforms (e.g. base64) and stateful escape codecs.
            if codec.name.startswith(("utf-", "iso8859", "cp", "windows", "shift_jis", "euc", "gb", "big5", "ascii", "latin")):
                return data.decode(encoding, errors="replace"), False
        except (LookupError, UnicodeError):
            pass
    try:
        return data.decode("utf-8"), False
    except UnicodeError:
        return data.decode("utf-8", errors="replace"), True


def quoted_text(text: str) -> tuple[str, bool]:
    """Keep literal text/PDF/JSON intact; never parse it as Markdown or HTML."""
    overflow = len(text) > 48000
    text = text[:48000]
    choices = [(max((len(m.group()) for m in re.finditer(re.escape(char) + "+", text)), default=0), char)
               for char in ("`", "~")]
    longest, char = min(choices)
    fence = char * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}", overflow


class _VisibleHTML(HTMLParser):
    """Retain visible quotation text, never active tags or script/form contents."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "form", "iframe", "object", "embed"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"br", "p", "div", "details", "summary", "li", "pre", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.hidden:
            del self.hidden[self.hidden.index(tag):]
        if not self.hidden and tag in {"p", "div", "details", "summary", "li", "pre", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(re.sub(r"([\\`*_[\]<>])", r"\\\1", data))

    def take(self):
        text, self.parts = "".join(self.parts), []
        return text


def inert_markdown(text: str, base: str) -> str:
    """Render parsed Markdown tokens back to inert Markdown, without image loads.

    Parsing instead of regex link replacement handles reference/escaped links.
    Visible HTML text is quoted without tags, code remains quoted, and only HTTP(S) links survive.
    """
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": True})
    tokens = parser.parse(text)

    def inline(children):
        result, links = [], []
        html = _VisibleHTML()
        for token in children or ():
            if token.type == "html_inline":
                html.feed(token.content)
                result.append(html.take())
                continue
            if html.hidden:
                continue
            if token.type == "text":
                # Text may itself contain syntax from an invalid/unsafe link.
                result.append(re.sub(r"([\\`*_[\]<>])", r"\\\1", token.content))
            elif token.type in {"softbreak", "hardbreak"}:
                result.append("\n")
            elif token.type == "code_inline":
                ticks = "`" * (max((len(m.group()) for m in re.finditer(r"`+", token.content)), default=0) + 1)
                result.append(f"{ticks} {token.content} {ticks}")
            elif token.type in {"em_open", "em_close", "strong_open", "strong_close"}:
                result.append(token.markup)
            elif token.type == "link_open":
                href = metadata_url(token.attrGet("href"), base)
                links.append(href)
                if href:
                    result.append("[")
            elif token.type == "link_close":
                href = links.pop() if links else None
                if href:
                    result.append(f"](<{href}>)")
            elif token.type == "image":
                # Alt text only; no remote-image Markdown survives.
                result.append(inline(token.children))
        return "".join(result)

    output, lists = [], []
    for token in tokens:
        if token.type == "inline":
            output.append(inline(token.children))
        elif token.type == "html_block":
            html = _VisibleHTML()
            html.feed(token.content)
            output.append(html.take() + "\n\n")
        elif token.type == "heading_open":
            output.append("\n" + "#" * int(token.tag[1:]) + " ")
        elif token.type in {"paragraph_close", "heading_close", "blockquote_close"}:
            output.append("\n\n")
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            lists.append(int(token.attrGet("start") or 1) if token.type == "ordered_list_open" else None)
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            if lists:
                lists.pop()
            output.append("\n\n")
        elif token.type == "list_item_open":
            marker = "- "
            if lists and lists[-1] is not None:
                marker = f"{lists[-1]}. "
                lists[-1] += 1
            output.append("\n" + marker)
        elif token.type == "blockquote_open":
            output.append("\n> ")
        elif token.type in {"fence", "code_block"}:
            ticks = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", token.content)), default=0))
            output.append(f"\n{ticks}\n{token.content}\n{ticks}\n")
        elif token.type == "hr":
            output.append("\n---\n")
    return "".join(output).strip()


def _same_origin_link(value: str, base: str) -> str | None:
    try:
        if any(c.isspace() for c in value) or "\\" in value:
            return None
        candidate = parse_url(urljoin(base, value)).url
        a, b = urlsplit(candidate), urlsplit(base)
        origin = lambda p: (p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80))
        if origin(a) == origin(b) and not a.username and not a.password and len(candidate) <= 8192:
            return candidate
    except (ValueError, FetchFailure):
        pass
    return None


def extract_page(data: bytes, content_type: str, url: str) -> Page:
    mime = content_type.partition(";")[0].strip().lower()
    if mime == "application/pdf":
        from pypdf import PdfReader

        try:
            pdf = PdfReader(io.BytesIO(data), strict=False)
            if pdf.is_encrypted:
                raise FetchFailure("fetch_target_unreadable")
            parts, total, overflow = [], 0, False
            for number, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(f"[Page {number}]\n{text}")
                    total += len(text)
                    if total > 48000:
                        overflow = True
                        break
            content = "\n\n---\n\n".join(parts)
            title = str(pdf.metadata.title)[:300] if pdf.metadata and pdf.metadata.title else None
        except FetchFailure:
            raise
        except Exception:
            raise FetchFailure("fetch_target_unreadable") from None
        if not content.strip():
            raise FetchFailure("fetch_target_unreadable")
        # Plain text is fenced so PDF text cannot create active markup.
        content, cut = quoted_text(content)
        return Page(content, "text/markdown", mime, title=title, content_overflow=overflow or cut)
    if mime not in {"text/html", "application/xhtml+xml", "text/markdown", "text/plain", "application/json"} and not mime.endswith("+json"):
        raise FetchFailure("fetch_unsupported_content")
    text, fallback = _decode(data, content_type)
    if "\x00" in text:
        raise FetchFailure("fetch_unsupported_content")
    if not text.strip():
        raise FetchFailure("fetch_target_unreadable")
    if mime in {"text/html", "application/xhtml+xml"}:
        import trafilatura
        from lxml import etree, html

        # lxml's Unicode parser rejects an XML encoding declaration even though
        # decoding has already honored the HTTP/BOM/meta encoding above.
        text = re.sub(r"^\s*<\?xml\b[^?]{0,1024}\?>", "", text, count=1, flags=re.I)
        try:
            tree = html.fromstring(text, parser=html.HTMLParser(no_network=True, recover=True, remove_comments=True))
        except (ValueError, TypeError, etree.ParserError):
            raise FetchFailure("fetch_target_unreadable") from None
        titles = tree.xpath("//title/text()")
        title = titles[0].strip()[:300] if titles else None
        # Structural challenge/login evidence, not phrases in article text.
        # A login widget beside readable public content is NOT a login wall.
        password_forms = tree.xpath('//form[.//input[translate(@type,"PASSWORD","password")="password"]]')
        has_password_form = bool(password_forms)
        main_login = any(not form.xpath("ancestor::header|ancestor::nav|ancestor::aside") for form in password_forms)
        if tree.xpath('//*[@id="challenge-form" or @id="cf-challenge-running" or @id="cf-spinner-please-wait"]'):
            raise FetchFailure("fetch_challenge")
        page = Page("", "text/markdown", mime, title=title, charset_fallback=fallback)
        canonical = tree.xpath('//link[contains(concat(" ",normalize-space(@rel)," ")," canonical ")]/@href')
        page.canonical_url = metadata_url(canonical[0], url) if canonical else None
        for meta in tree.xpath('//meta[translate(@http-equiv,"REFSH","refsh")="refresh"]/@content'):
            match = re.fullmatch(r"\s*0(?:\.0+)?\s*;\s*url\s*=\s*(.*?)\s*", meta, flags=re.I)
            if match and (follow := _same_origin_link(match[1].strip("\"'"), url)):
                page.follow_url, page.follow_kind = follow, "refresh"
                break
        if page.follow_url is None:
            alternate = tree.xpath('//link[contains(concat(" ",normalize-space(@rel)," ")," alternate ") and @type="text/markdown"]/@href')
            if alternate and (follow := _same_origin_link(alternate[0], url)):
                page.follow_url, page.follow_kind = follow, "alternate"
        has_scripts = bool(tree.xpath("//script"))
        for element in tree.xpath("//script|//style|//nav|//form|//iframe|//img|//svg|//object|//embed"):
            element.drop_tree()
        for anchor in tree.xpath("//*[@href]"):
            link = metadata_url(anchor.get("href"), url)
            if link:
                anchor.set("href", link)
            else:
                anchor.attrib.pop("href", None)
        content = trafilatura.extract(tree, output_format="markdown", include_links=True, include_images=False,
                                      include_comments=False, include_tables=True, favor_recall=True) or ""
        normalized = " ".join(content.split()).strip("# ").lower()
        if has_scripts and not main_login and (not normalized or normalized in {
            "loading", "loading...", "please enable javascript", "javascript is required",
        }) and page.follow_url is None:
            raise FetchFailure("fetch_js_shell")
        if has_password_form and (not normalized or normalized in {
            "sign in", "log in", "login", "please sign in", "sign in to continue", "login required", "log in to continue",
        }):
            raise FetchFailure("fetch_refused", terminal=True)
        if (not normalized or (has_scripts and normalized in {"loading", "loading...", "please enable javascript", "javascript is required"})) and page.follow_url is None:
            raise FetchFailure("fetch_js_shell" if has_scripts else "fetch_target_unreadable")
        # Bound token construction before markdown-it, not after its allocation.
        page.content = inert_markdown(content[:192000], url)
        page.content_overflow = len(content) > 192000 or len(page.content) > 48000
        page.content = page.content[:48000]
        return page
    if mime == "application/json" or mime.endswith("+json"):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            pass
        content, overflow = quoted_text(text)
    else:
        # Recognize only an exact challenge document, never a phrase within text.
        if text.strip().lower() in {"access denied", "just a moment...", "please enable javascript", "checking your browser..."}:
            raise FetchFailure("fetch_challenge")
        if not text.strip():
            raise FetchFailure("fetch_target_unreadable")
        if mime == "text/plain":
            content, overflow = quoted_text(text)
        else:
            content = inert_markdown(text[:192000], url)
            overflow = len(text) > 192000 or len(content) > 48000
            content = content[:48000]
    if not content.strip():
        raise FetchFailure("fetch_target_unreadable")
    return Page(content, "text/markdown", mime, charset_fallback=fallback, content_overflow=overflow)
