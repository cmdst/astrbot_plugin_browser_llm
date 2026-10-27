"""v1.7.6 投递接线守卫（``main._deliver_terminal`` → ``core.active_send``，契据 §3–§7）。

这份文件钉的是**接线**（适配器本身的行为在 ``test_v176_active_send.py``）：

1. 终态**先取额度**（``note_delivery(channel="terminal")``）→ 重放装饰钩子 →
   走 ``Context.send_message`` → 条件重放 after-sent → 把 ``DeliveryOutcome`` 交回
   ``core.background`` 记进 ``TerminalOutcome``；
2. 三种失败各有执行级用例：**发送抛错** / **平台不存在**（走 ``event.send`` 回退）/
   **装饰方压制**（``attempted=False``，一条都不发）；
3. 「恰好一条」不变量：一次任务只发一条、``_finalize`` 第二次是空操作、
   投递失败**不重试**；
4. 触发消息 ID 去重（spec §7.4）：同 ID 换措辞也拦、无 ID 退回 600s 指纹窗口、
   在途任务不受窗口限制、不同会话互不影响；
5. 事件生命周期纪律（spec §6 C1/C3）：投递回调里**没有** ``event``（AST 判据 +
   执行判据），平台回退句柄只被用一次。

假 ``Context`` / 假 ``active_send.Framework`` 全部注入，不联网、不打真适配器；
``_emit_media`` 与媒体附件那部分在 ``test_v176_media_delivery.py``（F-G4）。
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.background as bg  # noqa: E402
import core.active_send as active_send  # noqa: E402
import main as main_module  # noqa: E402

import v176_fakes as v176  # noqa: E402 — 共用假件（R14：假框架只在 tests/v176_fakes.py 一份）
from v176_fakes import (  # noqa: E402
    UMO,
    FakeContext,
    FakeHandler as _Handler,
    FakePlain as _FakePlain,
    FakeRegistry,
    FakeResult as _FakeResult,
    RealEventStub,
    deliver_once,
    install_framework,
    make_job,
    make_plugin,
    wait_handle as _wait_handle,
)


# ---------------------------------------------------------------------------
# 1. 投递走适配器：钩子 → Context.send_message → after-sent → 记账
# ---------------------------------------------------------------------------


def test_terminal_delivery_uses_context_send_message_with_hooks(tmp_path, monkeypatch):
    order = []

    async def decorate(event):
        order.append("decorate")
        event.get_result().chain = [_FakePlain("装饰后的终态")]

    async def after(event):
        order.append("after")

    registry = FakeRegistry(
        decorating=[_Handler("decorate", decorate)], after_sent=[_Handler("after", after)]
    )
    install_framework(monkeypatch, registry)
    context = FakeContext(on_send=lambda: order.append("send"))
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()

    receipt = deliver_once(plugin, event, job)

    assert order == ["decorate", "send", "after"], "装饰钩子 → L1 发送 → after-sent（§5.2/§5.4）"
    assert len(context.send_calls) == 1
    umo, chain = context.send_calls[0]
    assert umo == UMO
    assert [getattr(c, "text", "") for c in chain] == ["装饰后的终态"], "要发改写后的链"
    assert event.send_calls == 0, "平台可用时不得走 event.send 回退"
    assert receipt.accepted_by_platform is True and receipt.succeeded is True
    assert receipt.sent_via == active_send.CHANNEL_CONTEXT and receipt.error == ""
    assert job.terminal_sent == 1, "闸必须被消耗一次"


def test_terminal_gate_blocks_second_delivery(tmp_path, monkeypatch):
    registry = FakeRegistry()
    install_framework(monkeypatch, registry)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()

    first = deliver_once(plugin, event, job)
    hook_calls_after_first = len(registry.calls)
    second = deliver_once(plugin, event, job)

    assert first.accepted_by_platform is True
    assert second.attempted is False and second.error == "DeliveryBlocked"
    assert second.sent_via == active_send.CHANNEL_NONE
    assert len(context.send_calls) == 1, "额度用尽后一条都不许再发"
    assert len(registry.calls) == hook_calls_after_first, "额度用尽后不许再重放钩子"


# ---------------------------------------------------------------------------
# 2. 三种失败：发送抛错 / 平台不存在 / 装饰压制
# ---------------------------------------------------------------------------


def test_send_error_is_recorded_not_retried(tmp_path, monkeypatch):
    """两条腿都抛错：记首错、只各试一次（重试 = 多发）。"""
    install_framework(monkeypatch)
    context = FakeContext(send_error=RuntimeError("adapter down"))
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub(send_error=RuntimeError("event gone"))
    job = make_job()

    receipt = deliver_once(plugin, event, job)

    assert receipt.attempted is True and receipt.accepted_by_platform is False
    assert receipt.error == "PlatformSendError:RuntimeError", "保留 L1 的错（诊断价值最高）"
    assert receipt.sent_via == active_send.CHANNEL_FALLBACK, "最后试的是回退腿"
    assert len(context.send_calls) == 1 and event.send_calls == 1, "每条腿各一次，不重试"


def test_platform_missing_falls_back_to_event_send(tmp_path, monkeypatch):
    """R5：平台缺失是**原因**、回退救回是**结果** —— 账上把两者分开记。

    ``error`` 只表示"通道报告了失败"：回退腿成功 ⇒ ``error=""``；
    L1 腿为什么没用上记在 ``notes``（``Recovered:PlatformNotFound``），WARN 仍照打
    （不静默）。spec §4.2 该行按本实现改写（spec 勘误 r1）。
    """
    install_framework(monkeypatch)
    context = FakeContext(platform_insts=[])  # 平台实例一个都没有
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()
    log = v176.plugin_logger(monkeypatch)

    receipt = deliver_once(plugin, event, job)

    assert receipt.error == "", "回退救回 ⇒ 通道没报告失败"
    assert "Recovered:PlatformNotFound" in receipt.notes, "L1 腿没走的原因必须留痕"
    assert receipt.accepted_by_platform is True and receipt.dispatched is True
    assert receipt.sent_via == active_send.CHANNEL_FALLBACK
    assert context.send_calls == [], "平台找不到时不走 L1"
    assert event.send_calls == 1, "回退句柄必须用一次（不静默丢消息）"
    assert "【后台任务完成】" in str(event.sent[0])
    assert any("平台实例未找到" in text for text in log.texts("WARNING")), "不静默：平台缺失必须 WARN"
    assert any("已由平台回退救回" in text for text in log.texts("WARNING")), (
        "回退救回也要留痕（与「没发出去」区分）"
    )


def test_decorator_suppressed_records_attempted_false(tmp_path, monkeypatch):
    async def clear(event):
        event.set_result(_FakeResult([]))
        event.stop_event()

    registry = FakeRegistry(decorating=[_Handler("clear", clear)])
    install_framework(monkeypatch, registry)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()

    receipt = deliver_once(plugin, event, job)

    assert receipt.attempted is False
    assert receipt.accepted_by_platform is False
    assert receipt.error == active_send.ERROR_DECORATOR_SUPPRESSED
    assert receipt.sent_via == active_send.CHANNEL_NONE
    assert context.send_calls == [] and event.send_calls == 0


def test_webchat_delivery_marks_persisted_only_in_log(tmp_path, monkeypatch):
    """R6 / §4.2 webchat 行：无活跃流 ⇒ 消息**落库**，日志加只读标记，决策不变。"""
    install_framework(monkeypatch)
    context = FakeContext(platform_name="webchat", platform_id="webchat")
    plugin = make_plugin(tmp_path, context)
    webchat_event = RealEventStub(platform="webchat", umo="webchat:FriendMessage:room-1")

    log = v176.plugin_logger(monkeypatch)
    receipt = deliver_once(plugin, webchat_event, make_job())

    assert receipt.accepted_by_platform is True
    assert receipt.sent_via == active_send.CHANNEL_CONTEXT
    assert receipt.error == ""
    assert any("webchat_persisted=1" in text for text in log.texts("INFO")), (
        "webchat 的「落库」必须让 ops 看得见"
    )
    assert len(context.send_calls) == 1

    # 非 webchat 不打这个标记（只读判断，不改任何账字段）
    log.records.clear()
    install_framework(monkeypatch)
    plain_plugin = make_plugin(tmp_path, FakeContext())
    other = deliver_once(plain_plugin, RealEventStub(), make_job())
    assert other.sent_via == active_send.CHANNEL_CONTEXT
    assert not any("webchat_persisted=1" in text for text in log.texts("INFO"))


def test_send_message_true_is_not_delivery_proof(tmp_path, monkeypatch):
    """R7 / §4.1：``send_message`` 返回 True 只代表**找到匹配的平台**，不是"用户收到"。

    用户可见后果：``qq_official`` 群场景适配器可能静默跳过 —— 账上不许冒出任何
    "已送达/已读"语义字段，只能有 ``accepted_by_platform``（= 通道报告成功）。
    """

    class SkippingContext(FakeContext):
        async def send_message(self, umo, chain):
            self.send_calls.append((umo, list(getattr(chain, "chain", []) or [])))
            self.skip_next()  # 适配器内部记一次"跳过"（用户没收到）
            return True  # 但它仍然按核心语义返回"找到平台"

    install_framework(monkeypatch)
    context = SkippingContext()
    plugin = make_plugin(tmp_path, context)

    receipt = deliver_once(plugin, RealEventStub(), make_job())

    assert context.skipped == 1, "桩内部确实跳过了发送"
    assert receipt.accepted_by_platform is True, "账只记「通道报告成功」"
    assert receipt.sent_via == active_send.CHANNEL_CONTEXT
    field_names = {item.name for item in dataclasses.fields(receipt)}
    for forbidden in ("delivered", "delivered_to_user", "read", "acked", "user_seen"):
        assert forbidden not in field_names, f"账里不许有「已送达」语义字段 {forbidden}"
    # §4.1 的语义逐字钉住（docstring 是"给后来人看的合同"）
    module_doc = active_send.__doc__ or ""
    assert "是否找到匹配的平台" in module_doc, "§4.1 的核心 docstring 语义必须逐字写进模块文档"
    assert "不代表用户收到" in module_doc, "不许把这个 True 读成「已送达」"


def test_splitter_patch_and_hook_both_installed(tmp_path, monkeypatch):
    """R8 / §5.6：``splitter`` 补丁与装饰钩子**同装**时，终态仍然是恰好一条。"""

    async def keep_last_segment(event):
        result = event.get_result()
        if result is not None and result.chain:
            event.set_result(_FakeResult(result.chain[-1:]))

    registry = FakeRegistry(decorating=[_Handler("splitter_segments", keep_last_segment)])
    install_framework(monkeypatch, registry)
    context = FakeContext()
    context._splitter_send_patched = True  # 只读探测：补丁装着
    plugin = make_plugin(tmp_path, context)

    log = v176.plugin_logger(monkeypatch)
    receipt = deliver_once(plugin, RealEventStub(), make_job())

    assert len(context.send_calls) == 1, "补丁 + 钩子同装也只发一条"
    assert receipt.accepted_by_platform is True
    assert "splitter_patch=1" in receipt.notes
    assert any("splitter_patch=1" in text for text in log.texts("INFO")), (
        "标记必须进已投递 INFO 行（ops 一眼可见）"
    )


def test_at_tag_turns_into_component_only_via_decorating_hook(tmp_path, monkeypatch):
    """R8 / §5.7：``[at:1]`` 只有装饰钩子在场时才变成 ``At`` 组件；否则原样发文本。"""

    async def replace_at_tag(event):
        result = event.get_result()
        chain = list(result.chain) if result is not None else []
        rebuilt = []
        for component in chain:
            text = getattr(component, "text", "")
            if "[at:1]" in text:
                rebuilt.append(v176.FakeAt("10001", "tester"))
            else:
                rebuilt.append(component)
        event.set_result(_FakeResult(rebuilt))

    install_framework(monkeypatch)  # 没有装饰钩子
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    plain = deliver_once(plugin, RealEventStub(), make_job(), body="提醒 [at:1] 看这里")
    plain_chain = context.send_calls[-1][1]
    assert not any(getattr(item, "type", "") == "At" for item in plain_chain), (
        "没有装饰钩子 ⇒ [at:1] 只能是文本（插件不越权替 AtTool 干活）"
    )
    assert plain.accepted_by_platform is True

    registry = FakeRegistry(decorating=[_Handler("at_tags", replace_at_tag)])
    install_framework(monkeypatch, registry)
    context2 = FakeContext()
    plugin2 = make_plugin(tmp_path, context2)
    with_at = deliver_once(plugin2, RealEventStub(), make_job(), body="提醒 [at:1] 看这里")

    delivered = context2.send_calls[-1][1]
    assert any(getattr(item, "type", "") == "At" for item in delivered), (
        "装饰钩子把 [at:1] 换成 At ⇒ 后台终态与前台同权"
    )
    assert with_at.accepted_by_platform is True


# ---------------------------------------------------------------------------
# 3. TerminalOutcome 记账 + 恰好一条 + 不重试（走真 watchdog）
# ---------------------------------------------------------------------------


def _run_through_watchdog(plugin, event, *, runner=None):
    async def _main():
        async def default_runner(progress):
            return "后台结论"

        snapshot = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))
        handle = plugin._watchdog.submit(
            runner=runner or default_runner,
            deliver=plugin._terminal_deliverer(snapshot, event.send),
            umo=plugin._umo_of(event),
            tool=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="记账用例",
            budget_s=30.0,
            max_chars=1500,
            dedupe_window_s=0.0,
            max_parallel=2,
            trigger_message_id=plugin._trigger_message_id(event),
        )
        finished = await _wait_handle(handle)
        return handle, finished

    return asyncio.run(_main())


def test_finalize_records_delivery_receipt(tmp_path, monkeypatch):
    install_framework(monkeypatch)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)

    log = v176.plugin_logger(monkeypatch)
    handle, _ = _run_through_watchdog(plugin, RealEventStub())

    outcome = handle.outcome
    assert outcome is not None
    assert outcome.delivered is True, "闸放行过（恰好一条的额度已消耗）"
    assert outcome.attempted is True and outcome.succeeded is True
    assert outcome.accepted_by_platform is True and outcome.error == ""
    assert outcome.sent_via == active_send.CHANNEL_CONTEXT
    assert any("attempted=1 accepted=1 sent_via=context.send_message" in text for text in log.texts("INFO"))
    assert len(context.send_calls) == 1


def test_exactly_one_terminal_message_even_if_finalize_twice(tmp_path, monkeypatch):
    install_framework(monkeypatch)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()

    async def _main():
        async def runner(progress):
            return "一次就够"

        snapshot = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))
        handle = plugin._watchdog.submit(
            runner=runner,
            deliver=plugin._terminal_deliverer(snapshot, event.send),
            umo=plugin._umo_of(event),
            tool=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="恰好一条",
            budget_s=30.0,
            max_chars=1500,
            dedupe_window_s=0.0,
            max_parallel=2,
        )
        await _wait_handle(handle)
        await plugin._watchdog._finalize(
            handle,
            bg.STATUS_OK,
            "手工第二次",
            "",
            "",
            handle.deliver,
        )
        return handle

    handle = asyncio.run(_main())
    assert len(context.send_calls) == 1, "第二次收尾必须是空操作"
    assert handle.job.terminal_sent == 1


def test_delivery_failure_is_recorded_and_logged(tmp_path, monkeypatch):
    install_framework(monkeypatch)
    context = FakeContext(send_error=RuntimeError("adapter down"))
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub(send_error=RuntimeError("event gone"))

    log = v176.plugin_logger(monkeypatch)
    handle, _ = _run_through_watchdog(plugin, event)

    outcome = handle.outcome
    assert outcome.attempted is True and outcome.succeeded is False
    assert outcome.accepted_by_platform is False
    assert outcome.error == "PlatformSendError:RuntimeError"
    assert outcome.sent_via == active_send.CHANNEL_FALLBACK
    assert outcome.delivered is True, "闸放行过（额度已消耗）但发送没成功 —— 两件事分开记"
    failures = log.texts("WARNING")
    assert any("后台终态投递失败" in text for text in failures), (
        "§4.4 的失败面只在投递层落一条（后台层已降 DEBUG）"
    )
    assert any("PlatformSendError:RuntimeError" in text for text in failures)
    assert len(context.send_calls) == 1 and event.send_calls == 1, "不重试"


# ---------------------------------------------------------------------------
# 4. 触发消息 ID 去重（spec §7.4）
# ---------------------------------------------------------------------------


def _job_with(umo, tool, text, message_id, task_id):
    return bg.BackgroundJob(
        task_id=task_id,
        umo=umo,
        tool=tool,
        task_key=bg.task_key(umo, tool, text),
        trigger_key=bg.trigger_key(umo, tool, message_id),
    )


def test_same_message_id_blocks_retry_even_with_different_text():
    registry = bg.BackgroundRegistry()
    first = _job_with(UMO, "browse_web", "看看这个页面", "m-42", "bg-1")
    assert registry.submit(first).accepted

    second = _job_with(UMO, "browse_web", "换个说法再看看", "m-42", "bg-2")
    decision = registry.submit(second)

    assert decision.kind == bg.SUBMIT_DUPLICATE_RUNNING
    assert decision.trigger_hit is True and decision.duplicate_of == "bg-1"
    assert [job.task_id for job in registry.running()] == ["bg-1"], "第二次不得登记"


def test_missing_message_id_falls_back_to_task_key():
    registry = bg.BackgroundRegistry()
    first = _job_with(UMO, "browse_web", "同一个措辞", None, "bg-1")
    assert registry.submit(first).accepted
    assert first.trigger_key == "", "无 message_id ⇒ 没有 trigger 键"

    same = _job_with(UMO, "browse_web", " 同一个措辞 ", None, "bg-2")
    decision = registry.submit(same)
    assert decision.kind == bg.SUBMIT_DUPLICATE_RUNNING and decision.trigger_hit is False

    different = _job_with(UMO, "browse_web", "完全不同的措辞", None, "bg-3")
    assert registry.submit(different).kind == bg.SUBMIT_SINGLE_FLIGHT, (
        "措辞不同不命中内容指纹（此会话有在跑任务 ⇒ 单飞拒绝，而不是重复）"
    )


def test_trigger_dedupe_does_not_affect_other_sessions():
    registry = bg.BackgroundRegistry()
    first = _job_with(UMO, "browse_web", "任务甲", "m-42", "bg-1")
    other = _job_with("aiocqhttp-1:FriendMessage:888", "browse_web", "任务乙", "m-42", "bg-2")
    assert registry.submit(first).accepted
    assert registry.submit(other).accepted, "不同 umo 的同 ID 互不影响"


def test_trigger_dedupe_window_zero_disables_completed_branch():
    registry = bg.BackgroundRegistry(dedupe_window_s=0.0)
    first = _job_with(UMO, "browse_web", "任务甲", "m-42", "bg-1")
    assert registry.submit(first).accepted
    registry.finish("bg-1", bg.STATUS_OK)

    again = _job_with(UMO, "browse_web", "任务甲", "m-42", "bg-2")
    assert registry.submit(again).accepted, "窗口 0 ⇒ 只拦在跑，不拦刚完成"


def test_in_flight_task_not_blocked_by_zero_window():
    registry = bg.BackgroundRegistry(dedupe_window_s=0.0)
    first = _job_with(UMO, "browse_web", "任务甲", "m-42", "bg-1")
    assert registry.submit(first).accepted

    decision = registry.submit(_job_with(UMO, "browse_web", "别的措辞", "m-42", "bg-2"))
    assert decision.kind == bg.SUBMIT_DUPLICATE_RUNNING, "在途任务不受窗口限制"


def test_submit_background_uses_event_message_id(tmp_path, monkeypatch):
    """端到端：``_submit_background`` 把 ``event.message_obj.message_id`` 送进去重键。"""

    async def _main():
        plugin = make_plugin(tmp_path, FakeContext())
        event = RealEventStub(message_id="m-777")

        async def slow(progress):
            await asyncio.sleep(0.2)
            return "跑完了"

        first = await plugin._submit_background(
            event=event,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=slow,
            task_text="第一句",
        )
        second = await plugin._submit_background(
            event=event,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=slow,
            task_text="换了个措辞",
        )
        running = [job.task_id for job in plugin._watchdog.registry.running()]
        handle = plugin._watchdog.registry.get(running[0]) if running else None
        if handle is not None and handle.task is not None:
            await asyncio.wait({handle.task}, timeout=2.0)
        return first, second, running

    first, second, running = asyncio.run(_main())
    assert "已受理" in first
    assert "同一任务正在执行" in second or "重复" in second, second
    assert len(running) == 1, "同 ID 第二次提交不得登记新任务"


# ---------------------------------------------------------------------------
# 5. 生命周期纪律：投递回调里没有 event（AST + 执行判据）
# ---------------------------------------------------------------------------


def _function_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"main.py 里没有 {name}")


def test_deliver_terminal_body_never_touches_event():
    """AST 判据（spec §6.4）：``_deliver_terminal`` 里不得出现 ``event`` 这个名字。"""
    node = _function_node("_deliver_terminal")
    used = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "event":
            used.add(child.lineno)
        if isinstance(child, ast.Attribute) and child.attr == "event":
            used.add(child.lineno)
    assert not used, f"_deliver_terminal 仍在引用 event（行 {sorted(used)}）"
    arg_names = {arg.arg for arg in node.args.args + node.args.kwonlyargs}
    assert "event" not in arg_names, "投递入口不得接收 event（只收快照与回退句柄）"


def test_terminal_deliverer_captures_snapshot_and_fallback_only(tmp_path, monkeypatch):
    install_framework(monkeypatch)
    plugin = make_plugin(tmp_path, FakeContext())
    event = RealEventStub()
    snap = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))

    deliver = plugin._terminal_deliverer(snap, event.send)

    assert deliver.func == plugin._deliver_terminal
    assert deliver.args == ()
    assert set(deliver.keywords) == {"snapshot", "fallback_send"}, (
        "回调里只能有快照与回退句柄（§6 C1/C3）"
    )
    assert isinstance(deliver.keywords["snapshot"], active_send.DeliverySnapshot)
    assert deliver.keywords["fallback_send"] == event.send
    assert deliver.keywords["snapshot"].umo == UMO
    assert deliver.keywords["snapshot"].trigger_message_id == "m-1"


def test_probe_event_capabilities_are_not_touched_by_delivery(tmp_path, monkeypatch):
    """执行判据：快照抓完后把原事件的所有属性换成探针，投递照样跑完（不碰事件）。"""

    install_framework(monkeypatch)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()
    snapshot = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))
    fallback = event.send

    class _ProbeEvent:
        def __getattr__(self, name):  # pragma: no cover — 命中即失败
            raise AssertionError(f"投递层碰了原事件的 {name!r}")

        async def send(self, message):  # pragma: no cover — 平台可用，不该走回退
            raise AssertionError("平台可用时不该触发平台回退")

    probe = _ProbeEvent()
    assert probe is not event
    plugin._watchdog.registry.register(job)

    receipt = asyncio.run(
        plugin._deliver_terminal(job, "ok", "正文", snapshot=snapshot, fallback_send=fallback)
    )

    assert receipt.accepted_by_platform is True
    assert len(context.send_calls) == 1
