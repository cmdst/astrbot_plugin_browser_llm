"""v1.7.0 内层 provider：8 格决策表 + 面板候选（规格 §3）。

三条要点（每条都在本文件里钉死）：

1. **绝不静默降级**：
   - 声明了 ``modalities`` 却不含 ``tool_use`` ⇒ 【错误】拒绝。依据：核心
     ``_func_tool_for_provider``（``tool_loop_agent_runner.py:675-686``）会
     **静默清空工具集**，内层 agent 一步都调不了工具却回一段"我无法浏览"，
     外层看起来是成功 —— 最坏的静默失败形态。
   - 显式配置的 provider 未加载（被删/禁用/改名）⇒ 【错误】拒绝、**不回落**
     当前会话模型。静默换模型会让"以为是 A 在跑、其实是 B"。
2. **空/缺 modalities 按"未声明 = 支持"**（fail-open，与核心
   ``_provider_supports_modality`` 的 ``modalities == []`` 分支同口径）。
3. **面板下拉只列"多模态 + 可函数调用"**的候选：让运营方选到一个必然走兜底
   甚至被拒的模型，等于把运行期才发现的错推给运行期。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main as main_module
from main import BrowserLLMPlugin

from core import agent as agent_core

PINNED = "prov-inherit"


class _Cfg(dict):
    """最小配置对象：既是 dict（ConfigView 读它），又带内存 schema（面板同步改它）。"""

    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Ctx:
    def __init__(self, *, current: str = PINNED, configs=None):
        self._current = current
        self.provider_manager = SimpleNamespace(
            provider_insts=[
                SimpleNamespace(
                    meta=lambda pid=pid: SimpleNamespace(id=pid),
                    provider_config=cfg,
                )
                for pid, cfg in (configs or {}).items()
            ]
        )

    async def get_current_chat_provider_id(self, umo=None):
        return self._current


class _Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"

    def get_group_id(self):
        return "1"

    def get_sender_id(self):
        return "2"

    def get_platform_name(self):
        return "aiocqhttp"


def _plugin(config=None, ctx=None) -> BrowserLLMPlugin:
    """构造插件并把 ``config`` 挂回去。

    注意 ``BrowserLLMPlugin.__init__`` 里是 ``self.config = config or {}``：
    空 dict 是 falsy ⇒ 传 ``_Cfg({})`` 会被替换成另一个普通 dict，后续"面板
    保存"（改同一个对象）就读不到了。因此这里显式再挂一次同一个对象。
    """
    cfg = config if isinstance(config, _Cfg) else _Cfg(config or {})
    plugin = BrowserLLMPlugin(ctx or _Ctx(), cfg)
    plugin.config = cfg
    plugin.sessions = None
    return plugin


def _run(coro):
    return asyncio.run(coro)


def _decide(plugin, event=None):
    return _run(plugin._resolve_agent_provider(event or _Event()))


class _LogRecorder:
    """替换 ``main.logger`` 的记录器（conftest 的 logger 是空桩，caplog 收不到）。

    插件用的是 ``astrbot.api.logger``，测试里 conftest 把它桩成四个空函数，
    所以"日志是否打出来了"必须靠替换对象来量 —— 与 ``test_perception_rules``
    的 ``_capture_logger`` 同款做法，只是这里要一起收 ERROR/WARNING/INFO/DEBUG。
    """

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def debug(self, *a, **k):
        self._add("debug", a)

    def info(self, *a, **k):
        self._add("info", a)

    def warning(self, *a, **k):
        self._add("warning", a)

    def error(self, *a, **k):
        self._add("error", a)

    def text(self, level=None) -> str:
        return "\n".join(t for lv, t in self.records if level is None or lv == level)


def _record(monkeypatch):
    rec = _LogRecorder()
    monkeypatch.setattr(main_module, "logger", rec)
    return rec


# ---------------------------------------------------------------
# 8 格决策表（规格 §3.3）——一格一个参数组
# ---------------------------------------------------------------


def _decision_case(row: str):
    """构造 8 格各自的 (config, configs, current, expected) 四元组。"""
    multi = {"modalities": ["text", "image", "tool_use"]}
    text_only = {"modalities": ["text", "tool_use"]}
    no_tool = {"modalities": ["text", "image"]}
    undeclared = {}
    empty_modalities = {"modalities": []}
    cases = {
        # #1 未配置 + 取不到当前会话模型 ⇒ 【错误】
        "1-no-provider": (
            {},
            {},
            "",
            {"error_prefix": "【错误】无法确定当前对话模型", "inherited": True},
        ),
        # #2 显式多模态 + tool_use ⇒ 影像原生
        "2-explicit-native": (
            {"agent": {"provider_id": "p-multi"}},
            {"p-multi": multi},
            PINNED,
            {"error": "", "multimodal": True, "inherited": False},
        ),
        # #3 显式纯文本（有 tool_use）⇒ caption 兜底 + 【注意】
        "3-explicit-caption": (
            {"agent": {"provider_id": "p-text"}},
            {"p-text": text_only},
            PINNED,
            {
                "error": "",
                "multimodal": False,
                "notice": agent_core.NOTICE_CAPTION_FALLBACK,
            },
        ),
        # #4 显式声明了 modalities 但不含 tool_use ⇒ 【错误】拒绝
        "4-explicit-no-tool-use": (
            {"agent": {"provider_id": "p-notool"}},
            {"p-notool": no_tool},
            PINNED,
            {"error_prefix": "【错误】内层模型 p-notool 不支持工具调用"},
        ),
        # #5 显式配置但未加载 ⇒ 【错误】拒绝、不回落
        "5-explicit-unloaded": (
            {"agent": {"provider_id": "p-gone"}},
            {"p-other": multi},
            PINNED,
            {"error_prefix": "【错误】内层模型 p-gone 未加载"},
        ),
        # #6 显式配置 + 未声明 modalities ⇒ fail-open，照常影像原生
        "6-explicit-undeclared": (
            {"agent": {"provider_id": "p-undeclared"}},
            {"p-undeclared": undeclared},
            PINNED,
            {"error": "", "multimodal": True, "log_level": "warning"},
        ),
        # #6b 空 modalities 列表同"未声明"
        "6b-explicit-empty-modalities": (
            {"agent": {"provider_id": "p-empty"}},
            {"p-empty": empty_modalities},
            PINNED,
            {"error": "", "multimodal": True, "log_level": "warning"},
        ),
        # #7 继承档 + 当前会话非多模态 ⇒ caption 兜底
        "7-inherit-caption": (
            {},
            {PINNED: text_only},
            PINNED,
            {
                "error": "",
                "multimodal": False,
                "inherited": True,
                "notice": agent_core.NOTICE_CAPTION_FALLBACK,
            },
        ),
        # #8 继承档 + 当前会话不含 tool_use ⇒ 【错误】拒绝
        "8-inherit-no-tool-use": (
            {},
            {PINNED: no_tool},
            PINNED,
            {"error_prefix": "【错误】内层模型 " + PINNED + " 不支持工具调用"},
        ),
    }
    return cases[row]


@pytest.mark.parametrize(
    "row",
    [
        "1-no-provider",
        "2-explicit-native",
        "3-explicit-caption",
        "4-explicit-no-tool-use",
        "5-explicit-unloaded",
        "6-explicit-undeclared",
        "6b-explicit-empty-modalities",
        "7-inherit-caption",
        "8-inherit-no-tool-use",
    ],
)
def test_decision_table_rows(row, monkeypatch):
    """8 格（+ 空 modalities 变体）逐格断言：返回体前缀 + 日志级别。"""
    config, configs, current, expected = _decision_case(row)
    plugin = _plugin(config, _Ctx(current=current, configs=configs))
    rec = _record(monkeypatch)
    decision = _decide(plugin)
    if "error_prefix" in expected:
        assert decision.error.startswith(expected["error_prefix"]), decision.error
        # 拒绝必须留一条可 grep 的日志；#1（取不到模型）按规格是 DEBUG，
        # 其余四格是 ERROR（运营方必须能在日志里一眼看到"模型配错了"）。
        assert decision.log_level in ("error", "debug")
        assert decision.log_text, "拒绝必须留一条可 grep 的日志"
    else:
        assert decision.error == ""
    if "multimodal" in expected:
        assert decision.multimodal is expected["multimodal"]
    if "inherited" in expected:
        assert decision.inherited is expected["inherited"]
    if "notice" in expected:
        assert decision.notice == expected["notice"]
    if "log_level" in expected:
        assert decision.log_level == expected["log_level"]
    # 诊断行必须**真的**打在该级别（级别与内容都要可 grep）
    if decision.log_text:
        assert decision.log_text in rec.text(decision.log_level), rec.records
    # 可 grep 的日志：内层模型 + 影像原生 + 人格档（规格 §1.2.5）。
    # 它是**每次任务一行**的 INFO 选定行，与随格变化的诊断行分开。
    if decision.ok:
        assert decision.selection_log.startswith(f"内层模型={decision.provider_id}")
        assert "人格档=" in decision.selection_log
        assert ("影像原生=是" in decision.selection_log) is decision.multimodal


def test_unloaded_provider_never_falls_back_to_current_session(monkeypatch):
    """#5 的**关键否定**：拒绝文本里不许出现"已回退当前会话"这类暗示。"""
    plugin = _plugin(
        {"agent": {"provider_id": "p-gone"}}, _Ctx(configs={PINNED: {}})
    )
    rec = _record(monkeypatch)
    decision = _decide(plugin)
    assert decision.provider_id == "p-gone", "拒绝时不许把 id 换成当前会话模型"
    assert "已回退" not in decision.error, "拒绝文本里不许声称已经回退"
    assert decision.provider_id not in ("", PINNED)
    assert "未在已加载 Provider 中" in rec.text("error")


