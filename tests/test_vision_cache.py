"""识图短时缓存测试（v1.3.0 P1；v1.6.1 换键 + 事故回归）。

v1.6.1 的缓存键是七元组（见 ``main._vision_cache_key``）::

    (umo, 规范化URL, 工具种类, 裁剪参数, 图像内容哈希, 提示词哈希, provider_id)

- **图像内容哈希** = sha256(截图文件字节) 十六进制前 32 位（写死在
  ``_VISION_IMAGE_HASH_LEN``）。这是 2026-10-26 事故（
  ``ops_astrbot/incidents/20261026-browser-vision-cache-stale-on-spa.md``）的
  正面修复：v1.3.0 的键只有 ``(umo, 规范化URL)``，同一 URL 下所有截图/裁剪
  共用一个槽，交互改变画面后仍把上一张（甚至局部裁剪）的描述当本次结果。
- 工具种类 + 裁剪参数：全图与局部裁剪不再互相命中；
- 提示词哈希 + provider_id：运营方改 ``vision_prompt`` 或换识图 provider 后
  旧描述不再命中。

覆盖：
- 命中 / 画面变化未命中 / TTL 过期 / ``vision_cache_ttl=0`` 仍是「不读不写」
  （含「预置条目也不读」的关闭语义守卫）；
- URL 规范化（fragment / 尾斜杠）、会话隔离、缺 URL / 缺会话 / 图像不可读
  时不参与缓存（fail-closed）；
- 键成分逐项独立生效：内容哈希 / 工具种类 / 裁剪参数 / 提示词 / provider；
- **事故时间线回归**：真识图 → crop → screenshot 三连不串号，且
  ``vision_cache_ttl=0`` 与 ``>0`` 两种配置下都绿；
- 拒识结果不入缓存、条目超阈值清理过期项、terminate 清空缓存。

依赖 conftest 的 astrbot 桩；截图用 tmp_path 里的真实字节（内容哈希必须来自
真实文件，不许用路径/序号冒充）。
"""

import asyncio
import hashlib
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main as main_module
from main import (
    _DEFAULT_VISION_PROMPT,
    _VISION_CACHE_MAX_ENTRIES,
    _VISION_IMAGE_HASH_LEN,
    _VISION_PROMPT_HASH_LEN,
    _VISION_REJECTION_HINT,
    BrowserLLMPlugin,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _run(coro):
    return asyncio.run(coro)


def _png(tmp_path, name: str, payload: bytes) -> str:
    """写一张「伪 PNG」（PNG 魔数 + 载荷），返回本地路径。

    内容哈希只关心字节、不解析图片格式，因此无需真 PNG 编码器。
    """
    path = Path(tmp_path) / name
    path.write_bytes(_PNG_MAGIC + payload)
    return str(path)


def _sha_prefix(path: str, n: int = _VISION_IMAGE_HASH_LEN) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:n]


class _Provider:
    def __init__(self, pid):
        self._pid = pid

    def meta(self):
        return SimpleNamespace(id=self._pid)


class _ProviderManager:
    def __init__(self, inst_map=None, provider_insts=None):
        self.inst_map = dict(inst_map or {})
        self.provider_insts = list(provider_insts or [])


class _Event:
    def __init__(self, umo="grp:1:GroupMessage:123"):
        self.unified_msg_origin = umo

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


def _make_plugin(ttl=60, vision="vision/pid", pm=None, prompt="自定义提示词"):
    """构造带识图 provider 与 TTL 配置的最小插件实例（走真实 __init__）。"""
    if pm is None:
        pm = _ProviderManager(
            provider_insts=[_Provider(vision)],
            inst_map={vision: object()},
        )
    cfg = {
        "banned_words": ["赌博"],
        "block_internal_ip": True,
        "enable_screenshot": True,
        "silent_mode": True,
        "page_perception": "text_image",
        "perception_rules": [],
        "vision_provider_id": vision,
        "vision_prompt": prompt,
        "vision_cache_ttl": ttl,
    }
    plugin = BrowserLLMPlugin(
        context=SimpleNamespace(provider_manager=pm), config=cfg
    )
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="识图描述：页面顶部为导航栏。")
    )
    return plugin


def _capture_logger(monkeypatch, level="debug"):
    records = []
    monkeypatch.setattr(main_module.logger, level, records.append)
    return records


# ============================================================
# 命中 / 画面变化未命中 / TTL 过期 / 关闭语义
# ============================================================

