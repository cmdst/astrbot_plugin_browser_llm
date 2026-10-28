"""v1.8.0 接线守卫（规格 §9.3-U2 / §10.4 映射表 / §11.5 EX-48..EX-52）。

这个文件接替 v1.7.5 的 `tests/test_v175_background_wiring.py`（6 条），按映射表一条不丢：

| 旧守卫（v1.7.5） | 本文件的落点 |
| --- | --- |
| 三闸接线（`submit` 的四个参数来源） | :func:`test_submit_carries_config_snapshot_into_the_record`（**执行级**：真提交一个任务，读记录字段） |
| `main.py` 里 `self._watchdog = BudgetWatchdog(` | :func:`test_plugin_owns_a_task_book`（`__init__` 真建 `TaskBook`） |
| 运行期模块清单（`RUNTIME_MODULES`） | :data:`RUNTIME_MODULES` + :func:`test_runtime_modules_swap_background_for_tasks` |
| 「core 引擎痕迹不得回潮」标记串（整组保留） | :func:`test_core_engine_markers_are_gone_from_runtime_modules` |
| 终态投递只做发送（U4） | :func:`test_delivery_goes_through_proactive_exactly_once`（v1.8.0 换成 `context.send_message` 通道） |
| `terminate` 走 `watchdog.shutdown` | :func:`test_terminate_cancels_every_task_into_cancelled`（**执行级**：任务真落 `cancelled`） |

另外两条是 A1 的**接线级**判据（规格 §11.1 的 EX-12/EX-13）：替身 `parse_action`，断言它
收到的 `payload` 是 `dict` 而不是 `str` —— 这是唯一能抓住"边缘忘了 coerce"的断言形态。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
sys.path.insert(0, str(ROOT / "tests"))

import main as main_module  # noqa: E402 — conftest 已装包别名与 astrbot 桩
import test_v175_tools as helpers  # noqa: E402 — 复用同一批 fake（页面/会话/安全过滤器）
from astrbot_plugin_browser_llm.core import action_model as am  # noqa: E402
from astrbot_plugin_browser_llm.core import proactive  # noqa: E402
from astrbot_plugin_browser_llm.core import tasks as tb  # noqa: E402

#: 运行时模块（**不含** `core/config_map.py`：那里的 6 处是 v5 冻结视图的 drop 记录）
RUNTIME_MODULES = (
    "main.py",
    "core/agent.py",
    "core/a11y.py",
    "core/action_model.py",
    "core/browser.py",
    "core/crawler.py",
    "core/delivery.py",
    "core/executor.py",
    "core/extract.py",
    "core/http.py",
    "core/proactive.py",
    "core/safety.py",
    "core/search.py",
    "core/search_providers.py",
    "core/session.py",
    "core/tasks.py",
    "core/verify.py",
)

#: v1.6.0 的 core 引擎与 v1.5.0 的 5 态引擎的痕迹（整组保留，一条都不许回潮）
RETIRED_MARKERS = (
    "background_engine",
    "is_background_task",
    "_probe_background_capability",
    "_sync_background_engine",
    "_apply_background_flags",
    "_run_c_background_body",
    "_C_SCOPE_TOOLS",
    "BudgetWatchdog",
    "core.background",
    "BackgroundRegistry",
)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _plugin(tmp_path):
    """手工装配的插件实例（复用具名 fake；任务簿是**真的** TaskBook）。"""
    return helpers._bare_plugin(tmp_path)


# ------------------------------------------------------- 模块面 / 退役标记


def test_core_engine_markers_are_gone_from_runtime_modules():
    for rel in RUNTIME_MODULES:
        text = _src(rel)
        for marker in RETIRED_MARKERS:
            assert marker not in text, f"{rel} 仍引用已退役的后台引擎符号 {marker}"


def test_config_map_keeps_only_the_frozen_v5_drop_record():
    """`config_map` 里剩余提及必须是 v5 冻结视图的 drop 记录（迁移器要用）。"""
    text = _src("core/config_map.py")
    hits = [line for line in text.splitlines() if "background_engine" in line]
    assert hits, "v5 冻结视图里应保留 tools.background_engine 的 drop 记录"
    assert "MIGRATION_DROP" in text
    assert any("MIGRATION_DROP" in line for line in hits), "缺 drop 记录"
    for line in hits:
        stripped = line.strip()
        assert stripped.startswith('"background_engine"') or "tools.background_engine" in stripped, (
            f"config_map 里出现了非冻结视图的引用：{stripped}")


def test_runtime_modules_swap_background_for_tasks():
    """EX-52：清单含 `core/tasks.py` + `core/proactive.py`，**不含**已删的 `core/background.py`。"""
    assert "core/tasks.py" in RUNTIME_MODULES
    assert "core/proactive.py" in RUNTIME_MODULES
    assert "core/background.py" not in RUNTIME_MODULES
    assert not (ROOT / "core" / "background.py").exists(), "core/background.py 应已删除"
    for rel in RUNTIME_MODULES:
        assert (ROOT / rel).exists(), f"运行时清单里的 {rel} 不存在"


def test_line_budgets_hold_on_real_files():
    """EX-52 的行数判据（口径见规格 §9.2 + 勘误：tasks 上限按 900 的实际落地值）。"""
    main_lines = len(_src("main.py").splitlines())
    tasks_lines = len(_src("core/tasks.py").splitlines())
    proactive_lines = len(_src("core/proactive.py").splitlines())
    assert main_lines <= 4200, f"main.py {main_lines} 行（守则 ≤4200）"
    assert tasks_lines <= 900, f"core/tasks.py {tasks_lines} 行（守则 ≤900）"
    assert proactive_lines <= 300, f"core/proactive.py {proactive_lines} 行（守则 ≤300）"


def test_plugin_owns_a_task_book():
    """`__init__` 真的建 `TaskBook`（不再是 BudgetWatchdog / BackgroundRegistry）。"""
    plugin = main_module.BrowserLLMPlugin.__new__(main_module.BrowserLLMPlugin)
    plugin.metadata_name = main_module.METADATA_NAME
    main_module.BrowserLLMPlugin.__init__(
        plugin, context=None, config={})
    assert isinstance(plugin._book, tb.TaskBook), type(plugin._book)
    assert not hasattr(plugin, "_watchdog"), "5 态引擎的字段不许留壳"


def test_new_readonly_tools_are_coroutines_with_spec_docstrings():
    """两个新工具必须存在、是协程、且 docstring 就是模型看到的 schema 说明。"""
    for name in (main_module._TASK_STATUS_TOOL_NAME, main_module._RECENT_RESULTS_TOOL_NAME):
        handler = getattr(main_module.BrowserLLMPlugin, name, None)
        assert handler is not None, f"缺少外层工具 {name}"
        assert asyncio.iscoroutinefunction(handler), f"{name} 不是协程方法"
        doc = handler.__doc__ or ""
        assert "只读" in doc, f"{name} 的 docstring 必须自称只读"
    assert "不含结果正文" in main_module.BrowserLLMPlugin.browse_task_status.__doc__
    assert "不可信" in main_module.BrowserLLMPlugin.browse_recent_results.__doc__


# ------------------------------------------------------------- 接线：A1 边缘


def test_browse_act_hands_the_gate_a_dict_when_model_sends_a_json_string(
        tmp_path, monkeypatch):
    """**接线级（EX-12）**：`action` 写成 JSON 字符串时，闸门收到的必须是 ``dict``。

    这是唯一能抓住"边缘漏了 coerce"的断言：只看返回文本的话，闸门把字符串按 R-A1
    拒掉也"看起来正常"，而生产 8/8 全拒正是这条漏接线造成的。
    """
    seen: list[type] = []
    real = am.parse_action

    def spy(payload, **kwargs):
        seen.append(type(payload))
        return real(payload, **kwargs)

    monkeypatch.setattr(am, "parse_action", spy)
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    out = asyncio.run(plugin.browse_act(event, action=json.dumps({"action": "back"})))
    assert seen, "parse_action 没被调用（接线断了）"
    assert seen[0] is dict, f"闸门收到的是 {seen[0].__name__}，说明边缘没解一层"
    assert "【拒绝】R-A1" not in out, out[:120]


def test_browse_screenshot_hands_the_gate_a_dict_target(tmp_path, monkeypatch):
    """**接线级（EX-13）**：第二个入口 —— `target` 是 JSON 字符串时也解一层。"""
    seen: list[type] = []
    real = am.parse_action

    def spy(payload, **kwargs):
        seen.append(type(payload))
        assert isinstance(payload, dict), "screenshot 探针必须给 parse_action 一个 dict"
        assert type(payload.get("target")) is dict, type(payload.get("target"))
        return real(payload, **kwargs)

    monkeypatch.setattr(am, "parse_action", spy)
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    asyncio.run(plugin.browse_screenshot(
        event, target=json.dumps({"role": "button", "name": "提交"})))
    assert seen == [dict], seen


@pytest.mark.parametrize("payload", [[1, 2], 42, None, "[1,2]", "3"])
def test_real_non_objects_are_still_rejected_with_r_a1(tmp_path, payload):
    """**反向（EX-14）**：真非对象仍走 R-A1 —— 容错层不许把 list/int 也"猜"成动作。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    out = asyncio.run(plugin.browse_act(event, action=payload))
    assert "【拒绝】R-A1" in out, out[:200]


