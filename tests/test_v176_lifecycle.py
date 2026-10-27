"""v1.7.6 事件生命周期纪律（spec §6 C1–C8 / §14-F-6 / §15-A-5）。

三条判据，分别回答三个不同的问题：

1. **后台执行体碰不到原事件**（C1/C2/C8）—— 执行级探针：提交时抓完快照后，把原事件的
   **任何**属性访问都变成 ``AssertionError``，后台任务必须照样跑到终态并投递成功；
2. **平台回退句柄至多一次**（C3）—— 计数器断言：平台可用 ``0`` 次、平台缺失 ``1`` 次；
   "至多一次"是**上界**证据（与 ``test_v176_delivery_parity`` 里"恰好一次"的下界证据互补：
   那条证明"会用它"，这条证明"不会用它第二次"）；
3. **后台执行体闭包里不许出现平台能力**（C2 的静态面）—— AST 判三种执行体
   （``_browse_web_run`` / ``_local_page_render`` / ``_search_job``）的函数体：禁止
   ``event.<平台能力>``；``_emit_media`` 的"经 sink 转发 event"是**唯一**放行口，
   放行前提由 :func:`test_emit_media_forwards_event_only_through_artifact_sink` 钉住
   （sink 打开时一次 ``event.send`` 都没有）。

为什么要有这三条：v1.7.6 把终态从"捕获的 ``event.send``"改成"合成事件 + 主动发送"，
后台只剩**一个**跨轮持有的东西（``fallback_send`` 绑定方法）。纪律不靠人记，靠这三条。
"""

from __future__ import annotations

import asyncio
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT), str(ROOT / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.active_send as active_send  # noqa: E402
import core.delivery as delivery  # noqa: E402
import main as main_module  # noqa: E402
import v176_fakes as v176  # noqa: E402

#: 后台执行体闭包里禁止出现的**平台能力**（身份读取如 ``get_group_id`` 不在此列：
#: 它们已经在快照里、后台拿到的视图也提供；``message_obj`` 连读都不许 —— 见 C2）。
FORBIDDEN_EVENT_ATTRS = frozenset(
    {
        "send",
        "send_streaming",
        "react",
        "get_messages",
        "get_group",
        "get_group_members",
        "get_group_list",
        "get_platform_inst",
        "get_result",
        "set_result",
        "stop_event",
        "is_stopped",
        "set_extra",
        "get_extra",
        "clear_extra",
        "image_result",
        "chain_result",
        "plain_result",
        "message_obj",
        "raw_message",
    }
)

#: 三个后台执行体（spec §14-F-6 点名）
BACKGROUND_BODIES = ("_browse_web_run", "_local_page_render", "_search_job")


def _function_body(source: str, name: str) -> ast.AST:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"main.py 里找不到执行体 {name}")


def _event_attr_hits(node: ast.AST) -> list[str]:
    hits = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            if child.value.id == "event" and child.attr in FORBIDDEN_EVENT_ATTRS:
                hits.append(child.attr)
    return hits


