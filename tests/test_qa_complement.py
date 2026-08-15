"""QA 第三方补充测试（2026-08-16，独立评审视角）。

覆盖既有 306 例未覆盖的盲区，按维度分组：

A. 配置健壮性：非法 int 配置值导致插件 __init__ 崩溃、bool 字符串强转语义错误、
   schema 与代码读取键/默认值双向一致性、AstrBot 4.27.3 select 类型兼容；
B. 功能缺陷：链接文本含换行时 browse_click_link 无法点击、无会话时
   switch/close tab 先建会话的副作用、extract_text(max_chars=0) 边界、
   cache_days=0 清空全部缓存；
C. 安全补测：SSRF 混淆变体（十六进制/前导零/尾部点/十六进制 mapped IPv6/
   userinfo/fragment）、本地页面符号链接逃逸、媒体下载大小上限（预检+流式）、
   data: URI 媒体分类后被安全拦截、滚动参数 JS 注入尝试；
D. 行为补测：inject_browser_instruction 黑名单/正常注入、browse_web
   provider 异常/为空、browse_search 未知引擎/禁词/编码、懒创建会话行为。

测试风格与既有 tests/ 一致（conftest 桩 + FakePage/FakeEvent）。
"""

import asyncio
import time
import json
import re
from pathlib import Path

import pytest

from core.extract import ContentExtractor
from core.safety import SafetyFilter
from main import BrowserLLMPlugin

BANNED = ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sf():
    return SafetyFilter(BANNED, block_internal_ip=True)


class FakeEvent:
    unified_msg_origin = "grp:1:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def image_result(self, path):
        return f"image:{path}"

    def chain_result(self, chain):
        return f"chain:{chain}"

    async def send(self, msg):
        self.sent_messages = getattr(self, "sent_messages", []) + [msg]
        return None


class FakePage:
    """记录 goto/evaluate 调用，模拟最小页面行为。"""

    def __init__(self, text="正文"):
        self.goto_urls = []
        self.evaluated = []
        self._text = text

    async def goto(self, url, wait_until="domcontentloaded"):
        self.goto_urls.append(url)

    async def evaluate(self, js, *args):
        self.evaluated.append(js)
        return None

    async def inner_text(self, selector):
        return self._text

    def is_closed(self):
        return False


class FakeExtractor:
    def __init__(self, links_text="", text="正文"):
        self._links = links_text
        self._text = text

    async def extract_links(self, page, max_links=20):
        return self._links

    async def extract_page_info(self, page):
        return {"url": "https://example.com", "title": "示例"}

    async def extract_text(self, page, max_chars=4000):
        return self._text


class CountingSessions:
    """记录 ensure_page 调用次数（验证懒创建副作用）；tab_count 可配置。"""

    def __init__(self, page, tab_count=1):
        self._page = page
        self.ensure_calls = 0
        self._tab_count = tab_count

    async def ensure_page(self, umo):
        self.ensure_calls += 1
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()

    def tab_count(self, umo):
        return self._tab_count

    async def switch_tab(self, umo, index):
        return self._page

    async def close_tab(self, umo, index):
        return True


