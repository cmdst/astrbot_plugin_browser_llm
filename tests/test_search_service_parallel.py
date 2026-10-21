"""SearchService.search_waves 行为测试：并发 / 轮数 / 超时 / 去重排序 / 失败。

全部用 FakeProvider + 注入时钟，**零网络**。逐条对应 spec §13 的 AC：

- AC-16 真并发、AC-17 max_parallel、AC-18 max_rounds（搜索步数）、
  AC-19 早停、AC-20 单源超时隔离、AC-21 调用级预算、AC-23 模板覆盖超时、
- AC-24 去重合并、AC-25 排序确定、AC-26 全失败不抛异常、AC-27 冷却、
  AC-28 空 key 不占名额、AC-29 providers 过滤，
- 另外覆盖：SSRF 字面校验丢弃、TTL 缓存、返回体体积裁剪、snippet 先脱敏后截断。
"""

from __future__ import annotations

import asyncio
import json
import time

from core.search import (
    PAYLOAD_MAX_BYTES,
    SKIP_CODES,
    ProviderEntry,
    ProviderError,
    SearchAttempt,
    SearchResult,
    SearchService,
    clean_result,
    dedupe_key,
    merge_batches,
    render_payload,
    resolve_status,
    summarize,
)


class FakeProvider:
    """可编排的假 provider：延迟 / 结果 / 异常 / 记录调用参数。"""

    def __init__(self, name, *, delay=0.0, results=None, error=None):
        self.name = name
        self.delay = delay
        self._results = list(results or [])
        self._error = error
        self.calls = 0
        self.last_timeout = None
        self.last_limit = None

    async def search(self, query, *, limit, timeout):
        self.calls += 1
        self.last_timeout = timeout
        self.last_limit = limit
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self._error, BaseException):
            raise self._error
        return list(self._results)


def result(title="t", url="https://example.com/a", snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def entry(
    name,
    provider,
    *,
    template="brave",
    api_key="key-0001",
    weight=50,
    timeout_s=0.0,
    limit=0,
    enabled=True,
    url_guard=None,
    config_error="",
):
    return ProviderEntry(
        name=name,
        template=template,
        provider=provider,
        enabled=enabled,
        api_key=api_key,
        weight=weight,
        timeout_s=timeout_s,
        limit=limit,
        url_guard=url_guard,
        config_error=config_error,
    )


def service(entries, **kwargs):
    kwargs.setdefault("timeout_s", 1.0)
    kwargs.setdefault("max_results", 5)
    kwargs.setdefault("rate_limit_cooldown_s", 0.0)
    return SearchService(entries=entries, **kwargs)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# 并发 / 轮数 / 早停
# ---------------------------------------------------------------


def test_wave_is_really_parallel():
    """AC-16：3 个源各 sleep(0.05)、max_parallel=3 ⇒ 总耗时 ≈ 0.05（串行 ≥0.15）。"""
    providers = [
        FakeProvider(f"p{i}", delay=0.05, results=[result(url=f"https://e.com/{i}")])
        for i in range(3)
    ]
    entries = [entry(f"p{i}#1", p) for i, p in enumerate(providers)]
    svc = service(entries, max_results=10)

    began = time.monotonic()
    wave = run(
        svc.search_waves("q", max_rounds=1, max_parallel=3, provider_timeout=1.0)
    )
    elapsed = time.monotonic() - began

    assert elapsed < 0.12, f"未并发：{elapsed:.3f}s"
    assert wave.rounds_used == 1
    assert [a.round for a in wave.attempts] == [1, 1, 1]
    assert wave.code == "ok" and len(wave.results) == 3


def test_max_parallel_limits_wave_size():
    """AC-17：池 5、max_parallel=2、max_rounds=1 ⇒ 只调 2 个。"""
    providers = {
        f"p{i}": FakeProvider(f"p{i}", results=[result(url=f"https://e.com/{i}")])
        for i in range(5)
    }
    entries = [entry(f"p{i}#1", providers[f"p{i}"]) for i in range(5)]
    svc = service(entries, max_results=10)

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=2, provider_timeout=1.0))

    called = [name for name, p in providers.items() if p.calls]
    assert called == ["p0", "p1"]
    assert wave.rounds_used == 1


