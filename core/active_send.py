"""后台终态投递适配层（v1.7.6）：把「能发出去」升级为「与前台回复同权」。

本模块是 L1 + L2 的唯一实现处（契据 ``spec/20261027-v176-delivery-parity-spec.md``
§3–§6），只做五件事：合成事件（:func:`build_proactive_event`）、钩子重放
（:func:`replay_hooks`）、形态判定（:func:`classify_after_decorators`）、平台矩阵
（:func:`choose_channel`）、编排（:func:`deliver`）。

**合成事件**（§5.1）：一个「只为发送而生」的 ``AstrMessageEvent`` 壳 ——
``message_str`` 给触发文本，``message_obj.message`` / ``message_str`` /
``raw_message`` 诚实留空（``[]`` / ``""`` / ``None``，不假装拥有原始消息），
``plugins_name`` 原样继承，结果类型钉成 ``LLM_RESULT``（§5.5）。

**钩子重放**（§5.2）：``on_decorating_result`` 先跑、``on_after_message_sent`` 在
发送之后跑；过滤交给 registry 的 ``plugins_name``，顺序不自己排（registry 已按
``-priority`` 稳定排序），单钩子异常只记日志不中断，``CancelledError`` 原样上抛，
另加 10s/个、20s/段 的超时（已知边界 B-8）。

**编排**（§3.2 步骤 1-9）：步骤 0（``note_delivery`` 硬闸）由调用方在调用前完成 ——
本模块**不重试、不补发**（「不重」优先于「不丢」）。

记账（:class:`DeliveryOutcome`，对应 §4.4 与 §5.3 的不变量）：

* ``attempted``：进入了发送阶段（有内容要发，或装饰方已经经 ``event.send`` 发过）；
  装饰方压制（形态 C/C′/D）为 ``False``。
* ``dispatched``：**真的调用过某条通道**。与 ``attempted`` 分开是诚实的需要：
  平台闸拦下时"尝试过但没有通道可调"。
* ``accepted_by_platform``：**通道报告成功** —— ``Context.send_message`` 返回真，或
  回退句柄没抛错。核心 docstring 逐字写着「Returns: 是否找到匹配的平台」
  （``star/context.py:667``）：这个 True **不代表用户收到**（qq_official 群场景可能
  静默跳过），所以字段名不叫 ``delivered``。
* ``succeeded``：整体判定（本版恒等于 ``accepted_by_platform``；单列是为了让调用方
  不必自己解释上一条的语义，也给将来「装饰方另行发送成功但我们看不见」的形态留位）。
* ``error``：**第一处**失败/跳过原因；``""`` 只表示「通道没有报告任何失败」。钩子的
  异常/超时记在 ``hook_errors``（并 WARN），**不污染** ``error`` —— 否则不变量 4
  会被钩子问题打破（契据 §5.2 说超时写 ``receipt.error``；此处按 §5.3 的不变量优先，
  两者都可见、只是分字段）。
* ``sent_via``：``context.send_message`` / ``platform_fallback`` / ``decorator`` / ``none``。

四条不变量由 :func:`fold_outcome` 收敛（不靠调用方自觉）：

1. ``attempted=False ⇒ accepted_by_platform=False 且 error != ""``
2. ``accepted_by_platform=True ⇒ sent_via ∈ {context.send_message, platform_fallback, decorator}``
3. ``sent_via="decorator" ⇒ 装饰方确实经 event.send 发过``（``side_send_started``）
4. ``error="" ⇔ 通道未报告任何失败``

**生命周期纪律（§6）**：本模块不持有原事件、不读 ``raw_message``、不调平台能力
（``get_group`` / ``bot.api.*``）；唯一允许跨轮持有的是提交时抓下来的
``fallback_send`` 绑定方法，且只在 :func:`deliver` 里用一次。返回后合成事件即出栈：
不进全局表、不进 job 快照、:class:`DeliveryOutcome` 里没有事件引用。

**导入纪律（§3.1）**：模块顶层**只 import 标准库**；``astrbot`` 符号全部在
:func:`load_framework` 里惰性解析。测试注入 :class:`Framework` 替身即可在没有真
``astrbot`` 的环境里跑执行级用例（``tests/test_v176_active_send.py``）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

HOOK_TIMEOUT_S = 10.0  #: 单钩子超时（核心无超时，这是我们加的，§5.2 / B-8）
HOOK_TOTAL_TIMEOUT_S = 20.0  #: 一次钩子重放的总超时
DELIVERY_TOTAL_TIMEOUT_S = 30.0  #: 整次投递总超时（§6 C6：超时记 DeliveryTimeout、不重试）
TRIGGER_TEXT_LIMIT = 500  #: 合成事件 message_str 上限（§5.1：≤500 字、单行）

#: 通道标记（``DeliveryOutcome.sent_via`` 与 §4.4 日志文案的取值域）
CHANNEL_CONTEXT = "context.send_message"
CHANNEL_FALLBACK = "platform_fallback"
CHANNEL_DECORATOR = "decorator"
CHANNEL_NONE = "none"
#: ``accepted_by_platform=True`` 时允许出现的 sent_via（不变量 2）
SENT_VIA_CHANNELS = (CHANNEL_CONTEXT, CHANNEL_FALLBACK, CHANNEL_DECORATOR)

HOOK_DECORATING = "on_decorating_result"
HOOK_AFTER_SENT = "on_after_message_sent"

#: qq 官方系：L1 **优先**（``post_group_message`` 不需要入站 ``raw_message``），但群场景
#: 可能静默跳过（``qqofficial_platform_adapter.py:383-400``）⇒ 带非权威提示、不判失败。
QQ_OFFICIAL_PLATFORMS = ("qq_official", "qq_official_webhook")
QQ_OFFICIAL_GROUP_HINT = "qq_official_group_scene_unset"

SHAPE_A = "A"  #: 只改链（含分段插件"末段留链"）⇒ 用改写后的链走 L1
SHAPE_B = "B"  #: 装饰方已发 + stop ⇒ 认成功、不重发（sent_via=decorator）
SHAPE_B_PRIME = "B'"  #: 装饰方 send 抛错后 stop ⇒ 记 SideSend:<code>、不重发
SHAPE_C = "C"  #: 清空链 + stop（未发）⇒ DecoratorSuppressed
SHAPE_C_PRIME = "C'"  #: 表外补记：stop 但链非空且没发（核心同样不发）⇒ DecoratorStopped
SHAPE_D = "D"  #: 清空链、不 stop（未发）⇒ DecoratorSuppressed

ERROR_DECORATOR_SUPPRESSED = "DecoratorSuppressed"
ERROR_DECORATOR_STOPPED = "DecoratorStopped"
ERROR_PLATFORM_NOT_FOUND = "PlatformNotFound"
ERROR_BAD_SESSION = "BadSession"
ERROR_NO_FALLBACK = "NoFallbackChannel"
ERROR_DELIVERY_TIMEOUT = "DeliveryTimeout"
ERROR_UNACCOUNTED = "DeliveryUnaccounted"

_GROUP_TYPE_VALUES = ("groupmessage", "group_message", "group")
#: 会重放 on_after_message_sent 的形态（§5.4：C/C′/D 与构造失败都不跑）
_AFTER_SENT_SHAPES = (SHAPE_A, SHAPE_B, SHAPE_B_PRIME)


class FrameworkUnavailable(RuntimeError):
    """``astrbot`` 符号不可用（没有核心、或核心版本缺类）时由 :func:`load_framework` 抛。"""


@dataclass(frozen=True)
class DeliveryOutcome:
    """一次投递的账（§4.4 / §5.3）。字段语义见模块 docstring 的记账段。"""

    attempted: bool = False
    succeeded: bool = False
    accepted_by_platform: bool = False
    error: str = ""
    sent_via: str = CHANNEL_NONE
    dispatched: bool = False
    shape: str = ""
    gate: str = ""
    hint: str = ""
    platform: str = ""
    handlers: int = 0
    hook_errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    task_id: str = ""
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        """本版 ``succeeded`` 恒等于 ``accepted_by_platform``：直接构造也不许"没接受却说成功"。"""
        if self.succeeded != self.accepted_by_platform:
            object.__setattr__(self, "succeeded", bool(self.accepted_by_platform))

    @property
    def suppressed(self) -> bool:
        """装饰方把消息吃掉了（形态 C/C′/D）：不是"发失败"，是"没人要发"。"""
        return not self.attempted

    def as_log_fields(self) -> str:
        """单行日志用的字段串（**不带**正文/URL/用户标识明文，§4.4 末段）。"""
        return (
            f"attempted={int(self.attempted)} accepted={int(self.accepted_by_platform)} "
            f"error={self.error or '-'} sent_via={self.sent_via} shape={self.shape or '-'}"
        )


DeliveryReceipt = DeliveryOutcome
"""契据 §3.1 / §14-F-1 里的旧名（``TerminalOutcome.delivery`` 用），与上面同一个类。"""


@dataclass(frozen=True)
class DeliverySnapshot:
    """提交时的一次性快照（§6 C1 白名单，穷举）：后台只读它，不再触碰原事件。"""

    umo: str
    platform_name: str = ""
    platform_id: str = ""
    message_type: str = ""
    group_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    self_id: str = ""
    plugins_name: tuple[str, ...] | None = None
    trigger_message_id: str = ""
    trigger_text: str = ""
    session_id: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> DeliverySnapshot:
        """从 dict / 任意带同名属性的快照对象构造（容错：缺字段取默认）。"""
        return cls(**{f.name: _snapshot_value(f.name, data.get(f.name)) for f in fields(cls)})


@dataclass(frozen=True)
class ChannelPlan:
    """通道矩阵的判定结果（纯数据，不发消息）：§4.2 一行一结论。

    ``primary`` 是**先走谁**；``fallback`` 是"还有没有第二条腿"（§4.2 的「回退」列）：
    平台找不到 / umo 不可解析时 ``primary`` 本身就是 ``platform_fallback``，故
    ``fallback=None``（没有更后面的一条腿）。
    """

    primary: str
    fallback: str | None = CHANNEL_FALLBACK
    error: str = ""
    hint: str = ""
    platform_name: str = ""
    message_type: str = ""

    @property
    def uses_l1(self) -> bool:
        return self.primary == CHANNEL_CONTEXT


@dataclass(frozen=True)
class ShapeDecision:
    """装饰钩子跑完后的形态判定（§5.3 的四种 + B′ + 补记的 C′）。"""

    shape: str
    should_send: bool
    attempted: bool
    sent_via: str = CHANNEL_NONE
    error: str = ""
    note: str = ""


@dataclass(frozen=True)
class HookRunReport:
    """一次钩子重放的执行报告（谁跑过、谁被过滤、谁异常/超时）。"""

    hook: str
    handlers: int = 0
    ran: int = 0
    stopped: bool = False
    errors: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    total_timeout: bool = False
    skipped: str = ""


@dataclass(frozen=True)
class Framework:
    """``astrbot`` 符号集合（**测试注入点**，生产由 :func:`load_framework` 填充）。

    字段与核心类一一对应，不多造概念；``event_cls`` 的契约是
    ``__init__(*, context, umo, message_str, message_obj, platform_meta, session_id)``，
    并带 ``side_send_started`` / ``side_send_error`` / ``send`` / ``plugins_name`` /
    ``set_result`` / ``get_result`` / ``stop_event`` / ``is_stopped``。
    """

    event_cls: Any
    message_cls: Any
    group_cls: Any
    member_cls: Any
    session_cls: Any
    platform_meta_cls: Any
    chain_cls: Any
    result_cls: Any
    content_type_cls: Any
    plain_cls: Any
    registry: Any = None
    event_types: Mapping[str, Any] | None = None


@lru_cache(maxsize=1)
def load_framework() -> Framework:
    """惰性解析真 ``astrbot`` 符号（模块顶层不 import astrbot，§3.1）。"""
    try:
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import (
            MessageChain,
            MessageEventResult,
            ResultContentType,
        )
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import (
            AstrBotMessage,
            Group,
            MessageMember,
        )
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
    except Exception as exc:  # noqa: BLE001 — 缺核心时调用方走 BuildEvent 分支
        raise FrameworkUnavailable(f"astrbot 符号不可用（{type(exc).__name__}: {exc}）") from exc

    class ProactiveEvent(AstrMessageEvent):
        """合成事件：只为发送而生（§5.1），``send`` 记账后委托给 L1。"""

        def __init__(self, *, context, umo, message_str, message_obj, platform_meta, session_id):
            super().__init__(
                message_str=message_str,
                message_obj=message_obj,
                platform_meta=platform_meta,
                session_id=session_id,
            )
            self.side_send_started = False
            self.side_send_error = ""
            self._proactive_context = context
            self._proactive_umo = umo

        async def send(self, message: Any) -> None:
            """装饰方/钩子里的 ``event.send``：走 L1 并记 side-send 账（§5.3 形态 B）。"""
            self.side_send_started = True
            self._has_send_oper = True
            components = list(getattr(message, "chain", None) or [])
            try:
                found = await self._proactive_context.send_message(
                    self._proactive_umo, MessageChain(chain=components)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 记 code 后交给钩子层继续
                self.side_send_error = type(exc).__name__
                raise
            if not found:
                self.side_send_error = ERROR_PLATFORM_NOT_FOUND
            return None

        async def send_streaming(self, generator: Any, use_fallback: bool = False) -> None:
            """后台终态不是流式：装饰方走流式口时摊平成普通发送（不静默路由）。"""
            async for chunk in generator:
                await self.send(chunk)

    return Framework(
        event_cls=ProactiveEvent,
        message_cls=AstrBotMessage,
        group_cls=Group,
        member_cls=MessageMember,
        session_cls=MessageSession,
        platform_meta_cls=PlatformMetadata,
        chain_cls=MessageChain,
        result_cls=MessageEventResult,
        content_type_cls=ResultContentType,
        plain_cls=Plain,
        registry=star_handlers_registry,
        event_types={
            HOOK_DECORATING: EventType.OnDecoratingResultEvent,
            HOOK_AFTER_SENT: EventType.OnAfterMessageSentEvent,
        },
    )


# ---------------------------------------------------------------------------
# 纯函数：文本 / 平台矩阵 / 形态判定 / 记账折叠
# ---------------------------------------------------------------------------


def shorten_text(text: Any, limit: int = TRIGGER_TEXT_LIMIT) -> str:
    """单行化 + 截断（§5.1：``message_str`` ≤500 字、去换行）。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit > 0 and len(flat) > limit:
        return flat[: max(0, limit - 1)] + "…"
    return flat


