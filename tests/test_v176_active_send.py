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
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.active_send as active_send  # noqa: E402


# ---------------------------------------------------------------------------
# 假框架（事件 / 消息 / 链 / 结果）与假 registry / 假 context
# ---------------------------------------------------------------------------


class _FakeContentType:
    """``ResultContentType`` 替身：只需要 LLM_RESULT 一个成员。"""

    LLM_RESULT = "LLM_RESULT"
    GENERAL_RESULT = "GENERAL_RESULT"


class _FakePlatformMeta:
    def __init__(self, name="aiocqhttp", description="", id="aiocqhttp-1"):
        self.name = name
        self.description = description
        self.id = id


class _FakePlatformInst:
    def __init__(self, meta):
        self._meta = meta

    def meta(self):
        return self._meta


class _FakeChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])

    def __repr__(self):
        return f"MessageChain({self.chain!r})"


class _FakePlain:
    def __init__(self, text=""):
        self.text = text

    def __repr__(self):
        return f"Plain({self.text!r})"


class _FakeMessage:
    def __init__(self):
        self.type = None
        self.session_id = ""
        self.message_id = ""
        self.self_id = ""
        self.group = None
        self.sender = None
        self.message = []
        self.message_str = ""
        self.raw_message = None


class _FakeGroup:
    def __init__(self, group_id=""):
        self.group_id = group_id


class _FakeMember:
    def __init__(self, user_id="", nickname=None):
        self.user_id = user_id
        self.nickname = nickname


class _FakeSession:
    """``MessageSession`` 替身：``from_str`` 与 ``str()`` 与核心同形。"""

    def __init__(self, platform_name, message_type, session_id):
        self.platform_name = platform_name
        self.platform_id = platform_name
        self.message_type = message_type
        self.session_id = session_id

    def __str__(self):
        return f"{self.platform_id}:{self.message_type}:{self.session_id}"

    @staticmethod
    def from_str(value):
        platform_id, message_type, session_id = value.split(":", 2)
        if not platform_id or not message_type:
            raise ValueError(f"bad session: {value}")
        return _FakeSession(platform_id, message_type, session_id)


class _FakeResult:
    def __init__(self, chain=None):
        self.chain = list(chain or [])
        self.result_content_type = None
        self.stopped = False

    def set_result_content_type(self, value):
        self.result_content_type = value
        return self

    def is_llm_result(self):
        return self.result_content_type == _FakeContentType.LLM_RESULT

    def is_model_result(self):
        return self.is_llm_result()

    def stop_event(self):
        self.stopped = True
        return self

    def is_stopped(self):
        return self.stopped


class _FakeEvent:
    """合成事件替身：与 :class:`core.active_send.Framework` 文档的契约同形。"""

    def __init__(self, *, context, umo, message_str, message_obj, platform_meta, session_id):
        self.context = context
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session = _FakeSession.from_str(umo)
        self.session_id = session_id
        self.plugins_name = None
        self.side_send_started = False
        self.side_send_error = ""
        self._result = None
        self._stopped = False

    @property
    def unified_msg_origin(self):
        return str(self.session)

    def get_platform_name(self):
        return self.platform_meta.name

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    async def send(self, message):
        """装饰方经 ``event.send`` 发送：记 side-send 账后委托 L1（与生产同形）。"""
        self.side_send_started = True
        components = list(getattr(message, "chain", None) or [])
        try:
            found = await self.context.send_message(self.unified_msg_origin, _FakeChain(components))
        except Exception as exc:  # noqa: BLE001 — 与生产一致：记 code 后上抛
            self.side_send_error = type(exc).__name__
            raise
        if not found:
            self.side_send_error = active_send.ERROR_PLATFORM_NOT_FOUND

    async def send_streaming(self, generator, use_fallback=False):
        async for chunk in generator:
            await self.send(chunk)


def _fake_framework(registry=None):
    return active_send.Framework(
        event_cls=_FakeEvent,
        message_cls=_FakeMessage,
        group_cls=_FakeGroup,
        member_cls=_FakeMember,
        session_cls=_FakeSession,
        platform_meta_cls=_FakePlatformMeta,
        chain_cls=_FakeChain,
        result_cls=_FakeResult,
        content_type_cls=_FakeContentType,
        plain_cls=_FakePlain,
        registry=registry,
        event_types={
            active_send.HOOK_DECORATING: "OnDecoratingResultEvent",
            active_send.HOOK_AFTER_SENT: "OnAfterMessageSentEvent",
        },
    )


