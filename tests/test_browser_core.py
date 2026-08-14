"""BrowserCore 资源清理与 SSRF 兜底拦截测试。

覆盖：
- new_page 失败（context.new_page 抛异常）时半成品 context/page 必须关闭（防泄漏）；
- block_internal_ip=True 时安装 context 级路由拦截（**/*），False 时不安装；
- SSRF 路由处理器：内网 host abort、公网 host / file:// continue、判定缓存；
- _ssrf_host_is_internal：IP 字面量（含混淆形式）与 DNS 域名判定。

依赖 conftest.py 的 astrbot 桩与根目录 sys.path。
"""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import core.browser as browser_module
from core.browser import BrowserCore


def _run(coro):
    return asyncio.run(coro)


class _FakePage:
    def __init__(self):
        self.closed = False
        self.timeout = None

    def set_default_timeout(self, ms):
        self.timeout = ms

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, fail_new_page=False, fail_route=False):
        self.closed = False
        self.fail_new_page = fail_new_page
        self.fail_route = fail_route
        self.routes = []

    async def new_page(self):
        if self.fail_new_page:
            raise RuntimeError("context broken")
        return _FakePage()

    async def close(self):
        self.closed = True

    async def route(self, pattern, handler):
        if self.fail_route:
            raise RuntimeError("route failed")
        self.routes.append((pattern, handler))


class _FakeBrowser:
    def __init__(self, ctx, fail_close=False, hang_close=False):
        self.ctx = ctx
        self.closed = False
        self.fail_close = fail_close
        self.hang_close = hang_close
        self.close_calls = 0

    def is_connected(self):
        return True

    async def new_context(self, **kwargs):
        return self.ctx

    async def close(self):
        self.close_calls += 1
        if self.hang_close:
            await asyncio.sleep(3600)
        if self.fail_close:
            raise RuntimeError("browser close broken")
        self.closed = True


class _FakePlaywright:
    def __init__(self, fail_stop=False, hang_stop=False):
        self.stopped = False
        self.fail_stop = fail_stop
        self.hang_stop = hang_stop
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        if self.hang_stop:
            await asyncio.sleep(3600)
        if self.fail_stop:
            raise RuntimeError("playwright stop broken")
        self.stopped = True


def _make_core(ctx, block_internal_ip=True) -> BrowserCore:
    core = BrowserCore(
        {"browser_type": "chromium", "block_internal_ip": block_internal_ip}
    )
    core._browser = _FakeBrowser(ctx)
    core._playwright = object()  # ensure_browser 早退路径（_browser_alive 为真）
    return core


def _make_core_shutdown(browser, playwright) -> BrowserCore:
    """构造带真实 browser/playwright 假件的实例（用于 shutdown 链路测试）。"""
    core = BrowserCore({"browser_type": "chromium"})
    core._browser = browser
    core._playwright = playwright
    return core


class _Route:
    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.aborted = False
        self.continued = False

    async def abort(self, reason):
        self.aborted = True

    async def continue_(self):
        self.continued = True


# ------------------------------------------------------------
# new_page：泄漏防护
# ------------------------------------------------------------

def test_new_page_cleans_context_on_failure():
    ctx = _FakeContext(fail_new_page=True)
    core = _make_core(ctx)
    with pytest.raises(RuntimeError):
        _run(core.new_page())
    assert ctx.closed, "context.new_page 失败后半成品 context 必须关闭（防泄漏）"


def test_new_page_success_registers_context():
    ctx = _FakeContext()
    core = _make_core(ctx)
    page = _run(core.new_page())
    assert page is not None and page.timeout == 30000
    assert id(page) in core._page_contexts
    assert core._page_contexts[id(page)] is ctx


# ------------------------------------------------------------
# SSRF 兜底拦截：安装
# ------------------------------------------------------------

def test_new_page_installs_ssrf_guard():
    ctx = _FakeContext()
    core = _make_core(ctx)
    _run(core.new_page())
    assert len(ctx.routes) == 1 and ctx.routes[0][0] == "**/*"


def test_new_page_skips_guard_when_disabled():
    ctx = _FakeContext()
    core = _make_core(ctx, block_internal_ip=False)
    _run(core.new_page())
    assert ctx.routes == [], "关闭内网拦截时不应安装路由拦截"


