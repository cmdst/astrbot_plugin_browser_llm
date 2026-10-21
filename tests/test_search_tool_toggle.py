"""搜索工具可见性（开关）测试：activate/deactivate 对齐 + 降级 + 名称冲突。

对应 spec §6.2 与 §13 的 AC-08…AC-15。核心事实（部署树 v4.29.1 实测）：

- ``@filter.llm_tool`` 在 **import 期**就注册（``star_handler.py:667-670``），
  平台没有"按配置注册"的口子 ⇒ 只能切 ``active``；
- 请求装配期核心会剔除 ``active=False`` 的工具（``astr_main_agent.py:650-665``）
  ⇒ ``active=False`` 等价于"模型根本不知道有这个工具"；
- ``deactivate_llm_tool_async`` 会写全局持久化名单 ``inactivated_llm_tools``，
  所以配置必须每次 ``initialize()`` 双向收敛（配置 = 唯一事实源）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main as main_module
from main import BrowserLLMPlugin, _SEARCH_TOOL_NAME

OWN_MODULE = "astrbot_plugin_browser_llm.main"
OTHER_MODULE = "some_other_plugin.main"


class FakeContext:
    """最小 Context 替身：只记录 activate/deactivate 调用。"""

    def __init__(self, *, with_toggle: bool = True):
        self.calls: list[tuple[str, str]] = []
        if with_toggle:
            self.activate_llm_tool_async = self._activate
            self.deactivate_llm_tool_async = self._deactivate

    async def _activate(self, name):
        self.calls.append(("activate", name))
        return True

    async def _deactivate(self, name):
        self.calls.append(("deactivate", name))
        return True


class RecordingLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def info(self, *a, **k):
        self._add("info", a)

    def warning(self, *a, **k):
        self._add("warning", a)

    def error(self, *a, **k):
        self._add("error", a)

    def debug(self, *a, **k):
        self._add("debug", a)

    @property
    def text(self):
        return "\n".join(text for _lvl, text in self.records)


def search_config(**overrides) -> dict:
    search = {
        "enabled": True,
        "providers": [{"__template_key": "brave", "api_key": "sk-test-key-1"}],
        "max_parallel": 3,
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
        "region": "",
        "safesearch": "moderate",
        "proxy": "",
    }
    search.update(overrides)
    return {"search": search}


def make_plugin(context=None, **overrides) -> BrowserLLMPlugin:
    plugin = BrowserLLMPlugin(context, search_config(**overrides))
    plugin.context = context if context is not None else FakeContext()
    return plugin


@pytest.fixture()
def registry(monkeypatch):
    """替换 conftest 桩里的 llm_tools.func_list，按用例登记"已注册工具"。"""
    from astrbot.core.provider.register import llm_tools

    fake_list: list = []
    monkeypatch.setattr(llm_tools, "func_list", fake_list, raising=False)
    return fake_list


@pytest.fixture()
def recorder(monkeypatch):
    log = RecordingLogger()
    monkeypatch.setattr(main_module, "logger", log)
    return log


def own_tool(name=_SEARCH_TOOL_NAME, module=OWN_MODULE) -> SimpleNamespace:
    return SimpleNamespace(name=name, handler_module_path=module, active=True)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# AC-08 / AC-09 / AC-10：双向对齐
# ---------------------------------------------------------------


def test_activate_when_enabled_and_key_present(registry, recorder):
    registry.append(own_tool())
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("activate", _SEARCH_TOOL_NAME)]
    assert plugin._search_tool_active is True
    assert "搜索工具已启用" in recorder.text


def test_deactivate_when_disabled(registry, recorder):
    registry.append(own_tool())
    plugin = make_plugin(enabled=False)

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("deactivate", _SEARCH_TOOL_NAME)]
    assert plugin._search_tool_active is False
    assert "search tool inactive" in recorder.text
    assert "search.enabled=false" in recorder.text


def test_deactivate_when_pool_has_no_usable_provider(registry, recorder):
    """AC-10：enabled=true 但池空/全空 key ⇒ 自动闸门关掉（日志可查）。"""
    registry.append(own_tool())
    plugin = make_plugin(providers=[{"__template_key": "brave", "api_key": ""}])

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("deactivate", _SEARCH_TOOL_NAME)]
    assert "search tool inactive" in recorder.text
    assert "无可用 provider" in recorder.text


def test_deactivate_when_all_providers_disabled(registry):
    registry.append(own_tool())
    plugin = make_plugin(
        providers=[{"__template_key": "brave", "api_key": "k", "enabled": False}]
    )
    run(plugin._reconcile_search_tool())
    assert plugin.context.calls == [("deactivate", _SEARCH_TOOL_NAME)]


def test_deactivate_when_base_url_is_bad_config(registry):
    registry.append(own_tool())
    plugin = make_plugin(
        providers=[
            {"__template_key": "brave", "api_key": "k", "base_url": "http://127.0.0.1/x"}
        ]
    )
    run(plugin._reconcile_search_tool())
    assert plugin.context.calls == [("deactivate", _SEARCH_TOOL_NAME)]


def test_config_is_single_source_of_truth(registry, recorder):
    """AC-11：名单说关（active=False）但配置说开 ⇒ 必须被 activate 回来。"""
    tool = own_tool()
    tool.active = False  # 模拟 inactivated_llm_tools 里的持久化残留
    registry.append(tool)
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert ("activate", _SEARCH_TOOL_NAME) in plugin.context.calls
    assert plugin._search_tool_active is True


def test_config_off_deactivates_even_if_tool_active(registry):
    tool = own_tool()
    tool.active = True
    registry.append(tool)
    plugin = make_plugin(enabled=False)

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("deactivate", _SEARCH_TOOL_NAME)]


def test_tool_not_registered_only_updates_shadow(registry):
    """工具尚未注册（桩环境/import 期）：只记影子值，不误调上下文。"""
    plugin = make_plugin()
    run(plugin._reconcile_search_tool())
    assert plugin.context.calls == []
    assert plugin._search_tool_active is True


# ---------------------------------------------------------------
# AC-14 / AC-15：降级与名称冲突
# ---------------------------------------------------------------


def test_fallback_when_context_lacks_toggle(registry, recorder):
    """AC-14：Context 没有 activate/deactivate ⇒ 常驻注册 + 入口兜底。"""
    registry.append(own_tool())
    plugin = make_plugin(context=FakeContext(with_toggle=False))

    run(plugin._reconcile_search_tool())

    assert plugin._search_tool_active is True
    assert "降级为常驻注册" in recorder.text


def test_fallback_disabled_still_marks_inactive(registry):
    registry.append(own_tool())
    plugin = make_plugin(context=FakeContext(with_toggle=False), enabled=False)
    run(plugin._reconcile_search_tool())
    assert plugin._search_tool_active is False


def test_own_tool_detected_from_handler_module(registry, recorder):
    """import 期 ``handler_module_path`` 为空：靠 ``handler.__module__`` 认领。"""
    import main as main_module_alias

    tool = SimpleNamespace(
        name=_SEARCH_TOOL_NAME,
        handler_module_path=None,
        handler=main_module_alias.BrowserLLMPlugin.browser_web_search,
        active=True,
    )
    assert tool.handler.__module__ == main_module_alias.__name__
    registry.append(tool)
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("activate", _SEARCH_TOOL_NAME)]


def test_own_tool_detected_when_handler_is_partial(registry):
    """载入后 handler 是 ``partial(handler, star_cls)``：partial 自身无 __module__。"""
    import functools

    import main as main_module_alias

    tool = SimpleNamespace(
        name=_SEARCH_TOOL_NAME,
        handler_module_path=None,
        handler=functools.partial(
            main_module_alias.BrowserLLMPlugin.browser_web_search, object()
        ),
        active=True,
    )
    registry.append(tool)
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == [("activate", _SEARCH_TOOL_NAME)]


def test_name_clash_is_fail_closed(registry, recorder):
    """AC-15：别人家注册了同名工具 ⇒ 不碰 active，只报错。"""
    registry.append(own_tool(module=OTHER_MODULE))
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == []
    assert plugin._search_tool_active is False
    assert "名称冲突" in recorder.text
    assert OTHER_MODULE in recorder.text


def test_name_clash_does_not_block_when_own_tool_also_present(registry, recorder):
    registry.append(own_tool(module=OWN_MODULE))
    registry.append(own_tool(module=OTHER_MODULE))
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    assert plugin.context.calls == []
    assert "名称冲突" in recorder.text


# ---------------------------------------------------------------
# AC-12：关掉后"模型看不到"（按核心装配规则模拟）
# ---------------------------------------------------------------


def _visible_tool_names(funcs):
    """复刻 ``astr_main_agent.py:650-665`` 的过滤：只保留 active 的工具。"""
    return [f.name for f in funcs if getattr(f, "active", True)]


def test_inactive_tool_not_visible_to_model(registry):
    tool = own_tool()
    registry.append(tool)
    plugin = make_plugin(enabled=False)

    run(plugin._reconcile_search_tool())

    # 核心装配时会剔除 active=False ⇒ 模型根本看不到该工具
    for func in registry:
        if func.name == _SEARCH_TOOL_NAME:
            func.active = plugin._search_tool_active
    assert _SEARCH_TOOL_NAME not in _visible_tool_names(registry)


def test_active_tool_visible_to_model(registry):
    tool = own_tool()
    registry.append(tool)
    plugin = make_plugin()

    run(plugin._reconcile_search_tool())

    for func in registry:
        if func.name == _SEARCH_TOOL_NAME:
            func.active = plugin._search_tool_active
    assert _SEARCH_TOOL_NAME in _visible_tool_names(registry)


# ---------------------------------------------------------------
# 提示词注入：只在 active 时给指引
# ---------------------------------------------------------------


def test_instruction_omits_search_guide_when_inactive():
    plugin = make_plugin(enabled=False)
    plugin._search_tool_active = False
    text = plugin._build_browser_instruction()
    assert "browse_web" in text
    assert "browser_web_search" not in text


def test_instruction_includes_search_guide_when_active():
    plugin = make_plugin()
    plugin._search_tool_active = True
    text = plugin._build_browser_instruction()
    assert "browse_web" in text
    assert "browser_web_search" in text
    assert "只选其一" in text
    assert "browse_web" in text


def test_search_guide_never_claims_builtin_disabled():
    """并列语义：不得暗示"内置 web_search 已被关闭/被接管"。"""
    plugin = make_plugin()
    plugin._search_tool_active = True
    text = plugin._build_search_instruction()
    for forbidden in ("已关闭", "已接管", "替代内置", "停用内置"):
        assert forbidden not in text
    assert "各自的 key" in text
