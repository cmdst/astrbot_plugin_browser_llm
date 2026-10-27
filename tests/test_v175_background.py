"""v1.7.5 后台层判据（spec §6.1–§6.7）：U1 整链预算 / U2 去重与单次投递 / 取消。

这份测试是**行为契约**，不是实现快照。每条用例对应 ops 报错合集里的一个现象：

- U1「后台任务几十分钟还在跑」：``test_watchdog_*`` / ``test_wait_for_layer_*``
  —— 到点必有一条终态，且**只有一条**（两条时间闸 + 投递闸三重收口）。
- U1 二次证据「进度只有'尚未开始上报'」：``test_progress_*`` / ``test_terminal_*``
  —— 进度与终态都带 ``elapsed_s`` / ``budget_s`` / ``remaining_s``。
- U2「一次任务产出 6 条 / 4 条 / 2 条消息」：``test_dedupe_*`` /
  ``test_delivery_*`` —— 同键窗口内只投一次，每 task 主动投递上限 1。
- U2 反向「finished, but nothing was sent」：``test_empty_result_*``
  —— 空结果仍有模板兜底终态。
- U3/U4「后台链自己决定上下文」：``test_module_*`` / ``test_runner_*`` /
  ``test_terminal_first_line_*`` —— 不经 LLM、不读会话历史、终态带任务归属。
- D9「Future exception was never retrieved」：``test_no_unretrieved_exception``
  —— 失败/取消都不把异常遗弃在事件循环里。

时钟一律注入（:class:`FakeClock`），所以「到点」与「提前完成取消定时器」两条路径
都是零真实耗时的确定性断言；只有两层闸里的 ``wait_for`` 那条单独用真实时钟的
极小预算跑（0.05s）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from core.background import (
    ACCEPTED_HEADER,
    DEFAULT_MAX_PARALLEL,
    DUPLICATE_HEADER,
    EMPTY_RESULT_NOTE,
    HEADERS,
    PARTIAL_NOTE,
    REJECTED_HEADER,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    STATUS_TIMEOUT,
    TERMINAL_STATUSES,
    ERROR_DELIVERY_UNREPORTED,
    BackgroundJob,
    BackgroundRegistry,
    BudgetWatchdog,
    _BG_BUSY_TEXT,
    normalize_task_text,
    render_accepted_text,
    render_duplicate_text,
    render_rejected_text,
    render_terminal_text,
    shorten,
    task_key,
    truncate_result,
)

ROOT = Path(__file__).resolve().parent.parent
BG_SRC = (ROOT / "core" / "background.py").read_text(encoding="utf-8")
UMO = "aiocqhttp:GroupMessage:100001"
UMO2 = "aiocqhttp:GroupMessage:200002"


# ---------------------------------------------------------------
# 通用夹具：假时钟 + 投递收集器（模拟 event.send）
# ---------------------------------------------------------------


class FakeClock:
    """虚拟时钟：``sleep`` 不自行推进时间，只能由用例 ``advance`` 唤醒。

    这样「到点」是完全**确定性**的：执行体/看门狗何时醒只由用例决定，
    不会因为看门狗自己 tick 就把预算悄悄耗掉（真实时钟不会等人，假时钟必须等）。
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)
        self._waiters: list[tuple[float, asyncio.Future]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        target = self.t + max(0.0, float(seconds))
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((target, fut))
        await fut  # 只能由 advance() 唤醒

    async def advance(self, seconds: float) -> None:
        """推进虚拟时间并反复唤醒到点的等待者（直到没人再等）。"""
        self.t += max(0.0, float(seconds))
        for _ in range(1000):
            due = [(t, f) for (t, f) in self._waiters if t <= self.t and not f.done()]
            self._waiters = [(t, f) for (t, f) in self._waiters if not f.done()]
            if not due:
                return
            for _, fut in due:
                fut.set_result(None)
            await asyncio.sleep(0)


class Sink:
    """投递收集器：一条终态 = 一次 ``event.send``。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def first_line(self) -> str:
        return self.messages[0].splitlines()[0] if self.messages else ""


def deliver_to(sink: Sink):
    """把 sink 包成 ``deliver(job, status, text)`` 回调（= 生产里的 event.send）。"""

    async def deliver(job, status, text):
        await sink.send(text)

    return deliver


async def settle(*tasks) -> None:
    """等所有任务收尾（含被取消的看门狗），避免悬挂与未回收异常。"""
    pending = [t for t in tasks if t is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def watchdog(clock: FakeClock | None = None, **kwargs) -> BudgetWatchdog:
    """构造注入了假时钟的看门狗（时钟按可调用对象注入，sleep 也走假时钟）。"""
    clock = clock or FakeClock()
    kwargs.setdefault("budget_s", 100.0)
    kwargs.setdefault("grace", 5.0)
    kwargs.setdefault("poll", 10**6)  # 一次睡到目标：不让轮询粒度消耗预算
    return BudgetWatchdog(clock=clock.now, sleep=clock.sleep, **kwargs)


async def _noop_runner(progress: dict) -> str:
    return "ok"


# ---------------------------------------------------------------
# A. 静态门禁：core 引擎退役 + 后台链没有唤醒轮管线
# ---------------------------------------------------------------


def test_module_has_no_retired_engine_token():
    """spec §6.6：core 引擎退役，模块里不得残留该配置键或探测分支。"""
    assert "background_engine" not in BG_SRC
    assert "is_background_task" not in BG_SRC
    assert "_probe_background" not in BG_SRC


def test_module_has_no_wake_round_plumbing():
    """U3/U4：后台链不经唤醒轮 —— 三类管线符号一个都不许出现。"""
    for token in ("send_message_to_user", "persist_agent_history", "build_main_agent"):
        assert token not in BG_SRC, token
    assert "conv.history" not in BG_SRC


def test_public_surface_is_importable():
    """``__all__`` 里每个名字都必须真的存在（防止文档⇄代码漂移）。"""
    import core.background as bg

    assert bg.__all__
    missing = [name for name in bg.__all__ if not hasattr(bg, name)]
    assert missing == []


def test_four_state_headers_are_distinct():
    """四态四前缀：失败/超时/取消不得以「完成」开头（否则 grep 把失败算成成功）。"""
    assert len(set(HEADERS.values())) == 4
    for status in (STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED):
        assert HEADERS[STATUS_OK] not in HEADERS[status]
        assert HEADERS[status].startswith("【后台任务")
    assert TERMINAL_STATUSES == frozenset(
        {STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED}
    )


# ---------------------------------------------------------------
# B. U2 任务键：规范化口径
# ---------------------------------------------------------------


def test_normalize_strips_perception_prefix():
    assert normalize_task_text("perception=structure 打开知乎") == "打开知乎"
    assert normalize_task_text("  PERCEPTION=text  看板  ") == "看板"


def test_normalize_collapses_whitespace_and_casefolds():
    assert normalize_task_text("  Hello\t World \n Foo ") == "hello world foo"


def test_task_key_is_stable_and_short():
    key = task_key(UMO, "browse_web", "看 一下 攻略")
    assert key == task_key(UMO, "browse_web", "看 一下 攻略")
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    # 规范化之后的等价写法必须同键（空白/大小写/感知前缀都不算新任务）
    assert task_key(UMO, "browse_web", " 看 一下   攻略 ") == key
    assert task_key(UMO, "browse_web", "perception=text 看 一下 攻略") == key


def test_task_key_differs_by_umo_tool_or_text():
    base = task_key(UMO, "browse_web", "同一句话")
    assert task_key(UMO2, "browse_web", "同一句话") != base
    assert task_key(UMO, "browse_local_page", "同一句话") != base
    assert task_key(UMO, "browse_web", "另一句话") != base


# ---------------------------------------------------------------
# C. 提交三闸：受理 / 单飞 / 并发 / 去重（窗口内、边界、窗口外）
# ---------------------------------------------------------------


def test_submit_accepts_and_records_deadline():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=60)
        sink = Sink()
        handle = wd.submit(
            runner=_noop_runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="看攻略",
        )
        job = handle.job
        assert handle.decision.kind == "accepted"
        assert handle.reply_text.startswith(ACCEPTED_HEADER)
        assert job.status == STATUS_RUNNING
        assert job.deadline == job.started_at + 60
        assert wd.registry.active_job(UMO).task_id == job.task_id
        await settle(handle.task, handle.guard_task)

    asyncio.run(main())


def test_single_flight_rejects_second_job_same_umo():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()
        first = wd.submit(
            runner=_noop_runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="任务甲",
        )
        second = wd.submit(
            runner=_noop_runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="任务乙",
        )
        assert second.task is None and second.job is None
        assert second.decision.kind == "single_flight"
        assert second.reply_text.startswith(REJECTED_HEADER)
        assert first.task_id in second.reply_text
        await settle(first.task, first.guard_task)

    asyncio.run(main())


def test_concurrency_gate_rejects_when_at_limit():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, max_parallel=2)
        sink = Sink()
        handles = [
            wd.submit(
                runner=_noop_runner, deliver=deliver_to(sink), umo=f"{UMO}:{i}",
                tool="browse_web", task_text=f"任务{i}",
            )
            for i in range(3)
        ]
        assert [h.decision.kind for h in handles[:2]] == ["accepted", "accepted"]
        assert handles[2].decision.kind == "busy"
        assert handles[2].task is None
        assert "并发上限（2）" in handles[2].reply_text
        assert handles[2].reply_text.startswith(REJECTED_HEADER)
        await settle(*[h.task for h in handles], *[h.guard_task for h in handles])

    asyncio.run(main())


def test_dedupe_same_key_running_returns_accepted():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()
        first = wd.submit(
            runner=_noop_runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="同一道题",
        )
        dup = wd.submit(
            runner=_noop_runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="  同一道题 ",
        )
        assert dup.decision.kind == "duplicate_running"
        assert dup.reply_text.startswith(ACCEPTED_HEADER)
        assert "同一任务正在执行" in dup.reply_text
        assert dup.decision.duplicate_of == first.task_id
        assert len(wd.registry.running()) == 1  # 不登记第二个任务
        await settle(first.task, first.guard_task)

    asyncio.run(main())


def test_dedupe_completed_within_window_returns_duplicate_with_body():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, dedupe_window_s=600)

        async def runner(progress):
            return "已经做完了的结论"

        first = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="同一道题",
        )
        await settle(first.task, first.guard_task)
        await clock.advance(30)  # 窗口内
        dup = wd.submit(
            runner=_noop_runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="同一道题",
        )
        assert dup.decision.kind == "duplicate_done"
        assert dup.task is None
        assert dup.reply_text.startswith(DUPLICATE_HEADER)
        assert "已经做完了的结论" in dup.reply_text  # 让模型拿到内容
        assert first.task_id in dup.reply_text
        assert "只投递一次" in dup.reply_text

    asyncio.run(main())


def test_dedupe_window_boundary_then_expires():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, dedupe_window_s=600)

        async def runner(progress):
            return "结论"

        first = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="边界题",
        )
        await settle(first.task, first.guard_task)

        await clock.advance(600)  # 恰好落在窗口边界（含边界）
        at_edge = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="边界题",
        )
        assert at_edge.decision.kind == "duplicate_done"

        await clock.advance(1)  # 窗口外
        expired = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="边界题",
        )
        assert expired.decision.kind == "accepted"
        assert expired.task_id != first.task_id
        await settle(expired.task, expired.guard_task)

    asyncio.run(main())


def test_dedupe_window_zero_disables_completed_dedupe():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, dedupe_window_s=0)

        async def runner(progress):
            return "结论"

        first = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="关掉去重窗",
        )
        await settle(first.task, first.guard_task)
        again = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="关掉去重窗",
        )
        assert again.decision.kind == "accepted"
        await settle(again.task, again.guard_task)

    asyncio.run(main())


def test_registry_submit_returns_busy_text_with_configured_limit():
    """并发闸文案必须带**配置值**（不是写死的 4），且不占 task_id。"""

    async def main():
        registry = BackgroundRegistry(max_parallel=1)
        job = BackgroundJob(
            task_id=registry.next_task_id(), umo=UMO, tool="browse_web",
            started_at=registry.clock(), task_key=task_key(UMO, "browse_web", "x"),
        )
        assert registry.submit(job).kind == "accepted"
        other = BackgroundJob(
            task_id=registry.next_task_id(), umo=UMO2, tool="browse_web",
            started_at=registry.clock(), task_key=task_key(UMO2, "browse_web", "y"),
        )
        decision = registry.submit(other)
        assert decision.kind == "busy"
        assert "并发上限（1）" in decision.text
        assert other.task_id not in registry  # 被拒的任务不登记
        assert f"并发上限（{DEFAULT_MAX_PARALLEL}）" in _BG_BUSY_TEXT  # 兼容常量仍是 4

    asyncio.run(main())


# ---------------------------------------------------------------
# D. U1 预算看门狗：到点必有一条终态；提前完成取消定时器
# ---------------------------------------------------------------


def test_watchdog_deadline_cancels_hanging_runner_and_delivers_once():
    """假时钟走到 deadline：执行体被取消 → 一条【后台任务超时】，且内层任务真被取消。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=5)
        sink = Sink()
        hang = asyncio.Event()

        async def runner(progress):
            progress["last_observation"] = "已经看到的半成品"
            await hang.wait()
            return "不该到这里"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="跑到超时",
        )
        await asyncio.sleep(0)  # 让执行体起身、看门狗就位
        await clock.advance(5)  # 走到 deadline：看门狗取消执行体
        await clock.advance(5)  # 走到 deadline + GRACE：看门狗收尾
        await settle(handle.task, handle.guard_task)
        assert handle.task.cancelled() is True  # 取消没被吞
        assert handle.outcome.status == STATUS_TIMEOUT
        assert sink.count == 1  # 恰好一条终态
        assert sink.first_line.startswith(HEADERS[STATUS_TIMEOUT])
        assert PARTIAL_NOTE in sink.messages[0]
        assert "已经看到的半成品" in sink.messages[0]
        assert clock.now() >= handle.job.deadline

    asyncio.run(main())


