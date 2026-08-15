"""core.browser — Playwright 浏览器驱动（BrowserCore）。

职责：浏览器懒加载、页面创建/关闭、截图、崩溃重建。对上层
（session / 主插件）屏蔽 Playwright 细节，全部公共方法在浏览器
失效时可自动重建。

playwright 包不在此模块顶层导入：ensure_browser 首次调用时才
import（单测环境无此依赖亦可加载本模块）；类型标注仅用于
TYPE_CHECKING / 字符串形式。
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查，运行时永不导入
    from playwright.async_api import Browser, Page, Playwright

from .safety import acheck_hostname_internal

logger = logging.getLogger(__name__)

# 浏览器启动默认参数：容器环境普遍需要 --no-sandbox 与
# --disable-dev-shm-usage（/dev/shm 过小导致渲染进程崩溃）。
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

# 关闭超时保护（秒）：playwright 的 close/stop 在渲染进程卡死、页面
# 悬挂等场景下可能永久悬挂；不加超时会导致插件 terminate 卡死，重载后
# 遗留 chromium 进程（v1.2.0 发布后曾实测：browser.close 悬挂 2 小时+，
# playwright driver 与 chromium 进程均未退出）。单步超时阈值。
_BROWSER_CLOSE_TIMEOUT = 5.0
# 单页面 / 单 context 关闭超时（秒）：页面关闭悬挂不应阻塞会话回收链路。
_PAGE_CLOSE_TIMEOUT = 5.0

# SSRF 兜底拦截的 host 判定缓存 TTL（秒）：页面内大量子资源共享同一 host，
# 缓存可避免每个请求都做 DNS 解析。
_SSRF_GUARD_CACHE_TTL = 300.0
# DNS 无法判定（失败）时的短缓存 TTL（秒）。
_SSRF_GUARD_DNS_FAIL_TTL = 30.0

# 支持的浏览器内核（Playwright 规范名）。
_SUPPORTED_BROWSER_TYPES = ("chromium", "firefox", "webkit")


def _safe_float(value, default: float) -> float:
    """float 配置解析：非法值回退默认（不阻断浏览器初始化）。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("配置项数值非法（%r），已回退默认值 %s", value, default)
        return default


