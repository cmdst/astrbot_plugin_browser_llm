"""tests.test_v175_verify — 验证层（core/verify.py）：期望 / 差分 / 无变化 / 重复守卫。

钉住规格 §5 的判据与任务契约里的四条硬要求：

- V1 期望：`url_contains` / `text_contains` / `role+name` / `count` 逐项求值，全满足才 `verified`；
- V2 差分：六类（url/title/nodes/text/scroll/focus）至少一类变化 ⇒ `changed`；
- V3 无变化：六类全不变 ⇒ `no_change`，文案**逐字**含 `【无变化】`，并附候选（D2 的根因 2）；
- 重复守卫：同一 (action, 规范化 target) 连续第 3 次 ⇒ 拒绝，且提示「换一种定位方式」（D2 根因 3）；
- 勾选态三路读回 `aria-checked` → 原生 `checked` → `data-state`（D5）；
- `type` 之后 `value` 没生效 ⇒ **明确失败**（「点了成功」≠「状态变了」）；
- 观测块三段固定顺序 + `【预算】` 只在剩余 < 60s 时出现。
"""

from __future__ import annotations

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
from astrbot_plugin_browser_llm.core.action_model import Action, ParseContext, Rejection, parse_action  # noqa: E402
from astrbot_plugin_browser_llm.core import verify as vf  # noqa: E402


