"""browse_click_link / browse_open 的 SSRF 行为级测试。

用 mock page + mock safety 验证：页面内指向内网地址的链接（被攻陷
网页诱导点击）会在 goto 前被拦截，返回拦截原因文本而非跳转。
依赖 conftest.py 的 astrbot 桩与根目录 sys.path。
"""

import asyncio

import pytest

from core.safety import SafetyFilter
from main import BrowserLLMPlugin

BANNED = ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"]


def _run(coro):
    return asyncio.run(coro)


class FakeEvent:
    unified_msg_origin = "grp:1:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    # browse_sniff_media 成功分支需要的方法（参考 conftest AstrMessageEvent 桩）。
    def image_result(self, path):
        return f"image:{path}"

    def chain_result(self, chain):
        return f"chain:{chain}"

    async def send(self, msg):
        self.sent_messages = getattr(self, "sent_messages", []) + [msg]
        return None


class FakePage:
    """记录 goto 调用，模拟页面行为。"""

    def __init__(self):
        self.goto_urls = []

    async def goto(self, url, wait_until="domcontentloaded"):
        self.goto_urls.append(url)

    def is_closed(self):
        return False


class FakeExtractor:
    """固定返回链接列表：编号 1 为内网链接，编号 2 为正常外链。"""

    def __init__(self, links_text):
        self._links = links_text

    async def extract_links(self, page, max_links=20):
        return self._links

    async def extract_page_info(self, page):
        return {"url": "https://example.com", "title": "示例"}

    async def extract_text(self, page, max_chars=4000):
        return "正文"


class FakeSessions:
    def __init__(self, page):
        self._page = page

    async def ensure_page(self, umo):
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()

    def tab_count(self, umo):
        return 1

    async def new_tab(self, umo, url):
        await self._page.goto(url)  # 模拟 SessionManager.new_tab 的导航
        return self._page


