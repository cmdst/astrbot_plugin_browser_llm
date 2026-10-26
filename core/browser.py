"""core.browser — Playwright 浏览器驱动（BrowserCore）。

职责：浏览器懒加载、页面创建/关闭、截图、崩溃重建。对上层
（session / 主插件）屏蔽 Playwright 细节，全部公共方法在浏览器
失效时可自动重建。

v1.7.5 新增**显示固化**（规格 §10.1，感知层的前提：同一页面在不同无头形态、
不同视口 / DPR / locale 下会渲染出不同的可访问性树）：

- **无头模式三档** ``resources.headless``：``new``（默认，新版无头 ——
  ``chromium.launch(channel="chromium", headless=True)``）/ ``shell``
  （旧 headless shell）/ ``headful``（需 ``DISPLAY``，缺失时 **WARN + 回落 new**，
  绝不静默失败）。firefox/webkit 没有 ``channel`` 参数，只切 ``headless``。
- **视口 / DPR / locale 固化**：``resources.viewport`` 的
  ``{width, height, device_scale_factor}`` 同时喂 ``viewport`` 与 ``screen``，
  locale 恒为 :data:`_CONTEXT_LOCALE`（``zh-CN``）—— ``<html lang>`` 与 ``Intl``
  的默认行为随之固定，A11y 树里的中文标签才稳定。
- **字体自检**：``ensure_browser`` 后跑一次 ``fc-list``（一次，结果缓存）；
  找不到 CJK 字体 ⇒ **WARN 一行**并给包名 ``fonts-noto-cjk``，**不尝试安装**
  （容器里装包不是插件的事）。同时给新 context 注入 CJK 字体栈兜底
  （防"豆腐块"：缺字时渲染成方框，AX 树里的 name 会退化）。
- **SSRF 计数**（D9-①）：``route.abort("blockedbyclient")`` 时按 context 计数，
  由 :meth:`BrowserCore.ssrf_blocked` 提供给 ``PageSnapshot.ssrf_blocked``。

playwright 包不在此模块顶层导入：ensure_browser 首次调用时才
import（单测环境无此依赖亦可加载本模块）；类型标注仅用于
TYPE_CHECKING / 字符串形式。
"""

import asyncio
import logging
import os
import subprocess  # noqa: S404 — 一次性 fc-list 探测（见 probe_cjk_fonts）
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查，运行时永不导入
    from playwright.async_api import Browser, Page, Playwright

from . import as_bool, as_float
from .config_map import ConfigView
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

# ---------------------------------------------------------------------------
# 显示固化（v1.7.5 规格 §10.1）：无头形态 / 视口 / DPR / locale / 字体栈
# ---------------------------------------------------------------------------

#: 三档无头模式（``resources.headless``）。**默认 ``new``**：新版无头在字体、
#: canvas/WebGL 与指纹上与旧 headless shell 差异很大，是 A11y 树稳定的前提。
HEADLESS_MODES: tuple[str, ...] = ("new", "shell", "headful")

#: 兼容别名（面板手填 / 老文档写法）-> 规范值。
_HEADLESS_ALIASES: dict[str, str] = {
    "": "new",
    "new": "new",
    "new-headless": "new",
    "new_headless": "new",
    "新版": "new",
    "shell": "shell",
    "old": "shell",
    "headless-shell": "shell",
    "headless_shell": "shell",
    "headless": "shell",
    "旧版": "shell",
    "headful": "headful",
    "headed": "headful",
    "head": "headful",
    "有头": "headful",
}

#: context 级固定的 locale（**常量，不是配置键**）：影响 ``<html lang>`` 相关
#: 默认渲染与 ``Intl``；中文站点的 AX name 才会是稳定中文。
_CONTEXT_LOCALE = "zh-CN"

#: 中文字体栈（新 context 注入的兜底；SoM 标签也用同一份，避免标签变豆腐块）。
#: 顺序 = 从"最像无衬线中文"到系统兜底；容器里通常只有前两项之一被满足。
_CJK_FONT_STACK = (
    '"Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", '
    '"Source Han Sans CN", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", '
    '"Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", sans-serif'
)

