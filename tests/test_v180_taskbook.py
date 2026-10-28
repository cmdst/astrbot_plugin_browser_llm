"""`core/tasks.py` 执行级用例（规格 §5 / §11.2 EX-16..EX-27 的 t2 面）。

纪律：全部真跑状态机（假时钟或极短真实预算），断言**枚举值、字段、渲染文案**，
不做 `assert "xxx" in 源码` 这类形态断言（规格 §11.7）。
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import re
import time
from pathlib import Path

import pytest

from core import tasks as tb

ROOT = Path(__file__).resolve().parent.parent


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _book(**kwargs) -> tb.TaskBook:
    kwargs.setdefault("grace", 0.05)
    kwargs.setdefault("poll", 0.01)
    return tb.TaskBook(**kwargs)


def _recorder(bucket: list) -> tb.Deliver:
    async def deliver(record: tb.TaskRecord) -> tb.DeliveryNote:
        bucket.append((record.task_id, record.state))
        return tb.DeliveryNote(attempted=True, succeeded=True,
                               channel="context.send_message", shape="A")

    return deliver


def _record(state: str = tb.STATE_SUCCEEDED, **kwargs) -> tb.TaskRecord:
    base = {"task_id": "bg-0001", "umo": "aiocqhttp:FriendMessage:u1", "tool": "browse_web",
            "task_key": "k" * 16, "task_text_digest": "d" * 8, "state": state,
            "created_at": 0.0, "started_at": 0.0, "finished_at": 0.0, "budget_s": 300.0}
    base.update(kwargs)
    return tb.TaskRecord(**base)


# ---------------------------------------------------------------- EX-16 七态常量


def test_seven_states_and_disjoint_sets():
    assert len(tb.STATES) == 7
    assert tb.TERMINAL_STATES == {tb.STATE_SUCCEEDED, tb.STATE_FAILED, tb.STATE_TIMEOUT,
                                  tb.STATE_CANCELLED}
    assert tb.NON_TERMINAL_STATES == {tb.STATE_QUEUED, tb.STATE_RUNNING, tb.STATE_DELIVERING}
    assert not (tb.TERMINAL_STATES & tb.NON_TERMINAL_STATES)
    assert tb.TERMINAL_STATES | tb.NON_TERMINAL_STATES == set(tb.STATES)
    assert tb.MAX_DONE_RECORDS == 64 and tb.MAX_QUEUE_DEPTH == 8


# ------------------------------------------------- EX-17 非法转移必须抛 ValueError


def test_illegal_transition_raises_value_error():
    book = _book()
    record = _record(state=tb.STATE_QUEUED)
    with pytest.raises(ValueError):
        book._set_state(record, tb.STATE_SUCCEEDED)  # queued 不许直跳终态
    book._set_state(record, tb.STATE_RUNNING)
    with pytest.raises(ValueError):
        book._set_state(record, tb.STATE_SUCCEEDED)  # running 必须先到 delivering
    book._set_state(record, tb.STATE_DELIVERING)
    with pytest.raises(ValueError):
        book._set_state(record, tb.STATE_RUNNING)  # delivering 不许回退
    book._set_state(record, tb.STATE_SUCCEEDED)
    with pytest.raises(ValueError):
        book._set_state(record, tb.STATE_FAILED)  # 终态不可变


# --------------------------------- EX-18 每任务必过 delivering、deliver 恰好一次


def test_task_passes_delivering_and_delivers_exactly_once():
    deliveries: list = []

    async def scenario():
        book = _book()
        async def runner(progress: dict) -> str:
            progress["steps"] = 3
            return "结果正文"

        decision = book.submit(runner=runner, deliver=_recorder(deliveries), umo="u1",
                               tool="browse_web", task_text="查一下", budget_s=5.0)
        assert decision.kind == tb.SUBMIT_ACCEPTED
        assert "【已受理】" in decision.text and decision.accepted
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record, decision

    record, decision = asyncio.run(scenario())
    assert record.state == tb.STATE_SUCCEEDED
    assert record.attempts == 1 and record.started_at > 0 and record.finished_at > 0
    assert record.steps == 3
    assert deliveries == [(decision.task_id, tb.STATE_DELIVERING)], "投递时必须已进入 delivering"
    assert record.delivery.succeeded and record.delivery.channel == "context.send_message"
    assert record.delivery.as_fields().startswith("attempted=1 succeeded=1 channel=context")


def test_progress_dict_is_the_runner_side_channel():
    seen = {}

    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            progress["partial"] = "半成品"
            progress["steps"] = 2
            seen.update(progress)
            raise RuntimeError("炸了")

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="t",
                               budget_s=5.0)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record

    record = asyncio.run(scenario())
    assert seen["steps"] == 2
    assert record.state == tb.STATE_FAILED
    assert record.reason == "RuntimeError: 炸了"
    assert record.partial == "半成品"  # 中断前半成品被留下
    assert record.steps == 2


# -------------------------------------------- EX-19 预算到点 ⇒ timeout 终态


def test_budget_timeout_lands_in_terminal_timeout():
    deliveries: list = []

    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            await asyncio.sleep(5)
            return "never"

        decision = book.submit(runner=runner, deliver=_recorder(deliveries), umo="u1",
                               tool="browse_web", budget_s=0.12)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record

    record = asyncio.run(scenario())
    assert record.state == tb.STATE_TIMEOUT
    first = tb.render_terminal_text(record, record.result_text).splitlines()[0]
    assert first.startswith(tb.HEADERS[tb.STATE_TIMEOUT])
    assert len(deliveries) == 1


# --------------------------- EX-20 执行体吞掉取消时，看门狗补终态且总共只一条


def test_watchdog_forces_one_terminal_even_if_runner_swallows_cancel():
    deliveries: list = []
    swallowed: list = []

    async def scenario():
        book = _book(grace=0.05)

        stop_at = time.monotonic() + 0.5  # 远大于 deadline(0.1)+grace(0.05)

        async def stubborn(progress: dict) -> str:
            while time.monotonic() < stop_at:  # 吞掉取消、继续睡：wait_for 永不返回
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    swallowed.append(1)
                    continue
            return "太晚了"

        decision = book.submit(runner=stubborn, deliver=_recorder(deliveries), umo="u1",
                               tool="browse_web", budget_s=0.1)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        await asyncio.sleep(0.2)  # 给第二条终态（若存在）出现的窗口
        return record

    record = asyncio.run(scenario())
    assert swallowed, "执行体确实吞掉了一次取消"
    assert record.state == tb.STATE_TIMEOUT
    assert len(deliveries) == 1, f"终态只允许一条，实际 {len(deliveries)} 条"


# ---------------------------- EX-21 排队期耗尽预算 ⇒ timeout 且不启动执行体


def test_queued_budget_expiry_never_starts_runner():
    deliveries: list = []
    started: list = []

    async def scenario():
        book = _book()

        async def blocker(progress: dict) -> str:
            await asyncio.sleep(0.4)
            return "一"

        async def never(progress: dict) -> str:
            started.append(1)
            return "二"

        first = book.submit(runner=blocker, deliver=_recorder(deliveries), umo="u1",
                            tool="browse_web", task_text="一", budget_s=5.0, max_parallel=1)
        assert first.kind == tb.SUBMIT_ACCEPTED
        second = book.submit(runner=never, deliver=_recorder(deliveries), umo="u2",
                             tool="browse_web", task_text="二", budget_s=0.12, max_parallel=1)
        assert second.kind == tb.SUBMIT_QUEUED
        assert "【排队中】" in second.text and "队列第 1 位" in second.text
        record = book.get(second.task_id)
        assert record.state == tb.STATE_QUEUED
        await _wait_until(lambda: record.is_terminal)
        return record

    record = asyncio.run(scenario())
    assert record.state == tb.STATE_TIMEOUT
    assert record.reason == "queued_budget"
    assert record.attempts == 0, "排队期超预算不得启动执行体"
    assert started == []


def test_queue_pump_starts_next_task_when_slot_frees():
    order: list = []

    async def scenario():
        book = _book()
        gate = asyncio.Event()

        async def first(progress: dict) -> str:
            await gate.wait()
            return "一"

        async def second(progress: dict) -> str:
            order.append("second-started")
            return "二"

        one = book.submit(runner=first, deliver=_recorder([]), umo="u1", tool="t", budget_s=5.0,
                          max_parallel=1)
        two = book.submit(runner=second, deliver=_recorder([]), umo="u2", tool="t", budget_s=5.0,
                          max_parallel=1)
        assert two.kind == tb.SUBMIT_QUEUED
        assert [r.task_id for r in book.queued()] == [two.task_id]
        assert book.queue_position(two.task_id) == 1
        gate.set()
        await _wait_until(lambda: book.get(two.task_id).state == tb.STATE_RUNNING)
        await _wait_until(lambda: book.get(one.task_id).is_terminal and
                          book.get(two.task_id).is_terminal)
        return book

    asyncio.run(scenario())
    assert order == ["second-started"]


# ------------------------------------------------ EX-22 去重窗口三态


def test_dedupe_running_done_and_window():
    async def scenario():
        book = _book()
        slow_gate = asyncio.Event()

        async def slow(progress: dict) -> str:
            await slow_gate.wait()
            return "正文A"

        def submit():
            return book.submit(runner=slow, deliver=_recorder([]), umo="u1", tool="browse_web",
                               task_text="同一句话", budget_s=5.0, dedupe_window_s=60.0)

        first = submit()
        dup_running = submit()
        slow_gate.set()
        await _wait_until(lambda: book.get(first.task_id).is_terminal)
        dup_done = submit()
        fresh = book.submit(runner=slow, deliver=_recorder([]), umo="u1", tool="browse_web",
                            task_text="同一句话", budget_s=5.0, dedupe_window_s=0.0)
        return first, dup_running, dup_done, fresh, book

    first, dup_running, dup_done, fresh, book = asyncio.run(scenario())
    assert dup_running.kind == tb.SUBMIT_DUPLICATE_RUNNING
    assert dup_running.task_id == first.task_id and "同一任务正在执行" in dup_running.text
    assert dup_done.kind == tb.SUBMIT_DUPLICATE_DONE
    assert "【重复请求】" in dup_done.text and "正文A" in dup_done.text
    assert dup_done.duplicate_of == first.task_id
    assert fresh.kind == tb.SUBMIT_ACCEPTED, "窗口关掉后必须重新受理"
    assert book.get(fresh.task_id).task_id == fresh.task_id


def test_single_flight_and_busy_texts():
    async def scenario():
        book = _book(max_queue=1)
        gate = asyncio.Event()

        async def blocker(progress: dict) -> str:
            await gate.wait()
            return "一"

        one = book.submit(runner=blocker, deliver=_recorder([]), umo="u1", tool="t",
                          task_text="甲", budget_s=5.0, max_parallel=1)
        busy = book.submit(runner=blocker, deliver=_recorder([]), umo="u1", tool="t",
                          task_text="乙", budget_s=5.0, max_parallel=1)
        queued = book.submit(runner=blocker, deliver=_recorder([]), umo="u2", tool="t",
                            task_text="丙", budget_s=5.0, max_parallel=1)
        overflow = book.submit(runner=blocker, deliver=_recorder([]), umo="u3", tool="t",
                              task_text="丁", budget_s=5.0, max_parallel=1)
        gate.set()
        await book.cancel_all(0.5)
        return one, busy, queued, overflow

    one, busy, queued, overflow = asyncio.run(scenario())
    assert one.kind == tb.SUBMIT_ACCEPTED
    assert busy.kind == tb.SUBMIT_SINGLE_FLIGHT and "【拒绝】" in busy.text
    assert "本会话已有一个后台任务在跑" in busy.text
    assert queued.kind == tb.SUBMIT_QUEUED
    assert overflow.kind == tb.SUBMIT_BUSY
    assert "后台任务已满" in overflow.text and not overflow.accepted


# ------------------------------------------------ EX-23 保留上限 64 与淘汰


def test_retention_cap_evicts_oldest_terminal_records():
    async def scenario():
        book = _book()
        ids = []
        for index in range(70):
            async def runner(progress: dict, index: int = index) -> str:
                return f"正文{index}"

            decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="t",
                                   task_text=f"任务{index}", budget_s=5.0)
            ids.append(decision.task_id)
            await _wait_until(lambda tid=decision.task_id: book.get(tid).is_terminal)
        return book, ids

    book, ids = asyncio.run(scenario())
    terminal = [record for record in book if record.is_terminal]
    assert len(terminal) == tb.MAX_DONE_RECORDS
    assert book.get(ids[0]) is None, "最老的终态记录必须被淘汰"
    assert book.get(ids[-1]) is not None
    assert book.recent_of("u1", window_s=600.0, limit=200) == [] or all(
        r.task_id != ids[0] for r in book.recent_of("u1", window_s=600.0, limit=200))


# ------------------------------------------------ EX-24 四态四前缀 + 排队前缀唯一


def test_terminal_headers_are_verbatim_and_queued_header_is_unique():
    assert tb.HEADERS == {
        tb.STATE_SUCCEEDED: "【后台任务完成】", tb.STATE_FAILED: "【后台任务失败】",
        tb.STATE_TIMEOUT: "【后台任务超时】", tb.STATE_CANCELLED: "【后台任务已取消】"}
    queued_text = tb.render_queued_text(task_id="bg-0001", tool="browse_web", budget_s=300,
                                        position=2)
    assert tb.QUEUED_HEADER == "【排队中】" and queued_text.startswith(tb.QUEUED_HEADER)
    others = [
        tb.render_accepted_text(task_id="bg-0001", tool="browse_web", budget_s=300),
        tb.render_single_flight_text(task_id="bg-0001", tool="browse_web", elapsed_s=12),
        tb.render_busy_text(limit=8, running=4),
        tb.render_duplicate_text(task_id="bg-0001", tool="browse_web", elapsed_s=9),
        tb.render_duplicate_text(task_id="bg-0001", tool="browse_web", elapsed_s=0, running=True),
    ]
    for text in others:
        assert tb.QUEUED_HEADER not in text
    for state in (tb.STATE_FAILED, tb.STATE_TIMEOUT, tb.STATE_CANCELLED):
        first = tb.render_terminal_text(_record(state=state), "x").splitlines()[0]
        assert first.startswith(tb.HEADERS[state])
        assert not first.startswith(tb.HEADERS[tb.STATE_SUCCEEDED])


# --------------------------------- EX-25 空结果 / 半成品 / 诊断三分支逐字


def test_terminal_text_branches_are_verbatim():
    empty_ok = tb.render_terminal_text(_record(), "")
    assert tb.EMPTY_RESULT_NOTE in empty_ok

    partial = tb.render_terminal_text(_record(state=tb.STATE_TIMEOUT, partial="半成品正文"), "")
    lines = partial.splitlines()
    assert tb.PARTIAL_NOTE in lines and "半成品正文" in lines
    assert "本次没有可用结果。" not in lines, "有半成品时不再说没有结果"
    assert "可调大 resources.total_budget_s，或把任务拆小。" in lines

    bare_timeout = tb.render_terminal_text(_record(state=tb.STATE_TIMEOUT), "")
    assert "已用满 300 秒硬预算，任务被中断。" in bare_timeout
    assert "本次没有可用结果。" in bare_timeout

    with_reason = tb.render_terminal_text(_record(state=tb.STATE_TIMEOUT, reason="工具自报"),
                                          "")
    assert "任务在 300 秒预算内因工具自身超时而中断。" in with_reason
    assert "原因：工具自报" in with_reason

    failed = tb.render_terminal_text(_record(state=tb.STATE_FAILED, reason="boom"), "")
    assert "失败原因：boom" in failed and "本次没有可用结果。" in failed

    cancelled = tb.render_terminal_text(_record(state=tb.STATE_CANCELLED), "")
    assert "插件被重载或禁用（含 astrbot.service 重启/插件更新），任务被取消。" in cancelled

    diagnostics = tb.render_terminal_text(_record(), "正文", diagnostics=True)
    assert "（诊断：已用 0s / 预算 300s / 剩余 0s）" in diagnostics


def test_terminal_text_reports_failed_delivery_for_succeeded_task():
    record = _record()
    record.delivery = tb.DeliveryNote(attempted=True, succeeded=False, error="PlatformNotFound")
    text = tb.render_terminal_text(record, "正文")
    assert "（投递失败：PlatformNotFound；结果仍可用 browse_recent_results 回看）" in text


# ------------------------------------------------ EX-26 取消与 CancelledError 不吞


def test_cancel_all_cancels_running_and_queued():
    seen: dict = {"cancelled": 0}

    async def scenario():
        book = _book()
        gate = asyncio.Event()

        async def running_runner(progress: dict) -> str:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                seen["cancelled"] += 1
                raise  # 不吞：登记终态后原样上抛
            return "never"

        async def queued_runner(progress: dict) -> str:
            return "不该跑到这里"

        one = book.submit(runner=running_runner, deliver=_recorder([]), umo="u1", tool="t",
                          task_text="甲", budget_s=5.0, max_parallel=1)
        two = book.submit(runner=queued_runner, deliver=_recorder([]), umo="u2", tool="t",
                          task_text="乙", budget_s=5.0, max_parallel=1)
        assert two.kind == tb.SUBMIT_QUEUED
        gate.set()
        await asyncio.sleep(0.02)  # 让执行体真的进到 try 块里再取消（否则取消发生在首步之前）
        count = await book.cancel_all(0.5)
        return book, one, two, count

    book, one, two, count = asyncio.run(scenario())
    assert count == 2
    assert seen["cancelled"] >= 1
    assert book.get(one.task_id).state == tb.STATE_CANCELLED
    assert book.get(two.task_id).state == tb.STATE_CANCELLED


def test_cancel_single_task_returns_false_for_terminal():
    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            return "done"

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="t",
                               budget_s=5.0)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return book, decision

    book, decision = asyncio.run(scenario())
    assert book.cancel(decision.task_id) is False
    assert book.cancel("bg-9999") is False


# ------------------------------- EX-27 纪律（AST + 行为）：纯 Python、不持 event、日志无正文


def test_module_is_pure_python_and_holds_no_event():
    source = (ROOT / "core" / "tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "asyncio", "hashlib", "logging", "re", "time",
                        "dataclasses", "typing"}, imported
    fields = {item.name for item in dataclasses.fields(tb.TaskRecord)}
    assert not (fields & {"event", "context", "ctx", "astr_event"})
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "conversation_manager" not in attributes | names
    assert "history" not in attributes


def test_logs_never_contain_result_body(caplog):
    sentinel = "SENTINEL-聊天正文-不许进日志"

    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            return sentinel

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="browse_web",
                               task_text="看这个页面", budget_s=5.0)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record

    with caplog.at_level("INFO"):
        record = asyncio.run(scenario())
    assert record.result_text == sentinel
    assert sentinel not in caplog.text
    assert record.result_digest and len(record.result_digest) == 12
    assert record.task_text_digest and len(record.task_text_digest) == 8


def test_progress_view_has_no_body_and_no_plain_umo():
    record = _record(umo="aiocqhttp:FriendMessage:secret-user", state=tb.STATE_RUNNING,
                     started_at=1.0, created_at=0.0, budget_s=300.0)
    record.result_text = "正文"
    record.result_chars = 2
    view = record.progress_view(now=2.0)
    assert set(view) == {"task_id", "tool", "state", "elapsed_s", "budget_s", "remaining_s",
                         "queued_s", "attempts", "steps", "task_key", "umo_digest",
                         "result_digest"}
    assert "正文" not in json.dumps(view, ensure_ascii=False)
    assert "secret-user" not in json.dumps(view, ensure_ascii=False)
    assert len(view["umo_digest"]) == 8
    assert view["elapsed_s"] == 1.0 and view["queued_s"] == 1.0


# ---------------------------------------------- 记账：截断 / 幂等 / 投递异常


def test_set_result_truncates_but_keeps_full_length_and_digest():
    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            return "字" * 50

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="t",
                               budget_s=5.0, max_chars=10)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record

    record = asyncio.run(scenario())
    assert record.result_chars == 50
    assert tb.TRUNCATION_NOTE.format(omitted=40) in record.result_text
    assert len(record.result_digest) == 12


def test_note_delivery_is_idempotent_after_terminal():
    async def scenario():
        book = _book()

        async def runner(progress: dict) -> str:
            return "done"

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="t",
                               budget_s=5.0)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        second = book.note_delivery(decision.task_id, tb.DeliveryNote(attempted=True))
        return record, second

    record, second = asyncio.run(scenario())
    assert record.state == tb.STATE_SUCCEEDED
    assert second is None, "终态不可变：重复记账必须返回 None"
    assert record.delivery.succeeded is True, "重复记账不得覆盖第一次记账"


def test_deliver_raising_is_accounted_not_raised():
    async def scenario():
        book = _book()

        async def deliver(record: tb.TaskRecord) -> tb.DeliveryNote:
            raise RuntimeError("通道炸了")

        async def runner(progress: dict) -> str:
            return "done"

        decision = book.submit(runner=runner, deliver=deliver, umo="u1", tool="t", budget_s=5.0)
        record = book.get(decision.task_id)
        await _wait_until(lambda: record.is_terminal)
        return record

    record = asyncio.run(scenario())
    assert record.state == tb.STATE_SUCCEEDED
    assert record.delivery.attempted is True and record.delivery.succeeded is False
    assert record.delivery.error == "DeliveryRaised:RuntimeError"


# ------------------------------------------------ recent_of / 别名 / 状态渲染器


def test_recent_of_is_per_umo_and_only_succeeded_with_body():
    async def scenario():
        book = _book()

        async def ok(progress: dict) -> str:
            return "有正文"

        async def nothing(progress: dict) -> str:
            return ""

        async def boom(progress: dict) -> str:
            raise RuntimeError("x")

        for umo, runner in (("u1", ok), ("u1", nothing), ("u1", boom), ("u2", ok)):
            decision = book.submit(runner=runner, deliver=_recorder([]), umo=umo, tool="t",
                                   task_text=f"{umo}-{len(book)}", budget_s=5.0)
            await _wait_until(lambda tid=decision.task_id: book.get(tid).is_terminal)
        return book

    book = asyncio.run(scenario())
    u1 = book.recent_of("u1", window_s=600.0, limit=5)
    assert [record.result_text for record in u1] == ["有正文"]
    assert [record.umo for record in book.recent_of("u2", window_s=600.0, limit=5)] == ["u2"]
    assert book.recent_of("u1", window_s=0.0, limit=5) == []
    # 任务书别名与规格主名同义
    assert book.recent("u1", window_s=600.0, limit=5) == u1
    assert book.status(u1[0].task_id) is u1[0]
    assert book.progress("bg-9999")["state"] == "unknown"


def test_render_status_text_all_six_shapes():
    assert tb.render_status_text(None, mode="off") == tb.REJECT_TEXTS["TS-3"]
    idle = tb.render_status_text(None, mode="browse_only", recent_count=0)
    assert idle.startswith("【无任务】") and "也没有已完成任务" in idle
    with_history = tb.render_status_text(None, mode="browse_only", recent_count=2)
    assert "有 2 条已完成" in with_history and "browse_recent_results" in with_history

    record = _record(state=tb.STATE_RUNNING, created_at=0.0, started_at=1.0, budget_s=300.0)
    running = tb.render_status_text(record, mode="browse_only", now=4.5)
    assert running.startswith("【状态】task_id=bg-0001 | 工具=browse_web | 状态=在跑 |")
    assert "已用 3.5s / 预算 300s | 剩余 295.5s" in running

    queued = _record(state=tb.STATE_QUEUED, created_at=0.0, budget_s=300.0)
    queued.meta["queue_position"] = 3
    queued_text = tb.render_status_text(queued, mode="browse_only", now=2.0)
    assert "状态=排队中（第 3 位） | 已等待 2.0s" in queued_text

    delivering = tb.render_status_text(_record(state=tb.STATE_DELIVERING), mode="browse_only",
                                       now=1.0)
    assert "状态=投递中 | 已用 0.0s" in delivering

    words = {tb.STATE_SUCCEEDED: "完成", tb.STATE_FAILED: "失败",
             tb.STATE_TIMEOUT: "超时", tb.STATE_CANCELLED: "取消"}
    for state, word in words.items():
        text = tb.render_status_text(_record(state=state), mode="browse_only", now=1.0)
        assert f"状态={word} |" in text
    succeeded = _record(state=tb.STATE_SUCCEEDED)
    succeeded.result_chars = 42
    assert "结果 42 字（可用 browse_recent_results 回看）" in tb.render_status_text(
        succeeded, mode="browse_only", now=1.0)
    assert "本次没有可用结果" in tb.render_status_text(_record(state=tb.STATE_FAILED),
                                                   mode="browse_only", now=1.0)


def test_render_status_text_has_no_body_and_no_absolute_time():
    record = _record(state=tb.STATE_SUCCEEDED, started_at=0.0, finished_at=1.0)
    record.result_text = "SENTINEL-正文-不许出现"
    record.result_chars = 12
    text = tb.render_status_text(record, mode="browse_only", now=2.0)
    assert "SENTINEL" not in text
    assert not re.search(r"20\d\d-", text)
    assert not re.search(r"\d{1,2}:\d{2}", text)
    assert "失败原因" not in text


def test_render_recall_text_block_prefix_and_truncation():
    records = [_record(task_id="bg-0001"), _record(task_id="bg-0002")]
    records[0].result_chars, records[1].result_chars = 5, 5000
    records[0].result_text = "短正文"
    records[1].result_text = "长" * 5000
    records[0].created_at = records[1].created_at = 0.0
    text = tb.render_recall_text(records, now=100.0, max_chars=20, window_min=30, limit=5)
    lines = text.split("\n")
    assert lines[0] == "【最近结果】本会话 · 最近 30 分钟 · 2 条（上限 5）"
    assert lines[1] == tb.UNTRUSTED_PREFIX
    blocks = text.split("\n\n")[1:]
    assert blocks[0].startswith("【1】task_id=bg-0001 | 工具=browse_web | 完成 · 1 分钟前 | 5 字")
    assert "短正文" in blocks[0]
    assert tb.TRUNCATION_NOTE.format(omitted=4980) in blocks[1]
    empty = tb.render_recall_text([], now=0.0, max_chars=20, window_min=30, limit=5)
    assert empty.startswith("【暂无结果】")
    assert tb.render_reject("RR-2", task_id="bg-9") == tb.REJECT_TEXTS["RR-2"].format(
        task_id="bg-9")
    assert tb.render_reject("TS-2", task_id="bg-9").startswith("【拒绝】TS-2")


def test_render_recall_text_age_labels():
    fresh = _record(task_id="bg-0001", created_at=time.monotonic())
    fresh.result_text, fresh.result_chars = "x", 1
    text = tb.render_recall_text([fresh], now=time.monotonic(), max_chars=10, window_min=30,
                                 limit=5)
    assert "完成 · 刚刚 |" in text


# ============================================================================
# v1.8.0 / t3：接替 v1.7.5 `test_v175_background.py`（47 条）的**剩余组**
#
# 规格 §10.4 的映射表逐条要求：纯函数组、五态文案组（含新增的 queued / busy）、
# 提前完成不算第二次终态、CancelledError 不吞、未知任务的进度视图、跨会话不串。
# 这些组在删除 47 条时必须**有替代者**，所以单独列在这里，不混进上面按 EX-* 编号的组。
# ============================================================================


def test_pure_helpers_keep_their_verbatim_contract():
    """纯函数组：`normalize_task_text` / `task_key` / `digest_text` / `truncate_result`。"""
    assert tb.normalize_task_text("  perception=text  看这个页面 ") == "看这个页面"
    assert tb.normalize_task_text("Hello   WORLD") == "hello world"
    key = tb.task_key("umo-a", "browse_web", "  看页面 ")
    assert key == tb.task_key("umo-a", "browse_web", "看页面"), "规范化后同键"
    assert len(key) == 16 and key.isalnum()
    assert key != tb.task_key("umo-b", "browse_web", "看页面"), "umo 参与键"
    assert key != tb.task_key("umo-a", "browse_local_page", "看页面"), "tool 参与键"
    assert key != tb.task_key("umo-a", "browse_web", "别的页面"), "任务文本参与键"
    assert tb.digest_text("abcdef", 4) == tb.digest_text("abcdef", 4)
    assert len(tb.digest_text("abcdef", 12)) == 12
    text, truncated, omitted = tb.truncate_result("x" * 10, 4)
    assert truncated is True and omitted == 6
    assert text.startswith("xxxx") and tb.TRUNCATION_NOTE.format(omitted=6) in text
    assert tb.truncate_result("abc", 0) == ("abc", False, 0), "limit<=0 不截断"
    assert tb.truncate_result(None, 5) == ("", False, 0)
    assert tb.shorten("y" * 50, 10).endswith("…") and len(tb.shorten("y" * 50, 10)) <= 11


def test_accepted_text_says_there_is_no_result_yet():
    """受理文案必须明说"你现在还没有结果"（v1.7.5 的四句硬话逐字保留）。"""
    text = tb.render_accepted_text(task_id="bg-0007", tool="browse_web", budget_s=300.0)
    assert text.startswith(tb.ACCEPTED_HEADER), text[:40]
    assert "task_id=bg-0007" in text and "browse_web" in text and "硬预算 300 秒" in text
    assert "单独作为一条消息" in text
    assert "还没有" in text and "不要编造" in text
    for state in tb.TERMINAL_STATES:
        assert f"「{tb.HEADERS[state]}」" in text, f"受理文案没说清终态前缀 {state}"


def test_five_submit_texts_are_verbatim():
    """五态文案：accepted / queued / single_flight / busy / duplicate（queued 与 busy 是新增）。"""
    accepted = tb.render_accepted_text(task_id="bg-1", tool="browse_web", budget_s=300.0)
    assert accepted.startswith("【已受理】后台任务已开始执行")
    queued = tb.render_queued_text(task_id="bg-1", tool="browse_web", budget_s=300.0,
                                   position=2)
    assert queued.startswith(tb.QUEUED_HEADER), queued[:40]
    assert "第 2 位" in queued and "排队" in queued
    assert "还没有" in queued, "排队同样是「没结果」，必须说清"
    single = tb.render_single_flight_text(task_id="bg-2", tool="browse_web", elapsed_s=12.5)
    assert single.startswith(tb.REJECTED_HEADER) and "bg-0002" not in single
    busy = tb.render_busy_text(limit=8, running=4)
    assert busy.startswith(tb.REJECTED_HEADER) and "8" in busy and "4" in busy
    duplicate = tb.render_duplicate_text(task_id="bg-3", tool="browse_web", elapsed_s=30.0,
                                         body="已完成正文", max_chars=100)
    assert duplicate.startswith(tb.DUPLICATE_HEADER), duplicate[:40]
    assert "已完成正文" in duplicate, "重复回执要把结果给模型，否则等于没说"


def test_early_completion_never_produces_a_second_terminal():
    """提前完成 ⇒ 看门狗到点**不再**补第二条（`_finalized` 硬闸）。"""
    notes: list[str] = []

    async def scenario():
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        async def sleep(seconds: float) -> None:
            clock["t"] += float(seconds)  # 假时钟：sleep 即推进
            await asyncio.sleep(0)

        book = tb.TaskBook(clock=now, sleep=sleep, grace=0.0, poll=0.5)

        async def quick(progress: dict) -> str:
            return "很快"

        decision = book.submit(runner=quick, deliver=_recorder(notes), umo="u1", tool="t",
                               task_text="early", budget_s=10.0)
        record = book.get(decision.task_id)
        for _ in range(200):
            if record.is_terminal and clock["t"] > 20:
                break
            await asyncio.sleep(0.005)
            if not record.is_terminal:
                clock["t"] += 1.0
        return record, len(notes)

    record, count = asyncio.run(scenario())
    assert record.state == tb.STATE_SUCCEEDED
    assert count == 1, f"只允许一条投递，实际 {count}"


def test_cancelled_error_propagates_after_terminal_is_recorded():
    """`CancelledError` **不吞**：登记终态后原样上抛；调用方 `await` 能看到取消。"""
    async def scenario():
        book = _book()

        async def stubborn(progress: dict) -> str:
            await asyncio.sleep(30)
            return "never"

        decision = book.submit(runner=stubborn, deliver=_recorder([]), umo="u1", tool="t",
                               task_text="cancel", budget_s=30.0)
        await asyncio.sleep(0.02)
        task = book.get(decision.task_id).meta.get("task")
        book.cancel(decision.task_id)
        with pytest.raises(asyncio.CancelledError):
            await task
        return book.get(decision.task_id)

    record = asyncio.run(scenario())
    assert record.state == tb.STATE_CANCELLED, record.state
    assert record.is_terminal


def test_progress_of_unknown_task_is_field_complete():
    """未知 `task_id` 的进度视图也要**键齐全**（调用方不必写 KeyError 分支）。"""
    book = _book()
    view = book.progress("bg-9999")
    assert set(view) == set(_record().progress_view().keys())
    assert view["state"] == "unknown" and view["task_id"] == "bg-9999"


def test_two_umos_run_concurrently_without_crossing():
    """两个会话可以同时在跑（单飞是**按 umo** 的），结果与记账不串门。"""
    async def scenario():
        book = _book()
        notes: dict[str, list] = {"u1": [], "u2": []}

        async def make(tag: str):
            async def runner(progress: dict) -> str:
                await asyncio.sleep(0.05)
                return f"结果-{tag}"

            return runner

        one = book.submit(runner=await make("u1"), deliver=_recorder(notes["u1"]), umo="u1",
                          tool="t", task_text="a", budget_s=5.0, max_parallel=4)
        two = book.submit(runner=await make("u2"), deliver=_recorder(notes["u2"]), umo="u2",
                          tool="t", task_text="a", budget_s=5.0, max_parallel=4)
        await _wait_until(lambda: book.get(one.task_id).is_terminal
                          and book.get(two.task_id).is_terminal)
        return book, one, two

    book, one, two = asyncio.run(scenario())
    assert book.get(one.task_id).umo == "u1" and book.get(two.task_id).umo == "u2"
    assert book.get(one.task_id).result_text == "结果-u1"
    assert book.get(two.task_id).result_text == "结果-u2"
    assert [record.task_id for record in book.recent_of("u1", window_s=600.0, limit=5)] == [
        one.task_id]


def test_terminal_text_during_delivering_uses_the_pending_state():
    """回归（t3 修的 core 缺口）：投递回调在 `delivering` 期渲染，前缀必须是**将要落**的终态。

    修前 `render_terminal_text` 直接读 `record.state`（= delivering，不在终态集合里）⇒ 一律
    渲染成「失败」。这条用例把"成功任务在投递期也被渲染成失败"钉死。
    """
    record = _record(state=tb.STATE_DELIVERING, started_at=0.0, budget_s=300.0)
    record.meta["pending_state"] = tb.STATE_SUCCEEDED
    text = tb.render_terminal_text(record, "正文", now=1.0)
    assert text.splitlines()[0].startswith(tb.HEADERS[tb.STATE_SUCCEEDED]), text[:60]
    assert "正文" in text

    record.meta["pending_state"] = tb.STATE_TIMEOUT
    timed = tb.render_terminal_text(record, "", now=1.0)
    assert timed.splitlines()[0].startswith(tb.HEADERS[tb.STATE_TIMEOUT]), timed[:60]
    assert not timed.splitlines()[0].startswith(tb.HEADERS[tb.STATE_SUCCEEDED])


def test_status_and_recall_reports_follow_the_book_clock():
    """回归（t3 修的 core 缺口）：报告函数必须用任务簿的 `clock`，否则假时钟下年龄算成天文数字。"""
    clock = {"t": 1000.0}
    book = tb.TaskBook(clock=lambda: clock["t"])

    async def scenario():
        async def runner(progress: dict) -> str:
            return "正文"

        decision = book.submit(runner=runner, deliver=_recorder([]), umo="u1", tool="browse_web",
                               task_text="clock", budget_s=100000.0)
        await _wait_until(lambda: book.get(decision.task_id).is_terminal)
        return decision

    decision = asyncio.run(scenario())
    assert book.now() == 1000.0
    clock["t"] += 5 * 60
    recall = tb.recall_report(book, umo="u1", mode="browse_only")
    assert "5 分钟前" in recall, recall
    assert decision.task_id in recall
