"""交互原语 v1.6.1 回归测试（事故止血：checkbox index / click_text 候选 / 交互后失效）。

对应事故 ``ops_astrbot/incidents/20261026-browser-vision-cache-stale-on-spa.md``
的缺陷②（交互原语太粗）与缓存失效部分：

- ``browse_checkbox`` 支持 ``index``（默认 0）：多匹配不再抛 Playwright 的
  ``strict mode violation``（事故里 ``input[type="radio"]`` 匹配到 5 个元素直接
  失败）；``button`` 形态的选项走可点击路径成功；越界 index 返回可读错误并
  给出候选数。
- ``browse_click_text`` 失败时返回「相近可点击文本 top-5」（含元素类型与
  可见性），但**仍保留【错误】语义**（不把失败伪装成成功）。
- 交互后失效：改变画面的原语清掉该 ``(umo, 规范化URL)`` 的识图缓存；纯读
  操作（get_text / get_links / current_page）**不失效**（否则缓存白做）；
  其它 URL / 其它会话的条目一条都不动（不跨标签、不跨 URL 误删）。
- 内层指令：选项标签必须来自页面实际文本，禁止臆造。

依赖 conftest 的 astrbot 桩；定位器桩按 Playwright async API 语义实现
（``count`` / ``nth`` / ``is_checked`` / ``get_attribute`` 都是协程，
多匹配时 ``click`` 抛 strict mode violation）。
"""

import asyncio
from pathlib import Path

import pytest

from main import (
    _BROWSER_AGENT_INSTRUCTION,
    _BROWSER_TOOLS,
    _CLICK_TEXT_CANDIDATE_MAX_LEN,
    _CLICK_TEXT_CANDIDATE_TOP_N,
    BrowserLLMPlugin,
)


def _run(coro):
    return asyncio.run(coro)


class _Event:
    def __init__(self, umo="grp:1:GroupMessage:123"):
        self.unified_msg_origin = umo

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


# ============================================================
# 定位器桩（Playwright async API 语义）
# ============================================================

class _Element:
    """单个匹配元素：checked=None 表示「不是 checkbox/radio」。"""

    def __init__(self, checked=None, aria=None, text=""):
        self.checked = checked
        self.aria = dict(aria or {})
        self.text = text
        self.clicks = 0


class _Locator:
    """指向 _LocatorSet 中第 index 个元素的定位器。"""

    def __init__(self, owner, index):
        self._owner = owner
        self._index = index

    @property
    def _el(self):
        return self._owner.items[self._index]

    async def is_checked(self):
        if self._el.checked is None:
            raise RuntimeError("Not a checkbox or radio button")
        return self._el.checked

    async def get_attribute(self, name):
        return self._el.aria.get(name)

    async def click(self, timeout=None):
        self._el.clicks += 1
        self._owner.clicked_indices.append(self._index)
        if self._el.checked is not None:
            self._el.checked = not self._el.checked  # 复选框语义：点击即切换

    async def fill(self, value, timeout=None):
        self._owner.filled.append((self._index, value))


class _LocatorSet:
    """page.locator(selector) 的返回值：0/1/N 个匹配。"""

    def __init__(self, items=None, fail_count=False):
        self.items = list(items or [])
        self.clicked_indices = []
        self.filled = []
        self._fail_count = fail_count

    async def count(self):
        if self._fail_count:
            raise RuntimeError("count() 不可用")
        return len(self.items)

    def nth(self, index):
        if index >= len(self.items):
            raise IndexError(f"nth({index}) 越界（共 {len(self.items)} 个）")
        return _Locator(self, index)

    # --- 单匹配语义（与 Playwright 一致：多匹配抛 strict mode violation） ---

    def _single(self):
        if len(self.items) != 1:
            raise RuntimeError(
                f"strict mode violation: locator resolved to {len(self.items)} elements"
            )
        return _Locator(self, 0)

    async def is_checked(self):
        return await self._single().is_checked()

    async def get_attribute(self, name):
        return await self._single().get_attribute(name)

    async def click(self, timeout=None):
        return await self._single().click(timeout=timeout)

    async def fill(self, value, timeout=None):
        return await self._single().fill(value, timeout=timeout)


