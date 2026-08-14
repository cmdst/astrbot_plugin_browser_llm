"""感知模式精细化测试（v1.3.0 P0：工具参数级 + 会话规则级）。

覆盖：
- A. 工具参数级感知模式：browse_web input 前缀解析（大小写不敏感、
  空格/换行分隔、非法值忽略并告警、未带前缀回落）、本次任务覆盖全局；
- B. 会话规则级默认：perception_rules 按 UMO 子串匹配（不区分大小写）、
  第一条命中、优先级 显式参数 > 规则 > 全局、非法规则跳过并告警；
- 端到端：browse_web 传 perception=text 时传给 tool_loop_agent 的
  system_prompt 为纯文字指令（含"绝对不要使用 browse_screenshot"），
  且提示词为剔除前缀后的任务描述；规则命中与显式覆盖同样可观测。

依赖 conftest 的 astrbot 桩。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main as main_module
from main import BrowserLLMPlugin

# ------------------------------------------------------------
# 最小插件构造（配置可覆盖，含 v1.3.0 新配置项默认值）
# ------------------------------------------------------------


def _make_plugin(**overrides):
    cfg = {
        "banned_words": ["赌博"],
        "block_internal_ip": True,
        "enable_screenshot": True,
        "silent_mode": True,
        "page_perception": "text_image",
        "perception_rules": [],
        "vision_cache_ttl": 0,
    }
    cfg.update(overrides)
    plugin = BrowserLLMPlugin(context=SimpleNamespace(), config=cfg)
    return plugin


def _run(coro):
    return asyncio.run(coro)


class _Event:
    def __init__(self, umo="grp:1:GroupMessage:tester-123"):
        self.unified_msg_origin = umo

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


def _capture_logger(monkeypatch, level="warning"):
    """把 main.logger 指定级别替换为记录函数，返回记录列表。"""
    records = []
    monkeypatch.setattr(main_module.logger, level, records.append)
    return records


# ------------------------------------------------------------
# A. 工具参数级：前缀解析
# ------------------------------------------------------------

def test_parse_prefix_valid_text():
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix(
        "perception=text 打开 https://example.com 读取内容"
    )
    assert mode == "text"
    assert rest == "打开 https://example.com 读取内容"


def test_parse_prefix_case_insensitive():
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix("PERCEPTION=IMAGE\n打开页面")
    assert mode == "image"
    assert rest == "打开页面"


def test_parse_prefix_newline_and_space_separator():
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix("perception=text\n打开页面")
    assert mode == "text" and rest == "打开页面"
    mode, rest = plugin._parse_perception_prefix("perception=text_image 打开页面")
    assert mode == "text_image" and rest == "打开页面"


def test_parse_prefix_equals_spaces_tolerated():
    """perception = text 这类宽松写法也可解析（值前后空白容忍）。"""
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix("perception = text  打开页面")
    assert mode == "text"
    assert rest == "打开页面"


def test_parse_prefix_no_prefix_passthrough():
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix("直接打开 https://example.com")
    assert mode is None
    assert rest == "直接打开 https://example.com"


def test_parse_prefix_prefix_only_no_task():
    """仅前缀无任务：模式仍生效，任务为空由 browse_web 兜底默认提示词。"""
    plugin = _make_plugin()
    mode, rest = plugin._parse_perception_prefix("perception=text")
    assert mode == "text"
    assert rest == ""


def test_parse_prefix_invalid_value_ignored_with_warning(monkeypatch):
    """非法值（perception=foo）：告警 + 剔除前缀 + 回落（None）。"""
    plugin = _make_plugin()
    warns = _capture_logger(monkeypatch, "warning")
    mode, rest = plugin._parse_perception_prefix("perception=foo 打开页面")
    assert mode is None, "非法值必须回落默认链路"
    assert rest == "打开页面", "非法前缀应被剔除，不污染任务描述"
    assert warns, "非法前缀必须记录告警日志"
    assert "非法" in warns[0]


def test_parse_prefix_invalid_textimage_value_warned(monkeypatch):
    """textimage（缺少下划线）这类近似值同样告警回落。"""
    plugin = _make_plugin()
    warns = _capture_logger(monkeypatch, "warning")
    mode, rest = plugin._parse_perception_prefix("perception=textimage 打开页面")
    assert mode is None
    assert rest == "打开页面"
    assert warns


# ------------------------------------------------------------
# B. 会话规则级：perception_rules 匹配与优先级
# ------------------------------------------------------------

def test_rules_match_umo_substring_case_insensitive():
    """match 与 UMO 做子串匹配且不区分大小写。"""
    plugin = _make_plugin(
        page_perception="image",
        perception_rules=[{"match": "Tester", "perception": "text"}],
    )
    event = _Event(umo="grp:1:GroupMessage:tester-123")
    assert plugin._resolve_perception_mode(event) == "text"


def test_rules_first_match_wins():
    plugin = _make_plugin(
        perception_rules=[
            {"match": "tester", "perception": "text"},
            {"match": "test", "perception": "image"},  # 也命中但排在后面
        ]
    )
    event = _Event(umo="grp:1:GroupMessage:tester-123")
    assert plugin._resolve_perception_mode(event) == "text"


def test_rules_priority_explicit_over_rule():
    """显式参数 > 规则命中。"""
    plugin = _make_plugin(
        page_perception="text_image",
        perception_rules=[{"match": "tester", "perception": "text"}],
    )
    event = _Event(umo="grp:1:GroupMessage:tester-123")
    assert plugin._resolve_perception_mode(event, explicit="image") == "image"


def test_rules_priority_rule_over_global():
    """规则命中 > 全局 page_perception。"""
    plugin = _make_plugin(
        page_perception="text",
        perception_rules=[{"match": "qa", "perception": "image"}],
    )
    event = _Event(umo="grp:1:GroupMessage:qa-9")
    assert plugin._resolve_perception_mode(event) == "image"
    # 未命中规则时回落全局
    event2 = _Event(umo="grp:1:GroupMessage:dev-1")
    assert plugin._resolve_perception_mode(event2) == "text"


def test_rules_no_match_falls_back_to_global():
    plugin = _make_plugin(
        page_perception="image",
        perception_rules=[{"match": "tester", "perception": "text"}],
    )
    event = _Event(umo="grp:1:GroupMessage:other-1")
    assert plugin._resolve_perception_mode(event) == "image"


def test_rules_empty_default_uses_global():
    plugin = _make_plugin(page_perception="text", perception_rules=[])
    event = _Event()
    assert plugin._resolve_perception_mode(event) == "text"


def test_rules_invalid_perception_skipped_with_warning(monkeypatch):
    """规则 perception 非法：跳过该规则并告警，回落全局。"""
    plugin = _make_plugin(
        page_perception="text_image",
        perception_rules=[
            {"match": "tester", "perception": "ultra"},
            {"match": "tester", "perception": "text"},
        ],
    )
    warns = _capture_logger(monkeypatch, "warning")
    event = _Event(umo="grp:1:GroupMessage:tester-1")
    assert plugin._resolve_perception_mode(event) == "text", "应跳过非法规则命中下一条"
    assert warns


def test_rules_invalid_entry_skipped_with_warning(monkeypatch):
    """规则缺 match/perception 或非 dict：跳过并告警。"""
    plugin = _make_plugin(
        page_perception="image",
        perception_rules=[{"note": "缺字段"}, "not-a-dict"],
    )
    warns = _capture_logger(monkeypatch, "warning")
    event = _Event()
    assert plugin._resolve_perception_mode(event) == "image"
    assert len(warns) >= 2, "两条非法规则都应告警"


def test_rules_full_umo_match():
    """match 填完整 UMO 同样命中（子串匹配天然覆盖）。"""
    plugin = _make_plugin(
        perception_rules=[{"match": "grp:1:GroupMessage:tester-123", "perception": "text"}]
    )
    event = _Event(umo="grp:1:GroupMessage:tester-123")
    assert plugin._resolve_perception_mode(event) == "text"


# ------------------------------------------------------------
# A+B 端到端：browse_web 感知模式可观测性
# ------------------------------------------------------------

def _browse_setup(plugin):
    """装配 browse_web 所需的最小 mock，返回 (event, captured)。"""
    event = _Event(umo="grp:1:GroupMessage:tester-123")
    plugin._refresh_config = lambda: None
    plugin._sync_vision_provider_options = lambda: None
    plugin._is_session_allowed = lambda e: (True, "")
    plugin._browser_toolset = SimpleNamespace(tools=[])  # tool_loop_agent 已 mock，不被使用
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="chat/pid")
    captured = {}

    async def fake_tool_loop_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completion_text="浏览结果")

    plugin.context.tool_loop_agent = fake_tool_loop_agent
    return event, captured


def test_browse_web_prefix_text_overrides_global(monkeypatch):
    """perception=text 前缀：system_prompt 为纯文字指令（覆盖全局 text_image）。"""
    plugin = _make_plugin(page_perception="text_image")
    event, captured = _browse_setup(plugin)
    debugs = _capture_logger(monkeypatch, "debug")
    result = _run(plugin.browse_web(
        event,
        input="perception=text 打开 https://example.com 读取内容",
    ))
    assert result == "浏览结果"
    prompt = captured["system_prompt"]
    assert "仅文字" in prompt, "应注入纯文字感知指引"
    assert "绝对不要使用" in prompt and "browse_screenshot" in prompt, \
        "纯文字模式应禁止截图识图"
    assert "截图为主" not in prompt, "不应混入截图为主指引"
    assert captured["prompt"] == "打开 https://example.com 读取内容", \
        "任务描述应剔除感知模式前缀"
    assert any("感知模式=text" in d for d in debugs), "应记录本次感知模式（可观测）"


def test_browse_web_rule_default_and_explicit_override(monkeypatch):
    """规则默认 text + 显式 perception=image 覆盖（验收标准 2）。"""
    plugin = _make_plugin(
        page_perception="image",
        perception_rules=[{"match": "tester", "perception": "text"}],
    )
    # 1) 未带前缀：规则命中 tester → text
    event, captured = _browse_setup(plugin)
    _run(plugin.browse_web(event, input="打开 https://example.com"))
    assert "仅文字" in captured["system_prompt"]
    assert "截图为主" not in captured["system_prompt"]
    # 2) 显式 perception=image：覆盖规则 → image
    event2, captured2 = _browse_setup(plugin)
    _run(plugin.browse_web(event2, input="perception=image 看下页面外观"))
    assert "截图为主" in captured2["system_prompt"]
    assert "仅文字" not in captured2["system_prompt"]


def test_browse_web_no_prefix_falls_back_to_global():
    """未带前缀且无规则命中：全局 page_perception。"""
    plugin = _make_plugin(page_perception="image", perception_rules=[])
    event, captured = _browse_setup(plugin)
    _run(plugin.browse_web(event, input="打开 https://example.com"))
    assert "截图为主" in captured["system_prompt"]


def test_browse_web_invalid_prefix_falls_back_and_warns(monkeypatch):
    """非法前缀：告警 + 任务描述剔除前缀 + 回落全局。"""
    plugin = _make_plugin(page_perception="text", perception_rules=[])
    event, captured = _browse_setup(plugin)
    warns = _capture_logger(monkeypatch, "warning")
    _run(plugin.browse_web(event, input="perception=ultra 打开页面"))
    assert "仅文字" in captured["system_prompt"], "应回落全局 text"
    assert captured["prompt"] == "打开页面", "非法前缀不应残留到任务描述"
    assert warns


def test_browse_web_prefix_only_uses_default_prompt():
    """仅前缀无任务：模式生效且提示词走默认兜底。"""
    plugin = _make_plugin(page_perception="text_image")
    event, captured = _browse_setup(plugin)
    _run(plugin.browse_web(event, input="perception=text"))
    assert "仅文字" in captured["system_prompt"]
    assert captured["prompt"] == "请根据用户请求浏览网页并返回结果"


def test_subagent_instruction_invalid_mode_falls_back():
    """_build_subagent_instruction 对非法/空模式回退全局，不抛异常。"""
    plugin = _make_plugin(page_perception="image")
    assert "截图为主" in plugin._build_subagent_instruction("bogus")
    assert "截图为主" in plugin._build_subagent_instruction(None)
    assert "仅文字" in plugin._build_subagent_instruction("TEXT")