def test_max_rounds_is_wave_cap():
    """AC-18：池 5、max_parallel=2、max_rounds=3 ⇒ 5 个全试、rounds_used=3。"""
    providers = {
        f"p{i}": FakeProvider(f"p{i}", results=[result(url=f"https://e.com/{i}")])
        for i in range(5)
    }
    entries = [entry(f"p{i}#1", providers[f"p{i}"]) for i in range(5)]
    svc = service(entries, max_results=10)

    wave = run(svc.search_waves("q", max_rounds=3, max_parallel=2, provider_timeout=1.0))

    assert wave.rounds_used == 3
    assert sum(1 for p in providers.values() if p.calls) == 5
    # 上限公式：min(池, parallel × rounds) = min(5, 6) = 5
    assert len([a for a in wave.attempts if a.code != "ok"]) == 0
    assert sorted({a.round for a in wave.attempts}) == [1, 2, 3]


def test_max_rounds_one_never_calls_second_wave():
    providers = [FakeProvider(f"p{i}", results=[result(url=f"https://e.com/{i}")]) for i in range(3)]
    entries = [entry(f"p{i}#1", p) for i, p in enumerate(providers)]
    svc = service(entries, max_results=10)

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert wave.rounds_used == 1
    assert providers[0].calls == 1
    assert providers[1].calls == 0 and providers[2].calls == 0


def test_early_stop_when_enough_results():
    """AC-19：第 1 波就够 max_results ⇒ rounds_used=1，第 2 波不被调用。"""
    first = FakeProvider("a", results=[result(url=f"https://e.com/{i}") for i in range(5)])
    second = FakeProvider("b", results=[result(url="https://e.com/other")])
    svc = service([entry("a#1", first), entry("b#1", second)], max_results=3)

    wave = run(svc.search_waves("q", max_rounds=2, max_parallel=1, provider_timeout=1.0))

    assert wave.rounds_used == 1
    assert second.calls == 0
    assert len(wave.results) == 3


# ---------------------------------------------------------------
# 超时 / 预算
# ---------------------------------------------------------------


def test_single_source_timeout_is_isolated():
    """AC-20：慢源 5s + 快源，provider_timeout=0.2 ⇒ 0.2s 级返回，结果不丢。"""
    slow = FakeProvider("slow", delay=5.0, results=[result(url="https://e.com/slow")])
    fast = FakeProvider("fast", results=[result(url="https://e.com/fast")])
    svc = service([entry("slow#1", slow), entry("fast#1", fast)])

    began = time.monotonic()
    wave = run(
        svc.search_waves("q", max_rounds=1, max_parallel=2, provider_timeout=0.2)
    )
    elapsed = time.monotonic() - began

    assert elapsed < 1.0
    codes = {a.provider: a.code for a in wave.attempts}
    assert codes["slow#1"] == "timeout"
    assert codes["fast#1"] == "ok"
    assert [r.url for r in wave.results] == ["https://e.com/fast"]
    assert wave.status == "partial"
    assert "slow#1:timeout" in wave.error_codes


def test_time_budget_stops_new_waves_and_keeps_results():
    """AC-21：``provider_timeout``（15s）**大于**调用预算（0.4s）时，第 1 波
    的慢源按剩余预算截断、第 2 波不再开，已有结果保留 + notes=total_timeout。

    这是 t6 review 指的"真组合"：旧用例把 ``provider_timeout`` 压到 0.2s，
    测到的是**单源先超时**（AC-20）而不是**预算到点**（AC-21）。
    """
    fast = FakeProvider("fast", results=[result(url="https://e.com/fast")])
    slow1 = FakeProvider("slow1", delay=10.0)
    slow2 = FakeProvider("slow2", delay=10.0)
    slow3 = FakeProvider("slow3", delay=10.0)
    svc = service(
        [
            entry("fast#1", fast),
            entry("slow1#1", slow1),
            entry("slow2#1", slow2),
            entry("slow3#1", slow3),
        ],
        max_results=10,
    )

    began = time.monotonic()
    wave = run(
        svc.search_waves(
            "q",
            max_rounds=3,
            max_parallel=2,
            provider_timeout=15.0,  # 远大于预算：单源必须被预算压缩
            time_budget_s=0.4,
        )
    )
    elapsed = time.monotonic() - began

    # 慢源被压到 ~0.4s（不是 15s），第 2 波被预算拦下。
    assert elapsed < 1.6
    assert slow1.calls == 1
    assert slow2.calls == 0  # 第 2 波没开
    assert wave.rounds_used == 1
    assert "total_timeout" in wave.notes
    assert wave.status == "partial"
    assert [r.url for r in wave.results] == ["https://e.com/fast"]
    codes = {a.provider: a.code for a in wave.attempts}
    assert codes["slow1#1"] == "timeout"