def _message_type_value(message_type: Any) -> str:
    return str(getattr(message_type, "value", message_type) or "").strip()


def _is_group_message(message_type: Any) -> bool:
    return _message_type_value(message_type).strip().lower() in _GROUP_TYPE_VALUES


def _coerce_plugins(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(item) for item in raw)


def _snapshot_value(name: str, value: Any) -> Any:
    """快照字段归一（§6 C1）：文本单行截断、插件名归一、其余转 str。"""
    if name == "trigger_text":
        return shorten_text(value)
    if name == "plugins_name":
        return _coerce_plugins(value)
    if name == "message_type":
        return _message_type_value(value)
    return str(value or "")


def choose_channel(
    platform_name: Any, message_type: Any = "", *, platform_found: bool = True
) -> ChannelPlan:
    """平台矩阵（§4.2）：主通道 / 回退 / 提示，纯函数、不发消息、不抛错。

    * 平台实例找不到（或平台名为空）⇒ 只剩 ``event.send`` 回退句柄，
      ``error="PlatformNotFound"``（绝不静默，§4.4）；
    * qq 官方系 ⇒ **L1 优先**（实测推翻"不支持"的旧结论，§5.5-3），群场景带
      :data:`QQ_OFFICIAL_GROUP_HINT` 非权威提示，``error`` 留空；
    * 其余平台（aiocqhttp / webchat / telegram / …）⇒ L1 优先 + 回退兜住。
    """
    raw = str(platform_name or "").strip()
    name = raw.lower()
    kind = _message_type_value(message_type)
    if not platform_found or not name:
        return ChannelPlan(
            primary=CHANNEL_FALLBACK,
            fallback=None,
            error=ERROR_PLATFORM_NOT_FOUND,
            platform_name=raw,
            message_type=kind,
        )
    group_qq = name in QQ_OFFICIAL_PLATFORMS and _is_group_message(kind)
    return ChannelPlan(
        primary=CHANNEL_CONTEXT,
        fallback=CHANNEL_FALLBACK,
        hint=QQ_OFFICIAL_GROUP_HINT if group_qq else "",
        platform_name=raw,
        message_type=kind,
    )


