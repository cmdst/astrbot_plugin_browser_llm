"""`browse_recent_results` 执行级用例（规格 §7.3 / §11.4 EX-42..EX-47）。

四条语义边界逐条落成可执行断言：① 5 条 / 30 分钟；② 不跨会话；③ 逐条截断；
④ 网页来源带「不可信外部内容」前缀。另外两条硬边界：**不写会话历史**、**只读**。

时间全部靠注入的假时钟（`TaskBook(clock=…)`）推进，不用真实 sleep 猜时长。
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tests"))

import main as main_module  # noqa: E402
import test_v175_tools as helpers  # noqa: E402 — 复用同一批 fake
from astrbot_plugin_browser_llm.core import tasks as tb  # noqa: E402

SENTINEL = "SENTINEL-正文-不许出现"
_ABS_TIME = (re.compile(r"20\d\d-"), re.compile(r"\d{1,2}:\d{2}"))


class _Clock:
    """可推进的假时钟（`TaskBook` 的时间源；预算给得很大，避免看门狗插手）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _plugin(tmp_path, clock: _Clock | None = None, *, mode: str = "browse_only",
            max_chars: int = 1500):
    plugin = helpers._bare_plugin(tmp_path)
    plugin.background_mode = mode
    plugin.total_budget_s = 100000.0
    plugin.background_max_chars = max_chars
    plugin.dedupe_window_s = 0.0
    plugin.max_parallel = 8
    if clock is not None:
        plugin._book = tb.TaskBook(clock=clock)
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


def _submit(plugin, event, runner, *, task_text="t"):
    async def _deliver(record) -> tb.DeliveryNote:
        return tb.DeliveryNote(attempted=True, succeeded=True, channel="test")

    return plugin._book.submit(
        runner=runner, deliver=_deliver, umo=plugin._umo_of(event),
        tool=main_module._BROWSE_WEB_TOOL_NAME, task_text=task_text,
        budget_s=float(plugin.total_budget_s), max_chars=int(plugin.background_max_chars),
        dedupe_window_s=float(plugin.dedupe_window_s), max_parallel=int(plugin.max_parallel),
    )


def _recall(plugin, event, **kwargs) -> str:
    return asyncio.run(plugin.browse_recent_results(event, **kwargs))


def _assert_no_absolute_time(text: str) -> None:
    for pattern in _ABS_TIME:
        assert not pattern.search(text), f"出现了绝对时间：{text}"


# ------------------------------------------------- EX-42 有结果：块首 + 前缀 + 逐条


def test_results_block_shape_and_untrusted_prefix(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        for index in range(3):
            decision = _submit(plugin, event, _ok, task_text=f"t{index}")
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        return plugin._book.recent_of("aiocqhttp:GroupMessage:1", window_s=1800.0, limit=5)

    records = asyncio.run(scenario())
    assert len(records) == 3
    text = _recall(plugin, event)
    lines = text.split("\n")
    assert lines[0] == "【最近结果】本会话 · 最近 30 分钟 · 3 条（上限 5）", lines[0]
    assert lines[1] == tb.UNTRUSTED_PREFIX, lines[1]
    blocks = text.split("\n\n")[1:]
    assert len(blocks) == 3, blocks
    for index, block in enumerate(blocks, 1):
        head = block.splitlines()[0]
        assert head.startswith(f"【{index}】task_id=bg-000{4 - index}"), head
        assert "工具=browse_web" in head and "完成 · " in head
        assert f"{len(SENTINEL)} 字" in head, head
        assert SENTINEL in block, "正文必须回看得到（它只在后台上下文之外）"
    _assert_no_absolute_time(text)


# ----------------------------------------------------- EX-43 5 条 / 30 分钟边界


def test_limit_and_window_boundaries(tmp_path):
    """8 条记录、其中 6 条在窗口内 ⇒ 只回 5 条；窗口外的一条都不出现。"""
    clock = _Clock()
    plugin = _plugin(tmp_path, clock)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        # 先造 2 条窗口外（40 分钟前），再造 6 条窗口内
        for index in range(2):
            decision = _submit(plugin, event, _ok, task_text=f"old{index}")
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        old_ids = {record.task_id for record in plugin._book.recent_of(
            "aiocqhttp:GroupMessage:1", window_s=10 ** 9, limit=50)}
        clock.advance(40 * 60)
        for index in range(6):
            decision = _submit(plugin, event, _ok, task_text=f"new{index}")
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        return old_ids

    old_ids = asyncio.run(scenario())
    assert len(old_ids) == 2
    text = _recall(plugin, event)
    head = text.splitlines()[0]
    assert head == "【最近结果】本会话 · 最近 30 分钟 · 5 条（上限 5）", head
    for old_id in old_ids:
        assert old_id not in text, f"窗口外的 {old_id} 不许出现（30 分钟是硬边界）"
    assert text.count("【") >= 5 + 2  # 块首 + 前缀 + 5 条条目
    # limit=1 ⇒ 只回 1 条
    one = _recall(plugin, event, limit=1)
    assert one.splitlines()[0] == "【最近结果】本会话 · 最近 30 分钟 · 1 条（上限 1）", one[:80]


def test_age_labels_use_relative_minutes_only(tmp_path):
    clock = _Clock()
    plugin = _plugin(tmp_path, clock)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok, task_text="age")
        await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        fresh = await plugin.browse_recent_results(event)
        clock.advance(7 * 60)
        old = await plugin.browse_recent_results(event)
        return fresh, old

    fresh, old = asyncio.run(scenario())
    assert "完成 · 刚刚 |" in fresh, fresh
    assert "完成 · 7 分钟前 |" in old, old
    _assert_no_absolute_time(fresh)
    _assert_no_absolute_time(old)


