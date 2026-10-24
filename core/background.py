"""后台任务登记与终态文案渲染（v1.5.0 路线 D：插件内闭环）。

三个 LLM 工具在 ``tools.background_mode`` 打开后把真正的执行丢给
``asyncio.create_task``，主 Agent 立刻拿到「已受理 + task_id」。于是
「谁在跑」「跑完了怎么告诉用户」「插件重载时怎么收尾」必须有确定归属：

- **同 UMO 单飞**：同一会话同时最多一个后台任务。``browse_web`` 入口不持
  per-UMO 锁（持锁会让子代理内部再 acquire 同一把锁而死锁），两个任务并发
  时会在子工具层争同一把锁——表现为交错而非串行，所以必须在登记层拒绝。
  裁决是**纯查表**（无 await、无锁、不排队）：命中即立刻拒绝。
- **终态可观测**：成功/失败/超时/中断四种终态都要有一条用户可见消息；
  只写日志等于结果丢了（派单 §4.6）。
- **重载安全**：``terminate()`` 取消全部在跑任务并等待一小段，不让插件重载
  悬挂（派单 §4.3）。

本模块只做「登记 + 文案渲染 + 取消」三件事，不 import astrbot、不做 I/O，
因此可独立单测（``main.py`` 负责把文案包成 MessageChain 并投递）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

#: 终态取值。除成功外三种都要能单独渲染，便于运营方按 status 排障。
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"

#: **四态四个前缀**（队长 2026-10-25 口径 D3）：失败/超时/取消不得以
#: 「完成」开头，否则 ``grep -c "【后台任务完成】"`` 会把失败算成成功。
HEADERS: dict[str, str] = {
    STATUS_OK: "【后台任务完成】",
    STATUS_FAILED: "【后台任务失败】",
    STATUS_TIMEOUT: "【后台任务超时】",
    STATUS_CANCELLED: "【后台任务已取消】",
}

#: 立即返回 / 拒绝的前缀。**拒绝统一用【拒绝】**（与仓库既有工具文案同口径，
#: 不再造第二种拒绝前缀）；受理才用【已受理】（队长口径 D3）。
ACCEPTED_HEADER = "【已受理】"
REJECTED_HEADER = "【拒绝】"

#: 全局并发上限（队长口径 D2）：**不是配置键**，是进程级硬约束。
#: 每个后台任务 = 1 套子代理链，可能占 1 个浏览器 context + 最多
#: ``limits.max_pages`` 个标签；4 个并发已可能撞容量上限（那时子工具会返回
#: 明确错误文本，不会悬挂），但足以挡住"所有群同时用"的雪崩。
_BG_MAX_CONCURRENT = 4

#: 并发上限的拒绝文案（逐字，与生产判据/README 对齐）。
_BG_BUSY_TEXT = (
    f"{REJECTED_HEADER}后台任务已达并发上限（{_BG_MAX_CONCURRENT}）：请稍后再试。"
)

#: 部分结果提示（超时/失败/中断时，若能拿到中断前的半成品才附）。
PARTIAL_NOTE = "（以下是中断前已得部分结果，可能不完整）"

_TRUNCATION_NOTE = "…（完整结果已省略 {omitted} 字）"

#: 失败原因展示上限（异常文本可能很长/夹带换行）。
_REASON_LIMIT = 300


def shorten(text: str, limit: int) -> str:
    """把单行摘要按字符上限截断（失败原因、单飞拒绝文案用）。"""
    raw = " ".join(str(text or "").split())
    if limit <= 0 or len(raw) <= limit:
        return raw
    return raw[:limit] + "…"


def truncate_result(text: str, limit: int) -> tuple[str, bool, int]:
    """按字符上限截断任务结果。

    Returns:
        tuple[str, bool, int]: (截断后文本, 是否截断, 被省略的字符数)。
        ``limit <= 0`` 表示不截断（配置钳制后不会出现，函数保持通用）。
    """
    raw = str(text or "")
    limit = int(limit)
    if limit <= 0 or len(raw) <= limit:
        return raw, False, 0
    omitted = len(raw) - limit
    return raw[:limit] + "\n" + _TRUNCATION_NOTE.format(omitted=omitted), True, omitted


def render_accepted_text(*, task_id: str, tool: str, budget_s: float) -> str:
    """立即返回文案（设计 §2.6(a)，逐字）。

    关键约束：**必须明说"你现在还没有结果"**，否则主模型会当真去总结一个
    还不存在的结果（派单 §4.1）。四态前缀也在这里点名，用户/运营方据此对账。
    """
    titles = "/".join(f"「{HEADERS[status]}」" for status in (
        STATUS_OK,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_CANCELLED,
    ))
    return (
        f"{ACCEPTED_HEADER}后台任务已开始执行（task_id={task_id}，工具={tool}，"
        f"整任务硬预算 {float(budget_s):.0f} 秒）。\n"
        f"结果会在完成后**单独作为一条消息**发到本会话，标题以{titles}开头。\n"
        "\n"
        "【重要】你现在**还没有**任何结果：不要总结、不要猜测、不要编造浏览内容，"
        "也不要依据这句话回答用户。\n"
        "你现在唯一的动作：用一句话告诉用户“已开始处理，完成后会单独发结果”，"
        "然后结束本轮。\n"
        "不要重复提交同一任务（同一会话同时只允许一个后台任务），"
        "也不要调用其它工具去补这个结果。"
    )


def render_rejected_text(
    *, task_id: str, tool: str, elapsed_s: float, scope: str = "本会话"
) -> str:
    """单飞拒绝文案（设计 §2.6(b)）：不排队、不让主模型换参数重试。"""
    return (
        f"{REJECTED_HEADER}{scope}已有一个后台任务在跑（task_id={task_id}，"
        f"工具={tool}，已运行 {max(0.0, float(elapsed_s)):.0f} 秒）。\n"
        f"同一会话同时只允许一个后台任务。不要再提交、不要换参数重试。\n"
        "你现在的唯一动作：用一句话告诉用户“上一个任务还在跑，完成后会把结果发过来”。"
    )


def render_terminal_text(
    *,
    task_id: str,
    tool: str,
    status: str,
    text: str = "",
    reason: str = "",
    elapsed_ms: int = 0,
    budget_s: float = 0.0,
    truncated: bool = False,
    omitted: int = 0,
    artifact_count: int = 0,
    missing_files: int = 0,
    partial: str = "",
) -> str:
    """渲染终态消息正文（四态共用头部，前缀按状态分流，见 :data:`HEADERS`）。"""
    head = (
        f"{HEADERS.get(status, HEADERS[STATUS_FAILED])}{tool}"
        f"（task_id={task_id}，耗时 {max(0, int(elapsed_ms)) / 1000:.1f}s）"
    )
    if status == STATUS_OK:
        lines = [head, text.strip() or "（任务执行完成，但没有返回可展示的文本）"]
        if truncated:
            lines.append(f"（已截断：{_TRUNCATION_NOTE.format(omitted=omitted)}）")
        if artifact_count or missing_files:
            note = f"（另附 {artifact_count} 个媒体文件"
            if missing_files:
                note += f"；附件缺失 {missing_files} 个"
            lines.append(note + "）")
        return "\n".join(lines)

    # 非成功态：原因 + （可选）中断前的部分结果 + 处置建议
    if status == STATUS_TIMEOUT:
        lines = [
            head,
            f"已用满 {float(budget_s):.0f} 秒硬预算，任务被中断。",
        ]
        advice = "可调大 tools.background_timeout，或把任务拆小。"
    elif status == STATUS_CANCELLED:
        lines = [
            head,
            "插件被重载或禁用（含 astrbot.service 重启/插件更新），任务被取消。",
        ]
        advice = "重新发起即可。"
    else:
        lines = [
            head,
            f"失败原因：{shorten(reason, _REASON_LIMIT) or '未给出原因'}",
            "本次没有可用结果。",
        ]
        advice = "可稍后重试，或把 tools.background_mode 设为 off 改用同步模式。"
    if partial.strip():
        lines.append(PARTIAL_NOTE)
        lines.append(partial.strip())
    lines.append(advice)
    return "\n".join(lines)


@dataclass
class BackgroundJob:
    """一个在跑的后台任务（登记项 + 提交时快照，不含业务逻辑）。

    快照（``budget_s``/``max_chars``）在**提交那一刻**复制：运行中改配置不会
    让在跑的任务突然被 30s 预算掐断（设计 §1.7-3）。
    """

    task_id: str
    umo: str
    tool: str
    started_at: float
    budget_s: float = 240.0
    max_chars: int = 1500
    task: asyncio.Task | None = field(default=None, repr=False)

    def elapsed_s(self) -> float:
        return max(0.0, time.monotonic() - float(self.started_at))


class BackgroundRegistry:
    """同 UMO 单飞的任务登记表（纯内存、单事件循环内使用）。

    并发安全说明：插件运行在 asyncio 单线程事件循环内，方法体里没有
    ``await``，dict 读/写天然原子，无需额外锁。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._by_umo: dict[str, str] = {}
        self._seq = 0

    def next_task_id(self) -> str:
        """生成短 task_id（``bg-0001`` 起自增；只在单实例内唯一即可）。"""
        self._seq += 1
        return f"bg-{self._seq:04d}"

    def active_job(self, umo: str) -> BackgroundJob | None:
        """该会话正在跑的任务；没有则 ``None``（顺手清理已结束的残留）。"""
        task_id = self._by_umo.get(umo)
        if task_id is None:
            return None
        job = self._jobs.get(task_id)
        if job is None or (job.task is not None and job.task.done()):
            self._by_umo.pop(umo, None)
            self._jobs.pop(task_id, None)
            return None
        return job

    def active_task_id(self, umo: str) -> str | None:
        """该会话正在跑的任务 id（``None`` 表示可以提交）。"""
        job = self.active_job(umo)
        return job.task_id if job is not None else None

    def register(self, job: BackgroundJob) -> None:
        """登记任务并占住该 UMO（调用方应先查 :meth:`active_job`）。"""
        self._jobs[job.task_id] = job
        self._by_umo[job.umo] = job.task_id

    def attach_task(self, task_id: str, task: asyncio.Task) -> None:
        job = self._jobs.get(task_id)
        if job is not None:
            job.task = task

    def get(self, task_id: str) -> BackgroundJob | None:
        return self._jobs.get(task_id)

    def task_of(self, task_id: str) -> asyncio.Task | None:
        job = self._jobs.get(task_id)
        return job.task if job is not None else None

    def finish(self, task_id: str) -> None:
        """任务走到终态后登记出表（幂等）。"""
        job = self._jobs.pop(task_id, None)
        if job is None:
            return
        if self._by_umo.get(job.umo) == task_id:
            self._by_umo.pop(job.umo, None)

    def running(self) -> list[BackgroundJob]:
        """仍在跑的登记项（已结束但还没 ``finish()`` 的不算）。"""
        return [
            job
            for job in self._jobs.values()
            if job.task is not None and not job.task.done()
        ]

    def __len__(self) -> int:
        return len(self._jobs)

    async def cancel_all(self, timeout: float) -> int:
        """取消全部在跑任务并等待至多 ``timeout`` 秒（插件终止时调用）。

        Returns:
            int: 被取消的任务数（0 表示没有在跑的任务）。
        """
        jobs = self.running()
        tasks = [job.task for job in jobs if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
        self._jobs.clear()
        self._by_umo.clear()
        return len(tasks)