def test_cache_hit_within_ttl(tmp_path):
    """同会话 + 同 URL + **同图像字节**在 TTL 内二次识图命中（不再调 LLM）。

    两张图的**路径不同但内容相同** ⇒ 命中：证明键取自内容而不是路径。
    """
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    img_a = _png(tmp_path, "a.png", b"A")
    img_a2 = _png(tmp_path, "a-copy.png", b"A")

    first = _run(plugin._describe_screenshot(img_a, event, page_url=url))
    second = _run(plugin._describe_screenshot(img_a2, event, page_url=url))

    assert first == "识图描述：页面顶部为导航栏。"
    assert second == "[缓存] 识图描述：页面顶部为导航栏。", "缓存命中应带 [缓存] 前缀"
    plugin.context.llm_generate.assert_awaited_once()
    assert plugin._vision_cache, "命中应来自缓存写入"


def test_cache_hit_logs_debug(tmp_path, monkeypatch):
    """命中时记 debug 日志（事故复盘靠它区分「命中」与「真识图」）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    debugs = _capture_logger(monkeypatch, "debug")
    _run(plugin._describe_screenshot(img, event, page_url=url))
    _run(plugin._describe_screenshot(img, event, page_url=url))
    assert any("缓存命中" in d for d in debugs), "应有缓存命中日志标注"


def test_cache_miss_when_picture_changes(tmp_path):
    """**事故核心**：同 URL 不同画面不得互相命中（v1.3.0 会命中旧描述）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/spa"
    img_a = _png(tmp_path, "a.png", b"A")
    img_b = _png(tmp_path, "b.png", b"B")

    first = _run(plugin._describe_screenshot(img_a, event, page_url=url))
    second = _run(plugin._describe_screenshot(img_b, event, page_url=url))

    assert first == "识图描述：页面顶部为导航栏。"
    assert "[缓存]" not in second, "画面变了必须重新识图"
    assert plugin.context.llm_generate.await_count == 2


def test_cache_miss_after_ttl_expiry(tmp_path):
    """超过 TTL 后重新识图（过期条目删除并按新时间戳重写）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    key = plugin._vision_cache_key(
        url, event, img, prompt="自定义提示词", provider_id="vision/pid"
    )
    assert key is not None
    plugin._vision_cache[key] = (time.time() - 61, "过期描述")

    out = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert out == "识图描述：页面顶部为导航栏。", "过期后应重新识图"
    assert "过期描述" not in out
    plugin.context.llm_generate.assert_awaited_once()
    assert plugin._vision_cache[key][1] == "识图描述：页面顶部为导航栏。"


def test_cache_disabled_when_ttl_zero(tmp_path):
    """vision_cache_ttl=0：每次识图都调用 LLM，不读不写缓存。"""
    plugin = _make_plugin(ttl=0)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event, page_url=url))
    _run(plugin._describe_screenshot(img, event, page_url=url))
    assert plugin.context.llm_generate.await_count == 2, "关闭缓存应每次都识图"
    assert plugin._vision_cache == {}, "关闭缓存不得写入任何条目"


def test_ttl_zero_never_reads_preseeded_entry(tmp_path):
    """vision_cache_ttl=0 仍是**关闭**语义：连预置条目都不读（不得退化成 >0）。"""
    plugin = _make_plugin(ttl=0)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    key = plugin._vision_cache_key(
        url, event, img, prompt="自定义提示词", provider_id="vision/pid"
    )
    assert key is not None
    plugin._vision_cache[key] = (time.time(), "预置旧描述")

    out = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert "[缓存]" not in out, "ttl=0 不得命中缓存"
    assert out == "识图描述：页面顶部为导航栏。"
    plugin.context.llm_generate.assert_awaited_once()


# ============================================================
# URL 规范化 / 会话隔离 / fail-closed
# ============================================================

def test_cache_url_normalization_fragment_and_trailing_slash(tmp_path):
    """fragment 与尾斜杠差异视为同一 URL（v1.3.0 规范化语义保留）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    url_a = "https://example.com/page#section-2"
    url_b = "https://example.com/page/"
    url_c = "https://example.com/page"

    _run(plugin._describe_screenshot(img, event, page_url=url_a))
    out_b = _run(plugin._describe_screenshot(img, event, page_url=url_b))
    out_c = _run(plugin._describe_screenshot(img, event, page_url=url_c))
    assert out_b == "[缓存] 识图描述：页面顶部为导航栏。"
    assert out_c == "[缓存] 识图描述：页面顶部为导航栏。"
    plugin.context.llm_generate.assert_awaited_once()