#: ``fc-list`` 输出里出现任一 needle 即认为系统有 CJK 字体。
_CJK_FONT_NEEDLES: tuple[str, ...] = (
    "Noto Sans CJK",
    "Noto Serif CJK",
    "Noto Sans SC",
    "Source Han",
    "WenQuanYi",
)

#: 缺字体时给运营方的包名（WARN 里出现；**不自动安装**）。
_CJK_FONT_PACKAGES: tuple[str, ...] = ("fonts-noto-cjk", "fonts-wqy-microhei")

#: 仅在 chromium 上追加的启动参数（减少首启动噪声与后台联网）。
_CHROMIUM_EXTRA_ARGS: tuple[str, ...] = (
    "--disable-features=Translate",
    "--no-first-run",
    "--disable-background-networking",
)

#: 无头形态的选择说明日志前缀（验收判据：启动日志含且仅含一行）。
_HEADLESS_LOG_PREFIX = "无头模式 headless="

#: ``fc-list`` 探测超时（秒）：探测失败一律按"有字体"处理（不制造假告警）。
_FC_LIST_TIMEOUT = 5.0

#: 探测缓存（进程级；``fc-list`` 每进程只跑一次）。
_CJK_FONT_CACHE: dict[str, object] = {}


def validate_headless_mode(mode: str | None) -> str:
    """校验 ``resources.headless`` 并返回规范值；非法值抛 ValueError（含可选值）。

    与 :func:`validate_browser_type` 同一形态：非法配置在**启动前**就报错，
    而不是让 ``launch`` 抛一句 Playwright 内部错误。
    """
    key = str(mode or "").strip().lower()
    if key in _HEADLESS_ALIASES:
        return _HEADLESS_ALIASES[key]
    raise ValueError(
        f"不支持的无头模式 headless={mode!r}，可选：{'/'.join(HEADLESS_MODES)}"
    )


@dataclass(frozen=True)
class LaunchPlan:
    """``browser.launch`` 的**纯装配结果**（可单测，不碰 Playwright）。

    Attributes:
        mode: 实际生效的模式（``headful`` 缺 ``DISPLAY`` 时会被回落成 ``new``）。
        requested: 配置里写的模式（回落前）。
        headless: 传给 ``launch`` 的 ``headless``。
        channel: 传给 ``launch`` 的 ``channel``（``None`` = 不传）。
        args: 启动参数（含容器必需的 ``--no-sandbox`` 等）。
        notes: 人读说明（回落原因等），写进启动日志。
    """

    mode: str
    requested: str
    headless: bool
    channel: str | None
    args: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def launch_kwargs(self, *, proxy: str = "") -> dict:
        """直接喂给 ``engine.launch(**kwargs)``（代理可选）。"""
        kwargs: dict = {"headless": bool(self.headless), "args": list(self.args)}
        if self.channel:
            kwargs["channel"] = self.channel
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        return kwargs


