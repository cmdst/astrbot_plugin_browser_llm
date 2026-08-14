"""识图短时缓存测试（v1.3.0 P1）。

覆盖：
- 同 URL + 同会话在 TTL 内命中缓存（返回文本带 [缓存] 前缀，不再调用
  llm_generate，可观测 debug 日志）；
- URL 规范化：fragment / 尾斜杠差异视为同一 URL；
- 会话隔离：不同 umo 不共享缓存；
- TTL 过期：超过 TTL 后重新调用 LLM；
- vision_cache_ttl=0 关闭缓存（每次调用 LLM、不写缓存）；
- 拒识结果不写入缓存；
- 缺 page_url / 无会话时不参与缓存；
- 缓存条目超阈值时清理过期项（防内存膨胀）；
- terminate 清空缓存（防重载残留）。

依赖 conftest 的 astrbot 桩。
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main as main_module
from main import (
    _VISION_CACHE_MAX_ENTRIES,
    _VISION_REJECTION_HINT,
    BrowserLLMPlugin,
)


def _run(coro):
    return asyncio.run(coro)


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


def _make_plugin(ttl=60, vision="vision/pid", pm=None):
    """构造带识图 provider 与 TTL 配置的最小插件实例。"""
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
        "vision_prompt": "自定义提示词",
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


# ------------------------------------------------------------
# 命中 / 未命中 / TTL 过期
# ------------------------------------------------------------

def test_cache_hit_within_ttl(monkeypatch):
    """同 URL 同会话在 TTL 内二次识图命中缓存，不再调用 LLM。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    debugs = _capture_logger(monkeypatch, "debug")
    url = "https://example.com/page"

    first = _run(plugin._describe_screenshot("/tmp/a.png", event, page_url=url))
    second = _run(plugin._describe_screenshot("/tmp/b.png", event, page_url=url))

    assert first == "识图描述：页面顶部为导航栏。"
    assert second == "[缓存] 识图描述：页面顶部为导航栏。", "缓存命中应带 [缓存] 前缀"
    plugin.context.llm_generate.assert_awaited_once(), "命中缓存不得再次调用 LLM"
    assert any("缓存命中" in d for d in debugs), "应有缓存命中日志标注"


def test_cache_miss_after_ttl_expiry():
    """超过 TTL 后重新识图（过期条目删除并重写）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url = "https://example.com/page"
    key = (event.unified_msg_origin, plugin._normalize_vision_cache_url(url))
    # 预置一条已过期条目（61s 前写入）
    plugin._vision_cache[key] = (time.time() - 61, "过期描述")

    out = _run(plugin._describe_screenshot("/tmp/a.png", event, page_url=url))
    assert out == "识图描述：页面顶部为导航栏。", "过期后应重新识图"
    assert "过期描述" not in out
    plugin.context.llm_generate.assert_awaited_once()
    assert key in plugin._vision_cache, "重新识图结果应回写缓存"
    assert plugin._vision_cache[key][1] == "识图描述：页面顶部为导航栏。"


def test_cache_disabled_when_ttl_zero():
    """vision_cache_ttl=0：每次识图都调用 LLM，不读不写缓存。"""
    plugin = _make_plugin(ttl=0)
    event = _Event()
    url = "https://example.com/page"
    _run(plugin._describe_screenshot("/tmp/a.png", event, page_url=url))
    _run(plugin._describe_screenshot("/tmp/b.png", event, page_url=url))
    assert plugin.context.llm_generate.await_count == 2, "关闭缓存应每次都识图"
    assert plugin._vision_cache == {}, "关闭缓存不得写入任何条目"


# ------------------------------------------------------------
# URL 规范化 / 会话隔离
# ------------------------------------------------------------

def test_cache_url_normalization_fragment_and_trailing_slash():
    """fragment 与尾斜杠差异视为同一 URL（验收标准 3 的规范化语义）。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    url_a = "https://example.com/page#section-2"
    url_b = "https://example.com/page/"
    url_c = "https://example.com/page"

    _run(plugin._describe_screenshot("/tmp/a.png", event, page_url=url_a))
    out_b = _run(plugin._describe_screenshot("/tmp/b.png", event, page_url=url_b))
    out_c = _run(plugin._describe_screenshot("/tmp/c.png", event, page_url=url_c))
    assert out_b == "[缓存] 识图描述：页面顶部为导航栏。"
    assert out_c == "[缓存] 识图描述：页面顶部为导航栏。"
    plugin.context.llm_generate.assert_awaited_once()