def _safe_bool(value, default: bool) -> bool:
    """bool 配置解析：字符串 "false"/"0"/"off"/"no"/"" → False（语义归一）。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "off", "no")
    if value is None:
        return default
    return bool(value)


def validate_browser_type(browser_type: str) -> str:
    """校验浏览器内核配置并返回规范化值；非法值抛 ValueError（含可选值提示）。

    大小写不敏感；"chrome" 归一为 "chromium"（Chrome 与 Chromium 同内核，
    属最常见笔误）。启动前调用，避免 getattr 取到 None 后出现
    "None.launch()" 这类无提示报错（v1.3.1）。
    """
    bt = str(browser_type or "").strip().lower()
    if bt == "chrome":
        bt = "chromium"
    if bt not in _SUPPORTED_BROWSER_TYPES:
        raise ValueError(
            f"不支持的浏览器内核 browser_type={browser_type!r}，"
            f"可选：{'/'.join(_SUPPORTED_BROWSER_TYPES)}"
        )
    return bt


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
        self.browser_type: str = str(cfg.get("browser_type", "chromium"))
        self.proxy: str = str(cfg.get("proxy", "") or "")
        self.viewport: dict = cfg.get("viewport") or {"width": 1280, "height": 800}
        self.timeout: float = _safe_float(cfg.get("timeout", 30), 30)
        self.enable_screenshot: bool = _safe_bool(cfg.get("enable_screenshot", True), True)
        # 是否启用页面级 SSRF 兜底拦截（与 SafetyFilter.block_internal_ip 联动；
        # 关闭内网拦截时也不安装路由拦截，保持行为一致）。
        self.block_internal_ip: bool = _safe_bool(cfg.get("block_internal_ip", True), True)
        data_dir = cfg.get("data_dir") or str(Path(__file__).resolve().parent.parent / "data")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 运行时状态：首次 ensure_browser 才启动。
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # 已创建页面 -> 所属 context 的映射，close_page 时成对清理。
        self._page_contexts: dict = {}
        # SSRF 兜底拦截的 host 判定缓存：host -> (过期时间戳, 是否内网)。
        self._ssrf_host_cache: dict[str, tuple[float, bool]] = {}

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
                "_fix_browsers_path 清除无效 PLAYWRIGHT_BROWSERS_PATH=%s",
                path,
            )
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            logger.debug("_fix_browsers_path 保留有效路径: %s", path)

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

        logger.debug("ensure_browser 启动中")
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        try:
            launch_kwargs: dict = {
                "headless": True,
                "args": list(_LAUNCH_ARGS),
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            # 启动前校验内核配置：非法值给出可选值提示，避免
            # getattr 返回 None 后 None.launch() 的无提示报错（v1.3.1）。
            self.browser_type = validate_browser_type(self.browser_type)
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
        logger.debug("ensure_browser 完成: type=%s proxy=%s",
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
        """关闭浏览器与 playwright（幂等，容错，带超时保护）。

        任一步超时/异常都继续执行后续清理，且无论结果如何都清空内部
        引用：保证调用方（插件 terminate）不会被 close 悬挂卡死，避免
        重载场景遗留 chromium 进程。失败/超时均以 warning 级日志带
        实例标识上报，便于定位是哪个实例未清理干净。
        """
        tag = f"BrowserCore@{id(self):x}"
        browser, playwright = self._browser, self._playwright
        try:
            if browser is not None:
                try:
                    await asyncio.wait_for(
                        browser.close(), timeout=_BROWSER_CLOSE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] browser.close 超时（%.1fs），继续执行清理",
                        tag, _BROWSER_CLOSE_TIMEOUT,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 关闭浏览器失败（继续执行清理）: %s", tag, e)
            if playwright is not None:
                try:
                    await asyncio.wait_for(
                        playwright.stop(), timeout=_BROWSER_CLOSE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] playwright.stop 超时（%.1fs）", tag, _BROWSER_CLOSE_TIMEOUT
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 停止 playwright 失败（忽略）: %s", tag, e)
        finally:
            # 无论成功/失败/超时，引用一律清空：避免半关闭状态残留。
            self._browser = None
            self._playwright = None
            self._page_contexts.clear()
        logger.info("[%s] 浏览器资源已释放", tag)

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
        logger.debug("new_page 创建中")
        context = None
        page = None
        try:
            context = await self._browser.new_context(
                viewport={"width": int(self.viewport.get("width", 1280)),
                          "height": int(self.viewport.get("height", 800))}
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout * 1000)
            # SSRF 兜底：拦截解析到内网/保留地址的页面请求（防 302 重定向
            # 与子资源绕过 acheck_url 的前置校验）。context 级拦截可覆盖
            # target=_blank 弹窗页。mock/降级环境无 route API 时自动跳过。
            if self.block_internal_ip:
                await self._install_ssrf_guard(context)
        except Exception:
            # 创建失败：清理半成品 context/page，避免资源泄漏。
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
            # 创建期间浏览器断开：标记重建后向上抛，调用方捕获。
            if not self._browser_alive():
                self._invalidate()
            raise
        self._page_contexts[id(page)] = context
        logger.debug("new_page 完成")
        return page

    # ------------------------------------------------------------
    # SSRF 兜底拦截（重定向/子资源绕过防护）
    # ------------------------------------------------------------

    async def _install_ssrf_guard(self, context) -> None:
        """在 context 级安装 SSRF 兜底路由拦截。

        acheck_url 只校验 goto 前的初始 URL；页面 302/JS 重定向与子资源
        请求可绕过该前置校验直达内网。此处拦截 context 内全部 http(s)
        请求，目标主机解析到内网/保留地址即 abort。host 判定带缓存，
        避免对页面内大量子资源重复 DNS 解析。
        """
        if not hasattr(context, "route"):
            return  # mock/降级环境无 route API 时跳过

        async def _handler(route) -> None:
            try:
                url = route.request.url or ""
                if not url.lower().startswith(("http://", "https://")):
                    await route.continue_()
                    return
                hostname = urlsplit(url).hostname
                if not hostname:
                    await route.continue_()
                    return
                if await self._ssrf_host_is_internal(hostname):
                    logger.warning(
                        "SSRF 兜底拦截内网请求: %s -> %s", url, hostname
                    )
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()
            except Exception:  # noqa: BLE001 — 拦截器异常时放行，不阻断页面
                try:
                    await route.continue_()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await context.route("**/*", _handler)
        except Exception as e:  # noqa: BLE001 — 安装失败仅告警，不影响浏览
            logger.warning("安装 SSRF 兜底拦截失败: %s", e)

    async def _ssrf_host_is_internal(self, hostname: str) -> bool:
        """主机名是否解析到内网/保留地址（带缓存与 DNS 超时保护）。

        判定结果缓存 _SSRF_GUARD_CACHE_TTL 秒；DNS 无法判定（失败）时按
        放行处理并短缓存（第二道防线，宁可放行也不误伤正常页面）。
        """
        now = time.monotonic()
        cached = self._ssrf_host_cache.get(hostname)
        if cached is not None and cached[0] > now:
            return cached[1]
        verdict = await acheck_hostname_internal(hostname)
        ttl = (
            _SSRF_GUARD_CACHE_TTL
            if verdict is not None
            else _SSRF_GUARD_DNS_FAIL_TTL
        )
        self._ssrf_host_cache[hostname] = (now + ttl, bool(verdict))
        return bool(verdict)

    async def close_page(self, page) -> None:
        """关闭页面及其 context（容错，带超时保护）。

        任一步超时/异常都继续执行后续清理；页面/context 关闭悬挂不会
        阻塞会话回收与插件终止链路。失败/超时以 warning 级日志上报。
        """
        if page is None:
            return
        tag = f"BrowserCore@{id(self):x}"
        ctx = self._page_contexts.pop(id(page), None)
        try:
            await asyncio.wait_for(page.close(), timeout=_PAGE_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[%s] 关闭页面超时（%.1fs，忽略）", tag, _PAGE_CLOSE_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 关闭页面失败（忽略）: %s", tag, e)
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.close(), timeout=_PAGE_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[%s] 关闭 context 超时（%.1fs，忽略）", tag, _PAGE_CLOSE_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] 关闭 context 失败（忽略）: %s", tag, e)

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
