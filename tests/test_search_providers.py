"""provider 实现测试（brave/bocha/firecrawl/tavily/exa）：零网络，全部注入假传输层。

钉住四类行为：

1. **请求形态**：方法/端点/鉴权头/参数名/条数上限/query 截断；
2. **响应映射**：各家的 JSON 结构 -> ``SearchResult``（字段名差异都在这里）；
3. **错误分类**：401/403 -> auth、429 -> rate_limited、4xx/5xx、非 JSON、
   超时、连接异常；每条都保留 ``http_status``（进 attempts[]）；
4. **密钥边界**：``api_key`` 只出现在鉴权头/请求体，不进结果、不进异常文本。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.http import HttpResult
from core.search import ProviderError
from core.search_providers import (
    PROVIDER_CLASSES,
    PROVIDER_IDS,
    build_provider_entries,
    is_entry_usable,
    parse_provider_configs,
)


class FakeTransport:
    """记录调用并返回预置响应的传输层（唯一允许的"网络"替身）。"""

    def __init__(self, *, status: int = 200, payload=None, body: bytes | None = None,
                 headers: dict | None = None, error: BaseException | None = None):
        self.status = status
        self.payload = payload
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.error = error
        self.calls: list[dict] = []

    async def fetch(self, method, url, *, headers=None, params=None, json_body=None,
                    timeout=10.0, proxy="", max_bytes=0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
                "json_body": json_body,
                "timeout": timeout,
                "proxy": proxy,
            }
        )
        if self.error is not None:
            raise self.error
        if self.body is not None:
            body = self.body
        else:
            body = json.dumps(self.payload or {}, ensure_ascii=False).encode("utf-8")
        return HttpResult(status=self.status, headers=dict(self.headers), body=body)


def run(coro):
    return asyncio.run(coro)


def make_provider(template: str, transport, **kwargs):
    cls = PROVIDER_CLASSES[template]
    return cls(
        name=f"{template}#1",
        api_key=kwargs.pop("api_key", "sk-test-key-0123456789"),
        transport=transport,
        **kwargs,
    )


# ---------------------------------------------------------------
# 请求形态
# ---------------------------------------------------------------


def test_brave_request_shape_and_mapping():
    transport = FakeTransport(
        payload={
            "web": {
                "results": [
                    {"title": "T1", "url": "https://e.com/1", "description": "D1"},
                    {"title": "T2", "url": "https://e.com/2", "description": "D2"},
                ]
            }
        }
    )
    provider = make_provider(
        "brave", transport, region="cn", safesearch="strict"
    )
    results = run(provider.search("北京 天气", limit=5, timeout=8))

    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert call["headers"]["X-Subscription-Token"] == "sk-test-key-0123456789"
    assert call["params"]["q"] == "北京 天气"
    assert call["params"]["count"] == 5
    assert call["params"]["country"] == "CN"
    assert call["params"]["search_lang"] == "cn"
    assert call["params"]["safesearch"] == "strict"
    assert call["timeout"] == 8
    assert [r.url for r in results] == ["https://e.com/1", "https://e.com/2"]
    assert results[0].snippet == "D1"


def test_brave_query_truncated_to_500():
    transport = FakeTransport(payload={"web": {"results": []}})
    provider = make_provider("brave", transport)
    run(provider.search("x" * 900, limit=3, timeout=5))
    assert len(transport.calls[0]["params"]["q"]) == 500


def test_brave_count_capped_at_20():
    transport = FakeTransport(payload={"web": {"results": []}})
    provider = make_provider("brave", transport)
    run(provider.search("q", limit=50, timeout=5))
    assert transport.calls[0]["params"]["count"] == 20


def test_brave_without_region_omits_country():
    transport = FakeTransport(payload={"web": {"results": []}})
    provider = make_provider("brave", transport)
    run(provider.search("q", limit=3, timeout=5))
    assert "country" not in transport.calls[0]["params"]


def test_bocha_request_and_mapping():
    transport = FakeTransport(
        payload={"data": {"webPages": {"value": [
            {"name": "博查标题", "url": "https://b.com/1", "snippet": "摘要"}
        ]}}}
    )
    provider = make_provider("bocha", transport)
    results = run(provider.search("q", limit=4, timeout=5))
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.bochaai.com/v1/web-search"
    assert call["headers"]["Authorization"] == "Bearer sk-test-key-0123456789"
    assert call["json_body"]["query"] == "q"
    assert call["json_body"]["count"] == 4
    assert results[0].title == "博查标题"
    assert results[0].snippet == "摘要"


def test_firecrawl_request_and_mapping():
    transport = FakeTransport(
        payload={"data": [{"title": "FC", "url": "https://f.com/1", "description": "desc"}]}
    )
    provider = make_provider("firecrawl", transport)
    results = run(provider.search("q", limit=2, timeout=5))
    call = transport.calls[0]
    assert call["url"] == "https://api.firecrawl.dev/v1/search"
    assert call["headers"]["Authorization"].startswith("Bearer ")
    assert call["json_body"] == {"query": "q", "limit": 2}
    assert results[0].url == "https://f.com/1"
    assert results[0].snippet == "desc"


def test_tavily_key_in_body_not_headers():
    transport = FakeTransport(
        payload={"results": [{"title": "TV", "url": "https://t.com/1", "content": "c"}]}
    )
    provider = make_provider("tavily", transport)
    results = run(provider.search("q", limit=3, timeout=5))
    call = transport.calls[0]
    assert call["url"] == "https://api.tavily.com/search"
    assert call["json_body"]["api_key"] == "sk-test-key-0123456789"
    assert call["json_body"]["max_results"] == 3
    assert call["json_body"]["include_answer"] is False
    assert "Authorization" not in call["headers"]
    assert results[0].title == "TV"


def test_exa_request_and_mapping():
    transport = FakeTransport(
        payload={"results": [{"title": "EXA", "url": "https://x.com/1", "text": "body"}]}
    )
    provider = make_provider("exa", transport)
    results = run(provider.search("q", limit=6, timeout=5))
    call = transport.calls[0]
    assert call["url"] == "https://api.exa.ai/search"
    assert call["headers"]["x-api-key"] == "sk-test-key-0123456789"
    assert call["json_body"]["numResults"] == 6
    assert results[0].snippet == "body"


def test_base_url_override_used():
    transport = FakeTransport(payload={"results": []})
    provider = make_provider("tavily", transport, base_url="https://mirror.example.com/search")
    run(provider.search("q", limit=1, timeout=5))
    assert transport.calls[0]["url"] == "https://mirror.example.com/search"


def test_proxy_passed_to_transport():
    transport = FakeTransport(payload={"results": []})
    provider = make_provider("tavily", transport, proxy="http://127.0.0.1:7890")
    run(provider.search("q", limit=1, timeout=5))
    assert transport.calls[0]["proxy"] == "http://127.0.0.1:7890"


def test_results_dropped_without_url_and_trimmed_to_limit():
    transport = FakeTransport(
        payload={"results": [
            {"title": "no url"},
            {"title": "a", "url": "https://e.com/a"},
            {"title": "b", "url": "https://e.com/b"},
        ]}
    )
    provider = make_provider("tavily", transport)
    results = run(provider.search("q", limit=1, timeout=5))
    assert [r.url for r in results] == ["https://e.com/a"]


# ---------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------


def test_auth_401_maps_to_auth():
    transport = FakeTransport(status=401, payload={"error": "bad key"})
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "auth"
    assert exc.value.http_status == 401


def test_403_maps_to_auth():
    transport = FakeTransport(status=403)
    provider = make_provider("exa", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "auth"


def test_429_maps_to_rate_limited():
    transport = FakeTransport(status=429)
    provider = make_provider("bocha", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "rate_limited"
    assert exc.value.http_status == 429


def test_4xx_maps_to_http_4xx():
    transport = FakeTransport(status=422)
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "http_4xx"
    assert exc.value.http_status == 422


def test_5xx_maps_to_http_5xx():
    transport = FakeTransport(status=503)
    provider = make_provider("firecrawl", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "http_5xx"


def test_bad_json_maps_to_bad_response():
    transport = FakeTransport(body=b"<html>error page</html>")
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "bad_response"


def test_non_object_json_maps_to_bad_response():
    transport = FakeTransport(body=b"[1,2,3]")
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "bad_response"


def test_timeout_exception_maps_to_timeout():
    transport = FakeTransport(error=asyncio.TimeoutError())
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "timeout"


def test_connection_error_maps_to_network():
    class ClientConnectorError(OSError):
        pass

    transport = FakeTransport(error=ClientConnectorError("boom"))
    provider = make_provider("brave", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    assert exc.value.code == "network"


def test_empty_query_rejected_before_network():
    transport = FakeTransport(payload={"results": []})
    provider = make_provider("tavily", transport)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("   ", limit=1, timeout=5))
    assert exc.value.code == "empty_query"
    assert transport.calls == []


# ---------------------------------------------------------------
# 密钥边界
# ---------------------------------------------------------------


def test_api_key_never_in_results():
    key = "sk-UNIQUE-KEY-abcdef123456"
    transport = FakeTransport(
        payload={"results": [{"title": "t", "url": "https://e.com/1", "content": "c"}]}
    )
    provider = make_provider("tavily", transport, api_key=key)
    results = run(provider.search("q", limit=1, timeout=5))
    blob = json.dumps([r.__dict__ for r in results], ensure_ascii=False)
    assert key not in blob


def test_api_key_never_in_error_message():
    key = "sk-UNIQUE-KEY-abcdef123456"
    transport = FakeTransport(error=OSError(f"connect failed for {key}"))
    provider = make_provider("brave", transport, api_key=key)
    with pytest.raises(ProviderError) as exc:
        run(provider.search("q", limit=1, timeout=5))
    # ProviderError 的文本只放类型名/短语，绝不放原始异常文本
    assert key not in str(exc.value)


# ---------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------


def test_parse_provider_configs_defaults_and_clamps():
    entries = parse_provider_configs(
        [
            {"__template_key": "brave"},
            {"__template_key": "bocha", "weight": 999, "timeout": -5, "max_results": 999},
            {"__template_key": "exa", "enabled": False, "name": "备用"},
            "not-a-dict",
            {"template": "tavily", "api_key": "k"},
        ]
    )
    assert [e.name for e in entries] == ["brave#1", "bocha#2", "备用", "tavily#5"]
    assert entries[0].weight == 50
    assert entries[0].api_key == ""
    assert entries[1].weight == 100  # clamp 1..100
    assert entries[1].timeout_s == 0.0  # clamp 0..120
    assert entries[1].limit == 50  # clamp 0..50
    assert entries[2].enabled is False
    assert entries[3].template == "tavily"
    # 数组顺序 = index（复选框/波次顺序的口径）
    assert [e.index for e in entries] == [1, 2, 3, 5]


def test_parse_unknown_template_has_no_provider():
    entries = parse_provider_configs([{"__template_key": "serpapi", "api_key": "k"}])
    assert entries[0].provider is None
    assert entries[0].config_error == ""
    assert is_entry_usable(entries[0]) is False


def test_parse_internal_base_url_is_bad_config():
    entries = parse_provider_configs(
        [{"__template_key": "brave", "api_key": "k", "base_url": "http://127.0.0.1/search"}]
    )
    assert entries[0].provider is None
    assert entries[0].config_error == "bad_config"
    assert is_entry_usable(entries[0]) is False


def test_parse_non_http_base_url_is_bad_config():
    entries = parse_provider_configs(
        [{"__template_key": "brave", "api_key": "k", "base_url": "ftp://x/y"}]
    )
    assert entries[0].config_error == "bad_config"


def test_build_provider_entries_instantiates_and_attaches_guard():
    guard = lambda url: True  # noqa: E731 — 测试用
    entries = build_provider_entries(
        [{"__template_key": "brave", "api_key": "k", "base_url": "https://mirror.example.com/x"}],
        region="US",
        safesearch="off",
        proxy="http://p:1",
        transport=FakeTransport(payload={"web": {"results": []}}),
        url_guard=guard,
    )
    assert isinstance(entries[0].provider, PROVIDER_CLASSES["brave"])
    assert entries[0].provider.base_url == "https://mirror.example.com/x"
    assert entries[0].provider.region == "US"
    assert entries[0].provider.safesearch == "off"
    assert entries[0].provider.proxy == "http://p:1"
    assert entries[0].url_guard is guard
    assert is_entry_usable(entries[0]) is True


def test_build_provider_entries_skips_unknown_and_bad_config():
    entries = build_provider_entries(
        [
            {"__template_key": "serpapi", "api_key": "k"},
            {"__template_key": "brave", "api_key": "k", "base_url": "http://10.0.0.1/x"},
        ],
        transport=FakeTransport(),
    )
    assert entries[0].provider is None and entries[0].config_error == ""
    assert entries[1].provider is None and entries[1].config_error == "bad_config"


def test_usable_requires_key_and_enabled():
    entries = parse_provider_configs(
        [
            {"__template_key": "brave", "api_key": "k"},
            {"__template_key": "brave"},
            {"__template_key": "brave", "api_key": "k", "enabled": False},
        ]
    )
    assert [is_entry_usable(e) for e in entries] == [True, False, False]


def test_provider_ids_and_classes_are_in_sync():
    assert set(PROVIDER_IDS) == set(PROVIDER_CLASSES)
    for template_id, cls in PROVIDER_CLASSES.items():
        assert cls.template == template_id
        assert cls.default_endpoint.startswith("https://")
