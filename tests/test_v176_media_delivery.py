"""v1.7.6 后台媒体投递守卫（F-G4）：媒体随**终态那一条**消息发出，不裸发。

修复前（v1.7.5 现实形态）：``core/delivery.artifact_sink()`` 全仓零调用 ⇒
``collect_artifact()`` 恒 ``False`` ⇒ 后台 ``browse_screenshot`` / ``browse_media``
走 ``_emit_media`` 落到 ``event.send`` ⇒ 每条截图一条裸消息 + 一条终态，
同时违反「恰好一条终态」与事件生命周期纪律。

这份文件用**执行级**用例钉住修好后的行为：

1. 一次后台任务只产生**一条**终态消息，媒体作为附件随该条发出（``event.send`` 0 次）；
2. 后台 ``_emit_media`` 只登记路径（``artifact_sink`` 生效），前台行为不变（直发）；
3. ``artifact_count`` / ``missing_files`` 有真实生产者（渲染文案里可见）；
4. 缺失文件不拖垮终态；附件按 job 快照（``progress["artifacts"]``）构造，
   **不依赖**跨任务的 ContextVar；附件上限 12 条；视频按 ``Video`` 发。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_v176_delivery_parity as v176  # noqa: E402 — 复用假框架 / 假 Context / 装配
import main as main_module  # noqa: E402


def _make_file(tmp_path, name: str, *, suffix: str = ".png") -> str:
    path = tmp_path / f"{name}{suffix}"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return str(path)


def _submit_and_wait(plugin, event, runner, *, task_text="媒体任务"):
    """提交并等到收尾；``handle`` 在提交返回后立刻抓（它收尾时会从表里弹出）。"""

    async def _main():
        reply = await plugin._submit_background(
            event=event,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=runner,
            task_text=task_text,
        )
        handles = list(plugin._watchdog._handles.values())
        assert handles, "提交后应立刻有 handle（受理文案要求任务真的起来了）"
        return reply, await v176._wait_handle(handles[-1])

    return asyncio.run(_main())


def _texts(chain) -> list[str]:
    return [getattr(component, "text", "") for component in chain if hasattr(component, "text")]


def _kinds(chain) -> list[str]:
    return [getattr(component, "type", type(component).__name__) for component in chain]


def test_background_task_sends_exactly_one_terminal_with_media(tmp_path, monkeypatch):
    """一次后台任务 = 一条终态；两张截图作为附件随它发出，``event.send`` 0 次。"""
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    first = _make_file(tmp_path, "shot1")
    second = _make_file(tmp_path, "shot2")

    async def runner(progress):
        await plugin._emit_media(event, first, "image")
        await plugin._emit_media(event, second, "image")
        return "看完了：页面标题是「确认订单」"

    reply, handle = _submit_and_wait(plugin, event, runner)

    assert "已受理" in reply
    assert len(context.send_calls) == 1, "一次后台任务只允许一条终态消息（U2 不变量）"
    umo, chain = context.send_calls[0]
    assert umo == v176.UMO
    assert _kinds(chain) == ["Plain", "Image", "Image"], (
        "媒体必须作为附件拼在终态链尾（不是各自一条消息）"
    )
    body = _texts(chain) or [str(item) for item in chain]
    assert "【后台任务完成】" in "".join(str(part) for part in body)
    assert "另附 2 个媒体文件" in str(chain), "artifact_count 必须进正文（有真实生产者）"
    assert event.send_calls == 0, "后台绝不许直接 event.send（F-G4 的红线）"
    assert handle.outcome.accepted_by_platform is True
    assert handle.outcome.sent_via == "context.send_message"


def test_emit_media_in_foreground_still_sends_directly(tmp_path):
    """前台（没有 artifact 收集上下文）行为不变：立即直发。"""
    plugin = v176.make_plugin(tmp_path, v176.FakeContext())
    event = v176.RealEventStub()
    path = _make_file(tmp_path, "foreground")

    asyncio.run(plugin._emit_media(event, path, "image"))

    assert event.send_calls == 1
    assert event.sent == [("image", path)]


def test_artifact_count_and_missing_files_fed_into_body(tmp_path, monkeypatch):
    """``artifact_count`` / ``missing_files`` 由快照真实计算（缺一个 ⇒ 正文点名）。"""
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    present = _make_file(tmp_path, "present")
    gone = str(tmp_path / "never-existed.png")

    async def runner(progress):
        await plugin._emit_media(event, present, "image")
        await plugin._emit_media(event, gone, "image")
        return "结果"

    _, handle = _submit_and_wait(plugin, event, runner)

    chain = context.send_calls[0][1]
    assert _kinds(chain) == ["Plain", "Image"], "缺失文件不进链（只计数）"
    assert "另附 1 个媒体文件" in str(chain)
    assert "附件缺失 1 个" in str(chain)
    assert handle.outcome.accepted_by_platform is True, "缺失附件不拖垮终态"


def test_missing_artifact_does_not_break_terminal(tmp_path, monkeypatch):
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()

    async def runner(progress):
        await plugin._emit_media(event, str(tmp_path / "ghost.png"), "image")
        return "只有一条文本"

    _, handle = _submit_and_wait(plugin, event, runner)

    chain = context.send_calls[0][1]
    assert _kinds(chain) == ["Plain"], "没有任何可用附件时链里只有正文"
    assert "附件缺失 1 个" in str(chain)
    assert handle.outcome.succeeded is True


def test_terminal_builds_attachments_from_job_snapshot(tmp_path, monkeypatch):
    """投递层只读 job 快照 —— 不依赖 ContextVar（deliver 可能在别的 task 里跑）。"""
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    path = _make_file(tmp_path, "snapshot")
    job = v176.make_job(progress={"artifacts": [path]})

    receipt = v176.deliver_once(plugin, event, job, body="【后台任务完成】带附件")

    chain = context.send_calls[0][1]
    assert _kinds(chain) == ["Plain", "Image"], "附件来自 progress['artifacts']（快照）"
    assert receipt.accepted_by_platform is True


def test_artifacts_capped_at_twelve(tmp_path, monkeypatch):
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    paths = [_make_file(tmp_path, f"shot{i:02d}") for i in range(20)]

    async def runner(progress):
        for path in paths:
            await plugin._emit_media(event, path, "image")
        return "截图有点多"

    _, handle = _submit_and_wait(plugin, event, runner)

    chain = context.send_calls[0][1]
    assert _kinds(chain).count("Image") == plugin._MAX_TERMINAL_ARTIFACTS == 12
    assert "另附 12 个媒体文件" in str(chain)
    assert handle.job.progress["artifacts"][:1] == [paths[0]]
    assert len(handle.job.progress["artifacts"]) == 12


def test_artifact_snapshot_survives_runner_error(tmp_path, monkeypatch):
    """执行体中途抛错：已登记的媒体仍随终态发出（``finally`` 落快照）。"""
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    path = _make_file(tmp_path, "before-crash")

    async def runner(progress):
        await plugin._emit_media(event, path, "image")
        raise RuntimeError("页面崩了")

    _, handle = _submit_and_wait(plugin, event, runner)

    chain = context.send_calls[0][1]
    assert _kinds(chain) == ["Plain", "Image"], "中断前的截图也要随终态一条发出"
    assert handle.job.progress["artifacts"] == [path]
    assert handle.outcome.status == "failed"
    assert "【后台任务失败】" in str(chain)
    assert event.send_calls == 0


def test_video_suffix_uses_video_component(tmp_path, monkeypatch):
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    video = _make_file(tmp_path, "clip", suffix=".mp4")

    async def runner(progress):
        await plugin._emit_media(event, video, "video")
        return "下载好了"

    _submit_and_wait(plugin, event, runner)

    chain = context.send_calls[0][1]
    assert _kinds(chain) == ["Plain", "Video"], "视频必须走 Video（统一按 Image 发会失败）"