def test_install_guard_route_failure_does_not_raise():
    ctx = _FakeContext(fail_route=True)
    core = _make_core(ctx)
    page = _run(core.new_page())  # 路由安装失败仅告警，不影响页面创建
    assert page is not None


# ------------------------------------------------------------
# SSRF 兜底拦截：路由处理器行为
# ------------------------------------------------------------

def test_ssrf_guard_blocks_internal_host():
    ctx = _FakeContext()
    core = _make_core(ctx)
    _run(core.new_page())
    handler = ctx.routes[0][1]
    r = _Route("http://127.0.0.1/admin")
    _run(handler(r))
    assert r.aborted and not r.continued, "内网 host 请求应被 abort"
    assert "127.0.0.1" in core._ssrf_host_cache, "判定结果应进入缓存"


def test_ssrf_guard_continues_public_host():
    ctx = _FakeContext()
    core = _make_core(ctx)
    _run(core.new_page())
    handler = ctx.routes[0][1]
    r = _Route("https://example.com/page")
    _run(handler(r))
    assert r.continued and not r.aborted


def test_ssrf_guard_continues_file_url():
    """本地页面渲染（file://）不受路由拦截影响。"""
    ctx = _FakeContext()
    core = _make_core(ctx)
    _run(core.new_page())
    handler = ctx.routes[0][1]
    r = _Route("file:///tmp/x.html")
    _run(handler(r))
    assert r.continued and not r.aborted


def test_ssrf_guard_blocks_redirect_to_internal():
    """302 重定向到内网（绕过前置 acheck_url 的场景）→ 拦截。"""
    ctx = _FakeContext()
    core = _make_core(ctx)
    _run(core.new_page())
    handler = ctx.routes[0][1]
    r = _Route("http://169.254.169.254/latest/meta-data/")
    _run(handler(r))
    assert r.aborted, "指向云元数据服务的内网请求应被 abort"


# ------------------------------------------------------------
# _ssrf_host_is_internal：判定与缓存
# ------------------------------------------------------------

def test_ssrf_host_is_internal_literals():
    core = _make_core(_FakeContext())
    assert _run(core._ssrf_host_is_internal("127.0.0.1")) is True
    assert _run(core._ssrf_host_is_internal("192.168.1.1")) is True
    assert _run(core._ssrf_host_is_internal("::1")) is True
    assert _run(core._ssrf_host_is_internal("2130706433")) is True, "十进制混淆"
    assert _run(core._ssrf_host_is_internal("127.1")) is True, "短点分混淆"
    assert _run(core._ssrf_host_is_internal("93.184.216.34")) is False


def test_ssrf_host_is_internal_dns_localhost():
    core = _make_core(_FakeContext())
    # localhost 经 DNS 应判定为内网（沙箱 /etc/hosts 必有 localhost -> 127.0.0.1）。
    assert _run(core._ssrf_host_is_internal("localhost")) is True


def test_ssrf_host_cache_used():
    core = _make_core(_FakeContext())
    assert _run(core._ssrf_host_is_internal("127.0.0.1")) is True
    assert core._ssrf_host_cache["127.0.0.1"][1] is True
    # 二次查询命中缓存（无需再解析）。
    assert _run(core._ssrf_host_is_internal("127.0.0.1")) is True


# ------------------------------------------------------------
# shutdown：清理路径（正常 / 异常继续清理 / 超时不悬挂）
# ------------------------------------------------------------

def test_shutdown_normal_cleans_all():
    browser = _FakeBrowser(_FakeContext())
    playwright = _FakePlaywright()
    core = _make_core_shutdown(browser, playwright)
    core._page_contexts[1] = object()  # 模拟残留映射，必须一并清空
    _run(core.shutdown())
    assert browser.closed and browser.close_calls == 1
    assert playwright.stopped and playwright.stop_calls == 1
    assert core._browser is None and core._playwright is None
    assert core._page_contexts == {}, "shutdown 后内部映射必须清空"


def test_shutdown_close_exception_still_stops_playwright():
    """browser.close 抛异常：不得中断 playwright.stop，且不向上抛。"""
    browser = _FakeBrowser(_FakeContext(), fail_close=True)
    playwright = _FakePlaywright()
    core = _make_core_shutdown(browser, playwright)
    _run(core.shutdown())  # 不得向上抛异常
    assert playwright.stopped, "browser.close 抛异常后 playwright.stop 仍须执行"
    assert core._browser is None and core._playwright is None