def test_ast_no_event_platform_calls_in_background_bodies():
    """C2 的静态面：三个后台执行体的函数体里不许出现任何平台能力调用。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    offenders = {}
    for name in BACKGROUND_BODIES:
        hits = _event_attr_hits(_function_body(source, name))
        if hits:
            offenders[name] = sorted(set(hits))
    assert not offenders, (
        "后台执行体仍在调用平台能力（spec §6 C2）："
        f"{offenders}；要发东西只能登记 artifact 或走终态投递"
    )


def test_background_executors_receive_read_only_view():
    """C2 的运行时面：执行体拿到的是 :class:`BackgroundEventView`，不是原事件。

    这条对着**生产调用点**断言（``main._artifact_collecting`` 的包装），不是对着测试替身。
    """
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    body = ast.get_source_segment(source, _function_body(source, "_artifact_collecting")) or ""
    assert "BackgroundEventView" in body, "后台执行体必须拿只读视图（不持有原事件）"
    assert "active_send.BackgroundEventView(snapshot)" in body

    snap = v176.snapshot()
    view = active_send.BackgroundEventView(snap)
    assert view.unified_msg_origin == snap.umo
    assert view.get_group_id() == snap.group_id
    assert view.get_sender_id() == snap.sender_id
    assert view.get_platform_name() == snap.platform_name
    assert view.message_str == snap.trigger_text
    for ability in ("send", "send_streaming"):
        try:
            asyncio.run(getattr(view, ability)("x"))
        except RuntimeError as exc:
            assert "C2" in str(exc)
        else:  # pragma: no cover — 走不到
            raise AssertionError(f"BackgroundEventView.{ability} 必须拒绝发送（C2）")


def test_background_never_touches_event(tmp_path, monkeypatch):
    """C1/C8：提交时抓完快照后，后台链路对**原事件**零访问，任务照样跑到终态。

    探针事件在提交返回后打开 ``strict``：此后**任何**属性访问都抛 ``AssertionError``。
    后台跑完还能投递成功 ⇒ 后台确实只持有快照/视图/回退句柄。
    """
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    probe = v176.RealEventStub()

    async def _main():
        reply = await plugin._submit_background(
            event=probe,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="生命周期探针",
            runner=lambda view, progress: _runner(view, progress),
        )
        probe.strict = True  # 提交完成：此后碰原事件就炸
        handle = next(iter(plugin._watchdog._handles.values()))
        await v176.wait_handle(handle)
        return reply, handle

    async def _runner(view, progress):
        progress["steps"] = 1
        return f"结论：{view.unified_msg_origin}"

    reply, handle = asyncio.run(_main())

    assert "已受理" in reply
    assert probe.access_log == [], f"后台链路碰了原事件的这些属性：{probe.access_log}"
    assert handle.outcome is not None and handle.outcome.accepted_by_platform is True
    assert handle.outcome.sent_via == active_send.CHANNEL_CONTEXT
    assert len(context.send_calls) == 1, "终态只发一条"
    assert probe.send_calls == 0, "平台可用 ⇒ 回退句柄一次都不许用"


def test_fallback_send_called_at_most_once(tmp_path, monkeypatch):
    """C3：``fallback_send`` 是"至多一次"的上界证据（平台可用时 0 次）。"""
    counts = {}

    # ① 平台可用 ⇒ 0 次
    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    v176.deliver_once(plugin, event, v176.make_job(task_id="bg-a"))
    counts["platform_ok"] = event.send_calls

    # ② 平台缺失 ⇒ 1 次（回退救回）
    v176.install_framework(monkeypatch)
    fallback_ctx = v176.FakeContext(platform_insts=[])
    fallback_plugin = v176.make_plugin(tmp_path, fallback_ctx)
    fallback_event = v176.RealEventStub()
    outcome = v176.deliver_once(fallback_plugin, fallback_event, v176.make_job(task_id="bg-b"))
    counts["platform_missing"] = fallback_event.send_calls

    assert counts["platform_ok"] == 0
    assert counts["platform_missing"] == 1
    for name, value in counts.items():
        assert 0 <= value <= 1, f"{name} 用了 {value} 次回退（C3 要求至多一次）"
    assert outcome.accepted_by_platform is True and outcome.sent_via == active_send.CHANNEL_FALLBACK


def test_emit_media_forwards_event_only_through_artifact_sink(tmp_path, monkeypatch):
    """``_emit_media`` 是"经 sink 转发 event"的唯一放行口 —— 前提由这条钉住。

    sink 打开（后台）：只登记路径，**一次** ``event.send`` 都没有；
    sink 关闭（前台）：照旧直接发（行为不变）。放行口的边界在这里而不是在别处。
    """
    v176.install_framework(monkeypatch)
    plugin = v176.make_plugin(tmp_path, v176.FakeContext())
    media = tmp_path / "shot.png"
    media.write_bytes(b"\x89PNG\r\n\x1a\n")

    background_event = v176.RealEventStub()
    with delivery.artifact_sink() as bucket:
        asyncio.run(plugin._emit_media(background_event, str(media), "image"))
    assert background_event.send_calls == 0, "后台媒体不许裸发（F-G4 / C5）"
    assert list(bucket or []) == [str(media)]

    foreground_event = v176.RealEventStub()
    asyncio.run(plugin._emit_media(foreground_event, str(media), "image"))
    assert foreground_event.send_calls == 1, "前台媒体照旧直发（行为不变）"
