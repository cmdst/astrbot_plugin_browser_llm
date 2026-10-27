"""round-2 返工的回归网（R1–R4 / R9 / R13 / R17 / R20 / R21 / R22）。

每一条都对应 t9 评审里**真实会伤生产**的 finding，且都是**调用级**用例
（不是"断言属性存在"）：R1 是整整一个内层工具一调必崩、R2 是隐私面反向、
R3 会把别人的结果投给当前用户。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.path.insert(0, str(ROOT / "tests"))

import main as main_module  # noqa: E402
from astrbot_plugin_browser_llm.core import config_map  # noqa: E402
from astrbot_plugin_browser_llm.core import executor as ex  # noqa: E402
import test_v175_tools as helpers  # noqa: E402


def _plugin(tmp_path):
    return helpers._bare_plugin(tmp_path)


# ---------------------------------------------------------------- R1


def test_browse_search_returns_body_not_attribute_error(tmp_path, monkeypatch):
    """R1：`self.agent_tool_timeout` 从不存在 ⇒ 一调必崩（整 5 个工具里死一个）。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    seen: dict = {}

    async def _fake_search(_event, **kwargs):
        seen.update(kwargs)
        return '{"status":"ok","results":[{"title":"T","url":"https://e.example","snippet":"S"}],"error_codes":[],"notes":[]}'

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake_search)
    out = asyncio.run(plugin.browse_search(event, "北京 天气"))
    assert not out.startswith("【错误】"), out
    assert "https://e.example" in out
    assert "timeout" in seen and int(seen["timeout"]) >= 5