# --------------------------------------------------- 接线：提交 / 投递 / 终止


def test_submit_carries_config_snapshot_into_the_record(tmp_path):
    """EX-29/EX-50（执行级）：四个参数都来自配置快照，且文案是任务簿渲染器的产物。"""
    plugin = _plugin(tmp_path)
    plugin.background_mode = "browse_only"
    plugin.total_budget_s = 42.0
    plugin.background_max_chars = 1234
    plugin.dedupe_window_s = 600.0
    plugin.max_parallel = 3
    event = helpers._FakeEvent()

    async def _slow(progress: dict) -> str:
        await asyncio.sleep(0.05)
        return "后台结论"

    async def _run():
        text = await plugin._submit_background(
            event=event, tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_slow, task_text="同一个任务")
        records = [record for record in plugin._book]
        await asyncio.sleep(0.15)
        return text, records

    text, records = asyncio.run(_run())
    assert len(records) == 1, records
    record = records[0]
    assert isinstance(plugin._book, tb.TaskBook)
    assert record.tool == main_module._BROWSE_WEB_TOOL_NAME
    assert float(record.budget_s) == 42.0, record.budget_s
    assert int(record.max_chars) == 1234, record.max_chars
    assert float(record.dedupe_window_s) == 600.0, record.dedupe_window_s
    assert record.umo == event.unified_msg_origin
    assert text == tb.render_accepted_text(task_id=record.task_id,
                                          tool=record.tool, budget_s=record.budget_s)
    assert record.is_terminal and record.state == tb.STATE_SUCCEEDED


