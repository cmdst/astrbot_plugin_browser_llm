"""识图 provider 动态下拉与降级逻辑测试（Bug #1/#2 修复验证）。

覆盖：
- _collect_provider_ids：provider_insts 提取、保序去重、inst_map 兜底、空兜底；
- _sync_vision_provider_options：内存 schema options 动态更新、default 修正、
  "" 空选项保留、幂等；
- _resolve_vision_provider_id：配置实时读取（Dashboard 保存即生效）、存在性
  校验、失效自动回退当前会话 Provider、空配置不识别、无 ProviderManager 桩
  环境信任配置值；
- _describe_screenshot：失效 provider 自动回退并调用 llm_generate、空配置
  不调用、纯文本模型拒识文案返回明确提示；
- _is_vision_rejection：中英文拒识特征识别（大小写不敏感），正常视觉描述
  与空文本不误判；
- _conf_schema.json 静态契约：vision_provider_id 不再硬编码具体 provider。

依赖 conftest.py 的 astrbot 桩与根目录 sys.path。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main import (
    _VISION_REJECTION_HINT,
    _is_vision_rejection,
    BrowserLLMPlugin,
)

_ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


class _Provider:
    """最小 Provider 桩：meta().id 返回构造时指定的 id。"""

    def __init__(self, pid):
        self._pid = pid

    def meta(self):
        return SimpleNamespace(id=self._pid)


class _ProviderManager:
    """最小 ProviderManager 桩：可配置 inst_map 与 provider_insts。"""

    def __init__(self, inst_map=None, provider_insts=None):
        self.inst_map = dict(inst_map or {})
        self.provider_insts = list(provider_insts or [])


class _Event:
    unified_msg_origin = "grp:1:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


class _Config(dict):
    """可挂 schema 属性的 dict 子类（模拟 AstrBotConfig 的 schema 属性）。"""


def _make_plugin(
    pm=None,
    vision="",
    schema_options=None,
    schema_default=None,
):
    """构造带可配置 provider_manager 与 schema 的最小插件实例。"""
    cfg = _Config({
        "banned_words": ["赌博"],
        "block_internal_ip": True,
        "max_chars": 4000,
        "timeout": 30,
        "enable_screenshot": True,
        "vision_provider_id": vision,
        "vision_prompt": "自定义提示词",
        "silent_mode": True,
    })
    if schema_options is not None:
        cfg.schema = {
            "vision_provider_id": {
                "options": list(schema_options),
                "default": schema_default if schema_default is not None else "",
            }
        }
    context = SimpleNamespace(provider_manager=pm)
    plugin = BrowserLLMPlugin(context=context, config=cfg)
    return plugin


# ------------------------------------------------------------
# _collect_provider_ids
# ------------------------------------------------------------

def test_collect_provider_ids_from_provider_insts():
    pm = _ProviderManager(
        provider_insts=[_Provider("opencode-go/mimo-v2.5"), _Provider("other/vision")],
        inst_map={"opencode-go/mimo-v2.5": object(), "other/vision": object()},
    )
    plugin = _make_plugin(pm=pm)
    assert plugin._collect_provider_ids() == ["opencode-go/mimo-v2.5", "other/vision"]


def test_collect_provider_ids_dedup_keep_order():
    pm = _ProviderManager(provider_insts=[_Provider("a/x"), _Provider("a/x"), _Provider("b/y")])
    plugin = _make_plugin(pm=pm)
    assert plugin._collect_provider_ids() == ["a/x", "b/y"]


def test_collect_provider_ids_fallback_inst_map():
    """provider_insts 为空时退回 inst_map 键（面板展示兜底）。"""
    pm = _ProviderManager(provider_insts=[], inst_map={"p1": object(), "p2": object()})
    plugin = _make_plugin(pm=pm)
    assert plugin._collect_provider_ids() == ["p1", "p2"]


def test_collect_provider_ids_empty_without_manager():
    plugin = _make_plugin(pm=None)
    assert plugin._collect_provider_ids() == []


def test_collect_provider_ids_skips_broken_meta():
    class _Broken:
        def meta(self):
            raise RuntimeError("boom")

    pm = _ProviderManager(provider_insts=[_Broken(), _Provider("ok/pid")])
    plugin = _make_plugin(pm=pm)
    assert plugin._collect_provider_ids() == ["ok/pid"]


# ------------------------------------------------------------
# _sync_vision_provider_options
# ------------------------------------------------------------

def test_sync_options_updates_schema():
    plugin = _make_plugin(
        pm=_ProviderManager(provider_insts=[_Provider("opencode-go/mimo-v2.5"), _Provider("other/v")]),
        vision="opencode-go/mimo-v2.5",
        schema_options=["opencode-go/mimo-v2.5", ""],
        schema_default="opencode-go/mimo-v2.5",
    )
    options = plugin._sync_vision_provider_options()
    assert options == ["", "opencode-go/mimo-v2.5", "other/v"], options
    # 内存 schema 被就地更新（Dashboard 每次请求读取同一对象）。
    assert plugin.config.schema["vision_provider_id"]["options"] == options


def test_sync_options_keeps_empty_option():
    """无论 provider 列表如何，""（留空=不识别）始终保留且位于首位。"""
    plugin = _make_plugin(
        pm=_ProviderManager(provider_insts=[_Provider("only/one")]),
        schema_options=[""],
        schema_default="",
    )
    options = plugin._sync_vision_provider_options()
    assert options[0] == "" and "only/one" in options


def test_sync_options_fixes_stale_default():
    """原默认 provider 已不在候选中 → default 修正为空串。"""
    plugin = _make_plugin(
        pm=_ProviderManager(provider_insts=[_Provider("other/v")]),
        schema_options=["opencode-go/mimo-v2.5", ""],
        schema_default="opencode-go/mimo-v2.5",
    )
    plugin._sync_vision_provider_options()
    assert plugin.config.schema["vision_provider_id"]["default"] == ""


def test_sync_options_keeps_valid_default():
    plugin = _make_plugin(
        pm=_ProviderManager(provider_insts=[_Provider("other/v")]),
        schema_options=["other/v", ""],
        schema_default="other/v",
    )
    plugin._sync_vision_provider_options()
    assert plugin.config.schema["vision_provider_id"]["default"] == "other/v"


def test_sync_options_idempotent():
    plugin = _make_plugin(
        pm=_ProviderManager(provider_insts=[_Provider("a/x"), _Provider("b/y")]),
        schema_options=[""],
        schema_default="",
    )
    first = plugin._sync_vision_provider_options()
    second = plugin._sync_vision_provider_options()
    assert first == second == ["", "a/x", "b/y"]
    assert plugin.config.schema["vision_provider_id"]["options"] == first


def test_sync_options_no_schema_noop():
    """config 无 schema 属性（普通 dict）时不抛异常。"""
    plugin = _make_plugin(pm=_ProviderManager(provider_insts=[_Provider("a/x")]))
    assert plugin._sync_vision_provider_options() == ["", "a/x"]


# ------------------------------------------------------------
# _resolve_vision_provider_id
# ------------------------------------------------------------

def test_resolve_uses_configured_provider():
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/pid")],
        inst_map={"vision/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="vision/pid")
    assert _run(plugin._resolve_vision_provider_id(_Event())) == "vision/pid"


def test_resolve_reads_live_config_value():
    """Dashboard 保存后共享 config dict 更新 → 无需重启立即生效（Bug #1 回归）。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("new/pid")],
        inst_map={"new/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="old/pid")
    plugin.config["vision_provider_id"] = "new/pid"  # 模拟面板保存后的 dict 更新
    assert _run(plugin._resolve_vision_provider_id(_Event())) == "new/pid"


def test_resolve_falls_back_to_chat_provider():
    """配置的识图 provider 失效（被删除/改名）→ 回退当前会话聊天 Provider。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("chat/pid")],
        inst_map={"chat/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="stale/pid")
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="chat/pid")
    assert _run(plugin._resolve_vision_provider_id(_Event())) == "chat/pid"
    plugin.context.get_current_chat_provider_id.assert_awaited_once()


def test_resolve_fallback_failure_returns_empty():
    pm = _ProviderManager(provider_insts=[], inst_map={})
    plugin = _make_plugin(pm=pm, vision="stale/pid")
    plugin.context.get_current_chat_provider_id = AsyncMock(
        side_effect=RuntimeError("no provider")
    )
    assert _run(plugin._resolve_vision_provider_id(_Event())) == ""


def test_resolve_empty_config_disables_vision():
    """留空 = 不做识图；不应触发回退逻辑。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("chat/pid")],
        inst_map={"chat/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="")
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="chat/pid")
    assert _run(plugin._resolve_vision_provider_id(_Event())) == ""
    plugin.context.get_current_chat_provider_id.assert_not_awaited()