class _TextTarget:
    """get_by_text(...) / locator('text=...') 的返回值（只用到 .first.click/hover）。"""

    def __init__(self, fail):
        self.first = self
        self._fail = fail
        self.clicked = 0
        self.hovered = 0

    async def click(self, timeout=None):
        if self._fail:
            raise TimeoutError("locator.click: Timeout 10000ms exceeded.")
        self.clicked += 1

    async def hover(self, timeout=None):
        if self._fail:
            raise TimeoutError("locator.hover: Timeout 5000ms exceeded.")
        self.hovered += 1


class _Keyboard:
    def __init__(self, page):
        self._page = page

    async def press(self, key):
        self._page.pressed_keys.append(key)


class _Mouse:
    def __init__(self, page):
        self._page = page

    async def click(self, x, y):
        self._page.mouse_clicks.append((x, y))


class _Page:
    """通用页面桩：覆盖交互原语用到的全部方法（失败开关可控）。"""

    def __init__(self, url="https://example.com/spa"):
        self.url = url
        self.viewport_size = {"width": 1280, "height": 800}
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        self.pressed_keys = []
        self.mouse_clicks = []
        self.reloaded = 0
        self.went_back = 0
        self.went_forward = 0
        self.selected = []
        self.screenshots = []
        self.filled = []
        # 控件行为开关
        self.click_text_fails = False
        self.candidates = []
        self.candidates_error = False
        self.locators = {}
        self.default_locator = _LocatorSet([_Element(checked=False)])
        self.navigated_url = url

    # --- 导航 / 页面动作 ---

    async def goto(self, url, wait_until="domcontentloaded"):
        self.navigated_url = url
        self.url = url

    async def reload(self, wait_until="domcontentloaded", timeout=20000):
        self.reloaded += 1

    async def go_back(self):
        self.went_back += 1
        self.url = "https://example.com/previous"

    async def go_forward(self):
        self.went_forward += 1
        self.url = "https://example.com/next"

    async def evaluate(self, js, *args):
        if "querySelectorAll" in js and self.candidates_error:
            raise RuntimeError("evaluate 失败")
        if "querySelectorAll" in js:
            return self.candidates
        if "scrollBy" in js:
            return None
        return None

    async def screenshot(self, clip=None, path=None, full_page=False):
        self.screenshots.append((clip, path))
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nX")
        return path

    # --- 交互 ---

    async def fill(self, selector, text):
        self.filled.append((selector, text))

    async def type(self, selector, text):
        self.filled.append((selector, text))

    async def select_option(self, selector, value):
        self.selected.append((selector, value))

    def locator(self, selector):
        if selector.startswith("text="):
            return _TextTarget(fail=True)
        return self.locators.get(selector, self.default_locator)

    def get_by_text(self, text, exact=False):
        return _TextTarget(fail=self.click_text_fails)


class _Extractor:
    async def extract_page_info(self, page):
        return {"url": page.url, "title": "测试页"}

    async def extract_text(self, page, max_chars=4000):
        return "页面正文"

    async def extract_links(self, page, max_links=20):
        return "[1] 目标 → https://example.com/next"


class _Sessions:
    def __init__(self, page):
        self._page = page
        self.switched = []
        self.closed = []

    async def ensure_page(self, umo):
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()

    def tab_count(self, umo):
        return 2

    async def switch_tab(self, umo, index):
        self.switched.append(index)
        return self._page

    async def close_tab(self, umo, index):
        self.closed.append(index)
        return True

    async def new_tab(self, umo, url):
        return self._page


class _Safety:
    """安全过滤器桩：放行一切（本文件不测禁词/SSRF）。"""

    async def acheck_url(self, url):
        return True, ""

    def check_text(self, text):
        return True, ""