def test_watchdog_second_phase_fires_when_runner_ignores_cancel():
    """执行体吞掉第一次取消 ⇒ 看门狗第二层补投，仍是一条终态。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=5, grace=5)
        sink = Sink()
        swallowed = {"n": 0}

        async def stubborn(progress):
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    swallowed["n"] += 1
                    if swallowed["n"] >= 2:
                        raise

        handle = wd.submit(
            runner=stubborn, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="赖着不走",
        )
        await asyncio.sleep(0)
        await clock.advance(5)  # deadline：第一次取消被吞
        await clock.advance(5)  # deadline + GRACE：看门狗第二层
        await settle(handle.task, handle.guard_task)
        assert swallowed["n"] >= 1
        assert sink.count == 1
        assert sink.first_line.startswith(HEADERS[STATUS_TIMEOUT])
        assert handle.outcome.status == STATUS_TIMEOUT

    asyncio.run(main())


def test_early_completion_cancels_watchdog_timer():
    """提前完成必须取消定时器：看门狗被取消、没有超时文案、仍只有一条终态。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=5, grace=5)
        sink = Sink()

        async def runner(progress):
            return "很快就做完了"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="提前完成",
        )
        await settle(handle.task, handle.guard_task)
        assert handle.guard_task.cancelled() is True
        assert handle.guard_task.done() is True
        assert handle.finalized is True
        assert handle.outcome.status == STATUS_OK
        assert sink.count == 1
        assert sink.first_line.startswith(HEADERS[STATUS_OK])
        assert "很快就做完了" in sink.messages[0]
        assert clock.now() < handle.job.deadline  # 计时器没有白等到 deadline

    asyncio.run(main())


