"""v1.7.5 工具面与装配契约（规格 §9 / §5.5 / §1.3，契约 t6 的守卫用例）。

钉六件事：

1. 内层工具 **26 → 5**（``browse_observe`` / ``browse_act`` / ``browse_screenshot``
   / ``browse_media`` / ``browse_search``），且被替代的旧方法**删除**（不保留薄壳）；
2. 外层 3 个名字**不变**（人格白名单按名匹配），AST 抽装饰器字面量与
   ``_ALL_TOOL_NAMES`` 一致；
3. ``browse_observe`` / ``browse_act`` 的 description **由 ACTION_SPEC 生成**，
   并逐字含规格 §5.5 的三句硬话；
4. 观测块只剩**一处**渲染：``verify.render_observation`` 与
   ``a11y.render_snapshot`` 逐字等价，且 ``core/`` 里不再有私有 ``_node_line`` /
   ``_snapshot_header`` 实现；
5. ``core/session.py`` 无起始页（``about:blank``）且提供按任务取页 ``claim_page``；
6. 规模守则：``main.py`` ≤ 4200 行、``core/agent.py`` 600–750 行。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import main as main_module  # noqa: E402 — conftest 已装好包别名与 astrbot 桩
from astrbot_plugin_browser_llm.core import action_model as am  # noqa: E402
from astrbot_plugin_browser_llm.core import a11y  # noqa: E402
from astrbot_plugin_browser_llm.core import agent as agent_core  # noqa: E402
from astrbot_plugin_browser_llm.core import session as session_core  # noqa: E402
from astrbot_plugin_browser_llm.core import verify as vf  # noqa: E402

EXPECTED_INNER = (
    "browse_observe",
    "browse_act",
    "browse_screenshot",
    "browse_media",
    "browse_search",
)
EXPECTED_OUTER = ("browse_web", "browse_local_page", "browser_web_search")

#: v1.7.0 的 26 个内层方法里，凡是**不再存在**的名字（覆盖 v1.7.5 §9.2 的合并面）。
REMOVED_INNER = (
    "browse_open",
    "browse_current_page",
    "browse_get_text",
    "browse_get_links",
    "browse_click_link",
    "browse_scroll",
    "browse_go_back",
    "browse_go_forward",
    "browse_new_tab",
    "browse_switch_tab",
    "browse_close_tab",
    "browse_input",
    "browse_press_key",
    "browse_click_text",
    "browse_set_slider",
    "browse_select_option",
    "browse_click_coords",
    "browse_hover",
    "browse_reload",
    "browse_checkbox",
    "browse_zoom_crop",
    "browse_sniff_media",
    "browse_get_interactive",
    "browse_click_selector",
    "browse_web_search",
)


# ---------------------------------------------------------------- 工具面


def test_inner_tool_surface_is_five():
    assert main_module.INNER_TOOL_NAMES == EXPECTED_INNER
    assert tuple(spec["name"] for spec in main_module.INNER_TOOLS) == EXPECTED_INNER
    assert len(main_module.INNER_TOOLS) == 5


def test_outer_tool_names_are_unchanged():
    assert main_module._ALL_TOOL_NAMES == EXPECTED_OUTER
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "llm_tool"
        ):
            continue
        kw = next((k for k in node.keywords if k.arg == "name"), None)
        if kw is None:
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            names.add(kw.value.value)
        elif isinstance(kw.value, ast.Name):  # 模块级常量（_SEARCH_TOOL_NAME 等）
            resolved = getattr(main_module, kw.value.id, None)
            if isinstance(resolved, str):
                names.add(resolved)
    assert names == set(EXPECTED_OUTER), f"外层工具名被改动：{sorted(names)}"


def test_removed_inner_methods_are_deleted_not_shimmed():
    for name in REMOVED_INNER:
        assert not hasattr(main_module.BrowserLLMPlugin, name), f"{name} 仍是薄壳"


def test_expected_inner_methods_exist_as_coroutines():
    import asyncio

    for name in EXPECTED_INNER:
        handler = getattr(main_module.BrowserLLMPlugin, name, None)
        assert handler is not None, f"缺少内层工具方法 {name}"
        assert asyncio.iscoroutinefunction(handler), f"{name} 不是协程方法"


def test_inner_toolset_builds_five_function_tools():
    plugin = main_module.BrowserLLMPlugin.__new__(main_module.BrowserLLMPlugin)
    plugin.metadata_name = main_module.METADATA_NAME
    plugin._browser_tools = []
    plugin._browser_toolset = None
    plugin._rebuild_browser_toolset()
    assert len(plugin._browser_tools) == 5
    assert [tool.name for tool in plugin._browser_tools] == list(EXPECTED_INNER)


# ---------------------------------------------------- description 由 ACTION_SPEC 生成


def test_hard_rules_are_verbatim_in_both_dispatchers():
    sentences = tuple(agent_core.INNER_RULES)
    assert len(sentences) == 3
    for spec in main_module.INNER_TOOLS:
        if spec["name"] not in ("browse_observe", "browse_act"):
            continue
        for sentence in sentences:
            assert sentence in spec["description"], (
                f"{spec['name']} 的 description 缺 §5.5 硬话：{sentence[:20]}…"
            )


def test_act_description_is_generated_from_action_spec():
    desc = next(s for s in main_module.INNER_TOOLS if s["name"] == "browse_act")["description"]
    assert am.render_action_catalog() in desc, "动作目录不是 render_action_catalog() 生成的"
    for kind in am.ACTION_SPEC:
        assert kind.value in desc, f"description 缺动作 {kind.value}"


def test_act_parameters_carry_the_frozen_action_schema():
    spec = next(s for s in main_module.INNER_TOOLS if s["name"] == "browse_act")
    schema = spec["parameters"]["properties"]["action"]
    assert spec["parameters"]["required"] == ["action"]
    assert schema.get("description"), "action 参数必须有 description"
    assert schema == am.ACTION_JSON_SCHEMA, "工具参数里的动作契约与 ACTION_SPEC 漂移了"


def test_observe_view_enum_matches_spec():
    spec = next(s for s in main_module.INNER_TOOLS if s["name"] == "browse_observe")
    assert main_module.OBSERVE_VIEWS == ("structure", "text", "links", "full")
    assert "view" in spec["parameters"]["properties"]


# ------------------------------------------------------------ 观测块单处渲染


def _snapshot(**kwargs):
    base = dict(
        url="https://shop.example/order",
        title="确认订单",
        viewport=(1280, 800),
        dpr=1,
        scroll_y=0,
        scroll_height=1600,
        nodes=(
            a11y.A11yNode(
                ref=1,
                role="button",
                name="提交",
                box=(10.0, 20.0, 100.0, 30.0),
            ),
            a11y.A11yNode(ref=2, role="radio", name="加急", checked=True, checked_raw="true"),
        ),
        pruned=0,
        frames=1,
        ssrf_blocked=0,
        truncated=False,
        degraded=(),
    )
    base.update(kwargs)
    return a11y.PageSnapshot(**base)


def test_observation_render_matches_a11y_exactly():
    snap = _snapshot()
    assert vf.render_observation(step=1, snapshot=snap) == a11y.render_snapshot(snap, step=1)


def test_core_has_single_node_line_renderer():
    hits: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_node_line",
                "_snapshot_header",
            ):
                hits.append(f"{path.name}:{node.lineno} def {node.name}")
    assert hits == ["a11y.py:{} def _node_line".format(
        next(
            node.lineno
            for node in ast.walk(ast.parse((ROOT / "core" / "a11y.py").read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name == "_node_line"
        )
    )], f"节点行渲染必须只剩 a11y 一处：{hits}"


# ------------------------------------------------------------------ 会话取页


def test_session_has_no_start_page_and_claims_by_task():
    import inspect

    sig = inspect.signature(session_core.SessionManager.__init__)
    # R15：`default_url` 形参已按 spec §7.4-5 删除，起始页写死在 _new_page_goto 里
    assert "default_url" not in sig.parameters
    assert hasattr(session_core.SessionManager, "claim_page")
    src = inspect.getsource(session_core.SessionManager.claim_page)
    assert "_close_all_pages" in src and "ensure_page" in src


# ------------------------------------------------------------------ 配置读取点


def test_config_reads_exist_in_v6_leaf_table():
    from core.config_map import LEAVES

    leaves = {leaf.path for leaf in LEAVES}
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    read_paths = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "." in node.args[0].value
    }
    assert read_paths, "没扫到任何点号路径读取 —— 口径崩了"
    missing = sorted(path for path in read_paths if path not in leaves)
    assert not missing, f"main.py 读了 v6 键表里没有的键：{missing}"


# ------------------------------------------------------------------ 规模守则


def test_line_budgets():
    main_lines = len((ROOT / "main.py").read_text(encoding="utf-8").splitlines())
    agent_lines = len((ROOT / "core" / "agent.py").read_text(encoding="utf-8").splitlines())
    assert main_lines <= 4200, f"main.py {main_lines} 行（守则 ≤ 4200）"
    assert 600 <= agent_lines <= 750, f"core/agent.py {agent_lines} 行（守则 600–750）"


# ------------------------------------------------------- 接线冒烟（真跑一遍链）

class _FakePage:
    """最小页面替身：没有 CDP 会话 ⇒ 感知层降级，但装配链必须**不崩**。"""

    url = "https://shop.example/order"
    viewport_size = {"width": 1280, "height": 800}

    async def title(self):
        return "确认订单"

    async def evaluate(self, *args, **kwargs):  # noqa: D401 — 只为 capture 提供状态
        return {}

    async def goto(self, url, **kwargs):
        self.url = url

    async def screenshot(self, **kwargs):
        """假截图：真 playwright 会把图写到 path，这里只回字节（图不存在 ⇒ 【不可用】）。"""
        return b"\x89PNG\r\n\x1a\n"


class _FakeSessions:
    def __init__(self):
        self.claims: list[tuple[str, str]] = []
        self._locks: dict[str, object] = {}

    def get_lock(self, umo):
        import asyncio

        return self._locks.setdefault(umo, asyncio.Lock())

    async def claim_page(self, umo, task_key=""):
        self.claims.append((umo, task_key))
        return _FakePage()


class _FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"

    def get_group_id(self):
        return "1"

    def get_sender_id(self):
        return "2"

    async def send(self, msg):  # pragma: no cover — 冒烟不发送
        self.sent = msg

    def image_result(self, path):
        return path

    def chain_result(self, chain):
        return chain


class _FakeSafety:
    def check_text(self, text):
        return True, ""

    def update_config(self, **kwargs):  # _refresh_config 会顺带同步禁词表
        return None

    async def acheck_url(self, url):  # pragma: no cover — media 冒烟不下载
        return True, ""


def _bare_plugin(tmp_path):
    plugin = main_module.BrowserLLMPlugin.__new__(main_module.BrowserLLMPlugin)
    plugin.metadata_name = main_module.METADATA_NAME
    plugin.config = {}
    plugin._load_config()
    plugin.browser = None
    plugin.sessions = _FakeSessions()
    plugin.extractor = None
    plugin.safety = _FakeSafety()
    plugin._cdp_pool = a11y.CdpSessionPool()
    plugin._tasks = {}
    plugin._local_page_locks = {}
    plugin._local_page_allowed_roots = ()
    plugin._search_service = None
    plugin._search_service_signature = None
    plugin._search_engines = {"必应搜索": "https://x/?q={keyword}"}
    plugin._watchdog = main_module.BudgetWatchdog()
    plugin._browser_tools = []
    plugin._browser_toolset = None
    plugin._agent_sync_task = None
    plugin._persona_cache = {}
    plugin._fg_inflight = {}
    # 产物目录走 pytest 的 tmp_path：**不**往仓库里写任何东西（repo hygiene）。
    plugin._screenshot_dir = tmp_path / "screenshots"
    plugin._media_dir = tmp_path / "media"
    plugin._screenshot_dir.mkdir(parents=True, exist_ok=True)
    plugin._media_dir.mkdir(parents=True, exist_ok=True)
    return plugin


def test_observe_goes_through_a11y_and_renders_observation_block(tmp_path):
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    state = plugin.reset_task(event.unified_msg_origin, budget_s=30)
    block = asyncio.run(plugin.browse_observe(event))
    assert block.startswith("【观测 #1】"), block[:80]
    assert "https://shop.example/order" in block
    # 没有 CDP 会话 ⇒ 感知层**显式降级**（不是静默空树）
    assert "感知降级" in block
    assert state.refs.next_ref >= 1
    # 任务键已交给会话管理器（按任务取页）
    assert plugin.sessions.claims and plugin.sessions.claims[0][1] == state.task_key


def test_act_rejection_is_structured_and_marks_ledger_status(tmp_path):
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    state = plugin.reset_task(event.unified_msg_origin, budget_s=30)
    block = asyncio.run(
        plugin.browse_act(
            event, {"action": "click", "target": {"role": "button", "name": "x", "css": "#a"}}
        )
    )
    assert "【拒绝】R-A3" in block, block
    assert state.pending == "rejected"
    # 台账 status 由包装层取走（取走后清空，避免污染下一步）
    assert plugin._ledger_status_of(event) == "rejected"
    assert plugin._ledger_status_of(event) == ""


# --------------------------------------------- SoM 真接上（盒子 → 标签 → 纪元）

def _actionable_snapshot():
    """两个可交互节点（role=button/link），**box 先留空**（等 locator 层补）。"""
    return a11y.PageSnapshot(
        url="https://shop.example/order",
        title="确认订单",
        viewport=(1280, 800),
        dpr=1,
        scroll_y=0,
        scroll_height=1600,
        nodes=(
            a11y.A11yNode(ref=1, role="button", name="提交", node_id=101),
            a11y.A11yNode(ref=2, role="link", name="返回首页", node_id=102),
        ),
        pruned=0,
        frames=1,
    )


def test_select_marks_needs_boxes_and_boxes_make_marks():
    """不给 boxes ⇒ 0 个标签（盒子是 SoM 的唯一来源）；补上盒子 ⇒ 标签非空。"""
    snap = _actionable_snapshot()
    empty, _ = a11y.select_marks(snap.nodes, max_marks=10)
    assert empty == (), "没有 box 的节点不该产出标签"

    boxes = {101: (10.0, 20.0, 100.0, 30.0), 102: (10.0, 60.0, 120.0, 24.0)}
    filled = main_module.BrowserLLMPlugin._with_boxes(snap, boxes)
    marks, truncated = a11y.select_marks(filled.nodes, max_marks=10)
    assert [mark.id for mark in marks] == [1, 2]
    assert all(mark.box is not None for mark in marks)
    assert truncated is False


def test_screenshot_warns_loudly_when_no_marks(tmp_path):
    """没有框/没有节点时必须**明确告警**，不许静默返回一张空标签表。"""
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    text = asyncio.run(plugin._screenshot_impl(event, target=None, full=False))
    assert "没有可标注的可交互节点" in text, text
    assert "browse_observe" in text


def test_screenshot_marks_nonempty_when_boxes_present(tmp_path, monkeypatch):
    """给 locator 层的框 ⇒ 标签表非空（且不再告警）。"""
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    snap = _actionable_snapshot()

    async def _fake_snapshot(page, state):
        state.snapshot = snap
        return snap

    async def _fake_boxes(page, snapshot, limit):
        return {101: (10.0, 20.0, 100.0, 30.0), 102: (10.0, 60.0, 120.0, 24.0)}, 0

    monkeypatch.setattr(plugin, "_snapshot", _fake_snapshot)
    monkeypatch.setattr(plugin, "_mark_boxes", _fake_boxes)
    text = asyncio.run(plugin._screenshot_impl(event, target=None, full=False))
    assert "labels=2" in text, text
    assert "没有可标注的可交互节点" not in text
    state = plugin._task_state(event.unified_msg_origin)
    assert state.marks.active or state.marks.result is not None


def test_mark_epoch_on_action_called_once_per_action(tmp_path, monkeypatch):
    """动作之后 `MarkEpoch.on_action` 恰好被调用一次（作废 mark 纪元的唯一入口）。"""
    import asyncio

    calls: list[str] = []
    original = a11y.MarkEpoch.on_action

    def _spy(self, action):
        calls.append(str(action))
        return original(self, action)

    monkeypatch.setattr(a11y.MarkEpoch, "on_action", _spy)
    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    plugin.reset_task(event.unified_msg_origin, budget_s=30)
    out = asyncio.run(
        plugin.browse_act(event, {"action": "navigate", "value": "https://example.com/a"})
    )
    assert out.startswith("【观测 #"), out[:80]
    assert calls == ["navigate"], calls


def test_page_close_notifies_drop_hooks(tmp_path):
    """页面关闭 ⇒ 关页回调被调用一次（插件用它 `pool.drop` + `executor.drop_cdp`）。"""
    import asyncio

    from astrbot_plugin_browser_llm.core import session as session_mod

    dropped: list[object] = []

    class _Browser:
        async def close_page(self, page):
            return None

    page = object()
    mgr = session_mod.SessionManager(
        _Browser(), on_page_closed=dropped.append
    )
    asyncio.run(mgr._close_page("umo", page))
    assert dropped == [page]

    # 插件侧回调真的把两条缓存都摘了（id(page) 为键 ⇒ 不摘就是泄漏）
    plugin = _bare_plugin(tmp_path)
    plugin._forget_page_caches(page)  # 不抛即通过；两条 hook 都是幂等的
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "self._cdp_pool.clear()" in src, "terminate 必须清空感知层 CDP 会话池"
    assert "ex.drop_cdp(page)" in src


# ------------------------------------------- 无起始页 / 观测首行的两个定钉


def test_browse_web_requires_url_when_no_target(tmp_path):
    """v1.7.5 移除起始页：没有显式网址时**要网址**，绝不静默打开默认站。"""
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    out = asyncio.run(plugin.browse_web(event, "帮我看看这个页面写了什么"))
    assert out == main_module._NEED_URL_TEXT
    assert main_module._has_explicit_url("帮我看看这个页面写了什么") is False
    # 显式网址 / 裸域名都算目标：此时**不再**回【需要网址】（后续失败与本题无关）
    assert main_module._has_explicit_url("打开 https://example.com/a 看看")
    assert main_module._has_explicit_url("去 example.com/a 看看")
    assert asyncio.run(plugin.browse_web(event, "no-url")) == main_module._NEED_URL_TEXT


def test_browse_web_allows_url_task_past_the_url_gate(tmp_path):
    import asyncio

    plugin = _bare_plugin(tmp_path)
    event = _FakeEvent()
    out = asyncio.run(plugin.browse_web(event, "打开 https://example.com/a"))
    assert out != main_module._NEED_URL_TEXT


def test_observation_header_omits_pruned_segment_when_zero():
    """spec §5.4：`(+N 已裁)` 只在 pruned>0 时出现（pruned==0 不许写 `(+0 已裁)`）。"""
    plain = _actionable_snapshot()
    head = a11y.observation_header(plain, step=2)
    assert "已裁" not in head, head
    assert "nodes=2" in head

    pruned = a11y.PageSnapshot(
        url=plain.url,
        title=plain.title,
        viewport=plain.viewport,
        dpr=plain.dpr,
        scroll_y=plain.scroll_y,
        scroll_height=plain.scroll_height,
        nodes=plain.nodes,
        pruned=3,
        frames=1,
    )
    assert "(+3 已裁)" in a11y.observation_header(pruned, step=2)