def test_shutdown_close_timeout_does_not_hang(monkeypatch):
    """browser.close 悬挂：shutdown 限时返回，playwright.stop 仍执行。"""
    monkeypatch.setattr(browser_module, "_BROWSER_CLOSE_TIMEOUT", 0.05)
    browser = _FakeBrowser(_FakeContext(), hang_close=True)
    playwright = _FakePlaywright()
    core = _make_core_shutdown(browser, playwright)
    start = time.monotonic()
    _run(core.shutdown())
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"close 悬挂时 shutdown 必须限时返回，实际 {elapsed:.2f}s"
    assert playwright.stopped, "browser.close 超时后 playwright.stop 仍须执行"
    assert core._browser is None and core._playwright is None


def test_shutdown_stop_exception_ignored():
    browser = _FakeBrowser(_FakeContext())
    playwright = _FakePlaywright(fail_stop=True)
    core = _make_core_shutdown(browser, playwright)
    _run(core.shutdown())  # 不得向上抛异常
    assert browser.closed
    assert core._browser is None and core._playwright is None


def test_shutdown_both_hang_bounded(monkeypatch):
    """browser.close 与 playwright.stop 同时悬挂：总时长受单步超时约束。"""
    monkeypatch.setattr(browser_module, "_BROWSER_CLOSE_TIMEOUT", 0.05)
    browser = _FakeBrowser(_FakeContext(), hang_close=True)
    playwright = _FakePlaywright(hang_stop=True)
    core = _make_core_shutdown(browser, playwright)
    start = time.monotonic()
    _run(core.shutdown())
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"两步均悬挂时 shutdown 仍须限时返回，实际 {elapsed:.2f}s"
    assert core._browser is None and core._playwright is None


def test_shutdown_idempotent():
    browser = _FakeBrowser(_FakeContext())
    playwright = _FakePlaywright()
    core = _make_core_shutdown(browser, playwright)
    _run(core.shutdown())
    _run(core.shutdown())  # 二次调用为 no-op，不得报错
    assert browser.close_calls == 1 and playwright.stop_calls == 1


def test_shutdown_failure_logs_warning_with_tag(caplog):
    """关闭失败必须 warning 级且带实例标识（便于定位残留实例）。"""
    browser = _FakeBrowser(_FakeContext(), fail_close=True)
    playwright = _FakePlaywright()
    core = _make_core_shutdown(browser, playwright)
    with caplog.at_level(logging.WARNING):
        _run(core.shutdown())
    tag = f"BrowserCore@{id(core):x}"
    assert any(
        tag in r.message and r.levelno == logging.WARNING for r in caplog.records
    ), "关闭失败日志须为 warning 级并带实例标识"


def test_close_page_page_close_exception_context_still_closed():
    class _BrokenPage(_FakePage):
        async def close(self):
            raise RuntimeError("page close broken")

    ctx = _FakeContext()
    page = _BrokenPage()
    core = _make_core(_FakeBrowser(ctx))
    core._page_contexts[id(page)] = ctx
    _run(core.close_page(page))  # 不得向上抛异常
    assert ctx.closed, "page.close 抛异常后 context 仍须关闭（防泄漏）"
    assert id(page) not in core._page_contexts, "关闭后映射必须弹出"


def test_close_page_timeout_bounded(monkeypatch):
    """page.close 悬挂：close_page 限时返回，context 仍尝试关闭。"""
    monkeypatch.setattr(browser_module, "_PAGE_CLOSE_TIMEOUT", 0.05)

    class _HangPage(_FakePage):
        async def close(self):
            await asyncio.sleep(3600)

    ctx = _FakeContext()
    page = _HangPage()
    core = _make_core(_FakeBrowser(ctx))
    core._page_contexts[id(page)] = ctx
    start = time.monotonic()
    _run(core.close_page(page))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"page.close 悬挂时 close_page 必须限时返回，实际 {elapsed:.2f}s"
    assert ctx.closed, "page.close 超时后 context 仍须尝试关闭"
    assert id(page) not in core._page_contexts


def test_close_page_context_close_exception_ignored():
    class _BrokenContext(_FakeContext):
        async def close(self):
            raise RuntimeError("ctx close broken")

    ctx = _BrokenContext()
    page = _FakePage()
    core = _make_core(_FakeBrowser(ctx))
    core._page_contexts[id(page)] = ctx
    _run(core.close_page(page))  # 不得向上抛异常
    assert page.closed
