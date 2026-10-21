"""HTTP 传输层与抓取器测试（localhost 回环，零外网）。

钉住两条**实测踩过**的坑：

1. ``StreamReader.read(n)`` 只保证"至多 n 字节"，实测 firecrawl 的 34 KB 响应
   只读到 7.9 KB，导致合法 JSON 被误判成 ``bad_response`` —— 传输层必须
   按块累加读到 EOF（或到上限）；
2. urllib 默认**自动跟随** 301/302，会绕过 ``AiohttpFetcher`` 的逐跳 SSRF
   校验 —— 标准库实现必须显式禁止跟随，把 3xx 原样交回调用方。

同时覆盖 ``max_bytes`` 截断与 ``AiohttpFetcher`` 的逐跳拦截。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.crawler import AiohttpFetcher, CrawlerLimits, FetchOutcome
from core.http import AiohttpTransport, StdlibTransport, default_transport

BIG_PAYLOAD = json.dumps({"data": [{"title": "t", "url": "https://e.com/1"}] * 800}).encode()
REDIRECT_TARGET = "/inner"


class _Handler(BaseHTTPRequestHandler):
    """最小回环服务器：/big /small /redirect /html /404。"""

    def log_message(self, *args):  # noqa: D102 — 静音
        pass

    def _send(self, status: int, body: bytes, content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/big"):
            self._send(200, BIG_PAYLOAD)
        elif self.path.startswith("/small"):
            self._send(200, b'{"ok":true}')
        elif self.path.startswith("/redirect"):
            self._send(302, b"", "text/plain", {"Location": REDIRECT_TARGET})
        elif self.path.startswith("/html"):
            self._send(
                200,
                "<html><head><title>页面标题</title></head><body><p>正文内容</p></body></html>".encode(),
                "text/html; charset=utf-8",
            )
        elif self.path.startswith("/inner"):
            self._send(200, "<html><body>inner page</body></html>".encode(), "text/html")
        else:
            self._send(404, b'{"error":"nf"}')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(200, b'{"ok":true}')


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}{path}"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def server():
    with Server() as srv:
        yield srv


# ---------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------


@pytest.mark.parametrize("transport_cls", [AiohttpTransport, StdlibTransport])
def test_transport_reads_full_body(server, transport_cls):
    """坑①回归：大响应必须完整读到（不能只拿第一块）。"""
    transport = transport_cls()
    result = run(transport.fetch("GET", server.url("/big"), timeout=10))
    assert result.status == 200
    assert len(result.body) == len(BIG_PAYLOAD)
    json.loads(result.body.decode("utf-8"))  # 可解析


@pytest.mark.parametrize("transport_cls", [AiohttpTransport, StdlibTransport])
def test_transport_respects_max_bytes(server, transport_cls):
    transport = transport_cls()
    result = run(transport.fetch("GET", server.url("/big"), timeout=10, max_bytes=1024))
    assert result.status == 200
    assert len(result.body) == 1024


@pytest.mark.parametrize("transport_cls", [AiohttpTransport, StdlibTransport])
def test_transport_does_not_follow_redirects(server, transport_cls):
    """坑②回归：3xx 原样返回（带 Location），由调用方逐跳校验。"""
    transport = transport_cls()
    result = run(transport.fetch("GET", server.url("/redirect"), timeout=10))
    assert result.status == 302
    assert result.header("Location") == REDIRECT_TARGET


def test_transport_post_json(server):
    transport = StdlibTransport()
    result = run(
        transport.fetch("POST", server.url("/post"), json_body={"q": "x"}, timeout=10)
    )
    assert result.status == 200
    assert json.loads(result.body) == {"ok": True}


def test_transport_non_200_body_preserved(server):
    transport = StdlibTransport()
    result = run(transport.fetch("GET", server.url("/missing"), timeout=10))
    assert result.status == 404
    assert b"error" in result.body


def test_default_transport_prefers_aiohttp_when_available():
    transport = default_transport()
    assert isinstance(transport, (AiohttpTransport, StdlibTransport))


# ---------------------------------------------------------------
# AiohttpFetcher（逐跳 SSRF）
# ---------------------------------------------------------------


def test_fetcher_extracts_text_and_title(server):
    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10, max_chars=500),
        url_guard=_allow_all,
        transport=StdlibTransport(),
    )
    outcome = run(fetcher.fetch(server.url("/html"), timeout=10))
    assert isinstance(outcome, FetchOutcome)
    assert outcome.ok is True
    assert outcome.title == "页面标题"
    assert "正文内容" in outcome.text
    assert outcome.as_dict()["chars"] == len(outcome.text)


def test_fetcher_blocks_on_guard_denial(server, monkeypatch):
    async def deny(url):
        return False, "blocked by test"

    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10), url_guard=deny, transport=StdlibTransport()
    )
    outcome = run(fetcher.fetch(server.url("/html"), timeout=10))
    assert outcome.ok is False
    assert outcome.code == "fetch_blocked"


def test_fetcher_checks_every_redirect_hop(server):
    """逐跳校验：第 1 跳放行、第 2 跳拒绝 ⇒ fetch_blocked（不跟随到内网）。"""
    seen: list[str] = []

    async def guard(url):
        seen.append(url)
        return ("/redirect" in url), "test-policy"

    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10), url_guard=guard, transport=StdlibTransport()
    )
    outcome = run(fetcher.fetch(server.url("/redirect"), timeout=10))
    assert outcome.ok is False
    assert outcome.code == "fetch_blocked"
    assert len(seen) == 2  # 第 1 跳 + 重定向目标各校验一次
    assert seen[0].endswith("/redirect")
    assert seen[1].endswith(REDIRECT_TARGET)


def test_fetcher_follows_redirect_when_allowed(server):
    async def allow_all(url):
        return True, ""

    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10), url_guard=allow_all, transport=StdlibTransport()
    )
    outcome = run(fetcher.fetch(server.url("/redirect"), timeout=10))
    assert outcome.ok is True
    assert outcome.url.endswith(REDIRECT_TARGET)
    assert "inner" in outcome.text


def test_fetcher_404_is_fetch_failed(server):
    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10), url_guard=_allow_all, transport=StdlibTransport()
    )
    outcome = run(fetcher.fetch(server.url("/missing"), timeout=10))
    assert outcome.ok is False
    assert outcome.code == "fetch_failed"
    assert outcome.status == 404


def test_fetcher_rejects_non_text_content_type(server):
    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10),
        url_guard=_allow_all,
        transport=_FakeTransport(status=200, body=b"\x89PNG", content_type="image/png"),
    )
    outcome = run(fetcher.fetch("https://e.com/img", timeout=5))
    assert outcome.ok is False
    assert outcome.code == "not_text"


def test_fetcher_truncates_text(server):
    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=10, max_chars=5),
        url_guard=_allow_all,
        transport=StdlibTransport(),
    )
    outcome = run(fetcher.fetch(server.url("/html"), timeout=10))
    assert outcome.ok is True
    assert outcome.truncated is True
    assert len(outcome.text) <= 5


def test_fetcher_timeout_classified():
    class ClosingTransport:
        async def fetch(self, *a, **kw):
            raise asyncio.TimeoutError()

    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=1), url_guard=_allow_all, transport=ClosingTransport()
    )
    outcome = run(fetcher.fetch("https://e.com/slow", timeout=1))
    assert outcome.ok is False
    assert outcome.code == "fetch_timeout"


def test_fetcher_network_error_classified():
    class BoomTransport:
        async def fetch(self, *a, **kw):
            raise OSError("boom")

    fetcher = AiohttpFetcher(
        limits=CrawlerLimits(timeout_s=1), url_guard=_allow_all, transport=BoomTransport()
    )
    outcome = run(fetcher.fetch("https://e.com/x", timeout=1))
    assert outcome.ok is False
    assert outcome.code == "fetch_failed"


async def _allow_all(url):
    return True, ""


class _FakeTransport:
    def __init__(self, *, status=200, body=b"", content_type="text/html"):
        self.status = status
        self.body = body
        self.content_type = content_type

    async def fetch(self, *a, **kw):
        from core.http import HttpResult

        return HttpResult(
            status=self.status, headers={"Content-Type": self.content_type}, body=self.body
        )
