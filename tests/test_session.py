"""SessionManager 全量测试：会话复用/隔离、多标签（新建/切换/关闭）、
容量上限、空闲回收、会话锁、释放与关闭。使用 MockBrowser，不依赖
playwright。"""

import asyncio

import pytest

from core.session import SessionManager


class FakePage:
    """mock Page：带自增 id，可标记关闭。"""

    _seq = 0

    def __init__(self):
        FakePage._seq += 1
        self.id = FakePage._seq
        self.closed = False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, wait_until="domcontentloaded"):
        pass


class MockBrowser:
    """mock BrowserCore：记录创建/关闭的页面。"""

    def __init__(self):
        self.created = []
        self.closed = []

    async def new_page(self):
        p = FakePage()
        self.created.append(p)
        return p

    async def close_page(self, page):
        self.closed.append(page)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mb() -> MockBrowser:
    return MockBrowser()


@pytest.fixture
def sm(mb) -> SessionManager:
    return SessionManager(mb, max_pages=4, idle_timeout=60, default_url="https://www.baidu.com")


# ------------------------------------------------------------
# 单会话复用 / 多会话隔离
# ------------------------------------------------------------

def test_ensure_page_creates_once_and_reuses(sm, mb):
    p1 = _run(sm.ensure_page("grp-a"))
    p1b = _run(sm.ensure_page("grp-a"))
    assert p1 is not None and p1 is p1b, "同一 umo 应复用同一页面"
    assert len(mb.created) == 1, "只应创建一次"


def test_ensure_page_isolates_sessions(sm, mb):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.ensure_page("grp-b"))
    assert p1 is not p2, "不同 umo 应隔离"
    assert sm.tab_count("grp-a") == 1 and sm.tab_count("grp-b") == 1


def test_ensure_page_returns_active_tab(sm):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "https://example.com/2"))
    # 新标签激活后 ensure_page 应返回激活页
    assert _run(sm.ensure_page("grp-a")) is p2 is not p1


# ------------------------------------------------------------
# 多标签：新建 / 切换 / 计数
# ------------------------------------------------------------

def test_new_tab_appends_and_activates(sm):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "https://example.com/2"))
    assert sm.tab_count("grp-a") == 2
    assert sm._active["grp-a"] == 1 and sm._pages["grp-a"][1] is p2


def test_new_tab_on_nonexistent_session_creates(sm):
    p = _run(sm.new_tab("new-session", "https://example.com/x"))
    assert p is not None and sm.tab_count("new-session") == 1


def test_switch_tab_one_based(sm):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "u2"))
    p3 = _run(sm.new_tab("grp-a", "u3"))
    assert _run(sm.switch_tab("grp-a", 1)) is p1
    assert _run(sm.switch_tab("grp-a", 2)) is p2
    assert _run(sm.switch_tab("grp-a", 3)) is p3


@pytest.mark.parametrize("bad_index", [0, -1, 99])
def test_switch_tab_out_of_range(sm, bad_index):
    _run(sm.ensure_page("grp-a"))
    assert _run(sm.switch_tab("grp-a", bad_index)) is None


def test_switch_tab_nonexistent_session(sm):
    assert _run(sm.switch_tab("no-such", 1)) is None


def test_tab_count_zero_for_unknown(sm):
    assert sm.tab_count("no-such") == 0


# ------------------------------------------------------------
# 多标签：关闭
# ------------------------------------------------------------

def test_close_tab_active_prefers_previous(sm, mb):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "u2"))
    p3 = _run(sm.new_tab("grp-a", "u3"))
    _run(sm.switch_tab("grp-a", 3))  # 激活 p3 (active=2)
    assert _run(sm.close_tab("grp-a", 3)) is True
    assert sm.tab_count("grp-a") == 2
    assert p3 in mb.closed, "被关闭的标签应交给 browser.close_page"
    assert sm._active["grp-a"] == 1 and sm._pages["grp-a"][1] is p2, "应激活前一个标签"


def test_close_tab_active_first_prefers_next(sm):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "u2"))
    _run(sm.switch_tab("grp-a", 1))  # 激活第一个标签
    assert _run(sm.close_tab("grp-a", 1)) is True
    assert sm._active["grp-a"] == 0 and sm._pages["grp-a"][0] is p2, "关闭首标签应激活后一个"


def test_close_tab_before_active_keeps_page(sm):
    p1 = _run(sm.ensure_page("grp-a"))
    p2 = _run(sm.new_tab("grp-a", "u2"))
    p3 = _run(sm.new_tab("grp-a", "u3"))
    _run(sm.switch_tab("grp-a", 3))  # 激活 p3 (active=2)
    assert _run(sm.close_tab("grp-a", 1)) is True  # 关闭激活前的标签
    assert sm._active["grp-a"] == 1 and sm._pages["grp-a"][1] is p3, "激活页应保持不变"