def _decide(
    shape: str,
    should_send: bool,
    attempted: bool,
    *,
    sent_via: str = CHANNEL_NONE,
    error: str = "",
    note: str = "",
) -> ShapeDecision:
    return ShapeDecision(
        shape=shape,
        should_send=should_send,
        attempted=attempted,
        sent_via=sent_via,
        error=error,
        note=note,
    )


def classify_after_decorators(
    chain: Sequence[Any] | None,
    *,
    stopped: bool,
    side_started: bool,
    side_err: str = "",
) -> ShapeDecision:
    """装饰钩子跑完后的形态判定（§5.3 表，纯函数、**不做**任何发送）。

    ``chain`` / ``stopped`` / ``side_started`` / ``side_err`` 就是表里那四个观测量。
    注意 **A 与 A′ 在可观测状态上等价**：装饰方若绕开 ``event.send`` 直接用
    ``Context.send_message`` 发前 N 段（``splitter`` 常规路径），我们看不见 ⇒ 统一按
    A 处置（发剩余链），A′ 只作为理由记在 ``note`` 里。
    """
    pending = list(chain or [])
    if stopped and side_started:
        if side_err:
            return _decide(
                SHAPE_B_PRIME,
                False,
                True,
                sent_via=CHANNEL_DECORATOR,
                error=f"SideSend:{side_err}",
                note="装饰方 send 失败后 stop：记 B′，不再重发",
            )
        return _decide(
            SHAPE_B,
            False,
            True,
            sent_via=CHANNEL_DECORATOR,
            note="装饰方已发并 stop：认成功、不重发（§5.3 形态 B）",
        )
    if stopped:
        if pending:
            return _decide(
                SHAPE_C_PRIME,
                False,
                False,
                error=ERROR_DECORATOR_STOPPED,
                note="装饰方 stop 但没发、链非空：核心同样不发，记 DecoratorStopped 不静默",
            )
        return _decide(
            SHAPE_C,
            False,
            False,
            error=ERROR_DECORATOR_SUPPRESSED,
            note="清空链 + stop：装饰方压制",
        )
    if not pending:
        return _decide(
            SHAPE_D,
            False,
            False,
            error=ERROR_DECORATOR_SUPPRESSED,
            note="清空链、未 stop：装饰方压制（§5.3 形态 D）",
        )
    note = (
        "装饰方已发过一部分（event.send）且留了链：继续发剩余链"
        if side_started
        else "只改链 / A′（侧发不可见）：发改写后的链"
    )
    return _decide(SHAPE_A, True, True, sent_via=CHANNEL_CONTEXT, note=note)


