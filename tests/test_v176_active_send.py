"""v1.7.6 投递适配层执行级守卫（``core/active_send.py``，契据 §3–§6）。

钉七件事，全部**执行级**（真跑 :func:`core.active_send.deliver` 与
:func:`replay_hooks`，断言终态字段 / 发送次数 / 链内容 / 日志级别）：

1. 四类装饰形态 A/B/C/D（外加 B′、A′ 的可观测等价面）各自的 attempted /
   accepted_by_platform / error / sent_via 与"发几次"；
2. 钩子顺序：``on_decorating_result`` 先跑 → 发送 → ``on_after_message_sent`` 后跑；
   ``stop_event`` 立即停止后续装饰钩子；压制形态不重放 after-sent（§5.4）；
3. ``plugins_name`` 过滤交给 registry（``None`` / ``["*"]`` 全放行），我们不再过滤一遍；
4. 单钩子异常不中断、单钩子超时与整段超时都只记 WARN 并继续投递；
5. 平台闸：平台实例找不到 / qq_official 群场景提示 / L1 失败转回退 / umo 不可解析，
   每条都返回明确 error 且 WARN 可见（绝不静默）；
6. 记账不变量（``fold_outcome``）与"日志不带正文"；
7. 导入纪律（模块顶层不 import astrbot）+ **真 astrbot 类型**（子进程只读导入
   ``/root/AstrBot``）下的合成事件字段与端到端投递。

测试注入 ``Framework`` 替身（假事件 / 假 result / 假链）与假 registry / 假 context：
不联网、不 import 真 ``astrbot``、不依赖 ``tests/conftest.py`` 的桩之外的任何东西。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.active_send as active_send  # noqa: E402


# ---- 共用假件（R14：假框架/假 Context/假 registry/假日志只在 tests/v176_fakes.py 一份）----
import v176_fakes as v176  # noqa: E402

_FakeContentType = v176.FakeContentType
_FakePlatformMeta = v176.FakePlatformMeta
_FakePlatformInst = v176.FakePlatformInst
_FakeChain = v176.FakeChain
_FakePlain = v176.FakePlain
_FakeMessage = v176.FakeMessage
_FakeGroup = v176.FakeGroup
_FakeMember = v176.FakeMember
_FakeSession = v176.FakeSession
_FakeResult = v176.FakeResult
_FakeEvent = v176.SyntheticEvent
_FakeAt = v176.FakeAt
_fake_framework = v176.make_framework
_FakeHandler = v176.FakeHandler
_FakeRegistry = v176.FakeRegistry
_RecordingLog = v176.RecordingLog
_FakeContext = v176.FakeContext


def _snapshot(**overrides):
    data = {
        "umo": "aiocqhttp-1:GroupMessage:777001",
        "platform_name": "aiocqhttp",
        "platform_id": "aiocqhttp-1",
        "message_type": "GroupMessage",
        "group_id": "777001",
        "sender_id": "10001",
        "sender_name": "tester",
        "self_id": "90000",
        "plugins_name": None,
        "trigger_message_id": "m-1",
        "trigger_text": "帮我看一下这个页面",
    }
    data.update(overrides)
    return active_send.DeliverySnapshot(**data)


def _context_for(snapshot, **overrides):
    return _FakeContext(
        platform_name=snapshot.platform_name or "aiocqhttp",
        platform_id=snapshot.platform_id or "aiocqhttp-1",
        **overrides,
    )


async def _deliver(context, registry, *, snapshot=None, chain=None, framework=None, **kwargs):
    snap = snapshot if snapshot is not None else _snapshot()
    return await active_send.deliver(
        context,
        snap,
        chain=chain if chain is not None else [_FakePlain("终态正文")],
        registry=registry,
        framework=framework or _fake_framework(registry),
        log=kwargs.pop("log", None) or _RecordingLog(),
        task_id=kwargs.pop("task_id", "tsk00001"),
        status=kwargs.pop("status", "ok"),
        **kwargs,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 纯函数：平台矩阵 / 形态判定 / 记账不变量
# ---------------------------------------------------------------------------


def test_choose_channel_matrix():
    l1 = active_send.choose_channel("aiocqhttp", "GroupMessage")
    assert (l1.primary, l1.fallback, l1.error, l1.hint) == (
        active_send.CHANNEL_CONTEXT,
        active_send.CHANNEL_FALLBACK,
        "",
        "",
    )
    qq_group = active_send.choose_channel("qq_official", "GroupMessage")
    assert qq_group.primary == active_send.CHANNEL_CONTEXT
    assert qq_group.hint == active_send.QQ_OFFICIAL_GROUP_HINT
    assert qq_group.error == "", "qq 官方系的提示是非权威的，不得当失败"
    assert active_send.choose_channel("qq_official", "FriendMessage").hint == ""
    missing = active_send.choose_channel("aiocqhttp", "GroupMessage", platform_found=False)
    assert missing.primary == active_send.CHANNEL_FALLBACK
    assert missing.fallback is None
    assert missing.error == active_send.ERROR_PLATFORM_NOT_FOUND


@pytest.mark.parametrize(
    ("chain", "stopped", "side_started", "side_err", "shape", "send", "attempted", "via", "error"),
    [
        ([1], False, False, "", active_send.SHAPE_A, True, True, "context.send_message", ""),
        ([1], False, True, "", active_send.SHAPE_A, True, True, "context.send_message", ""),
        ([1], True, True, "", active_send.SHAPE_B, False, True, "decorator", ""),
        ([1], True, True, "RuntimeError", active_send.SHAPE_B_PRIME, False, True, "decorator", "SideSend:"),
        ([], True, False, "", active_send.SHAPE_C, False, False, "none", "DecoratorSuppressed"),
        ([], False, False, "", active_send.SHAPE_D, False, False, "none", "DecoratorSuppressed"),
        # R1：stop 但没发、链还在 ⇒ 照发（部署核心零 yield 时 stop 不生效，RespondStage 照发）
        ([1], True, False, "", active_send.SHAPE_C_PRIME, True, True, "context.send_message", ""),
        # R2：side_started 优先 —— 已发过 + 链空 + 未 stop 也认 B（不许落 DecoratorSuppressed）
        ([], False, True, "", active_send.SHAPE_B, False, True, "decorator", ""),
    ],
)
def test_classify_shapes(chain, stopped, side_started, side_err, shape, send, attempted, via, error):
    decision = active_send.classify_after_decorators(
        chain, stopped=stopped, side_started=side_started, side_err=side_err
    )
    assert decision.shape == shape
    assert decision.should_send is send
    assert decision.attempted is attempted
    assert decision.sent_via == via
    assert decision.error.startswith(error)


def test_fold_outcome_enforces_invariants():
    suppressed = active_send.classify_after_decorators([], stopped=True, side_started=False)
    forced = active_send.fold_outcome(decision=suppressed, accepted=True, dispatched=True)
    assert forced.attempted is False and forced.accepted_by_platform is False
    assert forced.sent_via == active_send.CHANNEL_NONE and forced.error

    sending = active_send.classify_after_decorators([1], stopped=False, side_started=False)
    bad_via = active_send.fold_outcome(decision=sending, accepted=True, dispatched=True, sent_via="?")
    assert bad_via.accepted_by_platform is False and bad_via.sent_via == active_send.CHANNEL_NONE
    assert "AccountingViolation:?" in bad_via.notes

    no_dispatch = active_send.fold_outcome(
        decision=sending, accepted=True, dispatched=False, sent_via=active_send.CHANNEL_CONTEXT
    )
    assert no_dispatch.accepted_by_platform is False
    assert "SentViaWithoutDispatch" in no_dispatch.notes

    decorator = active_send.classify_after_decorators(
        [1], stopped=True, side_started=True
    )
    assert active_send.fold_outcome(decision=decorator, accepted=True, dispatched=True).succeeded


# ---------------------------------------------------------------------------
# 合成事件字段（§5.1）
# ---------------------------------------------------------------------------


def test_synthetic_event_is_honest_about_the_original_message():
    snap = _snapshot()
    context = _FakeContext()
    event = active_send.build_proactive_event(
        context, snap, [_FakePlain("终态正文")], framework=_fake_framework()
    )
    assert event.message_str == "帮我看一下这个页面", "message_str 给触发文本（§5.1 / B-4）"
    assert event.message_obj.message_str == "" and event.message_obj.message == []
    assert event.message_obj.raw_message is None, "不假装拥有原始消息（B-1）"
    assert event.unified_msg_origin == snap.umo
    assert event.platform_meta.name == "aiocqhttp"
    assert event.message_obj.type == "GroupMessage"
    assert event.message_obj.group.group_id == "777001"
    assert event.message_obj.sender.user_id == "10001"
    assert event.message_obj.sender.nickname == "tester"
    assert event.message_obj.self_id == "90000"
    assert event.message_obj.message_id == "m-1", "装饰方插 Reply 时要引用用户那条消息"
    assert [c.text for c in event.get_result().chain] == ["终态正文"]


def test_synthetic_event_inherits_plugins_name_and_flattens_text():
    snap = _snapshot(plugins_name=["plugin_a"], trigger_text="第一行\n第二行" + "长" * 600)
    event = active_send.build_proactive_event(
        _FakeContext(), snap, [], framework=_fake_framework()
    )
    assert event.plugins_name == ["plugin_a"], "漏了它 = 装饰方集体消失（§5.1）"
    assert "\n" not in event.message_str and len(event.message_str) <= 500


def test_result_content_type_is_llm_result():
    event = active_send.build_proactive_event(
        _FakeContext(), _snapshot(), [_FakePlain("x")], framework=_fake_framework()
    )
    result = event.get_result()
    assert result.result_content_type == _FakeContentType.LLM_RESULT
    assert result.is_llm_result() and result.is_model_result(), (
        "GENERAL_RESULT 会让 splitter / emotion_favour 的 llm 门直接不理终态（§5.5）"
    )


def test_snapshot_from_event_reads_submit_time_fields():
    event = active_send.build_proactive_event(
        _FakeContext(), _snapshot(plugins_name=("plugin_a",)), [], framework=_fake_framework()
    )
    snap = active_send.snapshot_from_event(event)
    assert snap.umo == "aiocqhttp-1:GroupMessage:777001"
    assert (snap.platform_name, snap.platform_id) == ("aiocqhttp", "aiocqhttp-1")
    assert snap.message_type == "GroupMessage"
    assert (snap.group_id, snap.sender_id, snap.sender_name) == ("777001", "10001", "tester")
    assert snap.self_id == "90000" and snap.trigger_message_id == "m-1"
    assert snap.plugins_name == ("plugin_a",)


# ---------------------------------------------------------------------------
# 四类装饰形态（执行级）
# ---------------------------------------------------------------------------


def test_shape_a_chain_mutation_sends_via_context():
    log = _RecordingLog()

    async def decorate(event):
        event.get_result().chain = [_FakePlain("装饰后的正文")]

    registry = _FakeRegistry(decorating=[_FakeHandler("decorate", decorate)])
    context = _FakeContext()
    out = _run(_deliver(context, registry, log=log))

    assert out.shape == active_send.SHAPE_A
    assert out.attempted and out.succeeded and out.accepted_by_platform
    assert out.sent_via == active_send.CHANNEL_CONTEXT and out.error == ""
    assert len(context.send_calls) == 1
    assert [c.text for c in context.send_calls[0][1]] == ["装饰后的正文"], "要发改写后的链"
    assert any(text.startswith("后台终态已投递") for text in log.texts("INFO"))


def test_shape_b_decorator_sent_and_stopped_is_success():
    async def decorate(event):
        await event.send(_FakeChain([_FakePlain("装饰方自己发")]))
        event.stop_event()

    registry = _FakeRegistry(
        decorating=[_FakeHandler("decorate", decorate)],
        after_sent=[_FakeHandler("after", _noop)],
    )
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert out.shape == active_send.SHAPE_B
    assert out.sent_via == active_send.CHANNEL_DECORATOR
    assert out.attempted and out.accepted_by_platform and out.dispatched
    assert out.error == ""
    assert len(context.send_calls) == 1, "装饰方已发 ⇒ 我们不再重发"
    assert [c.text for c in context.send_calls[0][1]] == ["装饰方自己发"]


def test_shape_b_prime_side_send_error_is_recorded():
    calls = {"n": 0}

    async def decorate(event):
        try:
            await event.send(_FakeChain([_FakePlain("发不出去")]))
        finally:
            event.stop_event()

    registry = _FakeRegistry(decorating=[_FakeHandler("decorate", decorate)])
    context = _FakeContext(raise_error=RuntimeError("adapter down"))

    def on_send():
        calls["n"] += 1

    context.on_send = on_send
    log = _RecordingLog()
    out = _run(_deliver(context, registry, log=log))

    assert out.shape == active_send.SHAPE_B_PRIME
    assert out.sent_via == active_send.CHANNEL_DECORATOR
    assert out.attempted is True and out.accepted_by_platform is False
    assert out.error == "SideSend:RuntimeError"
    assert out.dispatched is True and calls["n"] == 1, "只发了装饰方那一次"
    assert any("后台终态投递失败" in text for text in log.texts("WARNING"))
    assert any("decorate" in item for item in out.hook_errors), "钩子异常本身也要留痕"


def test_shape_c_clear_and_stop_is_suppressed():
    async def decorate(event):
        event.set_result(_FakeResult([]))
        event.stop_event()

    registry = _FakeRegistry(
        decorating=[_FakeHandler("decorate", decorate)],
        after_sent=[_FakeHandler("after", _noop)],
    )
    context = _FakeContext()
    log = _RecordingLog()
    out = _run(_deliver(context, registry, log=log))

    assert out.shape == active_send.SHAPE_C
    assert out.attempted is False and out.accepted_by_platform is False
    assert out.sent_via == active_send.CHANNEL_NONE
    assert out.error == active_send.ERROR_DECORATOR_SUPPRESSED
    assert context.send_calls == []
    assert [call["hook"] for call in registry.calls] == ["OnDecoratingResultEvent"], (
        "空链不重放 after-sent（§5.4）"
    )
    assert any("后台终态未投递" in text and "DecoratorSuppressed" in text for text in log.texts("WARNING"))


def test_shape_d_clear_without_stop_is_suppressed():
    async def decorate(event):
        event.set_result(_FakeResult([]))

    registry = _FakeRegistry(decorating=[_FakeHandler("decorate", decorate)])
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert out.shape == active_send.SHAPE_D
    assert out.attempted is False and out.sent_via == active_send.CHANNEL_NONE
    assert out.error == active_send.ERROR_DECORATOR_SUPPRESSED
    assert context.send_calls == []


def test_shape_a_prime_keeps_last_segment():
    """splitter 形态：前 N-1 段用 ``context.send_message`` 直发（我们看不见），末段留链。"""

    async def decorate(event):
        segments = event.get_result().chain
        await event.context.send_message(event.unified_msg_origin, _FakeChain([segments[0]]))
        event.get_result().chain = segments[1:]

    registry = _FakeRegistry(decorating=[_FakeHandler("splitter", decorate)])
    context = _FakeContext()
    out = _run(
        _deliver(
            context,
            registry,
            chain=[_FakePlain("段1"), _FakePlain("段2")],
        )
    )

    assert out.shape == active_send.SHAPE_A, "A/A′ 可观测状态等价，统一按 A 处置"
    assert out.sent_via == active_send.CHANNEL_CONTEXT and out.accepted_by_platform
    assert [c.text for c in context.send_calls[0][1]] == ["段1"], "装饰方自己发的第一段"
    assert [c.text for c in context.send_calls[1][1]] == ["段2"], "我们只发末段，不重发"
    assert len(context.send_calls) == 2


def test_shape_b_with_cleared_chain_is_still_success():
    """装饰方 `event.send` 发完再把链清空 + stop：仍然认成功、不重发（§5.3 处置 1）。"""

    async def decorate(event):
        await event.send(_FakeChain([_FakePlain("装饰方已发的内容")]))
        event.set_result(_FakeResult([]))
        event.stop_event()

    registry = _FakeRegistry(
        decorating=[_FakeHandler("decorate", decorate)],
        after_sent=[_FakeHandler("after", _noop)],
    )
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert out.shape == active_send.SHAPE_B, "side_started 为真 ⇒ 走 B 的记账（哪怕链被清空）"
    assert out.attempted is True and out.accepted_by_platform is True
    assert out.sent_via == active_send.CHANNEL_DECORATOR and out.error == ""
    assert len(context.send_calls) == 1, "不得因为链空再发一次"
    assert [c.text for c in context.send_calls[0][1]] == ["装饰方已发的内容"]


# ---------------------------------------------------------------------------
# 钩子顺序 / 过滤 / 异常 / 超时（§5.2）
# ---------------------------------------------------------------------------


async def _noop(event):
    return None


def test_decorating_then_send_then_after_sent_order():
    order = []

    async def dec_high(event):
        order.append("dec_high")

    async def dec_low(event):
        order.append("dec_low")

    async def after(event):
        order.append("after")

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("dec_low", dec_low, priority=0),
            _FakeHandler("dec_high", dec_high, priority=10),
        ],
        after_sent=[_FakeHandler("after", after)],
    )
    context = _FakeContext(on_send=lambda: order.append("send"))
    out = _run(_deliver(context, registry))

    assert order == ["dec_high", "dec_low", "send", "after"], "顺序即契约（§5.2 / §5.4）"
    assert out.accepted_by_platform is True
    assert [call["hook"] for call in registry.calls] == [
        "OnDecoratingResultEvent",
        "OnAfterMessageSentEvent",
    ]


def test_stop_event_without_side_send_still_sends_the_chain():
    """R1：装饰钩子只 ``stop_event()``、不改链 ⇒ **照发**（与部署核心的实际行为一致）。

    依据（部署核心 ``db4ce0c4b``，只读核对 + ``test_real_core_scheduler_sends_when_zero_yield_stage_stops``
    实测）：``ResultDecorateStage.process`` 在 ``content_safety.also_use_in_response=False``（默认）
    时是**零 yield 的异步生成器**（``result_decorate/stage.py:156`` 是唯一 ``yield``，被内容安全
    开关包着），而 ``PipelineScheduler`` 的 ``is_stopped()`` 检查写在 ``async for`` 的**循环体内**
    （``scheduler.py:53-70``）⇒ 循环体一次都不进、stop 检查被跳过，``RespondStage``（无
    ``is_stopped`` 检查）对非空链**照发**。后台若判「不发」，就是前台发、后台丢。
    """
    order = []

    async def first(event):
        order.append("first")
        event.stop_event()

    async def second(event):
        order.append("second")

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("first", first, priority=10),
            _FakeHandler("second", second, priority=0),
        ]
    )
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert order == ["first"], "stop 之后不再跑后续装饰钩子（§5.2）"
    assert out.shape == active_send.SHAPE_C_PRIME
    assert out.attempted is True and out.accepted_by_platform is True, "链非空 ⇒ 必须发出去"
    assert out.sent_via == active_send.CHANNEL_CONTEXT and out.error == ""
    assert len(context.send_calls) == 1, "与前台同权：这条链只发一次"
    assert [c.text for c in context.send_calls[0][1]] == ["终态正文"]


def test_partial_side_send_plus_remaining_chain_sends_twice_like_core():
    """**继承核心的行为**（不是 bug）：装饰方发了一部分、链里还剩内容、也没 stop。

    核心在这种情况下同样会两条都发（装饰方那条 + ``RespondStage`` 对剩余链那条），
    我们按形态 A 处置（发剩余链）—— 用户会看到两条消息，与前台一致。
    这是 R1/R2 判序的**已知代价**，登记在 README/CHANGELOG 的「已知边界」，不做特殊处理。
    """

    async def decorate(event):
        await event.send(_FakeChain([_FakePlain("装饰方发的第一段")]))
        event.set_result(_FakeResult([_FakePlain("剩下的第二段")]))

    registry = _FakeRegistry(decorating=[_FakeHandler("decorate", decorate)])
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert len(context.send_calls) == 2, "装饰方一次 + 我们发剩余链一次（与核心同形）"
    assert [c.text for c in context.send_calls[0][1]] == ["装饰方发的第一段"]
    assert [c.text for c in context.send_calls[1][1]] == ["剩下的第二段"]
    assert out.shape == active_send.SHAPE_A and out.accepted_by_platform is True


def test_side_send_then_clear_without_stop_is_success():
    """R2：``event.send`` 已发 + 清空链 + **未** stop ⇒ 认成功（§5.3 处置 1）。

    旧判序会落到 ``DecoratorSuppressed``（账说"没投递"，用户其实已经收到了）。
    现在 ``side_started`` 优先：attempted=True、accepted=not side_err、sent_via=decorator、
    不重发；**after-sent 照常重放**（用户确实收到过这条，下游写记忆/写统计该跑；captain
    round-2 裁定推翻评审「链空不重放」的措辞）。
    """

    async def decorate(event):
        await event.send(_FakeChain([_FakePlain("装饰方发出去的内容")]))
        event.set_result(_FakeResult([]))
        # 故意不 stop：模拟"发完就清链"的装饰方

    after_calls = []

    async def after(event):
        after_calls.append("after")

    registry = _FakeRegistry(
        decorating=[_FakeHandler("decorate", decorate)],
        after_sent=[_FakeHandler("after", after)],
    )
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert out.shape == active_send.SHAPE_B and out.attempted is True
    assert out.accepted_by_platform is True and out.error == ""
    assert out.sent_via == active_send.CHANNEL_DECORATOR
    assert len(context.send_calls) == 1, "装饰方那一次；不再重发（不重复打扰用户）"
    assert [c.text for c in context.send_calls[0][1]] == ["装饰方发出去的内容"]
    assert after_calls == ["after"], "after-sent 照常重放（用户收到过这条）"
    assert len(context.send_calls) == 1, "platform_sent==1：不重复打扰"


def test_plugins_name_filters_handlers():
    ran = []

    async def handler_a(event):
        ran.append("a")

    async def handler_b(event):
        ran.append("b")

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("handler_a", handler_a, plugin="plugin_a"),
            _FakeHandler("handler_b", handler_b, plugin="plugin_b"),
        ]
    )
    out = _run(_deliver(_FakeContext(), registry, snapshot=_snapshot(plugins_name=["plugin_a"])))

    assert ran == ["a"], "白名单由 registry 承担，我们不再过滤一遍"
    assert registry.calls[0]["plugins_name"] == ["plugin_a"], "plugins_name 必须原样传给 registry"
    assert out.accepted_by_platform is True


@pytest.mark.parametrize("plugins_name", [None, ["*"]])
def test_plugins_name_none_or_wildcard_runs_all(plugins_name):
    ran = []

    async def handler_a(event):
        ran.append("a")

    async def handler_b(event):
        ran.append("b")

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("handler_a", handler_a, plugin="plugin_a"),
            _FakeHandler("handler_b", handler_b, plugin="plugin_b"),
        ]
    )
    _run(_deliver(_FakeContext(), registry, snapshot=_snapshot(plugins_name=plugins_name)))

    assert ran == ["a", "b"]


def test_after_sent_not_replayed_for_suppressed_shapes():
    calls = []

    async def after(event):
        calls.append("after")

    async def clear(event):
        event.set_result(_FakeResult([]))

    registry = _FakeRegistry(
        decorating=[_FakeHandler("clear", clear)],
        after_sent=[_FakeHandler("after", after)],
    )
    out = _run(_deliver(_FakeContext(), registry))

    assert out.suppressed is True
    assert calls == [], "没收到的消息不该触发下游写记忆/写统计（§5.4）"


def test_handler_exception_does_not_stop_replay():
    ran = []

    async def boom(event):
        ran.append("boom")
        raise RuntimeError("decorator crashed")

    async def fix(event):
        ran.append("fix")
        event.get_result().chain = [_FakePlain("后面的钩子救回来了")]

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("boom", boom, priority=10),
            _FakeHandler("fix", fix, priority=0),
        ]
    )
    context = _FakeContext()
    out = _run(_deliver(context, registry))

    assert ran == ["boom", "fix"], "单钩子异常只记日志、不中断（核心同款）"
    assert out.accepted_by_platform is True and out.error == ""
    assert f"{active_send.ERROR_HOOK_FAILED}:plugin_a.boom" in out.hook_errors, (
        "单钩子异常记 HookError:<plugin>.<handler>，不污染 error（§5.2）"
    )
    assert [c.text for c in context.send_calls[0][1]] == ["后面的钩子救回来了"]


def test_hook_timeout_continues_and_is_reported():
    async def slow(event):
        await asyncio.sleep(0.3)

    async def fix(event):
        event.get_result().chain = [_FakePlain("超时后仍然发出去")]

    registry = _FakeRegistry(
        decorating=[
            _FakeHandler("slow", slow, priority=10),
            _FakeHandler("fix", fix, priority=0),
        ]
    )
    context = _FakeContext()
    log = _RecordingLog()
    out = _run(_deliver(context, registry, log=log, hook_timeout_s=0.02, hook_total_timeout_s=5.0))

    assert f"{active_send.ERROR_HOOK_TIMEOUT}:plugin_a.slow" in out.hook_errors, (
        "spec §5.2 的制式是 HookTimeout:<plugin>.<handler>（R9：不许是幻影串）"
    )
    assert out.accepted_by_platform is True
    assert [c.text for c in context.send_calls[0][1]] == ["超时后仍然发出去"]
    assert any("钩子超时" in text for text in log.texts("WARNING"))


def test_total_replay_timeout_keeps_delivering():
    async def slow(event):
        await asyncio.sleep(0.2)

    registry = _FakeRegistry(decorating=[_FakeHandler("slow", slow)])
    context = _FakeContext()
    log = _RecordingLog()
    out = _run(
        _deliver(context, registry, log=log, hook_timeout_s=5.0, hook_total_timeout_s=0.02)
    )

    assert any("钩子重放整体超时" in text for text in log.texts("WARNING"))
    assert out.accepted_by_platform is True, "钩子段超时不该把整条投递拖死"


def test_no_decorating_handlers_logs_warning():
    log = _RecordingLog()
    out = _run(_deliver(_FakeContext(), _FakeRegistry(), log=log))

    assert out.accepted_by_platform is True, "没有装饰方也要正常发出去"
    assert any("未命中任何 on_decorating_result" in text for text in log.texts("WARNING"))


# ---------------------------------------------------------------------------
# 平台闸：不静默（§4.2 / §4.4）
# ---------------------------------------------------------------------------


def test_platform_not_found_uses_fallback_and_warns():
    snap = _snapshot()
    context = _FakeContext(has_platform=False)
    sent = []

    async def fallback(chain):
        sent.append(list(getattr(chain, "chain", []) or []))

    log = _RecordingLog()
    out = _run(_deliver(context, _FakeRegistry(), snapshot=snap, log=log, fallback_send=fallback))

    assert out.error == "", "回退救回 ⇒ 通道没报告失败（R5）"
    assert "Recovered:PlatformNotFound" in out.notes, "L1 腿没走的原因留 notes（与服务分离）"
    assert out.attempted is True and out.dispatched is True
    assert out.sent_via == active_send.CHANNEL_FALLBACK and out.accepted_by_platform is True
    assert context.send_calls == [], "平台找不到时不走 L1"
    assert [c.text for c in sent[0]] == ["终态正文"]
    assert any("平台实例未找到" in text for text in log.texts("WARNING"))
    assert any("回退通道" in text and "fallback=1" in text for text in log.texts("INFO"))


def test_platform_not_found_without_fallback_is_not_silent():
    context = _FakeContext(has_platform=False)
    log = _RecordingLog()
    out = _run(_deliver(context, _FakeRegistry(), log=log))

    assert out.error == active_send.ERROR_PLATFORM_NOT_FOUND
    assert out.attempted is True and out.dispatched is False
    assert out.accepted_by_platform is False and out.sent_via == active_send.CHANNEL_NONE
    assert active_send.ERROR_NO_FALLBACK in out.notes
    assert any("后台终态投递失败" in text for text in log.texts("WARNING"))


def test_qq_official_group_uses_l1_with_hint():
    snap = _snapshot(
        umo="qq_official-1:GroupMessage:777001",
        platform_name="qq_official",
        platform_id="qq_official-1",
    )
    context = _FakeContext(platform_name="qq_official", platform_id="qq_official-1")
    log = _RecordingLog()
    out = _run(_deliver(context, _FakeRegistry(), snapshot=snap, log=log))

    assert out.sent_via == active_send.CHANNEL_CONTEXT and out.accepted_by_platform is True
    assert out.hint == active_send.QQ_OFFICIAL_GROUP_HINT
    assert len(context.send_calls) == 1
    assert any(active_send.QQ_OFFICIAL_GROUP_HINT in text for text in log.texts("WARNING"))


@pytest.mark.parametrize(
    ("send_ok", "raise_error", "expected"),
    [
        (False, None, active_send.ERROR_PLATFORM_NOT_FOUND),
        (True, RuntimeError("not supported"), "PlatformSendError:RuntimeError"),
    ],
)
def test_qq_official_l1_failure_falls_back_with_error(send_ok, raise_error, expected):
    snap = _snapshot(
        umo="qq_official-1:GroupMessage:777001",
        platform_name="qq_official",
        platform_id="qq_official-1",
    )
    context = _FakeContext(
        platform_name="qq_official",
        platform_id="qq_official-1",
        send_ok=send_ok,
        raise_error=raise_error,
    )
    sent = []

    async def fallback(chain):
        sent.append(list(getattr(chain, "chain", []) or []))

    log = _RecordingLog()
    out = _run(_deliver(context, _FakeRegistry(), snapshot=snap, log=log, fallback_send=fallback))

    assert out.error == "", "回退救回 ⇒ 账上不记失败（R5）"
    assert f"Recovered:{expected}" in out.notes, "但 L1 腿为什么不可用必须留痕"
    assert out.sent_via == active_send.CHANNEL_FALLBACK and out.accepted_by_platform is True
    assert len(sent) == 1, "回退分支必须真的把消息发出去（不静默丢）"
    assert any("转回退" in text for text in log.texts("WARNING"))


def test_bad_umo_reports_bad_session_and_uses_fallback():
    snap = _snapshot(umo="777001|10001")  # main._umo_of 的兜底串
    sent = []

    async def fallback(chain):
        sent.append(list(getattr(chain, "chain", []) or []))

    log = _RecordingLog()
    out = _run(
        _deliver(_FakeContext(), _FakeRegistry(), snapshot=snap, log=log, fallback_send=fallback)
    )

    assert out.error == "", "回退救回 ⇒ 不记失败（R5）"
    assert "Recovered:BadSession" in out.notes, "会话不可解析的原因留 notes"
    assert out.attempted is True and out.dispatched is True
    assert out.sent_via == active_send.CHANNEL_FALLBACK and out.accepted_by_platform is True
    assert [c.text for c in sent[0]] == ["终态正文"]
    assert any("会话不可解析" in text for text in log.texts("WARNING"))


def test_delivery_timeout_marks_failed():
    async def slow(event):
        await asyncio.sleep(0.3)

    registry = _FakeRegistry(decorating=[_FakeHandler("slow", slow)])
    context = _FakeContext()
    log = _RecordingLog()
    out = _run(
        _deliver(
            context,
            registry,
            log=log,
            hook_timeout_s=5.0,
            hook_total_timeout_s=5.0,
            total_timeout_s=0.02,
        )
    )

    assert out.error == active_send.ERROR_DELIVERY_TIMEOUT
    assert out.attempted is True and out.accepted_by_platform is False
    assert any("投递超时" in text for text in log.texts("WARNING"))
    assert any("后台终态投递失败" in text for text in log.texts("WARNING")), "超时不能不吭声"


def test_splitter_patch_is_noted():
    context = _FakeContext()
    context._splitter_send_patched = True
    out = _run(_deliver(context, _FakeRegistry()))

    assert "splitter_patch=1" in out.notes, "补丁路径要在日志/账里可辨（§5.6）"
    assert out.sent_via == active_send.CHANNEL_CONTEXT


# ---------------------------------------------------------------------------
# 记账面与日志纪律
# ---------------------------------------------------------------------------


def test_outcome_has_contract_fields():
    names = {field.name for field in dataclasses.fields(active_send.DeliveryOutcome)}
    assert {
        "attempted",
        "succeeded",
        "accepted_by_platform",
        "error",
        "sent_via",
    } <= names
    assert active_send.DeliveryReceipt is active_send.DeliveryOutcome
    outcome = active_send.DeliveryOutcome(attempted=True, accepted_by_platform=True, sent_via="decorator")
    assert outcome.as_log_fields().startswith("attempted=1 accepted=1")


def test_log_lines_do_not_carry_body_text():
    async def decorate(event):
        event.get_result().chain = [_FakePlain("这段正文不许进日志")]

    log = _RecordingLog()
    _run(_deliver(_FakeContext(), _FakeRegistry(decorating=[_FakeHandler("dec", decorate)]), log=log))

    joined = "\n".join(log.texts())
    assert "这段正文不许进日志" not in joined, "日志不得出现正文（§4.4 末段）"
    assert "777001" not in joined and "10001" not in joined


# ---------------------------------------------------------------------------
# 导入纪律 + 真 astrbot 类型（子进程）
# ---------------------------------------------------------------------------

_IMPORT_DISCIPLINE_SCRIPT = r'''
import sys


class _Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "astrbot" or fullname.startswith("astrbot."):
            raise ImportError("core.active_send 顶层不得 import astrbot")
        return None


sys.meta_path.insert(0, _Blocker())
import core.active_send as module

assert not [name for name in sys.modules if name.split(".")[0] == "astrbot"]
outcome = module.DeliveryOutcome(
    attempted=True,
    accepted_by_platform=True,
    sent_via=module.CHANNEL_CONTEXT,
)
assert outcome.succeeded is True
assert module.choose_channel("aiocqhttp", "GroupMessage").primary == module.CHANNEL_CONTEXT
print("IMPORT-DISCIPLINE-OK")
'''

_REAL_ASTRBOT_SCRIPT = r'''
import asyncio
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import core.active_send as module
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata


class _Handler:
    def __init__(self, name, fn):
        self.handler_name = name
        self.handler_module_path = "data.plugins.real_plugin.main"
        self.handler = fn


class _Registry:
    def __init__(self, decorating=(), after=()):
        self.decorating = list(decorating)
        self.after = list(after)

    def get_handlers_by_event_type(self, event_type, only_activated=True, plugins_name=None):
        name = getattr(event_type, "name", "")
        if name == "OnDecoratingResultEvent":
            return list(self.decorating)
        if name == "OnAfterMessageSentEvent":
            return list(self.after)
        return []


class _PlatformInst:
    """真实平台适配器的 ``meta()`` 契约（不启动任何适配器）。"""

    def __init__(self):
        self._meta = PlatformMetadata(name="aiocqhttp", description="", id="aiocqhttp-1")

    def meta(self):
        return self._meta


class _Context:
    def __init__(self):
        self.calls = []
        self.platform_manager = SimpleNamespace(platform_insts=[_PlatformInst()])

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        return True



async def main():
    checks = {}
    framework = module.load_framework()
    checks["framework_is_real"] = framework.registry is not None
    context = _Context()
    snapshot = module.DeliverySnapshot(
        umo="aiocqhttp-1:GroupMessage:777",
        platform_name="aiocqhttp",
        platform_id="aiocqhttp-1",
        message_type="GroupMessage",
        group_id="777",
        sender_id="42",
        sender_name="alice",
        self_id="9",
        plugins_name=("splitter",),
        trigger_message_id="m-1",
        trigger_text="看下这个页面",
    )
    event = module.build_proactive_event(
        context, snapshot, [module.build_chain("原始正文", framework=framework)[0]],
        framework=framework,
    )
    checks["real_event"] = isinstance(event, AstrMessageEvent)
    checks["umo"] = event.unified_msg_origin == "aiocqhttp-1:GroupMessage:777"
    checks["event_message_str"] = event.message_str == "看下这个页面"
    checks["honest_shell"] = (
        event.message_obj.raw_message is None
        and event.message_obj.message == []
        and event.message_obj.message_str == ""
    )
    checks["group"] = event.message_obj.group.group_id == "777"
    checks["sender"] = (
        event.message_obj.sender.user_id == "42" and event.message_obj.sender.nickname == "alice"
    )
    checks["ids"] = event.message_obj.self_id == "9" and event.message_obj.message_id == "m-1"
    checks["plugins_name"] = event.plugins_name == ["splitter"]
    checks["message_type"] = getattr(event.message_obj.type, "value", "") == "GroupMessage"
    result = event.get_result()
    checks["llm_result"] = (
        isinstance(result, MessageEventResult) and result.is_llm_result() and result.is_model_result()
    )
    checks["platform_name"] = event.get_platform_name() == "aiocqhttp"
    roundtrip = module.snapshot_from_event(event)
    checks["snapshot_roundtrip"] = (
        roundtrip.umo,
        roundtrip.group_id,
        roundtrip.sender_id,
        roundtrip.message_type,
        roundtrip.trigger_message_id,
        roundtrip.plugins_name,
    ) == (
        "aiocqhttp-1:GroupMessage:777",
        "777",
        "42",
        "GroupMessage",
        "m-1",
        ("splitter",),
    )

    order = []

    async def decorate(ev):
        order.append("decorate")
        ev.get_result().chain.append(module.build_chain("—装饰", framework=framework)[0])

    async def after(ev):
        order.append("after")

    outcome = await module.deliver(
        context,
        snapshot,
        chain=[module.build_chain("原始正文", framework=framework)[0]],
        registry=_Registry(decorating=[_Handler("decorate", decorate)], after=[_Handler("after", after)]),
        framework=framework,
        task_id="real01",
        status="ok",
    )
    checks["delivery_shape"] = outcome.shape == module.SHAPE_A
    checks["delivery_accounting"] = (
        outcome.accepted_by_platform
        and outcome.succeeded
        and outcome.error == ""
        and outcome.sent_via == module.CHANNEL_CONTEXT
    )
    checks["delivery_order"] = order == ["decorate", "after"]
    checks["single_send"] = len(context.calls) == 1
    checks["real_chain"] = isinstance(context.calls[0][1], MessageChain)
    checks["decorated_text"] = [
        getattr(component, "text", "") for component in context.calls[0][1].chain
    ] == ["原始正文", "—装饰"]
    failed = sorted(name for name, ok in checks.items() if not ok)
    if failed:
        print("REAL-ASTRBOT-FAILED:", failed)
        print(json.dumps(checks, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)
    print("REAL-ASTRBOT-OK")


asyncio.run(main())
'''


def _run_python(
    script: str, *, pythonpath: str | None = None, cwd: Path | None = None, timeout: float = 180.0
):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_REAL_SCHEDULER_SCRIPT = r'''
"""R1 的执行级判定（对**部署核心**直接跑调度器，不依赖推断）。