def resolve_launch_plan(
    mode: str | None = "new",
    *,
    engine: str = "chromium",
    env: Mapping[str, str] | None = None,
    extra_args: tuple[str, ...] = (),
) -> LaunchPlan:
    """把 ``resources.headless`` 解析成 ``launch`` 参数（**纯函数**）。

    规则（规格 §10.1）：

    - ``new`` ⇒ ``headless=True`` + ``channel="chromium"``（仅 chromium：新版无头
      是 chromium 的 channel 构建，不是 headless shell）；
    - ``shell`` ⇒ ``headless=True`` + **不传** ``channel``（Playwright 默认的
      headless shell）；
    - ``headful`` ⇒ ``headless=False``；**缺 ``DISPLAY``/``WAYLAND_DISPLAY`` 时
      WARN 并回落 ``new``**（headful 在容器里没显示服务必然启动失败，
      静默失败比回落更糟）；
    - firefox/webkit 没有 ``channel`` 参数 ⇒ 只切 ``headless``。
    """
    requested = validate_headless_mode(mode)
    engine_name = str(engine or "chromium").strip().lower()
    environ = os.environ if env is None else env
    notes: list[str] = []
    effective = requested

    if requested == "headful":
        has_display = bool(
            environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")
        )
        if not has_display:
            effective = "new"
            notes.append("headful 需要 DISPLAY/WAYLAND_DISPLAY，未检测到 ⇒ 回落 new")

    args = list(_LAUNCH_ARGS)
    if engine_name == "chromium":
        args.extend(_CHROMIUM_EXTRA_ARGS)
        channel: str | None = "chromium" if effective in ("new", "headful") else None
    else:
        channel = None  # firefox/webkit 不接受 channel

    headless = effective != "headful"
    return LaunchPlan(
        mode=effective,
        requested=requested,
        headless=headless,
        channel=channel,
        args=tuple(dict.fromkeys(args)),  # 去重且保序
        notes=tuple(notes),
    )


def context_options(
    *,
    width: int = 1280,
    height: int = 800,
    device_scale_factor: float = 1.0,
    locale: str = _CONTEXT_LOCALE,
    extra: Mapping[str, object] | None = None,
) -> dict:
    """``browser.new_context`` 的**纯装配结果**（视口 / DPR / locale 一次定死）。

    ``screen`` 与 ``viewport`` 用同一对宽高：无头下二者一致，headful 下
    ``screen`` 决定窗口外框，写同一值可避免"截图宽度与视口不一致"。
    """
    w = max(1, int(width or 1280))
    h = max(1, int(height or 800))
    try:
        dpr = float(device_scale_factor or 1.0)
    except (TypeError, ValueError):
        dpr = 1.0
    dpr = max(0.5, min(3.0, dpr))
    options = {
        "viewport": {"width": w, "height": h},
        "screen": {"width": w, "height": h},
        "device_scale_factor": dpr,
        "locale": str(locale or _CONTEXT_LOCALE),
    }
    if extra:
        options.update(dict(extra))
    return options


def font_stack() -> str:
    """中文字体栈（CSS ``font-family`` 值；SoM 标签与页面兜底共用一份）。"""
    return _CJK_FONT_STACK


def font_css() -> str:
    """CJK 字体兜底 CSS（只对**未显式指定** font-family 的元素生效）。

    刻意不用 ``* { font-family: … !important }``：那会覆盖站点的图标字体
    （Private Use Area 字符会变成方框），反而让 AX name 变坏。
    """
    return (
        ":root{--blm-cjk-font:" + _CJK_FONT_STACK + "}"
        "html,body{font-family:var(--blm-cjk-font)}"
    )


def font_init_script() -> str:
    """在新 context 注入的字体兜底脚本（**零参**、幂等、不改页面语义）。

    通过 ``context.add_init_script`` 在每次导航的最早阶段插入一个
    ``<style>``；已存在同名 style 则跳过（幂等）。
    """
    css = font_css()
    return (
        "(() => {\n"
        "  const ID = '__blm_font_fallback__';\n"
        "  const install = () => {\n"
        "    if (document.getElementById(ID)) return;\n"
        "    const style = document.createElement('style');\n"
        "    style.id = ID;\n"
        f"    style.textContent = {css!r};\n"
        "    (document.head || document.documentElement).appendChild(style);\n"
        "  };\n"
        "  if (document.head || document.documentElement) install();\n"
        "  else document.addEventListener('DOMContentLoaded', install, {once: true});\n"
        "})();\n"
    )


def check_cjk_fonts(fc_list_output: str) -> tuple[bool, str]:
    """``fc-list`` 输出 → ``(是否有 CJK 字体, 提示文本)``（**纯函数**）。

    提示文本在缺字体时包含包名 ``fonts-noto-cjk``（验收判据：WARN 里出现包名），
    在字体齐全时为空串。
    """
    text = str(fc_list_output or "")
    for needle in _CJK_FONT_NEEDLES:
        if needle in text:
            return True, ""
    hint = (
        "系统未检测到中文字体（Noto Sans CJK / WenQuanYi / Source Han 均缺失），"
        f"页面可能渲染成方框且 A11y 名字退化；建议安装包：{' 或 '.join(_CJK_FONT_PACKAGES)}"
        "（插件不会自动安装）"
    )
    return False, hint