@pytest.mark.parametrize("bad_index", [0, -1, 99])
def test_close_tab_out_of_range(sm, bad_index):
    _run(sm.ensure_page("grp-a"))
    assert _run(sm.close_tab("grp-a", bad_index)) is False


def test_close_tab_nonexistent_session(sm):
    assert _run(sm.close_tab("no-such", 1)) is False


def test_close_last_tab_clears_mapping(sm):
    _run(sm.ensure_page("grp-a"))
    _run(sm.new_tab("grp-a", "u2"))
    assert _run(sm.close_tab("grp-a", 1)) is True
    assert _run(sm.close_tab("grp-a", 1)) is True
    assert sm.tab_count("grp-a") == 0
    assert "grp-a" not in sm._pages
    assert "grp-a" not in sm._active
    assert "grp-a" not in sm._last_active


def test_ensure_page_rebuilds_after_all_closed(sm):
    _run(sm.ensure_page("grp-a"))
    _run(sm.close_tab("grp-a", 1))
    assert sm.tab_count("grp-a") == 0
    p_re = _run(sm.ensure_page("grp-a"))
    assert p_re is not None and sm.tab_count("grp-a") == 1, "标签全关后应自动重建"


# ------------------------------------------------------------
# 容量上限
# ------------------------------------------------------------

def test_new_tab_rejects_when_full(sm, mb):
    for i in range(4):
        assert _run(sm.ensure_page(f"grp-{i}")) is not None
    # 已满 4 个，新会话/新标签都被拒绝
    assert _run(sm.ensure_page("grp-4")) is None
    assert _run(sm.new_tab("grp-0", "https://example.com/x")) is None
    assert len(mb.created) == 4


def test_no_eviction_on_full(sm):
    """达上限拒绝新会话时不驱逐已有会话。"""
    for i in range(4):
        _run(sm.ensure_page(f"grp-{i}"))
    _run(sm.ensure_page("grp-4")) is None
    assert sm.tab_count("grp-0") == 1, "已有会话不受影响"


# ------------------------------------------------------------
# 会话锁 / 回收 / 释放
# ------------------------------------------------------------

def test_get_lock_unique_per_session(sm):
    l1 = sm.get_lock("grp-a")
    l1b = sm.get_lock("grp-a")
    l2 = sm.get_lock("grp-b")
    assert l1 is l1b, "同一会话应返回同一锁"
    assert l1 is not l2, "不同会话应不同锁"


def test_sweep_idle_reclaims_expired(sm):
    _run(sm.ensure_page("grp-a"))
    _run(sm.ensure_page("grp-b"))
    # 伪造时钟：把 _last_active 全部前移超过 idle_timeout
    for umo in list(sm._last_active):
        sm._last_active[umo] -= 100
    n = _run(sm.sweep_idle())
    assert n == 2
    assert sm.tab_count("grp-a") == 0 and sm.tab_count("grp-b") == 0


def test_sweep_idle_keeps_active(sm):
    _run(sm.ensure_page("grp-a"))
    assert _run(sm.sweep_idle()) == 0, "未过期不应回收"
    assert sm.tab_count("grp-a") == 1


def test_release_closes_all_tabs(sm, mb):
    _run(sm.ensure_page("grp-a"))
    _run(sm.new_tab("grp-a", "u2"))
    _run(sm.release("grp-a"))
    assert "grp-a" not in sm._pages
    assert sm.tab_count("grp-a") == 0
    assert len(mb.closed) == 2, "全部标签应被关闭"


def test_release_nonexistent_session(sm):
    _run(sm.release("no-such"))  # 不应抛异常


def test_shutdown_idempotent(sm, mb):
    _run(sm.ensure_page("grp-a"))
    _run(sm.new_tab("grp-a", "u2"))
    _run(sm.ensure_page("grp-b"))
    _run(sm.shutdown())
    _run(sm.shutdown())  # 幂等
    assert len(sm._pages) == 0
    assert len(mb.closed) == 3, "全部会话标签应被关闭"


def test_sweeper_start_stop_idempotent(sm):
    async def _go():
        await sm.start_sweeper()
        await sm.start_sweeper()  # 幂等
        task = sm._sweeper_task
        assert task is not None and not task.done()
        await sm.stop_sweeper()
        await sm.stop_sweeper()  # 幂等
        assert sm._sweeper_task is None

    _run(_go())