def _make_plugin(page: FakePage, links_text: str) -> BrowserLLMPlugin:
    """组装最小插件实例（绕过 __init__，直接注入依赖）。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.extractor = FakeExtractor(links_text)
    plugin.sessions = FakeSessions(page)
    plugin.max_links = 20
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._check_banned = lambda text: None
    plugin._lock_for = lambda event: asyncio.Lock()

    async def _page_summary(p):
        return "URL: x\n标题: t\n\n正文摘要: 正文"

    plugin._page_summary = _page_summary  # 必须 async：工具内 await 调用
    return plugin


# ------------------------------------------------------------
# browse_click_link：内网链接拦截
# ------------------------------------------------------------

def test_click_link_blocks_internal_target():
    """被攻陷网页放内网链接：goto 前拦截，返回拦截原因。"""
    page = FakePage()
    links = "[1] 内网入口 → http://127.0.0.1/admin\n[2] 外链 → https://example.com/"
    plugin = _make_plugin(page, links)

    out = _run(plugin.browse_click_link(FakeEvent(), 1))
    assert "拒绝" in out and "内网" in out, out
    assert page.goto_urls == [], "内网链接不应触发 goto"


def test_click_link_blocks_metadata_target():
    """指向云元数据服务 169.254.169.254 的链接应被拦截。"""
    page = FakePage()
    links = "[1] meta → http://169.254.169.254/latest/meta-data/"
    plugin = _make_plugin(page, links)
    out = _run(plugin.browse_click_link(FakeEvent(), 1))
    assert "拒绝" in out, out
    assert page.goto_urls == []


def test_click_link_allows_public_target():
    """正常外链放行并 goto（用公网 IP 字面量，避免依赖 DNS）。"""
    page = FakePage()
    links = "[1] 外链 → http://93.184.216.34/page"
    plugin = _make_plugin(page, links)
    out = _run(plugin.browse_click_link(FakeEvent(), 1))
    assert "拒绝" not in out, out
    assert "正文摘要" in out, f"应返回页面摘要（成功分支）: {out}"
    assert page.goto_urls == ["http://93.184.216.34/page"]


# ------------------------------------------------------------
# browse_open：async acheck_url 拦截内网
# ------------------------------------------------------------

def test_open_blocks_internal_url():
    page = FakePage()
    plugin = _make_plugin(page, "")
    out = _run(plugin.browse_open(FakeEvent(), "http://127.0.0.1/x"))
    assert "拒绝" in out and "内网" in out, out
    assert page.goto_urls == []


def test_open_allows_public_url():
    page = FakePage()
    plugin = _make_plugin(page, "")
    out = _run(plugin.browse_open(FakeEvent(), "http://93.184.216.34/"))
    assert "拒绝" not in out, out
    assert "正文摘要" in out, f"应返回页面摘要（成功分支）: {out}"
    assert page.goto_urls == ["http://93.184.216.34/"]


# ------------------------------------------------------------
# browse_new_tab：async acheck_url 拦截内网
# ------------------------------------------------------------

def test_new_tab_blocks_internal_url():
    page = FakePage()
    plugin = _make_plugin(page, "")
    out = _run(plugin.browse_new_tab(FakeEvent(), "http://192.168.1.1/"))
    assert "拒绝" in out and "内网" in out, out
    assert page.goto_urls == [], "内网地址不应触发 new_tab"


def test_new_tab_allows_public_url():
    """放行路径必须真实走到成功分支（钉死成功文案，防被 except 吞掉）。"""
    page = FakePage()
    plugin = _make_plugin(page, "")
    out = _run(plugin.browse_new_tab(FakeEvent(), "http://93.184.216.34/"))
    assert "已新开标签页" in out, f"应走成功分支: {out}"
    assert "拒绝" not in out, out
    assert page.goto_urls == ["http://93.184.216.34/"]


# ------------------------------------------------------------
# browse_sniff_media：SSRF 拦截（内网/file:// 媒体 URL 拒绝下载）
# ------------------------------------------------------------

class _MediaPage(FakePage):
    """mock Page：evaluate 返回带标签类型的媒体列表。"""

    def __init__(self, media_items):
        super().__init__()
        self._media_items = media_items

    async def evaluate(self, js):
        if "querySelectorAll" in js and "tag" in js:
            return self._media_items
        return []

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 800}


_TEST_MEDIA_DIR = __import__("pathlib").Path("/tmp/browser_llm_media_test")


def _make_media_plugin(page) -> BrowserLLMPlugin:
    """组装支持 browse_sniff_media 的最小插件（含共享 media_dir）。"""
    plugin = _make_plugin(page, "")
    plugin._media_dir = _TEST_MEDIA_DIR
    _TEST_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return plugin


@pytest.fixture(autouse=True)
def _cleanup_media_dir():
    """每个测试后清理共享测试目录，避免残留文件。"""
    yield
    if _TEST_MEDIA_DIR.is_dir():
        for f in _TEST_MEDIA_DIR.iterdir():
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass


def test_sniff_media_blocks_internal_url():
    """被攻陷页面放内网/元数据媒体 URL：下载前 SSRF 拦截，不调 _download_media。"""
    page = _MediaPage([
        {"url": "http://169.254.169.254/latest/meta-data/", "tag": "img"},
        {"url": "https://example.com/ok.png", "tag": "img"},
    ])
    plugin = _make_media_plugin(page)
    called = []

    async def _fake_download(url, idx, cls):
        called.append(url)
        return "/tmp/browser_llm_media_test/x.png"

    plugin._download_media = _fake_download
    ev = FakeEvent()
    out = _run(plugin.browse_sniff_media(ev, "all", 5))
    # 内网 URL 被拦截（不下载），公网 URL 正常下载发送
    assert called == ["https://example.com/ok.png"], f"内网 URL 不应下载: {called}"
    assert "已下载并发送 1 个媒体" in out, out
    assert "1 个 URL 被安全拦截" in out, f"应提示被拦截数: {out}"
    assert ev.sent_messages == ["image:/tmp/browser_llm_media_test/x.png"], (
        f"应发送图片: {getattr(ev, 'sent_messages', None)}"
    )


def test_sniff_media_blocks_file_url():
    """file:// 协议媒体 URL 被 acheck_url 拒绝。"""
    page = _MediaPage([
        {"url": "file:///etc/passwd", "tag": "img"},
        {"url": "https://example.com/a.jpg", "tag": "img"},
    ])
    plugin = _make_media_plugin(page)
    called = []

    async def _fake_download(url, idx, cls):
        called.append(url)
        return "/tmp/browser_llm_media_test/x.jpg"

    plugin._download_media = _fake_download
    ev = FakeEvent()
    out = _run(plugin.browse_sniff_media(ev, "image", 5))
    assert called == ["https://example.com/a.jpg"], f"file:// 不应下载: {called}"
    assert "已下载并发送 1 个媒体" in out, out
    assert ev.sent_messages == ["image:/tmp/browser_llm_media_test/x.jpg"], (
        f"应发送图片: {getattr(ev, 'sent_messages', None)}"
    )


def test_sniff_media_max_items_invalid():
    """max_items 非数字返回参数错误（不抛异常被吞）。"""
    page = _MediaPage([])
    plugin = _make_media_plugin(page)
    out = _run(plugin.browse_sniff_media(FakeEvent(), "all", "abc"))
    assert "【错误】max_items 参数无效" in out, out