def probe_cjk_fonts(
    runner: Callable[[], str] | None = None, *, force: bool = False
) -> tuple[bool, str]:
    """跑一次 ``fc-list``（结果按进程缓存）并判断 CJK 字体是否可用。

    Args:
        runner: 注入的 ``fc-list`` 执行器（返回 stdout 文本）；``None`` 时真跑
            一次 ``fc-list :lang=zh``（超时/缺失/异常一律按"有字体"处理，
            **不制造假告警**）。
        force: 忽略缓存重跑。

    Returns:
        ``(ok, hint)``；``ok=False`` 时 ``hint`` 含包名（WARN 一行即可）。
    """
    if not force and "ok" in _CJK_FONT_CACHE:
        return _CJK_FONT_CACHE["ok"], _CJK_FONT_CACHE.get("hint", "")  # type: ignore[return-value]
    if runner is not None:
        try:
            output = runner()
        except Exception as exc:  # noqa: BLE001 — 探测失败不告警
            logger.debug("fc-list 探测异常（按有字体处理）: %s", exc)
            output = "assume-ok"
    else:
        try:
            completed = subprocess.run(  # noqa: S603,S607 — 固定命令，无外壳
                ["fc-list", ":lang=zh"],
                capture_output=True,
                text=True,
                timeout=_FC_LIST_TIMEOUT,
                check=False,
            )
            output = completed.stdout or ""
            if completed.returncode != 0 and not output:
                output = "assume-ok"
        except FileNotFoundError:
            # 容器里没有 fontconfig：不能因此断言"缺字体"（也无从修复）。
            logger.debug("环境无 fc-list（跳过字体自检）")
            output = "assume-ok"
        except Exception as exc:  # noqa: BLE001
            logger.debug("fc-list 执行失败（按有字体处理）: %s", exc)
            output = "assume-ok"
    ok, hint = check_cjk_fonts(output)
    if "assume-ok" in str(output) and not ok:
        ok, hint = True, ""
    _CJK_FONT_CACHE["ok"] = ok  # type: ignore[assignment]
    _CJK_FONT_CACHE["hint"] = hint  # type: ignore[assignment]
    return ok, hint