def test_cache_session_isolated_by_umo(tmp_path):
    """不同会话（umo 不同）不共享缓存。"""
    plugin = _make_plugin(ttl=60)
    event_a = _Event(umo="grp:1:GroupMessage:aaa")
    event_b = _Event(umo="grp:1:GroupMessage:bbb")
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event_a, page_url=url))
    out = _run(plugin._describe_screenshot(img, event_b, page_url=url))
    assert out == "识图描述：页面顶部为导航栏。", "不同会话应重新识图"
    assert plugin.context.llm_generate.await_count == 2, "不同会话不得命中缓存"


def test_cache_keyed_without_event_not_cached(tmp_path):
    """无 event（无法确定会话）时不参与缓存。"""
    plugin = _make_plugin(ttl=60)
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, None, page_url=url))
    _run(plugin._describe_screenshot(img, None, page_url=url))
    assert plugin.context.llm_generate.await_count == 2, "无会话键不应命中缓存"
    assert plugin._vision_cache == {}


def test_cache_no_url_no_cache(tmp_path):
    """page_url 缺省（如旧调用方）时不缓存、不影响既有行为。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event))
    _run(plugin._describe_screenshot(img, event))
    assert plugin.context.llm_generate.await_count == 2
    assert plugin._vision_cache == {}


def test_cache_unreadable_image_not_cached(tmp_path):
    """图像字节读不到（文件不存在）⇒ 不参与缓存（fail-closed，不猜键）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    missing = str(Path(tmp_path) / "not-there.png")
    _run(plugin._describe_screenshot(missing, event, page_url=url))
    _run(plugin._describe_screenshot(missing, event, page_url=url))
    assert plugin.context.llm_generate.await_count == 2, "无内容哈希不得命中缓存"
    assert plugin._vision_cache == {}, "无内容哈希不得写缓存"


def test_normalize_url_edge_cases():
    """URL 规范化边界：空串、纯根路径、fragment 在中间。"""
    plugin = _make_plugin()
    norm = plugin._normalize_vision_cache_url
    assert norm("") == ""
    assert norm("   ") == ""
    assert norm("https://example.com") == "https://example.com"
    assert norm("https://example.com/") == "https://example.com"
    assert norm("https://example.com///") == "https://example.com"
    assert norm("https://example.com/a#frag") == "https://example.com/a"
    assert norm("https://example.com/a/#frag") == "https://example.com/a"


# ============================================================
# 键成分逐项独立生效
# ============================================================

def test_image_hash_len_is_hardcoded_32():
    """图像内容哈希位数写死为 32（128 bit），不做成配置项。"""
    assert _VISION_IMAGE_HASH_LEN == 32
    assert _VISION_PROMPT_HASH_LEN == 8


def test_cache_key_contains_real_image_bytes_hash(tmp_path):
    """键里的内容哈希 == sha256(真实文件字节)[:32]，且与路径无关。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img_a = _png(tmp_path, "a.png", b"AAAA")
    img_copy = _png(tmp_path, "copy-of-a.png", b"AAAA")
    img_b = _png(tmp_path, "b.png", b"BBBB")

    key_a = plugin._vision_cache_key(
        "https://e.com/p", event, img_a, prompt="p", provider_id="v"
    )
    key_copy = plugin._vision_cache_key(
        "https://e.com/p", event, img_copy, prompt="p", provider_id="v"
    )
    key_b = plugin._vision_cache_key(
        "https://e.com/p", event, img_b, prompt="p", provider_id="v"
    )

    assert len(key_a) == 7, "v1.6.1 键是七元组"
    assert key_a[4] == _sha_prefix(img_a) == _sha_prefix(img_copy)
    assert key_a[4] != key_b[4], "不同字节必须不同哈希"
    assert key_a == key_copy, "同内容不同路径 ⇒ 同一个键"
    assert key_a != key_b


def test_cache_key_distinguishes_tool_kind_and_crop(tmp_path):
    """工具种类与裁剪参数都进键：全图 / 不同裁剪 / 同裁剪 三种关系正确。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"SAME")

    shot = plugin._vision_cache_key(
        "https://e.com/p", event, img, kind="screenshot", prompt="p", provider_id="v"
    )
    crop1 = plugin._vision_cache_key(
        "https://e.com/p", event, img,
        kind="zoom_crop", crop=(250, 150, 800, 500), prompt="p", provider_id="v",
    )
    crop1_again = plugin._vision_cache_key(
        "https://e.com/p", event, img,
        kind="zoom_crop", crop=(250, 150, 800, 500), prompt="p", provider_id="v",
    )
    crop2 = plugin._vision_cache_key(
        "https://e.com/p", event, img,
        kind="zoom_crop", crop=(0, 0, 1280, 900), prompt="p", provider_id="v",
    )

    assert shot != crop1, "同一张字节的 screenshot 与 zoom_crop 必须不同键"
    assert crop1 == crop1_again, "同裁剪参数 ⇒ 同键"
    assert crop1 != crop2, "不同裁剪参数 ⇒ 不同键"
    assert shot[2] == "screenshot" and shot[3] == ""
    assert crop1[2] == "zoom_crop" and crop1[3] == "250,150,800,500"


