"""browse_local_page（本地页面渲染预览，面向子代理）测试。

覆盖：
- 契约：@filter.llm_tool 注册、docstring Args 参数类型（string/boolean/number）；
- 安全：白名单（工作区/插件 data 目录）、路径穿越、扩展名、会话黑名单、禁词；
- 行为：视觉描述模式、视觉不可用降级文本模式、截图失败降级、参数/文件错误兜底；
- 集成（真实渲染）：含 CSS/JS 的本地 HTML 用 Playwright 真实渲染，验证 JS 动态
  内容进入提取文本、截图落盘、视觉描述透传。无 playwright/chromium 环境自动 skip。

依赖 conftest.py 的 astrbot 桩与根目录 sys.path。
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.extract import ContentExtractor
from core.safety import SafetyFilter
from main import (
    _LOCAL_PAGE_ALLOWED_EXTS,
    BrowserLLMPlugin,
    _resolve_local_page_roots,
)

BANNED = ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"]


def _run(coro):
    return asyncio.run(coro)


class FakeEvent:
    unified_msg_origin = "grp:1:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def image_result(self, path):
        return f"image:{path}"

    async def send(self, msg):
        return None


class FakePage:
    """模拟 Playwright Page：记录 goto 目标，支持截图与等待。"""

    def __init__(self, text="页面正文", title="测试页", url="file:///tmp/x.html"):
        self._text = text
        self._title = title
        self._url = url
        self.goto_urls = []
        self.screenshot_paths = []

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.goto_urls.append(url)

    async def wait_for_timeout(self, ms):
        return None

    async def title(self):
        return self._title

    @property
    def url(self):
        return self._url

    async def inner_text(self, selector):
        return self._text

    async def evaluate(self, js):
        return self._text

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"fake-png")
        self.screenshot_paths.append(path)
        return None

    def is_closed(self):
        return False


class FakeBrowser:
    """模拟 BrowserCore：new_page 返回 FakePage，screenshot 可配置失败。"""

    def __init__(self, page=None, screenshot_ok=True):
        self._page = page or FakePage()
        self.screenshot_ok = screenshot_ok
        self.closed_pages = []

    async def new_page(self):
        return self._page

    async def screenshot(self, page, save_path):
        if not self.screenshot_ok:
            return ""
        Path(save_path).write_bytes(b"fake-png")
        return save_path

    async def close_page(self, page):
        self.closed_pages.append(page)


def _make_plugin(tmp_path, page=None, screenshot_ok=True, session_blacklist=None,
                 session_whitelist=None):
    """构造最小可用插件实例（不调用 initialize，手动装配所需组件）。"""
    cfg = {
        "browser_type": "chromium",
        "banned_words": BANNED,
        "block_internal_ip": True,
        "max_chars": 4000,
        "timeout": 30,
        "session_whitelist": session_whitelist or [],
        "session_blacklist": session_blacklist or [],
        "enable_screenshot": True,
        "vision_provider_id": "opencode-go/mimo-v2.5",
        "silent_mode": True,
    }
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="模拟视觉描述：页面顶部为导航栏，主体为文字内容。")
        )
    )
    plugin = BrowserLLMPlugin(context=context, config=cfg)
    plugin.browser = FakeBrowser(page=page, screenshot_ok=screenshot_ok)
    plugin.safety = SafetyFilter(BANNED, True)
    plugin.extractor = ContentExtractor()
    shot_dir = tmp_path / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    plugin._screenshot_dir = shot_dir
    # 覆盖白名单根目录为 tmp_path 下的 workspaces/data（不依赖真实路径）。
    plugin._local_page_allowed_roots = (
        (tmp_path / "workspaces").resolve(),
        (tmp_path / "data").resolve(),
    )
    return plugin


def _write_html(root: Path, name="index.html", text="本地页面正文", title="本地测试页"):
    root.mkdir(parents=True, exist_ok=True)
    f = root / name
    f.write_text(
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1><p>{text}</p></body></html>",
        encoding="utf-8",
    )
    return f


# ------------------------------------------------------------
# 契约：注册与 docstring
# ------------------------------------------------------------

def test_browse_local_page_registered():
    """browse_local_page 带 @filter.llm_tool(name='browse_local_page') 装饰器。"""
    src = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_local_page"),
        None,
    )
    assert node is not None, "browse_local_page 方法不存在"
    names = []
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                and dec.func.attr == "llm_tool":
            names.extend(
                kw.value.value for kw in dec.keywords
                if kw.arg == "name" and isinstance(kw.value, ast.Constant)
            )
    assert names == ["browse_local_page"], f"llm_tool 注册缺失: {names}"


def test_browse_local_page_docstring_contract():
    """docstring 含 Args 段，参数类型 ∈ {string, boolean, number}（llm_tool 契约）。"""
    from docstring_parser import parse as dp_parse

    src = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_local_page"),
        None,
    )
    doc = ast.get_docstring(node) or ""
    assert "Args:" in doc
    params = {p.arg_name: p.type_name for p in dp_parse(doc).params}
    assert params.get("path") == "string", f"path 参数类型错误: {params}"
    assert params.get("full_page") == "boolean", f"full_page 参数类型错误: {params}"
    assert params.get("wait_ms") == "number", f"wait_ms 参数类型错误: {params}"


# ------------------------------------------------------------
# 安全：白名单 / 路径穿越 / 扩展名 / 会话黑名单 / 禁词
# ------------------------------------------------------------

def test_path_whitelist_allow_workspace(tmp_path):
    plugin = _make_plugin(tmp_path)
    html = _write_html(tmp_path / "workspaces", "index.html")
    ok, reason = plugin._check_local_page_path(html.resolve())
    assert ok, reason


def test_path_whitelist_allow_plugin_data(tmp_path):
    plugin = _make_plugin(tmp_path)
    html = _write_html(tmp_path / "data" / "pages", "demo.html")
    ok, reason = plugin._check_local_page_path(html.resolve())
    assert ok, reason


def test_path_whitelist_reject_outside(tmp_path):
    plugin = _make_plugin(tmp_path)
    outside = (tmp_path / "etc" / "passwd").resolve()
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("root:x:0:0:")
    ok, reason = plugin._check_local_page_path(outside)
    assert not ok
    assert "不在允许范围内" in reason


def test_path_traversal_rejected(tmp_path):
    """../ 穿越到白名单根之外必须被拒绝（resolve 后前缀校验）。"""
    plugin = _make_plugin(tmp_path)
    ws = (tmp_path / "workspaces").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    # 构造看似在白名单内、实则穿越出去的路径字符串。
    traversal = ws / ".." / ".." / "etc" / "shadow"
    resolved = traversal.resolve()
    assert not resolved.is_relative_to(ws)
    ok, reason = plugin._check_local_page_path(resolved)
    assert not ok
    assert "不在允许范围内" in reason


def test_extension_rejected(tmp_path):
    """非 .html/.htm 文件即使位于白名单内也拒绝。"""
    plugin = _make_plugin(tmp_path)
    txt = (tmp_path / "workspaces" / "note.txt").resolve()
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("hello")
    assert txt.suffix.lower() not in _LOCAL_PAGE_ALLOWED_EXTS
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(txt)))
    assert "仅支持 .html/.htm 文件" in result


def test_empty_path_rejected(tmp_path):
    plugin = _make_plugin(tmp_path)
    result = _run(plugin.browse_local_page(FakeEvent(), path="  "))
    assert "path 不能为空" in result


def test_file_not_exists(tmp_path):
    plugin = _make_plugin(tmp_path)
    missing = (tmp_path / "workspaces" / "nope.html").resolve()
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(missing)))
    assert "文件不存在" in result


def test_session_blacklist_rejected(tmp_path):
    plugin = _make_plugin(tmp_path, session_blacklist=["grp:1:GroupMessage:123"])
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【拒绝】" in result


def test_banned_text_rejected(tmp_path):
    """降级文本模式：提取文本命中禁词 → 拒绝输出。"""
    page = FakePage(text="页面包含赌博相关内容")
    plugin = _make_plugin(tmp_path, page=page)
    plugin.vision_provider_id = ""  # 关闭视觉，强制走文本降级路径
    html = _write_html(tmp_path / "workspaces", "index.html", text="页面包含赌博相关内容")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【拒绝】" in result and "赌博" in result


def test_banned_vision_rejected(tmp_path):
    """视觉描述命中禁词 → 拒绝输出。"""
    plugin = _make_plugin(tmp_path)
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text="页面有成人内容链接"
    )
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【拒绝】" in result and "成人" in result


# ------------------------------------------------------------
# 行为：视觉模式 / 降级文本模式 / 截图失败降级 / file:// 前缀
# ------------------------------------------------------------

def test_vision_mode_ok(tmp_path):
    """视觉描述成功：输出含描述文本与截图保存路径，且页面用完即关。"""
    page = FakePage()
    plugin = _make_plugin(tmp_path, page=page)
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【本地页面预览】" in result
    assert "模拟视觉描述" in result
    assert "截图:" in result
    assert page in plugin.browser.closed_pages, "独立页面应被关闭"
    assert page.goto_urls[0].startswith("file://"), "应以 file:// URL 渲染"


def test_file_uri_prefix_accepted(tmp_path):
    """入参带 file:// 前缀时同样放行。"""
    page = FakePage()
    plugin = _make_plugin(tmp_path, page=page)
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=html.as_uri()))
    assert "【本地页面预览】" in result


