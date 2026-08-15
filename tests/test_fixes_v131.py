"""v1.3.1 修复回归测试（P1 禁词正文链路 + P2/P3 修复验证）。

覆盖：
- P1：browse_open / browse_get_text / browse_current_page / browse_new_tab /
  browse_click_link 对含禁词正文/标题返回【拒绝】（真实 _page_summary 与
  _check_banned，端到端链路）；
- P2-3：browse_local_page 白名单包含平台工作区候选（tmp_path 模拟平台工作区，
  不依赖宿主机 /root/workspace）与环境变量扩展根目录；
- P2-4：extract_links 对链接文本做换行清洗（真实 ContentExtractor）；
- P3-2：_redact_url 日志 URL 脱敏；
- P3-7：browse_press_key 大小写不敏感；
- P3-8：browser_type 非法值校验（可选值提示，不再 None.launch 报错）；
- P3-9：browse_web 任务描述过禁词。

P2-1 / P2-2 / P2-4 点击链路 / P2-5 / P2-6 / P3-1 的修复断言位于
tests/test_qa_complement.py（原缺陷证明用例已更新为修复后行为）。
"""

import asyncio
import os
import types
from pathlib import Path

import pytest

import main as plugin_main
from core.browser import validate_browser_type
from core.extract import ContentExtractor
from core.safety import SafetyFilter
from main import BrowserLLMPlugin, _redact_url, _resolve_local_page_roots

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
    def __init__(self, text="正文"):
        self.goto_urls = []
        self._text = text

    async def goto(self, url, wait_until="domcontentloaded"):
        self.goto_urls.append(url)

    async def evaluate(self, js, *args):
        return None

    async def inner_text(self, selector):
        return self._text


class FakeExtractor:
    """可配置正文/标题/URL/链接列表的提取器桩。"""

    def __init__(self, text="正常正文", title="正常标题", url="https://example.com",
                 links_text=""):
        self._text = text
        self._title = title
        self._url = url
        self._links = links_text

    async def extract_links(self, page, max_links=20):
        return self._links

    async def extract_page_info(self, page):
        return {"url": self._url, "title": self._title}

    async def extract_text(self, page, max_chars=4000):
        return self._text


class Sessions:
    """记录 ensure_page 调用次数的最小会话桩。"""

    def __init__(self, page):
        self._page = page
        self.ensure_calls = 0

    async def ensure_page(self, umo):
        self.ensure_calls += 1
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()

    def tab_count(self, umo):
        return 1

    async def new_tab(self, umo, url):
        await self._page.goto(url)
        return self._page