def test_budget_caps_single_source_wait_not_provider_timeout():
    """AC-21（回归）：4 个慢源 + ``provider_timeout=15`` + 预算 0.4s ⇒ 墙钟
    由**预算**决定，不是 provider_timeout，也不会全丢成外层超时。

    复刻 reviewer 的实测比例（total=5 / provider=15 / 4×sleep(10)），
    用 0.4s 预算等比缩放以便秒级回归。
    """
    slows = [FakeProvider(f"s{i}", delay=10.0) for i in range(4)]
    svc = service([entry(f"s{i}#1", p) for i, p in enumerate(slows)], max_results=10)

    began = time.monotonic()
    wave = run(
        svc.search_waves(
            "q", max_rounds=1, max_parallel=4, provider_timeout=15.0, time_budget_s=0.4
        )
    )
    elapsed = time.monotonic() - began

    assert elapsed < 1.6, f"墙钟应由预算决定（0.4s 级），实测 {elapsed:.2f}s"
    assert wave.status == "failed"
    assert "total_timeout" in wave.notes
    assert {a.code for a in wave.attempts} == {"timeout"}
    # 关键：每个源收到的 timeout 被压缩到剩余预算，不是 15s。
    assert all(p.last_timeout is not None and p.last_timeout <= 0.5 for p in slows)


def test_budget_keeps_first_wave_results_when_next_wave_is_slow():
    """AC-21：第 1 波拿到结果、第 2 波是慢源 ⇒ 预算到点时第 1 波结果必须保留。"""
    fast = FakeProvider("fast", results=[result(url="https://e.com/fast")])
    slow = FakeProvider("slow", delay=10.0)
    svc = service([entry("fast#1", fast), entry("slow#1", slow)], max_results=10)

    began = time.monotonic()
    wave = run(
        svc.search_waves(
            "q",
            max_rounds=2,
            max_parallel=1,
            provider_timeout=15.0,
            time_budget_s=0.5,
        )
    )
    elapsed = time.monotonic() - began

    assert elapsed < 1.8
    assert wave.rounds_used == 2  # 第 2 波开了，但被预算截断
    assert wave.status == "partial"
    assert "total_timeout" in wave.notes
    assert [r.url for r in wave.results] == ["https://e.com/fast"]
    assert slow.last_timeout is not None and slow.last_timeout <= 0.6


def test_progress_container_reports_incremental_results():
    """AC-21（②）：传入 ``progress`` 容器 ⇒ 每波结束都能读到增量结果。

    main.py 的外层 ``asyncio.wait_for`` 超时会取消协程并丢掉返回值，靠这个
    容器把已得结果渲染成 partial（不许整批丢弃）。
    """
    fast = FakeProvider("fast", results=[result(url="https://e.com/fast")])
    slow = FakeProvider("slow", delay=10.0)
    svc = service([entry("fast#1", fast), entry("slow#1", slow)], max_results=10)
    progress: dict = {}

    wave = run(
        svc.search_waves(
            "q",
            max_rounds=2,
            max_parallel=1,
            provider_timeout=15.0,
            time_budget_s=0.5,
            progress=progress,
        )
    )

    assert progress["results"], "第一波结果必须已写入 progress"
    assert [r.url for r in progress["results"]] == ["https://e.com/fast"]
    assert progress["rounds_used"] == wave.rounds_used
    assert progress["status"] == wave.status
    assert progress["attempts"] and progress["attempts"][0].provider == "fast#1"


def test_pool_exhausted_is_not_reported_as_total_timeout():
    """AC-21：池耗尽（没有没试过的源）≠ 预算耗尽，后者才写 total_timeout。"""
    a = FakeProvider("a", results=[result(url="https://e.com/a")])
    svc = service([entry("a#1", a)], max_results=10)

    wave = run(svc.search_waves("q", max_rounds=5, max_parallel=1, time_budget_s=5.0))

    assert "total_timeout" not in wave.notes
    assert "pool_exhausted" in wave.notes
    assert wave.rounds_used == 1


