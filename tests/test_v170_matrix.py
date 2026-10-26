"""v1.7.0 三轴正交矩阵：``search.mode`` × ``background_mode`` × ``background_engine``。

正交性定义（规格 §4.4 写死）：

- ``background_mode`` 管**外层工具的调用方式**（前台同步 / 插件自管后台）；
- ``background_engine`` 管**后台由谁执行**（插件 / 核心原生）；
- ``search.mode`` 管**搜索工具挂在哪一层**（外层 / 内层）。

三轴互不改变彼此的取值，但**叠加**决定"用户会看到什么"。本文件把表里
4 行 × 3 个 ``background_mode`` 逐格跑一遍，断言两个判定函数的返回值与表一致；
并钉住"内层工具**永不**后台化"这条依据。
"""

from __future__ import annotations

import asyncio
from itertools import product
from types import SimpleNamespace

import pytest

from main import (
    _BROWSE_WEB_TOOL_NAME,
    _LOCAL_PAGE_TOOL_NAME,
    _SEARCH_TOOL_NAME,
    BrowserLLMPlugin,
)

OWN_MODULE = "astrbot_plugin_browser_llm.main"


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Ctx:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def activate_llm_tool_async(self, name):
        self.calls.append(("activate", name))
        return True

    async def deactivate_llm_tool_async(self, name):
        self.calls.append(("deactivate", name))
        return True


def _config(mode, background_mode, background_engine):
    return {
        "search": {
            "mode": mode,
            "enabled": mode == "internal",  # 池就绪与否另一维，见下
            "providers": [{"__template_key": "brave", "api_key": "sk-test"}],
        },
        "tools": {
            "background_mode": background_mode,
            "background_engine": background_engine,
        },
    }


def _plugin(mode, background_mode, background_engine, *, pool=True):
    cfg = _Cfg(_config(mode, background_mode, background_engine))
    if not pool:
        cfg["search"]["enabled"] = False
    ctx = _Ctx()
    plugin = BrowserLLMPlugin(ctx, cfg)
    plugin.config = cfg
    plugin.context = ctx
    plugin.sessions = None
    plugin.background_engine_effective = background_engine
    return plugin


def _registry(monkeypatch):
    from astrbot.core.provider.register import llm_tools

    monkeypatch.setattr(
        llm_tools,
        "func_list",
        [
            SimpleNamespace(
                name=_SEARCH_TOOL_NAME, handler_module_path=OWN_MODULE, active=True
            )
        ],
        raising=False,
    )


# ---------------------------------------------------------------
# 4 行 × 3 mode × 2 engine = 24 格
# ---------------------------------------------------------------

MATRIX = {
    # (engine, mode) -> {bg_mode: (browse_web_bg, local_page_bg, search_bg)}
    ("plugin", "external"): {
        "off": (False, False, False),
        "browse_only": (True, False, False),
        "all": (True, True, True),
    },
    ("core", "external"): {
        # core 生效时插件登记表恒空（互斥契约）⇒ 三工具都不走 plugin 后台
        "off": (False, False, False),
        "browse_only": (False, False, False),
        "all": (False, False, False),
    },
    ("plugin", "internal"): {
        "off": (False, False, False),
        "browse_only": (True, False, False),
        "all": (True, True, True),
    },
    ("core", "internal"): {
        "off": (False, False, False),
        "browse_only": (False, False, False),
        "all": (False, False, False),
    },
}


@pytest.mark.parametrize(
    "engine,mode,bg_mode",
    [(e, m, b) for (e, m), table in MATRIX.items() for b in table],
)
def test_plugin_background_matrix(engine, mode, bg_mode):
    """plugin 引擎后台判定与表逐格一致（core 生效时恒 False）。"""
    plugin = _plugin(mode, bg_mode, engine)
    expected = MATRIX[(engine, mode)][bg_mode]
    actual = (
        plugin._background_enabled_for(_BROWSE_WEB_TOOL_NAME),
        plugin._background_enabled_for(_LOCAL_PAGE_TOOL_NAME),
        plugin._background_enabled_for(_SEARCH_TOOL_NAME),
    )
    assert actual == expected, f"{engine}×{mode}×{bg_mode}"


@pytest.mark.parametrize(
    "mode,bg_mode",
    [(m, b) for m in ("external", "internal") for b in ("browse_only", "all")],
)
def test_c_scope_is_permanently_only_browse_web(mode, bg_mode):
    """C 路径范围恒定 = 只有 ``browse_web``（L/S 永久排除，D3 冻结）。

    ``background_mode=off`` 时 C 路径恒关（无范围 = 不置 flag），因此本用例
    只跑非 off 的两档。
    """
    plugin = _plugin(mode, bg_mode, "core")
    assert plugin._background_c_mode_for(_BROWSE_WEB_TOOL_NAME) is True
    assert plugin._background_c_mode_for(_LOCAL_PAGE_TOOL_NAME) is False
    assert plugin._background_c_mode_for(_SEARCH_TOOL_NAME) is False


def test_c_mode_off_when_engine_plugin():
    """engine=plugin 时 C 路径恒关（两门互斥，不会"一条任务两条消息"）。"""
    for bg_mode in ("off", "browse_only", "all"):
        plugin = _plugin("external", bg_mode, "plugin")
        plugin.background_engine_effective = "plugin"
        assert plugin._background_c_mode_for(_BROWSE_WEB_TOOL_NAME) is False


# ---------------------------------------------------------------
# search.mode 只影响"搜索挂在哪层"，不改另外两轴
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", ["external", "internal"]
)
def test_search_mode_does_not_change_background_flags(mode):
    """同一 background 配置下，两档 search.mode 的 plugin 后台判定完全相同。"""
    for bg_mode in ("off", "browse_only", "all"):
        ext = _plugin("external", bg_mode, "plugin", pool=True)
        internal = _plugin("internal", bg_mode, "plugin", pool=True)
        names = (_BROWSE_WEB_TOOL_NAME, _LOCAL_PAGE_TOOL_NAME, _SEARCH_TOOL_NAME)
        assert [ext._background_enabled_for(n) for n in names] == [
            internal._background_enabled_for(n) for n in names
        ]


def test_internal_mode_keeps_inner_tool_out_of_registry(monkeypatch):
    """内层工具不在 ``llm_tools.func_list`` 里 ⇒ 核心的 is_background_task 看不到它。"""
    _registry(monkeypatch)
    from astrbot.core.provider.register import llm_tools

    plugin = _plugin("internal", "all", "plugin")
    asyncio.run(plugin._sync_search_mode())
    names = {getattr(t, "name", "") for t in plugin._browser_tools}
    assert "browse_web_search" in names
    registered = {getattr(f, "name", "") for f in llm_tools.func_list}
    assert "browse_web_search" not in registered
    assert "browse_web" not in registered, "内层 browse_* 从不进 func_list"


def test_matrix_covers_every_cell():
    """矩阵自检：4 行 × 3 mode = 12 格，没有漏格（防止以后加档位忘补表）。"""
    combos = set(product(("plugin", "core"), ("external", "internal")))
    assert set(MATRIX) == combos
    for table in MATRIX.values():
        assert set(table) == {"off", "browse_only", "all"}
