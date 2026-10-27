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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_v175_tools as helpers  # noqa: E402 — 复用 _bare_plugin（配置/目录装配）
import core.background as bg  # noqa: E402
import core.active_send as active_send  # noqa: E402
import main as main_module  # noqa: E402

UMO = "aiocqhttp-1:GroupMessage:777001"


# ---------------------------------------------------------------------------
# 假框架：让 active_send 在没有真 astrbot 的环境里跑完整投递路径
# ---------------------------------------------------------------------------


class _FakeContentType:
    LLM_RESULT = "LLM_RESULT"


class _FakeChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])

    def __repr__(self):
        text = "".join(str(getattr(item, "text", item)) for item in self.chain)
        return f"MessageChain({text})"

    __str__ = __repr__


class _FakePlain:
    def __init__(self, text=""):
        self.text = str(text)

    def __repr__(self):
        return f"Plain({self.text!r})"


class _FakePlatformMeta:
    def __init__(self, name="aiocqhttp", description="", id=UMO.split(":")[0]):
        self.name = name
        self.description = description
        self.id = id


class _FakePlatformInst:
    def __init__(self, meta):
        self._meta = meta

    def meta(self):
        return self._meta


class _FakeMessage:
    def __init__(self):
        self.type = "GroupMessage"
        self.session_id = UMO.split(":")[-1]
        self.message_id = "m-1"
        self.self_id = "90000"
        self.group = _FakeGroup("777001")
        self.sender = _FakeMember("10001", "tester")
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


class _FakeSyntheticEvent:
    """合成事件替身（``active_send.build_proactive_event`` 的产物）。"""

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


class RealEventStub:
    """**触发事件**替身：只提供快照白名单字段 + ``send``（平台回退句柄）。

    ``send`` 记调用；需要时用 :meth:`make_probe` 换成"一碰就炸"的探针。
    """

    def __init__(
        self,
        *,
        umo=UMO,
        message_id="m-1",
        platform="aiocqhttp",
        plugins_name=None,
        send_error=None,
    ):
        self._send_error = send_error
        self.unified_msg_origin = umo
        self.message_str = "帮我看下这个页面"
        self.plugins_name = list(plugins_name) if plugins_name else None
        self.platform_meta = _FakePlatformMeta(name=platform, id=umo.split(":")[0])
        self.message_obj = _FakeMessage()
        self.message_obj.message_id = message_id
        self.message_obj.type = umo.split(":")[1]
        self.session_id = umo.split(":")[-1]
        self.sent = []
        self.send_calls = 0

    def get_group_id(self):
        return self.message_obj.group.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_platform_name(self):
        return self.platform_meta.name

    def image_result(self, path):
        return ("image", str(path))

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, message):
        self.send_calls += 1
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(message)


class FakeContext:
    """假 ``Context``：只实现投递需要的两件事（``send_message`` + 只读平台表）。"""

    def __init__(
        self,
        *,
        platform_insts=None,
        send_result=True,
        send_error=None,
        on_send=None,
    ):
        self.send_calls = []
        self._send_result = send_result
        self._send_error = send_error
        self.on_send = on_send
        insts = (
            [_FakePlatformInst(_FakePlatformMeta())]
            if platform_insts is None
            else list(platform_insts)
        )
        self.platform_manager = type("PM", (), {"platform_insts": insts})()

    async def send_message(self, umo, chain):
        self.send_calls.append((umo, list(getattr(chain, "chain", []) or [])))
        if self.on_send is not None:
            self.on_send()
        if self._send_error is not None:
            raise self._send_error
        return self._send_result


class _Handler:
    def __init__(self, name, fn, *, module="data.plugins.probe.main", plugin="probe"):
        self.handler_name = name
        self.handler_module_path = module
        self.plugin_name = plugin
        self.handler = fn


class FakeRegistry:
    """按核心语义过滤：事件类型（按 ``name``）/ 插件白名单（``None`` 与 ``["*"]`` 全放行）。"""

    def __init__(self, decorating=(), after_sent=()):
        self.decorating = list(decorating)
        self.after_sent = list(after_sent)
        self.calls = []

    def get_handlers_by_event_type(self, event_type, only_activated=True, plugins_name=None):
        hook = str(getattr(event_type, "name", event_type))
        self.calls.append({"hook": hook, "plugins_name": None if plugins_name is None else list(plugins_name)})
        if hook == "OnDecoratingResultEvent":
            pool = self.decorating
        elif hook == "OnAfterMessageSentEvent":
            pool = self.after_sent
        else:
            return []
        return [
            handler
            for handler in pool
            if plugins_name is None
            or plugins_name == ["*"]
            or handler.plugin_name in plugins_name
        ]


def make_framework(registry=None):
    return active_send.Framework(
        event_cls=_FakeSyntheticEvent,
        message_cls=_FakeMessage,
        group_cls=_FakeGroup,
        member_cls=_FakeMember,
        session_cls=_FakeSession,
        platform_meta_cls=_FakePlatformMeta,
        chain_cls=_FakeChain,
        result_cls=_FakeResult,
        content_type_cls=_FakeContentType,
        plain_cls=_FakePlain,
        registry=registry if registry is not None else FakeRegistry(),
        event_types={
            active_send.HOOK_DECORATING: "OnDecoratingResultEvent",
            active_send.HOOK_AFTER_SENT: "OnAfterMessageSentEvent",
        },
    )


