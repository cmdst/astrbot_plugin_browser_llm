"""爬虫后端：HTTP 取源码 -> 纯文本（v4 起由 ``browser_web_search`` 使用）。

只做"HTTP 取源码 -> 纯文本"这一段，不执行 JS、不截图、不带登录态。
**URL 安全校验不归本模块**：调用方（``main.py``）注入 ``url_guard``（异步，
通常是 ``SafetyFilter.acheck_url``，含 DNS），``AiohttpFetcher`` 在**每一跳**
重定向前都调一次；本模块不信任入参。

唯一入口是 ``AiohttpFetcher.fetch()``（搜索结果抓正文，``search.fetch_pages``）。
v1.4.0 清理删掉了 ``PageContent`` / ``PageBackend`` / ``open_page`` / ``html_links``：
它们只服务已删除的 ``core/pages.py``（浏览器→爬虫自动回退，v4 非目标）。
"""

from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urljoin

_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
)

_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class CrawlerLimits:
    timeout_s: float = 15.0
    max_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 5
    max_chars: int = 20000
    user_agent: str = "AstrBot-BrowserLLM/1.0"


def is_allowed_content_type(content_type: str) -> bool:
    """只接受可解析为文本的 Content-Type；缺失时按 html 处理。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return True
    return any(ct == allowed or ct.endswith("+" + allowed.split("/")[-1])
               for allowed in _ALLOWED_CONTENT_TYPES) or ct.startswith("text/")


def html_title(html: str) -> str:
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    return _html.unescape(_TAG_RE.sub("", match.group(1))).strip()[:200]


def html_to_text(html: str, *, max_chars: int = 20000) -> tuple[str, bool]:
    """HTML -> 纯文本。返回 (文本, 是否截断)。"""
    raw = html or ""
    raw = _COMMENT_RE.sub(" ", raw)
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"</(p|div|li|tr|h[1-6])\s*>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", raw)
    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _NL_RE.sub("\n\n", text).strip()
    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip()
        truncated = True
    return text, truncated


def decode_body(body: bytes, encoding: str | None = None) -> str:
    """编码回退：声明 -> utf-8 -> gb18030 -> latin-1（永不抛异常）。"""
    if not body:
        return ""
    candidates = [encoding, "utf-8", "gb18030", "latin-1"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def apply_size_limit(body: bytes, *, max_bytes: int) -> tuple[bytes, bool]:
    if max_bytes > 0 and len(body) > max_bytes:
        return body[:max_bytes], True
    return body, False


# ---------------------------------------------------------------
# v4：抓取器（搜索结果的 fetch_pages）
# ---------------------------------------------------------------


@dataclass
class FetchOutcome:
    """一次抓取的结论（失败也是**值**，不抛异常给调用方）。"""

    ok: bool = False
    code: str = "fetch_failed"  # ok / fetch_blocked / fetch_failed / fetch_timeout / not_text
    status: int = 0
    url: str = ""
    title: str = ""
    text: str = ""
    truncated: bool = False
    nbytes: int = 0
    elapsed_ms: int = 0

    def as_dict(self, *, max_text: int = 0) -> dict:
        """转成返回体 ``results[].fetched`` 的形状（可选再截一次文本）。"""
        text = self.text
        truncated = self.truncated
        if max_text and len(text) > max_text:
            text = text[:max_text]
            truncated = True
        return {
            "ok": self.ok,
            "code": self.code,
            "status": self.status,
            "chars": len(text),
            "truncated": truncated,
            "text": text,
            "elapsed_ms": self.elapsed_ms,
        }


#: 重定向状态码。
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

UrlGuard = Callable[[str], Awaitable[tuple[bool, str]]]


class AiohttpFetcher:
    """搜索结果正文抓取器：逐跳 SSRF 校验 + 限长 + 文本化。

    Args:
        limits: 超时/字节/字符上限（``CrawlerLimits``）。
        url_guard: 异步 URL 校验回调 ``(url) -> (ok, reason)``；每跳都调。
            通常是 ``SafetyFilter.acheck_url``（含 DNS）。None 表示不校验
            （仅测试用，生产由 ``main.py`` 强制注入）。
        proxy: 代理地址（``search.proxy``）；留空直连。
        transport: HTTP 传输层（测试注入假实现）。
    """

    def __init__(
        self,
        *,
        limits: CrawlerLimits | None = None,
        url_guard: UrlGuard | None = None,
        proxy: str = "",
        transport=None,
    ):
        self.limits = limits or CrawlerLimits()
        self.url_guard = url_guard
        self.proxy = proxy or ""
        self._transport = transport

    @property
    def transport(self):
        if self._transport is None:
            from .http import default_transport  # noqa: PLC0415 — 延迟导入

            self._transport = default_transport()
        return self._transport

    async def _guard(self, url: str) -> tuple[bool, str]:
        if self.url_guard is None:
            return True, ""
        try:
            return await self.url_guard(url)
        except Exception as exc:  # noqa: BLE001 — 校验器异常按"拦截"处理（fail-closed）
            return False, type(exc).__name__

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchOutcome:
        """抓取一页并返回文本（任何失败都落 ``FetchOutcome.code``）。"""
        timeout_s = float(timeout if timeout is not None else self.limits.timeout_s)
        began = time.monotonic()
        outcome = FetchOutcome(url=str(url or ""))
        current = str(url or "").strip()
        if not current:
            outcome.code = "fetch_blocked"
            return outcome

        headers = {
            "User-Agent": self.limits.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        }
        for _hop in range(int(self.limits.max_redirects) + 1):
            ok, _reason = await self._guard(current)
            if not ok:
                outcome.code = "fetch_blocked"
                outcome.url = current
                outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
                return outcome
            try:
                response = await self.transport.fetch(
                    "GET",
                    current,
                    headers=headers,
                    timeout=timeout_s,
                    proxy=self.proxy,
                    max_bytes=self.limits.max_bytes,
                )
            except Exception as exc:  # noqa: BLE001 — 网络/超时统一归类
                outcome.code = (
                    "fetch_timeout"
                    if "timeout" in type(exc).__name__.lower()
                    else "fetch_failed"
                )
                outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
                return outcome

            status = int(getattr(response, "status", 0) or 0)
            if status in _REDIRECT_STATUS:
                location = response.header("Location") if hasattr(response, "header") else ""
                if not location:
                    outcome.code = "fetch_failed"
                    outcome.status = status
                    outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
                    return outcome
                current = urljoin(current, location)
                continue
            if status != 200:
                outcome.code = "fetch_failed"
                outcome.status = status
                outcome.url = current
                outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
                return outcome

            content_type = (
                response.content_type
                if hasattr(response, "content_type")
                else (response.headers.get("Content-Type", "") if response.headers else "")
            )
            if not is_allowed_content_type(content_type):
                outcome.code = "not_text"
                outcome.status = status
                outcome.url = current
                outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
                return outcome

            body = bytes(getattr(response, "body", b"") or b"")
            body, size_truncated = apply_size_limit(
                body, max_bytes=self.limits.max_bytes
            )
            text, text_truncated = html_to_text(
                decode_body(body, _charset_of(content_type)),
                max_chars=self.limits.max_chars,
            )
            outcome.ok = True
            outcome.code = "ok"
            outcome.status = status
            outcome.url = current
            outcome.title = html_title(
                decode_body(body, _charset_of(content_type))
            )
            outcome.text = text
            outcome.truncated = bool(size_truncated or text_truncated)
            outcome.nbytes = len(body)
            outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
            return outcome

        # 超过逐跳上限
        outcome.code = "fetch_blocked"
        outcome.url = current
        outcome.elapsed_ms = int((time.monotonic() - began) * 1000)
        return outcome


def _charset_of(content_type: str) -> str | None:
    """从 Content-Type 里取 charset（缺失返回 None，交给 decode_body 回退链）。"""
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"') or None
    return None