def _make_plugin(page=None, ttl=60):
    """装配交互原语所需的最小插件（真实 _page_summary / 真实缓存实现）。"""
    page = page or _Page()
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.config = {"vision_cache_ttl": ttl}
    plugin.vision_cache_ttl = ttl
    plugin.safety = _Safety()
    plugin.enable_banned_filter = True
    plugin.browser = None
    plugin.extractor = _Extractor()
    plugin.sessions = _Sessions(page)
    plugin.max_chars = 4000
    plugin.max_links = 20
    plugin._vision_cache = {}
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._lock_for = lambda event: asyncio.Lock()
    return plugin, page


def _seed_cache(plugin, umo="grp:1:GroupMessage:123", url="https://example.com/spa"):
    """预置三条缓存：本会话本页面（应被删）+ 跨 URL + 跨会话（应保留）。"""
    this_page = (umo, url, "screenshot", "", "hash-a", "prompt1", "vision/pid")
    other_url = (
        umo,
        "https://example.com/other",
        "screenshot",
        "",
        "hash-b",
        "prompt1",
        "vision/pid",
    )
    other_umo = (
        "grp:1:GroupMessage:other",
        url,
        "screenshot",
        "",
        "hash-c",
        "prompt1",
        "vision/pid",
    )
    for key in (this_page, other_url, other_umo):
        plugin._vision_cache[key] = (0.0, "描述")
    return this_page, other_url, other_umo


# ============================================================
# browse_checkbox：index / 按钮式选项 / 越界
# ============================================================

def _checkbox_plugin(items, **kwargs):
    page = _Page()
    page.locators["input[type=radio]"] = _LocatorSet(items, **kwargs)
    plugin, _ = _make_plugin(page)
    return plugin, page, page.locators["input[type=radio]"]


def test_checkbox_multi_match_uses_index_no_strict_violation():
    """3 个匹配 + index=1：不抛 strict mode violation，只点第 2 个。

    事故现场：``input[type="radio"]`` 匹配到 5 个元素，旧实现直接
    ``loc.click()`` ⇒ strict mode violation，模型拿不到任何有用信息。
    """
    items = [_Element(checked=False), _Element(checked=False), _Element(checked=False)]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True, index=1))
    assert "【错误】" not in out, f"不得报错（strict mode violation 已修）：{out}"
    assert loc.clicked_indices == [1], "只应点击 index=1 的元素"
    assert items[0].clicks == 0 and items[2].clicks == 0


def test_checkbox_multi_match_default_index_zero():
    """多匹配不传 index ⇒ 默认 0（事故里 5 个匹配的第一条）。"""
    items = [_Element(checked=False) for _ in range(5)]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True))
    assert "【错误】" not in out
    assert loc.clicked_indices == [0]


def test_checkbox_multi_match_no_click_when_state_matches():
    """index 命中元素的状态已符合目标 ⇒ 不点击（幂等）。"""
    items = [_Element(checked=True), _Element(checked=False)]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True, index=0))
    assert "【错误】" not in out
    assert loc.clicked_indices == []


def test_checkbox_button_style_option_clicks():
    """button 形态选项（is_checked 抛错 + aria-pressed=false）走可点击路径成功。"""
    items = [_Element(checked=None, aria={"aria-pressed": "false"})]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True))
    assert "【错误】" not in out, f"按钮式选项必须成功：{out}"
    assert loc.clicked_indices == [0]


def test_checkbox_button_style_already_pressed_no_click():
    """button 形态选项已是选中态 ⇒ 不重复点击。"""
    items = [_Element(checked=None, aria={"aria-pressed": "true"})]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True))
    assert "【错误】" not in out
    assert loc.clicked_indices == []


def test_checkbox_button_style_uncheck_without_state_errors():
    """按钮式选项无可读状态 + 要求取消 ⇒ 明确报错、不盲点（不伪装成功）。"""
    items = [_Element(checked=None)]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", False))
    assert "【错误】" in out and "无法执行「取消选中」" in out
    assert loc.clicked_indices == [], "状态不可读时不得盲点"


def test_checkbox_index_out_of_range_reports_candidate_count():
    """越界 index ⇒ 可读错误 + 候选数（帮助模型自我纠正）。"""
    items = [_Element(checked=False) for _ in range(3)]
    plugin, _page, loc = _checkbox_plugin(items)
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True, index=7))
    assert "【错误】" in out
    assert "越界" in out and "3 个候选" in out
    assert "0..2" in out, "应给出合法编号范围"
    assert loc.clicked_indices == []


