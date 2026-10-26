"""v1.7.5 文档⇄代码守卫（取代 v1.7.0 时代的 `test_v170_docs_vs_code.py`）。

钉住四类事实（每一条都能被"改代码没改文档"打破）：

1. **版本四处同一笔**：`metadata.yaml` / `main._PLUGIN_VERSION_FALLBACK` /
   CHANGELOG 首节 / README「当前版本」；
2. **工具面**：外层 3 个名字不变 + 内层 5 个（26 → 5）；
3. **配置两口径**：配置叶 58 / JSON 叶子键 60（差 2 = `resources.viewport` 1↔3），
   且 README 与 CHANGELOG 都**带单位**写出来；
4. **迁移与行为变化**：迁移器五模式（`--dry-run/--apply/--verify/--restore-from/--strip-v6`）、
   「先迁配置 + 成对回滚 + 只回其一会永久丢失」、以及**后台引擎退役带来的行为变化**
   （`all × core` → `all × plugin`，终态由插件模板生成）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import main as main_module  # noqa: E402 — conftest 已装包别名
from astrbot_plugin_browser_llm.core import config_map  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
METADATA = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
MIGRATOR = (ROOT / "tools" / "migrate_config_v6.py").read_text(encoding="utf-8")

EXPECTED_VERSION = "v1.7.5"
EXPECTED_OUTER = ("browse_web", "browse_local_page", "browser_web_search")
EXPECTED_LEAVES = 57
EXPECTED_JSON_LEAVES = 59
MIGRATOR_MODES = ("--dry-run", "--apply", "--verify", "--restore-from", "--strip-v6")


def _version_of_metadata() -> str:
    match = re.search(r"^version:\s*(\S+)$", METADATA, re.MULTILINE)
    assert match, "metadata.yaml 里找不到 version:"
    return match.group(1).strip()


def _first_changelog_version() -> str:
    match = re.search(r"^##\s*\[(v\d+\.\d+\.\d+)\]", CHANGELOG, re.MULTILINE)
    assert match, "CHANGELOG 里找不到任何 `## [vX.Y.Z]` 版本节"
    return match.group(1)


def _readme_version() -> str:
    match = re.search(r"当前版本：\*\*(v\d+\.\d+\.\d+)\*\*", README)
    assert match, "README 里找不到「当前版本：**vX.Y.Z**」这一行"
    return match.group(1)


def _flatten(schema: dict) -> list[str]:
    """展平 `_conf_schema.json`（只递归 `type=="object"` 的组/子对象）。"""
    out: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            if isinstance(value, dict) and value.get("type") == "object":
                walk(value.get("items") or value.get("properties") or {}, f"{prefix}{key}.")
            else:
                out.append(f"{prefix}{key}")

    walk(schema, "")
    return out


def test_version_is_consistent_in_four_places():
    assert _version_of_metadata() == EXPECTED_VERSION, "metadata.yaml 的 version 没对齐"
    assert main_module._PLUGIN_VERSION_FALLBACK == EXPECTED_VERSION
    assert main_module.PLUGIN_VERSION == EXPECTED_VERSION
    assert _first_changelog_version() == EXPECTED_VERSION, "CHANGELOG 首节不是 v1.7.5"
    assert _readme_version() == EXPECTED_VERSION, "README 的「当前版本」不是 v1.7.5"


def test_tool_surface_is_three_outer_plus_five_inner():
    assert main_module._ALL_TOOL_NAMES == EXPECTED_OUTER
    assert tuple(main_module.INNER_TOOL_NAMES) == (
        "browse_observe",
        "browse_act",
        "browse_screenshot",
        "browse_media",
        "browse_search",
    )
    assert "26 → 5" in README and "26 → 5" in CHANGELOG


def test_config_counts_match_docs_with_units():
    assert len(config_map.KEY_MAP) == EXPECTED_LEAVES, "配置叶必须是 58"
    assert len(config_map.GROUPS) == 7
    assert len(_flatten(SCHEMA)) == EXPECTED_JSON_LEAVES, "JSON 叶子键必须是 60"
    assert EXPECTED_JSON_LEAVES - EXPECTED_LEAVES == 2, "差 2 = resources.viewport 1↔3"
    for name, text in (("README", README), ("CHANGELOG", CHANGELOG)):
        assert "配置叶 57 条" in text, f"{name} 缺「配置叶 58 条」"
        assert "JSON 叶子键 59 个" in text, f"{name} 缺「JSON 叶子键 60 个」"
    assert "resources.viewport" in README


def test_migrator_has_five_modes_and_docs_mention_them():
    for flag in MIGRATOR_MODES:
        assert flag in MIGRATOR, f"migrate_config_v6.py 缺模式 {flag}"
        assert flag in README, f"README 缺迁移器模式 {flag}"
        assert flag in CHANGELOG, f"CHANGELOG 缺迁移器模式 {flag}"


def test_docs_require_config_before_code_and_paired_rollback():
    for name, text in (("README", README), ("CHANGELOG", CHANGELOG)):
        assert "先迁配置" in text, f"{name} 没写「先迁配置」"
        assert "成对" in text, f"{name} 没写「成对回滚」"
        assert "不可恢复" in text or "永久丢失" in text, f"{name} 没写「只回其一会丢设置」"


def test_docs_state_the_background_behaviour_change():
    """引擎退役是**行为变化**：必须在 CHANGELOG 里逐字写明。"""
    assert "all × core" in CHANGELOG, "CHANGELOG 没写升级前的现值组合"
    assert "all × plugin" in CHANGELOG, "CHANGELOG 没写升级后的组合"
    assert "插件模板生成的一条终态消息" in CHANGELOG, "CHANGELOG 没写投递方式的变化"
    assert "background_engine" in CHANGELOG, "CHANGELOG 没点名被删除的配置键"


def _v175_changelog_section() -> str:
    """只截 `## [v1.7.5]` 那一节（历史节的表述不受本守卫约束）。"""
    start = CHANGELOG.index("## [v1.7.5]")
    rest = CHANGELOG[start:]
    nxt = re.search(r"\n## \[v", rest[3:])
    return rest if nxt is None else rest[: nxt.start() + 3]


def _readme_current_section() -> str:
    """README 的**现行**部分 = 「历史版本」标记之前的全部内容（R5）。"""
    end = README.find("## 📜 历史版本")
    assert end > 0, "README 缺「历史版本」分章标记（R5 要求旧内容整体归档）"
    return README[:end]


def _readme_history_section() -> str:
    end = README.find("## 📜 历史版本")
    return README[end:] if end > 0 else ""


def test_docs_do_not_advertise_deleted_tools_as_current():
    """已删的内层方法不许在**现行**文档里被当成能力介绍（历史节除外）。"""
    deleted = ("browse_click_coords", "browse_get_interactive", "browse_sniff_media")
    for name in deleted:
        assert name not in _readme_current_section(), (
            f"README 的现行部分把已删的 {name} 当能力在写"
        )
        section = _v175_changelog_section()
        for match in re.finditer(re.escape(name), section):
            window = section[max(0, match.start() - 140) : match.start() + 140]
            assert any(
                word in window for word in ("旧", "退役", "删除", "整条", "旧名")
            ), f"CHANGELOG v1.7.5 节把已删的 {name} 当现行能力在写：…{window}…"


def test_readme_current_part_has_no_deleted_keys_or_tools():
    """R5：现行部分不得把已删的键/工具/引擎当现状文档（历史章节里允许）。"""
    current = _readme_current_section()
    forbidden = (
        "search.mode",
        "browse_web_search",
        "browser.default_url",
        "tools.background_engine",
        "内层工具面：26",
        "is_background_task",
    )
    # 允许"点名说它已删除"的行（那是迁移说明，不是把旧键当现状）
    retired_words = ("退役", "已删", "删除", "不再", "改为", "已取消")
    for needle in forbidden:
        for line in current.splitlines():
            if needle not in line:
                continue
            assert any(word in line for word in retired_words), (
                f"README 现行部分把已删的 {needle} 当现状写：{line.strip()[:120]}"
            )


def test_readme_current_part_documents_v175_surface():
    """R5：现行部分必须写全六层 / 5 工具 / 58 叶 / 无起始页 / 无 core 引擎。"""
    current = _readme_current_section()
    for module in (
        "core/a11y.py",
        "core/action_model.py",
        "core/executor.py",
        "core/verify.py",
        "core/background.py",
        "core/config_map.py",
    ):
        assert module in current, f"README 现行部分缺六层模块 {module}"
    for tool in ("browse_observe", "browse_act", "browse_screenshot", "browse_media", "browse_search"):
        assert tool in current, f"README 现行部分缺内层工具 {tool}"
    assert "配置叶 57 条" in current and "JSON 叶子键 59 个" in current
    assert "【需要网址】" in current, "必须写清无起始页后的行为"
    assert "退役" in current or "无 core 引擎" in current
    # 58 叶键表：每个组都在现行部分出现
    for group in ("resources", "perception", "model", "actions", "search", "security", "tools"):
        assert f"`{group}`" in current, f"v6 键表缺组 {group}"


def test_readme_history_section_is_marked_and_reachable():
    history = _readme_history_section()
    assert "v1.7.0 及更早" in history
    assert "一律以前者为准" in history or "以其为准" in history


# ------------------------------------------------ R25：动作清单/视图/前缀必须"生成式"校验


def test_readme_action_list_is_generated_from_action_spec():
    """R25：README 的 16 动作清单必须是 `ACTION_SPEC` 的镜像（手抄必错）。"""
    from astrbot_plugin_browser_llm.core import action_model

    current = _readme_current_section()
    names = [spec.name.lower() for spec in action_model.ACTION_SPEC]
    assert len(names) == 16, names
    for name in names:
        assert f"`{name}`" in current, f"README 现行部分缺动作 {name}"
    # 反向：只查"动作清单那一段"，不许残留旧清单（`links`/`media` 是 observe 视图
    # 与工具名，不能全局禁）
    para_start = current.index("16 个动作")
    para = current[para_start : para_start + 500]
    for stale in ("`uncheck`", "`wait_for`", "`links`", "`media`", "`hover``"):
        assert stale not in para, f"README 的动作清单残留了 {stale}"


def test_readme_observe_views_match_code():
    """R25：`browse_observe` 的视图名必须与代码常量一致。"""
    import main as m

    current = _readme_current_section()
    for view in m.OBSERVE_VIEWS:
        assert f"`{view}`" in current, f"README 缺 observe 视图 {view}"
    assert "`compact`" not in current, "`compact` 不是合法视图（v1.7.5 是 structure/text/links/full）"


def test_readme_act_prefix_matches_code():
    """R25：`browse_act` 的返回前缀在文档与代码里必须一致（代码是【动作】）。"""
    current = _readme_current_section()
    assert "【动作】" in current, "README 必须写对 browse_act 的前缀"
    assert "【执行】" not in current, "【执行】是旧前缀，代码已改成【动作】"


def test_readme_current_part_has_no_legacy_numbers():
    """R25：现行部分不许再出现 v1.7.0 的数字/文件名（1128、50 文件、54 叶、v3/v4 迁移器）。"""
    current = _readme_current_section()
    for stale in ("1128", "配置叶 54 条", "migrate_config_v3", "migrate_config_v4"):
        assert stale not in current, f"README 现行部分仍有旧现状：{stale}"
    # 测试文件数必须与磁盘一致
    on_disk = len(list((ROOT / "tests").glob("test_*.py")))
    assert f"{on_disk} 个 `test_*.py`" in current, f"README 的测试文件数必须写 {on_disk}"


def test_changelog_test_numbers_are_not_stale():
    """R8：CHANGELOG 里的门禁条数必须是本轮实数（不许留上一轮的 770/775/776）。"""
    section = _v175_changelog_section()
    for stale in ("770 passed", "775 passed", "776 passed", "781 passed"):
        assert stale not in section, f"CHANGELOG v1.7.5 节仍有旧数字 {stale}"
    assert "783 passed" in section, "CHANGELOG 必须写本轮实数（781 passed）"