# ------------------------------------------------------------
# 资源泄漏回归：goto 失败时页面必须被关闭（P1 修复验证）
# ------------------------------------------------------------

class _GotoFailPage(FakePage):
    """goto 必失败的页面（模拟域名解析失败/目标不可达）。"""

    async def goto(self, url, wait_until="domcontentloaded"):
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")


class _GotoFailBrowser(MockBrowser):
    async def new_page(self):
        p = _GotoFailPage()
        self.created.append(p)
        return p


class _FlakyGotoBrowser(MockBrowser):
    """首次 goto 失败、之后成功的浏览器（验证失败后可自动恢复）。"""

    def __init__(self):
        super().__init__()
        self._fail_goto = True

    async def new_page(self):
        if self._fail_goto:
            self._fail_goto = False
            p = _GotoFailPage()
        else:
            p = FakePage()
        self.created.append(p)
        return p


def test_ensure_page_no_leak_when_goto_fails():
    """首标签 goto 失败 → 页面必须关闭，不得残留映射（防泄漏）。"""
    browser = _GotoFailBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)
    assert _run(sm.ensure_page("grp-a")) is None
    assert sm.tab_count("grp-a") == 0, "失败后不应残留标签映射"
    assert len(browser.closed) == 1, "goto 失败后页面必须交给 browser.close_page"


def test_new_tab_no_leak_when_goto_fails():
    """新标签 goto 失败 → 页面必须关闭（防泄漏）。"""
    browser = _GotoFailBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)
    assert _run(sm.new_tab("grp-a", "https://example.com/x")) is None
    assert sm.tab_count("grp-a") == 0
    assert len(browser.closed) == 1


def test_ensure_page_recovers_after_failed_goto():
    """失败后重试应能正常建页（失败不污染后续状态）。"""
    browser = _FlakyGotoBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)
    assert _run(sm.ensure_page("grp-a")) is None
    assert len(browser.closed) == 1
    page = _run(sm.ensure_page("grp-a"))
    assert page is not None and sm.tab_count("grp-a") == 1
    assert len(browser.closed) == 1, "成功路径不应误关页面"


def test_ensure_page_rebuilds_dead_active_page_and_closes_stale(sm, mb):
    """激活页失效（崩溃/关闭）→ 重建新页并关闭失效页（防 context 泄漏）。"""
    async def _go():
        p1 = await sm.ensure_page("grp-a")
        assert p1 is not None
        p1.closed = True  # 模拟页面崩溃/被外部关闭
        p2 = await sm.ensure_page("grp-a")
        assert p2 is not None and p2 is not p1, "失效页应被重建"
        assert sm.tab_count("grp-a") == 1, "重建后只保留一个标签"
        assert p1 in mb.closed, "失效页必须交给 browser.close_page"

    _run(_go())


# ------------------------------------------------------------
# P2 优化：空闲回收竞态（v1.2.0）
# ------------------------------------------------------------

def test_sweep_idle_skips_locked_session(sm, mb):
    """持锁会话（正在执行工具调用）跳过本轮回收，锁释放后正常回收。"""
    async def _go():
        await sm.ensure_page("grp-a")
        await sm.ensure_page("grp-b")
        # 伪造时钟：全部会话过期。
        for umo in list(sm._last_active):
            sm._last_active[umo] -= 100
        lock = sm.get_lock("grp-a")
        await lock.acquire()  # 模拟工具调用持锁中
        try:
            n = await sm.sweep_idle()
            assert n == 1, "持锁会话应被跳过，只回收空闲会话"
            assert sm.tab_count("grp-a") == 1, "持锁会话不得被回收"
            assert sm.tab_count("grp-b") == 0, "空闲会话照常回收"
        finally:
            lock.release()
        # 锁释放后再 sweep：grp-a 也应被回收。
        n2 = await sm.sweep_idle()
        assert n2 == 1 and sm.tab_count("grp-a") == 0, "锁释放后应回收"

    _run(_go())


def test_sweep_idle_skips_locked_session_without_lock_entry(sm):
    """未创建过锁条目的会话（get_lock 从未调用）不受影响。"""
    async def _go():
        await sm.ensure_page("grp-a")
        sm._last_active["grp-a"] -= 100
        # 不调用 get_lock：_locks 中无条目，等价于锁空闲。
        n = await sm.sweep_idle()
        assert n == 1 and sm.tab_count("grp-a") == 0

    _run(_go())


# ------------------------------------------------------------
# P2 优化：全局锁粒度（v1.2.0）——goto 移出锁外执行
# ------------------------------------------------------------

