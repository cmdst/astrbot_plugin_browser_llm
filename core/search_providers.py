"""搜索 provider 实现（v4 / S4）：brave / bocha / firecrawl / tavily / exa。

本模块只做「配置 -> HTTP 请求 -> 结构化结果」这一段，不做并发编排（那在
``core/search.py``）、不做抓正文（那在 ``core/crawler.py``）、不读任何配置
（调用方把 ``api_key`` / ``base_url`` 传进来）。

三条硬约束：

1. **密钥只从参数来**：``api_key`` 只用于鉴权头/请求体，绝不进日志、异常
   文本或 ``SearchResult``；
2. **不 import 核心私有实现**：核心的 ``astrbot/core/tools/web_search_tools.py``
   是 16 个 ``web_search_<provider>`` 内置工具的实现，本插件与之并存、不接管，
   因此不 import、不改它的配置、不读它的 key；
3. **HTTP 客户端可注入**：``transport`` 缺省用 aiohttp（AstrBot 自带依赖），
   aiohttp 不可用时回退标准库 ``urllib``（线程执行）；测试注入假 transport，
   全程零网络。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import as_bool, as_float, as_int
from .http import HttpTransport, default_transport
from .safety import check_url_literal
from .search import ProviderEntry, ProviderError, SearchProvider, SearchResult

#: 支持的 provider 模板 id（= ``_conf_schema.json`` 的 ``templates`` 键集合）。
PROVIDER_IDS: tuple[str, ...] = ("brave", "bocha", "firecrawl", "tavily", "exa")

#: 各模板的官方端点（``base_url`` 留空时使用；反代/镜像可覆盖）。
DEFAULT_ENDPOINTS: dict[str, str] = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "bocha": "https://api.bochaai.com/v1/web-search",
    "firecrawl": "https://api.firecrawl.dev/v1/search",
    "tavily": "https://api.tavily.com/search",
    "exa": "https://api.exa.ai/search",
}

#: 每模板的 query 长度上限（brave 超 400 字符会被网关拒；其余按 1000 截断）。
_QUERY_LIMITS = {"brave": 500}
_DEFAULT_QUERY_LIMIT = 1000

#: provider 模板字段（``template_list`` 的 7 个可编辑字段；``__template_key``
#: 由 Dashboard 写入，单独解析）。
TEMPLATE_FIELDS: tuple[str, ...] = (
    "enabled",
    "name",
    "api_key",
    "base_url",
    "weight",
    "timeout",
    "max_results",
)


# ---------------------------------------------------------------
# provider 基类
# ---------------------------------------------------------------


class BaseHttpProvider:
    """HTTP provider 公共骨架：鉴权、状态码分类、JSON 解析、结果裁剪。"""

    template: str = ""
    default_endpoint: str = ""

    def __init__(
        self,
        *,
        name: str = "",
        api_key: str = "",
        base_url: str = "",
        region: str = "",
        safesearch: str = "moderate",
        proxy: str = "",
        transport: HttpTransport | None = None,
    ):
        self.name = name or self.template
        self.api_key = api_key or ""
        self.base_url = base_url or ""
        self.region = region or ""
        self.safesearch = (safesearch or "moderate").lower()
        self.proxy = proxy or ""
        self._transport = transport

    # -- 子类必须实现 ------------------------------------------------

    def _endpoint(self) -> str:
        return self.base_url or self.default_endpoint

    def _request(
        self, query: str, limit: int
    ) -> tuple[str, str, dict[str, str], dict[str, Any] | None, dict[str, Any] | None]:
        """返回 ``(method, url, headers, params, json_body)``。"""
        raise NotImplementedError

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        raise NotImplementedError

    # -- 公共流程 ----------------------------------------------------

    @property
    def transport(self) -> HttpTransport:
        if self._transport is None:
            self._transport = default_transport()
        return self._transport

    def _truncate_query(self, query: str) -> str:
        limit = _QUERY_LIMITS.get(self.template, _DEFAULT_QUERY_LIMIT)
        text = (query or "").strip()
        return text[:limit]

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        """发起一次检索；失败一律抛 ``ProviderError``（带错误码）。"""
        text = self._truncate_query(query)
        if not text:
            raise ProviderError("empty_query")
        method, url, headers, params, json_body = self._request(
            text, max(1, int(limit))
        )
        if not self.base_url:
            # 官方端点恒为 https；只校验协议与主机名形态，不做 DNS。
            ok, reason = check_url_literal(url, block_internal_ip=False)
            if not ok:
                raise _provider_error("bad_config", reason)
        try:
            result = await self.transport.fetch(
                method,
                url,
                headers=headers,
                params=params,
                json_body=json_body,
                timeout=float(timeout),
                proxy=self.proxy,
            )
        except asyncio.TimeoutError as exc:
            raise _provider_error("timeout") from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — 全部归类为结构化错误码
            raise _provider_error(_classify_http_exception(exc), type(exc).__name__) from exc

        status = int(getattr(result, "status", 0) or 0)
        if status in (401, 403):
            raise _provider_error("auth", "鉴权失败", http_status=status)
        if status == 429:
            raise _provider_error("rate_limited", "触发限流", http_status=status)
        if 400 <= status < 500:
            raise _provider_error("http_4xx", "请求被拒", http_status=status)
        if status >= 500:
            raise _provider_error("http_5xx", "上游/网关错误", http_status=status)
        if status != 200:
            raise _provider_error("bad_response", "非 200 响应", http_status=status)

        try:
            payload = json.loads(result.body.decode("utf-8", errors="replace"))
        except (ValueError, TypeError) as exc:
            raise _provider_error("bad_response", "响应不是 JSON", http_status=status) from exc
        if not isinstance(payload, dict):
            raise _provider_error("bad_response", "响应结构不符", http_status=status)
        try:
            items = self._parse(payload)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _provider_error("bad_response", "响应结构不符", http_status=status) from exc
        return items[: max(1, int(limit))]


def _provider_error(code: str, message: str = "", *, http_status: int | None = None):
    """构造带 HTTP 状态码的 ``ProviderError``（状态码用于 attempts[]）。"""
    return ProviderError(code, message, http_status=http_status)


def _classify_http_exception(exc: BaseException) -> str:
    """把 HTTP 客户端异常归类成错误码（只看类型名，不读可能含 key 的文本）。"""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if any(
        token in name
        for token in ("connector", "connect", "client", "ssl", "tls", "dns", "gaierror")
    ):
        return "network"
    return "network"


def _to_result(item: dict[str, Any], *, snippet_keys: tuple[str, ...]) -> SearchResult | None:
    """把一条 provider 原始条目映射成 ``SearchResult``（缺 URL 则丢弃）。"""
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or item.get("link") or "").strip()
    if not url:
        return None
    title = str(item.get("title") or item.get("name") or "").strip()
    snippet = ""
    for key in snippet_keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            snippet = value.strip()
            break
    return SearchResult(title=title, url=url, snippet=snippet)


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------
# 5 个 provider 实现
# ---------------------------------------------------------------


class BraveProvider(BaseHttpProvider):
    """Brave Search API（``X-Subscription-Token`` 鉴权，GET + query 参数）。"""

    template = "brave"
    default_endpoint = DEFAULT_ENDPOINTS["brave"]

    def _request(self, query: str, limit: int):
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        params: dict[str, Any] = {
            "q": query,
            "count": min(int(limit), 20),
            "safesearch": self.safesearch or "moderate",
        }
        if self.region:
            # region 只在 brave 生效（ISO-3166-1 alpha-2）；其余 provider 记
            # region_unsupported:<p> 的说明由上层补。
            params["country"] = self.region.upper()
            params["search_lang"] = self.region.lower()
        return "GET", self._endpoint(), headers, params, None

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        web = payload.get("web")
        entries = _as_list((web or {}).get("results")) if isinstance(web, dict) else []
        out: list[SearchResult] = []
        for item in entries:
            mapped = _to_result(item, snippet_keys=("description", "snippet"))
            if mapped is not None:
                out.append(mapped)
        return out


class BochaProvider(BaseHttpProvider):
    """博查 Web Search API（``Authorization: Bearer``，POST JSON）。"""

    template = "bocha"
    default_endpoint = DEFAULT_ENDPOINTS["bocha"]

    def _request(self, query: str, limit: int):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"query": query, "count": int(limit), "summary": False}
        return "POST", self._endpoint(), headers, None, body

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        candidates: list = []
        data = payload.get("data")
        if isinstance(data, dict):
            web_pages = data.get("webPages")
            if isinstance(web_pages, dict):
                candidates = _as_list(web_pages.get("value"))
            if not candidates:
                candidates = _as_list(data.get("value"))
        if not candidates:
            web_pages = payload.get("webPages")
            if isinstance(web_pages, dict):
                candidates = _as_list(web_pages.get("value"))
        out: list[SearchResult] = []
        for item in candidates:
            mapped = _to_result(item, snippet_keys=("snippet", "summary", "description"))
            if mapped is not None:
                out.append(mapped)
        return out


class FirecrawlProvider(BaseHttpProvider):
    """Firecrawl Search API（``Authorization: Bearer``，POST JSON）。"""

    template = "firecrawl"
    default_endpoint = DEFAULT_ENDPOINTS["firecrawl"]

    def _request(self, query: str, limit: int):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"query": query, "limit": int(limit)}
        return "POST", self._endpoint(), headers, None, body

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        data = payload.get("data")
        if isinstance(data, dict):
            data = data.get("web") or data.get("results")
        out: list[SearchResult] = []
        for item in _as_list(data):
            mapped = _to_result(item, snippet_keys=("description", "snippet", "markdown"))
            if mapped is not None:
                out.append(mapped)
        return out


class TavilyProvider(BaseHttpProvider):
    """Tavily Search API（key 放请求体，POST JSON）。"""

    template = "tavily"
    default_endpoint = DEFAULT_ENDPOINTS["tavily"]

    def _request(self, query: str, limit: int):
        headers = {"Content-Type": "application/json"}
        body = {
            "api_key": self.api_key,
            "query": query,
            "max_results": int(limit),
            "search_depth": "basic",
            "include_answer": False,
        }
        return "POST", self._endpoint(), headers, None, body

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        out: list[SearchResult] = []
        for item in _as_list(payload.get("results")):
            mapped = _to_result(item, snippet_keys=("content", "snippet", "description"))
            if mapped is not None:
                out.append(mapped)
        return out


class ExaProvider(BaseHttpProvider):
    """Exa Search API（``x-api-key`` 头，POST JSON）。"""

    template = "exa"
    default_endpoint = DEFAULT_ENDPOINTS["exa"]

    def _request(self, query: str, limit: int):
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        body = {"query": query, "numResults": int(limit)}
        return "POST", self._endpoint(), headers, None, body

    def _parse(self, payload: dict[str, Any]) -> list[SearchResult]:
        out: list[SearchResult] = []
        for item in _as_list(payload.get("results")):
            mapped = _to_result(item, snippet_keys=("text", "snippet", "summary"))
            if mapped is not None:
                out.append(mapped)
        return out


#: 模板 id -> provider 类（``_conf_schema.json`` 的 templates 必须与它一一对应）。
PROVIDER_CLASSES: dict[str, type[BaseHttpProvider]] = {
    "brave": BraveProvider,
    "bocha": BochaProvider,
    "firecrawl": FirecrawlProvider,
    "tavily": TavilyProvider,
    "exa": ExaProvider,
}


# ---------------------------------------------------------------
# 配置 -> ProviderEntry 列表
# ---------------------------------------------------------------


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    number = as_int(value, default)
    return max(low, min(high, number))


def parse_provider_configs(raw: Any) -> list[ProviderEntry]:
    """把 ``search.providers`` 数组解析成 ``ProviderEntry`` 列表。

    - ``__template_key``（或 ``template``）决定实现类；未知模板 → ``provider=None``
      ⇒ 运行时记 ``unknown_template``（**不**发请求）；
    - ``api_key`` 空 → 仍然建 entry（保留 ``missing_key`` 可观测），不建实现类
      之外的副作用；
    - ``enabled=false`` → 保留 entry（记 ``disabled``），连实现类都不建；
    - ``base_url`` 非 http(s) 或命中内网字面量 → ``config_error="bad_config"``
      （防"把内网端点配成搜索 API"）；
    - 数组顺序即默认优先级（``weight`` 相同时靠 ``ProviderEntry.index``）。
    """
    entries: list[ProviderEntry] = []
    items = raw if isinstance(raw, list) else []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        template = str(item.get("__template_key") or item.get("template") or "").strip().lower()
        enabled = as_bool(item.get("enabled", True), True)
        api_key = str(item.get("api_key") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        name = str(item.get("name") or "").strip() or (
            f"{template}#{idx}" if template else f"provider#{idx}"
        )
        weight = _clamp_int(item.get("weight", 50), 50, 1, 100)
        timeout_s = as_float(item.get("timeout", 0), 0.0)
        timeout_s = max(0.0, min(120.0, float(timeout_s or 0.0)))
        limit = _clamp_int(item.get("max_results", 0), 0, 0, 50)
        entry = ProviderEntry(
            name=name,
            template=template,
            enabled=enabled,
            api_key=api_key,
            weight=weight,
            timeout_s=timeout_s,
            limit=limit,
            index=idx,
        )
        if not template or template not in PROVIDER_CLASSES:
            entry.provider = None
            entry.config_error = ""
            entries.append(entry)
            continue
        if base_url:
            ok, _reason = check_url_literal(base_url, block_internal_ip=True)
            if not ok:
                entry.provider = None
                entry.config_error = "bad_config"
                entries.append(entry)
                continue
        entries.append(entry)
    return entries


def build_provider_entries(
    raw: Any,
    *,
    region: str = "",
    safesearch: str = "moderate",
    proxy: str = "",
    transport: HttpTransport | None = None,
    url_guard=None,
) -> list[ProviderEntry]:
    """解析配置并实例化 5 个 provider 实现（密钥只留在实现对象内）。

    Args:
        raw: ``search.providers`` 的原值（list[dict]，可能含 ``__template_key``）。
        region: ``search.region``（仅 brave 消费；其余源记 ``region_unsupported``）。
        safesearch: ``search.safesearch``（仅 brave 消费）。
        proxy: ``search.proxy``（搜索与抓取共用；留空直连）。
        transport: HTTP 传输层（测试注入假实现；None 用 aiohttp/标准库）。
        url_guard: 可选的**结果 URL** 字面校验回调（返回 True 表示放行）；
            由 ``main.py`` 按 ``security.block_internal_ip`` 注入，命中即丢弃
            该条结果并记 ``ssrf_blocked``。

    Returns:
        ``list[ProviderEntry]``：``provider`` 为 None 的条目表示"模板未知/
        base_url 非法"（不发请求，只记错误码）。
    """
    entries = parse_provider_configs(raw)
    for entry in entries:
        if entry.provider is not None:
            continue
        if entry.config_error:
            continue
        if entry.template not in PROVIDER_CLASSES:
            continue
        cls = PROVIDER_CLASSES[entry.template]
        entry.provider = cls(
            name=entry.name,
            api_key=entry.api_key,
            base_url=str(_raw_base_url(raw, entry.index)),
            region=region,
            safesearch=safesearch,
            proxy=proxy,
            transport=transport,
        )
        entry.url_guard = url_guard
    # 未启用 / 缺 key 的条目也挂上 url_guard（行为一致，便于统一断言）。
    for entry in entries:
        if entry.url_guard is None:
            entry.url_guard = url_guard
    return entries


def _raw_base_url(raw: Any, index: int) -> str:
    """从原始配置里取第 ``index``（1 起）条的 ``base_url``。

    ``parse_provider_configs`` 不保留 ``base_url`` 原文（它是 provider 的构造
    入参而非池属性），这里按同一定位规则再取一次，避免把 base_url 塞进
    ``ProviderEntry`` 而让它进日志/返回体。
    """
    items = raw if isinstance(raw, list) else []
    if 1 <= index <= len(items):
        item = items[index - 1]
        if isinstance(item, dict):
            return str(item.get("base_url") or "").strip()
    return ""


def is_entry_usable(entry: ProviderEntry) -> bool:
    """池内"可用"判定：模板已知 + 启用 + 有 key + 无配置错误。

    只看**配置**，不看是否已实例化（``parse_provider_configs`` 的返回值也能用），
    因此 ``initialize()`` 的自动闸门与工具入口用的是同一判据。
    """
    return bool(
        entry.enabled
        and entry.api_key
        and not entry.config_error
        and entry.template in PROVIDER_CLASSES
    )


def describe_provider_sources(entries: list[ProviderEntry]) -> list[str]:
    """返回池内**可用**（有实现类 + 启用 + 有 key + 无配置错误）的 provider 名。"""
    return [
        entry.name
        for entry in entries
        if entry.provider is not None and is_entry_usable(entry)
    ]


#: 供测试/文档引用的类型别名（避免测试导入 Protocol）。
SearchProviderImpl = SearchProvider
