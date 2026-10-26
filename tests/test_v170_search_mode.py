"""v1.7.0 搜索内外置双模式（规格 §4）。

一行定死：

- ``search.mode=external``（默认，= v1.6.0 行为逐字节一致）：池搜索是**外层**
  工具 ``browser_web_search``，主 LLM 直调；内层子代理**没有**池搜索工具；
- ``search.mode=internal``：池搜索是**内层**工具 ``browse_web_search``；
  外层同名工具**强制 active=False**（模型根本看不到它）。

**两档互斥，绝不"同名两份并存"** —— 同时有会让主 LLM 与内层都去搜、结果重复、
token 翻倍，还多一个"该用哪个"的决策面（``main.py`` 顶部 ``_SEARCH_TOOL_NAME``
的注释记过同类教训）。

"内层工具永不后台化"的依据：它不在 ``llm_tools.func_list`` 里，核心的
``is_background_task`` 机制看不到它；后台粒度只到外层工具。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import main as main_module
from main import (
    _INNER_SEARCH_MAX_BYTES,
    _INNER_SEARCH_TOOL_NAME,
    _SEARCH_TOOL_NAME,
    BrowserLLMPlugin,
)

OWN_MODULE = "astrbot_plugin_browser_llm.main"


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


class _Log:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def debug(self, *a, **k):
        self._add("debug", a)

    def info(self, *a, **k):
        self._add("info", a)

    def warning(self, *a, **k):
        self._add("warning", a)

    def error(self, *a, **k):
        self._add("error", a)

    @property
    def text(self):
        return "\n".join(text for _lvl, text in self.records)


class _Ctx:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def activate_llm_tool_async(self, name):
        self.calls.append(("activate", name))
        return True

    async def deactivate_llm_tool_async(self, name):
        self.calls.append(("deactivate", name))
        return True


def _config(mode="external", *, enabled=True, **overrides):
    search = {
        "mode": mode,
        "enabled": enabled,
        "providers": [{"__template_key": "brave", "api_key": "sk-test"}],
        "max_results": 5,
        "max_rounds": 2,
        "provider_timeout": 15,
        "total_timeout": 60,
        "cooldown_seconds": 0,
        "cache_ttl": 0,
        "fetch_pages": False,
        "fetch_top_n": 0,
        "fetch_timeout": 3,
        "fetch_max_chars": 2000,
    }
    search.update(overrides)
    return {"search": search}


def _plugin(mode="external", *, enabled=True, ctx=None, **overrides):
    cfg = _Cfg(_config(mode, enabled=enabled, **overrides))
    ctx = ctx or _Ctx()
    plugin = BrowserLLMPlugin(ctx, cfg)
    plugin.config = cfg
    plugin.context = ctx
    plugin.sessions = None
    return plugin


def _run(coro):
    return asyncio.run(coro)


def _registry(monkeypatch):
    from astrbot.core.provider.register import llm_tools

    fake: list = [SimpleNamespace(name=_SEARCH_TOOL_NAME, handler_module_path=OWN_MODULE, active=True)]
    monkeypatch.setattr(llm_tools, "func_list", fake, raising=False)
    return fake


def _inner_names(plugin) -> set[str]:
    return {getattr(t, "name", "") for t in plugin._browser_tools}


# ---------------------------------------------------------------
# 注册路径（两条，互斥）
# ---------------------------------------------------------------


def test_internal_mode_deactivates_outer_tool_and_registers_inner(monkeypatch):
    registry = _registry(monkeypatch)
    plugin = _plugin("internal")
    plugin._reconcile_search_tool = BrowserLLMPlugin._reconcile_search_tool.__get__(
        plugin
    )
    _run(plugin._sync_search_mode())
    deactivates = [c for c in plugin.context.calls if c[0] == "deactivate"]
    activates = [c for c in plugin.context.calls if c[0] == "activate"]
    assert deactivates == [("deactivate", _SEARCH_TOOL_NAME)]
    assert activates == [], "internal 档不许激活外层同名工具"
    assert plugin._search_tool_active is False
    assert _INNER_SEARCH_TOOL_NAME in _inner_names(plugin)
    assert len(plugin._browser_tools) == 27, "26 + 内层池搜索"
    assert registry, "func_list 里确实有那一个已注册对象（模拟 import 期注册）"


def test_external_mode_activates_outer_tool_and_no_inner(monkeypatch):
    _registry(monkeypatch)
    plugin = _plugin("external")
    _run(plugin._sync_search_mode())
    assert ("activate", _SEARCH_TOOL_NAME) in plugin.context.calls
    assert plugin._search_tool_active is True
    assert _INNER_SEARCH_TOOL_NAME not in _inner_names(plugin)
    assert len(plugin._browser_tools) == 26


def test_internal_without_pool_registers_no_inner_tool(monkeypatch):
    """``internal`` + ``search.enabled=false`` ⇒ 外层停用 + 内层也不注册。"""
    _registry(monkeypatch)
    plugin = _plugin("internal", enabled=False)
    _run(plugin._sync_search_mode())
    assert ("deactivate", _SEARCH_TOOL_NAME) in plugin.context.calls
    assert _INNER_SEARCH_TOOL_NAME not in _inner_names(plugin)
    assert len(plugin._browser_tools) == 26


def test_outer_entry_rejects_when_mode_internal(monkeypatch):
    """同名两份并存的反面：internal 档外层入口**显式拒绝**并指向 browse_web。"""
    _registry(monkeypatch)
    plugin = _plugin("internal")
    out = _run(plugin.browser_web_search(_Event(), query="今天天气"))
    payload = main_module.json.loads(out)
    assert payload["status"] == "disabled"
    assert payload["code"] == "handled_by_agent"
    assert payload["summary"].startswith("【拒绝】")
    assert "browse_web" in payload["hint"]


def test_external_mode_outer_entry_returns_raw_results(monkeypatch):
    """external 档维持现状：外层入口返回原始结构化 JSON（不是加工过的结论）。"""
    _registry(monkeypatch)
    plugin = _plugin("external")

    async def _fake_run(event, **kwargs):
        return plugin._search_payload(
            status="ok", code="ok", summary="搜到 1 条", query="q", results=[]
        )

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake_run)
    out = _run(plugin.browser_web_search(_Event(), query="q"))
    payload = main_module.json.loads(out)
    assert payload["status"] == "ok"
    assert payload["code"] == "ok"


# ---------------------------------------------------------------
# 内层工具本身
# ---------------------------------------------------------------


def test_inner_search_tool_shares_execution_body():
    """内层池搜索**复用**外层执行体（同一条安全/去重/冷却链路）。"""
    plugin = _plugin("internal")
    tool = plugin._make_inner_search_tool()
    assert tool.name == _INNER_SEARCH_TOOL_NAME
    assert tool.name != _SEARCH_TOOL_NAME, "内外层必须不同名（避免被内置族挤掉）"
    params = set(tool.parameters["properties"])
    assert params == {
        "query",
        "max_results",
        "rounds",
        "timeout",
        "fetch_pages",
        "fetch_top_n",
    }, "内层参数 = 外层去掉 providers（provider 顺序是运营配置）"


def test_inner_search_tool_uses_tighter_byte_budget(monkeypatch):
    """内层返回体上限 6000 字节（外层 24KB），并叠 agent.result_max_chars。"""
    plugin = _plugin("internal")
    captured = {}

    async def _fake(event, **kwargs):
        captured.update(kwargs)
        return "x" * 5000

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake)
    tool = plugin._make_inner_search_tool()
    out = _run(tool.handler(_Event(), query="q"))
    assert captured["max_bytes"] == _INNER_SEARCH_MAX_BYTES == 6000
    assert captured["timeout"] == 60, "取 min(search.total_timeout, agent_tool_timeout)"
    assert len(out) <= 3000 + 30, "还要叠 agent.result_max_chars 截断"


def test_inner_search_budget_takes_min_of_two_timeouts(monkeypatch):
    plugin = _plugin("internal", total_timeout=300)
    # 走配置而不是直接改属性：handler 开头会 _refresh_config() 重读配置
    plugin.config["limits"] = {"agent_tool_timeout": 45}
    plugin._refresh_config()
    captured = {}

    async def _fake(event, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake)
    _run(plugin._make_inner_search_tool().handler(_Event(), query="q"))
    assert captured["timeout"] == 45, "agent_tool_timeout 更小 ⇒ 取它"


def test_inner_search_tool_never_backgrounded(monkeypatch):
    """内层工具不走后台：``background_mode=all`` 时它仍在前台（嵌套在子代理里）。"""
    plugin = _plugin("internal")
    plugin.background_mode = "all"
    plugin.background_engine = "plugin"
    plugin.background_engine_effective = "plugin"
    called = {}

    async def _fake(event, **kwargs):
        called["ran"] = True
        return "ok"

    async def _boom(**kwargs):
        raise AssertionError("内层工具不得走 _submit_background")

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake)
    monkeypatch.setattr(plugin, "_submit_background", _boom)
    out = _run(plugin._make_inner_search_tool().handler(_Event(), query="q"))
    assert called["ran"] is True and out == "ok"


# ---------------------------------------------------------------
# 热更新与签名
# ---------------------------------------------------------------


def test_toolset_rebuild_only_on_signature_change(monkeypatch):
    """签名不变就不重建 26 个 FunctionTool（否则每次调用都白建一套）。"""
    plugin = _plugin("external")
    plugin._rebuild_browser_toolset()
    first = plugin._browser_tools
    assert plugin._apply_toolset_signature() is False
    assert plugin._browser_tools is first
    plugin.search_mode = "internal"
    assert plugin._apply_toolset_signature() is True
    assert _INNER_SEARCH_TOOL_NAME in _inner_names(plugin)


def test_mode_switch_is_hot_updated_through_refresh_config(monkeypatch):
    """``_refresh_config`` 之后再调入口 ⇒ 一次调用就换挡（无需重载插件）。"""
    _registry(monkeypatch)
    plugin = _plugin("external")
    _run(plugin._sync_search_mode())
    assert plugin._search_tool_active is True
    plugin.config["search"]["mode"] = "internal"
    plugin._refresh_config()
    assert plugin.search_mode == "internal"
    _run(plugin._sync_search_mode())
    assert plugin._search_tool_active is False
    assert ("deactivate", _SEARCH_TOOL_NAME) in plugin.context.calls


def test_invalid_mode_falls_back_to_external_with_warning(monkeypatch):
    log = _Log()
    monkeypatch.setattr(main_module, "logger", log)
    plugin = _plugin("INTERNALLY-TYPO")
    assert plugin.search_mode == "external"
    assert "search.mode=" in log.text and "非法" in log.text


def test_internal_reason_is_logged(monkeypatch):
    """internal 档的"外层不可见"要有一条**正常态**说明（防运营以为搜索坏了）。"""
    _registry(monkeypatch)
    log = _Log()
    monkeypatch.setattr(main_module, "logger", log)
    plugin = _plugin("internal")
    _run(plugin._sync_search_mode())
    assert "search tool inactive" in log.text
    assert "search.mode=internal" in log.text


# ---------------------------------------------------------------
# 静态守卫
# ---------------------------------------------------------------


def test_reconcile_is_not_write_amplified(monkeypatch):
    """签名不变就**不**再调一次 activate/deactivate（那是全局持久化写）。

    ``deactivate_llm_tool_async`` 会写 DB 里的 ``inactivated_llm_tools``；
    如果每次 ``browse_web`` 都对齐一次，等于每次工具调用都白写一次配置。
    """
    _registry(monkeypatch)
    plugin = _plugin("external")
    _run(plugin._sync_search_mode())
    calls_after_first = list(plugin.context.calls)
    assert calls_after_first, "首次必须真的对齐一次"
    for _ in range(3):
        _run(plugin._sync_search_mode())
    assert plugin.context.calls == calls_after_first, "签名不变时不得重复写"
    # 换挡 ⇒ 必须重新对齐
    plugin.config["search"]["mode"] = "internal"
    plugin._refresh_config()
    _run(plugin._sync_search_mode())
    assert plugin.context.calls[-1] == ("deactivate", _SEARCH_TOOL_NAME)


def test_single_rebuild_definition_and_single_deactivate_call():
    src = (main_module._PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    assert src.count("def _rebuild_browser_toolset") == 1
    # 对齐只有一处实现：context 上那两个方法是 getattr 取的，按字面量数字符串
    assert src.count('"deactivate_llm_tool_async"') == 1, "对齐只允许一处实现"
    assert src.count('"activate_llm_tool_async"') == 1
