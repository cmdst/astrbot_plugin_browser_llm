"""v1.7.0 外层工具面与委派入口契约（规格 §1.2、§1.4、§7.1）。

四条判据（逐条钉死）：

1. ``browse_web`` 只有 **1 个** ``@filter.llm_tool(name="browse_web")``；
2. 三个外层工具名 == ``_ALL_TOOL_NAMES``（AST 抽装饰器字面量，不许出现第 4 个）；
3. 前台成功返回**以 ``C_UNTRUSTED_PREFIX`` 开头**且含 ``【执行台账】``；
   ``【拒绝】/【错误】`` 路径**不带**前缀；
4. plugin 引擎的后台终态消息**不含**「不可信外部内容」（消费者是人）。

另外钉住"不引入别名机制"与"内层工具集不含 send_message_to_user"
（``_BROWSER_TOOLS`` 是能力白名单，规格 §9.2）。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from core import agent as agent_core
from core.background import C_UNTRUSTED_PREFIX
from core.safety import SafetyFilter
from main import (
    _ALL_TOOL_NAMES,
    _BROWSE_WEB_TOOL_NAME,
    _LOCAL_PAGE_TOOL_NAME,
    _SEARCH_TOOL_NAME,
    BrowserLLMPlugin,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def __init__(self):
        self.sent = []

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def image_result(self, path):
        return ("image", path)

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, msg):
        self.sent.append(msg)
        return None


class _Ctx:
    def __init__(self, *, reply="子代理结论", failure=None):
        self.reply = reply
        self.failure = failure
        self.sent = []

    async def get_current_chat_provider_id(self, umo=None):
        return "prov-inherit"

    async def tool_loop_agent(self, **kwargs):
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(completion_text=self.reply)

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


def _plugin(config=None, ctx=None):
    cfg = config if isinstance(config, _Cfg) else _Cfg(config or {})
    plugin = BrowserLLMPlugin(ctx or _Ctx(), cfg)
    plugin.config = cfg
    plugin.sessions = None
    return plugin


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# 1/2：工具名与装饰器
# ---------------------------------------------------------------


def _module_string_constants() -> dict[str, str]:
    """模块级字符串常量（``_SEARCH_TOOL_NAME = "browser_web_search"`` 这种）。"""
    out: dict[str, str] = {}
    for node in TREE.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[target.id] = node.value.value
    return out


_MODULE_CONSTS = _module_string_constants()


def _decorated_tool_names() -> list[str]:
    names: list[str] = []
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute) or func.attr != "llm_tool":
                continue
            if not dec.keywords and not dec.args:
                continue
            literal = None
            for kw in dec.keywords:
                if kw.arg != "name":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    literal = kw.value.value
                elif isinstance(kw.value, ast.Name):
                    # ``name=_SEARCH_TOOL_NAME``：按模块级常量解析（改名会连带改它）
                    literal = _MODULE_CONSTS.get(kw.value.id, f"<{kw.value.id}>")
            if literal is None and dec.args and isinstance(dec.args[0], ast.Constant):
                literal = dec.args[0].value
            names.append(literal or f"<{node.name}>")
    return names


def test_exactly_three_outer_tools_with_pinned_names():
    names = _decorated_tool_names()
    assert sorted(names) == sorted(_ALL_TOOL_NAMES)
    assert len(names) == 3, "不新增第 4 个外层入口"
    assert set(names) == {"browse_web", "browse_local_page", "browser_web_search"}
    assert _BROWSE_WEB_TOOL_NAME == "browse_web"
    assert _LOCAL_PAGE_TOOL_NAME == "browse_local_page"
    assert _SEARCH_TOOL_NAME == "browser_web_search"


def test_all_tool_names_is_the_single_source_of_truth():
    assert _ALL_TOOL_NAMES == (
        _BROWSE_WEB_TOOL_NAME,
        _LOCAL_PAGE_TOOL_NAME,
        _SEARCH_TOOL_NAME,
    )


def test_no_alias_mechanism_introduced():
    """v1.7.0 不改名 ⇒ 不引入别名/双注册（规格 §7.1 / §11-N4）。"""
    assert "def _alias_tool" not in SRC
    assert "register_alias" not in SRC
    assert SRC.count('@filter.llm_tool(name="browse_web")') == 1
    assert SRC.count("@filter.llm_tool(name=_SEARCH_TOOL_NAME)") == 1
    assert SRC.count("@filter.llm_tool(") == 3, "只有 3 个外层工具装饰器"


def test_browse_web_signature_unchanged():
    """``browse_web`` 参数仍只有 ``input: str``（感知模式仍走前缀）。"""
    node = next(
        n
        for n in ast.walk(TREE)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_web"
    )
    args = [a.arg for a in node.args.args][1:]  # 去掉 self
    assert args == ["event", "input"]
    doc = ast.get_docstring(node) or ""
    assert "Args:" in doc and "input(string)" in doc


def test_shared_execution_body():
    """三个入口共享同一执行体/工具集构造（不许各写一套 agent loop）。"""
    assert SRC.count("def _browse_web_run") == 1
    assert SRC.count("context.tool_loop_agent(") == 1, "内层代理只在一个地方被调用"
    assert SRC.count("def _rebuild_browser_toolset") == 1
    assert SRC.count("def _make_inner_search_tool") == 1


def _code_references(name: str) -> int:
    """源码里**作为代码**引用该名字的次数（不含注释/文档字符串）。"""
    hits = 0
    for node in ast.walk(TREE):
        if isinstance(node, ast.Name) and node.id == name:
            hits += 1
        elif isinstance(node, ast.Attribute) and node.attr == name:
            hits += 1
        elif isinstance(node, ast.Constant) and node.value == name:
            hits += 1
    return hits


def test_inner_toolset_excludes_messaging_capability():
    """内层工具集是能力白名单：没有 send_message_to_user（规格 §9.2）。

    按 **AST** 判"代码里没有引用"，不按源码字符串判：C 模式的注释/文档字符串
    里必须能提到 ``send_message_to_user``（唤醒轮要靠它转发媒体），把它们一起
    禁掉等于删掉运营文档。
    """
    assert _code_references("send_message_to_user") == 0
    plugin = _plugin()
    plugin._rebuild_browser_toolset()
    names = {t.name for t in plugin._browser_tools}
    assert "send_message_to_user" not in names
    assert names == {spec["name"] for spec in main_module._BROWSER_TOOLS}


# ---------------------------------------------------------------
# 3/4：返回体前缀
# ---------------------------------------------------------------


class _TappingCtx(_Ctx):
    """回放一次内层工具调用（这样台账里才有一步可查）。"""

    async def tool_loop_agent(self, **kwargs):
        tools = kwargs["tools"]
        tool = next(t for t in tools.tools if t.name == "browse_get_links")
        await tool.handler(_Event())
        return SimpleNamespace(completion_text=self.reply)


def test_foreground_success_has_prefix_and_ledger():
    ctx = _TappingCtx(reply="结论：页面上有 3 个选项")
    plugin = _plugin({}, ctx)
    plugin._get_page = _stub_page()
    out = _run(plugin.browse_web(_Event(), "看看页面"))
    assert out.startswith(C_UNTRUSTED_PREFIX)
    assert "结论：页面上有 3 个选项" in out
    assert "【执行台账】" in out


def test_reject_path_has_no_prefix():
    """会话黑名单 ⇒ 【拒绝】，不套前缀（否则会变成"外部网页说的拒绝"）。"""
    plugin = _plugin({"security": {"session_blacklist": ["123"]}})
    out = _run(plugin.browse_web(_Event(), "看看页面"))
    assert out.startswith("【拒绝】")
    assert C_UNTRUSTED_PREFIX not in out


def test_error_path_has_no_prefix():
    plugin = _plugin({}, _Ctx(failure=RuntimeError("boom")))
    plugin._get_page = _never_page()
    out = _run(plugin.browse_web(_Event(), "看看页面"))
    assert out.startswith("【错误】")
    assert C_UNTRUSTED_PREFIX not in out


def test_provider_error_is_synchronous_text():
    """取不到当前会话模型 ⇒ 前台一条同步【错误】（不是异步终态）。"""
    ctx = _Ctx()

    async def _no_provider(umo=None):
        return ""

    ctx.get_current_chat_provider_id = _no_provider
    plugin = _plugin({}, ctx)
    out = _run(plugin.browse_web(_Event(), "任务"))
    assert out.startswith("【错误】无法确定当前对话模型")


def test_banned_task_description_is_rejected_synchronously():
    """新入口参数校验不绕过禁词（任务描述命中 ⇒ 【拒绝】）。"""
    plugin = _plugin(
        {"security": {"banned_words": ["赌博"], "enable_banned_filter": True}}
    )
    plugin.safety = SafetyFilter(["赌博"], True)
    out = _run(plugin.browse_web(_Event(), "去赌博网站看看"))
    assert out.startswith("【拒绝】")
    assert "违禁" in out


def test_plugin_background_terminal_has_no_prefix(monkeypatch):
    """plugin 引擎的后台终态消息**不含**不可信前缀（消费者是人）。"""
    sent: list = []

    class _RecCtx(_Ctx):
        async def send_message(self, session, chain):
            sent.append(chain)
            return True

    plugin = _plugin(
        {"tools": {"background_mode": "browse_only", "background_timeout": 60}},
        _RecCtx(reply="后台结论"),
    )
    plugin._get_page = _never_page()
    event = _Event()
    accepted = _run(plugin.browse_web(event, "后台跑一下"))
    assert accepted.startswith("【已受理】")
    # 等后台任务结束
    for job in list(plugin._background.running()):
        _run(plugin._background.task_of(job.task_id))
    assert sent, "后台终态必须单独投递一条"
    text = json.dumps(sent, ensure_ascii=False, default=str)
    assert "【后台任务完成】" in text
    assert C_UNTRUSTED_PREFIX not in text


def test_c_mode_body_has_prefix():
    """C 模式（消费者是唤醒轮 = 模型）⇒ **要**前缀。"""
    plugin = _plugin({}, _Ctx(reply="C模式结论"))
    plugin._get_page = _never_page()
    out = _run(plugin._run_c_background_body(_Event(), "任务", "text"))
    assert out.startswith(C_UNTRUSTED_PREFIX)


def test_untrusted_prefix_alias_equals_legacy_name():
    from core.background import UNTRUSTED_PREFIX

    assert UNTRUSTED_PREFIX == C_UNTRUSTED_PREFIX
    assert agent_core.UNTRUSTED_PREFIX == C_UNTRUSTED_PREFIX


# ---------------------------------------------------------------
# 入口收口后的可观测性
# ---------------------------------------------------------------


def test_entry_logs_perception_mode_and_provider(capsys):
    """入口仍记录 ``browse_web 进入`` 与感知模式（生产判据可 grep）。"""
    assert "browse_web 进入" in SRC
    assert "感知模式=" in SRC
    assert "内层模型=" in SRC


def test_foreground_concurrency_is_observed_not_blocked(monkeypatch):
    """同会话前台并发只告警、不拦截（规格 §8.5 / §11-N7）。"""
    warnings: list[str] = []
    real = main_module.logger

    class _Rec:
        def debug(self, *a, **k):
            return None

        def info(self, *a, **k):
            return None

        def error(self, *a, **k):
            return None

        def warning(self, *a, **k):
            warnings.append(" ".join(str(x) for x in a))

    monkeypatch.setattr(main_module, "logger", _Rec())
    try:
        gate = asyncio.Event()

        class _BlockingCtx(_Ctx):
            async def tool_loop_agent(self, **kwargs):
                await gate.wait()
                return SimpleNamespace(completion_text="done")

        plugin = _plugin({}, _BlockingCtx())
        plugin._get_page = _never_page()

        async def flow():
            first = asyncio.create_task(plugin._browse_web_run(_Event(), "a", "text"))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(plugin._browse_web_run(_Event(), "b", "text"))
            await asyncio.sleep(0.01)
            gate.set()
            return await asyncio.gather(first, second)

        results = _run(flow())
    finally:
        monkeypatch.setattr(main_module, "logger", real)
    assert len(results) == 2
    assert all("done" in r for r in results), "并发跑完两条（不拦截、不失败）"
    assert any("同会话并发 browse_web" in w for w in warnings)


def _stub_page():
    """返回一个最小可用页面（只需 extract_links 能跑）。"""

    class _Page:
        url = "https://example.com/p"
        viewport_size = {"width": 1280, "height": 800}

        async def evaluate(self, script, *args):
            return []

    async def _get_page(event):
        return _Page(), ""

    return _get_page


def _never_page():
    """内层工具不该被调用到页面（这些用例只验证入口语义）。"""

    async def _get_page(event):
        raise AssertionError("本用例不该用到页面")

    return _get_page


def test_configured_inner_provider_is_actually_used():
    """``agent.provider_id`` **生效**：内层 ``tool_loop_agent`` 收到的就是它。

    （决策表只管"该用谁"，这条管"真的把谁传下去了"——两者都成立才算生效。）
    """
    seen: dict = {}

    ctx = _Ctx()

    async def _capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    ctx.tool_loop_agent = _capture
    plugin = _plugin({"agent": {"provider_id": "prov-inner"}}, ctx)
    plugin.context.provider_manager = SimpleNamespace(
        provider_insts=[
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="prov-inner"),
                provider_config={"modalities": ["text", "image", "tool_use"]},
            )
        ]
    )
    _run(plugin.browse_web(_Event(), "任务"))
    assert seen["chat_provider_id"] == "prov-inner"
    assert seen["max_steps"] == plugin.agent_max_steps
    assert seen["tool_call_timeout"] == plugin.agent_tool_timeout
    assert seen["system_prompt"].startswith(agent_core.INNER_RED_LINES), "L0 必须在最前"
    assert seen["tools"] is plugin._browser_toolset


def test_inherited_inner_provider_uses_current_session_model():
    """留空 = 继承当前会话模型（= v1.6.0 行为）。"""
    seen: dict = {}

    ctx = _Ctx()

    async def _capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    ctx.tool_loop_agent = _capture
    plugin = _plugin({}, ctx)
    _run(plugin.browse_web(_Event(), "任务"))
    assert seen["chat_provider_id"] == "prov-inherit"


@pytest.mark.parametrize("tool", ["browse_web", "browse_local_page", "browser_web_search"])
def test_three_tools_are_all_registered_methods(tool):
    assert hasattr(BrowserLLMPlugin, tool)
    assert asyncio.iscoroutinefunction(getattr(BrowserLLMPlugin, tool))