def test_checkbox_no_match_reports_zero_candidates():
    """选择器零匹配 ⇒ 可读错误 + 「候选 0 个」。"""
    plugin, _page, loc = _checkbox_plugin([])
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True))
    assert "【错误】" in out and "候选 0 个" in out


def test_checkbox_negative_index_errors():
    """负 index ⇒ 明确报错（编号从 0 开始）。"""
    plugin, _page, loc = _checkbox_plugin([_Element(checked=False)])
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True, index=-1))
    assert "【错误】" in out and "不能为负数" in out
    assert loc.clicked_indices == []


def test_checkbox_invalid_index_type_errors():
    """非整数 index（LLM 传了字符串）⇒ 明确报错而不是抛异常。"""
    plugin, _page, loc = _checkbox_plugin([_Element(checked=False)])
    out = _run(plugin.browse_checkbox(_Event(), "input[type=radio]", True, index="x"))
    assert "【错误】" in out and "index 参数无效" in out


def test_checkbox_count_unavailable_falls_back_to_single_match():
    """定位器没有 count()（旧实现/桩）⇒ 按单一匹配处理，不因计数失败报错。"""

    class _NoCount(_LocatorSet):
        count = None  # 属性缺失语义：_locator_count 返回 None

    page = _Page()
    page.locators["#cb"] = _NoCount([_Element(checked=False)])
    plugin, _ = _make_plugin(page)
    out = _run(plugin.browse_checkbox(_Event(), "#cb", True))
    assert "【错误】" not in out


# ============================================================
# browse_click_text：失败返回相近候选 top-5
# ============================================================

def _candidates_plugin(candidates, error=False):
    page = _Page()
    page.click_text_fails = True
    page.candidates = candidates
    page.candidates_error = error
    plugin, _ = _make_plugin(page)
    return plugin, page


def test_click_text_failure_returns_top5_candidates():
    """失败返回【错误】+ 相近可点击文本 top-5（含类型与可见性）。"""
    plugin, _page = _candidates_plugin([
        {"text": "非常同意", "type": "button", "visible": True},
        {"text": "非常不同意", "type": "button", "visible": True},
        {"text": "中立", "type": "div[role=option]", "visible": False},
    ])
    out = _run(plugin.browse_click_text(_Event(), "既不赞同也不反对"))
    assert out.startswith(
        "【错误】页面上找不到包含文本「既不赞同也不反对」的可点击元素。"
    ), "原错误语义必须保留（不伪装成功）"
    assert "相近的可点击文本（top 3，页面共采集 3 个候选）" in out
    assert "「非常同意」（button，可见）" in out
    assert "「中立」（div[role=option]，不可见）" in out
    assert "禁止臆造选项标签" in out


def test_click_text_candidates_capped_at_five():
    """候选最多展示 5 条（即使页面上有更多）。"""
    candidates = [
        {"text": f"选项{i}", "type": "button", "visible": True} for i in range(12)
    ]
    plugin, _page = _candidates_plugin(candidates)
    out = _run(plugin.browse_click_text(_Event(), "不存在的标签"))
    numbered = [ln for ln in out.splitlines() if ln[:2] in {"1.", "2.", "3.", "4.", "5.", "6."}]
    assert len(numbered) == _CLICK_TEXT_CANDIDATE_TOP_N == 5
    assert "页面共采集 12 个候选" in out


def test_click_text_candidates_substring_match_ranks_first():
    """包含关系优先于相似度：目标文本是候选子串的排前面。"""
    plugin, _page = _candidates_plugin([
        {"text": "确认提交", "type": "button", "visible": True},
        {"text": "非常同意", "type": "button", "visible": True},
        {"text": "不同意", "type": "button", "visible": True},
    ])
    out = _run(plugin.browse_click_text(_Event(), "同意"))
    lines = [ln for ln in out.splitlines() if ln.startswith(("1.", "2.", "3."))]
    assert "不同意" in lines[0] and "非常同意" in lines[1]
    assert "确认提交" in lines[2], "无包含关系的候选排最后"


