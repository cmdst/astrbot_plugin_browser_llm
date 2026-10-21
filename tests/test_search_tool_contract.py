"""browser_web_search 工具契约测试（零网络：provider/抓取器全部注入假实现）。

覆盖 spec §6 与 §13 的这几条：

- AC-07 工具名不撞核心内置族 + description 首句的"并存不接管 / 只选其一"口径；
- AC-13 handler 入口兜底（配置关 ⇒ ``status=disabled`` 且**零网络**）；
- AC-20/AC-24/AC-26 通过工具跑一遍并行、去重、全失败（不抛异常、返回 JSON）；
- AC-21 阶段预算传给服务层；AC-22 抓取超时（``fetch_timeout``）不丢结果；
- AC-30 返回体/日志不含 api_key；AC-31 结果 URL 字面 SSRF；AC-32 禁词三处；
- AC-33 会话黑名单零请求。
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from docstring_parser import parse as dp_parse

import main as main_module
from core.search import (
    MergedResult,
    ProviderEntry,
    ProviderError,
    SearchAttempt,
    SearchResult,
    SearchService,
)
from core.safety import SafetyFilter
from main import BrowserLLMPlugin, _SEARCH_TOOL_NAME

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "main.py").read_text(encoding="utf-8")

#: 核心内置搜索族 16 个工具名（``astrbot/core/tools/web_search_tools.py:17-34``）
BUILTIN_SEARCH_NAMES = {
    "web_search_baidu",
    "web_search_tavily",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
    "web_search_exa",
    "web_search_anysearch",
    "web_search_serpapi",
    "web_search_deepseek",
    "tavily_extract_web_page",
    "firecrawl_extract_web_page",
    "exa_get_contents",
    "deepseek_fetch_web_page",
    "image_search_serpapi",
    "finance_search_serpapi",
    "scholar_search_serpapi",
}


class FakeEvent:
    def __init__(self, umo: str = "test:private:1", group_id: str = ""):
        self.unified_msg_origin = umo
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "1"


class FakeProvider:
    def __init__(self, name, *, delay=0.0, results=None, error=None):
        self.name = name
        self.delay = delay
        self._results = list(results or [])
        self._error = error
        self.calls = 0

    async def search(self, query, *, limit, timeout):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self._error, BaseException):
            raise self._error
        return list(self._results)


class FakeFetcher:
    """假抓取器：按 url 决定返回/超时/异常。"""

    def __init__(self, behaviours: dict):
        self.behaviours = behaviours
        self.calls: list[str] = []

    async def fetch(self, url, *, timeout=None):
        self.calls.append(url)
        behaviour = self.behaviours.get(url, {"ok": True, "text": "正文", "status": 200})
        if behaviour.get("timeout"):
            raise asyncio.TimeoutError()
        if behaviour.get("error"):
            raise OSError("boom")

        class _Outcome:
            ok = behaviour.get("ok", True)
            code = behaviour.get("code", "ok")
            status = behaviour.get("status", 200)
            url_ = url
            title = ""
            text = behaviour.get("text", "")
            truncated = False
            nbytes = len(behaviour.get("text", ""))
            elapsed_ms = 1

            def as_dict(self):
                return {
                    "ok": self.ok,
                    "code": self.code,
                    "status": self.status,
                    "chars": len(self.text),
                    "truncated": self.truncated,
                    "text": self.text,
                    "elapsed_ms": self.elapsed_ms,
                }

        outcome = _Outcome()
        outcome.url = url
        return outcome


def search_config(extra: dict | None = None, **overrides) -> dict:
    """构造一份"已启用 + 一个 provider"的分组配置。"""
    search = {
        "enabled": True,
        "providers": [{"__template_key": "brave", "api_key": "sk-test-key-1"}],
        "max_parallel": 3,
        "max_results": 5,
        "max_rounds": 2,
        "provider_timeout": 15,
        "total_timeout": 60,
        "cooldown_seconds": 0,
        "cache_ttl": 0,
        "fetch_pages": False,
        "fetch_top_n": 0,
        "fetch_timeout": 3,
        "fetch_max_chars": 2000,
        "region": "",
        "safesearch": "moderate",
        "proxy": "",
    }
    search.update(overrides)
    cfg = {"search": search}
    cfg.update(extra or {})
    return cfg


def make_plugin(extra: dict | None = None, **overrides) -> BrowserLLMPlugin:
    """构造插件实例。

    注意：工具入口会 ``_refresh_config()`` 重读配置，所以**所有**需要影响
    行为的设置都必须写进配置（不能改实例属性后再调用，否则会被刷新覆盖）。
    """
    return BrowserLLMPlugin(None, search_config(extra, **overrides))


def entry(name, provider, *, template="brave", api_key="sk-test-key-1", weight=50):
    return ProviderEntry(
        name=name,
        template=template,
        provider=provider,
        enabled=True,
        api_key=api_key,
        weight=weight,
    )


def run(coro):
    return asyncio.run(coro)


class RecordingLogger:
    """记录日志调用的替身（conftest 的 astrbot 桩 logger 是 no-op，无法断言）。"""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, args):
        text = " ".join(str(a) for a in args)
        self.records.append((level, text))

    def info(self, *a, **k):
        self._add("info", a)

    def warning(self, *a, **k):
        self._add("warning", a)

    def error(self, *a, **k):
        self._add("error", a)

    def debug(self, *a, **k):
        self._add("debug", a)

    @property
    def text(self):
        return "\n".join(t for _lvl, t in self.records)


@pytest.fixture()
def recorder(monkeypatch):
    log = RecordingLogger()
    monkeypatch.setattr(main_module, "logger", log)
    return log


# ---------------------------------------------------------------
# AC-07：命名与口径
# ---------------------------------------------------------------


def test_tool_name_not_in_builtin_family():
    assert _SEARCH_TOOL_NAME == "browser_web_search"
    assert _SEARCH_TOOL_NAME not in BUILTIN_SEARCH_NAMES
    assert not _SEARCH_TOOL_NAME.startswith("web_search_")
    # 子代理工具 browse_search 也不同名（日志/排障能分清）
    assert _SEARCH_TOOL_NAME != "browse_search"


def test_tool_name_defined_once():
    occurrences = _SRC.count('"browser_web_search"')
    assert occurrences == 1, f"工具名字面量应只在常量处出现一次，实际 {occurrences}"
    assert "_SEARCH_TOOL_NAME" in _SRC


def test_description_first_sentence_mentions_coexistence():
    doc = dp_parse(BrowserLLMPlugin.browser_web_search.__doc__ or "")
    short = (doc.short_description or "").strip()
    assert short, "工具 docstring 缺短描述（import 期会被核心解析为工具描述）"
    first_sentence = short.split("。")[0]
    assert "与内置" in first_sentence
    assert "只选其一" in first_sentence
    assert "web_search_<provider>" in short


def test_tool_declares_required_parameters():
    doc = dp_parse(BrowserLLMPlugin.browser_web_search.__doc__ or "")
    params = {p.arg_name: p.type_name for p in doc.params}
    # 契约要求至少覆盖 query / max_results / rounds / timeout / providers
    for name in ("query", "max_results", "rounds", "timeout", "providers",
                 "fetch_pages", "fetch_top_n"):
        assert name in params, f"工具缺少参数 {name}"
    for name in ("max_results", "rounds", "timeout", "fetch_top_n"):
        assert params[name] == "number"
    assert params["fetch_pages"] == "boolean"
    assert params["providers"] == "string"


def test_tool_is_llm_tool_with_constant_name():
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "browser_web_search":
            decs = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "llm_tool"
            ]
            assert decs, "browser_web_search 缺 @filter.llm_tool 装饰器"
            kw = {k.arg: k.value for k in decs[0].keywords}
            assert isinstance(kw.get("name"), ast.Name)
            assert kw["name"].id == "_SEARCH_TOOL_NAME"
            return
    raise AssertionError("main.py 里找不到 browser_web_search")


# ---------------------------------------------------------------
# AC-13 / AC-33：入口兜底与零网络
# ---------------------------------------------------------------


def test_disabled_returns_disabled_without_building_pool(monkeypatch):
    plugin = make_plugin(enabled=False)

    def boom():
        raise AssertionError("配置关闭时不应构造 provider 池")

    monkeypatch.setattr(plugin, "_build_search_entries", boom)
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="q")))

    assert payload["status"] == "disabled"
    assert payload["ok"] is False
    assert payload["code"] == "disabled"
    assert payload["summary"].startswith("【拒绝】")
    assert "results" in payload and payload["results"] == []


def test_disabled_when_pool_empty_is_inactive_gate(monkeypatch):
    """开关开着但池内无可用 provider ⇒ 工具本该不可见；handler 兜底也拒绝。"""
    plugin = make_plugin(providers=[])
    assert plugin._search_available() is False

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="q")))
    assert payload["status"] == "failed"
    assert payload["code"] == "no_provider"
    assert payload["summary"].startswith("【错误】")
    assert payload["hint"]


def test_session_denied_makes_zero_requests(monkeypatch):
    plugin = make_plugin(extra={"security": {"session_blacklist": ["test"]}})
    called = []
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: called.append(1) or [])

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="q")))

    assert payload["code"] == "session_denied"
    assert payload["summary"].startswith("【拒绝】")
    assert called == []


def test_empty_query_rejected():
    plugin = make_plugin()
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="  ")))
    assert payload["code"] == "empty_query"
    assert payload["status"] == "failed"


# ---------------------------------------------------------------
# AC-32：禁词（query 级拒绝 / 结果级丢弃）
# ---------------------------------------------------------------


def test_banned_query_rejected_without_network(monkeypatch):
    plugin = make_plugin(
        extra={"security": {"banned_words": ["赌博"], "enable_banned_filter": True}}
    )
    plugin.safety = SafetyFilter(["赌博"], True)
    called = []
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: called.append(1) or [])

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="赌博 网站")))

    assert payload["code"] == "banned"
    assert payload["summary"].startswith("【拒绝】")
    assert called == []


def test_banned_result_dropped_but_others_kept(monkeypatch):
    plugin = make_plugin(
        extra={"security": {"banned_words": ["赌博"], "enable_banned_filter": True}}
    )
    plugin.safety = SafetyFilter(["赌博"], True)
    provider = FakeProvider(
        "a",
        results=[
            SearchResult(title="赌博网站", url="https://e.com/bad", snippet="x"),
            SearchResult(title="正常", url="https://e.com/ok", snippet="y"),
        ],
    )
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: [entry("a#1", provider)])

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert [r["url"] for r in payload["results"]] == ["https://e.com/ok"]
    assert "banned_results:1" in payload["notes"]
    assert payload["status"] == "partial"


def test_banned_filter_off_keeps_results(monkeypatch):
    plugin = make_plugin(
        extra={"security": {"banned_words": ["赌博"], "enable_banned_filter": False}}
    )
    plugin.safety = SafetyFilter(["赌博"], True)
    provider = FakeProvider(
        "a", results=[SearchResult(title="赌博网站", url="https://e.com/bad", snippet="x")]
    )
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: [entry("a#1", provider)])

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))
    assert [r["url"] for r in payload["results"]] == ["https://e.com/bad"]


# ---------------------------------------------------------------
# 并行 / 去重 / 全失败（经工具跑一遍）
# ---------------------------------------------------------------


def test_tool_parallel_and_dedupe(monkeypatch):
    slow_a = FakeProvider("a", delay=0.05, results=[SearchResult(title="A", url="https://E.com/x?utm_source=1")])
    slow_b = FakeProvider("b", delay=0.05, results=[SearchResult(title="B", url="https://e.com/x")])
    plugin = make_plugin(max_parallel=2, max_results=5, cache_ttl=0)
    monkeypatch.setattr(
        plugin,
        "_build_search_entries",
        lambda: [entry("a#1", slow_a, weight=80), entry("b#1", slow_b, weight=50)],
    )

    began = time.monotonic()
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))
    elapsed = time.monotonic() - began

    assert elapsed < 0.12, f"未并行：{elapsed:.3f}s"
    assert payload["status"] == "ok" and payload["ok"] is True
    assert len(payload["results"]) == 1
    assert [s["provider"] for s in payload["results"][0]["sources"]] == ["a#1", "b#1"]
    assert {a["provider"] for a in payload["attempts"]} == {"a#1", "b#1"}
    assert payload["providers_effective"] == ["a#1", "b#1"]


def test_tool_all_fail_returns_json_not_exception(monkeypatch):
    plugin = make_plugin()
    monkeypatch.setattr(
        plugin,
        "_build_search_entries",
        lambda: [
            entry("a#1", FakeProvider("a", error=ProviderError("timeout"))),
            entry("b#1", FakeProvider("b", error=ProviderError("rate_limited"))),
        ],
    )

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert payload["status"] == "failed"
    assert payload["ok"] is False
    assert payload["code"] == "rate_limited"
    assert len(payload["error_codes"]) == 2
    assert payload["summary"].startswith("【错误】")


def test_tool_internal_error_is_json(monkeypatch):
    plugin = make_plugin()

    def boom():
        raise RuntimeError("internal boom")

    monkeypatch.setattr(plugin, "_build_search_entries", boom)
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert payload["status"] == "failed"
    assert payload["code"] == "internal_error"
    assert "internal boom" not in json.dumps(payload, ensure_ascii=False)


def test_tool_total_timeout_branch(monkeypatch):
    plugin = make_plugin()

    class TimeoutService:
        async def search_waves(self, *a, **kw):
            raise asyncio.TimeoutError()

    monkeypatch.setattr(plugin, "_get_search_service", lambda entries: TimeoutService())
    monkeypatch.setattr(
        plugin, "_build_search_entries", lambda: [entry("a#1", FakeProvider("a"))]
    )
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert payload["code"] == "total_timeout"
    assert "total_timeout" in payload["notes"]


def test_outer_timeout_renders_partial_with_kept_results(monkeypatch):
    """AC-21（t6 M-1）：外层 ``wait_for`` 超时时，**已得结果必须渲染 partial**。

    ``search_waves`` 通过 ``progress`` 容器把每波结果增量交出来；外层超时会
    取消协程并丢掉返回值，这里断言工具仍用最后一次快照渲染
    ``status=partial`` + ``notes`` 含 ``total_timeout``，而不是 failed/0 条。
    """
    plugin = make_plugin()

    class ProgressThenTimeout:
        async def search_waves(self, query, **kwargs):
            progress = kwargs["progress"]
            progress["results"] = [
                MergedResult(title="t1", url="https://e.com/fast", snippet="s", rank=1)
            ]
            progress["attempts"] = [SearchAttempt("fast#1", "brave", 1, "ok", "ok")]
            progress["rounds_used"] = 1
            progress["providers_effective"] = ["fast#1"]
            raise asyncio.TimeoutError()

    monkeypatch.setattr(
        plugin, "_get_search_service", lambda entries: ProgressThenTimeout()
    )
    monkeypatch.setattr(
        plugin, "_build_search_entries", lambda: [entry("fast#1", FakeProvider("fast"))]
    )
    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert payload["status"] == "partial"
    assert payload["code"] == "ok"
    assert payload["ok"] is True
    assert [r["url"] for r in payload["results"]] == ["https://e.com/fast"]
    assert "total_timeout" in payload["notes"]
    assert payload["attempts"][0]["provider"] == "fast#1"


def test_tool_wall_clock_is_capped_by_budget_not_provider_timeout(monkeypatch):
    """AC-21（回归，复刻 reviewer 实测）：``provider_timeout=15`` + 3 个
    ``sleep(10)`` 慢源 + 工具参数 ``timeout=5`` ⇒ 墙钟由**总预算**决定
    （~4s，搜索阶段留 1s 余量），不是 6s 外层超时后"全丢"。

    旧的实现让每个源吃满 15s，外层 ``budget+1=6s`` 先触发并丢掉整批结果；
    现在单源等待被压到剩余预算，第 1 波的快源结果得以保留。
    """
    plugin = make_plugin(provider_timeout=15)
    fast = FakeProvider("fast", results=[SearchResult("t", "https://e.com/fast", "s")])
    slows = [FakeProvider(f"s{i}", delay=10.0) for i in range(3)]
    entries = [entry("fast#1", fast)] + [entry(f"s{i}#1", p) for i, p in enumerate(slows)]

    def fake_service(entries_arg):
        return SearchService(
            entries=entries_arg, timeout_s=15.0, max_results=10, cache_ttl_s=0.0
        )

    monkeypatch.setattr(plugin, "_get_search_service", fake_service)
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: list(entries))

    began = time.monotonic()
    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试", timeout=5))
    )
    elapsed = time.monotonic() - began

    assert elapsed < 6.0, f"墙钟应由总预算（约 4s）决定，实测 {elapsed:.2f}s"
    assert payload["status"] == "partial"
    assert [r["url"] for r in payload["results"]] == ["https://e.com/fast"]
    assert "total_timeout" in payload["notes"]


# ---------------------------------------------------------------
# 参数透传（max_rounds / total_timeout / max_parallel / providers）
# ---------------------------------------------------------------


def test_tool_passes_rounds_budget_and_filter(monkeypatch):
    plugin = make_plugin(max_rounds=2, max_parallel=3, total_timeout=60)
    seen = {}

    class SpyService:
        async def search_waves(self, query, **kwargs):
            seen.update(kwargs)
            from core.search import WaveResult

            return WaveResult(status="ok", code="ok", rounds_used=kwargs["max_rounds"])

    monkeypatch.setattr(plugin, "_get_search_service", lambda entries: SpyService())
    monkeypatch.setattr(
        plugin, "_build_search_entries", lambda: [entry("a#1", FakeProvider("a"))]
    )

    run(
        plugin.browser_web_search(
            FakeEvent(),
            query="测试",
            max_results=4,
            rounds=3,
            timeout=30,
            providers="a#1, ghost#1",
        )
    )

    assert seen["max_rounds"] == 3
    assert seen["max_results"] == 4
    assert seen["max_parallel"] == 3
    assert seen["time_budget_s"] == pytest.approx(29.0)  # 不抓正文 ⇒ 预算 - 1s
    assert seen["provider_filter"] == ["a#1", "ghost#1"]


def test_tool_reserves_forty_percent_budget_for_fetch(monkeypatch):
    plugin = make_plugin(fetch_pages=True, fetch_top_n=2, timeout=30)
    seen = {}

    class SpyService:
        async def search_waves(self, query, **kwargs):
            seen.update(kwargs)
            from core.search import WaveResult

            return WaveResult(status="failed", code="empty")

    monkeypatch.setattr(plugin, "_get_search_service", lambda entries: SpyService())
    monkeypatch.setattr(
        plugin, "_build_search_entries", lambda: [entry("a#1", FakeProvider("a"))]
    )

    run(plugin.browser_web_search(FakeEvent(), query="测试", fetch_pages=True, timeout=50))

    assert seen["time_budget_s"] == pytest.approx(30.0)  # 50 * 0.6


# ---------------------------------------------------------------
# AC-22：抓取正文（超时/失败/禁词都不影响主结果）
# ---------------------------------------------------------------


def _fetch_plugin(monkeypatch, behaviours, *, top_n=2, extra=None):
    plugin = make_plugin(extra=extra, fetch_pages=True, fetch_top_n=top_n, fetch_timeout=3)
    provider = FakeProvider(
        "a",
        results=[
            SearchResult(title="A", url="https://e.com/a"),
            SearchResult(title="B", url="https://e.com/b"),
            SearchResult(title="C", url="https://e.com/c"),
        ],
    )
    fetcher = FakeFetcher(behaviours)
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: [entry("a#1", provider)])
    monkeypatch.setattr(plugin, "_make_search_fetcher", lambda: fetcher)
    return plugin, fetcher


def test_fetch_pages_success(monkeypatch):
    plugin, fetcher = _fetch_plugin(
        monkeypatch, {"https://e.com/a": {"ok": True, "text": "正文 A"}}
    )
    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试", fetch_pages=True))
    )
    assert fetcher.calls == ["https://e.com/a", "https://e.com/b"]  # 只抓前 top_n 条
    fetched = payload["results"][0]["fetched"]
    assert fetched["ok"] is True and fetched["text"] == "正文 A"
    assert payload["results"][1]["fetched"]["ok"] is True
    assert "fetched" not in payload["results"][2]
    assert payload["status"] == "ok"  # 两条都抓成功 ⇒ 不降级


def test_fetch_timeout_marks_only_that_item(monkeypatch):
    plugin, _fetcher = _fetch_plugin(
        monkeypatch,
        {
            "https://e.com/a": {"timeout": True},
            "https://e.com/b": {"ok": True, "text": "正文 B"},
        },
    )
    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试", fetch_pages=True))
    )
    assert len(payload["results"]) == 3  # 主结果不丢
    assert payload["results"][0]["fetched"]["code"] == "fetch_timeout"
    assert payload["results"][0]["fetched"]["ok"] is False
    assert payload["results"][1]["fetched"]["ok"] is True
    assert "fetched" not in payload["results"][2]  # 第 3 条不在 top_n 内


def test_fetch_blocked_by_banned_words(monkeypatch):
    plugin, _fetcher = _fetch_plugin(
        monkeypatch,
        {"https://e.com/a": {"ok": True, "text": "这里有赌博内容"}},
        extra={"security": {"banned_words": ["赌博"], "enable_banned_filter": True}},
    )
    plugin.safety = SafetyFilter(["赌博"], True)

    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试", fetch_pages=True))
    )
    fetched = payload["results"][0]["fetched"]
    assert fetched["ok"] is False
    assert fetched["code"] == "fetch_blocked"
    assert fetched["text"] == ""


def test_fetch_pages_param_is_anded_with_config(monkeypatch):
    """工具参数 fetch_pages=true 但配置 false ⇒ 不抓（防提示词注入式越权）。"""
    plugin = make_plugin(fetch_pages=False, fetch_top_n=2)
    plugin.safety = SafetyFilter([], True)
    provider = FakeProvider("a", results=[SearchResult(title="A", url="https://e.com/a")])
    fetcher = FakeFetcher({})
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: [entry("a#1", provider)])
    monkeypatch.setattr(plugin, "_make_search_fetcher", lambda: fetcher)

    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试", fetch_pages=True))
    )
    assert fetcher.calls == []
    assert "fetched" not in payload["results"][0]


# ---------------------------------------------------------------
# AC-30：密钥不泄漏
# ---------------------------------------------------------------


def test_output_and_logs_never_contain_api_key(monkeypatch, recorder):
    key = "sk-UNIQUE-LEAK-CHECK-0123456789"
    plugin = make_plugin(providers=[{"__template_key": "brave", "api_key": key}])
    failing = FakeProvider("a", error=ProviderError("auth", f"bad token {key}"))
    ok = FakeProvider("b", results=[SearchResult(title="t", url="https://e.com/a", snippet="s")])
    monkeypatch.setattr(
        plugin,
        "_build_search_entries",
        lambda: [entry("a#1", failing, api_key=key), entry("b#1", ok, api_key=key)],
    )

    text = run(plugin.browser_web_search(FakeEvent(), query="测试"))

    assert key not in text
    assert key not in recorder.text
    payload = json.loads(text)
    assert payload["status"] == "partial"
    # 结果里也不得出现 key 片段
    assert "UNIQUE" not in json.dumps(payload, ensure_ascii=False)


def test_exception_path_redacts_key(monkeypatch, recorder):
    key = "sk-LEAK-VIA-EXC-99887766"
    plugin = make_plugin(providers=[{"__template_key": "brave", "api_key": key}])

    def boom():
        raise RuntimeError(f"upstream said {key}")

    monkeypatch.setattr(plugin, "_build_search_entries", boom)
    text = run(plugin.browser_web_search(FakeEvent(), query="测试"))

    assert key not in text
    assert key not in recorder.text
    assert "[redacted]" in recorder.text  # 异常文本进日志前已脱敏


# ---------------------------------------------------------------
# AC-31：结果 URL 字面 SSRF（工具级：guard 由配置注入）
# ---------------------------------------------------------------


def test_internal_result_urls_dropped(monkeypatch):
    plugin = make_plugin()
    assert plugin.block_internal_ip is True
    provider = FakeProvider(
        "a",
        results=[
            SearchResult(title="loop", url="http://127.0.0.1/a"),
            SearchResult(title="priv", url="http://10.0.0.1/b"),
            SearchResult(title="local", url="http://localhost/c"),
            SearchResult(title="file", url="file:///etc/passwd"),
            SearchResult(title="ok", url="https://example.com/d"),
        ],
    )
    monkeypatch.setattr(plugin, "_build_search_entries", lambda: [entry("a#1", provider)])

    payload = json.loads(run(plugin.browser_web_search(FakeEvent(), query="测试")))

    assert [r["url"] for r in payload["results"]] == ["https://example.com/d"]


def test_url_guard_disabled_when_block_internal_ip_false():
    plugin = make_plugin()
    plugin.block_internal_ip = False
    guard = plugin._search_url_guard()
    assert guard("http://127.0.0.1/a") is True


def test_url_guard_blocks_when_enabled():
    plugin = make_plugin()
    plugin.block_internal_ip = True
    guard = plugin._search_url_guard()
    assert guard("http://127.0.0.1/a") is False
    assert guard("http://localhost/a") is False
    assert guard("file:///etc/passwd") is False
    assert guard("https://example.com/a") is True
