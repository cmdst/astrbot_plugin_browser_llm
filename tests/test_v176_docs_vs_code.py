"""v1.7.6 文档⇄代码守卫：**发布面口径只说一次，且必须等于代码**。

v1.7.5 那套（``test_v175_docs_vs_code.py``）钉的是 v1.7.5 的架构面；本文件钉 v1.7.6
新增的三件事，防止「README 写 v1.7.5、代码是 v1.7.6」这类漂移：

1. **版本四处同一笔** = `v1.7.6`（`metadata.yaml` / `main._PLUGIN_VERSION_FALLBACK` /
   CHANGELOG 首节 / README「当前版本」），且 `main.PLUGIN_VERSION` 运行时也解析到它；
2. **用户可见数字与代码一致**：配置叶 `len(config_map.LEAVES)` = 57、JSON 叶子键
   `len(config_map.JSON_LEAF_KEYS)` = 59（差 2 = `resources.viewport` 1↔3）、外层 3 个 /
   内层 5 个工具；README 与 `metadata.yaml` 的 `desc`（插件市场原样展示）都要写对；
3. **新行为写进文档、且文档里的标识符是代码里真有的**：CHANGELOG 的 v1.7.6 节必须写明
   `Context.send_message` + 钩子重放、`LLM_RESULT`、`plugins_name` 继承、媒体随终态一条、
   记账拆五字段、四条已知边界；README 现行部分也要有；而 `context.send_message` /
   `platform_fallback` / 五字段名 / `qq_official_group_scene_unset` 这些字符串必须能在
   :mod:`core.active_send` 与 :mod:`core.background` 里找到同名事实（不是文档自说自话）。

最后一条：README 的 `test_*.py` 计数与「N passed」必须等于**收口时的实测值**（计数与磁盘
同数，两处文档同数）。
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import main as main_module  # noqa: E402 — conftest 已装包别名
from astrbot_plugin_browser_llm.core import active_send, background, config_map  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
METADATA = (ROOT / "metadata.yaml").read_text(encoding="utf-8")

EXPECTED_VERSION = "v1.7.6"
EXPECTED_LEAVES = 57
EXPECTED_JSON_LEAVES = 59
EXPECTED_OUTER = ("browse_web", "browse_local_page", "browser_web_search")
EXPECTED_INNER = (
    "browse_observe",
    "browse_act",
    "browse_screenshot",
    "browse_media",
    "browse_search",
)
ACCOUNTING_FIELDS = (
    "attempted",
    "succeeded",
    "accepted_by_platform",
    "error",
    "sent_via",
)


def _readme_current_section() -> str:
    """README 的**现行**部分（「历史版本」标记之前）；历史节里的旧说法不受约束。"""
    end = README.find("## 📜 历史版本")
    assert end > 0, "README 缺「历史版本」分章标记"
    return README[:end]


def _changelog_section(version: str) -> str:
    """截取 `## [vX.Y.Z]` 那一节（到下一个 `## [v…` 为止）。"""
    marker = f"## [{version}]"
    start = CHANGELOG.index(marker)
    rest = CHANGELOG[start + len(marker) :]
    nxt = re.search(r"\n## \[v", rest)
    return rest if nxt is None else rest[: nxt.start()]


def _first_changelog_version() -> str:
    match = re.search(r"^##\s*\[(v\d+\.\d+\.\d+)\]", CHANGELOG, re.MULTILINE)
    assert match, "CHANGELOG 里找不到任何 `## [vX.Y.Z]` 版本节"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. 版本四处 + 运行时解析
# ---------------------------------------------------------------------------


def test_version_is_v176_in_four_places():
    metadata_version = re.search(r"^version:\s*(\S+)$", METADATA, re.MULTILINE)
    assert metadata_version and metadata_version.group(1) == EXPECTED_VERSION
    assert main_module._PLUGIN_VERSION_FALLBACK == EXPECTED_VERSION, "main.py 的兜底版本没对齐"
    assert main_module.PLUGIN_VERSION == EXPECTED_VERSION, "运行时解析到的版本不是当前版本"
    assert _first_changelog_version() == EXPECTED_VERSION, "CHANGELOG 首节不是 v1.7.6"
    readme_version = re.search(r"当前版本：\*\*(v\d+\.\d+\.\d+)\*\*", README)
    assert readme_version and readme_version.group(1) == EXPECTED_VERSION, (
        "README 的「当前版本」不是 v1.7.6"
    )


def test_changelog_has_a_v176_section_with_boundaries():
    section = _changelog_section(EXPECTED_VERSION)
    assert "已知边界" in section, "CHANGELOG v1.7.6 节必须单列「已知边界」"
    assert len(section) > 1500, "v1.7.6 节太短（行为/记账/边界三块都要写）"


# ---------------------------------------------------------------------------
# 2. 用户可见数字 = 代码实数
# ---------------------------------------------------------------------------


def test_leaf_counts_match_code_and_user_visible_docs():
    leaves = len(config_map.LEAVES)
    json_leaves = len(config_map.JSON_LEAF_KEYS)
    assert (leaves, json_leaves) == (EXPECTED_LEAVES, EXPECTED_JSON_LEAVES), (leaves, json_leaves)
    assert json_leaves - leaves == 2, "差 2 恒来自 resources.viewport（1 条复合叶 ↔ 3 个键）"
    assert len(config_map.KEY_MAP) == EXPECTED_LEAVES
    current = _readme_current_section()
    for name, text in (("README 现行部分", current), ("metadata.yaml desc", METADATA)):
        assert f"配置叶 {leaves} 条" in text, f"{name} 没写「配置叶 {leaves} 条」"
        assert f"JSON 叶子键 {json_leaves} 个" in text, f"{name} 没写「JSON 叶子键 {json_leaves} 个」"


def test_tool_surface_matches_user_visible_docs():
    assert tuple(main_module._ALL_TOOL_NAMES) == EXPECTED_OUTER
    assert tuple(main_module.INNER_TOOL_NAMES) == EXPECTED_INNER
    current = _readme_current_section()
    assert "外层 3 个" in current, "README 必须写「外层 3 个」"
    assert "内层 5 个" in current, "README 必须写「内层 5 个」"
    for name in (*EXPECTED_OUTER, *EXPECTED_INNER):
        assert f"`{name}`" in current, f"README 现行部分缺工具 {name}"


def test_readme_counts_and_gate_numbers_are_closed():
    """README 的测试文件数 = 磁盘实数；README 与 CHANGELOG 的门禁总数必须一致。"""
    on_disk = len(list((ROOT / "tests").glob("test_*.py")))
    current = _readme_current_section()
    assert f"{on_disk} 个 `test_*.py`" in current, f"README 的测试文件数必须写 {on_disk}"
    readme_gate = re.search(r"当前门禁：\*\*(\d+) passed / 0 failed\*\*", current)
    assert readme_gate, "README 现行部分缺「当前门禁：**N passed / 0 failed**」"
    changelog_gate = re.search(r"全量门禁 \*\*(\d+) passed / 0 failed\*\*", _changelog_section(EXPECTED_VERSION))
    assert changelog_gate, "CHANGELOG v1.7.6 节缺「全量门禁 **N passed / 0 failed**」"
    assert readme_gate.group(1) == changelog_gate.group(1), (
        f"README({readme_gate.group(1)}) 与 CHANGELOG({changelog_gate.group(1)}) 的门禁数不一致"
    )


# ---------------------------------------------------------------------------
# 3. 新行为：文档写了，且文档里的标识符在代码里是真的
# ---------------------------------------------------------------------------


def test_changelog_documents_behaviour_change():
    section = _changelog_section(EXPECTED_VERSION)
    required = (
        "Context.send_message",
        "on_decorating_result",
        "on_after_message_sent",
        "LLM_RESULT",
        "plugins_name",
        "event.send",
        "附件",
        "触发消息 ID",
    )
    for needle in required:
        assert needle in section, f"CHANGELOG v1.7.6 节缺「{needle}」"


def test_changelog_documents_accounting_split():
    section = _changelog_section(EXPECTED_VERSION)
    for name in ACCOUNTING_FIELDS:
        assert name in section, f"CHANGELOG v1.7.6 节缺记账字段「{name}」"
    assert "delivered" in section and "闸" in section, "必须写清 delivered 收窄为闸语义"


def test_changelog_documents_known_boundaries():
    section = _changelog_section(EXPECTED_VERSION)
    for needle in (
        "内容安全",
        "stage",
        "provider_ltm_settings",
        "qq_official",
        "回退",
        "HookTimeout",
    ):
        assert needle in section, f"CHANGELOG v1.7.6 节的已知边界缺「{needle}」"


def test_readme_documents_v176_behaviour():
    current = _readme_current_section()
    for needle in (
        "Context.send_message",
        "on_decorating_result",
        "on_after_message_sent",
        "attempted",
        "sent_via",
        "触发消息 ID",
    ):
        assert needle in current, f"README 现行部分缺 v1.7.6 行为说明「{needle}」"


def test_doc_identifiers_exist_in_code():
    """文档点名的标识符必须等于代码里的事实（不是文档自说自话）。"""
    assert active_send.CHANNEL_CONTEXT == "context.send_message"
    assert active_send.CHANNEL_FALLBACK == "platform_fallback"
    assert active_send.CHANNEL_DECORATOR == "decorator"
    assert active_send.QQ_OFFICIAL_GROUP_HINT == "qq_official_group_scene_unset"
    assert active_send.HOOK_DECORATING == "on_decorating_result"
    assert active_send.HOOK_AFTER_SENT == "on_after_message_sent"
    assert active_send.ERROR_DECORATOR_SUPPRESSED == "DecoratorSuppressed"

    outcome_fields = {item.name for item in dataclasses.fields(active_send.DeliveryOutcome)}
    for name in ACCOUNTING_FIELDS:
        assert name in outcome_fields, f"DeliveryOutcome 缺字段 {name}"
    # TerminalOutcome 直读同一批字段（properties）
    for name in ACCOUNTING_FIELDS:
        assert isinstance(getattr(background.TerminalOutcome, name, None), property), (
            f"TerminalOutcome.{name} 必须是直读属性"
        )
    assert background.ERROR_DELIVERY_UNREPORTED == "DeliveryUnreported"


def test_readme_structure_lists_every_core_module():
    """README 的仓库结构必须与 `core/` 实际模块同集（v1.7.6 新增 active_send 就是靠这条抓住的）。"""
    current = _readme_current_section()
    actual = {
        f"core/{path.name}"
        for path in (ROOT / "core").glob("*.py")
        if path.name != "__init__.py"
    }
    listed = {match for match in re.findall(r"core/[a-z0-9_]+\.py", current)}
    missing = sorted(actual - listed)
    stale = sorted(listed - actual)
    assert not missing, f"README 仓库结构缺模块：{missing}"
    assert not stale, f"README 仓库结构列了不存在的模块：{stale}"
    assert f"`core/` 现有 {len(actual)} 个模块" in current, (
        f"README 必须写「`core/` 现有 {len(actual)} 个模块」"
    )
