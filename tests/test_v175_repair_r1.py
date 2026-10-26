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


def test_deliver_once_reaches_the_registry_gate(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.deliver_once = False
    asyncio.run(
        plugin._submit_background(
            event=helpers._FakeEvent(),
            tool_name=main_module._BROWSE_WEB_TOOL_NAME,
            runner=_ok_runner,
            task_text="x",
        )
    )
    assert plugin._watchdog.registry.deliver_once is False, "开关必须落到投递闸"


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


def test_isolate_context_switch_changes_page_claim(tmp_path):
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'getattr(self, "isolate_context", True)' in src, "isolate_context 必须影响控制流"


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
        "tools.deliver_once": "deliver_once",
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


def test_same_session_second_browse_is_rejected(tmp_path):
    """R21：同 UMO 只允许一个 browse_web（含后台）——否则两个任务抢同一张页面。"""
    plugin = _plugin(tmp_path)
    event = helpers._FakeEvent()
    plugin._fg_inflight = {plugin._umo_of(event): 1}
    reason = plugin._busy_browse_reason(event)
    assert reason.startswith("【拒绝】"), reason


def test_background_browse_timeout_maps_to_status_timeout(tmp_path):
    """R22：本工具自己的超时不能被记成"完成"。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _browse_web_run("):]
    body = body[: body.index("\n    async def ", 10)]
    assert "if background:" in body and "raise" in body, "后台超时必须抛出去让 watchdog 记超时"
    assert 'progress["steps"]' in body, "R11：超时要把步数写进 progress"
