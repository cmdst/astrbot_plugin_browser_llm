"""`core/agent.py` 的行为用例（R7）：8 格决策 / 人格三档 / 六段顺序 / 台账 / 归类 / 抽图 / 作用域。

R7 的事实基础：745 行重写后这 7 个公开面在**任何**测试里出现次数都是 0，于是
"字段名写错就藏在盲区里"（R1 就是这么漏出去的）。这里全部直调纯函数，不需要
astrbot 也不需要 playwright。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from astrbot_plugin_browser_llm.core import agent as ag  # noqa: E402


# `provider_flags()` 读的是 **dict**（核心 provider_config 的形态）：
# 缺 `modalities` / 空列表 = 未声明 = fail-open 按支持处理。
def _multimodal_cfg():
    return {"modalities": ["text", "image", "tool_use"]}


def _text_only_cfg():
    return {"modalities": ["text", "tool_use"]}


# ---------------------------------------------------------------- 8 格决策表


def test_decide_provider_inherit_without_resolved_model_is_error():
    d = ag.decide_provider(configured="", resolved="")
    assert not d.ok and d.error == ag.ERR_NO_PROVIDER
    assert d.log_level == "debug" and d.inherited is True


def test_decide_provider_inherit_uses_session_model():
    d = ag.decide_provider(configured="", resolved="session-model", provider_config=_multimodal_cfg())
    assert d.ok and d.inherited is True and d.native_image is True
    assert d.provider_id == "session-model"


def test_decide_provider_explicit_not_loaded_is_rejected_without_fallback():
    d = ag.decide_provider(
        configured="explicit-model", resolved="session-model", loaded=False
    )
    assert not d.ok, "显式指定却没加载 ⇒ 必须拒绝（绝不静默回落）"
    assert d.inherited is False and d.log_level == "error"
    assert "explicit-model" in d.error


def test_decide_provider_explicit_loaded_multimodal_is_native():
    d = ag.decide_provider(
        configured="m", resolved="session", loaded=True, provider_config=_multimodal_cfg()
    )
    assert d.ok and d.native_image and d.provider_id == "m"


def test_decide_provider_text_only_is_usable_but_not_native():
    d = ag.decide_provider(
        configured="t", resolved="s", provider_config=_text_only_cfg()
    )
    assert d.ok and d.multimodal is False and d.native_image is False


def test_decide_provider_declared_without_tool_use_is_rejected():
    """核心会静默清空工具集 ⇒ 必须在插件侧拒绝（规格 §3.1 红线）。"""
    cfg = {"modalities": ["text"]}  # 声明了 modalities 但缺 tool_use
    d = ag.decide_provider(configured="no-tools", resolved="s", provider_config=cfg)
    assert not d.ok and "tool" in d.error.lower()


def test_decide_provider_unknown_config_assumes_capable():
    d = ag.decide_provider(configured="x", resolved="s", provider_config=None)
    assert d.ok, "拿不到 provider 配置时不能凭猜测拒绝（fail-open 给执行体）"


def test_decide_provider_selection_log_is_one_line():
    d = ag.decide_provider(configured="m", resolved="s", provider_config=_multimodal_cfg())
    assert "\n" not in d.selection_log
    assert "m" in d.selection_log


# ---------------------------------------------------------------- 人格三档


def test_persona_layer_default_is_empty():
    assert ag.persona_layer("default", "你是猫娘") == ""


def test_persona_layer_current_and_custom_pass_text_through():
    assert "猫娘" in ag.persona_layer("current", "你是猫娘")
    assert "猫娘" in ag.persona_layer("custom", "你是猫娘")


def test_persona_layer_blank_text_equals_default():
    assert ag.persona_layer("current", "   ") == ""
    assert ag.persona_layer("custom", None) == ""


# ---------------------------------------------------------------- 六段顺序 + 三句硬话


HARD_RULES = ag.INNER_RULES


def test_compose_inner_prompt_keeps_six_segments_in_order():
    text = ag.compose_inner_prompt(
        base="BASE_MARKER", persona_mode="custom", persona_text="PERSONA_MARKER"
    )
    # 六段顺序：L0 红线 → **L1 人格** → L2+L3（base 指令块）→ L4 硬话 → L5 契约
    assert text.index("PERSONA_MARKER") < text.index("BASE_MARKER"), "L1 人格在 L2/L3 之前"
    for sentence in HARD_RULES:
        assert sentence in text, "§5.5 三句硬话必须逐字出现在内层提示词里"


def test_compose_inner_prompt_default_persona_has_no_marker():
    text = ag.compose_inner_prompt(base="B", persona_mode="default", persona_text="P")
    assert "P" not in text.replace("B", "")


def test_inner_rules_are_exactly_three_sentences():
    assert isinstance(HARD_RULES, tuple) or isinstance(HARD_RULES, list)
    assert len(HARD_RULES) == 3, HARD_RULES


# ---------------------------------------------------------------- 台账


def test_ledger_records_steps_and_status():
    ledger = ag.Ledger()
    ledger.add("browse_act", {"action": "click"}, "【已点击】提交", status="已变化")
    ledger.add("browse_observe", {}, "【错误】boom")
    assert ledger.steps == 2
    assert ledger.entries[0].ok is True and ledger.entries[0].status == "已变化"
    assert ledger.entries[1].ok is False


def test_ledger_redacts_long_args_and_keeps_brief_short():
    ledger = ag.Ledger()
    ledger.add("browse_act", {"value": "x" * 400}, "ok")
    assert len(ledger.entries[0].args) < 400
    assert len(ledger.entries[0].brief) <= ag.LEDGER_BRIEF_MAX


def test_render_ledger_zero_lines_is_empty_and_truncates_to_recent():
    ledger = ag.Ledger()
    for index in range(12):
        ledger.add("t", {}, f"ok{index}")
    assert ag.render_ledger(ledger, 0) == ""
    block = ag.render_ledger(ledger, 3)
    assert "12. t()" in block, "必须保留最近几步（收尾失真发生在最后几步）"
    assert "\n1. t()" not in block, "不该保留最早的几步"
    assert ag.render_ledger(None, 5) == ""


def test_render_ledger_has_no_body_text_or_urls():
    ledger = ag.Ledger()
    ledger.add("browse_observe", {"url": "https://x.example/?k=v"}, "正文" * 80)
    block = ag.render_ledger(ledger, 10)
    assert "k=v" not in block, "台账不许带 URL query（?后面一律抹掉）"
    assert "***" in block, "抹掉的 query 要留痕"
    # 「不含正文」= 只留一行 brief（LEDGER_BRIEF_MAX），绝不整段正文
    body_line = [line for line in block.splitlines() if line.startswith("1.")][0]
    assert len(body_line) <= ag.LEDGER_BRIEF_MAX + 80


# ---------------------------------------------------------------- 结果归类四态


def test_classify_result_four_states():
    assert ag.classify_result("【错误】x") == "error"
    assert ag.classify_result("【拒绝】x") == "reject"
    assert ag.classify_result("【注意】x") == "notice"
    assert ag.classify_result("【观测】x") == "ok"


def test_classify_result_blank_is_ok_but_errors_are_not():
    """空正文按 ``ok`` 归类（fail-open），而显式失败前缀永远不算成功。"""
    assert ag.classify_result("") == "ok"
    assert ag.classify_result(None) == "ok"
    for text in ("【错误】x", "  【错误】缩进也算"):
        assert ag.classify_result(text) != "ok"


# ---------------------------------------------------------------- 抽图 / 作用域


def test_truncate_call_result_keeps_text_and_drops_extra_images():
    if ag._mcp_types is None:  # mcp 不在时该函数 fail-soft，跳过
        import pytest

        pytest.skip("mcp.types 不可用")
    types = ag._mcp_types
    result = ag.truncate_call_result(
        types.CallToolResult(
            content=[
                types.TextContent(type="text", text="hello"),
                types.ImageContent(type="image", data="a", mimeType="image/png"),
                types.ImageContent(type="image", data="b", mimeType="image/png"),
            ]
        ),
        1000,
    )
    kinds = [getattr(item, "type", "") for item in result.content]
    assert kinds.count("image") <= 1, "最多保留 1 张图"
    texts = " ".join(str(getattr(item, "text", "")) for item in result.content)
    assert "hello" in texts, "文本必须保留"


def test_run_scope_nested_restores_previous_context():
    outer = ag.AgentRun()
    inner = ag.AgentRun()
    assert ag.current_run() is None
    with ag.run_scope(outer):
        assert ag.current_run() is outer
        with ag.run_scope(inner):
            assert ag.current_run() is inner
        assert ag.current_run() is outer, "退出内层必须复原外层"
    assert ag.current_run() is None