def test_delivery_goes_through_proactive_exactly_once(tmp_path, monkeypatch):
    """EX-51：终态由 `proactive.deliver_text` 走出（**一次**），`target` 是提交时的快照。"""
    calls: list[dict] = []

    async def fake_deliver_text(target, body, *, context, media=(), diagnostics=False):
        calls.append({"target": target, "body": body, "media": media, "context": context})
        return tb.DeliveryNote(attempted=True, succeeded=True,
                               channel=proactive.CHANNEL_CONTEXT, shape=proactive.SHAPE_A)

    monkeypatch.setattr(proactive, "deliver_text", fake_deliver_text)
    plugin = _plugin(tmp_path)
    plugin.background_mode = "browse_only"
    plugin.context = object()
    event = helpers._FakeEvent()

    async def _fast(progress: dict) -> str:
        return "正文-一次性投递"

    async def _run():
        await plugin._submit_background(
            event=event, tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_fast, task_text="投递一次")
        for _ in range(200):
            record = next(iter(plugin._book), None)
            if record is not None and record.is_terminal:
                return record
            await asyncio.sleep(0.01)
        raise AssertionError("任务没有收尾")

    record = asyncio.run(_run())
    assert len(calls) == 1, f"一次逻辑投递只能是 1 次调用，实际 {len(calls)}"
    call = calls[0]
    assert isinstance(call["target"], proactive.DeliveryTarget)
    assert call["target"].umo == event.unified_msg_origin, "target 必须是提交时的 umo 快照"
    assert isinstance(call["media"], tuple), "media 必须是路径元组"
    assert call["body"].startswith(tuple(tb.HEADERS.values())), call["body"][:80]
    assert "正文-一次性投递" in call["body"]
    assert record.delivery.succeeded and record.delivery.attempted
    assert getattr(event, "sent", None) is None, (
        "投递不许再走 event.send（规格 §6.8：不加兜底腿）")


