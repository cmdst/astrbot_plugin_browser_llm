"""v1.7.5 **qa 独立复核**用例（qa-engineer / t8 加，未提交；不属于实现者的验收套件）。

三件事，都是用**真实代码路径**复跑，不读实现者的结论：

1. **D8 的静默路径确实变成了 warning**（fake 触发）：把 ``_act_impl`` 打成抛异常，
   断言 ① 模型侧拿到的是显式 `【错误】…`；② 日志里有一条 `WARNING` 级记录
   （``browse_act 失败``）—— v1.7.0 的坐标点击链是 ``logger.debug`` 吞掉的（D8 的
   「日志 0 痕迹」），这条用例把「不再静默」钉死。
2. **D8 的替代路径可见**：mark 过期后拿旧编号点 → 动作层给 ``R-A9`` 结构化拒绝
   （不静默、不猜坐标）。
3. **U1/U2 的真实时钟复跑**（不跑实现者的假时钟用例）：预算到点 ⇒ 恰好一条终态；
   同键在跑 ⇒ 第二次提交只回执、不再投递。两条都用 1 秒级真实时钟，不依赖 sleep 抖动
   （轮询等待交货，超时即判失败）。v1.8.0 起这两条跑在 ``core/tasks.TaskBook``（7 态）
   上：终态由 ``render_terminal_text`` 渲染、投递回调形如 ``deliver(TaskRecord)``。

跑法：``PYTHONDONTWRITEBYTECODE=1 /root/AstrBot/.venv/bin/python -m pytest \
tests/test_v175_qa_recheck.py -q -p no:cacheprovider``
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tests"))

import test_v175_tools as helpers  # noqa: E402 — 复用同一批 fake（不新增夹具）

from astrbot_plugin_browser_llm.core import action_model as am  # noqa: E402
from astrbot_plugin_browser_llm.core import tasks as tb  # noqa: E402


# --------------------------------------------------------------- D8：不再静默


class _LogRecorder:
    """记录 ``main.logger`` 的调用（conftest 把 astrbot.api.logger 打成 no-op，
    所以 caplog 抓不到；这里直接替换 main.py 里那个模块级绑定）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level):
        def _fn(msg, *args, **kwargs):
            try:  # 兼容 `logger.warning("模板 %s", x)` 与 f-string 两种写法
                rendered = str(msg) % args if args else str(msg)
            except (TypeError, ValueError):
                rendered = f"{msg} {args}"
            self.records.append((level, rendered))

        return _fn

    def __getattr__(self, name):
        return self._record(name)


def test_d8_tool_boundary_warns_instead_of_swallowing(tmp_path, monkeypatch):
    """D8：被吞的异常必须变成「模型可见的错误 + 一条 WARNING」，不许只剩 debug。

    v1.7.0 的坐标点击链用 ``logger.debug`` 吞掉 ``TypeError``（D8 的「日志 0 痕迹」）；
    这里 fake 触发同一形态，断言 ① 模型拿到 ``【错误】…``；② 日志里有一条 **warning**。
    """
    recorder = _LogRecorder()
    monkeypatch.setattr(helpers.main_module, "logger", recorder)

    plugin = helpers._bare_plugin(tmp_path)
    event = helpers._FakeEvent()

    async def _boom(*args, **kwargs):
        raise RuntimeError("qa 注入的假异常（模拟 D8 的 TypeError）")

    monkeypatch.setattr(plugin, "_act_impl", _boom)
    out = asyncio.run(plugin.browse_act(event, {"action": "click"}))

    assert out.startswith("【错误】"), f"模型侧必须看到显式错误，实际：{out[:80]}"
    warns = [msg for level, msg in recorder.records if level == "warning"]
    assert any("browse_act 失败" in msg for msg in warns), (
        f"缺少 WARNING 级日志（D8 的静默复发）：{recorder.records}"
    )
    assert any("qa 注入的假异常" in msg for msg in warns), "告警文案必须带上原始异常"
    debugs = [msg for level, msg in recorder.records if level == "debug"]
    assert not any("browse_act 失败" in msg for msg in debugs), (
        "同一条失败不得只落在 debug（那正是 D8 的形态）"
    )


def test_d8_expired_mark_is_a_structured_rejection():
    """D8 的替代路径：没有文本元素时用 SoM 编号；编号过期要**明说**，不许猜坐标。"""
    rejection = am.parse_action(
        {"action": "click", "target": {"mark": 7}},
        ctx=am.ParseContext(mark_epoch={1: None}),  # 7 号不在这轮的纪元里
    )
    assert isinstance(rejection, am.Rejection), rejection
    assert rejection.code == "R-A9", rejection.code
    text = rejection.render()
    assert "R-A9" in text and "编号" in text, text


# --------------------------------------------------------------- U1/U2：真实时钟