def _make_plugin(text="正常正文", title="正常标题", url="https://example.com",
                 links_text=""):
    """组装使用真实 _page_summary / _check_banned 的插件（正文链路禁词验证）。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.extractor = FakeExtractor(text=text, title=title, url=url,
                                     links_text=links_text)
    plugin.sessions = Sessions(FakePage())
    plugin.max_links = 20
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._lock_for = lambda event: asyncio.Lock()
    return plugin


# ================================================================
# P1：禁词过滤覆盖网页正文链路（真实 _page_summary/_check_banned）
# ================================================================

def test_browse_open_blocks_banned_body():
    """正文含禁词：browse_open 返回【拒绝】，违禁内容不返回给 LLM。"""
    plugin = _make_plugin(text="欢迎来到赌博网站")
    result = _run(plugin.browse_open(FakeEvent(), url="https://example.com/page"))
    assert "【拒绝】" in result and "赌博" in result
    assert "欢迎来到赌博网站" not in result


def test_browse_open_blocks_banned_title():
    """标题含禁词：browse_open 返回【拒绝】（标题链路同样受保护）。"""
    plugin = _make_plugin(text="正常内容", title="博彩赌博测试页")
    result = _run(plugin.browse_open(FakeEvent(), url="https://example.com/page"))
    assert "【拒绝】" in result and "赌博" in result
    assert "正常内容" not in result


def test_browse_open_allows_normal_page():
    """对照：无禁词的正常页面原样返回。"""
    plugin = _make_plugin(text="正常新闻内容", title="新闻页")
    result = _run(plugin.browse_open(FakeEvent(), url="https://example.com/news"))
    assert "【拒绝】" not in result
    assert "正常新闻内容" in result


def test_browse_get_text_blocks_banned_body():
    """browse_get_text 返回前过禁词：命中【拒绝】。"""
    plugin = _make_plugin(text="这里提供色情与赌博内容测试")
    result = _run(plugin.browse_get_text(FakeEvent(), max_chars=0))
    assert "【拒绝】" in result and "色情" in result


def test_browse_get_text_allows_normal():
    """对照：browse_get_text 正常正文透传。"""
    plugin = _make_plugin(text="普通页面正文")
    result = _run(plugin.browse_get_text(FakeEvent(), max_chars=0))
    assert result == "普通页面正文"


def test_browse_current_page_blocks_banned_body():
    """browse_current_page 走 _page_summary：正文命中禁词被拦截。"""
    plugin = _make_plugin(text="欢迎来到赌博网站")
    result = _run(plugin.browse_current_page(FakeEvent()))
    assert "【拒绝】" in result and "赌博" in result


def test_browse_current_page_allows_normal():
    """对照：browse_current_page 正常页面返回摘要。"""
    plugin = _make_plugin(text="正常内容")
    result = _run(plugin.browse_current_page(FakeEvent()))
    assert "【拒绝】" not in result
    assert "正文摘要: 正常内容" in result


def test_browse_new_tab_blocks_banned_body():
    """browse_new_tab 新标签摘要同样过禁词。"""
    plugin = _make_plugin(text="欢迎来到赌博网站")
    result = _run(plugin.browse_new_tab(FakeEvent(), url="https://example.com/new"))
    assert "【拒绝】" in result and "赌博" in result


def test_browse_click_link_summary_blocks_banned_body():
    """browse_click_link 点击后返回的页面摘要同样过禁词。"""
    links = "[1] 目标页 → https://example.com/real"
    plugin = _make_plugin(text="欢迎来到赌博网站", links_text=links)
    result = _run(plugin.browse_click_link(FakeEvent(), index=1))
    assert "【拒绝】" in result and "赌博" in result
    assert "欢迎来到赌博网站" not in result


# ================================================================
# P2-3：browse_local_page 白名单包含平台工作区
# ================================================================

def test_resolve_local_page_roots_includes_platform_workspace(tmp_path, monkeypatch):
    """平台工作区候选：存在即加入白名单，不存在则不加（环境解耦）。

    不依赖宿主机 /root/workspace：用 tmp_path 子目录模拟平台工作区候选，
    monkeypatch 替换 main._PLATFORM_WORKSPACE_CANDIDATES，CI 上真实执行。
    """
    candidate = tmp_path / "platform_workspace"
    monkeypatch.setattr(plugin_main, "_PLATFORM_WORKSPACE_CANDIDATES", (candidate,))

    # 候选目录不存在 → 「存在即加入」语义：不在白名单中
    roots_missing = _resolve_local_page_roots()
    assert candidate.resolve() not in roots_missing, \
        f"不存在的候选目录不应加入白名单，实际: {roots_missing}"

    # 创建目录后 → 应加入白名单（验证存在即加入语义）
    candidate.mkdir()
    roots_existing = _resolve_local_page_roots()
    assert candidate.resolve() in roots_existing, \
        f"已存在的候选目录应加入白名单，实际: {roots_existing}"


def test_resolve_local_page_roots_env_extra(tmp_path, monkeypatch):
    """环境变量 BROWSER_LLM_EXTRA_LOCAL_ROOTS 追加额外根目录。"""
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setenv("BROWSER_LLM_EXTRA_LOCAL_ROOTS", str(extra))
    roots = _resolve_local_page_roots()
    assert extra.resolve() in roots


def test_resolve_local_page_roots_dedup():
    """白名单去重保序，且恒含插件 data 目录。"""
    roots = _resolve_local_page_roots()
    assert len(roots) == len(set(roots)), "白名单应无重复根目录"
    data_dir = Path(__file__).resolve().parent.parent / "data"
    assert data_dir.resolve() in roots, "插件 data 目录应始终在列"


def test_local_page_allows_platform_workspace_file(tmp_path, monkeypatch):
    """browse_local_page 白名单校验：平台工作区下的 HTML 放行（环境解耦）。

    monkeypatch 将平台工作区候选指向 tmp_path 子目录并创建，模拟宿主机
    /root/workspace 存在的情形，CI 上真实执行、不依赖宿主目录。
    """
    ws = tmp_path / "platform_workspace"
    ws.mkdir()
    monkeypatch.setattr(plugin_main, "_PLATFORM_WORKSPACE_CANDIDATES", (ws,))

    probe = ws / f".browser_llm_probe_{os.getpid()}.html"
    probe.write_text("<html><body>probe</body></html>", encoding="utf-8")
    try:
        plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
        ok, _ = plugin._check_local_page_path(probe.resolve())
        assert ok is True, "平台工作区 HTML 应被白名单允许"
    finally:
        probe.unlink(missing_ok=True)


def test_local_page_rejects_outside_whitelist(tmp_path):
    """对照：白名单外路径仍被拒绝（回归保护）。"""
    f = tmp_path / "outside.html"
    f.write_text("<html></html>", encoding="utf-8")
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    ok, reason = plugin._check_local_page_path(f.resolve())
    assert ok is False and "不在允许范围内" in reason


# ================================================================
# P2-4：extract_links 链接文本换行清洗（真实 ContentExtractor）
# ================================================================

def test_extract_links_normalizes_newline_text():
    """多行链接文本在编号列表中压缩为单空格，不再拆散编号行。"""
    class LinkPage:
        async def evaluate(self, js):
            return [
                {"text": "第一行\n第二行", "href": "https://example.com/real", "raw": "/real"},
                {"text": " 正常 ", "href": "https://example.org", "raw": "https://example.org"},
            ]

    out = _run(ContentExtractor().extract_links(LinkPage(), max_links=20))
    lines = out.splitlines()
    assert lines[0] == "[1] 第一行 第二行 → https://example.com/real"
    assert lines[1] == "[2] 正常 → https://example.org"


# ================================================================
# P3-2：日志 URL 脱敏
# ================================================================

def test_redact_url_masks_query_and_fragment():
    """query 参数值与 fragment 整体打码，scheme/host/path 保留。"""
    assert _redact_url("https://example.com/path?a=1&b=2#sec") == \
        "https://example.com/path?a=***&b=***#***"
    assert _redact_url("https://example.com/path?a=1") == "https://example.com/path?a=***"


def test_redact_url_no_query_unchanged():
    """无 query/fragment 的 URL 原样返回（host/path 不敏感）。"""
    assert _redact_url("https://example.com/path/page.html") == \
        "https://example.com/path/page.html"


def test_redact_url_edge_cases():
    """无 scheme / 空值 / token 类参数名同样打码。"""
    assert _redact_url("example.com/x?token=abc") == "example.com/x?token=***"
    assert _redact_url("") == ""
    assert _redact_url(None) == ""


# ================================================================
# P3-7：browse_press_key 大小写不敏感
# ================================================================

class KeyPage:
    def __init__(self):
        self.pressed = []
        self.keyboard = self

    async def press(self, key):
        self.pressed.append(key)

    async def goto(self, url, wait_until="domcontentloaded"):
        pass

    async def inner_text(self, selector):
        return "正文"

    async def evaluate(self, js, *args):
        return None


def _make_key_plugin(page: KeyPage):
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = None
    plugin.extractor = FakeExtractor()
    plugin.sessions = Sessions(page)
    plugin.max_links = 20
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._check_banned = lambda text: None
    plugin._lock_for = lambda event: asyncio.Lock()
    return plugin


def test_press_key_lowercase_normalized():
    """小写 "enter" 归一为 Playwright 规范按键名 "Enter"。"""
    page = KeyPage()
    plugin = _make_key_plugin(page)
    result = _run(plugin.browse_press_key(FakeEvent(), key="enter"))
    assert "已按下按键 Enter" in result
    assert page.pressed == ["Enter"]


def test_press_key_case_variants():
    """大小写混合/全大写均可识别。"""
    page = KeyPage()
    plugin = _make_key_plugin(page)
    assert "ArrowDown" in _run(plugin.browse_press_key(FakeEvent(), key="arrowdown"))
    assert "Escape" in _run(plugin.browse_press_key(FakeEvent(), key="ESCAPE"))
    assert page.pressed == ["ArrowDown", "Escape"]


def test_press_key_unsupported_rejected():
    """不支持的按键仍拒绝（回归保护）。"""
    page = KeyPage()
    plugin = _make_key_plugin(page)
    result = _run(plugin.browse_press_key(FakeEvent(), key="F1"))
    assert "不支持的按键" in result
    assert page.pressed == []


# ================================================================
# P3-8：browser_type 非法值校验
# ================================================================

def test_validate_browser_type_normalizes():
    """合法内核归一化；chrome 别名映射为 chromium。"""
    assert validate_browser_type("chromium") == "chromium"
    assert validate_browser_type("CHROMIUM") == "chromium"
    assert validate_browser_type("chrome") == "chromium"
    assert validate_browser_type("Firefox") == "firefox"


def test_validate_browser_type_invalid_raises_with_options():
    """非法内核抛 ValueError 且消息含可选值提示。"""
    for bad in ("chrome2", "", "safari", None):
        with pytest.raises(ValueError, match="chromium/firefox/webkit"):
            validate_browser_type(bad)


def test_get_page_surfaces_browser_type_error():
    """非法 browser_type 经工具链路给出可选值提示，且不创建页面。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.extractor = FakeExtractor()
    plugin.sessions = Sessions(FakePage())
    plugin.browser = types.SimpleNamespace(browser_type="chrome2")
    plugin.max_links = 20
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._check_banned = lambda text: None
    plugin._lock_for = lambda event: asyncio.Lock()
    result = _run(plugin.browse_open(FakeEvent(), url="https://example.com/"))
    assert "不支持的浏览器内核" in result
    assert "chromium/firefox/webkit" in result
    assert plugin.sessions.ensure_calls == 0  # 校验前置，未创建页面