def test_template_timeout_overrides_global():
    """AC-23：模板 timeout=30 + 全局 1 ⇒ provider 收到 30。"""
    provider = FakeProvider("a", results=[result()])
    svc = service([entry("a#1", provider, timeout_s=30.0)], timeout_s=1.0)

    run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert provider.last_timeout == 30.0


def test_template_limit_overrides_global():
    provider = FakeProvider("a", results=[result()])
    svc = service([entry("a#1", provider, limit=7)], max_results=3)

    run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert provider.last_limit == 7


# ---------------------------------------------------------------
# 去重 / 排序
# ---------------------------------------------------------------


def test_dedupe_merges_same_url_across_providers():
    """AC-24：host 大小写 + utm_source 差异 ⇒ 1 条，sources 长度 2，rank 连续。"""
    brave = FakeProvider(
        "brave", results=[result(title="短", url="https://Example.com/a?utm_source=x")]
    )
    bocha = FakeProvider(
        "bocha", results=[result(title="更长的标题", url="https://example.com/a")]
    )
    svc = service([entry("brave#1", brave), entry("bocha#1", bocha)])

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=2, provider_timeout=1.0))

    assert len(wave.results) == 1
    merged = wave.results[0]
    assert merged.rank == 1
    assert [s["provider"] for s in merged.sources] == ["brave#1", "bocha#1"]
    assert merged.title == "更长的标题"  # 取更长
    assert merged.url == "https://Example.com/a?utm_source=x"  # 同权重、更早轮次胜出


def test_sort_order_is_deterministic_and_weight_first():
    """AC-25：weight 高者在前；同权重同 rank 按去重键字典序；跑 10 次一致。"""
    def build():
        high = FakeProvider("high", results=[result(title="h", url="https://z.example.com/1")])
        mid_a = FakeProvider("mid_a", results=[result(title="ma", url="https://b.example.com/1")])
        mid_b = FakeProvider("mid_b", results=[result(title="mb", url="https://a.example.com/1")])
        svc = service(
            [
                entry("high#1", high, weight=90),
                entry("mid_a#1", mid_a, weight=50),
                entry("mid_b#1", mid_b, weight=50),
            ]
        )
        return svc

    orders = []
    for _ in range(10):
        svc = build()
        wave = run(
            svc.search_waves("q", max_rounds=1, max_parallel=3, provider_timeout=1.0)
        )
        orders.append([r.url for r in wave.results])

    assert orders[0][0] == "https://z.example.com/1"
    assert orders[0][1] == "https://a.example.com/1"  # a... < b...（字典序）
    assert all(order == orders[0] for order in orders)


def test_dedupe_key_normalizes():
    assert dedupe_key("HTTPS://Example.COM:443/a/?utm_source=x&b=2&a=1#frag") == (
        "https://example.com/a?a=1&b=2"
    )
    assert dedupe_key("http://example.com:80/") == "http://example.com/"
    assert dedupe_key("https://e.com/a/") == "https://e.com/a"
    assert dedupe_key("") == ""


def test_merge_batches_keeps_higher_weight_title_source():
    a = entry("a#1", FakeProvider("a"), weight=10)
    b = entry("b#1", FakeProvider("b"), weight=90)
    merged = merge_batches(
        [
            (a, 1, [SearchResult(title="a", url="https://e.com/x", snippet="")]),
            (b, 1, [SearchResult(title="b", url="https://e.com/x", snippet="bbb")]),
        ]
    )
    assert len(merged) == 1
    assert merged[0].url == "https://e.com/x"
    assert merged[0].snippet == "bbb"
    assert [s["provider"] for s in merged[0].sources] == ["a#1", "b#1"]


# ---------------------------------------------------------------
# 失败 / 冷却 / 池过滤
# ---------------------------------------------------------------