def test_terminate_cancels_every_task_into_cancelled(tmp_path):
    """`terminate()` ⇒ 在跑与排队任务全部落 `cancelled`，没有任务停在 `running`。"""
    plugin = _plugin(tmp_path)
    plugin.background_mode = "browse_only"
    plugin._cache_cleanup_task = None
    plugin._agent_sync_task = None
    event = helpers._FakeEvent()

    async def _hang(progress: dict) -> str:
        await asyncio.sleep(30)  # 靠 terminate 取消
        return "never"

    async def _run():
        plugin._book.submit(
            runner=_hang, deliver=lambda record: _note(), umo=event.unified_msg_origin,
            tool=main_module._BROWSE_WEB_TOOL_NAME, task_text="重载取消",
            budget_s=60.0, max_parallel=2)
        await asyncio.sleep(0.05)
        assert plugin._book.running(), "提交后应有一条在跑"
        await plugin.terminate()
        return [record for record in plugin._book]

    async def _note():
        return tb.DeliveryNote()

    records = asyncio.run(_run())
    assert records == [], "terminate 的兜底 clear() 之后不应再留着记录"


def test_terminate_leaves_no_running_state():
    """`shutdown` 语义（不依赖 main.py）：取消后**没有**任务停在非终态。

    与上一条分开写，是为了把"落 cancelled"这条硬判据钉在任务簿上（main.terminate 只是
    一个调用点；真正的语义在 ``TaskBook.shutdown``）。
    """
    async def _run():
        book = tb.TaskBook()
        seen: list[tuple[str, str]] = []

        async def _hang(progress: dict) -> str:
            await asyncio.sleep(30)
            return "never"

        async def _deliver(record) -> tb.DeliveryNote:
            assert record.state == tb.STATE_DELIVERING, record.state
            seen.append(tb.render_terminal_text(record, record.result_text or ""))
            return tb.DeliveryNote(attempted=True, succeeded=False,
                                   error="PlatformNotFound")

        decision = book.submit(runner=_hang, deliver=_deliver, umo="u1", tool="browse_web",
                               task_text="hard-stop", budget_s=60.0)
        await asyncio.sleep(0.05)
        await book.shutdown(timeout=1.0)
        record = book.get(decision.task_id)
        return record, [r for r in book if r.is_active], seen

    record, still_active, seen = asyncio.run(_run())
    assert record is not None
    assert record.state == tb.STATE_CANCELLED, record.state
    assert still_active == [], "不许有残留的非终态任务"
    assert len(seen) <= 1, f"取消路径最多一条终态，实际 {len(seen)}"
    for text in seen:
        assert text.startswith(tb.HEADERS[tb.STATE_CANCELLED]), text[:80]


def test_platform_not_found_is_recorded_without_retry(tmp_path, monkeypatch):
    """规格 §6.8：平台找不到 ⇒ **只记账**（PlatformNotFound）+ WARN，不重发、不静默、不抛。"""
    calls: list[str] = []

    async def fake_deliver_text(target, body, *, context, media=(), diagnostics=False):
        calls.append(body)
        return tb.DeliveryNote(attempted=True, succeeded=False,
                               channel=proactive.CHANNEL_CONTEXT, shape=proactive.SHAPE_A,
                               error=proactive.ERROR_PLATFORM)

    monkeypatch.setattr(proactive, "deliver_text", fake_deliver_text)
    plugin = _plugin(tmp_path)
    plugin.background_mode = "browse_only"
    plugin.context = object()
    event = helpers._FakeEvent()

    async def _fast(progress: dict) -> str:
        return "投不出去也要留痕"

    async def _run():
        await plugin._submit_background(
            event=event, tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_fast, task_text="平台找不到")
        for _ in range(200):
            record = next(iter(plugin._book), None)
            if record is not None and record.is_terminal:
                return record
            await asyncio.sleep(0.01)
        raise AssertionError("任务没有收尾")

    record = asyncio.run(_run())
    assert len(calls) == 1, f"不重发：投递只允许 1 次调用，实际 {len(calls)}"
    assert record.state == tb.STATE_SUCCEEDED, "投递失败不改任务终态（结果仍可回看）"
    assert record.delivery.attempted is True and record.delivery.succeeded is False
    assert record.delivery.error == proactive.ERROR_PLATFORM
    # 状态工具会把这条失败记账暴露给模型（不静默）
    text = asyncio.run(plugin.browse_task_status(event, task_id=record.task_id))
    assert "投递失败" in text and proactive.ERROR_PLATFORM in text, text


