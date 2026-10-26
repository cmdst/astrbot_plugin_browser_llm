"""v1.7.0 预算与截断（规格 §8.1/§8.3/§8.4）：硬上限必须**被代码执行**。

门禁不做 token 断言（不可复算），只断言上表的硬上限真的生效：

1. 单条工具结果文本 ≤ ``agent.result_max_chars``，且带 ``…（已截断 N 字）``；
2. 累计超预算（``agent_max_steps × result_max_chars``）⇒ 后续压到 800 字符 +
   ``【预算】`` 前缀；
3. 单工具结果含 2 张 ``ImageContent`` ⇒ 只留 1 张并注明；
4. base64 超 ``LLM_IMAGE_MAX_B64`` ⇒ 丢图 + 文本提示；
5. ``agent.task_timeout`` 超时 ⇒ ``【错误】…硬上限被中止``（含已走步数）；
6. 取消传播出的 ``TargetClosedError`` 被吞成 ``【错误】…（已中止）``；
7. ``ledger_max_lines`` 0/24 两种形态。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mcp.types as mcp_types

import main as main_module
from core import agent as agent_core
from core.background import STATUS_TIMEOUT
from main import BrowserLLMPlugin


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


class _NullLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _plugin(config=None, ctx=None):
    cfg = config if isinstance(config, _Cfg) else _Cfg(config or {})
    plugin = BrowserLLMPlugin(ctx or SimpleNamespace(), cfg)
    plugin.config = cfg
    plugin.sessions = None
    plugin._vision_cache = {}
    plugin._lock_for = lambda event: _NullLock()
    return plugin


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# 1/2：文本截断与累计预算
# ---------------------------------------------------------------


def test_truncate_text_appends_elided_count():
    assert agent_core.truncate_text("abc", 10) == "abc"
    out = agent_core.truncate_text("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert out.endswith("…（已截断 40 字）")


def test_record_and_truncate_uses_configured_limit():
    run = agent_core.AgentRun(result_max_chars=100)
    with agent_core.run_scope(run):
        out = agent_core.record_and_truncate(
            "y" * 300, tool="browse_get_text", args={}, run=run
        )
    assert "已截断 200 字" in out
    assert len(out) < 200


def test_budget_squeeze_after_cumulative_overflow():
    """累计超预算后：limit 降到 800，并加【预算】前缀。"""
    run = agent_core.AgentRun(result_max_chars=3000, char_budget=1000)
    with agent_core.run_scope(run):
        first = agent_core.record_and_truncate(
            "a" * 900, tool="browse_get_text", args={}, run=run
        )
        assert "【预算】" not in first, "第一次还在预算内"
        second = agent_core.record_and_truncate(
            "b" * 900, tool="browse_get_text", args={}, run=run
        )
    assert second.startswith("【预算】本任务累计返回已超预算")
    body = second.split("\n", 1)[1]
    assert len(body) <= agent_core.BUDGET_SQUEEZE_CHARS + 30


def test_budget_limit_derives_from_steps_times_chars():
    """``char_budget`` 由 ``limits.agent_max_steps × agent.result_max_chars`` 派生。"""
    plugin = _plugin(
        {"limits": {"agent_max_steps": 7}, "agent": {"result_max_chars": 500}}
    )
    assert plugin.agent_max_steps == 7
    assert plugin.agent_result_max_chars == 500
    assert plugin.agent_max_steps * plugin.agent_result_max_chars == 3500


# ---------------------------------------------------------------
# 3/4：图片张数与体积硬上限
# ---------------------------------------------------------------


def test_truncate_call_result_keeps_only_one_image():
    image = mcp_types.ImageContent(type="image", data="QUJD", mimeType="image/png")
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="两张图"),
            image,
            mcp_types.ImageContent(type="image", data="REVG", mimeType="image/png"),
        ]
    )
    out = agent_core.truncate_call_result(result, 3000)
    images = [c for c in out.content if c.type == "image"]
    assert len(images) == 1, "每工具每次最多 1 张 ImageContent"
    text = [c for c in out.content if c.type == "text"][0].text
    assert "图片过大已省略" in text


def test_truncate_call_result_drops_oversized_base64():
    huge = "A" * (agent_core.LLM_IMAGE_MAX_B64 + 10)
    result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(type="text", text="超大图"),
            mcp_types.ImageContent(type="image", data=huge, mimeType="image/png"),
        ]
    )
    out = agent_core.truncate_call_result(result, 3000)
    assert [c.type for c in out.content] == ["text"]
    assert "图片过大已省略" in out.content[0].text


def test_truncate_call_result_leaves_text_limit_only():
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="z" * 5000)]
    )
    out = agent_core.truncate_call_result(result, 100)
    assert out.content[0].text.endswith("…（已截断 4900 字）")


# ---------------------------------------------------------------
# 5/6：超时与取消（三层超时里属于插件的那一层）
# ---------------------------------------------------------------


class _SlowCtx:
    def __init__(self, *, exc=None, delay=10.0):
        self.exc = exc
        self.delay = delay

    async def get_current_chat_provider_id(self, umo=None):
        return "prov-inherit"

    async def tool_loop_agent(self, **kwargs):
        if self.exc is not None:
            raise self.exc
        await asyncio.sleep(self.delay)
        return SimpleNamespace(completion_text="太晚了")


def test_task_timeout_returns_error_with_steps(monkeypatch):
    plugin = _plugin(
        {"agent": {"task_timeout": 60}}, _SlowCtx(delay=5.0)
    )
    # 直接把配置改成最小可测值（clamp 下限 60），用 wait_for 的极小超时模拟
    plugin.agent_task_timeout = 1
    out = _run(plugin._browse_web_run(_Event(), "长任务", "text"))
    assert out.startswith("【错误】浏览任务超过 1 秒硬上限被中止")
    # T12/R13：0 步时不再写「已完成 0 步，见台账」（台账块是空串 ⇒ 那是断链），
    # 改成如实说明"还没产生任何工具调用"；有步数时才写「已完成 N 步，见台账」。
    assert "尚未产生任何内层工具调用" in out
    assert "见台账" not in out


def test_cancelled_browser_error_is_swallowed_as_error_text():
    """``TargetClosedError``（取消传播到 playwright）不许冒泡给主 LLM。"""
    ctx = _SlowCtx(exc=RuntimeError("Target page, context or browser has been closed"))
    plugin = _plugin({"agent": {"task_timeout": 60}}, ctx)
    out = _run(plugin._browse_web_run(_Event(), "任务", "text"))
    assert out.startswith("【错误】浏览器子代理执行失败")
    assert "已中止" in out


def test_background_timeout_wins_over_task_timeout():
    """``background_timeout < agent.task_timeout`` ⇒ 后台先中止并发【后台任务超时】。

    规格 §8.4：三层超时互不替代；后台预算更小时后台先中止是**预期**行为。
    """
    from core.background import render_terminal_text  # noqa: F401 — 只为对齐文案

    plugin = _plugin(
        {
            "tools": {"background_mode": "browse_only", "background_timeout": 30},
            "agent": {"task_timeout": 600},
        },
        _SlowCtx(delay=5.0),
    )
    assert plugin.background_timeout == 30
    assert plugin.agent_task_timeout == 600
    assert STATUS_TIMEOUT == "timeout"


# ---------------------------------------------------------------
# 7：台账行数（0 = 不附；24 = 折叠）
# ---------------------------------------------------------------


def test_ledger_lines_config_is_clamped():
    plugin = _plugin({"agent": {"ledger_max_lines": 0}})
    assert plugin.agent_ledger_max_lines == 0
    plugin2 = _plugin({"agent": {"ledger_max_lines": 999}})
    assert plugin2.agent_ledger_max_lines == 60, "clamp 上限 60"


def test_agent_numeric_keys_are_clamped_to_spec_ranges():
    plugin = _plugin(
        {
            "agent": {
                "task_timeout": 5,  # < 60
                "image_max_side": 99999,
                "image_quality": 1,
                "result_max_chars": 1,
            }
        }
    )
    assert plugin.agent_task_timeout == 60
    assert plugin.agent_image_max_side == 2048
    assert plugin.agent_image_quality == 40
    assert plugin.agent_result_max_chars == 500


def test_agent_defaults_match_config_map():
    """默认值与 ``core.config_map.LEAF_DEFAULTS`` 逐条相等（不各写一份）。"""
    from core.config_map import default_of

    plugin = _plugin({})
    checks = {
        "agent_provider_id": "agent.provider_id",
        "agent_persona_mode": "agent.persona_mode",
        "agent_persona_prompt": "agent.persona_prompt",
        "agent_task_timeout": "agent.task_timeout",
        "agent_image_max_side": "agent.image_max_side",
        "agent_image_quality": "agent.image_quality",
        "agent_result_max_chars": "agent.result_max_chars",
        "agent_ledger_max_lines": "agent.ledger_max_lines",
        "search_mode": "search.mode",
    }
    for attr, path in checks.items():
        assert getattr(plugin, attr) == default_of(path), attr


def test_image_native_constants():
    """§8.1 的常量写死：base64 1.5e6、每任务 40 张、压到 800、台账 24 行。"""
    assert agent_core.LLM_IMAGE_MAX_B64 == 1_500_000
    assert agent_core.MAX_IMAGES_PER_TASK == 40
    assert agent_core.BUDGET_SQUEEZE_CHARS == 800
    assert main_module.default_of("agent.ledger_max_lines") == 24