def test_cache_key_includes_prompt_hash(tmp_path):
    """提示词进键（改 vision_prompt 后旧描述不再命中）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    key_a = plugin._vision_cache_key(
        "https://e.com/p", event, img, prompt="提示词甲", provider_id="v"
    )
    key_b = plugin._vision_cache_key(
        "https://e.com/p", event, img, prompt="提示词乙", provider_id="v"
    )
    assert key_a[5] == hashlib.sha256("提示词甲".encode()).hexdigest()[
        :_VISION_PROMPT_HASH_LEN
    ]
    assert key_a != key_b


def test_cache_key_includes_provider_id(tmp_path):
    """provider_id 进键（换识图 provider 后旧描述不再命中）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    key_a = plugin._vision_cache_key(
        "https://e.com/p", event, img, prompt="p", provider_id="vision/a"
    )
    key_b = plugin._vision_cache_key(
        "https://e.com/p", event, img, prompt="p", provider_id="vision/b"
    )
    assert key_a[6] == "vision/a"
    assert key_a != key_b


def test_cache_key_none_without_required_parts(tmp_path):
    """缺 URL / 缺会话 / 缺图像字节 ⇒ 键为 None（不读不写）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    assert plugin._vision_cache_key("", event, img, prompt="p") is None
    assert plugin._vision_cache_key("https://e.com/p", None, img, prompt="p") is None
    assert plugin._vision_cache_key("https://e.com/p", event, "", prompt="p") is None
    missing = str(Path(tmp_path) / "nope.png")
    assert plugin._vision_cache_key("https://e.com/p", event, missing, prompt="p") is None


def test_changing_vision_prompt_invalidates_cache(tmp_path):
    """端到端：面板改 vision_prompt ⇒ 同图同 URL 也重新识图。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event, page_url=url))
    hit = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert hit.startswith("[缓存] ")
    plugin.config["vision_prompt"] = "新提示词：逐字读出页面按钮"
    out = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert "[缓存]" not in out, "提示词变了必须重新识图"
    assert plugin.context.llm_generate.await_count == 2


