"""core.browser — Playwright 浏览器驱动（BrowserCore）。

职责：浏览器懒加载、页面创建/关闭、截图、崩溃重建。对上层
（session / 主插件）屏蔽 Playwright 细节，全部公共方法在浏览器
失效时可自动重建。

playwright 包不在此模块顶层导入：ensure_browser 首次调用时才
import（单测环境无此依赖亦可加载本模块）；类型标注仅用于
TYPE_CHECKING / 字符串形式。
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查，运行时永不导入
    from playwright.async_api import Browser, Page, Playwright

logger = logging.getLogger(__name__)

# 浏览器启动默认参数：容器环境普遍需要 --no-sandbox 与
# --disable-dev-shm-usage（/dev/shm 过小导致渲染进程崩溃）。
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


class BrowserCore:
    """Playwright 浏览器驱动。

    Attributes:
        browser_type: 浏览器引擎（chromium/firefox/webkit）。
        proxy: 代理地址，空串表示直连。
        viewport: 视口 dict，如 {"width": 1280, "height": 800}。
        timeout: 页面操作超时（秒）。
        enable_screenshot: 是否允许截图。
        data_dir: 截图保存目录（Path）。
    """

    def __init__(self, config: dict) -> None:
        """初始化浏览器驱动（仅存配置，浏览器懒加载）。

        Args:
            config: 插件配置 dict，读取 browser_type / proxy /
                viewport / timeout / enable_screenshot / data_dir。
        """
        cfg = config or {}
        self.browser_type: str = cfg.get("browser_type", "chromium")
        self.proxy: str = cfg.get("proxy", "") or ""
        self.viewport: dict = cfg.get("viewport") or {"width": 1280, "height": 800}
        self.timeout: float = float(cfg.get("timeout", 30))
        self.enable_screenshot: bool = bool(cfg.get("enable_screenshot", True))
        data_dir = cfg.get("data_dir") or str(Path(__file__).resolve().parent.parent / "data")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 运行时状态：首次 ensure_browser 才启动。
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # 已创建页面 -> 所属 context 的映射，close_page 时成对清理。
        self._page_contexts: dict = {}

    # ------------------------------------------------------------
    # 生命周期：启动 / 存活检查 / 关闭
    # ------------------------------------------------------------

    @staticmethod
    def _fix_browsers_path() -> None:
        """修正 PLAYWRIGHT_BROWSERS_PATH 环境变量（污染防御）。

        老插件 astrbot_plugin_browser 会在全局设置该变量指向一个空的
        浏览器目录，导致 Playwright 从错误路径找内核而 launch 失败。
        此处检测：若变量存在且指向的目录不存在（或为空），则清除该
        变量，让 Playwright 回退到默认浏览器路径。
        """
        path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if not path:
            return
        p = Path(path)
        if not p.exists() or not any(p.iterdir()):
            logger.warning(
                "[DIAG] _fix_browsers_path 清除无效 PLAYWRIGHT_BROWSERS_PATH=%s",
                path,
            )
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            logger.info("[DIAG] _fix_browsers_path 保留有效路径: %s", path)

    async def ensure_browser(self) -> None:
        """懒加载启动浏览器；已启动则直接返回。

        首次调用才 import playwright 并启动浏览器；启动失败抛出
        异常（由调用方决定是否降级）。浏览器曾崩溃断开时重新启动。
        """
        if self._browser_alive():
            return
        # 修正浏览器路径污染必须在 import playwright 之前（playwright
        # 在 import 时读取该环境变量决定浏览器内核查找目录）。
        self._fix_browsers_path()
        # 延迟导入：单测环境无 playwright 时本模块仍可加载。
        from playwright.async_api import async_playwright  # noqa: PLC0415

        logger.info("[DIAG] ensure_browser 启动中")
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        try:
            launch_kwargs: dict = {
                "headless": True,
                "args": list(_LAUNCH_ARGS),
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            engine = getattr(self._playwright, self.browser_type)
            self._browser = await engine.launch(**launch_kwargs)
        except Exception:
            # 启动失败：清理 playwright，避免残留半启动状态。
            self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None
            raise
        logger.info("[DIAG] ensure_browser 完成: type=%s proxy=%s",
                    self.browser_type, self.proxy or "(直连)")

    def _browser_alive(self) -> bool:
        """浏览器是否存活且已连接（崩溃/手动关闭后返回 False）。"""
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:  # noqa: BLE001 — 探测异常一律视为不存活
            return False

    def _invalidate(self) -> None:
        """标记浏览器失效：清空引用，下次 ensure_browser 重新启动。"""
        self._browser = None
        self._page_contexts.clear()

    async def shutdown(self) -> None:
        """关闭浏览器与 playwright（幂等，容错）。"""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("关闭浏览器失败（忽略）: %s", e)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("停止 playwright 失败（忽略）: %s", e)
        self._browser = None
        self._playwright = None
        self._page_contexts.clear()
        logger.info("浏览器资源已释放")

    # ------------------------------------------------------------
    # 页面管理
    # ------------------------------------------------------------

    async def new_page(self) -> "Page":
        """创建新页面：确保浏览器启动，新建 context + page。

        Returns:
            Playwright Page（已设置视口与默认超时）。
        """
        await self.ensure_browser()
        if self._browser is None:
            raise RuntimeError("浏览器启动失败")
        logger.info("[DIAG] new_page 创建中")
        try:
            context = await self._browser.new_context(
                viewport={"width": int(self.viewport.get("width", 1280)),
                          "height": int(self.viewport.get("height", 800))}
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout * 1000)
        except Exception:
            # 创建期间浏览器断开：标记重建后向上抛，调用方捕获。
            if not self._browser_alive():
                self._invalidate()
            raise
        self._page_contexts[id(page)] = context
        logger.info("[DIAG] new_page 完成")
        return page

    async def close_page(self, page) -> None:
        """关闭页面及其 context（容错忽略异常）。"""
        if page is None:
            return
        ctx = self._page_contexts.pop(id(page), None)
        try:
            await page.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("关闭页面失败（忽略）: %s", e)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("关闭 context 失败（忽略）: %s", e)

    # ------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------

    async def screenshot(self, page, save_path: str) -> str:
        """截取页面截图并保存到 save_path。

        Args:
            page: 目标页面。
            save_path: 截图保存路径。

        Returns:
            str: 截图保存路径；截图失败（或未启用截图）返回空串。
        """
        if not self.enable_screenshot:
            return ""
        try:
            await page.screenshot(path=save_path, full_page=False)
            return save_path
        except Exception as e:  # noqa: BLE001 — 截图失败不应拖垮工具调用
            logger.warning("截图失败: %s", e)
            if not self._browser_alive():
                self._invalidate()
            return ""