class _GatedGotoPage(FakePage):
    """goto 进入后挂起，直到 goto_release 放行（模拟慢导航）。"""

    def __init__(self):
        super().__init__()
        self.goto_entered = asyncio.Event()
        self.goto_release = asyncio.Event()

    async def goto(self, url, wait_until="domcontentloaded"):
        self.goto_entered.set()
        await self.goto_release.wait()


class _GatedBrowser(MockBrowser):
    """每个新页面都带独立的 gate（进入/放行事件）。"""

    async def new_page(self):
        p = _GatedGotoPage()
        self.created.append(p)
        return p


async def _wait_until(cond, timeout=3.0):
    """轮询等待条件成立（带超时，避免测试挂死）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise TimeoutError("等待条件超时")
        await asyncio.sleep(0.01)


def test_ensure_page_goto_not_under_global_lock():
    """grp-a 导航挂起期间，grp-b 的 ensure_page 不被全局锁阻塞。

    若 goto 仍在全局锁内，grp-b 的 new_page 必须等 grp-a 放行后才能
    发生；本测试在 grp-a 的 gate 关闭期间断言 grp-b 已成功建页。
    """
    browser = _GatedBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)

    async def _go():
        task_a = asyncio.create_task(sm.ensure_page("grp-a"))
        await _wait_until(lambda: browser.created and browser.created[0].goto_entered.is_set())
        # grp-a 正在 goto（gate 未放行）→ grp-b 应能并行创建页面。
        task_b = asyncio.create_task(sm.ensure_page("grp-b"))
        await _wait_until(lambda: len(browser.created) >= 2)
        assert browser.created[1].goto_entered.is_set(), "grp-b 应已进入 goto"
        # 仅放行 grp-b；grp-a 仍挂起，证明其 goto 未阻塞全局。
        browser.created[1].goto_release.set()
        page_b = await task_b
        assert page_b is not None and sm.tab_count("grp-b") == 1
        assert not browser.created[0].goto_release.is_set(), "grp-a 的 goto 尚未放行"
        browser.created[0].goto_release.set()
        page_a = await task_a
        assert page_a is not None and sm.tab_count("grp-a") == 1

    _run(_go())


def test_new_tab_goto_not_under_global_lock():
    """grp-a 新建标签导航挂起期间，grp-b 的 new_tab 不被全局锁阻塞。"""
    browser = _GatedBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)

    async def _go():
        task_a = asyncio.create_task(sm.new_tab("grp-a", "https://example.com/a"))
        await _wait_until(lambda: browser.created and browser.created[0].goto_entered.is_set())
        task_b = asyncio.create_task(sm.new_tab("grp-b", "https://example.com/b"))
        await _wait_until(lambda: len(browser.created) >= 2)
        browser.created[1].goto_release.set()
        page_b = await task_b
        assert page_b is not None and sm.tab_count("grp-b") == 1
        browser.created[0].goto_release.set()
        page_a = await task_a
        assert page_a is not None and sm.tab_count("grp-a") == 1

    _run(_go())


def test_concurrent_ensure_page_same_session_single_page():
    """并发 ensure_page 同一会话：锁外创建后去重，只保留一个标签。"""
    browser = _GatedBrowser()
    sm = SessionManager(browser, max_pages=4, idle_timeout=60)

    async def _go():
        t1 = asyncio.create_task(sm.ensure_page("grp-a"))
        t2 = asyncio.create_task(sm.ensure_page("grp-a"))
        await _wait_until(lambda: len(browser.created) >= 2)
        for p in browser.created:
            p.goto_release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1 is not None and r2 is not None
        assert r1 is r2, "并发 ensure_page 应复用同一页面"
        assert sm.tab_count("grp-a") == 1, "不得产生重复标签"
        assert len(browser.closed) == 1, "多余页面必须关闭（防泄漏）"

    _run(_go())


def test_capacity_race_closes_surplus_page():
    """容量竞态：额度被占满时，后挂载者关闭自己刚建的页并拒绝。"""
    browser = _GatedBrowser()
    sm = SessionManager(browser, max_pages=1, idle_timeout=60)

    async def _go():
        t1 = asyncio.create_task(sm.ensure_page("grp-a"))
        t2 = asyncio.create_task(sm.ensure_page("grp-b"))
        await _wait_until(lambda: len(browser.created) >= 2)
        for p in browser.created:
            p.goto_release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        ok = [r for r in (r1, r2) if r is not None]
        assert len(ok) == 1, "容量 1 时并发创建只能成功一个"
        assert sm._total_tabs() == 1
        assert len(browser.closed) == 1, "被拒方必须关闭刚建的页（防泄漏）"

    _run(_go())