def fold_outcome(
    *,
    decision: ShapeDecision,
    accepted: bool = False,
    dispatched: bool = False,
    sent_via: str = CHANNEL_NONE,
    error: str = "",
    hook_errors: Sequence[str] = (),
    notes: Sequence[str] = (),
    gate: str = "",
    hint: str = "",
    platform: str = "",
    handlers: int = 0,
    task_id: str = "",
    elapsed_s: float = 0.0,
) -> DeliveryOutcome:
    """把判定与发送结果折成 :class:`DeliveryOutcome`，并**强制**四条不变量（§5.3）。"""
    note_list = list(notes)
    attempted = bool(decision.attempted)
    # 调用方没给 sent_via（或缺省成 none）时沿用形态判定给的那条腿
    explicit = sent_via if sent_via and sent_via != CHANNEL_NONE else ""
    via = str(explicit or decision.sent_via or CHANNEL_NONE)
    err = str(error or "")
    ok = bool(accepted)
    sent = bool(dispatched)

    if not attempted:
        ok = sent = False
        via = CHANNEL_NONE
        err = err or str(decision.error or ERROR_DECORATOR_SUPPRESSED)
    else:
        err = err or str(decision.error or "")
        if ok and via not in SENT_VIA_CHANNELS:
            note_list.append(f"AccountingViolation:{via}")
            ok, via, err = False, CHANNEL_NONE, err or ERROR_UNACCOUNTED
        if not sent and via in (CHANNEL_CONTEXT, CHANNEL_FALLBACK):
            note_list.append("SentViaWithoutDispatch")
            ok, via, err = False, CHANNEL_NONE, err or ERROR_UNACCOUNTED
        if via == CHANNEL_DECORATOR and decision.sent_via != CHANNEL_DECORATOR:
            note_list.append("DecoratorWithoutSideSend")
            ok, via, err = False, CHANNEL_NONE, err or ERROR_UNACCOUNTED
        if not ok and not err:
            err = ERROR_UNACCOUNTED

    return DeliveryOutcome(
        attempted=attempted,
        succeeded=ok,
        accepted_by_platform=ok,
        error=err,
        sent_via=via,
        dispatched=sent,
        shape=decision.shape,
        gate=gate,
        hint=hint,
        platform=platform,
        handlers=int(handlers),
        hook_errors=tuple(hook_errors),
        notes=tuple(note_list),
        task_id=str(task_id or ""),
        elapsed_s=float(elapsed_s),
    )