def test_missing_tool_use_is_rejected_because_core_clears_tools():
    """#4/#8 的依据必须写在实现里（防止后人"顺手"改成放行）。"""
    src = (
        main_module.Path(main_module.__file__).resolve().parent / "core" / "agent.py"
    ).read_text(encoding="utf-8")
    assert "_func_tool_for_provider" in src
    assert "675-686" in src


# ---------------------------------------------------------------
# 面板下拉：只列多模态候选
# ---------------------------------------------------------------


def _schema_cfg():
    return _Cfg(
        {},
        {
            "agent": {
                "type": "object",
                "items": {
                    "provider_id": {
                        "type": "string",
                        "options": [""],
                        "default": "",
                    }
                },
            }
        },
    )


def test_agent_provider_options_only_multimodal():
    """3 个 provider（多模态/纯文本/未声明）⇒ 下拉 = ['', 多模态, 未声明]。"""
    cfg = _schema_cfg()
    ctx = _Ctx(
        configs={
            "p-multi": {"modalities": ["text", "image", "tool_use"]},
            "p-text": {"modalities": ["text", "tool_use"]},
            "p-undeclared": {},
        },
    )
    plugin = _plugin(cfg, ctx)
    options = plugin._sync_agent_provider_options()
    assert options == ["", "p-multi", "p-undeclared"], options
    item = cfg.schema["agent"]["items"]["provider_id"]
    assert item["options"] == options, "必须就地改内存 schema 的 options（面板读它）"