def test_cache_session_isolated_by_umo():
    """不同会话（umo 不同）不共享缓存。"""
    plugin = _make_plugin(ttl=60)
    event_a = _Event(umo="grp:1:GroupMessage:aaa")
    event_b = _Event(umo="grp:1:GroupMessage:bbb")
    url = "https://example.com/page"
    _run(plugin._describe_screenshot("/tmp/a.png", event_a, page_url=url))
    out = _run(plugin._describe_screenshot("/tmp/b.png", event_b, page_url=url))
    assert out == "识图描述：页面顶部为导航栏。", "不同会话应重新识图"
    assert plugin.context.llm_generate.await_count == 2, "不同会话不得命中缓存"


def test_cache_keyed_without_event_not_cached():
    """无 event（无法确定会话）时不参与缓存。"""
    plugin = _make_plugin(ttl=60)
    url = "https://example.com/page"
    _run(plugin._describe_screenshot("/tmp/a.png", None, page_url=url))
    _run(plugin._describe_screenshot("/tmp/b.png", None, page_url=url))
    assert plugin.context.llm_generate.await_count == 2, "无会话键不应命中缓存"
    assert plugin._vision_cache == {}


def test_cache_no_url_no_cache():
    """page_url 缺省（如旧调用方）时不缓存、不影响既有行为。"""
    plugin = _make_plugin(ttl=60)
    event = _Event()
    _run(plugin._describe_screenshot("/tmp/a.png", event))
    _run(plugin._describe_screenshot("/tmp/b.png", event))
    assert plugin.context.llm_generate.await_count == 2
    assert plugin._vision_cache == {}


def test_normalize_url_edge_cases():
    """URL 规范化边界：空串、纯根路径、fragment 在中间。"""
    plugin = _make_plugin()
    assert plugin._normalize_vision_cache_url("") == ""
    assert plugin._normalize_vision_cache_url("   ") == ""
    assert plugin._normalize_vision_cache_url("https://example.com") == "https://example.com"
    assert plugin._normalize_vision_cache_url("https://example.com/") == "https://example.com"
    assert plugin._normalize_vision_cache_url("https://example.com///") == "https://example.com"
    assert plugin._normalize_vision_cache_url("https://example.com/a#frag") == "https://example.com/a"
    assert plugin._normalize_vision_cache_url("https://example.com/a/#frag") == "https://example.com/a"


# ------------------------------------------------------------
# 拒识 / 清理 / 生命周期
# ------------------------------------------------------------

def test_cache_rejection_not_stored():
    """拒识结果不写入缓存：同一 URL 二次识图仍走 LLM。"""
    plugin = _make_plugin(ttl=60)
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="[Unsupported Image]")
    )
    event = _Event()
    url = "https://example.com/page"
    out1 = _run(plugin._describe_screenshot("/tmp/a.png", event, page_url=url))
    out2 = _run(plugin._describe_screenshot("/tmp/b.png", event, page_url=url))
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
        plugin._vision_cache[(f"umo{250+i}", f"https://e.com/{250+i}")] = (now - 1, f"new{i}")
    key = ("umo-fresh", "https://e.com/fresh")
    plugin._vision_cache_put(key, "最新描述")
    assert len(plugin._vision_cache) <= _VISION_CACHE_MAX_ENTRIES + 1
    assert all("old" not in v[1] for v in plugin._vision_cache.values()), \
        "过期条目应被清理"
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
    plugin._vision_cache[("umo", "https://e.com/x")] = (time.time(), "描述")
    _run(plugin.terminate())
    assert plugin._vision_cache == {}, "terminate 后缓存必须清空"