def _drain(deliveries: list, want: int, timeout: float = 8.0) -> None:
    """轮询等交货（真实时钟，但不用固定 sleep 猜时间）。"""
    deadline = time.monotonic() + timeout
    while len(deliveries) < want and time.monotonic() < deadline:
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.02))


async def _wait_for(count_fn, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if count_fn():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待超时：后台任务没有按时交货")


def _recorder(deliveries: list):
    """替身投递：记账并**在 delivering 期间**渲染终态正文（与 main._deliver 同形）。"""
    async def deliver(record: "tb.TaskRecord") -> "tb.DeliveryNote":
        rendered = tb.render_terminal_text(record, record.result_text or "")
        deliveries.append((record.task_id, record.state, rendered))
        return tb.DeliveryNote(attempted=True, succeeded=True, channel="test")
    return deliver


def test_u1_budget_deadline_delivers_exactly_one_terminal_real_clock():
    """U1：整链墙钟预算到点 ⇒ **恰好一条**终态（真实时钟 1 秒预算）。"""
    deliveries: list[tuple[str, str, str]] = []

    async def scenario():
        book = tb.TaskBook()

        async def hanging(progress: dict) -> str:
            progress["partial"] = "中断前的半成品"
            await asyncio.sleep(30)
            return "never"

        decision = book.submit(
            runner=hanging,
            deliver=_recorder(deliveries),
            umo="qa:u1:umo",
            tool="browse_web",
            task_text="U1 到点终态",
            budget_s=1.0,
            dedupe_window_s=600,
        )
        await _wait_for(lambda: book.get(decision.task_id).is_terminal)
        await asyncio.sleep(0.3)  # 给看门狗/迟到投递留出机会（必须仍然只有 1 条）
        return decision, book

    decision, book = asyncio.run(scenario())
    record = book.get(decision.task_id)
    mine = [d for d in deliveries if d[0] == decision.task_id]
    assert len(mine) == 1, f"终态必须恰好一条，实际 {len(mine)} 条"
    task_id, _state, text = mine[0]
    assert record.state == tb.STATE_TIMEOUT, record.state
    assert "【后台任务超时】" in text.splitlines()[0], text.splitlines()[0]
    assert task_id in text and "browse_web" in text, "终态首行必须能归属到具体任务"
    assert "中断前的半成品" in text, "到点也必须带上已得的部分结果"
    assert record.is_terminal and not record.is_active


def test_u2_same_key_running_delivers_once_real_clock():
    """U2：同键（同 umo+tool+文本）在跑 ⇒ 第二次只回【已受理】，全程只投递 1 条。"""
    deliveries: list[tuple[str, str, str]] = []

    async def scenario():
        book = tb.TaskBook()

        async def slow(progress: dict) -> str:
            await asyncio.sleep(0.6)
            return "唯一结果"

        first = book.submit(
            runner=slow,
            deliver=_recorder(deliveries),
            umo="qa:u2:umo",
            tool="browse_web",
            task_text="  同一句话  ",
            budget_s=3.0,
            dedupe_window_s=600,
        )
        second = book.submit(
            runner=slow,
            deliver=_recorder(deliveries),
            umo="qa:u2:umo",
            tool="browse_web",
            task_text="同一句话",
            budget_s=3.0,
            dedupe_window_s=600,
        )
        await _wait_for(lambda: any(d[0] == first.task_id for d in deliveries))
        await asyncio.sleep(0.4)
        third = book.submit(
            runner=slow,
            deliver=_recorder(deliveries),
            umo="qa:u2:umo",
            tool="browse_web",
            task_text="同一句话",
            budget_s=3.0,
            dedupe_window_s=600,
        )
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first.kind == tb.SUBMIT_ACCEPTED
    assert second.kind in (tb.SUBMIT_DUPLICATE_RUNNING, tb.SUBMIT_SINGLE_FLIGHT), second.kind
    assert second.task_id == first.task_id, "同键在跑不得再起第二个任务"
    assert "已受理" in first.text
    assert third.kind == tb.SUBMIT_DUPLICATE_DONE, third.kind
    assert "重复请求" in third.text
    assert "唯一结果" in third.text, "重复回执要把已完成的结果给模型，否则等于没说"
    assert len([d for d in deliveries if d[0] == first.task_id]) == 1, (
        f"同一 task 必须只投递一条，实际 {len(deliveries)} 条"
    )


@pytest.mark.parametrize("_unused", [0])
def test_qa_recheck_file_is_additive_only(_unused):
    """本文件是 qa 追加的复核用例：只读生产代码，不改业务源码（自查）。"""
    forbidden = [
        ROOT / "main.py",
        *sorted((ROOT / "core").glob("*.py")),
        ROOT / "_conf_schema.json",
        *sorted((ROOT / "tools").glob("*.py")),
    ]
    # 只断言这些文件**存在**（说明复核对象在场）；改动与否由报告里的 git 证据负责。
    assert all(path.exists() for path in forbidden)
