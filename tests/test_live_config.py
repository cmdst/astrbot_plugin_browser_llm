"""配置热更新与本地页面锁清理测试（v1.2.0 P2）。

- _refresh_config：浏览工具入口重读共享 config dict（Dashboard 保存即
  update 的同一对象），并同步 SessionManager / SafetyFilter / BrowserCore
  运行期参数——黑名单、内网拦截、截图开关等修改无需重启即生效；
- _local_page_locks：弱引用字典，browse 结束后锁条目自动清理（防字典
  无限增长），持锁/等待期间条目保持存在，不破坏 per-umo 串行化。

依赖 conftest 的 astrbot 桩；弱引用清理依赖 CPython 引用计数（本套件
运行环境即 CPython）。
"""

import asyncio
from types import SimpleNamespace

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from core.safety import SafetyFilter
from main import BrowserLLMPlugin, _make_browser_tool


def _make_plugin(**overrides):
    cfg = {
        "banned_words": ["赌博"],
        "block_internal_ip": True,
        "max_pages": 5,
        "idle_timeout": 1800,
        "default_url": "https://www.baidu.com",
        "session_whitelist": [],
        "session_blacklist": [],
        "enable_screenshot": True,
        "silent_mode": True,
    }
    cfg.update(overrides)
    return BrowserLLMPlugin(context=Context(), config=cfg)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------
# 需求 4：配置热更新（_refresh_config）
# ------------------------------------------------------------

def test_refresh_config_syncs_session_manager_limits():
    """max_pages / idle_timeout / default_url 热同步进 SessionManager。"""
    plugin = _make_plugin(max_pages=3, idle_timeout=600)
    plugin.sessions = SimpleNamespace(max_pages=99, idle_timeout=99, default_url="x")
    plugin.config["max_pages"] = 8
    plugin.config["idle_timeout"] = 300
    plugin.config["default_url"] = "https://example.com"
    plugin._refresh_config()
    assert plugin.sessions.max_pages == 8
    assert plugin.sessions.idle_timeout == 300
    assert plugin.sessions.default_url == "https://example.com"


def test_refresh_config_syncs_safety_filter():
    """禁词与内网拦截开关热同步进 SafetyFilter，且立即生效。"""
    plugin = _make_plugin(banned_words=["赌博"], block_internal_ip=True)
    plugin.safety = SafetyFilter(["赌博"], True)
    plugin.config["banned_words"] = ["色情", "赌博"]
    plugin.config["block_internal_ip"] = False
    plugin._refresh_config()
    assert plugin.safety.banned_words == ["色情", "赌博"]
    assert plugin.safety.block_internal_ip is False
    ok, word = plugin.safety.check_text("页面包含色情内容")
    assert ok is False and word == "色情", "新禁词应立即生效"


def test_refresh_config_syncs_browser_ssrf_flag():
    """block_internal_ip 热同步进 BrowserCore（作用于新建 context）。"""
    plugin = _make_plugin(block_internal_ip=True)
    plugin.browser = SimpleNamespace(block_internal_ip=True)
    plugin.config["block_internal_ip"] = False
    plugin._refresh_config()
    assert plugin.browser.block_internal_ip is False


def test_refresh_config_reloads_blacklist_attrs():
    """黑名单实例属性随配置重读刷新（入口判断立即使用新值）。"""
    plugin = _make_plugin(session_blacklist=["111"])
    plugin.config["session_blacklist"] = ["222"]
    plugin._refresh_config()
    assert plugin.session_blacklist == ["222"]


def test_refresh_config_tolerates_uninitialized_components():
    """initialize 之前调用（sessions/safety/browser 为 None）不抛异常。"""
    plugin = _make_plugin()
    plugin.config["session_blacklist"] = ["123"]
    plugin._refresh_config()
    assert plugin.session_blacklist == ["123"], "实例属性已刷新"