def test_click_text_candidates_filter_overlong_text():
    """超长文本（容器文本而非可点击标签）不进候选。"""
    long_text = "长" * (_CLICK_TEXT_CANDIDATE_MAX_LEN + 1)
    plugin, _page = _candidates_plugin([
        {"text": long_text, "type": "div", "visible": True},
        {"text": "开始作答", "type": "button", "visible": True},
    ])
    out = _run(plugin.browse_click_text(_Event(), "开始"))
    assert long_text not in out
    assert "「开始作答」（button，可见）" in out


def test_click_text_no_candidates_keeps_plain_error():
    """候选采集失败（evaluate 抛错）⇒ 只回原错误，不掺假信息。"""
    plugin, _page = _candidates_plugin([], error=True)
    out = _run(plugin.browse_click_text(_Event(), "不存在"))
    assert out == "【错误】页面上找不到包含文本「不存在」的可点击元素。"


def test_click_text_success_returns_summary_without_candidates():
    """点击成功路径不受影响：返回页面摘要，无【错误】、无候选块。"""
    page = _Page()
    page.click_text_fails = False
    plugin, _ = _make_plugin(page)
    out = _run(plugin.browse_click_text(_Event(), "开始作答"))
    assert "【错误】" not in out
    assert "相近的可点击文本" not in out
    assert "正文摘要: 页面正文" in out


def test_click_text_failure_logged_as_warning(monkeypatch):
    """失败仍记 warning 日志（日志侧可与成功区分）。"""
    import main as main_module

    records = []
    monkeypatch.setattr(main_module.logger, "warning", records.append)
    plugin, _page = _candidates_plugin([
        {"text": "开始作答", "type": "button", "visible": True},
    ])
    _run(plugin.browse_click_text(_Event(), "开始"))
    assert any("未找到可点击元素" in r for r in records)


# ============================================================
# 交互后失效：改画面的原语清缓存，纯读操作不清
# ============================================================

_INVALIDATING_CALLS = [
    ("click_text", lambda p, e: p.browse_click_text(e, "开始作答")),
    ("click_coords", lambda p, e: p.browse_click_coords(e, 340, 470)),
    ("checkbox", lambda p, e: p.browse_checkbox(e, "#cb", True)),
    ("set_slider", lambda p, e: p.browse_set_slider(e, 50)),
    ("select_option", lambda p, e: p.browse_select_option(e, "#age", "18")),
    ("input", lambda p, e: p.browse_input(e, "#q", "关键词")),
    ("press_key", lambda p, e: p.browse_press_key(e, "Enter")),
    ("hover", lambda p, e: p.browse_hover(e, "更多菜单")),
    ("scroll", lambda p, e: p.browse_scroll(e, "down", 800)),
    ("reload", lambda p, e: p.browse_reload(e)),
    ("go_back", lambda p, e: p.browse_go_back(e)),
    ("go_forward", lambda p, e: p.browse_go_forward(e)),
    ("switch_tab", lambda p, e: p.browse_switch_tab(e, 2)),
    ("close_tab", lambda p, e: p.browse_close_tab(e, 2)),
]


@pytest.mark.parametrize(
    "call", [c[1] for c in _INVALIDATING_CALLS], ids=[c[0] for c in _INVALIDATING_CALLS]
)
def test_interaction_invalidates_own_page_cache_only(call):
    """改画面的原语：删本会话本页面条目，其它 URL / 其它会话条目原样保留。"""
    page = _Page()
    page.locators["#cb"] = _LocatorSet([_Element(checked=False)])
    page.locators["input[type=range]"] = _LocatorSet([_Element(checked=False)])
    plugin, _ = _make_plugin(page)
    this_page, other_url, other_umo = _seed_cache(plugin)

    out = _run(call(plugin, _Event()))
    assert "【错误】" not in out, f"原语本身不应失败：{out}"

    assert this_page not in plugin._vision_cache, "本页面缓存必须失效"
    assert other_url in plugin._vision_cache, "跨 URL 不得误删"
    assert other_umo in plugin._vision_cache, "跨会话不得误删"