#: 模型实际看到的参数表（**从 docstring 的 Args 块派生** —— AstrBot 的
#: ``register_llm_tool`` 只读 docstring，不读函数签名；`int` 会被映射成 JSON 的
#: ``number``，``integer`` 不是核心支持的类型）。五个工具的期望参数逐字在此。
EXPECTED_TOOL_ARGS = {
    "browse_web": [("input", "string")],
    "browse_local_page": [("path", "string"), ("full_page", "boolean"),
                          ("wait_ms", "number"), ("perception", "string")],
    "browser_web_search": [("query", "string"), ("max_results", "number"),
                           ("rounds", "number"), ("timeout", "number"),
                           ("providers", "string"), ("fetch_pages", "boolean"),
                           ("fetch_top_n", "number")],
    "browse_task_status": [("task_id", "string")],
    "browse_recent_results": [("task_id", "string"), ("limit", "number"),
                              ("minutes", "number")],
}

_DOC_ARG = re.compile(r"^\s{8,}(\w+)\((\w+)\)\s*:", re.MULTILINE)


def _declared_args(name: str) -> list[tuple[str, str]]:
    """取工具的参数表：优先用核心同款解析器（`docstring_parser`），否则退回正则。

    AstrBot 的 ``register_llm_tool`` 只解析 docstring 的 ``Args:`` 块 —— 不写在那里的
    参数**根本不会**出现在模型看到的 schema 里（这是 v1.7.5 既有工具的做法，本版照抄）。
    """
    doc = getattr(main_module.BrowserLLMPlugin, name).__doc__ or ""
    try:
        from docstring_parser import parse  # noqa: PLC0415 — 核心的生产依赖，测试里可选
    except Exception:  # noqa: BLE001 — 没装就退回正则（同一份 docstring，同一份事实）
        return _DOC_ARG.findall(doc.split("Args:", 1)[-1] if "Args:" in doc else "")
    return [(param.arg_name, param.type_name) for param in parse(doc).params]


@pytest.mark.parametrize("tool_name", tuple(EXPECTED_TOOL_ARGS))
def test_tool_schemas_are_derived_from_docstrings_and_match_the_signature(tool_name):
    """五工具的模型可见参数表 = 规格 §7.1/§7.2/§7.3 的期望，且与签名逐个对得上。

    没有这条守卫时最容易犯的错是"签名加了参数、docstring 没写 Args" ⇒ 核心派生出**零参数**
    的 schema（模型没法传参，工具永远跑默认值）。
    """
    import inspect

    declared = _declared_args(tool_name)
    assert declared == EXPECTED_TOOL_ARGS[tool_name], f"{tool_name} 的参数表漂移：{declared}"
    parameters = inspect.signature(getattr(main_module.BrowserLLMPlugin, tool_name)).parameters
    for arg_name, _type_name in declared:
        assert arg_name in parameters, f"{tool_name} 的 docstring 写了签名里没有的参数 {arg_name}"
    # 反向：签名里除 event 之外的每个参数都必须写进 docstring（否则模型看不见）
    documented = {name for name, _type in declared}
    missing = [name for name in parameters if name not in ("self", "event") and name not in documented]
    assert missing == [], f"{tool_name} 的签名参数没写进 Args（模型看不见）：{missing}"