def test_agent_provider_options_drops_tool_use_less_provider():
    """声明了 image 但不含 tool_use：**不**进候选（选了必然被拒）。"""
    plugin = _plugin(
        _schema_cfg(),
        _Ctx(configs={"p-no-tool": {"modalities": ["text", "image"]}}),
    )
    assert plugin._sync_agent_provider_options() == [""]


def test_agent_provider_options_resets_default_when_absent():
    """default 不在候选中时归零（避免"恢复默认"写回一个不存在的 provider）。"""
    cfg = _schema_cfg()
    item = cfg.schema["agent"]["items"]["provider_id"]
    item["default"] = "p-gone"
    plugin = _plugin(cfg, _Ctx(configs={"p-multi": {}}))
    plugin._sync_agent_provider_options()
    assert item["default"] == ""


# ---------------------------------------------------------------
# 唯一解析点（规格 §3 判据 3）
# ---------------------------------------------------------------


def test_only_one_current_chat_provider_resolution_point():
    """``get_current_chat_provider_id`` 在 main.py 只出现 **1** 次（收口）。"""
    src = (main_module.Path(main_module.__file__).resolve().parent / "main.py")
    text = src.read_text(encoding="utf-8")
    hits = [
        line
        for line in text.splitlines()
        if "get_current_chat_provider_id" in line and line.strip().startswith("return")
    ]
    assert len(hits) == 1, f"解析点应只有 1 处，实际: {hits}"
    assert "self._current_chat_provider_id" in text


def test_live_agent_provider_id_reads_shared_config():
    """``_live_agent_provider_id`` 实时读共享 config（面板保存即生效）。"""
    cfg = _Cfg({})
    plugin = _plugin(cfg, _Ctx())
    assert plugin._live_agent_provider_id() == ""
    cfg["agent"] = {"provider_id": "p-multi"}
    assert plugin._live_agent_provider_id() == "p-multi"


def test_provider_flags_fail_open_for_undeclared():
    """``provider_flags``：空/缺 modalities ⇒ 两能力都按支持（fail-open）。"""
    for cfg in (None, {}, {"modalities": None}, {"modalities": []}):
        flags = agent_core.provider_flags(cfg)
        assert flags.declared is False
        assert flags.multimodal is True and flags.tool_capable is True
    flags = agent_core.provider_flags({"modalities": ["text"]})
    assert flags.declared is True
    assert flags.multimodal is False and flags.tool_capable is False
