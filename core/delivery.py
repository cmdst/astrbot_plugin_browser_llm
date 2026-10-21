"""后台媒体收集（ContextVar）：前台直发 vs 后台只登记路径。

v1.4.0 起**只保留 artifact 三件套**：``artifact_sink`` / ``collect_artifact`` /
``current_artifacts``。v3 的投递状态机（``DeliveryArbiter`` / ``BusyTracker`` /
``render_envelope`` 等）在 ``main.py`` 里零调用、只有测试在测，按 captain 裁定 D8
删除；**异步投递 / 唤醒属 v5 非目标**，将来接回时按届时 spec 重写，不要从这里恢复。
``redact_secrets()`` 已迁到 ``core/search.py``（唯一消费者）。

纯 Python，不 import astrbot。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_ARTIFACT_SINK: ContextVar[list[str] | None] = ContextVar(
    "browser_llm_artifact_sink", default=None
)


@contextmanager
def artifact_sink() -> Iterator[list[str]]:
    """在后台任务里收集媒体路径，替代直接 ``event.send``。"""
    bucket: list[str] = []
    token = _ARTIFACT_SINK.set(bucket)
    try:
        yield bucket
    finally:
        _ARTIFACT_SINK.reset(token)


def collect_artifact(path: str) -> bool:
    """登记一个媒体路径；前台（无 sink）返回 False，调用方走老路径。"""
    sink = _ARTIFACT_SINK.get()
    if sink is None:
        return False
    sink.append(str(path))
    return True


def current_artifacts() -> list[str]:
    """当前上下文已登记的媒体路径（只读副本；测试与诊断用）。"""
    return list(_ARTIFACT_SINK.get() or [])