def test_all_sources_fail_returns_structured_failure():
    """AC-26：timeout / 429 / bad_response ⇒ failed + code=rate_limited，不抛异常。"""
    slow = FakeProvider("slow", delay=5.0)
    limited = FakeProvider("limited", error=ProviderError("rate_limited"))
    broken = FakeProvider("broken", error=ProviderError("bad_response"))
    svc = service(
        [
            entry("slow#1", slow),
            entry("limited#1", limited),
            entry("broken#1", broken),
        ]
    )

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=3, provider_timeout=0.2))

    assert wave.status == "failed"
    assert wave.results == []
    assert wave.code == "rate_limited"  # 优先级 auth > rate_limited > http_5xx > timeout ...
    assert len(wave.error_codes) == 3


def test_auth_has_highest_priority():
    auth = FakeProvider("auth", error=ProviderError("auth"))
    limited = FakeProvider("limited", error=ProviderError("rate_limited"))
    svc = service([entry("auth#1", auth), entry("limited#1", limited)])
    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=2, provider_timeout=0.5))
    assert wave.code == "auth"


def test_rate_limit_sets_cooldown_and_skips_next_call():
    """AC-27：429 后同实例第二次调用记 cooldown 且请求计数不增。"""
    limited = FakeProvider("limited", error=ProviderError("rate_limited"))
    ok = FakeProvider("ok", results=[result()])
    svc = service(
        [entry("limited#1", limited), entry("ok#1", ok)], rate_limit_cooldown_s=60.0
    )

    async def go():
        first = await svc.search_waves("q", max_rounds=1, max_parallel=2, provider_timeout=0.5)
        second = await svc.search_waves("q2", max_rounds=1, max_parallel=2, provider_timeout=0.5)
        return first, second

    first, second = run(go())
    assert "limited#1:rate_limited" in first.error_codes
    assert limited.calls == 1
    cools = [a for a in second.attempts if a.code == "cooldown"]
    assert [a.provider for a in cools] == ["limited#1"]
    assert limited.calls == 1  # 冷却期内不再请求


def test_skipped_sources_do_not_make_partial():
    """AC-28（L-1）：池里有空 key 的源时，有结果的调用仍应是 ``ok``。

    ``missing_key`` / ``disabled`` / ``cooldown`` / ``unknown_template`` /
    ``bad_config`` 都是"没发请求就被跳过"，不是失败源；否则只要池里留着一个
    未配置的模板，每次调用都显示"部分失败"。
    """
    ok = FakeProvider("ok", results=[result(url="https://e.com/ok")])
    unconfigured = FakeProvider("nolink", results=[result(url="https://e.com/x")])
    svc = service(
        [
            entry("ok#1", ok),
            entry("nolink#1", unconfigured, api_key=""),
        ]
    )

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=2))

    assert wave.status == "ok", "跳过码不应把 status 拉成 partial"
    assert [r.url for r in wave.results] == ["https://e.com/ok"]
    assert unconfigured.calls == 0
    assert ("nolink#1", "missing_key") in {
        (a.provider, a.code) for a in wave.attempts
    }
    text = summarize(
        status=wave.status, code=wave.code, results=wave.results,
        attempts=wave.attempts, query="q",
    )
    assert "部分失败" not in text


def test_cooldown_skip_does_not_make_partial():
    """AC-27（L-1）：冷却中的源也算跳过，不当失败源。"""
    first = FakeProvider("a", error=ProviderError("rate_limited"))
    second = FakeProvider("b", results=[result(url="https://e.com/b")])
    svc = service(
        [entry("a#1", first), entry("b#1", second)],
        max_results=10,
        rate_limit_cooldown_s=60.0,
    )

    run(svc.search_waves("q", max_rounds=1, max_parallel=2))
    wave = run(svc.search_waves("q2", max_rounds=1, max_parallel=2))

    codes = {a.provider: a.code for a in wave.attempts}
    assert codes["a#1"] == "cooldown"
    assert wave.status == "ok"


def test_all_skipped_pool_reports_no_provider():
    """AC-28（L-1）：池内全是跳过源 ⇒ ``failed/no_provider``（不是 partial）。"""
    svc = service([entry("nolink#1", FakeProvider("x"), api_key="")])

    wave = run(svc.search_waves("q"))

    assert wave.status == "failed"
    assert wave.code == "no_provider"
    assert [(a.provider, a.code) for a in wave.attempts] == [("nolink#1", "missing_key")]
    assert SKIP_CODES >= {"missing_key", "disabled", "cooldown", "unknown_template", "bad_config"}