def _make_plugin(page: FakePage, links_text: str = "", text: str = "正文") -> BrowserLLMPlugin:
    """组装最小插件实例（绕过 __init__，直接注入依赖）。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.extractor = FakeExtractor(links_text, text)
    plugin.sessions = CountingSessions(page)
    plugin.max_links = 20
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._check_banned = lambda text: None
    plugin._lock_for = lambda event: asyncio.Lock()

    async def _page_summary(p):
        return "URL: x\n标题: t\n\n正文摘要: 正文"

    plugin._page_summary = _page_summary
    return plugin


# ================================================================
# A. 配置健壮性
# ================================================================

def test_int_config_invalid_value_falls_back_to_default():
    """修复验证（原缺陷：非法 int 配置致插件 __init__ 崩溃）。

    vision_cache_ttl/agent_max_steps/agent_tool_timeout/cache_days 等
    数值配置遇非法字符串/None 时回退 schema 默认值并记 warning，
    插件正常加载。
    """
    p = BrowserLLMPlugin(None, {"vision_cache_ttl": "abc"})
    assert p.vision_cache_ttl == 60
    for key, default in (
        ("agent_max_steps", 70),
        ("agent_tool_timeout", 1200),
        ("cache_days", 3),
    ):
        p2 = BrowserLLMPlugin(None, {key: "abc"})
        assert getattr(p2, key) == default
    # 空串/None 不崩溃：vision_cache_ttl 空值按「0=关闭缓存」处理
    # （与旧行为一致），其余回退默认。
    p3 = BrowserLLMPlugin(None, {
        "vision_cache_ttl": "", "cache_days": None, "agent_max_steps": None,
    })
    assert p3.vision_cache_ttl == 0
    assert p3.cache_days == 3
    assert p3.agent_max_steps == 70
    # float 类配置（timeout/max_pages/idle_timeout/max_chars/max_links）同样容错
    p4 = BrowserLLMPlugin(None, {
        "timeout": "abc", "max_pages": None, "idle_timeout": "x",
        "max_chars": [], "max_links": {},
    })
    assert p4.timeout == 30.0
    assert p4.max_pages == 5.0
    assert p4.idle_timeout == 1800.0
    assert p4.max_chars == 4000.0
    assert p4.max_links == 20.0


def test_bool_string_config_normalized():
    """修复验证（原缺陷：字符串 "false" 被强转 True，语义反转）。

    silent_mode/enable_screenshot/block_internal_ip 对字符串布尔值
    归一：false/0/off/"" → False，其余按真值；None 回退默认。
    """
    p = BrowserLLMPlugin(None, {"silent_mode": "false", "enable_screenshot": "false",
                                "block_internal_ip": "false"})
    assert p.silent_mode is False
    assert p.enable_screenshot is False
    assert p.block_internal_ip is False
    # 变体：0 / off / 大小写混合
    p2 = BrowserLLMPlugin(None, {"silent_mode": "0", "enable_screenshot": "off",
                                 "block_internal_ip": "FALSE"})
    assert p2.silent_mode is False
    assert p2.enable_screenshot is False
    assert p2.block_internal_ip is False
    # 真值字符串不受影响
    p3 = BrowserLLMPlugin(None, {"silent_mode": "true", "enable_screenshot": "1",
                                 "block_internal_ip": "on"})
    assert p3.silent_mode is True
    assert p3.enable_screenshot is True
    assert p3.block_internal_ip is True
    # None 回退默认（silent_mode 默认 True）
    p4 = BrowserLLMPlugin(None, {"silent_mode": None})
    assert p4.silent_mode is True


def test_max_chars_invalid_str_breaks_tool():
    """max_chars="abc" 时 browse_get_text 无法使用（int() 抛异常被兜底）。"""
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.max_chars = "abc"
    result = _run(plugin.browse_get_text(FakeEvent(), max_chars=0))
    assert "【错误】" in result


def test_conf_schema_keys_match_load_config():
    """_conf_schema.json 顶层键与 _load_config 读取键双向一致。"""
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_keys = set(schema.keys())

    # 从 _load_config 源码提取 cfg.get 的键名
    main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    load_section = main_src.split("def _load_config")[1].split("def _refresh_config")[0]
    code_keys = set(re.findall(r'cfg\.get\(\s*"([^"]+)"', load_section))

    assert schema_keys == code_keys, (
        f"schema 与代码不一致。仅 schema 有: {schema_keys - code_keys}；"
        f"仅代码读: {code_keys - schema_keys}"
    )


def test_conf_schema_defaults_match_code():
    """关键配置项 schema 默认值与代码默认值一致。"""
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    p = BrowserLLMPlugin(None, {})
    checks = {
        "browser_type": "chromium",
        "default_url": "https://www.baidu.com",
        "default_search_engine": "必应搜索",
        "block_internal_ip": True,
        "max_chars": 4000,
        "max_links": 20,
        "timeout": 30,
        "max_pages": 5,
        "idle_timeout": 1800,
        "enable_screenshot": True,
        "silent_mode": True,
        "page_perception": "text_image",
        "vision_cache_ttl": 60,
        "cache_days": 3,
        "agent_max_steps": 70,
        "agent_tool_timeout": 1200,
        "proxy": "",
    }
    for key, expect in checks.items():
        assert schema[key]["default"] == expect, f"schema default {key} != {expect}"
        assert getattr(p, key) == expect, f"代码默认 {key} != {expect}"
    # banned_words 默认列表
    assert schema["banned_words"]["default"] == p.banned_words
    # viewport
    assert schema["viewport"]["items"]["width"]["default"] == 1280
    assert schema["viewport"]["items"]["height"]["default"] == 800


def test_schema_no_select_type_astrbot_4273_compatible():
    """AstrBot 4.27.3 不支持 type=select；schema 应全部为 string+options 形式。"""
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for key, item in schema.items():
        assert item.get("type") != "select", f"{key} 使用了不兼容的 select 类型"
        if "options" in item:
            assert item["type"] == "string", f"{key} 带 options 但类型不是 string"
    # 嵌套 items（perception_rules）
    assert schema["perception_rules"]["items"]["perception"]["options"] == [
        "text", "text_image", "image"]


# ================================================================
# B. 功能缺陷
# ================================================================

def test_click_link_link_text_with_newline_now_works():
    """修复验证（原缺陷：链接可见文本含换行时编号行被拆散，click 失败）。

    extract_links 对链接文本做空白清洗 + browse_click_link 按编号行切分
    条目解析，多行链接文本可正常点击跳转。
    """
    links = "[1] 第一行\n第二行 → https://example.com/real\n[2] 正常链接 → https://example.org"
    page = FakePage()
    plugin = _make_plugin(page, links_text=links)
    result = _run(plugin.browse_click_link(FakeEvent(), index=1))
    assert "【错误】找不到编号" not in result
    assert page.goto_urls == ["https://example.com/real"]  # 成功点击跳转


def test_click_link_normal_link_still_works():
    """对照：无换行的链接可正常点击（确认缺陷仅限换行场景）。"""
    links = "[1] 正常链接 → https://example.org"
    page = FakePage()
    plugin = _make_plugin(page, links_text=links)
    result = _run(plugin.browse_click_link(FakeEvent(), index=1))
    assert page.goto_urls == ["https://example.org"]


def test_switch_tab_no_session_returns_error_without_creating():
    """修复验证（原缺陷：无会话时 browse_switch_tab 先隐式创建会话）。

    无会话直接返回明确错误，不触发 ensure_page 创建会话（不加载
    default_url、不占标签额度）。
    """
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.sessions = CountingSessions(page, tab_count=0)
    result = _run(plugin.browse_switch_tab(FakeEvent(), index=1))
    assert plugin.sessions.ensure_calls == 0  # 未创建会话
    assert "当前会话没有任何标签页" in result


def test_switch_tab_with_session_still_works():
    """对照：有会话时切换功能正常（回归保护）。"""
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.sessions = CountingSessions(page, tab_count=2)
    result = _run(plugin.browse_switch_tab(FakeEvent(), index=2))
    assert plugin.sessions.ensure_calls == 1  # 有会话才走取页
    assert "已切换到标签 2" in result


def test_close_tab_no_session_returns_error_without_creating():
    """修复验证（原缺陷：无会话时 browse_close_tab 先隐式创建会话再关闭）。"""
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.sessions = CountingSessions(page, tab_count=0)
    result = _run(plugin.browse_close_tab(FakeEvent(), index=1))
    assert plugin.sessions.ensure_calls == 0  # 未创建会话
    assert "当前会话没有任何标签页" in result


def test_close_tab_with_session_still_works():
    """对照：有会话时关闭功能正常（回归保护）。"""
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.sessions = CountingSessions(page, tab_count=2)
    result = _run(plugin.browse_close_tab(FakeEvent(), index=1))
    assert plugin.sessions.ensure_calls == 1
    assert "已关闭标签 1" in result


def test_extract_text_max_chars_zero_uses_default():
    """修复验证（原缺陷：max_chars<=0 输出 1 字符 + 截断标记的怪异结果）。

    max_chars<=0（含 None）按默认值 4000 处理，短文本原样返回。
    """
    class P:
        async def inner_text(self, selector):
            return "0123456789"

    ex = ContentExtractor()
    assert _run(ex.extract_text(P(), max_chars=0)) == "0123456789"
    assert _run(ex.extract_text(P(), max_chars=-5)) == "0123456789"
    assert _run(ex.extract_text(P(), max_chars=None)) == "0123456789"
    # 正常截断仍生效（回归保护）
    long_text = "".join(str(i % 10) for i in range(1000))

    class LongPage:
        async def inner_text(self, selector):
            return long_text

    out = _run(ex.extract_text(LongPage(), max_chars=100))
    assert "（内容过长已截断）" in out and len(out) <= 100


def test_cache_days_zero_skips_cleanup(tmp_path):
    """修复验证（原缺陷：cache_days=0 时清空全部媒体/截图缓存）。

    cache_days<=0 表示「不清理」（与 vision_cache_ttl=0 关闭缓存的
    语义一致），缓存文件全部保留。
    """
    media = tmp_path / "media"
    shots = tmp_path / "screenshots"
    media.mkdir()
    shots.mkdir()
    f1 = media / "a.png"
    f2 = shots / "b.png"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin._media_dir = media
    plugin._screenshot_dir = shots
    plugin.cache_days = 0
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    removed = plugin._cleanup_cache()
    assert removed == 0
    assert f1.exists() and f2.exists(), "cache_days=0 不应删除任何缓存文件"
    # 负值同样不清理
    plugin.cache_days = -1
    assert plugin._cleanup_cache() == 0
    assert f1.exists() and f2.exists()
    # 正值仍正常清理（回归保护）：文件 mtime 置为 2 天前
    import os
    old_ts = time.time() - 2 * 86400
    os.utime(f1, (old_ts, old_ts))
    os.utime(f2, (old_ts, old_ts))
    plugin.cache_days = 1
    assert plugin._cleanup_cache() == 2
    assert not f1.exists() and not f2.exists()


# ================================================================
# C. 安全补测
# ================================================================

@pytest.mark.parametrize("bad_url", [
    "http://0x7f000001/",            # 127.0.0.1 十六进制整数
    "http://0x7f.0.0.1/",            # 十六进制段
    "http://127.0.0.1./",            # 尾部点（DNS 解析回环）
    "http://127.000.000.001/",       # 前导零
    "http://[::ffff:7f00:1]/",       # 十六进制 IPv4-mapped IPv6
    "http://127.0.0.1#@example.com/",   # fragment 混淆（host 仍为内网）
    "http://example.com@127.0.0.1/",    # userinfo 混淆（host 为内网）
])
def test_ssrf_more_obfuscations(sf, bad_url):
    ok, reason = sf.check_url(bad_url)
    assert ok is False, f"应拦截混淆内网地址 {bad_url!r}: {reason}"


def test_local_page_symlink_escape_rejected(tmp_path):
    """符号链接逃逸：白名单内 symlink 指向白名单外文件必须拒绝。"""
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "evil.html"
    target.symlink_to("/etc/passwd")
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin._local_page_allowed_roots = (root.resolve(),)
    ok, reason = plugin._check_local_page_path(target.resolve())
    assert ok is False
    assert "不在允许范围内" in reason


def test_local_page_normal_file_allowed(tmp_path):
    """对照：白名单内普通文件放行。"""
    root = tmp_path / "ws"
    root.mkdir()
    f = root / "ok.html"
    f.write_text("<html></html>")
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin._local_page_allowed_roots = (root.resolve(),)
    ok, _ = plugin._check_local_page_path(f.resolve())
    assert ok is True


# ---- 媒体下载大小上限（mock aiohttp，不依赖真实网络） ----

class _FakeResp:
    def __init__(self, status=200, content_length=None, content=None, headers=None):
        self.status = status
        self.content_length = content_length
        self.content = content
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeContent:
    def __init__(self, total_bytes, chunk_size=1024 * 1024):
        self._left = total_bytes
        self._chunk = chunk_size

    async def iter_chunked(self, n):
        while self._left > 0:
            size = min(self._chunk, self._left)
            self._left -= size
            yield b"x" * size


class _FakeSession:
    def __init__(self, resp_factory):
        self._resp_factory = resp_factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kwargs):
        # 同步返回 _FakeResp（async context manager），避免未 await 告警
        return self._resp_factory(url, kwargs)


def _media_plugin(tmp_path):
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    plugin._media_dir = media_dir
    return plugin


def test_download_media_content_length_over_limit(tmp_path, monkeypatch):
    """Content-Length 预检超 50MB：直接跳过，不写文件。"""
    import aiohttp

    def factory(url, kwargs):
        return _FakeResp(content_length=51 * 1024 * 1024)

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(factory))
    plugin = _media_plugin(tmp_path)
    path = _run(plugin._download_media("https://example.com/big.mp4", 1, "video"))
    assert path == ""
    assert list(plugin._media_dir.iterdir()) == []  # 无半成品/成品


def test_download_media_stream_over_limit_cleans_part(tmp_path, monkeypatch):
    """流式累计超 50MB：中止并清理 .part 临时文件。"""
    import aiohttp

    def factory(url, kwargs):
        return _FakeResp(content_length=None, content=_FakeContent(51 * 1024 * 1024))

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(factory))
    plugin = _media_plugin(tmp_path)
    path = _run(plugin._download_media("https://example.com/big.png", 1, "image"))
    assert path == ""
    files = list(plugin._media_dir.iterdir())
    assert all(f.suffix != ".part" for f in files), "应清理半成品 .part 文件"


def test_download_media_redirect_to_internal_stops(tmp_path, monkeypatch):
    """重定向到内网：逐跳校验拦截，不下载。"""
    import aiohttp

    def factory(url, kwargs):
        return _FakeResp(status=302, headers={"Location": "http://127.0.0.1/steal"})

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _FakeSession(factory))
    plugin = _media_plugin(tmp_path)
    path = _run(plugin._download_media("https://example.com/redirect", 1, "image"))
    assert path == ""


def test_sniff_media_data_uri_classified_then_blocked():
    """data: URI 图片被分类为 image 后由协议白名单拦截（不下载不发送）。"""
    page = FakePage()

    async def fake_evaluate(js):
        return [{"url": "data:image/png;base64,iVBORw0KGgo=", "tag": "img"}]

    page.evaluate = fake_evaluate
    plugin = _make_plugin(page)
    event = FakeEvent()
    result = _run(plugin.browse_sniff_media(event, media_type="all", max_items=5))
    assert "被拦截" in result
    assert not hasattr(event, "sent_messages") or not event.sent_messages


def test_browse_scroll_pixels_string_no_js_injection():
    """滚动参数注入尝试：非数字 pixels 被 int() 拒绝，不执行任何 JS。"""
    page = FakePage()
    plugin = _make_plugin(page)
    result = _run(plugin.browse_scroll(FakeEvent(), direction="down", pixels="800);alert(1)//"))
    assert "【错误】" in result
    assert page.evaluated == []  # 无 JS 执行


def test_browse_scroll_invalid_direction_rejected():
    page = FakePage()
    plugin = _make_plugin(page)
    result = _run(plugin.browse_scroll(FakeEvent(), direction="left", pixels=100))
    assert "direction" in result
    assert page.evaluated == []


# ================================================================
# D. 行为补测
# ================================================================

def test_inject_browser_instruction_blacklist_hint():
    """黑名单会话：system_prompt 注入禁用提示，要求不调用浏览工具。"""
    from astrbot.api.provider import ProviderRequest

    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin._is_session_allowed = lambda event: (False, "此会话已被列入浏览器功能黑名单")
    plugin._build_browser_instruction = lambda: "INSTR"
    req = ProviderRequest()
    _run(plugin.inject_browser_instruction(FakeEvent(), req))
    assert "此会话已被列入浏览器功能黑名单" in req.system_prompt
    assert "不要调用任何 browse_* 工具" in req.system_prompt


def test_inject_browser_instruction_normal_injection():
    """正常会话：system_prompt 追加浏览委托指引。"""
    from astrbot.api.provider import ProviderRequest

    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._build_browser_instruction = lambda: "网页浏览委托指引"
    req = ProviderRequest()
    _run(plugin.inject_browser_instruction(FakeEvent(), req))
    assert "网页浏览委托指引" in req.system_prompt


def test_browse_web_provider_exception_returns_error():
    """browse_web 获取 provider 异常：返回错误提示，不抛出。"""
    class BoomContext:
        async def get_current_chat_provider_id(self, umo=None):
            raise RuntimeError("provider boom")

    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.config = {}
    plugin.sessions = None
    plugin.safety = None
    plugin.browser = None
    plugin.page_perception = "text_image"
    plugin.perception_rules = []
    plugin._vision_cache = {}
    plugin._local_page_locks = {}
    plugin._browser_instruction = "x"
    plugin.context = BoomContext()
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._parse_perception_prefix = BrowserLLMPlugin._parse_perception_prefix.__get__(plugin)
    plugin._resolve_perception_mode = lambda event, explicit: "text_image"
    plugin._build_subagent_instruction = lambda perception=None: "instr"
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._sync_vision_provider_options = lambda: []
    result = _run(plugin.browse_web(FakeEvent(), input="打开 example.com"))
    assert "【错误】无法确定当前对话模型" in result


def test_browse_web_provider_empty_returns_error():
    class EmptyContext:
        async def get_current_chat_provider_id(self, umo=None):
            return ""

    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.config = {}
    plugin.sessions = None
    plugin.safety = None
    plugin.browser = None
    plugin.page_perception = "text_image"
    plugin.perception_rules = []
    plugin._vision_cache = {}
    plugin._local_page_locks = {}
    plugin._browser_instruction = "x"
    plugin.context = EmptyContext()
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._parse_perception_prefix = BrowserLLMPlugin._parse_perception_prefix.__get__(plugin)
    plugin._resolve_perception_mode = lambda event, explicit: "text_image"
    plugin._build_subagent_instruction = lambda perception=None: "instr"
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._sync_vision_provider_options = lambda: []
    result = _run(plugin.browse_web(FakeEvent(), input="打开 example.com"))
    assert "【错误】无法确定当前对话模型" in result


def test_browse_search_unknown_engine():
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.default_search_engine = "必应搜索"
    plugin._search_engines = {
        "必应搜索": "https://cn.bing.com/search?q={keyword}",
        "百度搜索": "https://www.baidu.com/s?wd={keyword}",
        "谷歌搜索": "https://www.google.com.hk/search?&q={keyword}",
        "B站搜索": "https://search.bilibili.com/all?keyword={keyword}",
    }
    result = _run(plugin.browse_search(FakeEvent(), query="天气", engine="不存在的引擎"))
    assert "未知搜索引擎" in result
    assert page.goto_urls == []


def test_browse_search_banned_query():
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.default_search_engine = "必应搜索"
    plugin._search_engines = {"必应搜索": "https://cn.bing.com/search?q={keyword}"}
    plugin._check_banned = lambda text: "赌博" if "赌博" in text else None
    result = _run(plugin.browse_search(FakeEvent(), query="赌博", engine=""))
    assert "【拒绝】" in result
    assert page.goto_urls == []


def test_browse_search_normal_url_encoded():
    page = FakePage()
    plugin = _make_plugin(page)
    plugin.default_search_engine = "必应搜索"
    plugin._search_engines = {"必应搜索": "https://cn.bing.com/search?q={keyword}"}
    result = _run(plugin.browse_search(FakeEvent(), query="北京 天气", engine=""))
    from urllib.parse import quote
    assert page.goto_urls[0] == f"https://cn.bing.com/search?q={quote('北京 天气')}"


def test_browse_open_missing_scheme_rejected():
    """browse_open 传入无协议 URL：协议白名单拒绝。"""
    page = FakePage()
    plugin = _make_plugin(page)
    result = _run(plugin.browse_open(FakeEvent(), url="example.com/path"))
    assert "【拒绝】" in result
    assert page.goto_urls == []


def test_browse_open_empty_url_rejected():
    page = FakePage()
    plugin = _make_plugin(page)
    result = _run(plugin.browse_open(FakeEvent(), url=""))
    assert "【拒绝】" in result or "【错误】" in result
    assert page.goto_urls == []
