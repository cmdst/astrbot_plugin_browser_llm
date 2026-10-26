"""tests.test_v175_executor — 执行层（core/executor.py）：定位阶梯 + CDP 受信任事件。

全部用 fake page / locator / CDP（**不需要真浏览器**），钉住规格 §4 的判据：

- 策略链顺序 `role+name > label > text > testid > css`，且 role/label 有 exact 二次尝试；
- 隐藏节点过滤（D2 的 tooltip 场景：`pointer-events:none` / `opacity:0` /
  `aria-hidden=true` / `display:none` 全部排除**再**取 index）；
- 歧义 ⇒ 候选表且**不点**；有 index ⇒ 只点第 index 个；0 命中 ⇒ 相近 top-5；
- 受信任事件：CDP 命令序列含 `mouseMoved → mousePressed → mouseReleased`，
  键盘路径含 `dispatchKeyEvent` / `insertText`；回退标 `trusted=False` + 可读 reason；
- 取消传播：`asyncio.CancelledError` 不被 `except Exception` 吞掉；
- `navigate` 超时但有正文 ⇒ `【超时但有内容】`（D3/D7）；
- 源码守卫：`core/executor.py` 里没有原生 type API 调用、`Input.` 出现 ≥5 次。
"""

from __future__ import annotations

