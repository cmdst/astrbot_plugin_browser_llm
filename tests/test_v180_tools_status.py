"""`browse_task_status` 执行级用例（规格 §7.2 / §11.4 EX-36..EX-41）。

工具本体只有三行（接线到 `core/tasks.status_report`），所以这里**真跑**整条链：
真 `TaskBook` + 真提交 + 真状态机 → 调 `plugin.browse_task_status(...)` → 断言返回体。
两条硬边界单独钉：① 任何分支都不许出现正文（sentinel）；② 任何分支都不许出现绝对时间。
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tests"))

import main as main_module  # noqa: E402
import test_v175_tools as helpers  # noqa: E402 — 复用同一批 fake
from astrbot_plugin_browser_llm.core import tasks as tb  # noqa: E402

SENTINEL = "SENTINEL-正文-不许出现"

#: 绝对时间的两种形态（日期 / 时刻）—— 状态工具只给"已用/已等待/剩余"三个时长
_ABS_TIME = (re.compile(r"20\d\d-"), re.compile(r"\d{1,2}:\d{2}"))


def _plugin(tmp_path, *, mode: str = "browse_only"):
    plugin = helpers._bare_plugin(tmp_path)
    plugin.background_mode = mode
    plugin.total_budget_s = 30.0
    plugin.background_max_chars = 1500
    plugin.dedupe_window_s = 600.0
    plugin.max_parallel = 4
    return plugin


def _event(umo: str = "aiocqhttp:GroupMessage:1"):
    event = helpers._FakeEvent()
    event.unified_msg_origin = umo
    return event


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _submit(plugin, event, runner, *, task_text="t", **kwargs):
    async def _deliver(record) -> tb.DeliveryNote:
        return tb.DeliveryNote(attempted=True, succeeded=True, channel="test")

    return plugin._book.submit(
        runner=runner, deliver=_deliver, umo=plugin._umo_of(event),
        tool=main_module._BROWSE_WEB_TOOL_NAME, task_text=task_text,
        budget_s=kwargs.pop("budget_s", float(plugin.total_budget_s)),
        max_chars=int(plugin.background_max_chars),
        dedupe_window_s=float(plugin.dedupe_window_s),
        max_parallel=kwargs.pop("max_parallel", int(plugin.max_parallel)),
    )


def _status(plugin, event, task_id: str = "") -> str:
    return asyncio.run(plugin.browse_task_status(event, task_id=task_id))


def _assert_no_body_no_absolute_time(text: str) -> None:
    assert SENTINEL not in text, text
    for pattern in _ABS_TIME:
        assert not pattern.search(text), f"出现了绝对时间：{text}"


# --------------------------------------------------------------- EX-36 在跑


def test_running_task_shows_elapsed_budget_and_remaining(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _hang(progress: dict) -> str:
            await asyncio.sleep(30)
            return SENTINEL

        decision = _submit(plugin, event, _hang)
        await asyncio.sleep(0.05)
        text = await plugin.browse_task_status(event)
        plugin._book.cancel(decision.task_id)
        await asyncio.sleep(0.05)
        return text, decision.task_id

    text, task_id = asyncio.run(scenario())
    first = text.splitlines()[0]
    assert first.startswith(f"【状态】task_id={task_id} | 工具=browse_web | 状态=在跑 |"), first
    assert "已用" in first and "预算 30s" in first and "剩余" in first
    _assert_no_body_no_absolute_time(text)


# ----------------------------------------------------- EX-37 排队 / 投递中


def test_queued_task_shows_position(tmp_path):
    """排队是**全局**并发闸（单飞才是按 umo）⇒ 第二个任务必须来自另一个会话。"""
    plugin = _plugin(tmp_path)
    plugin.max_parallel = 1
    mine, other = _event("aiocqhttp:GroupMessage:1"), _event("aiocqhttp:GroupMessage:2")

    async def scenario():
        async def _hang(progress: dict) -> str:
            await asyncio.sleep(30)
            return SENTINEL

        first = _submit(plugin, mine, _hang, task_text="first", max_parallel=1)
        second = _submit(plugin, other, _hang, task_text="second", max_parallel=1)
        await asyncio.sleep(0.05)
        assert plugin._book.get(first.task_id).state == tb.STATE_RUNNING
        assert plugin._book.get(second.task_id).state == tb.STATE_QUEUED, (
            plugin._book.get(second.task_id).state)
        text = await plugin.browse_task_status(other, task_id=second.task_id)
        plugin._book.cancel(first.task_id)
        plugin._book.cancel(second.task_id)
        await asyncio.sleep(0.05)
        return text, second.task_id

    text, task_id = asyncio.run(scenario())
    line = text.splitlines()[0]
    assert f"task_id={task_id}" in line and "状态=排队中（第 1 位）" in line, line
    assert "已等待" in line and "剩余" in line
    _assert_no_body_no_absolute_time(text)


def test_delivering_state_is_visible_while_the_delivery_is_in_flight(tmp_path):
    """`delivering` 是本版新增的第 7 态：投递进行中必须**看得见**（不再"无终态"）。"""
    plugin = _plugin(tmp_path)
    event = _event()
    gate = {"release": None, "inside": None}

    async def scenario():
        loop = asyncio.get_running_loop()
        gate["release"] = loop.create_future()
        gate["inside"] = loop.create_future()

        async def _quick(progress: dict) -> str:
            return SENTINEL

        async def _deliver(record) -> tb.DeliveryNote:
            gate["inside"].set_result(True)
            await gate["release"]           # 卡在 delivering
            return tb.DeliveryNote(attempted=True, succeeded=True, channel="test")

        decision = plugin._book.submit(
            runner=_quick, deliver=_deliver, umo=plugin._umo_of(event),
            tool=main_module._BROWSE_WEB_TOOL_NAME, task_text="delivering",
            budget_s=float(plugin.total_budget_s), max_chars=1500,
            dedupe_window_s=600.0, max_parallel=4)
        await gate["inside"]
        record = plugin._book.get(decision.task_id)
        assert record.state == tb.STATE_DELIVERING, record.state
        text = await plugin.browse_task_status(event, task_id=decision.task_id)
        gate["release"].set_result(True)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        return text, decision.task_id

    text, task_id = asyncio.run(scenario())
    first = text.splitlines()[0]
    assert f"task_id={task_id}" in first and "状态=投递中" in first, first
    assert "已用" in first and "预算 30s" in first
    _assert_no_body_no_absolute_time(text)


# --------------------------------------------------------- EX-38 四终态词表


@pytest.mark.parametrize(
    ("expect_state", "expect_word"),
    [(tb.STATE_SUCCEEDED, "完成"), (tb.STATE_FAILED, "失败"),
     (tb.STATE_TIMEOUT, "超时"), (tb.STATE_CANCELLED, "取消")],
)
def test_terminal_state_words_are_verbatim(tmp_path, expect_state, expect_word):
    plugin = _plugin(tmp_path)
    plugin.total_budget_s = 0.15
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        async def _boom(progress: dict) -> str:
            raise RuntimeError("炸了")

        async def _slow(progress: dict) -> str:
            await asyncio.sleep(30)
            return SENTINEL

        runner = {tb.STATE_SUCCEEDED: _ok, tb.STATE_FAILED: _boom,
                  tb.STATE_TIMEOUT: _slow, tb.STATE_CANCELLED: _slow}[expect_state]
        decision = _submit(plugin, event, runner)
        if expect_state == tb.STATE_CANCELLED:
            await asyncio.sleep(0.03)
            plugin._book.cancel(decision.task_id)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal,
                          timeout=5.0)
        record = plugin._book.get(decision.task_id)
        assert record.state == expect_state, (record.state, record.reason)
        return await plugin.browse_task_status(event, task_id=decision.task_id)

    text = asyncio.run(scenario())
    first = text.splitlines()[0]
    assert f"状态={expect_word} |" in first, first
    assert "耗时" in first
    _assert_no_body_no_absolute_time(text)


def test_succeeded_task_points_at_the_recall_tool_without_echoing_the_body(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        return await plugin.browse_task_status(event, task_id=decision.task_id)

    text = asyncio.run(scenario())
    assert f"结果 {len(SENTINEL)} 字" in text, text
    assert "browse_recent_results" in text, "成功任务必须指路到回看工具"
    _assert_no_body_no_absolute_time(text)


def test_failed_task_reports_reason_and_no_result(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _boom(progress: dict) -> str:
            raise ValueError("页面崩了")

        decision = _submit(plugin, event, _boom)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        return await plugin.browse_task_status(event, task_id=decision.task_id)

    text = asyncio.run(scenario())
    assert "本次没有可用结果" in text
    assert "失败原因" in text and "ValueError" in text
    _assert_no_body_no_absolute_time(text)


# ----------------------------------------------------------- EX-39 空 / 无历史


def test_no_task_and_no_history(tmp_path):
    plugin = _plugin(tmp_path)
    text = _status(plugin, _event())
    assert text.startswith("【无任务】"), text
    assert "也没有已完成任务" in text
    assert "30" in text  # 窗口口径
    _assert_no_body_no_absolute_time(text)


def test_no_task_but_succeeded_history_points_at_the_recall_tool(tmp_path):
    """只剩**成功**的终态 ⇒ 情形 5：正文该由回看工具给，状态工具只报"没有在跑的"。"""
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        return await plugin.browse_task_status(event)

    text = asyncio.run(scenario())
    assert text.startswith("【无任务】"), text
    assert "有 1 条已完成" in text and "browse_recent_results" in text
    _assert_no_body_no_absolute_time(text)


def test_no_task_but_failed_history_shows_the_reason(tmp_path):
    """失败类终态即使没有在跑任务也要解释（情形 4 的「失败原因」行）。"""
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _boom(progress: dict) -> str:
            raise RuntimeError("页面崩了")

        decision = _submit(plugin, event, _boom)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        return await plugin.browse_task_status(event)

    text = asyncio.run(scenario())
    assert text.startswith("【状态】"), text
    assert "状态=失败" in text and "失败原因" in text, text
    _assert_no_body_no_absolute_time(text)


# ------------------------------------------------------------ EX-40 TS-2 / TS-3


def test_unknown_task_id_is_ts_2(tmp_path):
    plugin = _plugin(tmp_path)
    text = _status(plugin, _event(), task_id="bg-9999")
    assert text == tb.REJECT_TEXTS["TS-2"].format(task_id="bg-9999"), text


def test_other_session_task_is_indistinguishable_from_missing(tmp_path):
    """跨会话**不泄露存在性**：另一个 umo 的记录与"不存在"给同一句 TS-2。"""
    plugin = _plugin(tmp_path)
    mine, other = _event("aiocqhttp:GroupMessage:1"), _event("aiocqhttp:GroupMessage:2")

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, other, _ok)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        assert plugin._book.get(decision.task_id).is_terminal
        return await plugin.browse_task_status(mine, task_id=decision.task_id)

    text = asyncio.run(scenario())
    assert text == tb.REJECT_TEXTS["TS-2"].format(task_id=text.split("task_id=")[-1].split("（")[0])
    assert text.startswith("【拒绝】TS-2"), text


def test_mode_off_is_ts_3(tmp_path):
    plugin = _plugin(tmp_path, mode="off")
    text = _status(plugin, _event())
    assert text == tb.REJECT_TEXTS["TS-3"], text


# --------------------------------------------------------- EX-41 只读 / 无时间


def test_status_tool_is_read_only(tmp_path):
    """调用前后记录的字段快照必须相等（工具不许顺手写 `queue_position` 之类）。"""
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda: plugin._book.get(decision.task_id).is_terminal)
        before = dataclasses.asdict(plugin._book.get(decision.task_id))
        text = await plugin.browse_task_status(event, task_id=decision.task_id)
        after = dataclasses.asdict(plugin._book.get(decision.task_id))
        return before, after, text

    before, after, text = asyncio.run(scenario())
    assert before == after, "状态工具改动了记录（只读不成立）"
    _assert_no_body_no_absolute_time(text)
    assert not hasattr(plugin._book.get("bg-0001") or object(), "finished_at_iso")