def test_wait_for_layer_timeout_with_real_clock():
    """层①（``asyncio.wait_for``）单独可触发：真实时钟、极小预算也必须收尾。"""

    async def main():
        wd = BudgetWatchdog(
            budget_s=1, grace=0.2, poll=0.01, sleep=asyncio.sleep
        )
        sink = Sink()
        hang = asyncio.Event()

        async def runner(progress):
            await hang.wait()

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="真时钟超时", budget_s=0.05,
        )
        await settle(handle.task, handle.guard_task)
        assert handle.outcome.status == STATUS_TIMEOUT
        assert sink.count == 1
        assert sink.first_line.startswith(HEADERS[STATUS_TIMEOUT])

    asyncio.run(main())


def test_failure_maps_to_failed_terminal():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()

        async def runner(progress):
            raise RuntimeError("页面炸了")

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="会失败",
        )
        await settle(handle.task, handle.guard_task)
        assert handle.outcome.status == STATUS_FAILED
        assert "RuntimeError" in sink.messages[0]
        assert "页面炸了" in sink.messages[0]
        assert sink.first_line.startswith(HEADERS[STATUS_FAILED])
        assert sink.count == 1

    asyncio.run(main())


def test_empty_result_gets_template_fallback():
    """U2 反向缺口：结果为空也要一条模板兜底终态（不是「什么都没发」）。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()

        async def runner(progress):
            return ""

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="空结果",
        )
        await settle(handle.task, handle.guard_task)
        assert handle.outcome.status == STATUS_OK
        assert sink.count == 1
        assert EMPTY_RESULT_NOTE in sink.messages[0]

    asyncio.run(main())


def test_progress_view_has_elapsed_budget_remaining():
    """U1 二次证据：进度返回体必须带 elapsed_s / budget_s / remaining_s。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=60)
        gate = asyncio.Event()

        async def runner(progress):
            await gate.wait()
            return "ok"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="进度题",
        )
        await asyncio.sleep(0)
        view = wd.progress(handle.task_id)
        assert view["elapsed_s"] == 0.0
        assert view["budget_s"] == 60.0
        assert view["remaining_s"] == 60.0
        assert view["state"] == STATUS_RUNNING
        await clock.advance(15)  # 时间只由用例推进
        later = wd.progress(handle.task_id)
        assert later["elapsed_s"] == 15.0
        assert later["remaining_s"] == 45.0
        gate.set()
        await settle(handle.task, handle.guard_task)

    asyncio.run(main())


