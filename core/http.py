"""HTTP 传输层（搜索 provider 与正文抓取共用）。

只提供「一次请求 -> 状态码/头/字节体」这一层，**不做**业务解析、**不做**
URL 安全校验、**不回显**请求头/请求体（避免把密钥带进日志或异常）。

默认实现优先 aiohttp（AstrBot 自带依赖，``main.py`` 媒体下载已在用），环境里
没有 aiohttp 时回退标准库 ``urllib``（放到线程里执行，不阻塞事件循环）。
测试通过 ``transport=`` 注入假实现，全程零网络。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

#: 单次响应体读取上限（防异常端点灌爆内存）。
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass
class HttpResult:
    """一次 HTTP 调用的最小结果（不含请求体回显，便于脱敏）。"""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""

    def header(self, name: str) -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return ""


class HttpTransport(Protocol):
    async def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 10.0,
        proxy: str = "",
        max_bytes: int = MAX_BODY_BYTES,
    ) -> HttpResult: ...


class AiohttpTransport:
    """aiohttp 实现（不自动跟随重定向：逐跳校验由调用方负责）。"""

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 10.0,
        proxy: str = "",
        max_bytes: int = MAX_BODY_BYTES,
    ) -> HttpResult:
        import aiohttp  # noqa: PLC0415 — 延迟导入：无 aiohttp 的环境仍可 import 本模块

        client_timeout = aiohttp.ClientTimeout(total=float(timeout))
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.request(
                method,
                url,
                headers=headers or None,
                params=params or None,
                json=json_body,
                allow_redirects=False,
                proxy=proxy or None,
            ) as resp:
                # 注意：``StreamReader.read(n)`` 只保证"至少 1 字节、至多 n 字节"，
                # 拿到的可能是**半个报文**（实测 firecrawl 响应 34 KB 只读到 7.9 KB，
                # 随后被误判成 bad_response）。这里按块累加并显式截断到 max_bytes。
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                return HttpResult(
                    status=int(resp.status),
                    headers={k: v for k, v in resp.headers.items()},
                    body=body,
                )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向（把 3xx 当普通响应交回调用方）。

    为什么必须禁：``AiohttpFetcher`` 抓正文时**逐跳**做 SSRF 校验，自动跟随会
    让第 2 跳绕过校验直达内网（``main.py`` 媒体下载踩过同一个坑）。urllib 默认
    跟随 301/302，因此这里显式拦住——不返回新 Request 即可，3xx 会以
    ``HTTPError`` 形式交回，由 ``_fetch_sync`` 的 ``except HTTPError`` 分支
    当作"普通响应"返回（status + Location 都在）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class StdlibTransport:
    """标准库 urllib 实现（无 aiohttp 时的兜底；阻塞调用放线程里跑）。"""

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 10.0,
        proxy: str = "",
        max_bytes: int = MAX_BODY_BYTES,
    ) -> HttpResult:
        return await asyncio.to_thread(
            self._fetch_sync,
            method,
            url,
            headers,
            params,
            json_body,
            timeout,
            proxy,
            max_bytes,
        )

    @staticmethod
    def _fetch_sync(
        method: str,
        url: str,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        timeout: float,
        proxy: str,
        max_bytes: int,
    ) -> HttpResult:
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")}
            )
            if query:
                sep = "&" if urllib.parse.urlparse(url).query else "?"
                url = f"{url}{sep}{query}"
        data = None
        final_headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url, data=data, headers=final_headers, method=method.upper()
        )
        # 必须带 _NoRedirectHandler：默认 opener 会自动跟随 3xx（绕开逐跳校验）。
        handlers = [_NoRedirectHandler()]
        if proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=float(timeout)) as resp:
                return HttpResult(
                    status=int(getattr(resp, "status", 200)),
                    headers={k: v for k, v in resp.headers.items()},
                    body=resp.read(max_bytes),
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是"成功拿到响应"
            return HttpResult(
                status=int(exc.code),
                headers={k: v for k, v in (exc.headers or {}).items()},
                body=exc.read(max_bytes) if exc.fp else b"",
            )


def default_transport() -> HttpTransport:
    """优先 aiohttp；环境里没有时回退标准库（两者都不需要额外安装）。"""
    try:
        import aiohttp  # noqa: F401, PLC0415 — 仅探测可用性

        return AiohttpTransport()
    except Exception:  # noqa: BLE001 — ImportError 及任何探测异常
        return StdlibTransport()
