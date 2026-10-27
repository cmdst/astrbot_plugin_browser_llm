"""v1.7.6 测试共用假件（R14）：假框架 / 假事件 / 假 Context / 假 registry / 假日志。

三个 v1.7.6 用例文件（``test_v176_active_send`` / ``test_v176_delivery_parity`` /
``test_v176_media_delivery``，外加 ``test_v175_background_wiring`` 与
``test_v175_repair_r1`` 的小段复用）曾经各自抄一份假框架 —— 改一处漏三处。
现在只有这一份：

* **假框架**：``FakeChain`` / ``FakePlain`` / ``FakeResult`` / ``FakeSession`` /
  ``FakePlatformMeta`` / ``FakeMessage`` / ``FakeGroup`` / ``FakeMember`` —— 与
  :class:`core.active_send.Framework` 的契约同形，喂给 ``active_send.load_framework``
  的替身就能在没有真 ``astrbot`` 的环境里跑完整投递路径；
* **两个事件替身**：``SyntheticEvent``（合成事件的产物，覆写 ``send`` 记 side-send 账）
  与 ``RealEventStub``（**触发事件**，提供快照白名单字段 + 可计数的 ``send`` 回退句柄）；
* **``FakeContext``**：只实现投递要的两件事（``send_message`` + 只读平台表），
  可配成功/失败/抛错/回调（记录"跳过"场景也能造）；
* **``FakeRegistry`` / ``FakeHandler``**：按核心语义过滤（事件类型 / ``plugins_name``），
  并记录每次调用（``calls``），用于断言"过滤交出去、不在插件侧重过滤"；
* **``RecordingLog``**：记录 ``(level, text)``，断言 WARN/INFO/ERROR 档位而不受 root
  logger 配置影响；**``install_framework``**：``monkeypatch`` 掉
  ``active_send.load_framework``（生产代码不需要测试专用开关）。

另外几个装配助手：``snapshot()``（合规快照）、``make_job``（登记项）、
``make_plugin``（配好 context 的插件替身）、``deliver_once``（直接调投递入口）、
``wait_handle``（等收尾）、``task_runner``（给 ``_submit_background`` 用的同步执行体）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_v175_tools as helpers  # noqa: E402 — 复用 _bare_plugin（配置/目录装配）
import core.active_send as active_send  # noqa: E402
import core.background as bg  # noqa: E402
import main as main_module  # noqa: E402

UMO = "aiocqhttp-1:GroupMessage:777001"


# ---------------------------------------------------------------------------
# 假框架（组件 / 消息 / 会话 / 结果）
# ---------------------------------------------------------------------------


class FakeContentType:
    """``ResultContentType`` 替身：只需要 LLM_RESULT 一个成员。"""

    LLM_RESULT = "LLM_RESULT"
    GENERAL_RESULT = "GENERAL_RESULT"


class FakePlatformMeta:
    def __init__(self, name="aiocqhttp", description="", id="aiocqhttp-1"):
        self.name = name
        self.description = description
        self.id = id


class FakePlatformInst:
    def __init__(self, meta=None):
        self._meta = meta if meta is not None else FakePlatformMeta()

    def meta(self):
        return self._meta


class FakeChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])

    def __repr__(self):
        text = "".join(str(getattr(item, "text", item)) for item in self.chain)
        return f"MessageChain({text})"

    __str__ = __repr__


class FakePlain:
    def __init__(self, text=""):
        self.text = str(text)

    def __repr__(self):
        return f"Plain({self.text!r})"


class FakeAt:
    """``At`` 组件替身（§5.7 用例：``[at:1]`` 只有装饰钩子能把它变成 At）。"""

    def __init__(self, qq="", name=""):
        self.qq = str(qq)
        self.name = str(name)
        self.type = "At"

    def __repr__(self):
        return f"At({self.qq!r})"


class FakeMessage:
    def __init__(self):
        self.type = "GroupMessage"
        self.session_id = UMO.split(":")[-1]
        self.message_id = "m-1"
        self.self_id = "90000"
        self.group = FakeGroup("777001")
        self.sender = FakeMember("10001", "tester")
        self.message = []
        self.message_str = ""
        self.raw_message = None


class FakeGroup:
    def __init__(self, group_id=""):
        self.group_id = group_id


class FakeMember:
    def __init__(self, user_id="", nickname=None):
        self.user_id = user_id
        self.nickname = nickname


class FakeSession:
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
        return FakeSession(platform_id, message_type, session_id)


class FakeResult:
    def __init__(self, chain=None):
        self.chain = list(chain or [])
        self.result_content_type = None
        self.stopped = False

    def set_result_content_type(self, value):
        self.result_content_type = value
        return self

    def is_llm_result(self):
        return self.result_content_type == FakeContentType.LLM_RESULT

    def is_model_result(self):
        return self.is_llm_result()

    def stop_event(self):
        self.stopped = True
        return self

    def is_stopped(self):
        return self.stopped


# ---------------------------------------------------------------------------
# 两个事件替身：合成事件（投递链路用）与触发事件（提交时用）
# ---------------------------------------------------------------------------


class SyntheticEvent:
    """合成事件替身：与 :class:`core.active_send.Framework` 文档的契约同形。"""

    def __init__(self, *, context, umo, message_str, message_obj, platform_meta, session_id):
        self.context = context
        self.message_str = message_str
        self.message_obj = message_obj
        self.platform_meta = platform_meta
        self.session = FakeSession.from_str(umo)
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
            found = await self.context.send_message(
                self.unified_msg_origin, FakeChain(chain=components)
            )
        except Exception as exc:  # noqa: BLE001 — 与生产一致：记 code 后上抛
            self.side_send_error = type(exc).__name__
            raise
        if not found:
            self.side_send_error = active_send.ERROR_PLATFORM_NOT_FOUND

    async def send_streaming(self, generator, use_fallback=False):
        async for chunk in generator:
            await self.send(chunk)


class RealEventStub:
    """**触发事件**替身：只提供快照白名单字段 + ``send``（平台回退句柄，可计数）。

    ``send_calls`` 是"平台回退用了几次"的证据；``send_error`` 用来造"两条腿都失败"。
    ``strict`` 打开后任何未白名单的属性访问都会抛 ``AssertionError``（生命周期探针用）。
    """

    _ALLOWED = frozenset(
        {
            "unified_msg_origin",
            "message_str",
            "plugins_name",
            "platform_meta",
            "message_obj",
            "session_id",
            "send",
            "get_group_id",
            "get_sender_id",
            "get_platform_name",
            "image_result",
            "chain_result",
            "strict",
            "sent",
            "send_calls",
            "access_log",
        }
    )

    def __init__(
        self,
        *,
        umo=UMO,
        message_id="m-1",
        platform="aiocqhttp",
        plugins_name=None,
        send_error=None,
        strict=False,
    ):
        self.strict = strict
        self._send_error = send_error
        self.unified_msg_origin = umo
        self.message_str = "帮我看下这个页面"
        self.plugins_name = list(plugins_name) if plugins_name else None
        self.platform_meta = FakePlatformMeta(name=platform, id=umo.split(":")[0])
        self.message_obj = FakeMessage()
        self.message_obj.message_id = message_id
        self.message_obj.type = umo.split(":")[1]
        self.session_id = umo.split(":")[-1]
        self.sent = []
        self.send_calls = 0
        self.access_log = []

    def __getattr__(self, name):
        if name in self._ALLOWED:
            return object.__getattribute__(self, name)
        log = object.__getattribute__(self, "access_log")
        log.append(name)
        if object.__getattribute__(self, "strict"):
            raise AssertionError(f"后台/投递层碰了原事件的 {name!r}（spec §6 C2）")
        raise AttributeError(name)

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


# ---------------------------------------------------------------------------
# 假 Context / registry / 处理器 / 日志
# ---------------------------------------------------------------------------


class FakeContext:
    """假 ``Context``：只实现投递需要的两件事（``send_message`` + 只读平台表）。

    ``send_ok`` / ``send_result`` 与 ``raise_error`` / ``send_error`` 是同一件事的两种
    叫法（两个用例文件历史命名不同，这里都收）。``on_send`` 给"发送顺序"用例用，
    ``skipped`` 用来造"返回 True 但其实没发"（§4.1 的语义用例）。
    """

    def __init__(
        self,
        *,
        platform_name="aiocqhttp",
        platform_id="aiocqhttp-1",
        has_platform=True,
        platform_insts=None,
        send_ok=None,
        send_result=None,
        raise_error=None,
        send_error=None,
        on_send=None,
    ):
        self.send_calls = []
        self.skipped = 0
        self._send_result = True
        if send_ok is not None:
            self._send_result = bool(send_ok)
        if send_result is not None:
            self._send_result = bool(send_result)
        self._send_error = raise_error if raise_error is not None else send_error
        self.on_send = on_send
        if platform_insts is not None:
            insts = list(platform_insts)
        elif has_platform and platform_name:
            insts = [FakePlatformInst(FakePlatformMeta(name=platform_name, id=platform_id))]
        else:
            insts = []
        self.platform_manager = type("PM", (), {"platform_insts": insts})()

    async def send_message(self, umo, chain):
        self.send_calls.append((umo, list(getattr(chain, "chain", []) or [])))
        if self.on_send is not None:
            self.on_send()
        if self._send_error is not None:
            raise self._send_error
        return self._send_result

    def skip_next(self) -> None:
        """下一次 ``send_message`` 记一次"平台侧跳过"（仍按核心语义返回 True）。"""
        self.skipped += 1


class FakeHandler:
    def __init__(self, name, fn, *, module="data.plugins.probe.main", plugin="plugin_a",
                 priority=0, enabled=True, activated=True):
        self.handler_name = name
        self.handler_module_path = module
        self.plugin_name = plugin
        self.handler = fn
        self.enabled = enabled
        self.activated = activated
        self.extras_configs = {"priority": priority}


class FakeRegistry:
    """按核心语义过滤：事件类型（按 ``name``）/ 插件白名单（``None`` 与 ``["*"]`` 全放行）。"""

    def __init__(self, decorating=(), after_sent=()):
        self.decorating = list(decorating)
        self.after_sent = list(after_sent)
        self.calls = []

    def get_handlers_by_event_type(self, event_type, only_activated=True, plugins_name=None):
        hook = str(getattr(event_type, "name", event_type))
        self.calls.append(
            {"hook": hook, "plugins_name": None if plugins_name is None else list(plugins_name)}
        )
        if hook == "OnDecoratingResultEvent":
            pool = self.decorating
        elif hook == "OnAfterMessageSentEvent":
            pool = self.after_sent
        else:
            return []
        selected = [
            handler
            for handler in pool
            if plugins_name is None
            or plugins_name == ["*"]
            or handler.plugin_name in plugins_name
        ]
        # 核心按 ``extras_configs["priority"]`` **降序**跑（大者先）；我们不排序、原样用
        selected.sort(key=lambda item: int((item.extras_configs or {}).get("priority", 0)), reverse=True)
        return selected


class RecordingLog:
    """记录 ``(level, text)`` 的日志器替身（取代 caplog，避免受 root logger 配置影响）。"""

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


# ---------------------------------------------------------------------------
# 装配助手
# ---------------------------------------------------------------------------


def make_framework(registry=None):
    return active_send.Framework(
        event_cls=SyntheticEvent,
        message_cls=FakeMessage,
        group_cls=FakeGroup,
        member_cls=FakeMember,
        session_cls=FakeSession,
        platform_meta_cls=FakePlatformMeta,
        chain_cls=FakeChain,
        result_cls=FakeResult,
        content_type_cls=FakeContentType,
        plain_cls=FakePlain,
        registry=registry if registry is not None else FakeRegistry(),
        event_types={
            active_send.HOOK_DECORATING: "OnDecoratingResultEvent",
            active_send.HOOK_AFTER_SENT: "OnAfterMessageSentEvent",
        },
    )


def install_framework(monkeypatch, registry=None):
    """把 ``active_send.load_framework`` 换成假框架（生产代码不留测试开关）。"""
    framework = make_framework(registry)
    monkeypatch.setattr(active_send, "load_framework", lambda: framework)
    return framework


def snapshot(**overrides):
    data = {
        "umo": UMO,
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


def plugin_logger(monkeypatch) -> "RecordingLog":
    """把 ``main.py`` 的模块级 ``logger`` 换成 :class:`RecordingLog` 并返回它。

    ``from astrbot.api import logger`` 在测试里是 no-op（生产是 loguru，不进 stdlib），
    所以插件层的日志进不了 ``caplog``；项目里已有同样做法（``test_v175_qa_recheck``）。
    投递层拿到的就是 ``main.logger``（``_deliver_terminal`` 传 ``log=logger``），
    因此这条注入能同时看到投递层的 WARN/INFO。
    """
    log = RecordingLog()
    monkeypatch.setattr(main_module, "logger", log)
    return log


def make_plugin(tmp_path, context):
    """装配一个可投递的插件替身（不建浏览器、不联网）。"""
    plugin = helpers._bare_plugin(tmp_path)
    plugin.context = context
    plugin.background_mode = "browse_only"
    return plugin


def deliver_once(plugin, event, job, *, body="【后台任务完成】正文", status="ok"):
    """直接调用生产投递入口（快照 + 回退句柄由 ``_terminal_deliverer`` 固定）。"""
    plugin._watchdog.registry.register(job)
    snap = active_send.snapshot_from_event(event, umo=plugin._umo_of(event))
    deliverer = plugin._terminal_deliverer(snap, event.send)
    return asyncio.run(deliverer(job, status, body))


async def wait_handle(handle, *, timeout=3.0):
    """等到这个 handle 收尾（``_finalize`` 只跑一次；不依赖 registry 残留）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if handle.finalized:
            await asyncio.sleep(0)
            return handle
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {handle.task_id} 没有在 {timeout}s 内收尾")