def test_resolve_status_ignores_skip_codes_only():
    """L-1 单元口径：跳过码不算失败，真实错误码仍算。"""
    skipped = [SearchAttempt("a", "brave", 0, "skipped", "missing_key")]
    real = [SearchAttempt("b", "brave", 1, "timeout", "timeout")]

    assert resolve_status([result()], skipped) == ("ok", "ok")
    assert resolve_status([result()], skipped + real) == ("partial", "ok")
    assert resolve_status([], skipped) == ("failed", "no_provider")
    assert resolve_status([], real) == ("failed", "timeout")


def test_missing_key_does_not_consume_wave_slot():
    """AC-28：空 key 的源在组波前过滤（round=0、missing_key），不占名额。"""
    tavily = FakeProvider("tavily", results=[result(url="https://e.com/t")])
    brave = FakeProvider("brave", results=[result(url="https://e.com/b")])
    svc = service(
        [
            entry("tavily#1", tavily, template="tavily", api_key=""),
            entry("brave#1", brave),
        ]
    )

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert tavily.calls == 0
    assert brave.calls == 1
    skipped = [a for a in wave.attempts if a.code == "missing_key"]
    assert len(skipped) == 1 and skipped[0].round == 0
    assert [r.url for r in wave.results] == ["https://e.com/b"]


def test_disabled_provider_skipped():
    provider = FakeProvider("a", results=[result()])
    svc = service([entry("a#1", provider, enabled=False)])
    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))
    assert provider.calls == 0
    assert [a.code for a in wave.attempts] == ["disabled"]
    assert wave.code == "no_provider"


def test_unknown_template_reported():
    svc = service([entry("x#1", None, template="serpapi", config_error="")])
    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))
    assert [a.code for a in wave.attempts] == ["unknown_template"]


def test_bad_config_reported():
    svc = service([entry("x#1", None, config_error="bad_config")])
    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))
    assert [a.code for a in wave.attempts] == ["bad_config"]


def test_providers_filter_limits_calls():
    """AC-29：只传池内一个名字 ⇒ 只调它。"""
    a = FakeProvider("a", results=[result(url="https://e.com/a")])
    b = FakeProvider("b", results=[result(url="https://e.com/b")])
    svc = service([entry("a#1", a), entry("b#1", b)])

    wave = run(
        svc.search_waves(
            "q", max_rounds=1, max_parallel=2, provider_timeout=1.0, provider_filter=["b#1"]
        )
    )

    assert a.calls == 0 and b.calls == 1
    assert [r.url for r in wave.results] == ["https://e.com/b"]


def test_unknown_provider_name_reported_and_ignored():
    a = FakeProvider("a", results=[result(url="https://e.com/a")])
    svc = service([entry("a#1", a)])

    wave = run(
        svc.search_waves(
            "q",
            max_rounds=1,
            max_parallel=2,
            provider_timeout=1.0,
            provider_filter=["a#1", "ghost#9"],
        )
    )

    assert [r.url for r in wave.results] == ["https://e.com/a"]
    assert any(a.code == "unknown_provider" for a in wave.attempts)


def test_empty_query_short_circuits_without_network():
    provider = FakeProvider("a", results=[result()])
    svc = service([entry("a#1", provider)])
    wave = run(svc.search_waves("   ", max_rounds=1, max_parallel=1))
    assert wave.status == "failed" and wave.code == "empty_query"
    assert provider.calls == 0


# ---------------------------------------------------------------
# SSRF 字面校验 / 缓存 / 返回体
# ---------------------------------------------------------------


def test_url_guard_drops_blocked_results():
    """AC-31（结果侧）：未过字面校验的链接全部丢弃并记 ssrf_blocked。"""
    provider = FakeProvider(
        "a",
        results=[
            result(url="http://127.0.0.1/a"),
            result(url="http://10.0.0.1/b"),
            result(url="https://ok.example.com/c"),
        ],
    )
    guard = lambda url: "ok.example.com" in url  # noqa: E731
    svc = service([entry("a#1", provider, url_guard=guard)])

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert [r.url for r in wave.results] == ["https://ok.example.com/c"]
    assert [a.code for a in wave.attempts] == ["ok"]


