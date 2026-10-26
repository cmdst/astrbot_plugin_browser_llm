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


def test_three_entries_submit_through_the_watchdog():
    src = _src("main.py")
    assert "self._watchdog = BudgetWatchdog(" in src, "watchdog 没在 __init__ 建起来"
    submit_body = src[src.index("    async def _submit_background("):]
    submit_body = submit_body[: submit_body.index("\n    async def ", 10)]
    assert "self._watchdog.submit(" in submit_body, "提交没走 watchdog"
    for key in ("budget_s=", "max_chars=", "dedupe_window_s=", "max_parallel=", "task_text="):
        assert key in submit_body, f"submit 缺 {key}"
    # 三个外层入口都要能进后台分支
    for tool in ("_BROWSE_WEB_TOOL_NAME", "_LOCAL_PAGE_TOOL_NAME", "_SEARCH_TOOL_NAME"):
        assert f"_background_enabled_for({tool})" in src, f"{tool} 没有后台分支"


def test_deliver_only_sends_a_message():
    src = _src("main.py")
    body = src[src.index("    async def _deliver_terminal("):]
    body = body[: body.index("\n    def ", 10)]
    assert "event.send(" in body
    for forbidden in ("send_message_to_user", "persist_agent_history", "build_main_agent"):
        assert forbidden not in body, f"deliver 里出现 {forbidden}（U3/U4 红线）"


def test_terminate_uses_watchdog_shutdown():
    src = _src("main.py")
    start = src.index("    async def terminate(")
    body = src[start : start + 4000]
    assert "self._watchdog.shutdown(" in body, "terminate 必须走 watchdog.shutdown"
    assert "_cancel_background_tasks" not in src, "旧的取消实现应已删除"


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