def test_degraded_text_mode_when_vision_unavailable(tmp_path):
    """视觉模型不可用（描述为空）→ 降级文本提取，保证不空转。"""
    page = FakePage(text="正文一 正文二 正文三")
    plugin = _make_plugin(tmp_path, page=page)
    plugin.vision_provider_id = ""
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【本地页面预览·文本模式】" in result
    assert "正文一 正文二 正文三" in result
    assert "已降级为文本提取" in result


def test_degraded_text_mode_when_screenshot_fails(tmp_path):
    """截图失败（enable_screenshot=false / 截图异常）→ 降级文本提取。"""
    page = FakePage(text="只有文字也能看")
    plugin = _make_plugin(tmp_path, page=page, screenshot_ok=False)
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【本地页面预览·文本模式】" in result
    assert "只有文字也能看" in result


def test_vision_mode_with_full_page(tmp_path):
    """full_page=True 时走整页截图分支。"""
    page = FakePage()
    plugin = _make_plugin(tmp_path, page=page)
    html = _write_html(tmp_path / "workspaces", "index.html")
    result = _run(
        plugin.browse_local_page(FakeEvent(), path=str(html), full_page=True)
    )
    assert "【本地页面预览】" in result
    assert len(page.screenshot_paths) == 1


# ------------------------------------------------------------
# 集成：真实 Playwright 渲染（含 CSS/JS）
# ------------------------------------------------------------