def test_terminal_outcome_and_text_carry_diagnostics():
    """终态也带三件套：outcome 字段 + 渲染文本的「诊断」行。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=30)
        sink = Sink()

        async def runner(progress):
            await clock.advance(10)
            return "做完"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="诊断题",
        )
        await settle(handle.task, handle.guard_task)
        outcome = handle.outcome
        assert outcome.elapsed_s == 10.0
        assert outcome.budget_s == 30.0
        assert outcome.remaining_s == 20.0
        assert "（诊断：已用 10s / 预算 30s / 剩余 20s）" in outcome.text

    asyncio.run(main())


def test_progress_of_unknown_task_is_field_complete():
    registry = BackgroundRegistry()
    view = registry.progress("bg-9999")
    for key in ("task_id", "tool", "state", "elapsed_s", "budget_s", "remaining_s"):
        assert key in view
    assert view["state"] == "unknown"


# ---------------------------------------------------------------
# E. U2 投递闸：每 task 一次主动投递、一次终态投递
# ---------------------------------------------------------------


def test_note_delivery_allows_only_once(caplog):
    registry = BackgroundRegistry()
    job = BackgroundJob(task_id="bg-0001", umo=UMO, tool="browse_web")
    registry.register(job)
    assert registry.note_delivery("bg-0001") is True
    with caplog.at_level("WARNING"):
        assert registry.note_delivery("bg-0001") is False
    assert job.delivered == 1
    assert "重复投递被拦截" in caplog.text


def test_terminal_channel_is_independent_from_tool_channel():
    registry = BackgroundRegistry()
    job = BackgroundJob(task_id="bg-0002", umo=UMO, tool="browse_web")
    registry.register(job)
    assert registry.note_delivery("bg-0002") is True  # 工具侧那一次
    assert registry.note_delivery("bg-0002", channel="terminal") is True
    assert registry.note_delivery("bg-0002", channel="terminal") is False
    assert job.delivered == 1 and job.terminal_sent == 1


def test_deliver_only_once_even_if_finalize_called_twice():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()

        async def runner(progress):
            return "一次就够"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="重复收尾",
        )
        await settle(handle.task, handle.guard_task)
        await wd._finalize(handle, STATUS_OK, "手工第二次", "", "", deliver_to(sink))
        assert sink.count == 1
        assert handle.finalized is True
        assert handle.outcome is not None
        # v1.7.6：终态额度由**投递回调自己取**（``main._deliver_terminal`` 调
        # ``note_delivery(channel="terminal")``）。这里的替身回调不走闸，所以
        # ``terminal_sent`` 保持 0 —— 「恰好一条」的闸在
        # ``tests/test_v176_delivery_parity.py`` 里用真投递入口钉住。
        assert handle.job.terminal_sent == 0

    asyncio.run(main())


def test_delivery_failure_is_recorded_once_and_not_retried():
    """投递抛错：只试一次、不重发，且**不**冒充发送成功（v1.7.6 记账拆分）。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        attempts = {"n": 0}

        async def bad_deliver(job, status, text):
            attempts["n"] += 1
            raise RuntimeError("平台对象已销毁")

        async def runner(progress):
            return "结果"

        handle = wd.submit(
            runner=runner, deliver=bad_deliver, umo=UMO,
            tool="browse_web", task_text="投递失败",
        )
        await settle(handle.task, handle.guard_task)
        assert attempts["n"] == 1, "失败不重试（重试 = 多发）"
        outcome = handle.outcome
        assert outcome.attempted is True
        assert outcome.succeeded is False and outcome.accepted_by_platform is False
        assert outcome.error == "DeliveryError:RuntimeError"
        assert outcome.sent_via == "none"
        assert outcome.delivered is False, "这个替身回调不走闸 ⇒ 额度没被消耗"
        assert handle.job.terminal_sent == 0

    asyncio.run(main())