# ---------------------------------------------------------------------------
# 提交时（原事件还活着）：快照 + 链构造；平台探测（只读，不调平台能力）
# ---------------------------------------------------------------------------


def snapshot_from_event(event: Any, umo: str = "") -> DeliverySnapshot:
    """提交时从**还活着**的事件抓快照（§6 C1/C4）：之后后台只读这份快照。"""
    message_obj = getattr(event, "message_obj", None)
    group = getattr(message_obj, "group", None)
    sender = getattr(message_obj, "sender", None)
    meta = getattr(event, "platform_meta", None)
    return DeliverySnapshot.from_mapping(
        {
            "umo": umo or getattr(event, "unified_msg_origin", "") or "",
            "platform_name": getattr(meta, "name", "") or "",
            "platform_id": getattr(meta, "id", "") or "",
            "message_type": getattr(message_obj, "type", ""),
            "group_id": getattr(group, "group_id", "") or "",
            "sender_id": getattr(sender, "user_id", "") or "",
            "sender_name": getattr(sender, "nickname", "") or "",
            "self_id": getattr(message_obj, "self_id", "") or "",
            "plugins_name": getattr(event, "plugins_name", None),
            "trigger_message_id": getattr(message_obj, "message_id", "") or "",
            "trigger_text": getattr(event, "message_str", "") or "",
            "session_id": getattr(event, "session_id", "") or "",
        }
    )


def build_chain(
    text: Any = "", components: Sequence[Any] = (), *, framework: Framework | None = None
) -> list[Any]:
    """终态链：``[Plain(text)] + components``（附件组件由调用方造）。"""
    chain: list[Any] = []
    if str(text or ""):
        chain.append((framework or load_framework()).plain_cls(str(text)))
    chain.extend(components)
    return chain


def _coerce_snapshot(snapshot: Any) -> DeliverySnapshot:
    if isinstance(snapshot, DeliverySnapshot):
        return snapshot
    if isinstance(snapshot, Mapping):
        return DeliverySnapshot.from_mapping(snapshot)
    data = {
        f.name: getattr(snapshot, f.name)
        for f in fields(DeliverySnapshot)
        if hasattr(snapshot, f.name)
    }
    return DeliverySnapshot.from_mapping(data)


def _meta_of(platform_inst: Any) -> Any:
    meta_fn = getattr(platform_inst, "meta", None)
    if not callable(meta_fn):
        return None
    try:
        return meta_fn()
    except Exception:  # noqa: BLE001 — 探测失败不判失败（§4.2 qq_official 行）
        return None


def platform_lookup(context: Any, platform_id: str) -> tuple[Any, Any]:
    """返回 ``(平台实例, meta)``；找不到返回 ``(None, None)``（只读探测）。"""
    key = str(platform_id or "")
    if not key:
        return None, None
    getter = getattr(context, "get_platform_inst", None)
    if callable(getter):
        try:
            inst = getter(key)
        except Exception:  # noqa: BLE001
            inst = None
        if inst is not None:
            return inst, _meta_of(inst)
    manager = getattr(context, "platform_manager", None)
    for inst in list(getattr(manager, "platform_insts", None) or []):
        meta = _meta_of(inst)
        if str(getattr(meta, "id", "") or "") == key:
            return inst, meta
    return None, None


def probe_splitter_patch(context: Any) -> bool:
    """只读探测 ``splitter`` 的 ``Context.send_message`` 补丁是否装着（§5.6，仅用于日志）。"""
    try:
        return bool(getattr(context, "_splitter_send_patched", False))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 合成事件 + 钩子重放
# ---------------------------------------------------------------------------


def build_proactive_event(
    context: Any,
    snapshot: Any,
    chain: Sequence[Any] | None = None,
    *,
    framework: Framework | None = None,
    platform_meta: Any = None,
    session: Any = None,
) -> Any:
    """按 §5.1 逐字段合成事件；任何一步失败都由调用方记 ``BuildEvent:<Type>``。"""
    fw = framework or load_framework()
    snap = _coerce_snapshot(snapshot)
    session = session if session is not None else fw.session_cls.from_str(snap.umo)
    meta = platform_meta
    if meta is None:
        platform_id = str(getattr(session, "platform_name", "") or "")
        meta = fw.platform_meta_cls(
            name=snap.platform_name or platform_id, description="", id=platform_id
        )
    message_obj = fw.message_cls()
    message_obj.type = session.message_type
    message_obj.session_id = session.session_id
    message_obj.message_id = snap.trigger_message_id or ""
    message_obj.self_id = snap.self_id or ""
    message_obj.group = (
        fw.group_cls(group_id=snap.group_id)
        if _is_group_message(session.message_type) and snap.group_id
        else None
    )
    message_obj.sender = fw.member_cls(
        user_id=snap.sender_id or "", nickname=snap.sender_name or None
    )
    message_obj.message = []
    message_obj.message_str = ""
    message_obj.raw_message = None
    event = fw.event_cls(
        context=context,
        umo=snap.umo,
        message_str=shorten_text(snap.trigger_text),
        message_obj=message_obj,
        platform_meta=meta,
        session_id=session.session_id,
    )
    event.plugins_name = list(snap.plugins_name) if snap.plugins_name is not None else None
    result = fw.result_cls(chain=list(chain or []))
    result.set_result_content_type(fw.content_type_cls.LLM_RESULT)
    event.set_result(result)
    return event