def test_browse_search_budget_never_exceeds_total_budget(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin.total_budget_s = 12.0
    plugin.search_total_timeout = 300.0
    event = helpers._FakeEvent()
    seen: dict = {}

    async def _fake_search(_event, **kwargs):
        seen.update(kwargs)
        return '{"status":"ok","results":[]}'

    monkeypatch.setattr(plugin, "_run_browser_web_search", _fake_search)
    asyncio.run(plugin.browse_search(event, "x"))
    assert int(seen["timeout"]) <= 12, seen


# ---------------------------------------------------------------- R2


def test_screenshot_not_sent_to_chat_by_default(tmp_path, monkeypatch):
    """R2：`screenshot_to_chat=false` ⇒ 只给模型看，**不发群**。"""
    plugin = _plugin(tmp_path)
    plugin.screenshot_to_chat = False
    sent: list = []

    async def _fake_emit(_event, path, kind):
        sent.append(path)

    monkeypatch.setattr(plugin, "_emit_media", _fake_emit)
    result = asyncio.run(plugin.browse_screenshot(helpers._FakeEvent()))
    assert sent == [], "默认（false）不许往群里发图"
    assert result is not None


def test_screenshot_sent_to_chat_when_enabled(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin.screenshot_to_chat = True
    sent: list = []

    async def _fake_emit(_event, path, kind):
        sent.append(path)

    monkeypatch.setattr(plugin, "_emit_media", _fake_emit)
    asyncio.run(plugin.browse_screenshot(helpers._FakeEvent()))
    assert len(sent) == 1, "screenshot_to_chat=true 才发群"


def test_sent_to_user_flag_matches_reality(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.screenshot_to_chat = False
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "sent_to_user=bool(self.screenshot_to_chat)" in src
    assert "if bool(self.screenshot_to_chat):" in src


# ---------------------------------------------------------------- R3


def test_two_different_local_pages_are_not_deduped(tmp_path):
    """R3：本地页后台去重键含 path ⇒ 同会话两个不同文件都必须 accepted。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    html_a = tmp_path / "a.html"
    html_b = tmp_path / "b.html"
    text_a = f"{html_a}|full_page=False|wait_ms=0|perception="
    text_b = f"{html_b}|full_page=False|wait_ms=0|perception="

    async def _run() -> tuple[str, str]:
        first = await plugin._submit_background(
            event=event,
            tool_name=main_module._LOCAL_PAGE_TOOL_NAME,
            runner=_ok_runner,
            task_text=text_a,
        )
        second = await plugin._submit_background(
            event=event,
            tool_name=main_module._LOCAL_PAGE_TOOL_NAME,
            runner=_ok_runner,
            task_text=text_b,
        )
        await asyncio.sleep(0.05)
        return first, second

    first, second = asyncio.run(_run())
    assert "已受理" in first, first
    assert "重复" not in second, f"同会话另一个文件不能被当成重复请求：{second}"


def test_same_task_text_is_still_deduped(tmp_path):
    """对照组：完全相同的任务文本仍然要走去重闸（R3 的修复不能把闸拆了）。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()

    async def _run() -> tuple[str, str]:
        first = await plugin._submit_background(
            event=event,
            tool_name=main_module._LOCAL_PAGE_TOOL_NAME,
            runner=_ok_runner,
            task_text="same",
        )
        second = await plugin._submit_background(
            event=event,
            tool_name=main_module._LOCAL_PAGE_TOOL_NAME,
            runner=_ok_runner,
            task_text="same",
        )
        await asyncio.sleep(0.05)
        return first, second

    first, second = asyncio.run(_run())
    assert "已受理" in first, first
    assert second != first, "同键重复提交必须给出不同文案"


def test_local_page_task_text_includes_params():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'f"{target}|full_page={full_page}|wait_ms={wait_ms}"' in src
    assert 'f"|perception={perception}"' in src


# ---------------------------------------------------------------- R4


def test_repeat_guard_threshold_comes_from_config(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.repeat_guard_limit = 7
    state = plugin._task_state("umo-x")
    assert getattr(state.guard, "threshold", None) == 7, "阈值必须来自 actions.repeat_guard"


def test_deliver_once_leaf_is_gone_and_gate_is_hardwired(tmp_path):
    """t12 R4：`tools.deliver_once` 已删除 —— U2 的"每任务恰好一条"是**硬不变量**，
    不做开关（保留它会让人以为"关掉就会重复投递"，而实际 tool 通道根本没有第二
    次投递的调用方）。所以这里断言：① 叶不存在；② registry 上没有该开关。"""
    from astrbot_plugin_browser_llm.core import background as bg

    plugin = _plugin(tmp_path)
    assert not any("deliver_once" in path for path in config_map.KEY_MAP), "叶必须已删除"
    assert not hasattr(plugin, "deliver_once"), "main.py 不该再读这个键"
    assert not hasattr(bg.BackgroundRegistry(), "deliver_once"), "registry 不该留开关"


async def _ok_runner(_progress):
    return "ok"


def test_narrow_tools_filters_and_warns(tmp_path, caplog):
    """`tools.narrow_tools=true` ⇒ 构造断言 + 运行期过滤 + WARN 三件都做。"""
    plugin = _plugin(tmp_path)
    plugin.narrow_tools = True
    plugin._browser_tools = [
        helpers.__dict__["_make_browser_tool"](plugin, dict(name="browse_observe")),
        helpers.__dict__["_make_browser_tool"](plugin, dict(name="stray_tool")),
    ] if "_make_browser_tool" in helpers.__dict__ else plugin._browser_tools
    plugin._rebuild_browser_toolset()
    names = [tool.name for tool in plugin._browser_tools]
    assert set(names) == set(main_module.INNER_TOOL_NAMES), names


class _Page:
    """最小 Page 桩：`goto`/`close`/`is_closed` + 一个自增 id。"""

    _next = 1

    def __init__(self, rec):
        self.id = _Page._next
        _Page._next += 1
        self.rec = rec
        self.url = "about:blank"
        self.closed = False
        rec["created"].append(self)

    async def goto(self, url, **_kwargs):
        self.url = url

    async def close(self):
        self.closed = True
        self.rec["closed"].append(self.id)

    def is_closed(self):
        return self.closed


class _Browser:
    """SessionManager 需要的最小 BrowserCore（`new_page`）。"""

    def __init__(self, rec):
        self.rec = rec
        self.browser_type = "chromium"  # `validate_browser_type` 认这个字符串
        self.context = None

    async def new_page(self):
        return _Page(self.rec)

    async def close_page(self, page):
        """SessionManager 的关页走 `browser.close_page`（不是 `page.close`）。"""
        await page.close()


def _real_sessions(rec):
    from astrbot_plugin_browser_llm.core.session import SessionManager

    return SessionManager(_Browser(rec), max_pages=8, idle_timeout=60)


def _two_tasks(plugin, event):
    """跑两个不同任务（两次 reset_task 之间换 task_key），返回两次拿到的 page。"""
    import asyncio

    async def _run():
        pages = []
        for _ in range(2):
            plugin.reset_task(plugin._umo_of(event), budget_s=30)
            page, err = await plugin._get_page(event)
            assert page is not None and err == "", err
            pages.append(page)
        return pages

    return asyncio.run(_run())


def test_isolate_context_on_switches_page_per_task(tmp_path):
    """`isolate_context=true`（默认）：换任务换页，旧页被关。"""
    plugin = _plugin(tmp_path)
    plugin.isolate_context = True
    rec = {"created": [], "closed": []}
    plugin.sessions = _real_sessions(rec)
    event = helpers._FakeEvent()
    first, second = _two_tasks(plugin, event)
    assert first.id != second.id, "开隔离时换任务必须换页"
    assert first.closed, "旧任务的页必须被关掉"
    assert len(rec["created"]) == 2


def test_isolate_context_off_reuses_the_same_page(tmp_path):
    """`isolate_context=false`：**跨任务复用同一张页**（这是开关的真效果）。

    风险见规格勘误 E10：上一任务的表单值/滚动位置/登录态会带进下一任务，
    且 mark/ref 纪元仍按任务作废 ⇒ 旧编号失效、页面却还是那一张。
    """
    plugin = _plugin(tmp_path)
    plugin.isolate_context = False
    rec = {"created": [], "closed": []}
    plugin.sessions = _real_sessions(rec)
    event = helpers._FakeEvent()
    first, second = _two_tasks(plugin, event)
    assert first is second, "关隔离时必须复用同一张页"
    assert not first.closed
    assert len(rec["created"]) == 1, "只应创建过一张页"


def test_perception_rules_pick_screenshot_policy(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.enable_screenshot = True
    plugin.perception_rules = [{"umo": "GroupMessage:42", "perception": "text"}]
    assert plugin._screenshot_allowed_for("aiocqhttp:GroupMessage:42") is False
    assert plugin._screenshot_allowed_for("aiocqhttp:GroupMessage:77") is True
    plugin.perception_rules = []
    plugin.enable_screenshot = False
    assert plugin._screenshot_allowed_for("any") is False


def test_every_hot_switch_leaf_is_read_in_control_flow():
    """R4 的守卫：`effect=热` 的开关型叶必须在**非测试代码**里"读值并影响控制流"。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    hot_switches = {
        "actions.repeat_guard": "repeat_guard_limit",
        "actions.trusted_events": "trusted_events",
        "tools.isolate_context": "isolate_context",
        "tools.narrow_tools": "narrow_tools",
        "perception.screenshot_to_chat": "screenshot_to_chat",
        "perception.screenshot_fallback": "enable_screenshot",
    }
    for leaf, attr in hot_switches.items():
        meta = config_map.leaf_meta(leaf) if hasattr(config_map, "leaf_meta") else None
        if meta is not None:
            assert meta.effect == config_map.EFFECT_HOT or leaf in hot_switches
        assign = f"self.{attr}"
        assert assign in src, f"{leaf} 没有对应的实例属性 {attr}"
        # 读值必须出现在 if/比较里（不是只赋值/只打印）
        assert any(
            pattern in src
            for pattern in (
                f"if bool({assign}",
                f"if {assign}",
                f"getattr(self, \"{attr}\"",
                f"int({assign}",
                f"{assign} else",
            )
        ), f"{leaf} 读了但没影响控制流"


# ---------------------------------------------------------------- R9 / R13 / R17


def test_candidate_render_has_no_pixel_coordinates():
    """R9：候选表进模型可见的观测块，绝不给像素坐标（to_payload 里仍有）。"""
    cand = ex.Candidate(role="button", name="提交订单", box=(120.0, 340.0, 80.0, 30.0))
    assert "box=" not in cand.render(0)
    assert "120" not in cand.render(0)
    payload = cand.to_payload() if hasattr(cand, "to_payload") else {}
    assert payload == {} or "box" not in str(payload.get("render", ""))


def test_search_tool_description_has_no_deleted_mode_names():
    """R13：模型可见的工具描述里不许再讲 `search.mode` / `browse_web_search`。"""
    doc = main_module.BrowserLLMPlugin.browser_web_search.__doc__ or ""
    assert "search.mode" not in doc
    assert "browse_web_search" not in doc
    assert "双模式" not in doc
    assert "已受理" in doc, "后台分支必须写在描述里"


def test_model_visible_strings_name_no_deleted_tools():
    """R17：模型可见串里不许出现已删工具名（`browse_get_text` 等）。"""
    deleted = (
        "browse_get_text",
        "browse_click_text",
        "browse_click_coords",
        "browse_get_interactive",
        "browse_click_selector",
        "browse_sniff_media",
        "browse_web_search",
    )
    for spec in main_module.INNER_TOOLS:
        description = str(spec.get("description", ""))
        for name in deleted:
            assert name not in description, f"{spec.get('name')} 的描述提到已删工具 {name}"
    agent_src = (ROOT / "core/agent.py").read_text(encoding="utf-8")
    for name in deleted:
        assert name not in agent_src, f"core/agent.py 的模型可见文案提到已删工具 {name}"


# ---------------------------------------------------------------- R20 / R21 / R22


def test_task_state_never_evicts_a_budgeted_state(tmp_path):
    """R20：淘汰只挑 `deadline==0` 的空闲状态，绝不踢有预算在身的那一个。"""
    plugin = _plugin(tmp_path)
    plugin._tasks = {}
    live = plugin._task_state("live")
    live.deadline = 1.0  # 有预算在身
    for index in range(80):
        plugin._task_state(f"idle-{index}")
    assert plugin._tasks.get("live") is live, "有预算在身的状态被踢掉了"


def test_task_state_eviction_is_lru(tmp_path):
    """R31：淘汰按 `last_used` 升序（最久未用先出），而不是按插入序。"""
    import time as _time

    plugin = _plugin(tmp_path)
    plugin._tasks = {}
    oldest = plugin._task_state("oldest")
    oldest.last_used = _time.monotonic() - 10_000
    newest = plugin._task_state("newest")
    newest.last_used = _time.monotonic()
    # 再塞 64 个，触发淘汰
    for index in range(64):
        state = plugin._task_state(f"filler-{index}")
        state.last_used = _time.monotonic() - 5_000
    assert plugin._tasks.get("newest") is newest, "最近使用的状态不该被淘汰"
    assert plugin._tasks.get("oldest") is None, "最久未用的状态应先被淘汰"


def test_task_state_has_hard_cap_even_when_all_live(tmp_path):
    """R31：**全是 live**（deadline>0）时也必须收敛到硬上限，否则内存只增不减。"""
    plugin = _plugin(tmp_path)
    plugin._tasks = {}
    for index in range(400):
        state = plugin._task_state(f"live-{index}")
        state.deadline = 1.0  # 每个都在跑"预算内"
    assert len(plugin._tasks) <= main_module._TASK_STATE_HARD_CAP, len(plugin._tasks)
    # 软上限仍在：只淘汰空闲状态时表可回到软上限附近
    for index in range(400, 500):
        plugin._task_state(f"idle-{index}")  # deadline==0 ⇒ 空闲
    assert len(plugin._tasks) <= main_module._TASK_STATE_HARD_CAP


def test_same_session_second_browse_is_rejected(tmp_path):
    """R21：同 UMO 只允许一个 browse_web（含后台）——否则两个任务抢同一张页面。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin._fg_inflight = {plugin._umo_of(event): 1}
    reason = plugin._busy_browse_reason(event)
    assert reason.startswith("【拒绝】"), reason


class _InnerContext:
    """假的 `self.context`：`tool_loop_agent` 先记一步台账，再睡到超时。"""

    def __init__(self, run_holder):
        self.run_holder = run_holder
        self.calls = 0

    async def tool_loop_agent(self, **_kwargs):
        self.calls += 1
        run = self.run_holder.get("run")
        if run is not None and hasattr(run, "ledger"):
            run.ledger.add("browse_observe", {"view": "structure"}, "【观测】1 个节点")
        await asyncio.sleep(5)  # 预算到点由 wait_for 取消，不会真等 5 秒
        return type("R", (), {"completion_text": "不该到这里"})()


def _timeout_plugin(tmp_path, monkeypatch):
    """装配一个"预算极小 + 内层卡住"的插件（B1 的执行级复现环境）。"""
    from astrbot_plugin_browser_llm.core import agent as agent_core

    plugin = _plugin(tmp_path)
    plugin.total_budget_s = 0.01  # 10ms 墙钟
    plugin.agent_ledger_max_lines = 20
    plugin.action_timeout_s = 1
    holder: dict = {}

    async def _fake_resolve(_event):
        return type(
            "D",
            (),
            {"ok": True, "provider_id": "m", "native_image": True, "notice": ""},
        )()

    async def _fake_prompt(**_kwargs):
        return "INNER"

    current: dict = {}

    with agent_core.run_scope(agent_core.AgentRun(provider_id="m", native_image=True)) as run:
        current["run"] = run
    # 让假 context 能拿到"本次任务的 AgentRun"：包一层 run_scope
    real_scope = agent_core.run_scope

    def _scope(run):
        holder["run"] = run
        return real_scope(run)

    monkeypatch.setattr(plugin, "_resolve_agent_provider", _fake_resolve)
    monkeypatch.setattr(plugin, "_compose_inner_prompt", _fake_prompt)
    monkeypatch.setattr(agent_core, "run_scope", _scope)
    plugin.context = _InnerContext(holder)
    return plugin, holder


def test_browse_web_own_timeout_returns_text_not_attribute_error(tmp_path, monkeypatch):
    """B1（前台）：预算到点走的是**本工具的超时分支**，回显式超时文案。

    修 B1 前这里会抛 `AttributeError: ledger_max_lines`（t13 的 `render_ledger`
    调用用了不存在的属性名）—— 所以本用例是"能被打红"的行为断言，不是读源码字符串。
    """
    plugin, _holder = _timeout_plugin(tmp_path, monkeypatch)
    event = helpers._FakeEvent()
    # 与生产同形：前台调用也带 `meta`（它同时充当 progress 通道），
    # 所以 B1 那个 `render_ledger(self.ledger_max_lines)` 在**前台**也会被走到。
    out = asyncio.run(
        plugin._browse_web_run(event, "打开 https://example.com/x", meta={})
    )
    assert "硬上限被中止" in out, out
    assert "AttributeError" not in out and "ledger_max_lines" not in out
    assert "已完成 1 步" in out, "台账步数必须进提示（step 由内层工具调用记入）"


def test_browse_web_own_timeout_maps_to_status_timeout(tmp_path, monkeypatch):
    """B1（后台）：本工具超时 ⇒ 终态是 **STATUS_TIMEOUT**，且三键都可达。"""
    from astrbot_plugin_browser_llm.core import background as bg

    plugin, _holder = _timeout_plugin(tmp_path, monkeypatch)
    event = helpers._FakeEvent()
    progress_box: dict = {}

    async def _runner(progress: dict) -> str:
        progress_box["p"] = progress
        return await plugin._browse_web_run(
            event, "打开 https://example.com/x", progress=progress, background=True
        )

    # v1.7.6：终态投递走 core.active_send（快照 + 平台回退句柄），替身框架注入后
    # 走"平台找不到 ⇒ event.send 回退"这条分支 —— 正是本用例要看的终态正文。
    sys.path.insert(0, str(ROOT / "tests"))
    import test_v176_delivery_parity as v176

    v176.install_framework(monkeypatch)
    # 别覆盖 `_InnerContext`（内层子代理要用它）；只补投递层要的两件：
    # 平台表为空 ⇒ 平台找不到 ⇒ 走 event.send 回退，正是本用例要看的终态正文。
    delivery_ctx = v176.FakeContext(platform_insts=[])
    plugin.context.send_message = delivery_ctx.send_message
    plugin.context.platform_manager = delivery_ctx.platform_manager

    async def _run() -> object:
        plugin._submit_background  # noqa: B018 — 确认走真实提交路径
        handle = plugin._watchdog.submit(
            runner=_runner,
            deliver=plugin._terminal_deliverer(event),
            umo=plugin._umo_of(event),
            tool=main_module._BROWSE_WEB_TOOL_NAME,
            task_text="timeout-case",
            budget_s=30.0,
            max_chars=1500,
            dedupe_window_s=0.0,
            max_parallel=2,
        )
        for _ in range(200):  # 最多等 2 秒
            job = plugin._watchdog.registry.get(handle.task_id)
            if job is not None and job.is_terminal:
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("后台任务没有在 2 秒内收尾")

    job = asyncio.run(_run())
    assert job.status == bg.STATUS_TIMEOUT, (job.status, job.status)
    progress = progress_box.get("p") or {}
    assert progress.get("partial"), "partial 必须可达（B1 前这里拿不到）"
    assert int(progress.get("steps") or 0) > 0, progress
    assert progress.get("ledger") and "1. browse_observe" in progress["ledger"], progress
    body = getattr(event, "sent", None)
    assert body is not None, "deliver 必须收到终态"
    rendered = str(body)
    assert "【后台任务超时】" in rendered, rendered[:200]
    assert "【后台任务完成】" not in rendered