# ================================================================
# P3-9：browse_web 任务描述过禁词
# ================================================================

def _make_web_plugin():
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.config = {}
    plugin.sessions = None
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.browser = None
    plugin.page_perception = "text_image"
    plugin.perception_rules = []
    plugin._vision_cache = {}
    plugin._local_page_locks = {}
    plugin._browser_instruction = None  # 避免 _refresh_config 重建指令
    plugin.context = types.SimpleNamespace(get_current_chat_provider_id=None)
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._parse_perception_prefix = \
        BrowserLLMPlugin._parse_perception_prefix.__get__(plugin)
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._sync_vision_provider_options = lambda: []
    return plugin


def test_browse_web_banned_input_rejected():
    """任务描述含禁词：入口直接【拒绝】，不进入子代理链路。"""
    plugin = _make_web_plugin()
    result = _run(plugin.browse_web(FakeEvent(), input="帮我搜索赌博网站"))
    assert "【拒绝】任务描述包含违禁内容" in result
    assert "赌博" in result


def test_browse_web_perception_prefix_then_banned():
    """带感知模式前缀的输入同样过禁词（前缀剔除后检查）。"""
    plugin = _make_web_plugin()
    result = _run(plugin.browse_web(FakeEvent(), input="perception=text 打开赌博网站"))
    assert "【拒绝】任务描述包含违禁内容" in result