def _handler_label(handler: Any) -> str:
    module = str(getattr(handler, "handler_module_path", "") or "?")
    name = str(getattr(handler, "handler_name", "") or getattr(handler, "name", "") or "?")
    return f"{module}.{name}"


def _event_type_for(hook: str, framework: Framework | None) -> Any:
    if framework is not None and framework.event_types:
        event_type = framework.event_types.get(hook)
        if event_type is not None:
            return event_type
    return hook


def _select_handlers(registry: Any, event_type: Any, plugins_name: Any) -> list[Any]:
    getter = getattr(registry, "get_handlers_by_event_type", None)
    if not callable(getter):
        return []
    try:
        handlers = getter(event_type, plugins_name=plugins_name)
    except TypeError:
        handlers = getter(event_type)
    except Exception:  # noqa: BLE001 — registry 异常不拖垮投递
        return []
    return list(handlers or [])


async def _invoke_handler(handler: Any, event: Any) -> None:
    outcome = getattr(handler, "handler", handler)(event)
    if inspect.isawaitable(outcome):
        await outcome


async def replay_hooks(
    event: Any,
    hook: str = HOOK_DECORATING,
    *,
    registry: Any = None,
    framework: Framework | None = None,
    log: Any = None,
    timeout_s: float = HOOK_TIMEOUT_S,
    total_timeout_s: float = HOOK_TOTAL_TIMEOUT_S,
) -> HookRunReport:
    """重放一次事件钩子（§5.2）：过滤交给 registry、顺序不排、异常不中断、超时有上限。"""
    log_ = log if log is not None else logger
    fw = framework
    if registry is None:
        try:
            fw = fw if fw is not None else load_framework()
        except Exception as exc:  # noqa: BLE001
            log_.warning(f"钩子重放跳过 {hook}（astrbot 符号不可用：{type(exc).__name__}）")
            return HookRunReport(hook=hook, skipped="framework-unavailable")
        registry = fw.registry

    plugins_name = getattr(event, "plugins_name", None)
    handlers = _select_handlers(registry, _event_type_for(hook, fw), plugins_name)
    if not handlers and hook == HOOK_DECORATING:
        shown = "*" if plugins_name is None else plugins_name
        log_.warning(f"主动发送未命中任何 {hook} 处理器 plugins_name={shown}")

    ran = 0
    stopped = False
    errors: list[str] = []
    timed_out: list[str] = []
    total_timeout = False

    async def _run() -> None:
        nonlocal ran, stopped
        for handler in handlers:
            label = _handler_label(handler)
            try:
                await asyncio.wait_for(_invoke_handler(handler, event), timeout=float(timeout_s))
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                timed_out.append(f"{hook}:{label}")
                log_.warning(f"钩子超时 {hook} handler={label}（继续下一个）")
            except BaseException:  # noqa: BLE001 — 单钩子异常不中断（核心同款）
                errors.append(f"{hook}:{label}")
                log_.warning(f"钩子异常 {hook} handler={label}（继续下一个）\n{traceback.format_exc()}")
            ran += 1
            if event.is_stopped():
                stopped = True
                break

    try:
        await asyncio.wait_for(_run(), timeout=float(total_timeout_s))
    except TimeoutError:
        total_timeout = True
        log_.warning(f"钩子重放整体超时 {hook} 已完成={ran}/{len(handlers)}")
    return HookRunReport(
        hook=hook,
        handlers=len(handlers),
        ran=ran,
        stopped=stopped,
        errors=tuple(errors),
        timed_out=tuple(timed_out),
        total_timeout=total_timeout,
    )


# ---------------------------------------------------------------------------
# 编排（§3.2 步骤 1-9；步骤 0 的硬闸在调用方）
# ---------------------------------------------------------------------------


def _tag(task_id: str) -> str:
    return str(task_id or "-")[:8] or "-"