def _as_int(value: object, default: int) -> int:
    """宽松 int（配置可能是字符串；非法值回退默认，不抛异常）。"""
    if value is None or isinstance(value, bool):
        return default if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    """宽松 float（DPR 可能是 ``"1.5"``）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_viewport(view: ConfigView) -> dict:
    """读视口配置：``resources.viewport`` 的 ``{width, height, device_scale_factor}``。

    返回**总是**含 ``width`` / ``height`` / ``device_scale_factor`` 的 dict ——
    下游（``context_options`` / 感知层首行 / 截图标签）不必各自兜底。

    宽高按 320…3840 / 240…2160、DPR 按 0.5…3.0 兜底钳制：面板是自由 JSON
    编辑框，``width`` 写成 0 或 999999 会让 context 创建直接抛错，
    而"渲染环境坏掉"比"参数被夹到合法区间"更糟。
    """
    raw = view.get("resources.viewport")
    source = raw if isinstance(raw, Mapping) else {}
    width = max(320, min(3840, _as_int(source.get("width"), 1280)))
    height = max(240, min(2160, _as_int(source.get("height"), 800)))
    dpr = max(0.5, min(3.0, _as_float(source.get("device_scale_factor"), 1.0)))
    return {"width": width, "height": height, "device_scale_factor": dpr}


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
        view = ConfigView(cfg)
        # v6 键（规格 §7.1）：配置结构只有一份事实源；v5 → v6 的搬迁由迁移器负责，
        # 代码不写第二套键名（否则就是第二个事实源，见铁律 3）。
        self.browser_type: str = str(view.get("resources.engine", "chromium"))
        self.proxy: str = str(view.get("resources.proxy", "") or "")
        self.viewport: dict = _read_viewport(view)
        self.locale: str = _CONTEXT_LOCALE
        self.headless_mode: str = validate_headless_mode(
            str(view.get("resources.headless", "new") or "new")
        )
        self.timeout: float = as_float(view.get("resources.action_timeout_s", 30), 30)
        self.enable_screenshot: bool = as_bool(
            view.get("perception.screenshot_fallback", True), True
        )
        # 是否启用页面级 SSRF 兜底拦截（与 SafetyFilter.block_internal_ip 联动；
        # 关闭内网拦截时也不安装路由拦截，保持行为一致）。
        self.block_internal_ip: bool = as_bool(
            view.get("security.block_internal_ip", True), True
        )
        # data_dir 是插件 initialize() 注入的运行时路径，不是 schema 配置键，
        # 因此不进 KEY_MAP，也不走 ConfigView。
        data_dir = cfg.get("data_dir") or str(
            Path(__file__).resolve().parent.parent / "data"
        )
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 运行时状态：首次 ensure_browser 才启动。
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # 已创建页面 -> 所属 context 的映射，close_page 时成对清理。
        self._page_contexts: dict = {}
        # SSRF 兜底拦截的 host 判定缓存：host -> (过期时间戳, 是否内网)。
        self._ssrf_host_cache: dict[str, tuple[float, bool]] = {}
        # 每个 context 的 SSRF 拦截计数（D9-①）：id(context) -> 次数。
        self._ssrf_blocked: dict[int, int] = {}
        # 字体自检只跑一次（进程级缓存另有一层）。
        self._fonts_checked: bool = False
        # 实际生效的无头模式（``new`` 因缺少 chromium 构建回落时会是 ``shell``）。
        self.launch_mode: str = self.headless_mode

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
        # 启动前校验内核配置：非法值给出可选值提示，避免
        # getattr 返回 None 后 None.launch() 的无提示报错（v1.3.1）。
        self.browser_type = validate_browser_type(self.browser_type)
        plan = self.launch_plan()
        try:
            engine = getattr(self._playwright, self.browser_type)
            try:
                self._browser = await engine.launch(
                    **plan.launch_kwargs(proxy=self.proxy)
                )
            except Exception as primary_error:  # noqa: BLE001
                # 新版无头（``channel="chromium"``）需要单独下载的 chromium 构建；
                # 只装了 headless shell 的环境（CI/离线容器）会在 launch 时报
                # "Executable doesn't exist"。**回落 headless shell 并 WARN**，
                # 绝不静默失败、也不让整个插件起不来（规格 §10.1 的降级口径）。
                if not plan.channel:
                    raise
                logger.warning(
                    "无头模式 %s 启动失败（%s），回落 headless shell（功能等效，"
                    "建议 playwright install chromium 以获得新版无头）",
                    plan.mode,
                    str(primary_error).splitlines()[0][:200],
                )
                fallback = resolve_launch_plan(
                    "shell", engine=self.browser_type
                )
                self._browser = await engine.launch(
                    **fallback.launch_kwargs(proxy=self.proxy)
                )
                plan = fallback
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
        self.launch_mode = plan.mode
        # 判据 §10-2：启动日志含且仅含一行无头形态选择说明。
        logger.info(
            "%s%s%s",
            _HEADLESS_LOG_PREFIX,
            plan.mode,
            f"（{'; '.join(plan.notes)}）" if plan.notes else "",
        )
        await self._selfcheck_cjk_fonts()
        logger.debug(
            "ensure_browser 完成: type=%s proxy=%s headless=%s dpr=%s locale=%s",
            self.browser_type,
            self.proxy or "(直连)",
            plan.mode,
            self.viewport.get("device_scale_factor", 1),
            self.locale,
        )

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
        self._ssrf_blocked.clear()

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
                        tag,
                        _BROWSER_CLOSE_TIMEOUT,
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
                        "[%s] playwright.stop 超时（%.1fs）",
                        tag,
                        _BROWSER_CLOSE_TIMEOUT,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] 停止 playwright 失败（忽略）: %s", tag, e)
        finally:
            # 无论成功/失败/超时，引用一律清空：避免半关闭状态残留。
            self._browser = None
            self._playwright = None
            self._page_contexts.clear()
            self._ssrf_blocked.clear()
        logger.info("[%s] 浏览器资源已释放", tag)

    # ------------------------------------------------------------
    # 页面管理
    # ------------------------------------------------------------

    async def new_page(self) -> "Page":
        """创建新页面：确保浏览器启动，新建 context + page。

        视口 / DPR / locale / 字体栈在 context 级**一次定死**（显示固化，
        规格 §10.1）：同一 URL 在不同视口或 DPR 下 A11y 树不同，感知层
        必须面对一个确定的渲染环境。

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
            context = await self._browser.new_context(**self.context_kwargs())
            await self._install_font_fallback(context)
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
    # 显示固化：启动参数 / context 参数 / 字体
    # ------------------------------------------------------------

    def launch_plan(self) -> LaunchPlan:
        """当前配置对应的 ``launch`` 参数（纯装配；integrator 可只读它做日志/探针）。"""
        return resolve_launch_plan(
            self.headless_mode,
            engine=self.browser_type,
            extra_args=(),
        )

    def context_kwargs(self) -> dict:
        """``browser.new_context`` 的参数：视口 + DPR + locale（三处一起定死）。"""
        return context_options(
            width=_as_int(self.viewport.get("width"), 1280),
            height=_as_int(self.viewport.get("height"), 800),
            device_scale_factor=_as_float(
                self.viewport.get("device_scale_factor"), 1.0
            ),
            locale=self.locale,
        )

    async def _install_font_fallback(self, context) -> None:
        """给 context 注入 CJK 字体栈兜底（无 ``add_init_script`` 的环境跳过）。"""
        add_script = getattr(context, "add_init_script", None)
        if not callable(add_script):
            return
        try:
            result = add_script(font_init_script())
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 — 字体兜底失败不影响浏览
            logger.debug("注入 CJK 字体兜底失败（忽略）: %s", exc)

    async def _selfcheck_cjk_fonts(self) -> None:
        """启动后一次 ``fc-list`` 自检；缺字体 **WARN 一行**（不安装、不阻断）。

        ``fc-list`` 是同步子进程（最多 5s 超时）：放线程里跑，不阻塞事件循环
        （启动阶段正好是浏览器已就绪、还没有页面操作的空档）。
        """
        if self._fonts_checked:
            return
        self._fonts_checked = True
        try:
            ok, hint = await asyncio.to_thread(probe_cjk_fonts)
        except Exception as exc:  # noqa: BLE001 — 自检异常不阻断启动
            logger.debug("字体自检异常（忽略）: %s", exc)
            return
        if not ok and hint:
            logger.warning(hint)

    def ssrf_blocked(self, page) -> int:
        """该页所属 context 被 SSRF 兜底拦截的请求数（喂 ``PageSnapshot.ssrf_blocked``）。"""
        context = self._page_contexts.get(id(page))
        if context is None:
            return 0
        return int(self._ssrf_blocked.get(id(context), 0))

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
                    logger.warning("SSRF 兜底拦截内网请求: %s -> %s", url, hostname)
                    await route.abort("blockedbyclient")
                    key = id(context)
                    self._ssrf_blocked[key] = self._ssrf_blocked.get(key, 0) + 1
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
        ttl = _SSRF_GUARD_CACHE_TTL if verdict is not None else _SSRF_GUARD_DNS_FAIL_TTL
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
                logger.warning(
                    "[%s] 关闭 context 超时（%.1fs，忽略）", tag, _PAGE_CLOSE_TIMEOUT
                )
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