# ------------------------------------------------------------- EX-44 不跨会话


def test_results_are_session_scoped(tmp_path):
    plugin = _plugin(tmp_path)
    mine, other = _event("aiocqhttp:GroupMessage:1"), _event("aiocqhttp:GroupMessage:2")

    async def scenario():
        async def _ok_mine(progress: dict) -> str:
            return "我的结果"

        async def _ok_other(progress: dict) -> str:
            return "别人的结果"

        for index in range(2):
            decision = _submit(plugin, mine, _ok_mine, task_text=f"mine{index}")
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        other_ids = []
        for index in range(2):
            decision = _submit(plugin, other, _ok_other, task_text=f"other{index}")
            other_ids.append(decision.task_id)
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        text = await plugin.browse_recent_results(mine)
        cross = await plugin.browse_recent_results(mine, task_id=other_ids[0])
        return text, cross

    text, cross = asyncio.run(scenario())
    assert text.count("【1】") == 1 and text.count("【2】") == 1, text
    assert "我的结果" in text
    assert "别人的结果" not in text, "本会话看不到别的会话的结果"
    assert cross.startswith("【拒绝】RR-2"), cross
    assert "别人的结果" not in cross


# ------------------------------------------------------------- EX-45 逐条截断


def test_each_item_is_truncated_to_max_chars(tmp_path):
    plugin = _plugin(tmp_path, max_chars=1500)
    event = _event()
    long_body = "长" * 5000

    async def scenario():
        async def _long(progress: dict) -> str:
            return long_body

        decision = _submit(plugin, event, _long, task_text="long")
        await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        return await plugin.browse_recent_results(event)

    text = asyncio.run(scenario())
    blocks = text.split("\n\n")[1:]
    assert len(blocks) == 1
    body = blocks[0].split("\n", 1)[1]
    assert len(body) <= 1500 + len(tb.TRUNCATION_NOTE.format(omitted=99999))
    assert body.rstrip().endswith("）"), body[-40:]
    assert tb.TRUNCATION_NOTE.format(omitted=5000 - 1500) in body, body[-80:]
    assert "5000 字" in blocks[0].splitlines()[0], "条目行报的是**原始**字数"


# ------------------------------------------- EX-46 空窗 / RR-3 / 参数钳制


def test_empty_window_and_mode_off(tmp_path):
    plugin = _plugin(tmp_path)
    text = _recall(plugin, _event())
    assert text.startswith("【暂无结果】"), text
    assert "不跨会话" in text and "30" in text
    off = _plugin(tmp_path / "off", mode="off")
    assert _recall(off, _event()) == tb.REJECT_TEXTS["RR-3"]


def test_clamped_params_are_notice_not_rejection(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        return await plugin.browse_recent_results(event, limit=99, minutes=999)

    text = asyncio.run(scenario())
    assert text.splitlines()[0] == "（参数已钳制：minutes=30、limit=5）", text[:80]
    assert "【最近结果】" in text and "【拒绝】" not in text, "钳制不是拒绝"


def test_garbage_params_fall_back_to_defaults(tmp_path):
    plugin = _plugin(tmp_path)
    text = _recall(plugin, _event(), limit="abc", minutes=None)
    assert text.startswith("【暂无结果】"), text
    assert "最近 30 分钟" in text


def test_task_id_filter_returns_only_that_result(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        ids = []
        for index in range(3):
            decision = _submit(plugin, event, _ok, task_text=f"t{index}")
            ids.append(decision.task_id)
            await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        text = await plugin.browse_recent_results(event, task_id=ids[1])
        missing = await plugin.browse_recent_results(event, task_id="bg-9999")
        return text, missing, ids[1]

    text, missing, wanted = asyncio.run(scenario())
    assert text.splitlines()[0] == "【最近结果】本会话 · 最近 30 分钟 · 1 条（上限 5）", text[:80]
    assert f"task_id={wanted}" in text
    assert text.count("【1】") == 1 and "【2】" not in text
    assert missing == tb.REJECT_TEXTS["RR-2"].format(task_id="bg-9999")


# --------------------------------------------------------- EX-47 只读 / 不写历史


def test_recall_tool_is_read_only(tmp_path):
    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        before = [dataclasses.asdict(record) for record in plugin._book]
        text = await plugin.browse_recent_results(event)
        after = [dataclasses.asdict(record) for record in plugin._book]
        return before, after, text

    before, after, text = asyncio.run(scenario())
    assert before == after, "回看工具改动了记录（只读不成立）"
    assert SENTINEL in text


def test_recall_never_writes_conversation_history(tmp_path):
    """规格 §2.1-H2：两个新工具与任务簿都**不碰** `conv.history` / 会话历史管理器。"""
    import ast

    for rel in ("main.py", "core/tasks.py", "core/proactive.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle in ("conversation_manager", "conv.history", "conv_history"):
            assert needle not in text, f"{rel} 引用了会话历史：{needle}"
        for node in ast.walk(tree):  # 也不许把这些名字当属性/变量用
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("history", "conversations"), (
                    f"{rel} 触到了会话历史属性 {node.attr}")

    plugin = _plugin(tmp_path)
    event = _event()

    async def scenario():
        async def _ok(progress: dict) -> str:
            return SENTINEL

        decision = _submit(plugin, event, _ok)
        await _wait_until(lambda tid=decision.task_id: plugin._book.get(tid).is_terminal)
        return await plugin.browse_recent_results(event)

    assert SENTINEL in asyncio.run(scenario())
    assert not hasattr(plugin, "conversation_manager"), "插件不该持有会话历史管理器"