def test_url_guard_blocking_everything_marks_ssrf_blocked():
    provider = FakeProvider("a", results=[result(url="http://127.0.0.1/a")])
    svc = service([entry("a#1", provider, url_guard=lambda url: False)])

    wave = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert wave.results == []
    assert [a.code for a in wave.attempts] == ["ssrf_blocked"]
    assert "a#1:ssrf_blocked" in wave.error_codes


def test_cache_hit_avoids_second_call():
    provider = FakeProvider("a", results=[result()])
    svc = service([entry("a#1", provider)], cache_ttl_s=120.0)

    first = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))
    second = run(svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0))

    assert first.cache == "miss"
    assert second.cache == "hit"
    assert provider.calls == 1
    assert second.results[0].url == first.results[0].url


def test_cache_expires_with_injected_clock():
    provider = FakeProvider("a", results=[result()])
    now = [1000.0]
    svc = SearchService(
        entries=[entry("a#1", provider)],
        timeout_s=1.0,
        cache_ttl_s=10.0,
        clock=lambda: now[0],
    )

    async def go():
        await svc.search_waves("q", max_rounds=1, max_parallel=1, provider_timeout=1.0)
        now[0] += 11
        return await svc.search_waves(
            "q", max_rounds=1, max_parallel=1, provider_timeout=1.0
        )

    out = run(go())
    assert out.cache == "miss"
    assert provider.calls == 2


def test_clean_result_redacts_before_truncating():
    """spec §11.2 #13/#16：先脱敏再截断（顺序反了会留下"半截 key"）。"""
    secret = "sk-SECRET-0123456789abcdef"
    item = SearchResult(title=f"t {secret}", url="https://e.com/x", snippet=secret + "y" * 400)
    cleaned = clean_result(item, secrets=[secret])
    assert secret not in cleaned.snippet
    assert cleaned.snippet.endswith("…")
    assert len(cleaned.snippet) <= 302


def test_clean_result_drops_blank_url():
    assert clean_result(SearchResult(title="t", url="   ")) is None


def test_render_payload_shape_and_status_mapping():
    payload = json.loads(
        render_payload(
            status="partial",
            code="ok",
            summary="部分失败",
            query="q",
            rounds_used=2,
            max_rounds=2,
            results=[merge_batches([]) or []][0] or [],
            attempts=[
                SearchAttempt(provider="a#1", template="brave", round=1, outcome="ok", code="ok")
            ],
            notes=["region_unsupported:tavily"],
        )
    )
    assert payload["status"] == "partial"
    assert payload["ok"] is True
    assert payload["query"] == "q"
    assert payload["attempts"][0]["provider"] == "a#1"
    assert payload["notes"] == ["region_unsupported:tavily"]
    assert set(payload) >= {
        "status", "ok", "code", "summary", "query", "rounds_used", "max_rounds",
        "providers_effective", "cache", "elapsed_ms", "results", "attempts",
        "error_codes", "notes",
    }


def test_render_payload_truncates_and_flags():
    """超体积：先砍 fetched.text，再砍 results 尾部，并记 payload_truncated。"""
    from core.search import MergedResult

    big_text = "x" * (PAYLOAD_MAX_BYTES // 2)
    results = [
        MergedResult(
            title=f"t{i}",
            url=f"https://e.com/{i}",
            snippet="s" * 200,
            sources=[{"provider": "a#1", "rank": i, "weight": 50, "round": 1}],
            fetched={"ok": True, "code": "ok", "status": 200, "chars": len(big_text),
                     "truncated": False, "text": big_text, "elapsed_ms": 1},
        )
        for i in range(4)
    ]
    text = render_payload(
        status="ok", code="ok", summary="s", query="q", results=results
    )
    payload = json.loads(text)
    assert len(text.encode("utf-8")) <= PAYLOAD_MAX_BYTES
    assert "payload_truncated" in payload["notes"]
    assert all(not item["fetched"]["text"] for item in payload["results"])


def test_status_failed_uses_error_prefix():
    from core.search import summarize

    attempt = SearchAttempt(
        provider="a#1", template="brave", round=1, outcome="timeout", code="timeout"
    )
    text = summarize(status="failed", code="timeout", results=[], attempts=[attempt], query="q")
    assert text.startswith("【错误】")