def test_changing_vision_provider_invalidates_cache(tmp_path):
    """端到端：换识图 provider ⇒ 同图同 URL 也重新识图。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/a"), _Provider("vision/b")],
        inst_map={"vision/a": object(), "vision/b": object()},
    )
    plugin = _make_plugin(ttl=60, vision="vision/a", pm=pm)
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event, page_url=url))
    assert _run(
        plugin._describe_screenshot(img, event, page_url=url)
    ).startswith("[缓存] ")
    plugin.config["vision_provider_id"] = "vision/b"
    out = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert "[缓存]" not in out, "provider 变了必须重新识图"
    assert plugin.context.llm_generate.await_count == 2


def test_default_prompt_used_when_config_empty(tmp_path):
    """vision_prompt 留空 ⇒ 用模块级默认提示词（键里也是它）。"""
    plugin = _make_plugin(ttl=60, prompt="")
    event = _Event()
    img = _png(tmp_path, "a.png", b"A")
    _run(plugin._describe_screenshot(img, event, page_url="https://e.com/p"))
    kwargs = plugin.context.llm_generate.call_args.kwargs
    assert kwargs["prompt"] == _DEFAULT_VISION_PROMPT


# ============================================================
# 事故时间线回归：真识图 → crop → screenshot 三连不串号
# ============================================================

class _PicturePage:
    """可换「画面」的页面桩：URL 固定（SPA 语义），pixels 决定截图字节。"""

    def __init__(self, url="https://example.com/spa"):
        self.url = url
        self.viewport_size = {"width": 1280, "height": 800}
        self.pixels = _PNG_MAGIC + b"A"

    async def screenshot(self, clip=None, path=None, full_page=False):
        Path(path).write_bytes(self.pixels)
        return path


class _PictureBrowser:
    def __init__(self, page):
        self.page = page
        # _get_page 会校验 browser_type（非法值提前报错），桩给合法值。
        self.browser_type = "chromium"

    async def screenshot(self, page, save_path):
        Path(save_path).write_bytes(page.pixels)
        return save_path


class _Extractor:
    async def extract_page_info(self, page):
        return {"url": page.url, "title": "SPA 测试页"}

    async def extract_text(self, page, max_chars=4000):
        return "页面正文"


class _Sessions:
    def __init__(self, page):
        self._page = page

    async def ensure_page(self, umo):
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()


def _describe_of(out: str) -> str:
    """从工具返回文本里取出识图描述（识图桩按画面字节生成）。"""
    m = re.search(r"画面摘要 (\w+)", out or "")
    assert m, f"返回文本里没有识图描述：{out!r}"
    return m.group(1)


def _picture_llm():
    """识图桩：描述由**实际传入的图片字节**决定（串号会立刻暴露）。"""

    async def _gen(**kwargs):
        paths = kwargs.get("image_urls") or []
        assert paths, "识图桩需要 image_urls（conftest 桩环境无 contexts 通道）"
        digest = hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest()[:8]
        return SimpleNamespace(completion_text=f"画面摘要 {digest}")

    return _gen


def _make_tool_plugin(tmp_path, page, ttl, provider="vision/pid"):
    """装配 browse_screenshot / browse_zoom_crop 所需的最小插件。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.config = {
        "vision_provider_id": provider,
        "vision_prompt": "自定义提示词",
        "vision_cache_ttl": ttl,
    }
    plugin.vision_provider_id = provider
    plugin.vision_prompt = "自定义提示词"
    plugin.vision_cache_ttl = ttl
    plugin.silent_mode = True
    plugin.enable_banned_filter = True
    plugin.safety = None
    plugin._screenshot_dir = Path(tmp_path)
    plugin._vision_cache = {}
    plugin.browser = _PictureBrowser(page)
    plugin.extractor = _Extractor()
    plugin.sessions = _Sessions(page)
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._lock_for = lambda event: asyncio.Lock()

    async def _resolve(_event=None):
        return provider

    plugin._resolve_vision_provider_id = _resolve
    plugin._sync_vision_provider_options = lambda: None
    plugin.context = SimpleNamespace(llm_generate=_picture_llm())
    return plugin


@pytest.mark.parametrize("ttl", [0, 60])
def test_incident_timeline_screenshot_crop_screenshot_no_cross_talk(tmp_path, ttl):
    """事故时间线（04:49:08 → 04:49:12 → 04:49:22）：三连识图不得串号。

    ``vision_cache_ttl=0``（止血档）与 ``>0``（默认档）两种配置都必须绿：
    1. 真识图（画面 A）；2. crop（画面 B）；3. screenshot（画面 C）——
    三次描述两两不同，且都不带 [缓存]。
    随后同一画面再来一次：``ttl>0`` 必须命中（缓存没白做），``ttl=0``
    必须重算且不写缓存。
    """
    page = _PicturePage()
    plugin = _make_tool_plugin(tmp_path, page, ttl)
    event = _Event()

    page.pixels = _PNG_MAGIC + b"AAAA"
    out1 = _run(plugin.browse_screenshot(event))
    assert "识图结果" in out1 and "[缓存]" not in out1
    desc_a = _describe_of(out1)

    page.pixels = _PNG_MAGIC + b"BBBB"
    out2 = _run(plugin.browse_zoom_crop(event, 250, 150, 800, 500))
    assert "裁剪区域识图结果" in out2
    assert "[缓存]" not in out2, "crop 不得命中 screenshot 的缓存槽"
    desc_b = _describe_of(out2)

    page.pixels = _PNG_MAGIC + b"CCCC"
    out3 = _run(plugin.browse_screenshot(event))
    assert "[缓存]" not in out3, "画面变了必须重新识图"
    desc_c = _describe_of(out3)

    assert len({desc_a, desc_b, desc_c}) == 3, "三连识图不得互相串号"

    out4 = _run(plugin.browse_screenshot(event))
    if ttl > 0:
        assert out4 == (
            "截图已识图（静默模式，未发送）。识图结果：\n[缓存] 画面摘要 " + desc_c
        )
    else:
        assert "[缓存]" not in out4, "ttl=0 必须每次真识图"
        assert _describe_of(out4) == desc_c
        assert plugin._vision_cache == {}, "ttl=0 不得写缓存"


