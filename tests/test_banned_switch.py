"""v1.3.3 内容审核总开关（enable_banned_filter）与识图结果禁词覆盖测试。

覆盖：
- 开关收口：_check_banned 受 enable_banned_filter 控制——默认开启时命中
  返回禁词，关闭后统一放行（正文/标题/搜索词/URL/识图结果等全部拦截点
  均经此一处收口）；
- 识图旁路修复：browse_screenshot / browse_zoom_crop 识图结果与页面正文
  同口径过禁词（对齐 browse_local_page 既有做法）；
- 热更新：_refresh_config 后开关即时生效；
- 安全边界不受影响：开关关闭时 SSRF 内网拦截仍然生效。

依赖 conftest 的 astrbot 桩。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace


from astrbot.api.star import Context
from core.safety import SafetyFilter
from main import BrowserLLMPlugin

BANNED = ["赌博"]


def _run(coro):
    return asyncio.run(coro)


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:1234"

    def get_group_id(self):
        return "1234"

    def get_sender_id(self):
        return "5678"


class FakePage:
    url = "https://example.com/page"
    viewport_size = {"width": 1280, "height": 800}

    async def screenshot(self, **kwargs):
        return True


class FakeBrowser:
    browser_type = "chromium"

    async def screenshot(self, page, save_path):
        return save_path


class Sessions:
    def __init__(self, page):
        self._page = page

    async def ensure_page(self, umo):
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()

    def tab_count(self, umo):
        return 1


def _full_plugin(**overrides):
    """走真实 __init__/_load_config 的插件（开关默认值/归一化链路可测）。

    SafetyFilter 在 initialize() 才创建，此处按 _load_config 结果手动
    装配（与 test_live_config 的做法一致），保证 _check_banned 可用。
    """
    cfg = {
        "banned_words": list(BANNED),
        "session_whitelist": [],
        "session_blacklist": [],
    }
    cfg.update(overrides)
    plugin = BrowserLLMPlugin(context=Context(), config=cfg)
    plugin.safety = SafetyFilter(list(cfg["banned_words"]), block_internal_ip=True)
    return plugin


def _fake_extractor(text="赌博论坛热帖正文", title="正常标题"):
    async def info(page):
        return {"title": title, "url": "https://example.com"}

    async def get_text(page, max_chars=4000):
        return text

    return SimpleNamespace(extract_page_info=info, extract_text=get_text)


def _tool_plugin(enable_banned_filter=True, desc="正常识图描述"):
    """__new__ 直构的最小插件桩（browse_screenshot / browse_zoom_crop 链路）。"""
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = SafetyFilter(BANNED, block_internal_ip=True)
    plugin.enable_banned_filter = enable_banned_filter
    plugin.sessions = Sessions(FakePage())
    plugin.browser = FakeBrowser()
    plugin.silent_mode = True
    plugin._screenshot_dir = Path("/tmp")
    plugin._vision_cache = {}
    plugin.vision_cache_ttl = 0

    # 桩签名与真实 _describe_screenshot 对齐（v1.6.1 起多了 kind / crop
    # 两个进缓存键的关键字参数，这里用 **kwargs 吞掉，只关心返回文本）。
    async def _fake_desc(image_path, event=None, page_url=None, **kwargs):
        return desc

    plugin._describe_screenshot = _fake_desc
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    return plugin


# ------------------------------------------------------------
# 开关收口：_check_banned
# ------------------------------------------------------------

def test_switch_default_on_check_banned_hits():
    """默认开启（缺省配置）：命中返回禁词原文，干净文本返回 None。"""
    plugin = _full_plugin()
    assert plugin.enable_banned_filter is True
    assert plugin._check_banned("这里有赌博内容") == "赌博"
    assert plugin._check_banned("正常内容") is None


def test_switch_off_check_banned_always_none():
    """关闭开关：_check_banned 统一放行，不再因禁词拦截。"""
    plugin = _full_plugin(enable_banned_filter=False)
    assert plugin._check_banned("这里有赌博内容") is None


def test_switch_none_value_falls_back_default_on():
    """非法/缺省值经 _as_bool 归一：None 回退默认 True（不阻断加载）。"""
    plugin = _full_plugin(enable_banned_filter=None)
    assert plugin.enable_banned_filter is True


def test_page_summary_passthrough_when_off():
    """关闭开关：含禁词的页面正文照常返回（不再整页拒绝）。"""
    plugin = _full_plugin(enable_banned_filter=False)
    plugin.extractor = _fake_extractor()
    summary = _run(plugin._page_summary(FakePage()))
    assert "【拒绝】" not in summary
    assert "赌博论坛热帖正文" in summary


def test_page_summary_still_rejects_when_on():
    """默认开启：含禁词正文仍整页拒绝（与 v1.3.2 行为一致）。"""
    plugin = _full_plugin()
    plugin.extractor = _fake_extractor()
    summary = _run(plugin._page_summary(FakePage()))
    assert "【拒绝】页面正文包含违禁内容：赌博" in summary


# ------------------------------------------------------------
# 识图旁路修复：browse_screenshot / browse_zoom_crop
# ------------------------------------------------------------

def test_screenshot_banned_desc_rejected():
    """默认开启：识图结果含禁词返回【拒绝】，不透传给 LLM/用户。"""
    plugin = _tool_plugin(desc="页面截图上是赌博广告")
    result = _run(plugin.browse_screenshot(FakeEvent()))
    assert "【拒绝】截图识图结果包含违禁内容：赌博" in result


def test_screenshot_banned_desc_allowed_when_off():
    """关闭开关：同样的识图结果放行（论坛等敏感词密集站点场景）。"""
    plugin = _tool_plugin(enable_banned_filter=False, desc="页面截图上是赌博广告")
    result = _run(plugin.browse_screenshot(FakeEvent()))
    assert "赌博广告" in result
    assert "【拒绝】" not in result


def test_screenshot_clean_desc_normal():
    """干净识图描述：输出结构与 v1.3.2 一致。"""
    plugin = _tool_plugin(desc="页面主体为搜索结果列表")
    result = _run(plugin.browse_screenshot(FakeEvent()))
    assert "截图已识图" in result
    assert "页面主体为搜索结果列表" in result


def test_zoom_crop_banned_desc_rejected():
    """默认开启：裁剪区域识图结果含禁词返回【拒绝】。"""
    plugin = _tool_plugin(desc="放大区域可见赌博网站链接")
    result = _run(plugin.browse_zoom_crop(FakeEvent(), 0, 0, 100, 100))
    assert "【拒绝】裁剪区域识图结果包含违禁内容：赌博" in result


def test_zoom_crop_banned_desc_allowed_when_off():
    """关闭开关：裁剪区域识图结果放行。"""
    plugin = _tool_plugin(enable_banned_filter=False, desc="放大区域可见赌博网站链接")
    result = _run(plugin.browse_zoom_crop(FakeEvent(), 0, 0, 100, 100))
    assert "裁剪区域识图结果：\n放大区域可见赌博网站链接" in result


# ------------------------------------------------------------
# 热更新与安全边界
# ------------------------------------------------------------

def test_refresh_config_hot_flips_switch():
    """Dashboard 保存配置（就地 update 同一 dict）后 _refresh_config 即生效。"""
    plugin = _full_plugin()
    plugin.config["enable_banned_filter"] = False
    plugin._refresh_config()
    assert plugin.enable_banned_filter is False
    assert plugin._check_banned("赌博") is None
    plugin.config["enable_banned_filter"] = True
    plugin._refresh_config()
    assert plugin.enable_banned_filter is True
    assert plugin._check_banned("赌博") == "赌博"


def test_ssrf_unaffected_when_switch_off():
    """关闭审核开关不影响 SSRF 边界：内网地址仍被 acheck_url 拦截。"""
    plugin = _full_plugin(enable_banned_filter=False)
    assert plugin._check_banned("赌博") is None
    ok, _reason = _run(plugin.safety.acheck_url("http://127.0.0.1:8080/admin"))
    assert ok is False
