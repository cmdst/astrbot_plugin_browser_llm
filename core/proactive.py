"""core.proactive — 极简同权投递层（v1.8.0；规格 §6）：L1 通道同权 + L2 子集。

**一次逻辑投递**（本层唯一语义，§6.5）：对一条任务终态，``context.send_message`` 的**恰好一次**
调用（或装饰方代发一次）。装饰方把一条消息切成多条、加 TTS、加前缀、改文案，都**不算**第二次
投递。本层**不重试、不补发**：``succeeded=False`` 也是一次"已尝试"的结果。

**交付**（§6.1）：L1 = 从 ``context.send_message(umo, chain)`` 出去（**不是** ``event.send`` 直捅
适配器），平台找不到时**不静默**（WARN + ``PlatformNotFound``）；L2 子集 = 合成 ``AstrMessageEvent``
+ 重放 ``on_decorating_result`` / ``on_after_message_sent`` + ``plugins_name`` 继承 +
``ResultContentType.LLM_RESULT``。**不交付 L3**：内容安全 / 核心分段 / TTS / 前缀 / t2i / 合并转发 /
``At``+``Reply`` 补插 **都不跑** —— 正确表述是"生态插件看得见、通道走正规口；核心 stage 不跑"。
**不做**（§6.8）：平台回退腿（``platform_fallback`` / ``event.send`` 兜底）、补偿式第二条 / 重试、
六形态细分、跨插件识别（探别人插件私有开关）。**生命周期**（§6.7）：后台**不持 event** ——
:class:`DeliveryTarget` 是提交那一刻的只读快照（无 event/context 引用）；合成事件不进任何全局表。
本模块顶层**只** import 标准库与本包 ``core.tasks``（取 ``DeliveryNote``）；``astrbot`` 符号一律在
:func:`_framework` 里惰性 import，失败 ⇒ :class:`FrameworkUnavailable`（降级记账，绝不抛）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from .tasks import DeliveryNote

logger = logging.getLogger(__name__)

HOOK_TIMEOUT_S = 5.0  # 单个钩子超时
HOOK_TOTAL_TIMEOUT_S = 10.0  # 一次重放的总超时
DELIVERY_TIMEOUT_S = 30.0  # 整次投递总超时（超时记 DeliveryTimeout、**不重试**）
TRIGGER_TEXT_LIMIT = 500  # 合成事件 message_str 上限（单行、≤500 字）
CHANNEL_CONTEXT = "context.send_message"
CHANNEL_NONE = "none"
SHAPE_A = "A"  # 装饰方只改链（含"末段留链"）⇒ 用改写后的链走 L1
SHAPE_B = "B"  # 装饰方已发送（置了 _side_send_started / stop_event）⇒ 认成功、不重发
SHAPE_C = "C"  # 装饰方清空链且未发 ⇒ DecoratorSuppressed
ERROR_FRAMEWORK = "FrameworkUnavailable"
ERROR_PLATFORM = "PlatformNotFound"
ERROR_BAD_SESSION = "BadSession"
ERROR_SUPPRESSED = "DecoratorSuppressed"
ERROR_HOOK_TIMEOUT = "HookTimeout"  # "HookTimeout:<plugin>.<handler>"
ERROR_HOOK_FAILED = "HookError"  # "HookError:<plugin>.<handler>"
ERROR_DELIVERY_TIMEOUT = "DeliveryTimeout"
ERROR_EMPTY = "EmptyChain"
_VIDEO_EXTS = ("mp4", "webm", "mov", "mkv")

class FrameworkUnavailable(RuntimeError):
    """拿不到 astrbot 符号（降级路径）。"""

@dataclass(frozen=True)
class DeliveryTarget:
    """提交那一刻的**只读快照**：没有 event、没有 context 引用。"""
    umo: str
    platform_name: str = ""
    platform_id: str = ""
    session_id: str = ""
    message_type: str = ""  # "GroupMessage" / "FriendMessage" / …
    group_id: str = ""
    sender_id: str = ""
    self_id: str = ""
    plugins_name: tuple[str, ...] | None = None
    trigger_text: str = ""  # 触发文本（截断到 TRIGGER_TEXT_LIMIT，单行）
    trigger_message_id: str = ""

    @classmethod
    def from_event(cls, event: Any, umo: str = "") -> "DeliveryTarget":
        """从**提交时还活着**的 event 抽快照；任何字段取不到则留空，绝不抛。"""
        def pick(name: str) -> str:
            try:
                attr = getattr(event, name, None)
                value = attr() if callable(attr) else attr
            except Exception:  # noqa: BLE001 — 快照绝不抛
                return ""
            return str(getattr(value, "value", value) or "") if value is not None else ""

        plugins = getattr(event, "plugins_name", None)
        text = pick("get_message_str") or pick("message_str")
        message_obj = getattr(event, "message_obj", None)
        return cls(
            umo=umo or pick("unified_msg_origin"), platform_name=pick("get_platform_name"),
            platform_id=pick("get_platform_id"),
            session_id=pick("get_session_id") or pick("session_id"),
            message_type=pick("get_message_type"), group_id=pick("get_group_id"),
            sender_id=pick("get_sender_id"), self_id=pick("get_self_id"),
            plugins_name=(tuple(plugins) if isinstance(plugins, (list, tuple)) else None),
            trigger_text=" ".join(str(text).split())[:TRIGGER_TEXT_LIMIT],
            trigger_message_id=pick("get_message_id")
            or str(getattr(message_obj, "message_id", "") or ""))

def snapshot_of(event: Any, umo: str = "") -> DeliveryTarget:
    """``DeliveryTarget.from_event`` 的函数别名（调用点更短）。"""
    return DeliveryTarget.from_event(event, umo)

@dataclass(frozen=True)
class _Framework:
    """惰性取到的 astrbot 符号（**唯一** import 面；测试可整体替身）。"""
    event_cls: Any
    plain: Any
    media: tuple[Any, Any]  # (image_cls, video_cls)
    chain_cls: Any
    result_cls: Any
    message_cls: Any
    metadata_cls: Any
    decorating_type: Any
    after_type: Any
    llm_result: Any

def _framework() -> _Framework:
    """惰性 import astrbot 符号（失败 ⇒ :class:`FrameworkUnavailable`）。"""
    try:
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.message_components import Image, Plain, Video
        from astrbot.core.message.message_event_result import (
            MessageChain, MessageEventResult, ResultContentType)
        from astrbot.core.platform.astrbot_message import AstrBotMessage
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.star.star_handler import EventType
        return _Framework(AstrMessageEvent, Plain, (Image, Video), MessageChain,
                          MessageEventResult, AstrBotMessage, PlatformMetadata,
                          EventType.OnDecoratingResultEvent, EventType.OnAfterMessageSentEvent,
                          ResultContentType.LLM_RESULT)
    except Exception as exc:  # noqa: BLE001 — 核心改名/未安装 ⇒ 降级
        raise FrameworkUnavailable(f"{type(exc).__name__}: {exc}") from exc

def _handlers(event_type: Any, plugins_name: Sequence[str] | None) -> list[Any]:
    """取某类钩子的 handler 列表（``plugins_name`` 继承前台的插件白名单语义）。"""
    if event_type is None:
        return []
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
        return list(star_handlers_registry.get_handlers_by_event_type(
            event_type, plugins_name=list(plugins_name) if plugins_name else None))
    except Exception:  # noqa: BLE001 — 拿不到注册表 ⇒ 当作"没有钩子"
        return []

def _label(meta: Any) -> str:
    """``<plugin>.<handler>``（错误记账用；不探别人插件的业务属性）。"""
    return f"{getattr(meta, 'handler_module_path', '?')}.{getattr(meta, 'handler_name', '?')}"

async def _replay(event_type: Any, event: Any, plugins_name: Sequence[str] | None) -> list[str]:
    """重放一类钩子：单钩子 ≤``HOOK_TIMEOUT_S``、整轮 ≤``HOOK_TOTAL_TIMEOUT_S``。

    单钩子异常/超时只记进返回的错误摘要，**继续下一个**（不中断投递）。
    """
    errors: list[str] = []
    handlers = _handlers(event_type, plugins_name)
    loop, deadline = asyncio.get_running_loop(), None
    for meta in handlers:
        deadline = deadline or (loop.time() + HOOK_TOTAL_TIMEOUT_S)
        remain, label = deadline - loop.time(), _label(meta)
        if remain <= 0:
            errors.append(f"{ERROR_HOOK_TIMEOUT}:{label}")  # 总预算耗尽：剩余钩子不再跑
            break
        try:
            await asyncio.wait_for(meta.handler(event), timeout=min(HOOK_TIMEOUT_S, remain))
        except asyncio.TimeoutError:
            errors.append(f"{ERROR_HOOK_TIMEOUT}:{label}")
        except Exception as exc:  # noqa: BLE001 — 钩子炸了不许拖垮投递
            logger.warning("钩子异常（%s）: %s", label, type(exc).__name__)
            errors.append(f"{ERROR_HOOK_FAILED}:{label}")
    return errors

def _build_chain(fw: _Framework, body: str, media: Sequence[str]) -> list[Any]:
    """合成链：``[Plain(body)] + [Image/Video(每个 media)]``（构造失败 ⇒ 只发文本）。"""
    chain: list[Any] = [fw.plain(str(body))] if str(body or "").strip() else []
    image_cls, video_cls = fw.media
    for path in media:
        ext = str(path).rsplit(".", 1)[-1].lower()
        component = video_cls if ext in _VIDEO_EXTS else image_cls
        try:
            chain.append(component.fromFileSystem(str(path)))
        except Exception:  # noqa: BLE001 — 媒体构造失败不影响文本投递
            logger.warning("媒体组件构造失败，已跳过：%s", path)
    return chain

def _classify(event: Any, chain: list[Any]) -> str:
    """三形态判定（§6.4，**只看我们自己合成事件上的标记**；不探别人插件私有属性）。"""
    if chain:
        return SHAPE_A  # 非空链照发（含 stop：v1.7.6 的 C′ 裁定）
    return SHAPE_B if getattr(event, "_side_send_started", False) else SHAPE_C

def _build_event(fw: _Framework, target: DeliveryTarget, context: Any) -> Any:
    """合成 ``AstrMessageEvent``（``send()`` 覆写成走 ``context.send_message`` 的一腿）。"""

    class _ProactiveEvent(fw.event_cls):  # type: ignore[misc,valid-type]
        _side_send_started = False
        _side_send_error = ""
        _send_stage_count = 0

        def begin_send_stage(self) -> None:
            self._send_stage_count += 1

        async def send(self, message: Any) -> None:
            # **不**调 super().send()：基类只埋点，将来若真发送会与这里双发
            self.begin_send_stage()
            self._side_send_started = True
            if not await self._proactive_context.send_message(self._proactive_umo, message):
                self._side_send_error = ERROR_PLATFORM
                raise RuntimeError(ERROR_PLATFORM)

    message_obj = fw.message_cls()
    message_obj.type = target.message_type or "FriendMessage"
    message_obj.self_id, message_obj.session_id = target.self_id, target.session_id
    message_obj.message_id = target.trigger_message_id
    message_obj.message_str, message_obj.message = target.trigger_text, []
    meta = fw.metadata_cls(name=target.platform_name or "unknown", description="",
                           id=target.platform_id or target.platform_name or "unknown")
    event = _ProactiveEvent(target.trigger_text, message_obj, meta, target.session_id)
    event.unified_msg_origin = target.umo
    event.plugins_name = list(target.plugins_name) if target.plugins_name is not None else None
    event._proactive_context, event._proactive_umo = context, target.umo
    return event

async def _send(context: Any, umo: str, fw: _Framework, chain: list[Any]) -> tuple[bool, str]:
    """L1 主通道：``context.send_message(umo, chain)``；返回 (是否成功, 失败码)。"""
    try:
        sent = await context.send_message(umo, fw.chain_cls(chain=list(chain)))
    except ValueError:  # 核心对不合法 session 抛 ValueError
        return False, ERROR_BAD_SESSION
    except Exception as exc:  # noqa: BLE001 — 适配器/平台异常一律记账，不重试
        logger.warning("context.send_message 抛错：%s", type(exc).__name__)
        return False, ERROR_BAD_SESSION
    return bool(sent), ("" if sent else ERROR_PLATFORM)

async def _deliver_once(target: DeliveryTarget, body: str, context: Any,
                        media: Sequence[str]) -> DeliveryNote:
    """9 步编排（§6.3）：判空 → 建事件 → 装饰 → 判形态 → 发送 → 发送后 → 记账。"""
    if not str(body or "").strip() and not media:
        return DeliveryNote(attempted=False, error=ERROR_EMPTY)  # 不发空消息、不建事件
    try:
        fw = _framework()
    except FrameworkUnavailable as exc:
        logger.warning("投递降级（拿不到框架符号）：%s", exc)
        return DeliveryNote(attempted=True, channel=CHANNEL_NONE, error=ERROR_FRAMEWORK)
    try:
        event = _build_event(fw, target, context)
    except Exception as exc:  # noqa: BLE001 — 合成事件失败 ⇒ 记账，不抛
        logger.warning("合成事件失败：%s", type(exc).__name__)
        return DeliveryNote(attempted=True, channel=CHANNEL_NONE, error=ERROR_BAD_SESSION)
    hooks: list[str] = []
    try:
        result = fw.result_cls()
        result.chain = _build_chain(fw, str(body or ""), media)
        result.set_result_content_type(fw.llm_result)
        event.set_result(result)
        hooks += await _replay(fw.decorating_type, event, target.plugins_name)  # 步骤 3
        chain = list(getattr(event.get_result(), "chain", None) or [])
        shape = _classify(event, chain)  # 步骤 4
        succeeded, error, channel = False, "", CHANNEL_NONE
        if shape == SHAPE_A:
            succeeded, error = await _send(context, target.umo, fw, chain)  # 步骤 5
            channel = CHANNEL_CONTEXT
        elif shape == SHAPE_B:  # 装饰方代发：不再发，认成功（除非它自己报错）
            channel = CHANNEL_CONTEXT
            side_error = str(getattr(event, "_side_send_error", "") or "")
            succeeded, error = not side_error, (f"SideSend:{side_error}" if side_error else "")
        else:  # SHAPE_C：装饰方清空链且没发
            error = ERROR_SUPPRESSED
        hooks += await _replay(fw.after_type, event, target.plugins_name)  # 步骤 6
    finally:
        event.clear_result()  # 步骤 8：不留引用（事件对象随函数返回回收）
    if error == ERROR_PLATFORM:
        logger.warning("投递未找到平台（不重试）：umo=%s", target.platform_id or "unknown")
    return DeliveryNote(attempted=True, succeeded=succeeded, channel=channel, shape=shape,
                        error=error, hook_errors=tuple(hooks))

async def deliver(target: DeliveryTarget, body: str, *, context: Any,
                  media: Sequence[str] = (), diagnostics: bool = False) -> DeliveryNote:
    """把一条终态正文投递到 ``target.umo``（**只调用一次、不重试、不补发**）。

    任何异常都收敛成 :class:`DeliveryNote`（不抛给调用方）；整次投递受
    ``DELIVERY_TIMEOUT_S`` 约束（超时记 ``DeliveryTimeout``）。
    """
    del diagnostics  # 保留参数只为调用点对齐（诊断行由渲染器负责）
    try:
        return await asyncio.wait_for(_deliver_once(target, body, context, tuple(media)),
                                      timeout=DELIVERY_TIMEOUT_S)
    except asyncio.TimeoutError:
        return DeliveryNote(attempted=True, channel=CHANNEL_CONTEXT, error=ERROR_DELIVERY_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — 投递层绝不把异常抛给任务簿
        logger.warning("投递异常：%s", type(exc).__name__)
        return DeliveryNote(attempted=True, channel=CHANNEL_NONE,
                            error=f"{ERROR_DELIVERY_TIMEOUT}:{type(exc).__name__}")

async def deliver_text(target: DeliveryTarget, body: str, *, context: Any,
                       media: Sequence[str] = (), diagnostics: bool = False) -> DeliveryNote:
    """``deliver()`` 的别名，仅为调用点可读性（main.py 里用这个名）。"""
    return await deliver(target, body, context=context, media=media, diagnostics=diagnostics)

__all__ = ["HOOK_TIMEOUT_S", "HOOK_TOTAL_TIMEOUT_S", "DELIVERY_TIMEOUT_S", "TRIGGER_TEXT_LIMIT",
           "CHANNEL_CONTEXT", "CHANNEL_NONE", "SHAPE_A", "SHAPE_B", "SHAPE_C", "ERROR_FRAMEWORK",
           "ERROR_PLATFORM", "ERROR_BAD_SESSION", "ERROR_SUPPRESSED", "ERROR_HOOK_TIMEOUT",
           "ERROR_HOOK_FAILED", "ERROR_DELIVERY_TIMEOUT", "ERROR_EMPTY", "FrameworkUnavailable",
           "DeliveryTarget", "snapshot_of", "deliver", "deliver_text"]
