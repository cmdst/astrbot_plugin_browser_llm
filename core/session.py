"""core.session — 浏览会话管理（SessionManager）。

职责：UMO（unified_msg_origin）→ 标签页列表映射，多会话互不串页；
每会话支持多标签页（new_tab / switch_tab / tab_count，用户视角
标签编号从 1 开始）；max_pages 总标签数上限；idle_timeout 空闲回收；
每会话锁串行化同会话的并发工具调用。

设计说明：_pages[umo] 为该会话的标签页列表，_active[umo] 为当前
激活标签的 0 起始索引；ensure_page 返回激活页（无会话时新建第一个标签，
落在 default_url = ``about:blank``）。v1.7.5 起工具链走
:meth:`claim_page`：**按任务**取页 —— 同一任务复用，换任务先关旧任务页
（跨任务复用会带上上一任务的表单/滚动/登录态，且 mark/ref 纪元是按任务
隔离的，页面也应隔离）。

playwright 对象以鸭子类型传递（mock 可替换），本模块不 import
playwright。
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查
    from playwright.async_api import Page

    from core.browser import BrowserCore

logger = logging.getLogger(__name__)

# 后台回收任务周期（秒）。
_SWEEP_INTERVAL = 60.0


class SessionManager:
    """浏览会话管理器：会话隔离、多标签、并发串行化与空闲回收。"""

    def __init__(
        self,
        browser: "BrowserCore",
        max_pages: int = 5,
        idle_timeout: float = 1800,
        default_url: str = "about:blank",
        on_page_closed: "Callable[[Any], None] | None" = None,
    ) -> None:
        """初始化会话管理器。

        Args:
            browser: BrowserCore 实例，负责实际页面创建与关闭。
            max_pages: 全部会话的标签页总数上限；达到后新标签/
                新会话被拒绝。
            idle_timeout: 会话空闲回收阈值（秒）。
            default_url: 新会话首个标签的初始页地址（v1.7.5 恒为
                ``about:blank``：不再有"起始页"配置，第一个动作自己 navigate）。
            on_page_closed: 页面关闭后的回调（插件用它把感知层的 CDP 会话
                （``a11y.CdpSessionPool``）与执行层的会话缓存（``executor.drop_cdp``）
                一起释放 —— 两者都以 ``id(page)`` 为键，不摘就是泄漏）。
        """
        self.browser = browser
        self.max_pages = int(max_pages)
        self.idle_timeout = float(idle_timeout)
        self.default_url = default_url
        self.on_page_closed = on_page_closed

        # umo -> 标签页列表；_active 为当前激活标签的 0 起始索引。
        self._pages: dict[str, list[Page]] = {}
        self._active: dict[str, int] = {}
        # 最后活动时间（time.monotonic 时钟）。
        self._last_active: dict[str, float] = {}

        # 每会话的"当前任务键"（claim_page 用：换任务 ⇒ 关旧任务页）。
        self._task_of: dict[str, str] = {}

        # 每会话专用锁：串行化同一会话的并发工具调用。
        self._locks: dict[str, asyncio.Lock] = {}
        # 全局锁：保护新建标签与容量检查的原子性，避免并发创建超限。
        self._global_lock = asyncio.Lock()

        # 后台回收任务。
        self._sweeper_task: asyncio.Task | None = None

    # ------------------------------------------------------------
    # 会话锁
    # ------------------------------------------------------------

    def get_lock(self, umo: str) -> asyncio.Lock:
        """返回该会话的专用锁（不存在则创建）。

        锁为会话级：同一会话内无论哪个标签的操作都被串行化。
        """
        lock = self._locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[umo] = lock
        return lock

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------

    def _total_tabs(self) -> int:
        """全部会话的标签页总数（容量上限按此计算）。"""
        return sum(len(pages) for pages in self._pages.values())

    @staticmethod
    def _page_alive(page) -> bool:
        """Page 是否仍可用（关闭/崩溃后页面方法会抛异常）。"""
        try:
            return not page.is_closed()
        except Exception:  # noqa: BLE001 — 探测异常一律视为失效
            return False

    def _touch(self, umo: str) -> None:
        """更新会话最后活动时间（time.monotonic）。"""
        self._last_active[umo] = time.monotonic()

    async def _close_page(self, umo: str, page) -> None:
        """关闭单个标签页（容错，供各清理路径复用）。"""
        try:
            await self.browser.close_page(page)
        except Exception as e:  # noqa: BLE001
            logger.debug("关闭标签页失败（忽略） umo=%s: %s", umo, e)
        finally:
            # 无论关成功与否都摘掉按 id(page) 建的会话缓存（幂等、只做字典操作）。
            if self.on_page_closed is not None:
                try:
                    self.on_page_closed(page)
                except Exception as e:  # noqa: BLE001 — 回调失败不许影响关页
                    logger.debug("关页回调失败（忽略） umo=%s: %s", umo, e)

    def _drop_tab(self, umo: str, index: int) -> None:
        """从会话标签列表中移除 index（0 起始），不关闭页面。

        激活索引修正规则：
        - 关闭的是激活标签：优先激活前一个，其次后一个（顶上来）；
        - 关闭的标签在激活标签之前：激活索引前移，保持指向同一页面；
        - 列表因此变空时清理该会话的全部映射。
        """
        pages = self._pages.get(umo)
        if not pages:
            return
        if 0 <= index < len(pages):
            pages.pop(index)
        active = self._active.get(umo, 0)
        if index < active:
            # 关闭的标签在激活标签之前：激活索引前移，仍指向同一页面。
            active -= 1
        elif index == active:
            # 关闭的是激活标签：优先前一个，其次后一个（原位置顶上来）。
            if active >= 1:
                active -= 1
            # active == 0 时保持 0：后一个标签顶到原位置。
        # 越界保护（如关闭最后一个标签后无后一个可顶）。
        if active >= len(pages):
            active = max(0, len(pages) - 1)
        if not pages:
            self._pages.pop(umo, None)
            self._active.pop(umo, None)
            self._last_active.pop(umo, None)
        else:
            self._active[umo] = active

    async def _new_page_goto(self, url: str) -> Optional["Page"]:
        """创建新标签页并跳转（失败返回 None，不抛出）。

        注意：new_page 成功但 goto 失败时，必须关闭该页面（含其 context），
        否则每次失败导航都会泄漏一个浏览器页面/上下文。
        """
        page = None
        try:
            page = await self.browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            return page
        except Exception as e:  # noqa: BLE001 — 创建失败不抛出
            logger.warning("创建标签页失败: %s", e)
            if page is not None:
                try:
                    await self.browser.close_page(page)
                except Exception:  # noqa: BLE001 — 关闭失败忽略
                    pass
            return None

    # ------------------------------------------------------------
    # 页面获取 / 标签管理
    # ------------------------------------------------------------

    async def _close_all_pages(self, umo: str) -> None:
        """关闭该会话全部标签页（**不动**会话锁与任务键，供 claim_page 复用）。"""
        pages = self._pages.pop(umo, [])
        self._active.pop(umo, None)
        self._last_active.pop(umo, None)
        for page in pages:
            await self._close_page(umo, page)

    def current_page(self, umo: str) -> Optional["Page"]:
        """该会话当前的激活页（**不创建**、不导航；没有/已关闭返回 None）。

        用途：无起始页之后，"这次任务需不需要显式网址"取决于会话里有没有一个
        仍然活着的页面 —— 有就能"在这个页面上继续操作"，没有就必须问网址。
        """
        pages = self._pages.get(umo) or []
        index = self._active.get(umo, 0)
        page = pages[index] if 0 <= index < len(pages) else None
        return page if page is not None and self._page_alive(page) else None

    async def claim_page(self, umo: str, *, task_key: str = "") -> Optional["Page"]:
        """按**任务**取该会话的页面（v1.7.5 的唯一取页入口）。

        语义：``task_key`` 变了 ⇒ 先把上一任务的标签页全部关掉再新建；同一个
        ``task_key``（同一任务内的多步动作/观测）⇒ 复用当前激活页。

        为什么按任务隔离：跨任务复用同一标签会把上一任务的表单值、滚动位置、
        甚至登录态带进新任务（上下文污染）；而且 mark 编号与 ref 编号都是**按
        任务**作废的，页面若不隔离，"新任务的页面"与"旧任务的观测"会错位。

        锁纪律：本方法**不**持有会话锁（调用方在 ``async with lock`` 内调用），
        因此清理路径不使用 ``release()``（它会 pop 掉 ``_locks``，让并发调用者
        拿到另一把锁 ⇒ 会话串行化失效）。
        """
        key = str(task_key or "")
        if key and self._task_of.get(umo, "") != key:
            await self._close_all_pages(umo)
            self._task_of[umo] = key
        return await self.ensure_page(umo)

    async def ensure_page(self, umo: str) -> Optional["Page"]:
        """获取该会话当前激活的 Page；无会话则新建第一个标签。

        兼容 T4 单页用法：同一 umo 重复调用返回同一（激活）页；
        激活页失效时尝试重建该标签。

        锁粒度（v1.2.0）：_global_lock 仅在容量检查与页面挂载期间持有，
        页面创建与 goto（慢操作）在锁外执行，避免某个会话的慢导航
        串行阻塞全部会话的标签操作；锁外创建期间并发产生的重复页/超额
        页在挂载阶段去重或关闭，不泄漏页面。

        Args:
            umo: 会话标识（unified_msg_origin）。

        Returns:
            激活页；总标签数达 max_pages 或创建失败时返回 None。
        """
        logger.debug(f"ensure_page 进入 umo={umo!r}")
        # 阶段一（全局锁内）：会话复用判断、失效页移除与容量预检。
        stale_page = None
        capacity_ok = True
        async with self._global_lock:
            pages = self._pages.get(umo)
            if pages:
                active = self._active.get(umo, 0)
                page = pages[active] if active < len(pages) else None
                if page is not None and self._page_alive(page):
                    self._touch(umo)
                    logger.debug("ensure_page 复用 page")
                    return page
                # 激活页失效：先移除映射（容量即时释放），关闭动作移到锁外。
                if page is not None:
                    self._drop_tab(umo, active)
                    stale_page = page

            # 总标签数达上限 → 拒绝新标签。
            if self._total_tabs() >= self.max_pages:
                logger.warning(
                    "标签总数达上限 %d，拒绝新建（umo=%s）", self.max_pages, umo
                )
                capacity_ok = False

        # 锁外：失效页无论能否新建都必须关闭（防 context 泄漏）。
        if stale_page is not None:
            await self._close_page(umo, stale_page)
        if not capacity_ok:
            return None

        # 锁外：创建并导航新页（goto 不占全局锁）。
        page = await self._new_page_goto(self.default_url)
        if page is None:
            return None

        # 阶段二（全局锁内）：并发复用复检 + 容量复检 + 挂载。
        async with self._global_lock:
            pages = self._pages.get(umo)
            if pages:
                active = self._active.get(umo, 0)
                existing = pages[active] if active < len(pages) else None
                if existing is not None and self._page_alive(existing):
                    # 锁外创建期间本会话已被其他协程建好可用页：复用，丢弃新页。
                    await self._close_page(umo, page)
                    logger.debug("ensure_page 复用 page（并发创建去重）")
                    return existing
                if existing is not None:
                    # 并发期间激活页再次失效：移除并关闭后走新建路径。
                    self._drop_tab(umo, active)
                    await self._close_page(umo, existing)
            if self._total_tabs() >= self.max_pages:
                # 锁外等待期间额度被其他会话占满：关闭刚建的页，拒绝。
                await self._close_page(umo, page)
                logger.warning(
                    "标签总数达上限 %d，拒绝新建（umo=%s）", self.max_pages, umo
                )
                return None
            self._pages.setdefault(umo, []).append(page)
            self._active[umo] = len(self._pages[umo]) - 1
            self._touch(umo)
            logger.info(
                "新建浏览会话标签 umo=%s（标签 %d/%d）",
                umo,
                self._total_tabs(),
                self.max_pages,
            )
            logger.debug("ensure_page 返回")
            return page

    async def new_tab(self, umo: str, url: str) -> Optional["Page"]:
        """为该会话新建标签页并激活；无会话时先建会话。

        新建后自动激活新标签。总标签数达 max_pages 时返回 None。

        锁粒度（v1.2.0）：容量检查与页面挂载在 _global_lock 内完成，
        页面创建与 goto（慢操作）在锁外执行，避免串行阻塞全部会话；
        挂载前复检容量，超额时关闭刚建的页并拒绝（不泄漏）。

        Args:
            umo: 会话标识。
            url: 新标签要打开的地址。

        Returns:
            新标签页；超限或创建失败返回 None。
        """
        # 阶段一（全局锁内）：容量预检（无会话时同样受全局额度约束）。
        async with self._global_lock:
            if self._total_tabs() >= self.max_pages:
                logger.warning(
                    "标签总数达上限 %d，拒绝新标签（umo=%s）", self.max_pages, umo
                )
                return None

        # 锁外：创建并导航新页（goto 不占全局锁）。
        page = await self._new_page_goto(url)
        if page is None:
            return None

        # 阶段二（全局锁内）：容量复检 + 挂载（无会话时以 url 为首标签）。
        async with self._global_lock:
            if self._total_tabs() >= self.max_pages:
                # 锁外等待期间额度被其他会话占满：关闭刚建的页，拒绝。
                await self._close_page(umo, page)
                logger.warning(
                    "标签总数达上限 %d，拒绝新标签（umo=%s）", self.max_pages, umo
                )
                return None
            pages = self._pages.setdefault(umo, [])
            pages.append(page)
            self._active[umo] = len(pages) - 1
            self._touch(umo)
            logger.info(
                "新建标签 umo=%s（标签 %d/%d）", umo, self._total_tabs(), self.max_pages
            )
            return page

    async def switch_tab(self, umo: str, index: int) -> Optional["Page"]:
        """切换到该会话第 index 个标签（用户视角 1 起始）并激活。

        Args:
            umo: 会话标识。
            index: 标签编号，从 1 开始。

        Returns:
            切换后的激活页；会话不存在或 index 越界返回 None。
        """
        async with self._global_lock:
            pages = self._pages.get(umo)
            if not pages:
                return None
            idx = int(index) - 1
            if idx < 0 or idx >= len(pages):
                return None
            page = pages[idx]
            if not self._page_alive(page):
                await self._close_page(umo, page)
                self._drop_tab(umo, idx)
                return None
            self._active[umo] = idx
            self._touch(umo)
            return page

    def tab_count(self, umo: str) -> int:
        """返回该会话的标签页数量（无会话返回 0）。"""
        return len(self._pages.get(umo, []))

    async def close_tab(self, umo: str, index: int) -> bool:
        """关闭该会话第 index 个标签（用户视角 1 起始）。

        关闭的是激活标签时，自动激活相邻标签（同索引位或最后一个）；
        全部标签关闭后清理会话映射。

        Args:
            umo: 会话标识。
            index: 标签编号，从 1 开始。

        Returns:
            bool: 关闭成功返回 True；会话不存在或 index 越界返回 False。
        """
        async with self._global_lock:
            pages = self._pages.get(umo)
            if not pages:
                return False
            idx = int(index) - 1
            if idx < 0 or idx >= len(pages):
                return False
            page = pages[idx]
            await self._close_page(umo, page)
            self._drop_tab(umo, idx)
            # 仅当会话仍有标签时更新活动时间；标签全关后 _drop_tab
            # 已清空映射，不能再 _touch 把 _last_active 写回。
            if umo in self._pages:
                self._touch(umo)
            return True

    # ------------------------------------------------------------
    # 释放 / 回收
    # ------------------------------------------------------------

    async def release(self, umo: str) -> None:
        """关闭该会话全部标签页并清理映射（容错）。

        Args:
            umo: 会话标识。
        """
        pages = self._pages.pop(umo, [])
        self._active.pop(umo, None)
        self._last_active.pop(umo, None)
        self._task_of.pop(umo, None)
        self._locks.pop(umo, None)  # 会话锁随会话销毁
        for page in pages:
            await self._close_page(umo, page)
        logger.info("已释放浏览会话 umo=%s（关闭 %d 个标签）", umo, len(pages))

    async def sweep_idle(self) -> int:
        """回收超过 idle_timeout 未活动的会话，返回回收数。

        竞态防护（v1.2.0）：会话锁被占用（正在执行工具调用）时跳过
        该会话，避免关闭使用中的页面；锁释放后下一轮再尝试回收。
        """
        if not self._last_active:
            return 0
        now = time.monotonic()
        expired = [
            umo for umo, ts in self._last_active.items() if now - ts > self.idle_timeout
        ]
        reclaimed_umos = []
        for umo in expired:
            lock = self._locks.get(umo)
            if lock is not None and lock.locked():
                # 会话正在执行工具调用：跳过本轮，避免回收使用中的页面。
                logger.debug("会话 %s 正在使用中，跳过本轮空闲回收", umo)
                continue
            await self.release(umo)
            reclaimed_umos.append(umo)
        if reclaimed_umos:
            logger.info("空闲回收 %d 个会话: %s", len(reclaimed_umos), reclaimed_umos)
        return len(reclaimed_umos)

    # ------------------------------------------------------------
    # 后台回收任务
    # ------------------------------------------------------------

    async def start_sweeper(self) -> None:
        """启动后台回收任务：每 60s 执行一次 sweep_idle。"""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(_SWEEP_INTERVAL)
                    await self.sweep_idle()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — 单轮失败不退出循环
                    logger.warning("空闲回收任务异常（继续下一轮）: %s", e)

        self._sweeper_task = asyncio.create_task(
            _loop(), name="browser-session-sweeper"
        )
        logger.info("会话空闲回收任务已启动（周期 %.0fs）", _SWEEP_INTERVAL)

    async def stop_sweeper(self) -> None:
        """停止后台回收任务（幂等）。"""
        task = self._sweeper_task
        if task is None:
            return
        self._sweeper_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug("停止回收任务异常（忽略）: %s", e)
        logger.info("会话空闲回收任务已停止")

    # ------------------------------------------------------------
    # 总清理
    # ------------------------------------------------------------

    async def shutdown(self) -> None:
        """停止回收任务并关闭全部会话标签页（幂等，容错）。"""
        await self.stop_sweeper()
        umos = list(self._pages.keys())
        for umo in umos:
            await self.release(umo)
        logger.info("会话管理器已清理（剩余 %d 个会话）", len(self._pages))