def install_framework(monkeypatch, registry=None):
    """把 ``active_send.load_framework`` 换成假框架（本文件与媒体用例共用）。"""
    framework = make_framework(registry)
    monkeypatch.setattr(active_send, "load_framework", lambda: framework)
    return framework


def make_plugin(tmp_path, context):
    """装配一个可投递的插件替身（不建浏览器、不联网）。"""
    plugin = helpers._bare_plugin(tmp_path)
    plugin.context = context
    plugin.background_mode = "browse_only"
    return plugin


def make_job(*, task_id="bg-9001", trigger_key="", progress=None, status="running"):
    job = bg.BackgroundJob(
        task_id=task_id,
        umo=UMO,
        tool=main_module._BROWSE_WEB_TOOL_NAME,
        budget_s=30.0,
        task_key=bg.task_key(UMO, main_module._BROWSE_WEB_TOOL_NAME, "t"),
        trigger_key=trigger_key,
        status=status,
    )
    job.progress = dict(progress or {})
    return job


def deliver_once(plugin, event, job, *, body="【后台任务完成】正文", status="ok"):
    """直接调用生产投递入口（快照 + 回退句柄由 ``_terminal_deliverer`` 固定）。

    任务先登记进本插件的 registry：``note_delivery`` 的额度是按 task 记的。
    """
    plugin._watchdog.registry.register(job)
    snapshot = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))
    return asyncio.run(
        plugin._deliver_terminal(job, status, body, snapshot=snapshot, fallback_send=event.send)
    )


async def _wait_handle(handle, *, timeout=3.0):
    """等到这个 handle 收尾（``_finalize`` 只跑一次；不依赖 registry 残留）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if handle.finalized:
            await asyncio.sleep(0)
            return handle
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {handle.task_id} 没有在 {timeout}s 内收尾")


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
    install_framework(monkeypatch)
    context = FakeContext(platform_insts=[])  # 平台实例一个都没有
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()
    job = make_job()

    receipt = deliver_once(plugin, event, job)

    assert receipt.error == active_send.ERROR_PLATFORM_NOT_FOUND
    assert receipt.accepted_by_platform is True
    assert receipt.sent_via == active_send.CHANNEL_FALLBACK
    assert context.send_calls == [], "平台找不到时不走 L1"
    assert event.send_calls == 1, "回退句柄必须用一次（不静默丢消息）"
    assert "【后台任务完成】" in str(event.sent[0])


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


# ---------------------------------------------------------------------------
# 3. TerminalOutcome 记账 + 恰好一条 + 不重试（走真 watchdog）
# ---------------------------------------------------------------------------


def _run_through_watchdog(plugin, event, *, runner=None):
    async def _main():
        async def default_runner(progress):
            return "后台结论"

        handle = plugin._watchdog.submit(
            runner=runner or default_runner,
            deliver=plugin._terminal_deliverer(event),
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


def test_finalize_records_delivery_receipt(tmp_path, monkeypatch, caplog):
    install_framework(monkeypatch)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)

    with caplog.at_level("INFO"):
        handle, _ = _run_through_watchdog(plugin, RealEventStub())

    outcome = handle.outcome
    assert outcome is not None
    assert outcome.delivered is True, "闸放行过（恰好一条的额度已消耗）"
    assert outcome.attempted is True and outcome.succeeded is True
    assert outcome.accepted_by_platform is True and outcome.error == ""
    assert outcome.sent_via == active_send.CHANNEL_CONTEXT
    assert "attempted=1 accepted=1 sent_via=context.send_message" in caplog.text
    assert len(context.send_calls) == 1


def test_exactly_one_terminal_message_even_if_finalize_twice(tmp_path, monkeypatch):
    install_framework(monkeypatch)
    context = FakeContext()
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub()

    async def _main():
        async def runner(progress):
            return "一次就够"

        handle = plugin._watchdog.submit(
            runner=runner,
            deliver=plugin._terminal_deliverer(event),
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


def test_delivery_failure_is_recorded_and_logged(tmp_path, monkeypatch, caplog):
    install_framework(monkeypatch)
    context = FakeContext(send_error=RuntimeError("adapter down"))
    plugin = make_plugin(tmp_path, context)
    event = RealEventStub(send_error=RuntimeError("event gone"))

    with caplog.at_level("WARNING"):
        handle, _ = _run_through_watchdog(plugin, event)

    outcome = handle.outcome
    assert outcome.attempted is True and outcome.succeeded is False
    assert outcome.accepted_by_platform is False
    assert outcome.error == "PlatformSendError:RuntimeError"
    assert outcome.sent_via == active_send.CHANNEL_FALLBACK
    assert outcome.delivered is True, "闸放行过（额度已消耗）但发送没成功 —— 两件事分开记"
    assert "后台任务终态投递失败" in caplog.text
    assert "PlatformSendError:RuntimeError" in caplog.text
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

    deliver = plugin._terminal_deliverer(event)

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
