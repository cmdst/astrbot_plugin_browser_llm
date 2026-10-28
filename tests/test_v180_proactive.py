"""`core/proactive.py` 执行级用例（规格 §6 / §11.3 EX-28..EX-35 的 t2 面）。

全部用**假框架**（假 event / 假 message chain / 假 registry），不打网络、不依赖真 astrbot；
两条接缝是 `proactive._framework`（符号面）与 `proactive._handlers`（钩子面）。
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

from core import proactive as pa
from core import tasks as tb

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 假框架


class FakePlain:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.type = "Plain"


class FakeImage:
    def __init__(self, file: str = "") -> None:
        self.file = file
        self.path = file
        self.type = "Image"

    @staticmethod
    def fromFileSystem(path, **kwargs):
        return FakeImage(str(path))


class FakeVideo(FakeImage):
    def __init__(self, file: str = "") -> None:
        super().__init__(file)
        self.type = "Video"

    @staticmethod
    def fromFileSystem(path, **kwargs):
        return FakeVideo(str(path))


class FakeChain:
    def __init__(self, chain=None) -> None:
        self.chain = list(chain or [])


class FakeResult:
    def __init__(self) -> None:
        self.chain: list = []
        self.content_type = None
        self.result_type = "CONTINUE"

    def stop_event(self):
        self.result_type = "STOP"
        return self

    def is_stopped(self) -> bool:
        return self.result_type == "STOP"

    def set_result_content_type(self, typ):
        self.content_type = typ
        return self


class FakeMessage:
    def __init__(self) -> None:
        self.type = ""
        self.self_id = ""
        self.session_id = ""
        self.message_id = ""
        self.message_str = ""
        self.message: list = []


class FakeMeta:
    def __init__(self, name: str = "", description: str = "", id: str = "") -> None:
        self.name, self.description, self.id = name, description, id


class FakeEvent:
    """最小 `AstrMessageEvent` 替身：只保留本层用到的读写面。"""

    def __init__(self, message_str, message_obj, platform_meta, session_id) -> None:
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session_id = session_id
        self.umo = f"{platform_meta.id}:FriendMessage:{session_id}"
        self.plugins_name = None
        self._result = None
        self.outbound: list = []

    @property
    def unified_msg_origin(self) -> str:
        return self.umo

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:
        self.umo = value

    def get_platform_name(self) -> str:
        return self.platform_meta.name

    def get_platform_id(self) -> str:
        return self.platform_meta.id

    def get_session_id(self) -> str:
        return self.session_id

    def get_message_str(self) -> str:
        return self.message_str

    def get_message_type(self) -> str:
        return "FriendMessage"

    def get_group_id(self) -> str:
        return ""

    def get_sender_id(self) -> str:
        return ""

    def get_self_id(self) -> str:
        return ""

    def get_message_id(self) -> str:
        return ""

    def set_result(self, result) -> None:
        self._result = result

    def get_result(self):
        return self._result

    def clear_result(self) -> None:
        self._result = None

    def is_stopped(self) -> bool:
        return self._result is not None and self._result.is_stopped()

    async def send(self, message) -> None:
        """基类只埋点，**不真发送**（本层覆写版会走 context.send_message）。"""


def _framework() -> pa._Framework:
    return pa._Framework(event_cls=FakeEvent, plain=FakePlain, media=(FakeImage, FakeVideo),
                         chain_cls=FakeChain, result_cls=FakeResult, message_cls=FakeMessage,
                         metadata_cls=FakeMeta, decorating_type="DECORATING", after_type="AFTER",
                         llm_result="LLM_RESULT")


class FakeContext:
    def __init__(self, ok: bool = True, raises: BaseException | None = None) -> None:
        self.calls: list = []
        self.ok, self.raises = ok, raises

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if self.raises is not None:
            raise self.raises
        return self.ok


def _target(**kwargs) -> pa.DeliveryTarget:
    base = {"umo": "aiocqhttp:FriendMessage:u1", "platform_name": "aiocqhttp",
            "platform_id": "aiocqhttp", "session_id": "u1", "message_type": "FriendMessage",
            "sender_id": "u1", "self_id": "bot", "trigger_text": "看一下这个页面"}
    base.update(kwargs)
    return pa.DeliveryTarget(**base)


def _hook(name: str, fn, module: str = "plugin.main") -> types.SimpleNamespace:
    return types.SimpleNamespace(handler_module_path=module, handler_name=name, handler=fn)


def _install(monkeypatch, hooks: dict | None = None, framework=None) -> None:
    hooks = hooks or {}
    monkeypatch.setattr(pa, "_framework", lambda: framework or _framework())
    monkeypatch.setattr(pa, "_handlers", lambda event_type, plugins_name: hooks.get(event_type, []))


def _chain_texts(chain) -> list[str]:
    return [getattr(component, "text", "") for component in chain.chain]


# ------------------------------------------------------------------ EX-28 形态 A


def test_shape_a_rewritten_chain_goes_through_context_send_message(monkeypatch):
    context = FakeContext()

    async def decorate(event):
        result = event.get_result()
        result.chain.append(FakePlain("【前缀】"))
        result.chain.reverse()  # 装饰方改写链

    _install(monkeypatch, {"DECORATING": [_hook("on_dec", decorate)]})

    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.attempted and note.succeeded
    assert note.shape == pa.SHAPE_A and note.channel == pa.CHANNEL_CONTEXT
    assert note.error == "" and note.hook_errors == ()
    assert len(context.calls) == 1, "一次逻辑投递只允许一次 send_message"
    umo, chain = context.calls[0]
    assert umo == "aiocqhttp:FriendMessage:u1"
    assert _chain_texts(chain) == ["【前缀】", "正文"], "发出去的必须是**改写后**的链"


def test_shape_a_keeps_chain_when_decorator_stops_without_clearing(monkeypatch):
    """装饰方只 stop（不清链）⇒ 仍照发（v1.7.6 的 C′ 裁定，归形态 A）。"""
    context = FakeContext()

    async def decorate(event):
        event.get_result().stop_event()

    _install(monkeypatch, {"DECORATING": [_hook("on_dec", decorate)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.shape == pa.SHAPE_A and len(context.calls) == 1


# ------------------------------------------------------------------ EX-29 形态 B


def test_shape_b_decorator_sent_so_we_do_not_send_again(monkeypatch):
    context = FakeContext()

    async def decorate(event):
        await event.send(FakeChain([FakePlain("装饰方自己发的")]))
        event.clear_result()  # 装饰方代发后自己清链 ⇒ 形态 B（规格 §6.4 规则 2/4）

    _install(monkeypatch, {"DECORATING": [_hook("on_dec", decorate)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.shape == pa.SHAPE_B and note.succeeded is True
    assert len(context.calls) == 1, "计数 1 次来自装饰方，本层不得补发"
    assert _chain_texts(context.calls[0][1]) == ["装饰方自己发的"]


def test_shape_b_side_send_failure_is_accounted(monkeypatch):
    context = FakeContext(ok=False)  # 装饰方的 event.send 走我们的覆写 ⇒ 平台找不到

    async def decorate(event):
        try:
            await event.send(FakeChain([FakePlain("x")]))
        except RuntimeError:
            pass  # 装饰方吞掉自己的发送失败，但 _side_send_error 已被记下
        event.clear_result()

    _install(monkeypatch, {"DECORATING": [_hook("on_dec", decorate)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.shape in (pa.SHAPE_B, pa.SHAPE_C)
    assert note.succeeded is False
    assert note.shape == pa.SHAPE_B
    assert note.error == f"SideSend:{pa.ERROR_PLATFORM}"


# ------------------------------------------------------------------ EX-30 形态 C


def test_shape_c_decorator_suppressed_chain(monkeypatch):
    context = FakeContext()

    async def decorate(event):
        event.clear_result()  # 清空链且没发

    _install(monkeypatch, {"DECORATING": [_hook("on_dec", decorate)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.shape == pa.SHAPE_C
    assert note.error == pa.ERROR_SUPPRESSED
    assert note.succeeded is False and note.attempted is True
    assert context.calls == [], "被抑制时空链不得发出去"


# ------------------------------------------------------- EX-31 双钩子顺序


def test_hooks_order_is_decorating_then_send_then_after(monkeypatch):
    order: list[str] = []

    class OrderedContext(FakeContext):
        async def send_message(self, umo, chain):
            order.append("context.send_message")
            return await super().send_message(umo, chain)

    async def decorate(event):
        order.append("on_decorating_result")

    async def after_sent(event):
        order.append("on_after_message_sent")

    _install(monkeypatch, {"DECORATING": [_hook("dec", decorate)],
                           "AFTER": [_hook("after", after_sent)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=OrderedContext()))
    assert order == ["on_decorating_result", "context.send_message", "on_after_message_sent"]
    assert note.shape == pa.SHAPE_A and note.succeeded


# ------------------------------------------------- EX-32 / EX-33 钩子异常与超时


def test_hook_exception_does_not_break_delivery(monkeypatch):
    context = FakeContext()

    async def boom(event):
        raise RuntimeError("钩子炸了")

    async def good(event):
        event.get_result().chain.append(FakePlain("后续钩子仍跑"))

    _install(monkeypatch, {"DECORATING": [_hook("boom", boom), _hook("good", good)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.succeeded is True and len(context.calls) == 1
    assert note.hook_errors == ("HookError:plugin.main.boom",)
    assert "后续钩子仍跑" in _chain_texts(context.calls[0][1])


def test_hook_timeout_is_recorded_and_total_time_is_bounded(monkeypatch):
    context = FakeContext()

    async def slow(event):
        await asyncio.sleep(5)

    monkeypatch.setattr(pa, "HOOK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pa, "HOOK_TOTAL_TIMEOUT_S", 0.2)
    _install(monkeypatch, {"DECORATING": [_hook("slow", slow)]})

    async def scenario():
        started = asyncio.get_running_loop().time()
        note = await pa.deliver(_target(), "正文", context=context)
        return note, asyncio.get_running_loop().time() - started

    note, elapsed = asyncio.run(scenario())
    assert note.succeeded is True
    assert note.hook_errors == ("HookTimeout:plugin.main.slow",)
    assert elapsed < 0.2 + 0.5, f"总耗时应受 HOOK_TOTAL_TIMEOUT_S 约束，实际 {elapsed:.2f}s"


def test_hook_total_budget_cuts_remaining_hooks(monkeypatch):
    context = FakeContext()
    ran: list[str] = []

    async def slow(event):
        await asyncio.sleep(5)

    async def never(event):
        ran.append("never")

    monkeypatch.setattr(pa, "HOOK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pa, "HOOK_TOTAL_TIMEOUT_S", 0.03)
    _install(monkeypatch, {"DECORATING": [_hook("slow", slow), _hook("never", never)]})
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.hook_errors[0] == "HookTimeout:plugin.main.slow"
    assert note.hook_errors[-1] == "HookTimeout:plugin.main.never"
    assert ran == [], "总预算耗尽后剩余钩子不再跑"


# ------------------------------------------------------- EX-34 平台找不到（不重试）


def test_platform_not_found_is_recorded_once_and_never_retried(monkeypatch):
    context = FakeContext(ok=False)
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.attempted is True and note.succeeded is False
    assert note.error == pa.ERROR_PLATFORM
    assert note.channel == pa.CHANNEL_CONTEXT
    assert len(context.calls) == 1, "不重试、不补发"


def test_bad_session_is_recorded_not_raised(monkeypatch):
    context = FakeContext(raises=ValueError("不合法的 session 字符串"))
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.attempted is True and note.error == pa.ERROR_BAD_SESSION
    assert len(context.calls) == 1


# ------------------------------------------------- EX-35 框架不可用 / 空正文


def test_framework_unavailable_degrades_without_raising(monkeypatch):
    context = FakeContext()

    def broken():
        raise pa.FrameworkUnavailable("No module named 'astrbot'")

    _install(monkeypatch, framework=None)
    monkeypatch.setattr(pa, "_framework", broken)
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert note.attempted is True and note.succeeded is False
    assert note.error == pa.ERROR_FRAMEWORK
    assert context.calls == []


def test_empty_body_and_no_media_is_not_attempted(monkeypatch):
    context = FakeContext()
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "   ", context=context))
    assert note.attempted is False and note.error == pa.ERROR_EMPTY
    assert context.calls == []


def test_media_only_message_still_sends(monkeypatch):
    context = FakeContext()
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "", context=context,
                                  media=("/tmp/a.png", "/tmp/b.mp4")))
    assert note.succeeded is True
    chain = context.calls[0][1].chain
    assert [component.type for component in chain] == ["Image", "Video"]


def test_delivery_timeout_is_accounted(monkeypatch):
    class SlowContext(FakeContext):
        async def send_message(self, umo, chain):
            await asyncio.sleep(5)
            return True

    monkeypatch.setattr(pa, "DELIVERY_TIMEOUT_S", 0.05)
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "正文", context=SlowContext()))
    assert note.attempted is True and note.error == pa.ERROR_DELIVERY_TIMEOUT


# ------------------------------------------------- 快照与白名单（生命周期纪律）


def test_snapshot_of_broken_event_never_raises():
    class Broken:
        @property
        def unified_msg_origin(self):
            raise RuntimeError("event 已回收")

        def get_platform_name(self):
            raise RuntimeError("boom")

    snapshot = pa.snapshot_of(Broken(), "aiocqhttp:FriendMessage:u9")
    assert snapshot.umo == "aiocqhttp:FriendMessage:u9"
    assert snapshot.platform_name == "" and snapshot.session_id == ""
    assert snapshot.plugins_name is None


def test_snapshot_truncates_trigger_text_to_one_line():
    class Event:
        unified_msg_origin = "aiocqhttp:FriendMessage:u1"
        plugins_name = ["astrbot_plugin_browser_llm"]

        def get_message_str(self):
            return "第一行\n\n第二行 " + "x" * 2000

        def get_message_type(self):
            return "GroupMessage"

    snapshot = pa.DeliveryTarget.from_event(Event())
    assert snapshot.plugins_name == ("astrbot_plugin_browser_llm",)
    assert snapshot.message_type == "GroupMessage"
    assert "\n" not in snapshot.trigger_text
    assert len(snapshot.trigger_text) == pa.TRIGGER_TEXT_LIMIT


def test_plugins_name_is_inherited_into_synthetic_event(monkeypatch):
    context = FakeContext()
    seen: list = []

    async def decorate(event):
        seen.append(event.plugins_name)
        event.get_result().chain.append(FakePlain("ok"))

    _install(monkeypatch, {"DECORATING": [_hook("dec", decorate)]})
    note = asyncio.run(pa.deliver(_target(plugins_name=("astrbot_plugin_browser_llm",)),
                                  "正文", context=context))
    assert note.succeeded is True
    assert seen == [["astrbot_plugin_browser_llm"]]


# ------------------------------------------------- 与任务簿的契约（记账字段）


def test_delivery_note_reuses_taskbook_dataclass(monkeypatch):
    context = FakeContext()
    _install(monkeypatch)
    note = asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert isinstance(note, tb.DeliveryNote)
    fields = note.as_fields().split()
    assert fields[0] == "attempted=1" and fields[1] == "succeeded=1"
    assert "channel=context.send_message" in fields
    assert "shape=A" in fields


def test_deliver_is_at_most_once_per_call(monkeypatch):
    context = FakeContext()
    _install(monkeypatch)
    for _ in range(2):
        asyncio.run(pa.deliver(_target(), "正文", context=context))
    assert len(context.calls) == 2, "每次逻辑投递恰好一次通道调用（不做内部重试）"


def test_deliver_text_alias_is_same_behaviour(monkeypatch):
    context = FakeContext()
    _install(monkeypatch)
    note = asyncio.run(pa.deliver_text(_target(), "正文", context=context))
    assert note.shape == pa.SHAPE_A and note.succeeded is True