import ast
import asyncio
import functools
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# 用**包限定名**导入：测试与生产（main.py 的 `from .core.x import …`）指向同一个
# 模块实例；否则 conftest 的 `core` 顶层别名会让 Rejection 出现两个类对象。
for _path in (str(_ROOT.parent), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from astrbot_plugin_browser_llm.core.action_model import Action, ParseContext, parse_action  # noqa: E402
from astrbot_plugin_browser_llm.core import executor as ex  # noqa: E402

EXECUTOR_PATH = _ROOT / "core" / "executor.py"

def asyncio_test(fn):
    """用 `asyncio.run` 驱动协程的测试装饰器。

    不依赖 `pytest-asyncio`（CI 只装 pytest / docstring_parser / aiohttp），
    也不需要 conftest 里设 `asyncio_mode`：装饰后的对象是**同步函数**，pytest 直接收集。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper



# ---------------------------------------------------------------------------
# fake 基础设施（鸭子类型：只实现 executor 真正调用的方法）
# ---------------------------------------------------------------------------


class FakeElement:
    """一个可探测/可点击的元素。"""

    def __init__(
        self,
        name: str = "提交",
        *,
        role: str = "button",
        box: tuple[float, float, float, float] | None = (100.0, 100.0, 80.0, 30.0),
        display: str = "block",
        visibility: str = "visible",
        opacity: str = "1",
        pointer_events: str = "auto",
        aria_hidden: str = "",
        disabled: bool = False,
        checked: bool | None = None,
        toggle_on_click: bool = False,
        options: list[str] | None = None,
        value: str = "",
    ) -> None:
        self.name = name
        self.role = role
        self.box = box
        self.display = display
        self.visibility = visibility
        self.opacity = opacity
        self.pointer_events = pointer_events
        self.aria_hidden = aria_hidden
        self.disabled = disabled
        self.checked = checked
        self.toggle_on_click = toggle_on_click
        self.options = options or []
        self.value = value
        self.clicks = 0
        self.fills: list[str] = []
        self.hovers = 0
        self.presses: list[str] = []
        self.scrolls = 0
        self.selected: list[dict] = []
        self.waits: list[dict] = []
        self.cancelled_on_evaluate = False

    # --- 探针 -----------------------------------------------------------

    async def evaluate(self, script: str, arg=None):
        if self.cancelled_on_evaluate:
            raise asyncio.CancelledError
        if "getComputedStyle" in script:
            return {
                "display": self.display,
                "visibility": self.visibility,
                "opacity": self.opacity,
                "pointer_events": self.pointer_events,
                "aria_hidden": self.aria_hidden,
                "disabled": self.disabled,
                "role": self.role,
                "name": self.name,
                "box": list(self.box) if self.box else None,
            }
        if "el.options" in script:
            return list(self.options)
        return None

    async def bounding_box(self):
        if self.box is None:
            return None
        x, y, width, height = self.box
        return {"x": x, "y": y, "width": width, "height": height}

    async def is_visible(self) -> bool:
        return self.box is not None and self.display != "none"

    async def is_enabled(self) -> bool:
        return not self.disabled

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolls += 1

    # --- 交互 -----------------------------------------------------------

    async def click(self, **kwargs) -> None:
        self.clicks += 1
        if self.toggle_on_click and self.checked is not None:
            self.checked = not self.checked

    async def fill(self, text: str, **kwargs) -> None:
        self.fills.append(text)
        self.value = text

    async def hover(self, **kwargs) -> None:
        self.hovers += 1

    async def press(self, key: str, **kwargs) -> None:
        self.presses.append(key)

    async def get_attribute(self, name: str) -> str:
        if name == "aria-checked":
            return "" if self.checked is None else ("true" if self.checked else "false")
        if name == "data-state":
            return ""
        if name == "aria-hidden":
            return self.aria_hidden
        return ""

    async def is_checked(self) -> bool:
        if self.checked is None:
            raise RuntimeError("Not a checkbox or radio button")
        return self.checked

    async def input_value(self) -> str:
        return self.value

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        self.waits.append({"state": state, "timeout": timeout})
        if state == "hidden" and self.box is not None:
            raise TimeoutError("still visible")
        if state == "visible" and self.box is None:
            raise TimeoutError("still hidden")

    async def select_option(self, **kwargs) -> None:
        self.selected.append(dict(kwargs))
        wanted = kwargs.get("label") or kwargs.get("value")
        if self.options and wanted not in self.options:
            raise RuntimeError(f"no option {wanted!r}")


class FakeLocator:
    """一个 locator：`count()` + `nth(i)` + 聚合操作。"""

    def __init__(self, elements: list[FakeElement] | None = None, label: str = "") -> None:
        self.elements = list(elements or [])
        self.label = label
        self.nth_calls: list[int] = []
        self.clicks = 0

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> FakeElement:
        self.nth_calls.append(index)
        return self.elements[index]

    async def click(self, **kwargs) -> None:
        self.clicks += 1
        if self.elements:
            await self.elements[0].click()

    async def fill(self, text: str, **kwargs) -> None:
        if self.elements:
            await self.elements[0].fill(text)

    async def hover(self, **kwargs) -> None:
        if self.elements:
            await self.elements[0].hover()

    async def press(self, key: str, **kwargs) -> None:
        if self.elements:
            await self.elements[0].press(key)

    async def select_option(self, **kwargs) -> None:
        if self.elements:
            await self.elements[0].select_option(**kwargs)

    async def evaluate(self, script: str, arg=None):
        if self.elements:
            return await self.elements[0].evaluate(script, arg)
        return None

    async def input_value(self) -> str:
        return self.elements[0].value if self.elements else ""

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        if self.elements:
            await self.elements[0].wait_for(state=state, timeout=timeout)


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeCDP:
    """假 CDP：记录命令序列；可选 `page` 让 mouseReleased 按坐标命中元素（模真）。"""

    def __init__(self, fail_methods: tuple[str, ...] = (), page=None) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail_methods = set(fail_methods)
        self.page = page

    async def send(self, method: str, params: dict) -> None:
        if method in self.fail_methods:
            raise RuntimeError(f"{method} failed")
        self.sent.append((method, dict(params)))
        if (
            self.page is not None
            and method == "Input.dispatchMouseEvent"
            and params.get("type") == "mouseReleased"
        ):
            await self.page.hit_test(float(params["x"]), float(params["y"]))

    @property
    def command_names(self) -> list[str]:
        out = []
        for method, params in self.sent:
            kind = params.get("type")
            out.append(f"{method}:{kind}" if isinstance(kind, str) else method)
        return out


class FakeContext:
    def __init__(self, cdp: FakeCDP | None = None, *, fail: bool = False) -> None:
        self.cdp = cdp
        self.fail = fail
        self.created = 0

    async def new_cdp_session(self, page):
        self.created += 1
        if self.fail:
            raise RuntimeError("no cdp")
        return self.cdp


class FakePage:
    """只实现 executor 会用到的 Playwright 表面。"""

    def __init__(
        self,
        *,
        role: dict[bool, list[FakeElement]] | None = None,
        label: dict[bool, list[FakeElement]] | None = None,
        text: list[FakeElement] | None = None,
        testid: list[FakeElement] | None = None,
        css: list[FakeElement] | None = None,
        viewport: tuple[int, int] = (1280, 800),
        url: str = "https://shop.example/order",
        body_text: str = "",
        goto_error: BaseException | None = None,
        cdp: FakeCDP | None = None,
        cdp_fail: bool = False,
        title: str = "确认订单",
    ) -> None:
        self.role = {True: [], False: [], **(role or {})}
        self.label = {True: [], False: [], **(label or {})}
        self.text = text or []
        self.testid = testid or []
        self.css = css or []
        self.viewport_size = {"width": viewport[0], "height": viewport[1]}
        self.url = url
        self.body_text = body_text
        self.goto_error = goto_error
        self.goto_calls = 0
        self.title_text = title
        self.keyboard = FakeKeyboard()
        self.evaluated: list[tuple[str, object]] = []
        self.waits: list[int] = []
        self.context = FakeContext(cdp, fail=cdp_fail)
        self.calls: list[tuple] = []

    # --- 定位工厂（记录调用顺序，供策略链断言） ---------------------------

    def get_by_role(self, role, name=None, exact=True):
        self.calls.append(("get_by_role", role, name, exact))
        elements = self.role.get(bool(exact), [])
        return FakeLocator(elements, label=f"role({role})")

    def get_by_label(self, label, exact=True):
        self.calls.append(("get_by_label", label, exact))
        return FakeLocator(self.label.get(bool(exact), []), label="label")

    def get_by_text(self, text, exact=False):
        self.calls.append(("get_by_text", text, exact))
        return FakeLocator(self.text, label="text")

    def get_by_test_id(self, testid):
        self.calls.append(("get_by_test_id", testid))
        return FakeLocator(self.testid, label="testid")

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakeLocator(self.css, label="css")

    # --- 页面表面 -------------------------------------------------------

    async def title(self) -> str:
        return self.title_text

    async def evaluate(self, script: str, arg=None, **_kwargs):
        self.evaluated.append((script, arg))
        if isinstance(arg, (int, float)) and "scrollTo" in script:
            self.body_text += ""
            return None
        if isinstance(arg, (int, float)) and "scrollBy" in script:
            return None
        return self.body_text

    async def goto(self, url: str, wait_until: str = "load", timeout: int | None = None):
        self.goto_calls += 1
        self.url = url
        if self.goto_error is not None:
            raise self.goto_error
        return SimpleNamespace(url=url)

    async def go_back(self, **kwargs):
        return SimpleNamespace(url="https://shop.example/cart")

    async def go_forward(self, **kwargs):
        return None

    async def reload(self, **kwargs):
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(int(ms))

    def all_elements(self) -> list[FakeElement]:
        pool: list[FakeElement] = []
        for group in (self.role, self.label):
            for elements in group.values():
                pool.extend(elements)
        pool.extend(self.text)
        pool.extend(self.testid)
        pool.extend(self.css)
        return pool

    async def hit_test(self, x: float, y: float) -> int:
        """命中测试：坐标落在哪个可见元素上就点它（模拟浏览器行为）。"""
        hits = 0
        for element in self.all_elements():
            if element.box is None or element.display == "none":
                continue
            bx, by, bw, bh = element.box
            if bx <= x <= bx + bw and by <= y <= by + bh:
                await element.click()
                hits += 1
        return hits

    def attach_cdp(self, cdp: FakeCDP) -> FakeCDP:
        cdp.page = self
        return cdp


def _snapshot(nodes: list[SimpleNamespace] | None = None, **kwargs) -> SimpleNamespace:
    fields = {
        "url": "https://shop.example/order",
        "title": "确认订单",
        "viewport": (1280, 800),
        "dpr": 1,
        "scroll_y": 0,
        "scroll_height": 1600,
        "nodes": tuple(nodes or ()),
        "pruned": 0,
        "frames": 1,
        "text": "",
        "ssrf_blocked": 0,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _node(role: str, name: str, box=None, **kwargs) -> SimpleNamespace:
    data = {"ref": 1, "role": role, "name": name, "value": "", "checked": None,
            "disabled": False, "frame_path": (), "box": box}
    data.update(kwargs)
    return SimpleNamespace(**data)


def _action(payload: dict, *, ctx: ParseContext | None = None) -> Action:
    action = parse_action(payload, ctx=ctx)
    assert isinstance(action, Action), action
    return action


# ---------------------------------------------------------------------------
# 策略链顺序
# ---------------------------------------------------------------------------


@asyncio_test
async def test_strategy_chain_order_role_first_then_loose():
    page = FakePage()
    page.get_by_role = lambda role, name=None, exact=True: (
        page.calls.append(("get_by_role", role, name, exact)) or FakeLocator([])
    )
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    result = await ex.resolve(page, _snapshot(), action)
    assert [call[0] for call in page.calls] == ["get_by_role", "get_by_role"]
    assert [call[3] for call in page.calls] == [True, False], "role 必须 exact=True 再 exact=False"
    assert result.ok is False and result.code == "E2_NOT_FOUND"


@asyncio_test
async def test_strategy_chain_falls_back_to_label_then_text():
    page = FakePage(text=[FakeElement("提交")])
    action = _action({"action": "click", "target": {"text": "提交"}})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is True
    assert result.strategy == "text"
    assert [call[0] for call in page.calls] == ["get_by_text"]


@asyncio_test
async def test_strategy_order_is_role_label_text_testid_css():
    """同一个 target 只会走它自己那条策略，但策略内部的 exact 二次尝试顺序必须钉死。"""
    page = FakePage(label={True: [FakeElement("搜索", role="textbox")]})
    action = _action({"action": "type", "target": {"label": "搜索"}, "value": "天气"})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is True
    assert result.strategy == "label"
    assert [call[0] for call in page.calls] == ["get_by_label"]

    page2 = FakePage(testid=[FakeElement("搜索", role="textbox")])
    action2 = _action({"action": "type", "target": {"testid": "search-input"}, "value": "x"})
    result2 = await ex.resolve(page2, _snapshot(), action2)
    assert result2.ok and result2.strategy == "testid"

    page3 = FakePage(css=[FakeElement("提交")])
    action3 = _action({"action": "click", "target": {"css": "#submit"}})
    result3 = await ex.resolve(page3, _snapshot(), action3)
    assert result3.ok and result3.strategy == "css"


# ---------------------------------------------------------------------------
# 隐藏节点过滤（D2 的 tooltip 场景）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"display": "none"}, "display:none"),
        ({"visibility": "hidden"}, "visibility:hidden"),
        ({"opacity": "0"}, "opacity:0"),
        ({"pointer_events": "none"}, "pointer-events:none"),
        ({"aria_hidden": "true"}, "aria-hidden=true"),
        ({"box": None}, "zero-size"),
    ],
)
@asyncio_test
async def test_hidden_candidates_are_filtered(kwargs, reason):
    hidden = FakeElement("提交", **kwargs)
    page = FakePage(text=[hidden])
    action = _action({"action": "click", "target": {"text": "提交"}})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is False
    assert result.code == "E2_NOT_FOUND"
    assert "全部不可见" in result.reason
    assert hidden.clicks == 0


@asyncio_test
async def test_tooltip_scenario_picks_the_visible_one():
    """D2 复现：同名节点里第一个是隐藏 tooltip，必须落到第二个（可见的）那个。"""
    tooltip = FakeElement("提交", role="tooltip", pointer_events="none")
    real = FakeElement("提交", role="button")
    page = FakePage(text=[tooltip, real])
    action = _action({"action": "click", "target": {"text": "提交"}})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is True
    assert result.locator is real
    assert [cand.visible for cand in result.candidates] == [True]
    assert all(cand.hidden_reason == "" for cand in result.candidates)


@asyncio_test
async def test_hidden_reason_is_reported_for_each_rule():
    assert ex._hidden_reason({"display": "none"}) == "display:none"
    assert ex._hidden_reason({"opacity": "0.0"}) == "opacity:0"
    assert ex._hidden_reason({"pointer_events": "none"}) == "pointer-events:none"
    assert ex._hidden_reason({"aria_hidden": "true"}) == "aria-hidden=true"
    assert ex._hidden_reason({"box": [0, 0, 0, 0]}) == "zero-size"
    assert ex._hidden_reason({"box": [1, 2, 3, 4]}) == ""


# ---------------------------------------------------------------------------
# 多命中：歧义 / index / 0 命中建议
# ---------------------------------------------------------------------------


@asyncio_test
async def test_ambiguous_without_index_returns_candidates_and_does_not_click():
    first, second, third = (FakeElement(f"选项{i}") for i in range(3))
    page = FakePage(text=[first, second, third])
    action = _action({"action": "click", "target": {"text": "选项"}})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is False
    assert result.code == "E2_AMBIGUOUS"
    assert len(result.candidates) == 3
    assert "index" in result.render()
    assert [first.clicks, second.clicks, third.clicks] == [0, 0, 0]


@asyncio_test
async def test_index_selects_exactly_that_candidate():
    elements = [FakeElement(f"选项{i}") for i in range(3)]
    page = FakePage(text=elements)
    action = _action({"action": "click", "target": {"text": "选项"}, "index": 1})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is True
    assert result.locator is elements[1]
    assert result.index == 1


@asyncio_test
async def test_index_counts_filtered_candidates_not_raw_matches():
    """index 作用在「过滤后的候选序列」上（隐藏的不占位）—— 规格 §4.1 的索引口径。"""
    hidden = FakeElement("选项0", display="none")
    visible_a = FakeElement("选项1")
    visible_b = FakeElement("选项2")
    page = FakePage(text=[hidden, visible_a, visible_b])
    action = _action({"action": "click", "target": {"text": "选项"}, "index": 1})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is True
    assert result.locator is visible_b, "隐藏节点不占位，index=1 应落到第二个可见元素"


@asyncio_test
async def test_index_out_of_range_lists_candidates():
    page = FakePage(text=[FakeElement("提交")])
    action = _action({"action": "click", "target": {"text": "提交"}, "index": 5})
    result = await ex.resolve(page, _snapshot(), action)
    assert result.ok is False
    assert result.code == "E2_INDEX_OUT_OF_RANGE"
    assert result.candidates
    assert "0 起算" in result.reason


@asyncio_test
async def test_zero_match_gives_similar_names_top5():
    nodes = [
        _node("link", "提交订单"),
        _node("link", "提交留言"),
        _node("link", "提交申请"),
        _node("button", "提交表单"),
        _node("button", "提交"),
        _node("link", "取消订单"),
        _node("link", "帮助中心"),
    ]
    page = FakePage()
    action = _action({"action": "click", "target": {"text": "提交订单页"}})
    result = await ex.resolve(page, _snapshot(nodes), action)
    assert result.ok is False
    assert result.code == "E2_NOT_FOUND"
    assert 1 <= len(result.suggestions) <= 5
    assert any("提交" in item for item in result.suggestions)


@asyncio_test
async def test_resolve_orders_candidates_by_ax_snapshot():
    """AX 顺序优先于 locator 顺序（规格 §4.1「先按 AX 定候选」）。"""
    dom_second = FakeElement("下一页")
    dom_first = FakeElement("下一页")
    page = FakePage(text=[dom_second, dom_first])
    nodes = [_node("link", "下一页", box=(0, 0, 10, 10))]
    action = _action({"action": "click", "target": {"text": "下一页"}})
    result = await ex.resolve(page, _snapshot(nodes), action)
    # 两个同名候选 ⇒ 歧义（不点），但候选顺序仍按 locator 与 AX 对账后稳定
    assert result.code == "E2_AMBIGUOUS"
    assert len(result.candidates) == 2


@asyncio_test
async def test_role_strategy_must_exist_in_ax_tree():
    """规格 §4.1：role 候选必须先落在 AX 树上 —— 观测里没出现过的元素不许点。"""
    ghost = FakeElement("幽灵按钮")
    page = FakePage(role={True: [ghost]})
    nodes = [_node("button", "真实按钮")]
    action = _action({"action": "click", "target": {"role": "button", "name": "幽灵按钮"}})
    result = await ex.resolve(page, _snapshot(nodes), action)
    assert result.ok is False
    assert result.code == "E2_NOT_FOUND"
    assert "AX 树里没有" in result.reason
    assert ghost.clicks == 0


@asyncio_test
async def test_ax_gating_relaxed_when_nodes_are_pruned():
    """快照被裁剪过（pruned>0）⇒ 不做强校验，退回 locator 对账，避免误杀。"""
    element = FakeElement("被裁掉的按钮")
    page = FakePage(role={True: [element]})
    action = _action({"action": "click", "target": {"role": "button", "name": "被裁掉的按钮"}})
    result = await ex.resolve(page, _snapshot([], pruned=12), action)
    assert result.ok is True
    assert result.locator is element


@asyncio_test
async def test_ax_gating_skipped_without_snapshot():
    element = FakeElement("提交")
    page = FakePage(role={True: [element]})
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    assert (await ex.resolve(page, None, action)).ok is True


@asyncio_test
async def test_radio_group_index_uses_ax_candidates():
    """D4/D5：单选组（同 role+name 前缀）按 **AX 候选序** 定 index，而不是 DOM 序。"""
    dom_other = FakeElement("标准配送", role="radio", box=(10.0, 100.0, 20.0, 20.0))
    dom_target = FakeElement("加急配送", role="radio", box=(10.0, 140.0, 20.0, 20.0))
    nodes = [
        _node("radio", "加急配送", box=(10, 140, 20, 20)),
        _node("radio", "标准配送", box=(10, 100, 20, 20)),
    ]

    # ① 精确 name 命中：locator 只回目标那个
    page = FakePage(role={True: [dom_target]})
    action = _action({"action": "check", "target": {"role": "radio", "name": "加急配送"}})
    exact = await ex.resolve(page, _snapshot(nodes), action)
    assert exact.ok is True
    assert exact.locator is dom_target
    assert exact.node is not None and exact.node.name == "加急配送"

    # ② 宽松 name 命中（DOM 顺序是「标准配送 → 加急配送」）：index 作用在 AX 候选序上
    loose_page = FakePage(role={False: [dom_other, dom_target]})
    indexed = _action({"action": "check", "target": {"role": "radio", "name": "配送"}, "index": 1})
    loose = await ex.resolve(loose_page, _snapshot(nodes), indexed)
    assert loose.ok is True
    assert loose.locator is dom_other, "AX 序里第 2 个是「标准配送」，与 DOM 顺序相反"
    assert [cand.name for cand in loose.candidates] == ["加急配送", "标准配送"]


@asyncio_test
async def test_resolution_candidates_feed_no_change_verdict():
    """D4 的两条候选路径要能接到验证层：0 匹配给 suggestions、歧义给 candidates。"""
    page = FakePage(text=[FakeElement("甲"), FakeElement("乙")])
    action = _action({"action": "click", "target": {"text": "选项"}})
    ambiguous = await ex.resolve(page, _snapshot([_node("button", "选项甲")]), action)
    assert ambiguous.ok is False
    lines = ambiguous.candidate_lines()
    assert lines and all(isinstance(line, str) for line in lines)

    page2 = FakePage()
    missing = await ex.resolve(page2, _snapshot([_node("button", "选项甲")]), action)
    assert missing.ok is False and missing.candidate_lines(), "0 命中也要给相近候选"


# ---------------------------------------------------------------------------
# mark（SoM）策略
# ---------------------------------------------------------------------------


@asyncio_test
async def test_mark_resolves_via_epoch_and_role_name():
    element = FakeElement("提交订单", role="button")
    page = FakePage(role={True: [element]})
    node = _node("button", "提交订单", box=(10, 20, 30, 40))
    action = _action({"action": "click", "target": {"mark": 2}}, ctx=ParseContext(mark_epoch={2: node}))
    result = await ex.resolve(page, _snapshot([node]), action, mark_epoch={2: node})
    assert result.ok is True
    assert result.locator is element
    assert result.strategy == "mark"


@asyncio_test
async def test_mark_without_locator_requires_coordinate_click():
    page = FakePage()
    node = _node("button", "canvas 按钮", box=(10, 20, 30, 40))
    action = _action({"action": "click", "target": {"mark": 1}}, ctx=ParseContext(mark_epoch={1: node}))
    result = await ex.resolve(page, _snapshot([node]), action, mark_epoch={1: node})
    assert result.ok is True
    assert result.locator is None
    assert result.requires_coordinates is True
    assert result.box == (10.0, 20.0, 30.0, 40.0)


@asyncio_test
async def test_mark_not_in_epoch_is_structured_failure():
    page = FakePage()
    action = _action({"action": "click", "target": {"mark": 7}}, ctx=ParseContext(mark_epoch={7: _node("button", "x")}))
    result = await ex.resolve(page, _snapshot(), action, mark_epoch={1: _node("button", "x")})
    assert result.ok is False
    assert result.code == "E2_MARK_EXPIRED"


# ---------------------------------------------------------------------------
# CDP session（注入式）
# ---------------------------------------------------------------------------


@asyncio_test
async def test_ensure_cdp_uses_injected_provider_and_caches():
    page = FakePage(cdp=FakeCDP())
    ex.drop_cdp(page)
    first = await ex.ensure_cdp(page)
    second = await ex.ensure_cdp(page)
    assert first is not None and first is second
    assert page.context.created == 1, "同一 page 只建一条 CDP session"
    ex.drop_cdp(page)


@asyncio_test
async def test_ensure_cdp_returns_none_when_unavailable():
    page = FakePage(cdp_fail=True)
    ex.drop_cdp(page)
    assert await ex.ensure_cdp(page) is None
    ex.drop_cdp(page)


# ---------------------------------------------------------------------------
# 受信任事件：点击
# ---------------------------------------------------------------------------


@asyncio_test
async def test_trusted_click_sends_mouse_sequence():
    cdp = FakeCDP()
    element = FakeElement("提交")
    report = await ex.trusted_click(FakePage(), element, cdp=cdp)
    assert report.trusted is True
    assert report.used == "cdp"
    assert report.commands == (
        "Input.dispatchMouseEvent:mouseMoved",
        "Input.dispatchMouseEvent:mousePressed",
        "Input.dispatchMouseEvent:mouseReleased",
    )
    assert cdp.sent[1][1]["button"] == "left"
    assert cdp.sent[1][1]["clickCount"] == 1
    assert cdp.sent[0][1]["button"] == "none"
    # 中心点 = (100+40, 100+15)
    assert (cdp.sent[0][1]["x"], cdp.sent[0][1]["y"]) == (140.0, 115.0)
    assert element.clicks == 0, "CDP 路径不应再调 element.click()"


@asyncio_test
async def test_trusted_click_falls_back_when_cdp_missing():
    element = FakeElement("提交")
    report = await ex.trusted_click(FakePage(), element, cdp=None)
    assert report.trusted is False
    assert report.used == "locator"
    assert "CDP 会话不可用" in report.reason
    assert element.clicks == 1


@asyncio_test
async def test_trusted_click_falls_back_when_coordinates_outside_viewport():
    cdp = FakeCDP()
    element = FakeElement("提交", box=(5000.0, 10.0, 10.0, 10.0))
    report = await ex.trusted_click(FakePage(viewport=(1280, 800)), element, cdp=cdp)
    assert report.trusted is False
    assert "坐标越界" in report.reason
    assert cdp.sent == []
    assert element.clicks == 1


@asyncio_test
async def test_trusted_click_scrolls_then_falls_back_without_box():
    cdp = FakeCDP()
    element = FakeElement("提交", box=None)
    report = await ex.trusted_click(FakePage(), element, cdp=cdp)
    assert element.scrolls == 1, "box 为 None 应先 scroll_into_view_if_needed()"
    assert report.trusted is False
    assert "bounding box" in report.reason


@asyncio_test
async def test_trusted_click_falls_back_when_cdp_send_fails():
    cdp = FakeCDP(fail_methods=("Input.dispatchMouseEvent",))
    element = FakeElement("提交")
    report = await ex.trusted_click(FakePage(), element, cdp=cdp)
    assert report.trusted is False
    assert "Input.dispatchMouseEvent 失败" in report.reason
    assert element.clicks == 1


# ---------------------------------------------------------------------------
# 受信任事件：键盘 / 输入 / 悬停 / 滚动
# ---------------------------------------------------------------------------


@asyncio_test
async def test_trusted_type_uses_insert_text():
    cdp = FakeCDP()
    element = FakeElement("搜索", role="textbox", value="")
    report = await ex.trusted_focus_and_type(FakePage(), "天气 预报", element=element, cdp=cdp)
    assert report.trusted is True
    assert "Input.insertText" in report.commands
    assert element.fills == [""], "受信任路径只用来清空，文本必须走 insertText"
    assert element.clicks == 0


@asyncio_test
async def test_trusted_type_with_submit_sends_enter_key_events():
    cdp = FakeCDP()
    element = FakeElement("搜索", role="textbox")
    report = await ex.trusted_focus_and_type(
        FakePage(), "天气", element=element, cdp=cdp, submit=True
    )
    assert report.commands[:3] == (
        "Input.dispatchMouseEvent:mouseMoved",
        "Input.dispatchMouseEvent:mousePressed",
        "Input.dispatchMouseEvent:mouseReleased",
    )
    assert "Input.insertText" in report.commands
    assert report.commands[-3:] == (
        "Input.dispatchKeyEvent:rawKeyDown",
        "Input.dispatchKeyEvent:char",
        "Input.dispatchKeyEvent:keyUp",
    )


@asyncio_test
async def test_trusted_type_falls_back_to_fill_without_cdp():
    element = FakeElement("搜索", role="textbox")
    report = await ex.trusted_focus_and_type(FakePage(), "天气", element=element, cdp=None)
    assert report.trusted is False
    assert report.used == "locator"
    assert element.fills == ["天气"]
    assert "locator.fill()" in report.reason


@asyncio_test
async def test_press_key_enter_sends_char_event():
    cdp = FakeCDP()
    report = await ex.press_key(FakePage(), "Enter", cdp=cdp)
    assert report.trusted is True
    assert report.commands == (
        "Input.dispatchKeyEvent:rawKeyDown",
        "Input.dispatchKeyEvent:char",
        "Input.dispatchKeyEvent:keyUp",
    )
    char_params = cdp.sent[1][1]
    assert char_params["text"] == "\r", "Enter 必须发 char(text='\\r') 才能提交表单"
    assert cdp.sent[0][1]["windowsVirtualKeyCode"] == 13


@asyncio_test
async def test_press_key_falls_back_to_page_keyboard():
    page = FakePage()
    report = await ex.press_key(page, "Escape", cdp=None)
    assert report.trusted is False
    assert report.used == "page"
    assert page.keyboard.pressed == ["Escape"]
    assert "CDP 不可用" in report.reason


@asyncio_test
async def test_press_key_prefers_locator_press():
    element = FakeElement("搜索")
    report = await ex.press_key(FakePage(), "Tab", cdp=None, element=element)
    assert report.used == "locator"
    assert element.presses == ["Tab"]


@asyncio_test
async def test_hover_sends_mouse_moved_and_settles():
    cdp = FakeCDP()
    page = FakePage()
    report = await ex.hover_at(page, FakeElement("更多"), cdp=cdp)
    assert report.trusted is True
    assert report.commands == ("Input.dispatchMouseEvent:mouseMoved",)
    assert page.waits == [ex.HOVER_SETTLE_MS], "悬停后要等 120ms 让菜单出现"


@asyncio_test
async def test_hover_falls_back_to_locator():
    element = FakeElement("更多")
    report = await ex.hover_at(FakePage(), element, cdp=None)
    assert report.trusted is False
    assert element.hovers == 1


@asyncio_test
async def test_scroll_prefers_cdp_wheel():
    cdp = FakeCDP()
    report = await ex.scroll_at(FakePage(), -640, cdp=cdp)
    assert report.trusted is True
    assert report.commands == ("Input.dispatchMouseEvent:mouseWheel",)
    assert cdp.sent[0][1]["deltaY"] == -640


@asyncio_test
async def test_scroll_falls_back_to_single_arg_evaluate():
    page = FakePage()
    report = await ex.scroll_at(page, 800, cdp=None)
    assert report.trusted is False
    assert report.used == "page"
    assert page.evaluated, "回退路径必须只传 1 个位置实参（F1）"
    script, arg = page.evaluated[0]
    assert "scrollBy" in script and arg == 800


# ---------------------------------------------------------------------------
# 16 动作矩阵（apply）
# ---------------------------------------------------------------------------


@asyncio_test
async def test_apply_click_uses_trusted_path_and_reports_commands():
    cdp = FakeCDP()
    element = FakeElement("提交")
    page = FakePage(role={True: [element]}, cdp=cdp)
    ex.drop_cdp(page)
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    result = await ex.apply(page, action, cdp=cdp)
    assert result.ok is True
    assert result.trusted is True
    assert result.commands[0] == "Input.dispatchMouseEvent:mouseMoved"
    ex.drop_cdp(page)


@asyncio_test
async def test_apply_click_native_agrees_with_trusted_path():
    """判据 §4-2：受信任与原生两条路径的**最终页面状态一致**（对拍）。"""
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})

    trusted_el = FakeElement("提交", toggle_on_click=True, checked=False)
    trusted_page = FakePage(role={True: [trusted_el]}, cdp=FakeCDP())
    trusted_page.attach_cdp(trusted_page.context.cdp)
    ex.drop_cdp(trusted_page)
    trusted = await ex.apply(trusted_page, action, cdp=trusted_page.context.cdp)
    ex.drop_cdp(trusted_page)

    native_el = FakeElement("提交", toggle_on_click=True, checked=False)
    native_page = FakePage(role={True: [native_el]})
    native = await ex.apply(native_page, action, trusted_events=False)

    assert trusted.ok and native.ok
    assert trusted.trusted is True and native.trusted is False
    assert trusted_el.checked == native_el.checked == True  # noqa: E712 — 对拍的是页面状态


@asyncio_test
async def test_apply_click_reports_location_failure_with_candidates():
    page = FakePage(text=[FakeElement("甲"), FakeElement("乙")])
    action = _action({"action": "click", "target": {"text": "选项"}})
    result = await ex.apply(page, action, cdp=FakeCDP())
    assert result.ok is False
    assert "【歧义】" in result.reason
    assert result.candidates


@asyncio_test
async def test_apply_type_writes_and_reports_length():
    cdp = FakeCDP()
    element = FakeElement("搜索", role="textbox")
    page = FakePage(label={True: [element]}, cdp=cdp)
    action = _action({"action": "type", "target": {"label": "搜索"}, "value": "上海天气"})
    result = await ex.apply(page, action, cdp=cdp)
    assert result.ok is True
    assert result.value == len("上海天气")
    assert "Input.insertText" in result.commands


@asyncio_test
async def test_apply_select_reports_all_options_when_missing():
    element = FakeElement("配送方式", role="combobox", options=["标准配送", "加急配送"])
    page = FakePage(label={True: [element]})
    action = _action({"action": "select", "target": {"label": "配送方式"}, "value": "自提"})
    result = await ex.apply(page, action, cdp=FakeCDP())
    assert result.ok is False
    assert "标准配送" in result.reason and "加急配送" in result.reason


@asyncio_test
async def test_apply_select_succeeds_by_label():
    element = FakeElement("配送方式", role="combobox", options=["标准配送", "加急配送"])
    page = FakePage(label={True: [element]})
    action = _action({"action": "select", "target": {"label": "配送方式"}, "value": "加急配送"})
    result = await ex.apply(page, action, cdp=FakeCDP())
    assert result.ok is True
    assert element.selected == [{"label": "加急配送"}]


@asyncio_test
async def test_apply_check_noop_when_already_checked():
    element = FakeElement("同意", role="checkbox", checked=True)
    page = FakePage(label={True: [element]})
    action = _action({"action": "check", "target": {"label": "同意"}})
    result = await ex.apply(page, action, cdp=FakeCDP())
    assert result.ok is True
    assert "无需点击" in result.detail
    assert element.clicks == 0


@asyncio_test
async def test_apply_check_toggles_and_reads_back():
    element = FakeElement("同意", role="checkbox", checked=False, toggle_on_click=True)
    page = FakePage(label={True: [element]}, cdp=FakeCDP())
    page.attach_cdp(page.context.cdp)
    ex.drop_cdp(page)
    action = _action({"action": "check", "target": {"label": "同意"}})
    result = await ex.apply(page, action, cdp=page.context.cdp)
    assert result.ok is True
    assert element.checked is True
    assert result.value == "checked"
    ex.drop_cdp(page)


@asyncio_test
async def test_apply_check_detects_state_not_changing():
    element = FakeElement("同意", role="checkbox", checked=False, toggle_on_click=False)
    page = FakePage(label={True: [element]})
    action = _action({"action": "check", "target": {"label": "同意"}})
    result = await ex.apply(page, action, cdp=None, trusted_events=False)
    assert result.ok is False
    assert "【无变化】" in result.reason


@asyncio_test
async def test_apply_navigate_timeout_with_content():
    """判据 §4-3（D3/D7）：超时但有正文 ⇒ 【超时但有内容】，且不是【错误】。"""
    page = FakePage(goto_error=TimeoutError("page load timeout"), body_text="正文" * 40)
    action = _action({"action": "navigate", "value": "https://shop.example/slow"})
    result = await ex.apply(page, action)
    assert result.ok is True
    assert "【超时但有内容】" in result.detail
    assert "【错误】" not in result.detail
    assert page.goto_calls == 1
    assert result.attempt == 1, "第一次就拿到正文 ⇒ attempt=1"
    assert result.to_payload()["elapsed_ms"] >= 0 and "attempt" in result.to_payload()


@asyncio_test
async def test_apply_navigate_timeout_without_content_retries_once():
    page = FakePage(goto_error=TimeoutError("page load timeout"), body_text="")
    action = _action({"action": "navigate", "value": "https://shop.example/dead"})
    result = await ex.apply(page, action)
    assert result.ok is False
    assert "【超时】" in result.reason
    assert page.goto_calls == 2
    assert result.attempt == 2, "0 正文 ⇒ 重试 1 次，返回体 attempt=2（D3/D7）"


@asyncio_test
async def test_apply_navigate_hard_error_is_reported():
    page = FakePage(goto_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    action = _action({"action": "navigate", "value": "https://nope.invalid/"})
    result = await ex.apply(page, action)
    assert result.ok is False
    assert "【错误】" in result.reason


@asyncio_test
async def test_apply_back_without_history_is_unavailable():
    page = FakePage()
    page.go_back = lambda **kwargs: _none()
    action = _action({"action": "back"})
    result = await ex.apply(page, action)
    assert result.ok is False
    assert "【不可用】" in result.reason


async def _none():
    return None


@asyncio_test
async def test_apply_scroll_top_uses_scroll_to():
    page = FakePage()
    action = _action({"action": "scroll", "value": "top"})
    result = await ex.apply(page, action)
    assert result.ok is True
    assert any("scrollTo" in script for script, _ in page.evaluated)


@asyncio_test
async def test_apply_wait_caps_at_action_timeout():
    page = FakePage()
    action = _action({"action": "wait", "value": 30000})
    result = await ex.apply(page, action, action_timeout_s=0.5)
    assert result.ok is True
    assert page.waits == [500], "wait 必须夹到 action_timeout_s*1000"


@asyncio_test
async def test_apply_read_text_and_links():
    page = FakePage(body_text="正文内容")
    text_result = await ex.apply(page, _action({"action": "read", "value": "text"}))
    assert text_result.ok and text_result.value == "正文内容"
    links_result = await ex.apply(page, _action({"action": "read", "value": "links"}))
    assert links_result.ok and isinstance(links_result.value, list)


@asyncio_test
async def test_apply_screenshot_without_som_is_unavailable():
    page = FakePage()
    result = await ex.apply(page, _action({"action": "screenshot", "value": "viewport"}))
    assert result.ok is False
    assert "【不可用】" in result.reason


@asyncio_test
async def test_apply_screenshot_with_injected_som():
    page = FakePage()

    async def fake_som(page_, target=None, mode="viewport"):
        return {"labels": 3, "mode": mode}

    result = await ex.apply(
        page, _action({"action": "screenshot", "value": "full"}), som=fake_som
    )
    assert result.ok is True
    assert result.value == {"labels": 3, "mode": "full"}


@asyncio_test
async def test_apply_tab_requires_session_manager():
    result = await ex.apply(FakePage(), _action({"action": "tab", "value": "list"}))
    assert result.ok is False
    assert "未注入标签页管理器" in result.reason


@asyncio_test
async def test_apply_tab_switch_uses_one_based_index():
    class FakeTabs:
        def __init__(self) -> None:
            self.switched: list[int] = []

        async def list_tabs(self):
            return ["https://a/", "https://b/"]

        async def switch_tab(self, index: int):
            self.switched.append(index)

    tabs = FakeTabs()
    result = await ex.apply(
        FakePage(),
        _action({"action": "tab", "value": "switch", "index": 2}),
        tabs=tabs,
    )
    assert result.ok is True
    assert tabs.switched == [2]

    bad = await ex.apply(
        FakePage(), _action({"action": "tab", "value": "switch", "index": 0}), tabs=tabs
    )
    assert bad.ok is False and "1 起算" in bad.reason


@asyncio_test
async def test_apply_result_always_carries_elapsed_and_attempt():
    page = FakePage()
    for payload in (
        {"action": "done", "value": "结论"},
        {"action": "wait", "value": 0},
        {"action": "reload"},
    ):
        result = await ex.apply(page, _action(payload), goto_timeout_s=0.2)
        assert result.to_payload()["elapsed_ms"] >= 0
        assert result.to_payload()["attempt"] >= 1


@asyncio_test
async def test_reload_receives_goto_and_action_timeouts():
    """验收口径 §5：navigate/reload 的超时来自**显式参数**（resources.goto_timeout_s / action_timeout_s）。"""
    page = FakePage()
    seen: dict[str, object] = {}

    async def slow_reload(**kwargs):
        seen.update(kwargs)

    page.reload = slow_reload
    result = await ex.apply(page, _action({"action": "reload"}), goto_timeout_s=7)
    assert seen["timeout"] == 7000, "reload 必须用 goto_timeout_s*1000"
    assert result.ok is True


@asyncio_test
async def test_reload_timeout_without_content_marks_attempt_two():
    page = FakePage(body_text="")

    async def slow_reload(**kwargs):
        raise TimeoutError("reload timeout")

    page.reload = slow_reload
    result = await ex.apply(page, _action({"action": "reload"}))
    assert result.ok is False
    assert "【超时】" in result.reason
    assert result.attempt == 2


@asyncio_test
async def test_apply_done_returns_conclusion():
    result = await ex.apply(FakePage(), _action({"action": "done", "value": "答案在第二条"}))
    assert result.ok is True
    assert result.value == "答案在第二条"


# ---------------------------------------------------------------------------
# 取消传播（D9 的形态：CancelledError 被吞掉 ⇒ Future exception was never retrieved）
# ---------------------------------------------------------------------------


@asyncio_test
async def test_cancelled_error_propagates_from_probe():
    cancelled = FakeElement("提交")
    cancelled.cancelled_on_evaluate = True
    page = FakePage(text=[cancelled])
    action = _action({"action": "click", "target": {"text": "提交"}})
    with pytest.raises(asyncio.CancelledError):
        await ex.apply(page, action, cdp=FakeCDP())


@asyncio_test
async def test_cancelled_error_propagates_from_goto():
    page = FakePage(goto_error=asyncio.CancelledError())
    action = _action({"action": "navigate", "value": "https://shop.example/x"})
    with pytest.raises(asyncio.CancelledError):
        await ex.apply(page, action)


@asyncio_test
async def test_cancelled_error_propagates_from_cdp_session():
    class CancellingContext:
        async def new_cdp_session(self, page):
            raise asyncio.CancelledError()

    page = FakePage()
    page.context = CancellingContext()
    ex.drop_cdp(page)
    with pytest.raises(asyncio.CancelledError):
        await ex.ensure_cdp(page)


# ---------------------------------------------------------------------------
# 源码守卫（判据 §4-4 / §4-5）
# ---------------------------------------------------------------------------


def test_source_has_no_native_type_api_call():
    """AST 守卫：`core/executor.py` 里不得出现原生 type API（D6 的编译期化）。"""
    tree = ast.parse(EXECUTOR_PATH.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "type":
                offenders.append(node.func.attr)
    assert offenders == [], f"禁止使用原生 type API：{offenders}"


def test_source_dispatches_at_least_five_input_commands():
    source = EXECUTOR_PATH.read_text(encoding="utf-8")
    assert source.count("Input.") >= 5, "click/type/press/hover/scroll 五处受信任事件必须都在"
    for method in (
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.insertText",
    ):
        assert method in source


def test_evaluate_calls_take_one_positional_arg():
    """D1/D8：本任务新增的 `evaluate(...)` 调用**全部单参数**。

    规格 §8.2 的下限是「位置实参 <= 2」（`page.evaluate(js, arg)` 合法）；验收口径要求
    更严：要传值统一用 `arg=` 关键字，位置实参只留 `expression` 一个。
    """
    offenders = []
    for path in (EXECUTOR_PATH, _ROOT / "core" / "verify.py", _ROOT / "core" / "action_model.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "evaluate" and len(node.args) > 1:
                    offenders.append((path.name, node.lineno, len(node.args)))
    assert offenders == [], f"evaluate 位置实参必须为 1（D1/D8）：{offenders}"


def test_source_does_not_import_playwright_at_module_level():
    """层纪律：模块顶层不 import playwright（没有浏览器也能加载）。"""
    source = EXECUTOR_PATH.read_text(encoding="utf-8")
    head = source.split('"""', 2)[-1].splitlines()
    imports = [line for line in head if line.startswith(("import ", "from "))]
    assert not any("playwright" in line for line in imports)