def _chromium_available() -> bool:
    """chromium 内核是否可用（缺 playwright 或内核目录时返回 False）。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.is_dir():
        return False
    return any(p.name.startswith("chromium") for p in cache.iterdir())


def _build_integration_plugin(tmp_path, vision_text):
    """装配真实 BrowserCore 的插件实例（不启动会话管理器）。"""
    from core.browser import BrowserCore  # noqa: PLC0415

    cfg = {
        "banned_words": BANNED,
        "block_internal_ip": True,
        "max_chars": 4000,
        "timeout": 30,
        "enable_screenshot": True,
        "vision_provider_id": "fake/vision" if vision_text else "",
        "viewport": {"width": 1280, "height": 800},
        "data_dir": str(tmp_path),
    }
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text=vision_text or "")
        )
    )
    plugin = BrowserLLMPlugin(context=context, config=cfg)
    plugin.browser = BrowserCore(cfg)
    plugin.safety = SafetyFilter(BANNED, True)
    plugin.extractor = ContentExtractor()
    shot_dir = tmp_path / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    plugin._screenshot_dir = shot_dir
    plugin._local_page_allowed_roots = (
        (tmp_path / "workspaces").resolve(),
        (tmp_path / "data").resolve(),
    )
    return plugin


@pytest.mark.skipif(not _chromium_available(), reason="无 playwright/chromium 环境")
def test_real_render_css_js_vision(tmp_path):
    """真实渲染含 CSS/JS 的 HTML：JS 动态写入文本，视觉描述透传，截图落盘。"""
    ws = tmp_path / "workspaces"
    ws.mkdir(parents=True, exist_ok=True)
    html = ws / "preview_demo.html"
    html.write_text(
        """<!doctype html>
<html><head><meta charset="utf-8"><title>CSS/JS 渲染验证页</title>
<style>
  body { font-family: sans-serif; background: #f5f5f5; }
  .card { border: 2px solid #333; padding: 24px; max-width: 480px; margin: 40px auto; }
  h1 { color: #c0392b; font-size: 28px; }
</style></head>
<body>
<div class="card">
  <h1>静态标题</h1>
  <p>静态段落文本</p>
  <p id="dynamic">（等待 JS 填充）</p>
</div>
<script>
  document.getElementById('dynamic').textContent = 'JS动态生成文本-渲染成功';
</script>
</body></html>""",
        encoding="utf-8",
    )
    plugin = _build_integration_plugin(tmp_path, vision_text="视觉描述：红色标题卡片居中，含动态文本。")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【本地页面预览】" in result
    assert "视觉描述：红色标题卡片居中" in result, result
    # 截图已落盘且是真实 PNG。
    shot_line = next((ln for ln in result.splitlines() if ln.startswith("截图: ")), "")
    assert shot_line, result
    shot_path = Path(shot_line.split("截图: ", 1)[1].strip())
    assert shot_path.is_file() and shot_path.stat().st_size > 0


@pytest.mark.skipif(not _chromium_available(), reason="无 playwright/chromium 环境")
def test_real_render_degraded_text_js_executed(tmp_path):
    """真实渲染 + 视觉不可用降级：JS 动态文本必须出现在提取结果中。"""
    ws = tmp_path / "workspaces"
    ws.mkdir(parents=True, exist_ok=True)
    html = ws / "js_demo.html"
    html.write_text(
        """<!doctype html><html><head><meta charset="utf-8"><title>JS 页</title></head>
<body><p>静态文本</p><p id="dyn"></p>
<script>document.getElementById('dyn').textContent = 'JS-OK-动态内容';</script>
</body></html>""",
        encoding="utf-8",
    )
    plugin = _build_integration_plugin(tmp_path, vision_text="")
    result = _run(plugin.browse_local_page(FakeEvent(), path=str(html)))
    assert "【本地页面预览·文本模式】" in result
    assert "JS-OK-动态内容" in result, (
        f"JS 动态内容未进入提取文本（JS 未执行或时序问题）: {result}"
    )