def test_same_bytes_different_tool_kind_no_shared_slot(tmp_path):
    """字节完全相同但工具不同（screenshot vs zoom_crop）也不共槽。

    事故里 04:49:08 与 04:49:12 两次 zoom_crop 是**同 URL、相近画面**，
    只有「工具种类 + 裁剪参数」能区分它们；这里用「字节刻意相同」把该维度
    单独钉死。
    """
    page = _PicturePage()
    page.pixels = _PNG_MAGIC + b"SAME"
    plugin = _make_tool_plugin(tmp_path, page, 60)
    event = _Event()

    shot = _run(plugin.browse_screenshot(event))
    assert "[缓存]" not in shot
    crop = _run(plugin.browse_zoom_crop(event, 0, 0, 640, 480))
    assert "[缓存]" not in crop, "同字节的 crop 不得命中 screenshot 的槽"
    crop2 = _run(plugin.browse_zoom_crop(event, 250, 150, 800, 500))
    assert "[缓存]" not in crop2, "同字节不同裁剪参数不得互相命中"
    crop2_again = _run(plugin.browse_zoom_crop(event, 250, 150, 800, 500))
    assert crop2_again.startswith("裁剪区域识图结果：\n[缓存] ")


# ============================================================
# 拒识 / 清理 / 生命周期
# ============================================================

def test_cache_rejection_not_stored(tmp_path):
    """拒识结果不写入缓存：同一 URL 二次识图仍走 LLM。"""
    plugin = _make_plugin(ttl=60)
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="[Unsupported Image]")
    )
    event = _Event()
    url = "https://example.com/page"
    img = _png(tmp_path, "a.png", b"A")
    out1 = _run(plugin._describe_screenshot(img, event, page_url=url))
    out2 = _run(plugin._describe_screenshot(img, event, page_url=url))
    assert out1 == _VISION_REJECTION_HINT
    assert out2 == _VISION_REJECTION_HINT
    assert plugin.context.llm_generate.await_count == 2, "拒识不应被缓存"
    assert plugin._vision_cache == {}


def test_cache_prune_expired_entries_on_overflow():
    """缓存条目超过阈值时清理过期项（防内存膨胀）。"""
    plugin = _make_plugin(ttl=60)
    now = time.time()
    # 灌入 300 条：250 条已过期、50 条未过期 + 1 条新写入 → 触发清理
    for i in range(250):
        plugin._vision_cache[(f"umo{i}", f"https://e.com/{i}")] = (now - 120, f"old{i}")
    for i in range(50):
        plugin._vision_cache[(f"umo{250 + i}", f"https://e.com/{250 + i}")] = (
            now - 1,
            f"new{i}",
        )
    key = ("umo-fresh", "https://e.com/fresh", "screenshot", "", "h", "p", "v")
    plugin._vision_cache_put(key, "最新描述")
    assert len(plugin._vision_cache) <= _VISION_CACHE_MAX_ENTRIES + 1
    assert all("old" not in v[1] for v in plugin._vision_cache.values()), (
        "过期条目应被清理"
    )
    assert plugin._vision_cache[key][1] == "最新描述", "新条目应写入且保留"


def test_cache_put_ignores_none_or_empty():
    plugin = _make_plugin(ttl=60)
    plugin._vision_cache_put(None, "x")
    plugin._vision_cache_put(("u", "https://e.com/"), "")
    assert plugin._vision_cache == {}


def test_terminate_clears_cache():
    """terminate 资源清理路径清空识图缓存（防重载残留）。"""
    plugin = _make_plugin(ttl=60)
    plugin.browser = None
    plugin.sessions = None
    plugin._vision_sync_task = None
    plugin._cache_cleanup_task = None
    plugin._vision_cache[
        ("umo", "https://e.com/x", "screenshot", "", "h", "p", "v")
    ] = (time.time(), "描述")
    _run(plugin.terminate())
    assert plugin._vision_cache == {}, "terminate 后缓存必须清空"
