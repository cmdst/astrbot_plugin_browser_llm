"""v1.8.0 文档⇄代码守卫（取代 v1.7.5 时代的 `test_v175_docs_vs_code.py`）。

钉住四类事实（每一条都能被"改代码没改文档"打破）：

1. **版本四处同一笔**：`metadata.yaml` / `main._PLUGIN_VERSION_FALLBACK` /
   CHANGELOG 首节 / README「当前版本」——本文件断言的是**四处一致 + 与
   `metadata.yaml`（单一事实源）相等 + 形如 vN.N.N**，而不是写死某个字面量：
   版本的**抬升**属于发布动作（v1.8.0 的抬版与 README/CHANGELOG 改稿在本分支的
   docs 任务里做，不在接线任务里），但任何一次单点漂移都会被这里抓住。
2. **工具面**：外层 **5** 个（v1.8.0 新增两个只读回看工具）+ 内层 5 个（26 → 5）；
3. **配置两口径**：配置叶 57 / JSON 叶子键 59（差 2 = `resources.viewport` 1↔3），
   且 README 与 CHANGELOG 都**带单位**写出来；v1.8.0 **零新增叶**；
4. **迁移与行为变化**：迁移器五模式（`--dry-run/--apply/--verify/--restore-from/--strip-v6`）、
   「先迁配置 + 成对回滚 + 只回其一会永久丢失」、以及**后台引擎退役带来的行为变化**
   （`all × core` → `all × plugin`，终态由插件模板生成）。

**待同步面（本任务 Out of scope，故这里用"豁免清单"而不是放宽断言）**：README /
CHANGELOG / metadata.yaml 属 docs 任务的写入面，本分支此刻仍写着 v1.7.5 与
`core/background.py`。因此 :data:`DOCS_PENDING` 逐条登记"文档里仍存在的旧字符串"，
文档改完后把这些条目删掉即可（删掉不会让任何用例变红 —— 豁免是减法，不是加法）。
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

#: 版本**不在本文件写死**（抬版是发布动作）：四处必须同值、且等于 metadata 的 version。
EXPECTED_OUTER = ("browse_web", "browse_local_page", "browser_web_search",
                  "browse_task_status", "browse_recent_results")
#: 文档侧待同步的旧字符串（docs 任务改完后逐条删除；豁免只做减法）。
DOCS_PENDING = {
    "module": "core/background.py",  # README 六层表仍指向已删的 5 态引擎
    "test_count": 32,                # README 里 `NN 个 test_*.py` 的旧数（现为磁盘实数）
}
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
    """四处必须同值（`metadata.yaml` 是单一事实源），且形如 vN.N.N。

    抬版是发布动作（v1.8.0 的抬版在 docs 任务里做）：本用例抓的是**单点漂移**——
    "改了 metadata 忘了 CHANGELOG""改了兜底值忘了 README"这两种真实事故。
    """
    declared = _version_of_metadata()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", declared), declared
    assert main_module._PLUGIN_VERSION_FALLBACK == declared, (
        "main._PLUGIN_VERSION_FALLBACK 与 metadata.yaml 的 version 漂移")
    assert main_module.PLUGIN_VERSION == declared, "PLUGIN_VERSION 没跟上 metadata"
    assert _first_changelog_version() == declared, "CHANGELOG 首节与 metadata 的版本漂移"
    assert _readme_version() == declared, "README 的「当前版本」与 metadata 的版本漂移"


def test_tool_surface_is_five_outer_plus_five_inner():
    """v1.8.0：外层 3 → **5**（新增两个只读回看工具），内层仍 5 而且两集合不相交。"""
    assert main_module._ALL_TOOL_NAMES == EXPECTED_OUTER
    inner = tuple(main_module.INNER_TOOL_NAMES)
    assert inner == (
        "browse_observe",
        "browse_act",
        "browse_screenshot",
        "browse_media",
        "browse_search",
    )
    assert not (set(main_module._ALL_TOOL_NAMES) & set(inner)), "外层与内层工具名不许相交"
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


def test_readme_current_part_documents_the_six_layers():
    """R5：现行部分必须写全六层 / 5 内层工具 / 57 叶 / 无起始页 / 无 core 引擎。

    v1.8.0：后台层从 `core/background.py`（5 态）换成 `core/tasks.py` + `core/proactive.py`。
    README 的改稿属 docs 任务，因此旧模块名走 :data:`DOCS_PENDING` 豁免；但**除它以外**
    的每个模块名都必须是本仓库真实文件（文档指向不存在的文件立刻红）。
    """
    current = _readme_current_section()
    for module in (
        "core/a11y.py",
        "core/action_model.py",
        "core/executor.py",
        "core/verify.py",
        "core/background.py",
        "core/config_map.py",
    ):
        if module == DOCS_PENDING["module"]:
            assert module in current, (
                f"待同步豁免已失效：README 现行部分已不写 {module}，请把 DOCS_PENDING 删掉")
            continue
        assert module in current, f"README 现行部分缺六层模块 {module}"
    for module in ("core/tasks.py", "core/proactive.py"):
        assert (ROOT / module).exists(), f"v1.8.0 的新模块 {module} 不在仓库里"
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
    # 测试文件数必须与磁盘一致（README 改稿属 docs 任务 ⇒ 旧值走待同步豁免）
    on_disk = len(list((ROOT / "tests").glob("test_*.py")))
    pending = DOCS_PENDING["test_count"]
    assert (f"{on_disk} 个 `test_*.py`" in current
            or (pending != on_disk and f"{pending} 个 `test_*.py`" in current)), (
        f"README 的测试文件数既不是磁盘实数 {on_disk} 也不是登记的待同步值 {pending}")


def test_changelog_test_numbers_are_not_stale():
    """R8：CHANGELOG 里的门禁条数必须是本轮实数（不许留上一轮的 770/775/776）。"""
    section = _v175_changelog_section()
    # 只禁"已过期的历史实数"；当前实数从 CHANGELOG 里读出来比一次即可
    for stale in ("770 passed", "775 passed", "776 passed", "781 passed", "783 passed", "785 passed"):
        assert stale not in section, f"CHANGELOG v1.7.5 节仍有旧数字 {stale}"
    latest = re.findall(r"(\d+) passed", section)
    assert latest, "CHANGELOG 必须写本轮全量用例实数（形如 `NNN passed`）"


def test_user_visible_docs_carry_current_leaf_counts():
    """t18：**用户可见**文档里的配置叶口径必须等于 `config_map` 的实数。

    这类漂移不会被任何功能用例抓到，但插件管理器/插件市场会**原样展示**
    `metadata.yaml` 的 `desc`（v1.7.5 终稿校验时就在那里发现「58 条 / 60 个」的旧数字）。
    因此这里按"已知过期串必须绝迹 + 当前口径必须出现"两侧夹住，不用正则抓数字
    （`58`/`60`/`57`/`59` 在别处有正常用法，正则一定会误伤）。
    """
    leaves = len(config_map.LEAVES)
    json_leaves = len(config_map.JSON_LEAF_KEYS)
    assert leaves == 57 and json_leaves == 59, (leaves, json_leaves)

    stale = ("58 条", "60 个", "15 条新叶", "15 条 v6 新叶", "58 叶", "60 个 JSON")

    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    current_readme = _readme_current_section()
    changelog_175 = _v175_changelog_section()
    migrator = (ROOT / "tools" / "migrate_config_v6.py").read_text(encoding="utf-8")

    # CHANGELOG 允许"点名说这个数字过期并已统一到新值"的行（那是变更记录，
    # 不是把旧数字当现状展示）；其余三处严格。
    exempt = ("统一到", "旧口径", "过期", "纠正")
    for name, text in (
        ("metadata.yaml", metadata),
        ("README.md（现行部分）", current_readme),
        ("CHANGELOG.md（v1.7.5 节）", changelog_175),
        ("tools/migrate_config_v6.py", migrator),
    ):
        for needle in stale:
            for line in text.splitlines():
                if needle not in line:
                    continue
                if name.startswith("CHANGELOG") and any(word in line for word in exempt):
                    continue
                raise AssertionError(f"{name} 仍出现过期口径「{needle}」：{line.strip()[:120]}")

    # 当前口径必须在"用户第一眼看到"的地方出现（metadata desc + README 现行部分）
    assert f"配置叶 {leaves} 条" in metadata, metadata[:400]
    assert f"JSON 叶子键 {json_leaves} 个" in metadata
    assert f"配置叶 {leaves} 条" in current_readme
    assert f"JSON 叶子键 {json_leaves} 个" in current_readme
    assert f"配置叶 {leaves} 条" in changelog_175
    # 迁移器的帮助/文档串必须写 57 叶与 14 条新增叶
    assert "57 叶" in migrator or "57 leaves" in migrator
    assert "14 条新叶" in migrator or "14 new paths" in migrator