async def deliver(
    context: Any,
    snapshot: Any,
    *,
    chain: Sequence[Any] | None = None,
    text: Any = "",
    components: Sequence[Any] = (),
    registry: Any = None,
    fallback_send: Callable[[Any], Awaitable[Any]] | None = None,
    framework: Framework | None = None,
    log: Any = None,
    task_id: str = "",
    status: str = "",
    hook_timeout_s: float = HOOK_TIMEOUT_S,
    hook_total_timeout_s: float = HOOK_TOTAL_TIMEOUT_S,
    total_timeout_s: float = DELIVERY_TOTAL_TIMEOUT_S,
) -> DeliveryOutcome:
    """投递一条后台终态（``main.py`` 的 ``_deliver_terminal`` 调这个）。

    顺序即契约（§3.2）：合成事件 → 重放 ``on_decorating_result`` → 形态判定 +
    发送（L1，失败/不可用则回退）→ 条件重放 ``on_after_message_sent`` → 记账。
    **不发消息也不抛错**：所有失败都折成账 + WARN（"不丢"靠可见性保证）。

    Args:
        context: ``Context``（只需 ``send_message`` 与只读的平台表）。
        snapshot: :class:`DeliverySnapshot` / dict / 任意带同名字段的对象。
        chain: 已构造好的终态链；给 ``None`` 时用 ``[Plain(text)] + components``。
        registry: 钩子注册表；``None`` ⇒ 真 ``star_handlers_registry``。
        fallback_send: 提交时抓下的 ``event.send`` 绑定方法（§6 C3，唯一例外），
            形如 ``async def (chain) -> Any``；只在 L1 不可用时调用一次。
        framework: 符号替身（测试注入）；``None`` ⇒ :func:`load_framework`。
        log: 日志器（测试注入）；``None`` ⇒ 模块 logger。
        task_id / status: 只用于日志文案与 :class:`DeliveryOutcome`。
    """
    log_ = log if log is not None else logger
    started = time.monotonic()
    snap = _coerce_snapshot(snapshot)
    payload = (
        list(chain) if chain is not None else build_chain(text, components, framework=framework)
    )
    try:
        outcome = await asyncio.wait_for(
            _deliver_once(
                context,
                snap,
                payload,
                registry=registry,
                fallback_send=fallback_send,
                framework=framework,
                log=log_,
                task_id=task_id,
                hook_timeout_s=float(hook_timeout_s),
                hook_total_timeout_s=float(hook_total_timeout_s),
            ),
            timeout=float(total_timeout_s),
        )
    except TimeoutError:
        outcome = DeliveryOutcome(
            attempted=True,
            error=ERROR_DELIVERY_TIMEOUT,
            platform=snap.platform_name,
            task_id=str(task_id or ""),
            elapsed_s=round(time.monotonic() - started, 3),
        )
        log_.warning(f"后台终态投递超时 task_id={_tag(task_id)}（不重试）")
    _log_outcome(outcome, log_, status=status)
    return outcome


async def _deliver_once(
    context: Any,
    snap: DeliverySnapshot,
    payload: list[Any],
    *,
    registry: Any,
    fallback_send: Callable[[Any], Awaitable[Any]] | None,
    framework: Framework | None,
    log: Any,
    task_id: str,
    hook_timeout_s: float,
    hook_total_timeout_s: float,
) -> DeliveryOutcome:
    started = time.monotonic()
    tag = _tag(task_id)
    notes: list[str] = []
    hook_errors: list[str] = []
    handlers = 0
    platform_name = snap.platform_name
    platform_meta = None
    platform_found = False
    session = None
    session_error = ""

    def failed(error: str, plan: ChannelPlan | None) -> DeliveryOutcome:
        """§3.2 步骤 4 的失败面：``attempted=False``、不发送、只记账。"""
        return DeliveryOutcome(
            attempted=False,
            error=error,
            platform=platform_name,
            gate=plan.primary if plan is not None else "",
            hint=plan.hint if plan is not None else "",
            notes=tuple(notes),
            task_id=str(task_id or ""),
            elapsed_s=round(time.monotonic() - started, 3),
        )

    # 步骤 1：符号 + 会话 + 平台闸（只读探测，不调平台能力）
    try:
        fw = framework if framework is not None else load_framework()
    except Exception as exc:  # noqa: BLE001
        error = f"BuildEvent:{type(exc).__name__}"
        log.warning(f"后台终态合成事件失败 task_id={tag} error={error}")
        return failed(error, None)
    try:
        session = fw.session_cls.from_str(snap.umo)
    except Exception:  # noqa: BLE001 — umo 兜底串（main._umo_of 的 "{group}|{sender}"）
        session_error = ERROR_BAD_SESSION
        notes.append(ERROR_BAD_SESSION)
        log.warning(f"后台终态投递 会话不可解析 task_id={tag} error={ERROR_BAD_SESSION}")
    if not session_error:
        platform_id = snap.platform_id or str(getattr(session, "platform_name", "") or "")
        platform_inst, platform_meta = platform_lookup(context, platform_id)
        platform_found = platform_inst is not None
        if not platform_found:
            notes.append(ERROR_PLATFORM_NOT_FOUND)
            log.warning(f"后台终态投递 平台实例未找到 task_id={tag}（绝不静默）")
        elif not platform_name:
            platform_name = str(getattr(_meta_of(platform_inst), "name", "") or "")
    message_type = snap.message_type or _message_type_value(getattr(session, "message_type", ""))
    if session_error:
        plan = ChannelPlan(
            primary=CHANNEL_FALLBACK,
            fallback=None,
            error=ERROR_BAD_SESSION,
            platform_name=platform_name,
            message_type=message_type,
        )
    else:
        plan = choose_channel(platform_name, message_type, platform_found=platform_found)
    if plan.hint:
        log.warning(f"后台终态投递 平台提示 task_id={tag} hint={plan.hint}（可能静默跳过群消息）")

    # 步骤 4：合成事件（失败 ⇒ 不发送，只记账）
    event = None
    if not session_error:
        try:
            event = build_proactive_event(
                context, snap, payload, framework=fw, platform_meta=platform_meta, session=session
            )
        except Exception as exc:  # noqa: BLE001
            error = f"BuildEvent:{type(exc).__name__}"
            notes.append(error)
            log.warning(f"后台终态合成事件失败 task_id={tag} error={error}")
            return failed(error, plan)

    # 步骤 5：重放 on_decorating_result
    working = list(payload)
    stopped = side_started = False
    side_err = ""
    if event is not None:
        report = await replay_hooks(
            event,
            HOOK_DECORATING,
            registry=registry,
            framework=fw,
            log=log,
            timeout_s=hook_timeout_s,
            total_timeout_s=hook_total_timeout_s,
        )
        handlers = report.handlers
        hook_errors.extend(report.errors)
        hook_errors.extend(report.timed_out)
        result = event.get_result()
        working = list(getattr(result, "chain", None) or [])
        stopped = bool(event.is_stopped())
        side_started = bool(getattr(event, "side_send_started", False))
        side_err = str(getattr(event, "side_send_error", "") or "")

    # 步骤 6：形态判定 + 发送（§5.3）
    decision = classify_after_decorators(
        working, stopped=stopped, side_started=side_started, side_err=side_err
    )
    accepted = dispatched = False
    sent_via = decision.sent_via
    error = decision.error
    if decision.should_send:
        accepted, sent_via, error, dispatched = await _dispatch(
            context,
            snap.umo,
            working,
            plan,
            fallback_send,
            fw,
            log=log,
            notes=notes,
            task_id=task_id,
        )
    elif decision.shape in (SHAPE_B, SHAPE_B_PRIME):
        dispatched = side_started
        accepted = decision.shape == SHAPE_B and not error

    # 步骤 7：条件重放 on_after_message_sent（§5.4）
    if event is not None and decision.shape in _AFTER_SENT_SHAPES:
        after = await replay_hooks(
            event,
            HOOK_AFTER_SENT,
            registry=registry,
            framework=fw,
            log=log,
            timeout_s=hook_timeout_s,
            total_timeout_s=hook_total_timeout_s,
        )
        hook_errors.extend(after.errors)
        hook_errors.extend(after.timed_out)

    # 步骤 8/9：记账；事件对象在返回时即出栈（不进任何全局表）
    return fold_outcome(
        decision=decision,
        accepted=accepted,
        dispatched=dispatched,
        sent_via=sent_via,
        error=error,
        hook_errors=hook_errors,
        notes=notes,
        gate=plan.primary,
        hint=plan.hint,
        platform=platform_name,
        handlers=handlers,
        task_id=task_id,
        elapsed_s=round(time.monotonic() - started, 3),
    )


