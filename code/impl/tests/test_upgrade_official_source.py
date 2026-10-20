"""Official page reader never searches or sends event context."""

import io
import gzip
import unittest
from email.message import Message

from panda_brain.official_source import (
    OfficialSourceUnavailable, _NoRedirect, official_url, read_official_page,
)


class _Response:
    def __init__(self, url, body, content_type="text/html; charset=utf-8",
                 content_encoding=""):
        self.url = url
        self.body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        return self.body.read(size)


class OfficialSourceTests(unittest.TestCase):
    URL = "https://www.mem.gov.cn/gk/notice.html"

    def test_default_gate_blocks_transport(self):
        calls = []
        with self.assertRaisesRegex(OfficialSourceUnavailable, "未启用"):
            read_official_page(self.URL, network_enabled=False,
                               transport=lambda *a, **k: calls.append((a, k)))
        self.assertEqual([], calls)

    def test_only_exact_government_https_origin(self):
        for url in (
            "http://www.mem.gov.cn/a",
            "https://gov.cn@evil.example/a",
            "https://www.mem.gov.cn.evil.example/a",
            "https://www.mem.gov.cn:444/a",
            "https://127.0.0.1/a",
            "https://www.mem.gov.cn/a#fragment",
            "https://www.mem.gov.cn/a\nHost: evil.example",
        ):
            self.assertFalse(official_url(url), url)
        self.assertTrue(official_url(self.URL))

    def test_bounded_html_excerpt_and_claimed_date(self):
        calls = []
        html = ('<html><head><title> 官方公告 </title>'
                '<meta name="PubDate" content="2026-10-05"></head>'
                '<body><script>evil()</script><p>检查现场安全</p></body></html>').encode()
        def transport(request, *, timeout):
            calls.append((request, timeout))
            return _Response(self.URL, html)
        page = read_official_page(self.URL, network_enabled=True,
                                  timeout_seconds=3, transport=transport)
        self.assertEqual("官方公告", page.title)
        self.assertEqual("2026-10-05", page.stated_date)
        self.assertIn("检查现场安全", page.excerpt)
        self.assertNotIn("evil()", page.excerpt)
        self.assertEqual("GET", calls[0][0].get_method())
        self.assertEqual(self.URL, calls[0][0].full_url)
        self.assertIsNone(calls[0][0].data)
        self.assertEqual(3, calls[0][1])

    def test_redirect_pdf_and_oversize_rejected(self):
        with self.assertRaisesRegex(OfficialSourceUnavailable, "跳转"):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")
        with self.assertRaisesRegex(OfficialSourceUnavailable, "不一致"):
            read_official_page(self.URL, network_enabled=True,
                               transport=lambda *_a, **_k: _Response(
                                   "https://evil.example/", b"<title>x</title>"))
        with self.assertRaisesRegex(OfficialSourceUnavailable, "HTML"):
            read_official_page(self.URL, network_enabled=True,
                               transport=lambda *_a, **_k: _Response(
                                   self.URL, b"%PDF", "application/pdf"))
        with self.assertRaisesRegex(OfficialSourceUnavailable, "上限"):
            read_official_page(self.URL, network_enabled=True,
                               transport=lambda *_a, **_k: _Response(
                                   self.URL, b"x" * 1_000_001))

    def test_official_gzip_html_is_bounded_after_decompression(self):
        html = '<html><head><title>应急公告</title></head><body>公开标准目录</body></html>'.encode()
        page = read_official_page(
            self.URL, network_enabled=True,
            transport=lambda *_a, **_k: _Response(
                self.URL, gzip.compress(html), content_encoding="gzip"))
        self.assertEqual("应急公告", page.title)
        self.assertIn("公开标准目录", page.excerpt)
        with self.assertRaisesRegex(OfficialSourceUnavailable, "上限"):
            read_official_page(
                self.URL, network_enabled=True,
                transport=lambda *_a, **_k: _Response(
                    self.URL, gzip.compress(b"x" * 1_000_001), content_encoding="gzip"))
        with self.assertRaisesRegex(OfficialSourceUnavailable, "编码"):
            read_official_page(
                self.URL, network_enabled=True,
                transport=lambda *_a, **_k: _Response(
                    self.URL, html, content_encoding="br"))

    def test_visible_publication_date_precedes_document_date(self):
        html = ('<html><title>公告</title><body>成文日期：2025年10月15日 '
                '发布日期：2025年11月03日 正文</body></html>').encode()
        page = read_official_page(
            self.URL, network_enabled=True,
            transport=lambda *_a, **_k: _Response(self.URL, html))
        self.assertEqual('2025年11月03日', page.stated_date)


if __name__ == "__main__":
    unittest.main()