@pytest.mark.parametrize(
    "call",
    [
        lambda p, e: p.browse_get_text(e),
        lambda p, e: p.browse_get_links(e),
        lambda p, e: p.browse_current_page(e),
    ],
    ids=["get_text", "get_links", "current_page"],
)
def test_read_only_primitives_do_not_invalidate(call):
    """纯读操作不得失效（否则识图缓存白做）。"""
    plugin, _page = _make_plugin()
    this_page, other_url, other_umo = _seed_cache(plugin)
    out = _run(call(plugin, _Event()))
    assert "【错误】" not in out
    assert this_page in plugin._vision_cache, "纯读操作不得清缓存"
    assert other_url in plugin._vision_cache
    assert other_umo in plugin._vision_cache


def test_invalidate_scoped_rule_and_return_count():
    """失效按 (umo, 规范化URL) 前缀删：返回删除数，URL 规范化后匹配。"""
    plugin, _page = _make_plugin()
    umo = "grp:1:GroupMessage:123"
    this_page, other_url, other_umo = _seed_cache(plugin)
    # fragment / 尾斜杠差异也应命中同一前缀
    frag = (umo, "https://example.com/spa", "zoom_crop", "0,0,10,10", "h2", "p", "v")
    plugin._vision_cache[frag] = (0.0, "描述")

    removed = plugin._vision_cache_invalidate(_Event(umo), "https://example.com/spa/#x")
    assert removed == 2
    assert frag not in plugin._vision_cache
    assert this_page not in plugin._vision_cache
    assert other_url in plugin._vision_cache
    assert other_umo in plugin._vision_cache


def test_invalidate_noop_without_url_or_umo_or_cache():
    """缺 URL / 缺会话 / 无缓存字典 ⇒ 返回 0，不抛异常。"""
    plugin, _page = _make_plugin()
    _seed_cache(plugin)
    assert plugin._vision_cache_invalidate(_Event(), "") == 0
    assert plugin._vision_cache_invalidate(None, "https://example.com/spa") == 0
    del plugin._vision_cache
    assert plugin._vision_cache_invalidate(_Event(), "https://example.com/spa") == 0


def test_interaction_does_not_invalidate_other_tab_entries():
    """切标签只失效「离开的那一页」，其它标签页的条目不动。"""
    page = _Page(url="https://example.com/tab1")
    plugin, _ = _make_plugin(page)
    tab1 = ("grp:1:GroupMessage:123", "https://example.com/tab1",
            "screenshot", "", "h1", "p", "v")
    tab2 = ("grp:1:GroupMessage:123", "https://example.com/tab2",
            "screenshot", "", "h2", "p", "v")
    plugin._vision_cache[tab1] = (0.0, "标签1")
    plugin._vision_cache[tab2] = (0.0, "标签2")
    _run(plugin.browse_switch_tab(_Event(), 2))
    assert tab1 not in plugin._vision_cache, "离开的标签页失效"
    assert tab2 in plugin._vision_cache, "其它标签页不得误删"


# ============================================================
# 内层指令与工具契约（臆造标签红线 / checkbox index 可见）
# ============================================================

def test_instruction_forbids_fabricated_option_labels():
    """内层指令明确禁止臆造选项标签（事故缺陷③的提示词侧兜底）。"""
    assert "选项标签必须来自页面实际文本" in _BROWSER_AGENT_INSTRUCTION
    assert "禁止臆造" in _BROWSER_AGENT_INSTRUCTION


def test_instruction_mentions_checkbox_index():
    """内层指令告知 browse_checkbox 的 index 用法。"""
    assert "index 指定第几个" in _BROWSER_AGENT_INSTRUCTION


def test_checkbox_tool_spec_exposes_optional_index():
    """工具 schema 暴露 index（可选），否则内层模型永远传不了它。"""
    spec = next(s for s in _BROWSER_TOOLS if s["name"] == "browse_checkbox")
    params = spec["parameters"]
    assert "index" in params["properties"]
    assert params["properties"]["index"]["type"] == "number"
    assert "index" not in params["required"]
    assert set(params["required"]) == {"selector", "checked"}
