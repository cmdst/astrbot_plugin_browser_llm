"""v1.7.0 内层工具面 v2：2 个新交互原语 + 16 处返回体贴紧（规格 §1.3）。

本文件只覆盖"**原语层**"的可判定行为（工具面数量/参数由
``tests/test_main_contract.py`` 负责）：

- ``browse_get_interactive``：8 列输出（#n | role | name | type | checked |
  disabled | selector | cx,cy）；``max_items`` / ``role_filter`` 生效；
  只做 DOM 枚举（脚本里没有网络调用 / innerHTML 全量）；
- ``browse_click_selector``：``locator(selector).nth(index)`` 落点、
  ``wait_for(visible)`` 先等、越界 index 给可读错误、点击后读回状态；
- ``browse_checkbox``：多匹配用 nth（不再 strict violation）、非 checkbox
  降级点击 + 读回、``checked`` 可选默认 true；
- ``browse_click_text``：``index`` 扩参、失败仍给 top-5 候选；
- ``browse_select_option``：失败列出该 select 的全部 option；
- 紧凑返回体：scroll 只回进度、click_link 只回 URL+标题、click_coords 附命中元素。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.safety import SafetyFilter
from main import _BROWSER_AGENT_INSTRUCTION, _BROWSER_TOOLS, BrowserLLMPlugin

UMO = "aiocqhttp:GroupMessage:123"


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Event:
    unified_msg_origin = UMO

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


class _NullLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _Element:
    def __init__(self, *, checked=None, attrs=None, text="", visible=True):
        self.checked = checked
        self.attrs = dict(attrs or {})
        self.text = text
        self.visible = visible
        self.clicks = 0
        self.hovers = 0
        self.waits = []

    async def is_checked(self):
        if self.checked is None:
            raise RuntimeError("Not a checkbox or radio button")
        return self.checked

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def click(self, timeout=None):
        self.clicks += 1

    async def hover(self, timeout=None):
        self.hovers += 1

    async def wait_for(self, **kwargs):
        self.waits.append(kwargs)


class _Locator:
    def __init__(self, items):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return _Single(self.items[index])

    @property
    def first(self):
        return _Single(self.items[0])

    async def click(self, timeout=None):
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation: resolved to N elements")
        await self.items[0].click(timeout=timeout)

    async def hover(self, timeout=None):
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        await self.items[0].hover(timeout=timeout)

    async def fill(self, value):
        return None

    async def is_checked(self):
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return await self.items[0].is_checked()

    async def get_attribute(self, name):
        if len(self.items) != 1:
            raise RuntimeError("strict mode violation")
        return await self.items[0].get_attribute(name)


class _Single:
    def __init__(self, element):
        self.element = element

    async def click(self, timeout=None):
        await self.element.click(timeout=timeout)

    async def hover(self, timeout=None):
        await self.element.hover(timeout=timeout)

    async def wait_for(self, **kwargs):
        await self.element.wait_for(**kwargs)

    async def is_checked(self):
        return await self.element.is_checked()

    async def get_attribute(self, name):
        return await self.element.get_attribute(name)


class _Page:
    url = "https://example.com/spa"
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, *, interactives=None, options=None, click_at=None):
        self.interactives = interactives or []
        self.options = options
        self.click_at = click_at
        self.evaluated: list[str] = []
        self.buttons = {}
        self.goto_urls = []
        self.mouse = self

    # --- 交互元素枚举 / 选项 / 命中元素 ---
    async def evaluate(self, script, *args):
        self.evaluated.append(script)
        if "scrollY" in script:
            return {"y": 400, "h": 2000, "vh": 800}
        if "elementFromPoint" in script:
            return self.click_at or ""
        if "querySelectorAll" in script and "interactive" not in script:
            return self.interactives
        if "tagName.toLowerCase() !== 'select'" in script:
            return self.options
        return self.interactives

    def locator(self, selector):
        return _Locator(self.buttons.get(selector, []))

    def get_by_text(self, text, exact=False):
        return _Locator(self.buttons.get(f"text={text}", []))

    async def mouse_click(self, x, y):
        return None

    async def click(self, x, y):
        return None

    async def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        self.url = url

    async def select_option(self, selector, value):
        if value not in {opt["value"] for opt in (self.options or [])}:
            raise RuntimeError(f'no option with value "{value}"')

    async def screenshot(self, clip=None, path=""):
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    async def reload(self, **kwargs):
        return None

    async def go_back(self):
        return None

    async def go_forward(self):
        return None


class _Extractor:
    async def extract_page_info(self, page):
        return {"url": getattr(page, "url", ""), "title": "测试页"}

    async def extract_text(self, page, max_chars=0):
        text = "页面正文ABCD"
        return text[:max_chars] if max_chars else text

    async def extract_links(self, page, max_links=0):
        return "[1] 目标页 → https://example.com/real"


def _plugin(page=None, config=None):
    cfg = config if isinstance(config, _Cfg) else _Cfg(config or {})
    plugin = BrowserLLMPlugin(SimpleNamespace(), cfg)
    plugin.config = cfg
    plugin.sessions = None
    plugin.extractor = _Extractor()
    plugin._vision_cache = {}
    plugin._vision_cache_invalidate = lambda *a, **k: 0
    plugin._lock_for = lambda event: _NullLock()
    page = page or _Page()

    async def _get_page(event):
        return page, ""

    plugin._get_page = _get_page
    plugin.max_chars = 4000
    plugin.max_links = 20
    plugin.agent_result_max_chars = 3000
    plugin.safety = SafetyFilter([], True)
    return plugin


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# browse_get_interactive（#25）
# ---------------------------------------------------------------


INTERACTIVE_ROW = {
    "role": "radio",
    "name": "非常同意",
    "type": "radio",
    "state": "false",
    "disabled": False,
    "selector": "#agree",
    "x": 120,
    "y": 240,
    "visible": True,
}


def test_get_interactive_lists_eight_columns():
    """8 列 + ``#n`` **从 0 起算**（= ``browse_click_selector(index=…)`` 的基数）。

    v1.7.0 repair（评审 R5）：此前 ``#n`` 从 1 数而 index 从 0 起 —— 模型看到
    ``#3`` 传 ``index=3`` 就会点到第 4 个（同页 click_link / click_coords /
    checkbox 都是 0 起，只有这里不一致）。
    """
    page = _Page(interactives=[INTERACTIVE_ROW])
    plugin = _plugin(page)
    out = _run(plugin.browse_get_interactive(_Event(), 40, ""))
    header = "#n | role | name | type | checked | disabled | selector | cx,cy"
    assert header in out
    assert "0 | radio | 非常同意 | radio | false | 否 | #agree | 120,240" in out
    assert "browse_click_selector" in out, "要告诉模型 selector 怎么用"
    assert "#n **就是** index" in out, "文案必须说明 #n 与 index 同基数"


def test_get_interactive_number_column_matches_click_index():
    """基数关系：第 k 行（0 起）的 ``#k`` 与 ``browse_click_selector(index=k)`` 同指。

    用两行元素真跑一遍：``#1`` 那行的 selector 交给 ``click_selector(index=1)``，
    断言点中的正是第二个元素（而不是第一个/第三个）。
    """
    rows = [
        dict(INTERACTIVE_ROW, name="甲", selector="#a", x=10, y=10),
        dict(INTERACTIVE_ROW, name="乙", selector="#b", x=20, y=20),
    ]
    page = _Page(interactives=rows)
    plugin = _plugin(page)
    out = _run(plugin.browse_get_interactive(_Event(), 40, ""))
    assert "1 | radio | 乙 |" in out, out
    elements = [_Element(attrs={"aria-pressed": "false"}) for _ in range(2)]
    page.buttons["#b"] = elements
    _run(plugin.browse_click_selector(_Event(), "#b", index=1))
    assert elements[1].clicks == 1 and elements[0].clicks == 0


def test_get_interactive_passes_limits_and_role_filter():
    captured = {}

    class _P(_Page):
        async def evaluate(self, script, *args):
            captured["args"] = args
            return [INTERACTIVE_ROW]

    plugin = _plugin(_P())
    _run(plugin.browse_get_interactive(_Event(), 5, "radio"))
    assert captured["args"] == (5, "radio")


def test_get_interactive_clamps_max_items():
    captured = {}

    class _P(_Page):
        async def evaluate(self, script, *args):
            captured["args"] = args
            return [INTERACTIVE_ROW]

    plugin = _plugin(_P())
    _run(plugin.browse_get_interactive(_Event(), 9999, ""))
    assert captured["args"][0] == 200, "上限 200（防一次吐几千行）"


def test_get_interactive_empty_page_is_explicit():
    plugin = _plugin(_Page(interactives=[]))
    out = _run(plugin.browse_get_interactive(_Event()))
    assert "没有找到可交互元素" in out
    assert "browse_get_text" in out, "要给出替代路径，不然模型只能瞎试"


def test_get_interactive_js_is_read_only():
    """注入脚本只做 DOM 枚举：无网络调用、不读 innerHTML 全文（§9.3）。"""
    import main as main_module

    js = main_module._INTERACTIVE_JS
    for forbidden in ("fetch(", "XMLHttpRequest", "innerHTML", "goto(", "eval("):
        assert forbidden not in js, f"枚举脚本不得出现 {forbidden}"


# ---------------------------------------------------------------
# browse_click_selector（#26）
# ---------------------------------------------------------------


def test_click_selector_uses_nth_and_waits_visible():
    elements = [_Element(attrs={"aria-pressed": "false"}) for _ in range(3)]
    page = _Page()
    page.buttons["[role=radio]"] = elements
    plugin = _plugin(page)
    out = _run(plugin.browse_click_selector(_Event(), "[role=radio]", index=2))
    assert elements[2].clicks == 1, "必须点第 index 个（0-based）"
    assert elements[0].clicks == 0
    assert elements[2].waits and elements[2].waits[0]["state"] == "visible"
    assert "点击后状态读回：false" in out


def test_click_selector_out_of_range_reports_count():
    page = _Page()
    page.buttons["#x"] = [_Element()]
    plugin = _plugin(page)
    out = _run(plugin.browse_click_selector(_Event(), "#x", index=5))
    assert out.startswith("【错误】index=5 越界")
    assert "共匹配 1 个" in out


def test_click_selector_empty_selector_rejected():
    plugin = _plugin()
    assert _run(plugin.browse_click_selector(_Event(), "  ")) == (
        "【错误】selector 不能为空。"
    )


def test_click_selector_zero_match_suggests_enumeration():
    plugin = _plugin(_Page())
    out = _run(plugin.browse_click_selector(_Event(), "#nope"))
    assert out.startswith("【错误】")
    assert "browse_get_interactive" in out


# ---------------------------------------------------------------
# browse_checkbox（#22 三条语义）
# ---------------------------------------------------------------


def test_checkbox_multi_match_uses_index_not_strict_violation():
    elements = [_Element(checked=False) for _ in range(3)]
    page = _Page()
    page.buttons['input[type="radio"]'] = elements
    plugin = _plugin(page)
    out = _run(plugin.browse_checkbox(_Event(), 'input[type="radio"]', True, index=1))
    assert "【错误】" not in out
    assert elements[1].clicks == 1 and elements[0].clicks == 0


def test_checkbox_checked_defaults_to_true():
    elements = [_Element(checked=False)]
    page = _Page()
    page.buttons["#c"] = elements
    plugin = _plugin(page)
    out = _run(plugin.browse_checkbox(_Event(), "#c"))
    assert elements[0].clicks == 1
    assert "选中状态=True" in out


def test_checkbox_button_style_downgrades_to_click_and_reads_back():
    # 读不到任何可读状态（既不是 checkbox，也没有 aria-*/data-state）
    element = _Element(checked=None, attrs={})
    page = _Page()
    page.buttons["button#x"] = [element]
    plugin = _plugin(page)

    async def _click(timeout=None):
        element.clicks += 1
        element.attrs["aria-pressed"] = "true"

    element.click = _click
    out = _run(plugin.browse_checkbox(_Event(), "button#x", True))
    assert element.clicks == 1
    assert "已降级为点击" in out
    assert "点击后状态=" in out


def test_checkbox_no_click_when_state_already_matches():
    element = _Element(checked=True)
    page = _Page()
    page.buttons["#c"] = [element]
    plugin = _plugin(page)
    out = _run(plugin.browse_checkbox(_Event(), "#c", True))
    assert element.clicks == 0
    assert "选中状态已符合目标" in out


def test_checkbox_out_of_range_reports_candidates():
    page = _Page()
    page.buttons["#c"] = [_Element(checked=False)]
    plugin = _plugin(page)
    out = _run(plugin.browse_checkbox(_Event(), "#c", True, index=3))
    assert out.startswith("【错误】index=3 越界")


# ---------------------------------------------------------------
# browse_click_text（#16 扩参 index）
# ---------------------------------------------------------------


def test_click_text_index_selects_nth_match():
    elements = [_Element(text="提交") for _ in range(3)]
    page = _Page()

    class _ByText:
        def __init__(self, items):
            self.items = items

        @property
        def first(self):
            return _Single(self.items[0])

        def nth(self, index):
            return _Single(self.items[index])

    page.get_by_text = lambda text, exact=False: _ByText(elements)
    plugin = _plugin(page)
    out = _run(plugin.browse_click_text(_Event(), "提交", index=2))
    assert elements[2].clicks == 1 and elements[0].clicks == 0
    assert "【错误】" not in out


def test_click_text_negative_index_rejected():
    plugin = _plugin()
    out = _run(plugin.browse_click_text(_Event(), "提交", index=-1))
    assert out.startswith("【错误】index 不能为负数")


def test_click_text_failure_keeps_top5_candidates():
    page = _Page(
        interactives=[
            {"text": "非常同意", "type": "button", "visible": True},
            {"text": "中立", "type": "div[role=option]", "visible": False},
        ]
    )

    class _ByText:
        @property
        def first(self):
            raise RuntimeError("not found")

        def nth(self, index):
            raise RuntimeError("not found")

    page.get_by_text = lambda text, exact=False: _ByText()
    page.locator = lambda sel: _ByText()
    plugin = _plugin(page)

    async def _candidates(p, text):
        return await BrowserLLMPlugin._click_text_candidates(plugin, p, text)

    plugin._click_text_candidates = lambda p, t: _candidates(p, t)
    out = _run(plugin.browse_click_text(_Event(), "略微同意", index=0))
    assert out.startswith("【错误】")
    assert "相近的可点击文本" in out
    assert "高相似" not in out  # 无评分泄漏
    assert "「非常同意」" in out


# ---------------------------------------------------------------
# browse_select_option（#18 失败列出全部选项）
# ---------------------------------------------------------------


def test_select_option_failure_lists_all_real_options():
    page = _Page(
        options=[
            {"label": "18-24", "value": "a", "selected": False},
            {"label": "25-30", "value": "b", "selected": True},
            {"label": "31-40", "value": "c", "selected": False},
        ]
    )
    plugin = _plugin(page)
    out = _run(plugin.browse_select_option(_Event(), "#age", "99"))
    assert out.startswith("【错误】选择下拉选项失败")
    assert "共有 3 个真实选项" in out
    assert "「25-30」 / value=b（当前选中）" in out
    assert "禁止臆造选项" in out


# ---------------------------------------------------------------
# 紧凑返回体（#3/#6/#7/#19）
# ---------------------------------------------------------------


def test_click_link_returns_url_and_title_only():
    plugin = _plugin(_Page())
    out = _run(plugin.browse_click_link(_Event(), 1))
    assert "URL: https://example.com" in out
    assert "标题: 测试页" in out
    assert "正文" not in out, "跳转类原语不附正文（正文用 browse_get_text）"


def test_scroll_returns_progress_not_body():
    plugin = _plugin(_Page())
    out = _run(plugin.browse_scroll(_Event(), "down", 400))
    assert out.startswith("已向下滚动 400 px，进度 ")
    assert "页面高度 2000px" in out
    assert "browse_get_text" in out
    assert "页面正文ABCD" not in out


def test_click_coords_reports_hit_element():
    page = _Page(click_at="button「提交」")
    plugin = _plugin(page)
    out = _run(plugin.browse_click_coords(_Event(), 10, 20))
    assert "已点击坐标 (10, 20)" in out
    assert "命中元素：button「提交」" in out
    assert "URL: " in out


def test_current_page_and_reload_are_compact():
    plugin = _plugin(_Page())
    current = _run(plugin.browse_current_page(_Event()))
    assert "正文前" in current
    reloaded = _run(plugin.browse_reload(_Event()))
    assert "正文前" in reloaded


def test_hover_failure_returns_candidates():
    page = _Page(
        interactives=[{"text": "设置", "type": "button", "visible": True}]
    )

    class _ByText:
        @property
        def first(self):
            raise RuntimeError("not found")

    page.get_by_text = lambda text, exact=False: _ByText()
    page.locator = lambda sel: _ByText()
    plugin = _plugin(page)
    out = _run(plugin.browse_hover(_Event(), "选项"))
    assert out.startswith("【错误】页面上找不到包含文本「选项」的可悬停元素。")
    assert "相近的可点击文本" in out


# ---------------------------------------------------------------
# 指令文本：26 个工具都要出现在内层指令里（判据 §1.3-3）
# ---------------------------------------------------------------


def test_instruction_lists_every_inner_tool():
    for spec in _BROWSER_TOOLS:
        assert spec["name"] in _BROWSER_AGENT_INSTRUCTION, (
            f"内层指令缺 {spec['name']}（模型不知道它存在）"
        )


def test_instruction_covers_conditional_inner_tools():
    """v1.7.0 repair（评审 R9）：**当前 toolset** 的每个名字都要在指令里。

    条件工具（``browse_web_search``）不在固定常量里，必须在
    ``_build_subagent_instruction`` 里按配置动态补 —— 否则模型看得到工具却
    不知道它是什么，是幻觉调用的来源。
    """
    plugin = _plugin(_Page())
    # external 档：没有条件工具，指令里也不该出现（不诱导幻觉）
    plugin.search_mode = "external"
    plugin._rebuild_browser_toolset()
    names = {t.name for t in plugin._browser_tools}
    text = plugin._build_subagent_instruction("text")
    assert names == {spec["name"] for spec in _BROWSER_TOOLS}
    assert "browse_web_search" not in text
    for name in names:
        assert name in text, f"指令缺 {name}"

    # internal 档 + 池就绪：条件工具进 toolset，也必须进指令
    plugin.config["search"] = {
        "mode": "internal",
        "enabled": True,
        "providers": [{"__template_key": "brave", "api_key": "sk-test"}],
    }
    plugin._refresh_config()
    plugin._rebuild_browser_toolset()
    names = {t.name for t in plugin._browser_tools}
    text = plugin._build_subagent_instruction("text")
    assert "browse_web_search" in names
    assert "browse_web_search：用配置的搜索 API 池检索" in text
    for name in names:
        assert name in text, f"指令缺 {name}（条件工具也要覆盖）"


def test_instruction_teaches_enumeration_first():
    """指令要给"先枚举再点"的明确动线（规格 §1.3 新增 2 工具的理由）。"""
    assert "browse_get_interactive" in _BROWSER_AGENT_INSTRUCTION
    assert "browse_click_selector" in _BROWSER_AGENT_INSTRUCTION
    assert "标签必须逐字取自页面" in _BROWSER_AGENT_INSTRUCTION


def test_inner_tools_count_is_26():
    assert len(_BROWSER_TOOLS) == 26
    names = [s["name"] for s in _BROWSER_TOOLS]
    assert len(names) == len(set(names))
    assert "browse_get_interactive" in names and "browse_click_selector" in names
    # 条件工具**不在**这个字面量里（运行时按 search.mode 追加）
    assert "browse_web_search" not in names