与 captain 的 ``verification/probe_scheduler_stop_semantics.py`` 同形：
一个**零 yield** 但 ``stop_event()`` 的 stage（= ``ResultDecorateStage`` 在
``also_use_in_response=False`` 时的形态）+ 一个记录调用的下游 stage（= ``RespondStage``）。
输出形如：``zero-yield stop -> downstream stages ran: ['SENDS'] | stopped = True``。
"""
import asyncio
import sys

sys.path.insert(0, "/root/AstrBot")
from astrbot.core.pipeline.scheduler import PipelineScheduler  # noqa: E402


class _Event:
    def __init__(self):
        self.stopped = False

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True


class _ZeroYieldStopStage:
    """等价 ``ResultDecorateStage``（生产口径）：零 yield，且在 process 里 stop。"""

    def __init__(self, event, log):
        self.event = event
        self.log = log

    async def process(self, _event):
        self.log.append("DECORATE")
        self.event.stop_event()
        return
        yield  # noqa: unreachable —— 让函数成为异步生成器（唯 yield 永不执行）


class _Responder:
    def __init__(self, log):
        self.log = log

    async def process(self, _event):
        self.log.append("SENDS")


async def main():
    scheduler = PipelineScheduler.__new__(PipelineScheduler)  # 跳过 __init__ 的依赖
    scheduler.ctx = None
    event = _Event()
    log = []
    scheduler.stages = [_ZeroYieldStopStage(event, log), _Responder(log)]
    await scheduler._process_stages(event)
    print("zero-yield stop -> downstream stages ran:", log, "| stopped =", event.is_stopped())


asyncio.run(main())
'''


@pytest.mark.skipif(
    not (Path("/root/AstrBot") / "astrbot").is_dir(),
    reason="本机无 /root/AstrBot 部署树（导出树/CI 里跳过）",
)
def test_real_core_scheduler_sends_when_zero_yield_stage_stops():
    """R1：**真核心调度器**实测 —— 零 yield 的 stage 里 ``stop_event()`` **不阻断**后续阶段。

    这就是形态 C′ 必须"照发"的依据：``ResultDecorateStage`` 在
    ``content_safety.also_use_in_response=False``（生产默认）时零 yield（唯一 ``yield`` 在
    内容安全循环 ``result_decorate/stage.py:156``），``PipelineScheduler`` 的
    ``is_stopped()`` 检查在 ``async for`` 的循环体内（``scheduler.py:53-70``）⇒ 循环体不执行
    ⇒ stop 被忽略 ⇒ ``RespondStage``（无 ``is_stopped`` 检查）照发非空链。
    """
    result = _run_python(_REAL_SCHEDULER_SCRIPT, cwd=Path("/tmp"))
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "['DECORATE', 'SENDS']" in result.stdout, (
        f"下游阶段必须照跑（实测输出：{result.stdout.strip()}）"
    )
    assert "stopped = True" in result.stdout
    # 与实现对齐：同一观测量（stopped、链非空、装饰方没发）在我们这里也必须"照发"
    decision = active_send.classify_after_decorators(
        ["终态正文"], stopped=True, side_started=False
    )
    assert decision.shape == active_send.SHAPE_C_PRIME
    assert decision.should_send is True and decision.sent_via == active_send.CHANNEL_CONTEXT


def test_module_never_imports_astrbot_at_import_time():
    result = _run_python(_IMPORT_DISCIPLINE_SCRIPT)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "IMPORT-DISCIPLINE-OK" in result.stdout


@pytest.mark.skipif(
    not (Path("/root/AstrBot") / "astrbot").is_dir(),
    reason="本机无 /root/AstrBot 部署树（导出树/CI 里跳过）",
)
def test_real_astrbot_types_end_to_end(tmp_path):
    # 在临时目录里跑：真 astrbot 的 import 会写 ``data/``，不许写进插件库工作树
    result = _run_python(
        _REAL_ASTRBOT_SCRIPT, pythonpath=f"{ROOT}{os.pathsep}/root/AstrBot", cwd=tmp_path
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "REAL-ASTRBOT-OK" in result.stdout