def test_browse_web_entry_refreshes_config():
    """browse_web 入口先热更新配置再执行（拒绝路径也触发）。"""
    plugin = _make_plugin()
    calls = []
    plugin._refresh_config = lambda: calls.append(1)
    plugin._sync_vision_provider_options = lambda: None
    plugin._is_session_allowed = lambda e: (False, "deny")
    result = _run(plugin.browse_web(object(), input="x"))
    assert result == "【拒绝】deny"
    assert calls == [1], "browse_web 入口应调用 _refresh_config"


def test_browse_local_page_entry_refreshes_config():
    """browse_local_page 入口先热更新配置（空路径提前返回也触发）。"""
    plugin = _make_plugin()
    calls = []
    plugin._refresh_config = lambda: calls.append(1)
    event = AstrMessageEvent()
    result = _run(plugin.browse_local_page(event, path=""))
    assert "path 不能为空" in result
    assert calls == [1], "browse_local_page 入口应调用 _refresh_config"


def test_tool_handler_refreshes_config_before_call():
    """子代理工具 handler 调用前先热更新配置（_make_browser_tool 包装）。"""
    plugin = _make_plugin()
    calls = []
    plugin._refresh_config = lambda: calls.append(1)

    async def fake_browse(event, **kwargs):
        return "ok"

    plugin.browse_open = fake_browse  # 实例属性覆盖，验证包装层调用顺序
    spec = {
        "name": "browse_open",
        "description": "d",
        "parameters": {},
        "method": "browse_open",
    }
    tool = _make_browser_tool(plugin, spec)
    ctx = SimpleNamespace(context=SimpleNamespace(event=None))
    result = _run(tool.call(context=ctx))
    assert result == "ok"
    assert calls == [1], "工具调用前应先热更新配置"


# ------------------------------------------------------------
# 需求 5：本地页面锁表清理（弱引用字典）
# ------------------------------------------------------------

def test_local_lock_entry_removed_after_browse():
    """锁无任何强引用后条目自动清理（browse 结束即释放）。"""
    plugin = _make_plugin()
    umo = "grp:1:GroupMessage:123"
    lock = plugin._local_lock_for(umo)
    assert umo in plugin._local_page_locks
    assert lock.locked() is False
    del lock  # 仅剩字典弱引用 → 条目自动清理
    assert umo not in plugin._local_page_locks, "无引用后条目应被清理"


def test_local_lock_entry_persists_while_held():
    """持锁期间条目必须存在；释放且无引用后清理。"""
    plugin = _make_plugin()
    umo = "grp:1:GroupMessage:123"

    async def _go():
        lock = plugin._local_lock_for(umo)
        await lock.acquire()
        try:
            assert umo in plugin._local_page_locks, "持锁期间条目必须存在"
            assert plugin._local_lock_for(umo) is lock, "持锁期间必须复用同一锁"
        finally:
            if lock.locked():
                lock.release()

    _run(_go())
    assert umo not in plugin._local_page_locks, "释放且无引用后条目应被清理"


def test_local_lock_same_object_while_referenced():
    """并发期间（锁仍被引用）多次获取返回同一锁，串行化不被破坏。"""
    plugin = _make_plugin()
    umo = "grp:1:GroupMessage:123"
    l1 = plugin._local_lock_for(umo)
    l2 = plugin._local_lock_for(umo)
    assert l1 is l2, "同会话并发期间应复用同一锁"
    del l1, l2
    assert umo not in plugin._local_page_locks


def test_local_lock_cleared_on_terminate():
    """terminate 显式清空锁表（兜底）。"""
    plugin = _make_plugin()
    plugin.browser = None
    plugin.sessions = None
    # terminate 需要后台任务属性（正常流程由 initialize 设置）。
    plugin._vision_sync_task = None
    plugin._cache_cleanup_task = None
    umo = "grp:1:GroupMessage:123"
    lock = plugin._local_lock_for(umo)
    assert umo in plugin._local_page_locks
    _run(plugin.terminate())
    assert umo not in plugin._local_page_locks or lock.locked() is False
    assert len(plugin._local_page_locks) == 0
