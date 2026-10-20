"""Read a user-supplied official HTML page on demand; never search or adopt facts."""

from __future__ import annotations

import gzip
import io
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit


MAX_BYTES = 1_000_000
MAX_URL_LENGTH = 2048


class OfficialSourceUnavailable(ValueError):
    """The page could not be safely read as an official HTML source."""


@dataclass(frozen=True)
class OfficialPage:
    url: str
    title: str
    excerpt: str
    stated_date: str
    fetched_at: str


def official_url(value: str) -> bool:
    """Allow only ordinary HTTPS government URLs without userinfo or custom ports."""
    if not value or len(value) > MAX_URL_LENGTH or any(ord(c) < 33 for c in value):
        return False
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        return (parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443) and not parts.fragment
                and (host == "gov.cn" or host.endswith(".gov.cn")))
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise OfficialSourceUnavailable("页面发生跳转；请核对最终官方链接后重新提交。")


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title = []
        self.text = []
        self.date = ""

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            fields = dict(attrs)
            name = (fields.get("name") or fields.get("property") or "").lower()
            if name in {"publishdate", "pubdate", "article:published_time", "date"}:
                self.date = (fields.get("content") or "")[:80]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)


def read_official_page(url: str, *, network_enabled: bool,
                       timeout_seconds: float = 10.0, transport=None) -> OfficialPage:
    """GET exactly the submitted URL. No redirect, search query, or event payload."""
    url = (url or "").strip()
    if not official_url(url):
        raise OfficialSourceUnavailable("仅接受 gov.cn 域名下的 HTTPS 官方网页链接。")
    if not network_enabled:
        raise OfficialSourceUnavailable("联网资料核验未启用。")
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "text/html", "Accept-Encoding": "identity"})
    fetch = transport or urllib.request.build_opener(_NoRedirect()).open
    try:
        with fetch(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            if final_url != url or not official_url(final_url):
                raise OfficialSourceUnavailable("响应地址与提交链接不一致，未采用页面。")
            content_type = response.headers.get_content_type().lower()
            if content_type != "text/html":
                raise OfficialSourceUnavailable("当前只读取官方 HTML 网页；PDF 请使用已核验的本地标准文本。")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise OfficialSourceUnavailable("官方网页超过读取上限，未采用摘录。")
            encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(MAX_BYTES + 1)
            elif encoding != "identity":
                raise OfficialSourceUnavailable("官方网页使用不支持的内容编码，未采用摘录。")
            if len(raw) > MAX_BYTES:
                raise OfficialSourceUnavailable("官方网页超过读取上限，未采用摘录。")
            charset = response.headers.get_content_charset() or "utf-8"
        parser = _PageText()
        parser.feed(raw.decode(charset, errors="replace"))
    except OfficialSourceUnavailable:
        raise
    except (OSError, EOFError, UnicodeError, LookupError, ValueError) as exc:
        raise OfficialSourceUnavailable("官方网页读取失败；请核对链接或稍后重试。") from exc
    title = " ".join(" ".join(parser.title).split())[:160]
    body = " ".join(" ".join(parser.text).split())
    if not title or not body:
        raise OfficialSourceUnavailable("网页正文或标题为空，未采用摘录。")
    stated_date = parser.date
    if not stated_date:
        for label in ("发布日期", "发布时间", "成文日期"):
            match = re.search(
                label + r"\s*[：:]?\s*([12]\d{3}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)",
                body)
            if match:
                stated_date = match.group(1)
                break
    return OfficialPage(
        url=url, title=title, excerpt=body[:2400], stated_date=stated_date,
        fetched_at=datetime.now(timezone.utc).isoformat())