class _FakeHandler:
    def __init__(self, name, fn, *, plugin="plugin_a", priority=0, enabled=True, activated=True):
        self.handler_name = name
        self.handler_module_path = f"data.plugins.{plugin}.main"
        self.plugin_name = plugin
        self.handler = fn
        self.enabled = enabled
        self.activated = activated
        self.extras_configs = {"priority": priority}


class _FakeRegistry:
    """按核心语义过滤：事件类型 / enabled / only_activated / plugins_name（含 ``["*"]``）。"""

    def __init__(self, *, decorating=(), after_sent=()):
        self.handlers = {
            "OnDecoratingResultEvent": sorted(
                decorating, key=lambda h: -h.extras_configs["priority"]
            ),
            "OnAfterMessageSentEvent": sorted(
                after_sent, key=lambda h: -h.extras_configs["priority"]
            ),
        }
        self.calls = []

    def get_handlers_by_event_type(self, event_type, only_activated=True, plugins_name=None):
        hook = str(getattr(event_type, "name", event_type))
        self.calls.append(
            {"hook": hook, "plugins_name": None if plugins_name is None else list(plugins_name)}
        )
        selected = []
        for handler in self.handlers.get(hook, []):
            if not handler.enabled:
                continue
            if only_activated and not handler.activated:
                continue
            if plugins_name is not None and plugins_name != ["*"]:
                if handler.plugin_name not in plugins_name:
                    continue
            selected.append(handler)
        return selected


class _RecordingLog:
    """记录 (level, text) 的日志器替身（取代 caplog，避免受 root logger 配置影响）。"""

    def __init__(self):
        self.records = []

    def _add(self, level, message, args):
        self.records.append((level, message % args if args else str(message)))

    def info(self, message, *args, **kwargs):
        self._add("INFO", message, args)

    def warning(self, message, *args, **kwargs):
        self._add("WARNING", message, args)

    def error(self, message, *args, **kwargs):
        self._add("ERROR", message, args)

    def debug(self, message, *args, **kwargs):
        self._add("DEBUG", message, args)

    def texts(self, level=None):
        return [text for lv, text in self.records if level is None or lv == level]


class _FakeContext:
    """假 Context：``send_message`` 可配成功/失败/抛错，并记录每次调用的链。"""

    def __init__(
        self,
        *,
        platform_name="aiocqhttp",
        platform_id="aiocqhttp-1",
        send_ok=True,
        raise_error=None,
        has_platform=True,
        on_send=None,
    ):
        self.send_calls = []
        self._send_ok = send_ok
        self._raise = raise_error
        self.on_send = on_send
        insts = []
        if has_platform and platform_name:
            insts.append(
                _FakePlatformInst(_FakePlatformMeta(name=platform_name, id=platform_id))
            )
        self.platform_manager = SimpleNamespace(platform_insts=insts)

    async def send_message(self, umo, chain):
        self.send_calls.append((umo, list(getattr(chain, "chain", []) or [])))
        if self.on_send is not None:
            self.on_send()
        if self._raise is not None:
            raise self._raise
        return self._send_ok


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
        ([1], True, False, "", active_send.SHAPE_C_PRIME, False, False, "none", "DecoratorStopped"),
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


def test_stop_event_stops_remaining_decorators():
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
    out = _run(_deliver(_FakeContext(), registry))

    assert order == ["first"]
    assert out.shape == active_send.SHAPE_C_PRIME, "stop 但没发、链还在 ⇒ 不静默（核心同样不发）"
    assert out.error == active_send.ERROR_DECORATOR_STOPPED


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
    assert any("boom" in item for item in out.hook_errors)
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

    assert any("slow" in item for item in out.hook_errors), "超时要留痕（不静默）"
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

    assert out.error == active_send.ERROR_PLATFORM_NOT_FOUND
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

    assert out.error == expected, "qq 官方系 L1 不可用时必须给出明确 error"
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

    assert out.error == active_send.ERROR_BAD_SESSION
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