def test_resolve_trusts_config_without_provider_manager():
    """无 ProviderManager（单测桩）时不校验存在性，直接信任配置值。"""
    plugin = _make_plugin(pm=None, vision="fake/vision")
    assert _run(plugin._resolve_vision_provider_id(_Event())) == "fake/vision"


# ------------------------------------------------------------
# _describe_screenshot：失效回退 / 空配置
# ------------------------------------------------------------

def test_describe_screenshot_falls_back_and_calls_llm():
    pm = _ProviderManager(
        provider_insts=[_Provider("chat/pid")],
        inst_map={"chat/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="stale/pid")
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="chat/pid")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="识图描述：页面顶部为导航栏。")
    )
    out = _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    assert out == "识图描述：页面顶部为导航栏。"
    # 桩环境 contexts 通道 ImportError → 走 image_urls 通道，且用回退后的 provider。
    kwargs = plugin.context.llm_generate.call_args.kwargs
    assert kwargs["chat_provider_id"] == "chat/pid"
    assert kwargs["image_urls"] == ["/tmp/x.png"]


def test_describe_screenshot_disabled_no_call():
    pm = _ProviderManager(
        provider_insts=[_Provider("chat/pid")],
        inst_map={"chat/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="x")
    )
    out = _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    assert out == ""
    plugin.context.llm_generate.assert_not_awaited()


def test_describe_screenshot_uses_live_prompt():
    """vision_prompt 实时读取：面板改提示词后无需重启生效。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/pid")],
        inst_map={"vision/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="vision/pid")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="ok")
    )
    plugin.config["vision_prompt"] = "新提示词：逐字读出页面按钮"
    _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    kwargs = plugin.context.llm_generate.call_args.kwargs
    assert kwargs["prompt"] == "新提示词：逐字读出页面按钮"


# ------------------------------------------------------------
# _describe_screenshot / _is_vision_rejection：拒识检测（纯文本模型）
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "[Unsupported Image]",
        "[unsupported image]",
        "[UNSUPPORTED IMAGE]",
        "I cannot view images.",
        "I can't view the picture you sent.",
        "I am unable to process the image.",
        "Sorry, I'm not able to see the image.",
        "This model does not support image inputs.",
        "The current model does not accept pictures.",
        "No vision support is available for this request.",
        "I'm a text-only model, so I can't view images.",
        "As a text-only AI, I cannot process pictures.",
    ],
)
def test_is_vision_rejection_detects_english(text):
    assert _is_vision_rejection(text), f"应识别为拒识: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "图片无法识别，请换一种方式提问。",
        "图无法处理。",
        "抱歉，无法处理图片内容。",
        "我无法查看图片，但可以帮你分析文字。",
        "当前模型不支持图片输入。",
        "不支持图像，仅支持文本。",
        "不支持多模态输入。",
        "我不是多模态模型。",
        "我是纯文本模型，无法查看截图。",
        "纯文本 LLM 无法处理图像。",
        "没有视觉能力，无法识别截图。",
        "没有识图功能。",
    ],
)
def test_is_vision_rejection_detects_chinese(text):
    assert _is_vision_rejection(text), f"应识别为拒识: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "识图描述：页面顶部为导航栏，包含搜索框与登录按钮。",
        "截图显示一个深色主题的 dashboard，左侧为菜单栏，右侧为图表区域。",
        "页面中有一个无法点击的按钮，样式为灰色。",
        "图片加载完成后，页面整体布局正常。",
    ],
)
def test_is_vision_rejection_accepts_normal_text(text):
    """正常视觉描述与无关文本（含“无法”等词但不构成拒识）不误判。"""
    assert not _is_vision_rejection(text), f"不应识别为拒识: {text!r}"


def test_describe_screenshot_rejection_returns_hint():
    """纯文本模型拒识 → 返回明确提示文案，不把拒识文本当视觉描述。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/pid")],
        inst_map={"vision/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="vision/pid")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="[Unsupported Image]")
    )
    out = _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    assert out == _VISION_REJECTION_HINT


def test_describe_screenshot_rejection_chinese_hint():
    """中文拒识同样返回提示文案（大小写/语种无关）。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/pid")],
        inst_map={"vision/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="vision/pid")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text="我无法查看图片，请更换模型。")
    )
    out = _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    assert out == _VISION_REJECTION_HINT


def test_describe_screenshot_normal_vision_text_passthrough():
    """正常视觉描述原样返回，不受拒识检测影响。"""
    pm = _ProviderManager(
        provider_insts=[_Provider("vision/pid")],
        inst_map={"vision/pid": object()},
    )
    plugin = _make_plugin(pm=pm, vision="vision/pid")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(
            completion_text="截图显示：顶部导航栏，中部为商品卡片列表。"
        )
    )
    out = _run(plugin._describe_screenshot("/tmp/x.png", _Event()))
    assert out == "截图显示：顶部导航栏，中部为商品卡片列表。"


# ------------------------------------------------------------
# _conf_schema.json 静态契约（Bug #2）
# ------------------------------------------------------------

def test_schema_no_hardcoded_provider():
    """schema 不应再硬编码具体 provider（options 仅保留 ""，default 为空）。"""
    schema = json.loads(
        (_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    item = schema["vision_provider_id"]
    assert item["type"] == "string"
    assert item["options"] == [""], item["options"]
    assert item["default"] == "", item["default"]
    assert "opencode-go" not in json.dumps(item, ensure_ascii=False)
    assert "自动同步" in item["hint"], "hint 应说明下拉选项自动同步"