def test_unreported_delivery_is_not_counted_as_success():
    """旧回调只返回 ``None``：记 ``DeliveryUnreported``，绝不当成"平台已接收"。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()

        async def runner(progress):
            return "结果"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="没回报",
        )
        await settle(handle.task, handle.guard_task)
        assert sink.count == 1
        outcome = handle.outcome
        assert outcome.error == ERROR_DELIVERY_UNREPORTED
        assert outcome.accepted_by_platform is False
        assert outcome.attempted is True

    asyncio.run(main())


# ---------------------------------------------------------------
# F. 取消与终止：不吞 CancelledError、不遗弃协程
# ---------------------------------------------------------------


def test_cancel_single_task_marks_cancelled_and_notifies():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()
        hang = asyncio.Event()

        async def runner(progress):
            await hang.wait()

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="被用户取消",
        )
        await clock.advance(3)
        assert await wd.cancel(handle.task_id, reason="user") is True
        await settle(handle.task, handle.guard_task)
        assert handle.outcome.status == STATUS_CANCELLED
        assert sink.count == 1
        assert sink.first_line.startswith(HEADERS[STATUS_CANCELLED])
        assert await wd.cancel("bg-9999") is False  # 未知/已终态返回 False

    asyncio.run(main())


def test_cancelled_error_is_not_swallowed():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        hang = asyncio.Event()
        seen = []

        async def runner(progress):
            try:
                await hang.wait()
            except asyncio.CancelledError:
                seen.append("runner 收到了取消")
                raise

        handle = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="取消传播",
        )
        await asyncio.sleep(0)
        await wd.cancel(handle.task_id, reason="user")
        assert seen == ["runner 收到了取消"]
        assert handle.task.cancelled() is True  # 向上传播，没被吞成正常返回
        await settle(handle.guard_task)

    asyncio.run(main())


def test_shutdown_cancels_all_running():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        sink = Sink()
        hang = asyncio.Event()

        async def runner(progress):
            await hang.wait()

        handles = [
            wd.submit(
                runner=runner, deliver=deliver_to(sink), umo=f"{UMO}:{i}",
                tool="browse_web", task_text=f"批量{i}",
            )
            for i in range(3)
        ]
        count = await wd.shutdown(timeout=1.0)
        assert count == 3
        await settle(*[h.task for h in handles], *[h.guard_task for h in handles])
        assert all(h.task.done() for h in handles)  # 一个都不遗弃
        assert wd.registry.running() == []
        assert sink.count == 3  # 每个任务恰好一条取消终态

    asyncio.run(main())


def test_registry_cancel_all_clears_table():
    async def main():
        registry = BackgroundRegistry()
        hang = asyncio.Event()

        async def runner(progress):
            await hang.wait()

        tasks = []
        for i in range(2):
            job = BackgroundJob(task_id=registry.next_task_id(), umo=f"{UMO}:{i}",
                                tool="browse_web")
            registry.register(job)
            task = asyncio.create_task(runner(job.progress))
            registry.attach_task(job.task_id, task)
            tasks.append(task)
        assert len(registry) == 2
        assert await registry.cancel_all(timeout=1.0) == 2
        await settle(*tasks)
        assert len(registry) == 0
        assert registry.running() == []

    asyncio.run(main())


def test_no_unretrieved_exception_on_failure_or_cancel():
    """D9 回归：失败与取消都不留「Future exception was never retrieved」。"""

    async def main():
        loop = asyncio.get_running_loop()
        seen: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
        clock = FakeClock()
        wd = watchdog(clock)

        async def boom(progress):
            raise ValueError("连接被对端关闭")

        failed = wd.submit(
            runner=boom, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="会抛错",
        )
        await settle(failed.task, failed.guard_task)
        hang = asyncio.Event()
        cancelled = wd.submit(
            runner=lambda progress, _e=hang: _e.wait(), deliver=deliver_to(Sink()),
            umo=UMO2, tool="browse_web", task_text="会被取消",
        )
        await asyncio.sleep(0)
        await wd.cancel(cancelled.task_id, reason="shutdown")
        await settle(cancelled.task, cancelled.guard_task)
        await asyncio.sleep(0)
        assert seen == []

    asyncio.run(main())


# ---------------------------------------------------------------
# G. U3/U4：后台链不持有上下文，终态带归属
# ---------------------------------------------------------------


def test_terminal_first_line_carries_task_id_tool_elapsed():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=30)
        sink = Sink()

        async def runner(progress):
            await clock.advance(2)
            return "正文"

        handle = wd.submit(
            runner=runner, deliver=deliver_to(sink), umo=UMO,
            tool="browse_web", task_text="归属题",
        )
        await settle(handle.task, handle.guard_task)
        head = sink.first_line
        assert handle.task_id in head
        assert "browse_web" in head
        assert "耗时 2.0s" in head
        assert head.startswith(HEADERS[STATUS_OK])

    asyncio.run(main())


def test_runner_receives_only_progress_dict():
    """执行体只拿到一个可写的 progress dict：没有 event、没有历史、没有工具集。"""
    calls: list[tuple] = []

    async def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return "ok"

    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        handle = wd.submit(
            runner=runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="入参形状",
        )
        await settle(handle.task, handle.guard_task)
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert kwargs == {}
        assert len(args) == 1 and isinstance(args[0], dict)
        assert args[0] is handle.job.progress

    asyncio.run(main())


def test_progress_view_has_no_chat_text():
    """任务表对外只有摘要：结果正文与任务文本不进进度/快照。"""
    job = BackgroundJob(task_id="bg-0003", umo=UMO, tool="browse_web")
    job.meta["result_text"] = "这里是聊天正文，不许外泄"
    view = job.progress_view()
    blob = json.dumps(view, ensure_ascii=False)
    assert "这里是聊天正文，不许外泄" not in blob
    assert "result_text" not in view
    assert view["umo_digest"] and UMO not in blob  # 会话号只留摘要
    registry = BackgroundRegistry()
    registry.register(job)
    registry.set_result("bg-0003", "正文")
    assert job.result_digest and "正文" not in json.dumps(job.progress_view(), ensure_ascii=False)


# ---------------------------------------------------------------
# H. 兼容面：v1.5.0 的调用点与逐字文案
# ---------------------------------------------------------------


def test_legacy_registry_api_still_works():
    async def main():
        registry = BackgroundRegistry()
        assert registry.next_task_id() == "bg-0001"
        job = BackgroundJob(
            task_id="bg-0001", umo=UMO, tool="browse_web",
            started_at=registry.clock(), budget_s=240, max_chars=1500,
        )
        registry.register(job)
        assert len(registry) == 1
        assert registry.active_job(UMO) is job
        assert registry.active_task_id(UMO) == "bg-0001"
        assert [j.task_id for j in registry.running()] == ["bg-0001"]
        assert registry.task_of("bg-0001") is None  # 还没 attach
        task = asyncio.create_task(_noop_runner({}))
        registry.attach_task("bg-0001", task)
        assert registry.task_of("bg-0001") is task
        await task
        assert registry.active_job(UMO) is None  # 任务结束 ⇒ 会话不再被占
        registry.finish("bg-0001")
        assert len(registry) == 0
        registry.clear()

    asyncio.run(main())


def test_legacy_finish_without_status_does_not_archive():
    registry = BackgroundRegistry()
    job = BackgroundJob(task_id="bg-0007", umo=UMO, tool="browse_web")
    registry.register(job)
    registry.finish("bg-0007")  # 旧调用点：只出表，不给状态
    assert registry.get("bg-0007") is None
    assert registry.progress("bg-0007")["state"] == "unknown"


def test_accepted_text_says_no_result_yet():
    text = render_accepted_text(task_id="bg-0001", tool="browse_web", budget_s=360)
    assert text.startswith(ACCEPTED_HEADER)
    assert "还没有**任何结果" in text
    assert "整任务硬预算 360 秒" in text
    for status in (STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED):
        assert HEADERS[status] in text  # 四态前缀在受理时就说清楚


def test_rejected_text_is_verbatim_and_mentions_running_task():
    text = render_rejected_text(task_id="bg-0002", tool="browse_web", elapsed_s=12.7)
    assert text.startswith(REJECTED_HEADER)
    assert "已有一个后台任务在跑（task_id=bg-0002，工具=browse_web，已运行 13 秒）" in text
    assert "不要再提交、不要换参数重试" in text


def test_duplicate_texts_headers_and_body():
    running = render_duplicate_text(task_id="bg-0003", tool="browse_web", elapsed_s=0, running=True)
    assert running.startswith(ACCEPTED_HEADER) and "同一任务正在执行" in running
    done = render_duplicate_text(
        task_id="bg-0003", tool="browse_web", elapsed_s=42, body="结论正文"
    )
    assert done.startswith(DUPLICATE_HEADER)
    assert "42s 前已完成" in done and "结论正文" in done
    empty = render_duplicate_text(task_id="bg-0003", tool="browse_web", elapsed_s=1, body="")
    assert EMPTY_RESULT_NOTE in empty


def test_truncate_result_and_shorten():
    body, cut, omitted = truncate_result("x" * 30, 10)
    assert cut and omitted == 20 and body.startswith("x" * 10)
    assert "已省略 20 字" in body
    raw, cut, omitted = truncate_result("short", 100)
    assert (raw, cut, omitted) == ("short", False, 0)
    assert truncate_result("x" * 5, 0)[0] == "x" * 5  # limit<=0 不截断
    assert shorten("a\n b\tc", 100) == "a b c"
    assert shorten("abcdef", 3) == "abc…"


def test_terminal_render_keeps_legacy_verbatim_and_optional_diagnostics():
    legacy = render_terminal_text(
        task_id="bg-0004", tool="browse_web", status=STATUS_TIMEOUT,
        elapsed_ms=1000, budget_s=240,
    )
    assert legacy.splitlines()[1] == "已用满 240 秒硬预算，任务被中断。"
    assert "本次没有可用结果。" in legacy
    assert "诊断" not in legacy  # 旧调用点不传 diagnostics ⇒ 逐字不变
    with_diag = render_terminal_text(
        task_id="bg-0004", tool="browse_web", status=STATUS_TIMEOUT,
        elapsed_ms=1000, budget_s=240, diagnostics=True,
    )
    assert "（诊断：已用 1s / 预算 240s / 剩余 239s）" in with_diag


def test_c_media_block_compat_shell():
    from core.background import C_MEDIA_MAX_LIST, C_MEDIA_UNPUBLISHED, render_c_media_block

    block = render_c_media_block([f"/tmp/{i}.png" for i in range(C_MEDIA_MAX_LIST + 2)],
                                unpublished=True)
    assert "【媒体文件】" in block
    assert C_MEDIA_UNPUBLISHED in block
    assert "（另有 2 个媒体文件未列出）" in block


# ---------------------------------------------------------------
# I. 并发：同会话多任务不串号
# ---------------------------------------------------------------


def test_concurrent_tasks_different_umo_do_not_cross():
    async def main():
        clock = FakeClock()
        wd = watchdog(clock, budget_s=60)
        first_sink, second_sink = Sink(), Sink()
        gates = {"a": asyncio.Event(), "b": asyncio.Event()}

        def make(tag, text):
            async def runner(progress):
                progress["partial"] = text
                await gates[tag].wait()
                return text

            return runner

        a = wd.submit(
            runner=make("a", "甲的结论"), deliver=deliver_to(first_sink),
            umo=UMO, tool="browse_web", task_text="甲",
        )
        b = wd.submit(
            runner=make("b", "乙的结论"), deliver=deliver_to(second_sink),
            umo=UMO2, tool="media_analyze", task_text="乙",
        )
        assert a.task_id != b.task_id
        await asyncio.sleep(0)
        assert wd.progress(a.task_id)["tool"] == "browse_web"
        assert wd.progress(b.task_id)["tool"] == "media_analyze"
        gates["b"].set()
        await settle(b.task, b.guard_task)
        gates["a"].set()
        await settle(a.task, a.guard_task)
        assert "甲的结论" in first_sink.messages[0]
        assert "乙的结论" not in first_sink.messages[0]  # 不串号
        assert "乙的结论" in second_sink.messages[0]
        assert first_sink.count == second_sink.count == 1

    asyncio.run(main())


def test_two_jobs_same_umo_never_both_run():
    """单飞是硬约束：同 UMO 第二个任务根本起不来（连 task 都不创建）。"""

    async def main():
        clock = FakeClock()
        wd = watchdog(clock)
        first = wd.submit(
            runner=_noop_runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="browse_web", task_text="甲",
        )
        second = wd.submit(
            runner=_noop_runner, deliver=deliver_to(Sink()), umo=UMO,
            tool="media_analyze", task_text="乙",
        )
        assert second.task is None
        assert wd.registry.active_job(UMO).task_id == first.task_id
        await settle(first.task, first.guard_task)

    asyncio.run(main())