async def _dispatch(
    context: Any,
    umo: str,
    chain: Sequence[Any],
    plan: ChannelPlan,
    fallback_send: Callable[[Any], Awaitable[Any]] | None,
    framework: Framework,
    *,
    log: Any,
    notes: list[str],
    task_id: str,
) -> tuple[bool, str, str, bool]:
    """发送一次：L1 →（失败/不可用）回退；返回 ``(accepted, sent_via, error, dispatched)``。"""
    tag = _tag(task_id)
    dispatched = False
    error = plan.error
    if probe_splitter_patch(context):
        notes.append("splitter_patch=1")

    def link() -> Any:
        return framework.chain_cls(chain=list(chain))

    if plan.primary == CHANNEL_CONTEXT:
        dispatched = True
        found = False
        try:
            found = bool(await context.send_message(umo, link()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = f"PlatformSendError:{type(exc).__name__}"
            log.warning(f"后台终态 L1 发送抛错 task_id={tag} error={error}（转回退）")
        if found:  # True 只代表「找到匹配的平台」，不代表用户收到（§4.1）
            return True, CHANNEL_CONTEXT, "", dispatched
        if not error:
            error = ERROR_PLATFORM_NOT_FOUND
            log.warning(f"后台终态 L1 未找到平台 task_id={tag}（转回退）")

    wants_fallback = plan.primary == CHANNEL_FALLBACK or plan.fallback == CHANNEL_FALLBACK
    if wants_fallback and fallback_send is not None:
        dispatched = True
        try:
            await fallback_send(link())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            code = f"PlatformSendError:{type(exc).__name__}"
            log.warning(f"后台终态回退发送抛错 task_id={tag} error={code}")
            return False, CHANNEL_FALLBACK, error or code, dispatched
        return True, CHANNEL_FALLBACK, error, dispatched

    notes.append(ERROR_NO_FALLBACK)
    if not error:
        error = ERROR_NO_FALLBACK
        log.warning(f"后台终态投递无可用通道 task_id={tag}")
    return False, CHANNEL_NONE, error, dispatched


def _log_outcome(outcome: DeliveryOutcome, log: Any, *, status: str = "") -> None:
    """§4.4 的四条硬性输出：不静默、不带正文、不带用户标识明文。"""
    tag = _tag(outcome.task_id)
    if not outcome.attempted:
        log.warning(
            f"后台终态未投递 task_id={tag} reason={outcome.error or ERROR_DECORATOR_SUPPRESSED} "
            f"decorators={outcome.handlers} shape={outcome.shape or '-'}"
        )
        return
    if not outcome.accepted_by_platform:
        log.warning(
            f"后台终态投递失败 task_id={tag} status={status or '-'} "
            f"error={outcome.error or ERROR_UNACCOUNTED} sent_via={outcome.sent_via} "
            f"dispatched={int(outcome.dispatched)} shape={outcome.shape or '-'}"
        )
        return
    if outcome.sent_via == CHANNEL_FALLBACK:
        log.info(
            f"后台终态已投递(回退通道) task_id={tag} platform={outcome.platform or '-'} "
            f"fallback=1 attempted=1 accepted=1 sent_via={outcome.sent_via}"
        )
        return
    log.info(
        f"后台终态已投递 task_id={tag} status={status or '-'} elapsed={outcome.elapsed_s:.1f}s "
        f"attempted=1 accepted=1 sent_via={outcome.sent_via}"
    )