def asyncio_test(fn):
    """用 `asyncio.run` 驱动协程的测试装饰器（不依赖 pytest-asyncio）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeLocator:
    """只实现 verify 会问的表面。"""

    def __init__(
        self,
        *,
        aria_checked: str = "",
        native_checked: bool | None = None,
        data_state: str = "",
        value: str = "",
        count: int = 1,
        visible: bool = True,
    ) -> None:
        self.aria_checked = aria_checked
        self.native_checked = native_checked
        self.data_state = data_state
        self.value = value
        self._count = count
        self.visible = visible
        self.waits: list[dict] = []

    async def get_attribute(self, name: str) -> str:
        if name == "aria-checked":
            return self.aria_checked
        if name == "data-state":
            return self.data_state
        return ""

    async def is_checked(self) -> bool:
        if self.native_checked is None:
            raise RuntimeError("Not a checkbox or radio button")
        return self.native_checked

    async def is_enabled(self) -> bool:
        return True

    async def input_value(self) -> str:
        return self.value

    async def count(self) -> int:
        return self._count

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        self.waits.append({"state": state, "timeout": timeout})
        if state == "visible" and not self.visible:
            raise TimeoutError("not visible")


class FakePage:
    def __init__(
        self,
        *,
        url: str = "https://shop.example/order",
        title: str = "确认订单",
        text: str = "正文",
        active: str = "button:提交",
        scroll_y: int = 0,
        nodes: int = 120,
        title_error: BaseException | None = None,
        evaluate_error: BaseException | None = None,
    ) -> None:
        self.url = url
        self.title_text = title
        self.text = text
        self.active = active
        self.scroll_y = scroll_y
        self.nodes = nodes
        self.title_error = title_error
        self.evaluate_error = evaluate_error
        self.evaluated: list[str] = []

    async def title(self) -> str:
        if self.title_error is not None:
            raise self.title_error
        return self.title_text

    async def evaluate(self, script: str, arg=None):
        self.evaluated.append(script)
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return {
            "text": self.text,
            "active": self.active,
            "scroll_y": self.scroll_y,
            "nodes": self.nodes,
        }


def _action(payload: dict, *, ctx: ParseContext | None = None) -> Action:
    action = parse_action(payload, ctx=ctx)
    assert isinstance(action, Action), action
    return action


def _snapshot(nodes: list[SimpleNamespace] | None = None, **kwargs) -> SimpleNamespace:
    data = {
        "url": "https://shop.example/order",
        "title": "确认订单",
        "viewport": (1280, 800),
        "dpr": 1,
        "scroll_y": 0,
        "scroll_height": 1600,
        "nodes": tuple(nodes or ()),
        "pruned": 0,
        "frames": 1,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def _node(role: str, name: str, **kwargs) -> SimpleNamespace:
    data = {"role": role, "name": name, "value": "", "checked": None, "disabled": False}
    data.update(kwargs)
    return SimpleNamespace(**data)


def _resolution(locator=None) -> SimpleNamespace:
    return SimpleNamespace(ok=True, locator=locator, box=None, candidates=())


# ---------------------------------------------------------------------------
# capture / State / diff
# ---------------------------------------------------------------------------


@asyncio_test
async def test_capture_collects_the_four_required_fields():
    state = await vf.capture(FakePage(text="你好，世界"))
    assert state.url == "https://shop.example/order"
    assert state.title == "确认订单"
    assert state.active_element == "button:提交"
    assert state.body_text_hash and len(state.body_text_hash) == 16
    assert state.text == "你好，世界"
    assert state.to_payload()["text_len"] == len("你好，世界")


@asyncio_test
async def test_capture_degrades_when_title_and_evaluate_fail():
    page = FakePage(title_error=RuntimeError("boom"), evaluate_error=RuntimeError("boom"))
    state = await vf.capture(page)
    assert state.url.endswith("/order")
    assert state.title == ""
    assert state.text == ""
    assert state.body_text_hash == "" or len(state.body_text_hash) == 16


@asyncio_test
async def test_capture_propagates_cancelled_error():
    page = FakePage(evaluate_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await vf.capture(page)


def test_state_hash_is_content_based():
    one = vf.State(url="/a", title="T", text="同样的正文")
    two = vf.State(url="/a", title="T", text="同样的正文")
    three = vf.State(url="/a", title="T", text="不同的正文")
    assert one.body_text_hash == two.body_text_hash
    assert one.body_text_hash != three.body_text_hash
    assert one.digest() == two.digest()


def test_diff_detects_each_of_the_six_classes():
    base = vf.State(url="/a", title="A", text="正文", active_element="button:甲", scroll_y=0, nodes=10)
    assert vf.is_unchanged(vf.diff(base, vf.State(url="/a", title="A", text="正文",
                                                 active_element="button:甲", scroll_y=0, nodes=10)))
    deltas = {
        "url": vf.diff(base, vf.State(url="/b", title="A", text="正文", active_element="button:甲",
                                      scroll_y=0, nodes=10)),
        "title": vf.diff(base, vf.State(url="/a", title="B", text="正文", active_element="button:甲",
                                        scroll_y=0, nodes=10)),
        "text": vf.diff(base, vf.State(url="/a", title="A", text="正文加长", active_element="button:甲",
                                       scroll_y=0, nodes=10)),
        "focus": vf.diff(base, vf.State(url="/a", title="A", text="正文", active_element="button:乙",
                                        scroll_y=0, nodes=10)),
        "scroll": vf.diff(base, vf.State(url="/a", title="A", text="正文", active_element="button:甲",
                                         scroll_y=400, nodes=10)),
        "nodes": vf.diff(base, vf.State(url="/a", title="A", text="正文", active_element="button:甲",
                                        scroll_y=0, nodes=12)),
    }
    for name, delta in deltas.items():
        assert getattr(delta, name) is True, name
        assert delta.changed is True
    assert "标题变了" in deltas["title"].detail
    assert "滚动到 400px" in deltas["scroll"].detail


def test_ax_digest_and_attach_snapshot_drive_the_ax_class():
    """六类里的「AX 摘要」来自快照三元组哈希，不是 DOM 元素数。"""
    one = _snapshot([_node("button", "提交"), _node("radio", "加急", checked=True)])
    two = _snapshot([_node("button", "提交"), _node("radio", "加急", checked=False)])
    state = vf.State(url="/o", title="T", text="正文")
    first = vf.attach_snapshot(state, one)
    second = vf.attach_snapshot(state, two)
    assert first.ax_digest and first.ax_digest != second.ax_digest
    delta = vf.diff(first, second)
    assert delta.nodes is True, "勾选态变化必须体现在 AX 摘要上"
    assert vf.attach_snapshot(state, None).ax_digest == ""


def test_delta_six_classes_match_acceptance_vocabulary():
    """六类差分的名字与验收口径对齐：url / title / AX 摘要 / 正文摘要 / scrollY / 状态读回。"""
    delta = vf.Delta()
    for name in ("url", "title", "nodes", "text", "scroll", "focus"):
        assert hasattr(delta, name), name
    assert delta.to_payload()["detail"] == ""
    assert "AX 摘要" in (vf.Delta.__doc__ or "")
    assert "scrollY" in (vf.Delta.__doc__ or "")


def test_diff_ignores_tiny_scroll_shifts():
    base = vf.State(url="/a", title="A", text="x", scroll_y=100)
    tiny = vf.State(url="/a", title="A", text="x", scroll_y=104)
    assert vf.is_unchanged(vf.diff(base, tiny))
    bigger = vf.State(url="/a", title="A", text="x", scroll_y=120)
    assert not vf.is_unchanged(vf.diff(base, bigger))


# ---------------------------------------------------------------------------
# V3 无变化 / V2 已变化
# ---------------------------------------------------------------------------


def test_unchanged_click_is_reported_with_candidates():
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    before = vf.State(url="/order", title="确认订单", text="正文", active_element="")
    after = vf.State(url="/order", title="确认订单", text="正文", active_element="")
    verdict = vf.check(action, before, after, candidates=("①[link]「提交订单」",))
    assert verdict.outcome == vf.OUTCOME_NO_CHANGE
    assert verdict.ok is False
    rendered = verdict.render()
    assert rendered.startswith("【无变化】")
    assert "候选" in rendered and "提交订单" in rendered


def test_changed_click_is_reported_with_detail():
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    before = vf.State(url="/order", title="确认订单", text="正文", active_element="")
    after = vf.State(url="/success", title="下单成功", text="正文", active_element="")
    verdict = vf.check(action, before, after)
    assert verdict.outcome == vf.OUTCOME_CHANGED
    assert verdict.ok is True
    assert verdict.render().startswith("【验证】")
    assert "URL 变了" in verdict.render()


def test_read_only_action_is_not_judged_as_no_change():
    action = _action({"action": "read", "value": "text"})
    state = vf.State(url="/a", title="A", text="x")
    verdict = vf.check(action, state, state)
    assert verdict.outcome == vf.OUTCOME_SKIPPED
    assert verdict.ok is True


# ---------------------------------------------------------------------------
# V1 期望
# ---------------------------------------------------------------------------


def test_expect_url_and_text_satisfied():
    action = _action(
        {
            "action": "click",
            "target": {"role": "button", "name": "搜索"},
            "expect": {"url_contains": "/search", "text_contains": "共 12 条结果"},
        }
    )
    after = vf.State(url="https://shop.example/search?q=x", title="搜索", text="共 12 条结果")
    verdict = vf.check(action, None, after)
    assert verdict.outcome == vf.OUTCOME_VERIFIED
    assert verdict.ok is True
    assert dict(verdict.checks) == {"url_contains": True, "text_contains": True}


def test_expect_failure_names_the_first_unmet_key():
    action = _action(
        {
            "action": "click",
            "target": {"role": "button", "name": "提交"},
            "expect": {"url_contains": "/success", "title_contains": "成功"},
        }
    )
    after = vf.State(url="https://shop.example/order", title="确认订单", text="x")
    verdict = vf.check(action, None, after)
    assert verdict.outcome == vf.OUTCOME_FAILED
    assert verdict.ok is False
    assert "url_contains" in verdict.reason
    assert verdict.render().startswith("【未达成】")


def test_expect_role_and_count_use_snapshot():
    action = _action(
        {
            "action": "click",
            "target": {"text": "加急配送"},
            "expect": {"role": "radio", "name": "加急配送", "count": 2},
        }
    )
    nodes = [_node("radio", "加急配送"), _node("radio", "加急配送"), _node("radio", "标准配送")]
    verdict = vf.check(action, None, vf.State(url="/o", title="T", text="x"), snapshot=_snapshot(nodes))
    assert verdict.ok is True
    assert dict(verdict.checks)["count"] is True


def test_expect_count_mismatch_fails():
    action = _action(
        {"action": "click", "target": {"text": "加急"}, "expect": {"count": 3}}
    )
    nodes = [_node("radio", "加急"), _node("radio", "加急")]
    verdict = vf.check(action, None, vf.State(url="/o", title="T", text="x"), snapshot=_snapshot(nodes))
    assert verdict.ok is False
    assert "count" in verdict.reason


# ---------------------------------------------------------------------------
# 重复守卫（D2 根因 3）
# ---------------------------------------------------------------------------


def test_repeat_guard_rejects_on_third_identical_action():
    guard = vf.RepeatGuard(threshold=3)
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    digest = action.digest()

    # 第 1 次：放行 → 记账（无变化）
    assert guard.before(action) is None
    assert guard.note(digest, vf.OUTCOME_NO_CHANGE) is None
    # 第 2 次：放行 → 记账（无变化）
    assert guard.before(action) is None
    assert guard.note(digest, vf.OUTCOME_NO_CHANGE) is None
    # 第 3 次：**直接拒绝**，不再执行
    rejection = guard.before(action)
    assert isinstance(rejection, Rejection)
    assert rejection.code == "E4"
    assert "连续 3 次无变化" in rejection.reason
    assert "换一种定位方式" in rejection.hint
    assert rejection.render().startswith("【拒绝】")


def test_repeat_guard_resets_after_a_change():
    guard = vf.RepeatGuard(threshold=3)
    action = _action({"action": "click", "target": {"text": "提交"}})
    digest = action.digest()
    guard.before(action)
    guard.note(digest, vf.OUTCOME_NO_CHANGE)
    guard.note(digest, vf.OUTCOME_CHANGED)  # 页面变了 ⇒ 归零
    guard.note(digest, vf.OUTCOME_NO_CHANGE)
    assert guard.before(action) is None, "变化之后重新计数，不应立刻拒绝"


def test_repeat_guard_ignores_failures_and_can_be_disabled():
    guard = vf.RepeatGuard(threshold=2)
    action = _action({"action": "click", "target": {"text": "提交"}})
    digest = action.digest()
    assert guard.note(digest, vf.OUTCOME_FAILED) is None
    assert guard.note(digest, vf.OUTCOME_FAILED) is None, "定位失败不计入「点了没反应」的连续计数"

    off = vf.RepeatGuard(threshold=0)
    for _ in range(5):
        assert off.before(action) is None
        assert off.note(digest, vf.OUTCOME_NO_CHANGE) is None


def test_repeat_guard_keys_on_normalized_target_not_index():
    guard = vf.RepeatGuard(threshold=2)
    first = _action({"action": "click", "target": {"text": "选项"}, "index": 0})
    second = _action({"action": "click", "target": {"text": "选项"}, "index": 1})
    assert guard.note(first.digest(), vf.OUTCOME_NO_CHANGE) is None
    assert guard.before(second) is not None, "同 target 不同 index 也算同一个动作（index 不进 digest）"


def test_check_with_guard_rejects_the_third_identical_click():
    """前置闸门接进 check()：第 1/2 次判定「无变化」，第 3 次直接拒绝（D2 根因 3）。"""
    guard = vf.RepeatGuard(threshold=3)
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    before = vf.State(url="/order", title="确认订单", text="正文")
    after = vf.State(url="/order", title="确认订单", text="正文")

    first = vf.check(action, before, after, guard=guard)
    second = vf.check(action, before, after, guard=guard)
    third = vf.check(action, before, after, guard=guard)

    assert first.outcome == vf.OUTCOME_NO_CHANGE and first.ok is False
    assert second.outcome == vf.OUTCOME_NO_CHANGE and second.ok is False
    assert third.outcome == vf.OUTCOME_REJECTED
    assert third.rejection is not None and third.rejection.code == "E4"
    assert third.render().startswith("【拒绝】")
    assert "换一种定位方式" in third.render()


def test_check_with_guard_resets_after_page_changed():
    guard = vf.RepeatGuard(threshold=3)
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    same = vf.State(url="/order", title="确认订单", text="正文")
    moved = vf.State(url="/success", title="成功", text="正文")
    vf.check(action, same, same, guard=guard)
    changed = vf.check(action, same, moved, guard=guard)
    assert changed.outcome == vf.OUTCOME_CHANGED
    assert guard.before(action) is None, "页面变过之后重新计数"


# ---------------------------------------------------------------------------
# 勾选态三路读回（D5）
# ---------------------------------------------------------------------------


@asyncio_test
async def test_state_readback_via_aria_checked():
    assert await vf.read_control_state(FakeLocator(aria_checked="true")) == "checked"
    assert await vf.read_control_state(FakeLocator(aria_checked="false")) == "unchecked"


@asyncio_test
async def test_state_readback_via_native_checked():
    assert await vf.read_control_state(FakeLocator(native_checked=True)) == "checked"
    assert await vf.read_control_state(FakeLocator(native_checked=False)) == "unchecked"


@asyncio_test
async def test_state_readback_via_data_state():
    assert await vf.read_control_state(FakeLocator(data_state="on")) == "checked"
    assert await vf.read_control_state(FakeLocator(data_state="off")) == "unchecked"


@asyncio_test
async def test_state_readback_unknown_is_not_an_exception():
    """三路都读不到 ⇒ `unknown`（不再抛 `Not a checkbox or radio button`）。"""
    assert await vf.read_control_state(FakeLocator()) == "unknown"


# ---------------------------------------------------------------------------
# type 之后 value 校验
# ---------------------------------------------------------------------------


@asyncio_test
async def test_check_value_success():
    verdict = await vf.check_value(FakeLocator(value="上海天气"), "上海天气", timeout_s=0.05)
    assert verdict.outcome == vf.OUTCOME_VERIFIED
    assert verdict.ok is True


@asyncio_test
async def test_check_value_failure_when_input_did_not_take_effect():
    verdict = await vf.check_value(FakeLocator(value=""), "上海天气", timeout_s=0.05)
    assert verdict.outcome == vf.OUTCOME_FAILED
    assert verdict.ok is False
    assert "输入未生效" in verdict.reason
    assert verdict.render().startswith("【未达成】")


@asyncio_test
async def test_check_value_on_non_input_element_fails_clearly():
    class NotAnInput:
        async def input_value(self):
            raise RuntimeError("not an input")

    verdict = await vf.check_value(NotAnInput(), "x", timeout_s=0.01)
    assert verdict.ok is False
    assert "不是输入控件" in verdict.reason


# ---------------------------------------------------------------------------
# locator 类期望（state / count）
# ---------------------------------------------------------------------------


@asyncio_test
async def test_wait_for_expect_visible_state_passes():
    locator = FakeLocator(visible=True)
    action = _action(
        {"action": "click", "target": {"text": "提交"}, "expect": {"state": "visible", "timeout_ms": 800}}
    )
    verdict = await vf.wait_for_expect(FakePage(), action, _resolution(locator), action_timeout_s=5)
    assert verdict.ok is True
    assert locator.waits == [{"state": "visible", "timeout": 800}]


@asyncio_test
async def test_wait_for_expect_is_clamped_by_action_timeout():
    locator = FakeLocator(visible=True)
    action = _action(
        {"action": "click", "target": {"text": "提交"}, "expect": {"state": "visible", "timeout_ms": 25000}}
    )
    await vf.wait_for_expect(FakePage(), action, _resolution(locator), action_timeout_s=2)
    assert locator.waits[0]["timeout"] == 2000, "期望的 timeout 不许绕过 action_timeout_s"


@asyncio_test
async def test_wait_for_expect_timeout_reports_failure():
    locator = FakeLocator(visible=False)
    action = _action(
        {"action": "click", "target": {"text": "提交"}, "expect": {"state": "visible", "timeout_ms": 10}}
    )
    verdict = await vf.wait_for_expect(FakePage(), action, _resolution(locator), action_timeout_s=5)
    assert verdict.ok is False
    assert "state=visible" in verdict.reason


@asyncio_test
async def test_wait_for_expect_without_locator_fails_clearly():
    action = _action(
        {"action": "click", "target": {"text": "提交"}, "expect": {"state": "visible"}}
    )
    verdict = await vf.wait_for_expect(FakePage(), action, _resolution(None), action_timeout_s=1)
    assert verdict.ok is False
    assert "没有 locator" in verdict.reason


@asyncio_test
async def test_wait_for_expect_count_mismatch():
    locator = FakeLocator(count=5)
    action = _action({"action": "click", "target": {"text": "行"}, "expect": {"count": 3}})
    verdict = await vf.wait_for_expect(FakePage(), action, _resolution(locator), action_timeout_s=1)
    assert verdict.ok is False
    assert "count=3" in verdict.reason


# ---------------------------------------------------------------------------
# 观测块（唯一渲染处）
# ---------------------------------------------------------------------------


def test_observation_block_shape_has_three_sections_in_order():
    action = _action({"action": "click", "target": {"role": "button", "name": "提交"}})
    after = vf.State(url="/success", title="成功", text="x")
    verdict = vf.check(action, vf.State(url="/order", title="确认"), after)
    block = vf.render_observation(
        step=3,
        snapshot=_snapshot([_node("button", "提交"), _node("radio", "加急", checked=True)]),
        action=action,
        verdict=verdict,
    )
    lines = block.splitlines()
    assert lines[0].startswith("【观测 #3】")
    assert "https://shop.example/order" in lines[0]
    assert "1280x800@dpr1" in lines[0]
    assert "- [button] 提交" in block
    assert "(checked=true)" in block
    action_index = next(i for i, line in enumerate(lines) if line.startswith("【动作】"))
    verdict_index = next(i for i, line in enumerate(lines) if line.startswith("【验证】"))
    assert action_index < verdict_index, "三段顺序固定：观测 → 动作 → 验证"
    assert "【预算】" not in block


def test_observation_budget_line_only_below_threshold():
    action = _action({"action": "back"})
    shown = vf.render_observation(
        step=1, snapshot=_snapshot(), action=action, remaining_s=12.4
    )
    assert "【预算】剩余 12 秒" in shown
    hidden = vf.render_observation(
        step=1, snapshot=_snapshot(), action=action, remaining_s=300
    )
    assert "【预算】" not in hidden


def test_observation_without_action_has_no_action_or_verdict_section():
    block = vf.render_observation(step=1, snapshot=_snapshot(), header="https://a/ | 标题：A")
    assert "【动作】" not in block
    assert "【验证】" not in block
    assert block.startswith("【观测 #1】")


def test_ledger_status_maps_no_change_to_unchanged():
    """任务契约说 outcome=no_change，规格 §1.6 的台账 status 是 unchanged —— 两者由映射钉住。"""
    no_change = vf.Verdict(ok=False, outcome=vf.OUTCOME_NO_CHANGE)
    assert vf.ledger_status(no_change) == "unchanged"
    changed = vf.Verdict(ok=True, outcome=vf.OUTCOME_CHANGED)
    assert vf.ledger_status(changed) == "changed"
    rejected = vf.Verdict(ok=False, outcome=vf.OUTCOME_REJECTED)
    assert vf.ledger_status(rejected) == "rejected"
    assert set(vf.LEDGER_STATUS.values()) == {
        "verified",
        "changed",
        "unchanged",
        "failed",
        "rejected",
    }


def test_repeat_guard_accepts_spec_spelling_unchanged():
    """规格 §5.3 的 status 词是 `unchanged`，任务契约写 `no_change` —— 两个都要认。"""
    guard = vf.RepeatGuard(threshold=1)
    rejection = guard.note("d1", "unchanged")
    assert isinstance(rejection, Rejection) and rejection.code == "E4"


def test_observation_rejects_no_change_section_for_rejection():
    rejection = Rejection(code="R-A4", reason="role 必须与 name 一起用")
    verdict = vf.Verdict(ok=False, outcome=vf.OUTCOME_REJECTED, rejection=rejection)
    assert verdict.render().startswith("【拒绝】R-A4")


def test_observation_hides_css_selector_in_action_line():
    action = _action({"action": "click", "target": {"css": "#main > div:nth-child(2)"}})
    block = vf.render_observation(step=1, snapshot=_snapshot(), action=action)
    assert "nth-child" not in block
    assert "选择器不回显" in block
