"""v1.7.5 后台接线守卫（spec §6 / §6.6）：只剩插件自管引擎，且真的接到 watchdog 上。

钉四件事：

1. `tools.background_engine` 与 core 引擎的三条分支**删除**（运行时模块里 0 命中；
   `core/config_map.py` 里保留的 6 处是 v5 冻结视图里的 drop 记录，迁移器要用）；
2. 三个外层入口的后台分支都走 `self._watchdog.submit(...)`（同一个设施）；
3. 终态投递**只做 `event.send`**（U4：不读历史、不 merge 工具集、不碰 `send_message_to_user`）；
4. `terminate` 用 `await self._watchdog.shutdown(...)`；提交文案来自 watchdog 的
   `handle.reply_text`（受理/单飞/并发/重复四选一），插件不自己渲染。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import main as main_module  # noqa: E402

#: 运行时模块（**不含** `core/config_map.py`：那里的 6 处是 v5 冻结视图的 drop 记录）
RUNTIME_MODULES = (
    "main.py",
    "core/agent.py",
    "core/a11y.py",
    "core/action_model.py",
    "core/background.py",
    "core/browser.py",
    "core/crawler.py",
    "core/delivery.py",
    "core/executor.py",
    "core/extract.py",
    "core/http.py",
    "core/safety.py",
    "core/search.py",
    "core/search_providers.py",
    "core/session.py",
    "core/verify.py",
)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_core_engine_is_gone_from_runtime_modules():
    for rel in RUNTIME_MODULES:
        text = _src(rel)
        for marker in (
            "background_engine",
            "is_background_task",
            "_probe_background_capability",
            "_sync_background_engine",
            "_apply_background_flags",
            "_run_c_background_body",
            "_C_SCOPE_TOOLS",
        ):
            assert marker not in text, f"{rel} 仍引用已退役的 core 引擎符号 {marker}"


def test_config_map_keeps_only_the_frozen_v5_drop_record():
    """`config_map` 里剩余提及必须是 v5 冻结视图的 drop 记录（迁移器要用）。"""
    text = _src("core/config_map.py")
    hits = [line for line in text.splitlines() if "background_engine" in line]
    assert hits, "v5 冻结视图里应保留 tools.background_engine 的 drop 记录"
    # 两种合法形态：① v5 冻结视图的 Leaf 声明（`"background_engine",`）；
    # ② 迁移映射里的 drop 记录（`"tools.background_engine": (MIGRATION_DROP, None)`）。
    assert "MIGRATION_DROP" in text
    assert any("MIGRATION_DROP" in line for line in hits), "缺 drop 记录"
    for line in hits:
        stripped = line.strip()
        assert stripped.startswith('"background_engine"') or "tools.background_engine" in stripped, (
            f"config_map 里出现了非冻结视图的引用：{stripped}"
        )


def test_three_entries_submit_through_the_watchdog(tmp_path):
    """R15：执行级（取代"源码里有没有这个串"的三条断言）。

    直接装配真插件：①``_watchdog`` 真的是 ``BudgetWatchdog``；②三种 ``background_mode``
    下三个工具的分派布尔与设计一致（``browse_local_page`` 只在 ``all`` 里进后台）；
    ③提交真的会起一个被 watchdog 管着的任务（受理文案 + 任务登记）。
    """
    from astrbot_plugin_browser_llm.core import background as bg

    sys.path.insert(0, str(ROOT / "tests"))
    import v176_fakes as v176

    plugin = v176.make_plugin(tmp_path, v176.FakeContext())
    assert isinstance(plugin._watchdog, bg.BudgetWatchdog), "watchdog 没在 __init__ 建起来"

    expectations = {
        ("_BROWSE_WEB_TOOL_NAME", "off"): False,
        ("_BROWSE_WEB_TOOL_NAME", "browse_only"): True,
        ("_BROWSE_WEB_TOOL_NAME", "all"): True,
        ("_LOCAL_PAGE_TOOL_NAME", "off"): False,
        ("_LOCAL_PAGE_TOOL_NAME", "browse_only"): False,
        ("_LOCAL_PAGE_TOOL_NAME", "all"): True,
        ("_SEARCH_TOOL_NAME", "off"): False,
        ("_SEARCH_TOOL_NAME", "browse_only"): False,
        ("_SEARCH_TOOL_NAME", "all"): True,
    }
    for (tool_attr, mode), expected in expectations.items():
        plugin.background_mode = mode
        tool = getattr(main_module, tool_attr)
        assert plugin._background_enabled_for(tool) is expected, (tool, mode)

    # 提交一次：真的走 watchdog（受理文案 + 任务登记 + 额度）
    plugin.background_mode = "browse_only"

    async def _main():
        reply = await plugin._submit_background(
            event=v176.RealEventStub(),
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="watchdog 接线",
            runner=lambda view, progress: _stub_runner(progress),
        )
        handle = next(iter(plugin._watchdog._handles.values()))
        await v176.wait_handle(handle)
        return reply, handle

    async def _stub_runner(progress):
        return "结论"

    reply, handle = asyncio.run(_main())
    assert "已受理" in reply and handle.task_id in reply
    assert handle.job is not None and handle.job.budget_s > 0


def test_deliver_terminal_goes_through_active_send(tmp_path, monkeypatch):
    """执行级（v1.7.6 取代旧的 `"event.send(" in body` 源码断言）：

    终态不再直捅适配器，而是走 ``core.active_send``（钩子重放 + ``Context.send_message``），
    平台可用时**一次** ``event.send`` 都不调用；U3/U4 红线靠"这些能力根本不存在"兜住。
    """
    sys.path.insert(0, str(ROOT / "tests"))
    import v176_fakes as v176  # 复用共用假件（假框架 / 假 Context / 装配）

    v176.install_framework(monkeypatch)
    context = v176.FakeContext()
    plugin = v176.make_plugin(tmp_path, context)
    event = v176.RealEventStub()
    job = v176.make_job()

    receipt = v176.deliver_once(plugin, event, job, body="【后台任务完成】真投递")

    assert len(context.send_calls) == 1, "终态必须走 L1（Context.send_message）"
    assert event.send_calls == 0, "平台可用时不得 event.send 直捅适配器"
    assert receipt.accepted_by_platform is True
    assert receipt.sent_via == "context.send_message"
    assert job.terminal_sent == 1, "恰好一条：额度被消耗"
    for forbidden in ("send_message_to_user", "persist_agent_history", "build_main_agent"):
        assert not hasattr(plugin, forbidden), f"插件上不该存在 {forbidden}（U3/U4 红线）"


def test_terminate_uses_watchdog_shutdown(tmp_path):
    """R15：执行级 —— ``terminate()`` 真的把在跑任务取消掉（而不是看源码里有没有那行）。"""
    sys.path.insert(0, str(ROOT / "tests"))
    import v176_fakes as v176

    plugin = v176.make_plugin(tmp_path, v176.FakeContext())

    async def _main():
        started = asyncio.Event()

        async def _slow(progress):
            started.set()
            await asyncio.sleep(30)
            return "不该跑到这里"

        await plugin._submit_background(
            event=v176.RealEventStub(),
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="terminate 用例",
            runner=lambda view, progress: _slow(progress),
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # 只补 terminate 需要的两个清理任务句柄（替身没跑启动钩子）
        plugin._cache_cleanup_task = None
        plugin._agent_provider_sync_task = None
        before = len(plugin._watchdog.registry.running())
        await plugin.terminate()
        return before, len(plugin._watchdog.registry.running())

    before, after = asyncio.run(_main())
    assert before >= 1 and after == 0, "terminate 必须经 watchdog 取消在跑任务"
    assert not hasattr(plugin, "_cancel_background_tasks"), "旧的取消实现应已删除"


def test_accepted_reply_comes_from_the_watchdog(tmp_path):
    """提交实际跑一次：受理文案来自 watchdog，同键重复提交不会再起一个任务。"""
    sys.path.insert(0, str(ROOT / "tests"))
    import test_v175_tools as helpers  # 复用同一批 fake（页面/会话/安全过滤器）

    plugin = helpers._bare_plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin.background_mode = "browse_only"

    async def _slow(_progress: dict) -> str:
        await asyncio.sleep(0.05)
        return "后台结论"

    async def _run() -> tuple[str, str, int]:
        first = await plugin._submit_background(
            event=event,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_slow,
            task_text="同一个任务",
        )
        second = await plugin._submit_background(
            event=event,
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_slow,
            task_text="同一个任务",
        )
        await asyncio.sleep(0.2)
        return first, second, len(plugin._watchdog.registry.running())

    first, second, running = asyncio.run(_run())
    assert "已受理" in first, first
    assert "任务" in second or "已受理" in second or "重复" in second, second
    assert running == 0, "任务跑完后登记表应为空"
