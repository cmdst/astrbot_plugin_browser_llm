"""v1.8.0 文档⇄代码守卫（取代 v1.7.5 时代的 `test_v175_docs_vs_code.py`）。

钉住六类事实（每一条都能被"改代码没改文档"打破，而**任何功能用例都抓不到**）：

1. **版本四处同一笔**：`metadata.yaml` 的 `version` / `main._PLUGIN_VERSION_FALLBACK` /
   CHANGELOG 首节 / README「当前版本」——四处必须等于 :data:`EXPECTED_VERSION`（v1.8.0）。
   抬版是发布动作：**抬版时本常量必须同笔移动**，否则本文件立刻红（这正是 docs gate 的用法）。
2. **工具面**：外层恰好 **5** 个（v1.8.0 新增两个只读回看工具）+ 内层 5 个 + 两集合不相交，
   且 **README 的工具表 ⇄ `main._ALL_TOOL_NAMES` 双向相等**（文档多写一个 / 少写一个都红）。
3. **配置两口径冻结在 v6**：配置叶 57 / JSON 叶子键 59（差 2 = `resources.viewport` 1↔3）/ 7 组；
   v1.8.0 **零新增叶**（`CONFIG_V6_NEW_PATHS` 仍是 14，且禁止出现候选新叶名）。
4. **任务簿与投递的语义边界**：状态查询**不含正文 / 不给完成时间**、回看 **5 条 / 30 分钟 / 不跨会话**、
   投递**不重试不补发**、**内容安全 stage 不重放**（L3 不交付）、只重放两个插件钩子。
5. **行为面文案**：A1 根因（模型把 `action` 写成 JSON 字符串 ⇒ 8/8 R-A1）、v1.7.6/v1.7.7 作废、
   配置 v6 不变 ⇒ 无迁移、7 态任务簿、外层 3 → 5。
6. **不许回流的旧口径**：`core/background.py`、`search.mode`、`browser.default_url`、
   `is_background_task`、"配置叶 58 条"/"JSON 叶子键 60 个"/"15 条新叶"、上一轮的门禁条数。

写作纪律：本文件**不写死任何历史门禁条数**（`NNN passed` 从 CHANGELOG 现读现比），失败提示里
也只报"期望 vs 实际"，不写"必须是 58"这类会自己过期的示例数字。
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

import main as main_module  # noqa: E402 — conftest 已装 astrbot 桩 + 包别名
from astrbot_plugin_browser_llm.core import config_map, tasks  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
METADATA = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
MIGRATOR = (ROOT / "tools" / "migrate_config_v6.py").read_text(encoding="utf-8")

#: 发布动作的锚点：抬版时本常量与本文件同笔移动（四处任何一处漂移都会被下面第 1 条抓住）。
EXPECTED_VERSION = "v1.8.0"
EXPECTED_OUTER = (
    "browse_web",
    "browse_local_page",
    "browser_web_search",
    "browse_task_status",
    "browse_recent_results",
)
EXPECTED_INNER = (
    "browse_observe",
    "browse_act",
    "browse_screenshot",
    "browse_media",
    "browse_search",
)
EXPECTED_GROUPS = ("resources", "perception", "model", "actions", "search", "security", "tools")
EXPECTED_LEAVES = 57
EXPECTED_JSON_LEAVES = 59
EXPECTED_NEW_V6_PATHS = 14
MIGRATOR_MODES = ("--dry-run", "--apply", "--verify", "--restore-from", "--strip-v6")
#: v1.8.0 明确**不加**的配置叶（§8.4：要可配就加这两条 ⇒ 本版不做，登记 v1.8.1 候选）。
FORBIDDEN_NEW_LEAVES = ("tools.recall_window_min", "tools.recall_limit")
#: 用户可见文档里**必须绝迹**的过期口径（v1.7.5 终稿前的 58/60 与"15 条新叶"）。
STALE_LEAF_NUMBERS = ("58 条", "60 个", "58 叶", "60 个 JSON", "15 条新叶", "15 条 v6 新叶",
                      "15 new paths")


def _version_of_metadata() -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", METADATA, re.MULTILINE)
    assert match, "metadata.yaml 里找不到 `version:`"
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


def _readme_current_section() -> str:
    """README 的**现行**部分 = 「历史版本」标记之前的全部内容。"""
    end = README.find("## 📜 历史版本")
    assert end > 0, "README 缺「历史版本」分章标记（旧内容必须整体归档）"
    return README[:end]


def _readme_history_section() -> str:
    end = README.find("## 📜 历史版本")
    return README[end:] if end > 0 else ""


def _v175_changelog_section() -> str:
    """只截 `## [v1.7.5]` 那一节（历史节的表述不受 v1.8.0 守卫约束）。"""
    start = CHANGELOG.index("## [v1.7.5]")
    rest = CHANGELOG[start:]
    nxt = re.search(r"\n## \[v", rest[3:])
    return rest if nxt is None else rest[: nxt.start() + 3]


def _v180_changelog_section() -> str:
    start = CHANGELOG.index("## [v1.8.0]")
    rest = CHANGELOG[start:]
    nxt = re.search(r"\n## \[v", rest[3:])
    return rest if nxt is None else rest[: nxt.start() + 3]


def _readme_tool_table() -> tuple[set[str], set[str]]:
    """README 工具表里的(外层, 内层)工具名集合 —— 与代码注册双向比对用。"""
    current = _readme_current_section()
    outer = set(re.findall(r"^\|\s*外层\s*\|\s*`([a-z_]+)`", current, re.MULTILINE))
    inner = set(re.findall(r"^\|\s*内层\s*\|\s*`([a-z_]+)`", current, re.MULTILINE))
    return outer, inner


# ------------------------------------------------------------------ 1. 版本四处


def test_version_is_consistent_in_four_places():
    """版本四处必须同值、等于发布锚点，且形如 vN.N.N。"""
    declared = _version_of_metadata()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", declared), declared
    assert declared == EXPECTED_VERSION, f"metadata.yaml 的版本是 {declared}，期望 {EXPECTED_VERSION}"
    assert main_module._PLUGIN_VERSION_FALLBACK == declared, (
        "main._PLUGIN_VERSION_FALLBACK 与 metadata.yaml 的 version 漂移")
    assert main_module.PLUGIN_VERSION == declared, "PLUGIN_VERSION 没跟上 metadata"
    assert _first_changelog_version() == declared, "CHANGELOG 首节与 metadata 的版本漂移"
    assert _readme_version() == declared, "README 的「当前版本」与 metadata 的版本漂移"


# --------------------------------------------------------------- 2. 工具面 3 + 5


def test_outer_and_inner_tool_surface_are_exactly_five_each():
    """v1.8.0：外层 3 → **5**（两个只读回看工具），内层仍 5，两集合不相交。"""
    assert tuple(main_module._ALL_TOOL_NAMES) == EXPECTED_OUTER
    assert len(EXPECTED_OUTER) == 5 and len(set(EXPECTED_OUTER)) == 5
    assert tuple(main_module.INNER_TOOL_NAMES) == EXPECTED_INNER
    assert not (set(EXPECTED_OUTER) & set(EXPECTED_INNER)), "外层与内层工具名不许相交"
    assert "26 → 5" in README, "README 必须保留 v1.7.5 的「内层工具面 26 → 5」收口口径"
    assert "26 → 5" in CHANGELOG, "CHANGELOG 必须保留「内层工具面 26 → 5」"


def test_readme_tool_table_matches_code_registration():
    """README 工具表 ⇄ `_ALL_TOOL_NAMES` / `INNER_TOOL_NAMES` **双向**相等。

    单向只查"代码里的名字在文档里出现过"会漏掉"文档把已删工具当现状在写"；反向只查
    "文档里的名字在代码里存在"会漏掉"代码新增了工具但文档没写"。两侧都要。
    """
    outer, inner = _readme_tool_table()
    assert outer == set(EXPECTED_OUTER), f"README 外层工具表与注册不一致：多 {outer - set(EXPECTED_OUTER)}、少 {set(EXPECTED_OUTER) - outer}"
    assert inner == set(EXPECTED_INNER), f"README 内层工具表与注册不一致：多 {inner - set(EXPECTED_INNER)}、少 {set(EXPECTED_INNER) - inner}"


def test_readme_documents_the_two_new_tools_with_bounds():
    """两个新工具的用法与边界必须写清（含参数名与拒绝码家族）。"""
    current = _readme_current_section()
    for needle in ("browse_task_status", "browse_recent_results", "task_id", "limit", "minutes"):
        assert needle in current, f"README 现行部分缺 {needle}"
    for code in ("TS-2", "TS-3", "RR-2", "RR-3"):
        assert code in current, f"README 现行部分缺拒绝码 {code}"
    # 工具注册恒定（不是"off 就不注册"）
    assert "注册恒定" in current, "README 必须写清两个工具注册恒定（off 时行为落在 TS-3/RR-3）"


def test_readme_states_background_results_do_not_enter_context():
    """本版对用户最重要的行为：后台结果**不进**上下文 ⇒ 想回看必须用回看工具。"""
    current = _readme_current_section()
    assert "结果不进上下文" in current, "README 必须写「结果不进上下文」"
    assert "browse_recent_results" in current
    # 使用示例里也要有可照抄的一句
    assert "刚才那个后台任务跑完了吗" in current, "README 的使用示例要给回看的自然语言例子"


# -------------------------------------------------------- 3. 配置不变量（v6 冻结）


def test_config_counts_are_frozen_at_v6():
    """57 叶 / 59 JSON 键 / 7 组，差恒为 2（`resources.viewport` 1↔3）。"""
    assert len(config_map.KEY_MAP) == EXPECTED_LEAVES, (
        f"配置叶应为 {EXPECTED_LEAVES}，实际 {len(config_map.KEY_MAP)}")
    assert len(config_map.LEAVES) == EXPECTED_LEAVES
    assert len(config_map.GROUPS) == EXPECTED_GROUPS.__len__(), "组数漂移"
    assert tuple(config_map.GROUPS) == EXPECTED_GROUPS, config_map.GROUPS
    json_leaves = _flatten(SCHEMA)
    assert len(json_leaves) == EXPECTED_JSON_LEAVES, (
        f"JSON 叶子键应为 {EXPECTED_JSON_LEAVES}，实际 {len(json_leaves)}")
    assert EXPECTED_JSON_LEAVES - EXPECTED_LEAVES == 2, "差 2 = resources.viewport 1↔3"
    assert tuple(SCHEMA) == EXPECTED_GROUPS, f"schema 顶层组名漂移：{tuple(SCHEMA)}"


def test_no_new_config_leaves_in_v180():
    """v1.8.0 零新增叶：v6 新增叶仍是 14 条，且两个"改口候选"叶不存在。"""
    new_paths = tuple(getattr(config_map, "CONFIG_V6_NEW_PATHS"))
    assert len(new_paths) == EXPECTED_NEW_V6_PATHS, (
        f"v6 新增叶应为 {EXPECTED_NEW_V6_PATHS} 条，实际 {len(new_paths)}")
    for leaf in FORBIDDEN_NEW_LEAVES:
        assert leaf not in config_map.KEY_MAP, f"v1.8.0 不许新增配置叶 {leaf}（§8.4 登记 v1.8.1）"
        assert leaf not in config_map.LEAVES
    # 两个新工具复用既有叶，不许自造开关
    assert "tools.background_mode" in config_map.KEY_MAP
    assert "tools.background_max_chars" in config_map.KEY_MAP


def test_docs_carry_config_counts_with_units():
    """三处用户可见文本都必须**带单位**写 57 / 59（裸写"57 叶"会被误读成 JSON 键数）。"""
    for name, text in (
        ("metadata.yaml", METADATA),
        ("README.md（现行部分）", _readme_current_section()),
        ("CHANGELOG.md（v1.8.0 节）", _v180_changelog_section()),
    ):
        assert f"配置叶 {EXPECTED_LEAVES} 条" in text, f"{name} 缺「配置叶 {EXPECTED_LEAVES} 条」"
        assert f"JSON 叶子键 {EXPECTED_JSON_LEAVES} 个" in text, (
            f"{name} 缺「JSON 叶子键 {EXPECTED_JSON_LEAVES} 个」")


def test_user_visible_docs_have_no_stale_leaf_numbers():
    """过期口径必须绝迹（metadata / README 现行部分 / CHANGELOG v1.8.0 节 / 迁移器）。

    CHANGELOG 的**历史节**允许"点名说这个数字过期并已统一到新值"的句子 —— 那是变更记录，
    不是把旧数字当现状展示，故这里只查 v1.8.0 节。
    """
    for name, text in (
        ("metadata.yaml", METADATA),
        ("README.md（现行部分）", _readme_current_section()),
        ("CHANGELOG.md（v1.8.0 节）", _v180_changelog_section()),
        ("tools/migrate_config_v6.py", MIGRATOR),
    ):
        for needle in STALE_LEAF_NUMBERS:
            for line in text.splitlines():
                assert needle not in line, f"{name} 仍出现过期口径「{needle}」：{line.strip()[:120]}"
    # 迁移器口径：43 承接叶 + 14 新叶（v1.7.5 终稿已收口，别再回流）
    assert f"{EXPECTED_NEW_V6_PATHS} 条新叶" in MIGRATOR or "14 new paths" in MIGRATOR


def test_migrator_header_says_v180_needs_no_migration():
    """迁移器的文件头必须回答运营商唯一会问的问题："v1.8.0 要不要跑我？"

    v1.8.0 零新增叶 ⇒ 从 v1.7.5 升上来**不需要**迁移；只有从 v1.6.x / v1.7.0（v5）升级才要。
    这条文案是"15 条新叶"那类历史遗留最容易回流的地方，故单独钉一条。
    """
    head = MIGRATOR.split('"""', 2)[1]
    assert "v1.8.0" in head, "迁移器文件头必须点名 v1.8.0"
    assert "不需要运行本脚本" in head or "不需要迁移" in head, (
        "迁移器文件头必须写清 v1.7.5 → v1.8.0 不需要运行迁移")
    assert "先迁配置" in head, "迁移器文件头必须保留「先迁配置、再换代码」的硬顺序"


def test_migrator_has_five_modes_and_docs_mention_them():
    section = _v180_changelog_section()
    for flag in MIGRATOR_MODES:
        assert flag in MIGRATOR, f"migrate_config_v6.py 缺模式 {flag}"
        assert flag in README, f"README 缺迁移器模式 {flag}"
        assert flag in section, f"CHANGELOG v1.8.0 节缺迁移器模式 {flag}"


def test_docs_require_config_before_code_and_paired_rollback():
    """v5 → v6 的老路径仍然是"先迁配置、成对回滚"（只回其一会永久丢设置）。"""
    current = _readme_current_section()
    for name, text in (("README", current), ("CHANGELOG", _v180_changelog_section())):
        assert "先迁配置" in text, f"{name} 没写「先迁配置」"
        assert "成对" in text, f"{name} 没写「成对回滚」"
        assert "永久丢失" in text or "不可恢复" in text, f"{name} 没写「只回其一会丢设置」"
    # 反过来：v1.7.5 → v1.8.0 必须写清"只有一条腿 / 零迁移"
    assert "零新增叶" in current, "README 必须写清本版零新增叶"
    assert "无迁移" in current or "无需迁移" in current, "README 必须写清本版无需迁移"


# ---------------------------------------------------------- 4. 任务簿与投递语义


def test_readme_taskbook_states_match_code():
    """README 列出的状态词必须与 `core/tasks.STATES` 双向一致（7 态）。"""
    current = _readme_current_section()
    for state in tasks.STATES:
        assert f"`{state}`" in current, f"README 现行部分缺任务态 {state}"
    assert len(tasks.STATES) == 7 and len(set(tasks.STATES)) == 7
    assert "恰好一条终态" in current, "README 必须写「每任务恰好一条终态」"
    assert "排队" in current, "README 必须写满载改为排队（行为变化）"


def test_readme_taskbook_semantics_are_documented():
    """状态查询不含正文 / 不给完成时间；回看 5 条 / 30 分钟 / 不跨会话。"""
    current = _readme_current_section()
    for needle in ("不含正文", "不给完成时间", "5 条", "30 分钟", "不跨会话", "只在内存"):
        assert needle in current, f"README 现行部分缺任务簿边界「{needle}」"
    # 常量必须与代码一致（写死数字会漂）
    assert f"{tasks.RECALL_LIMIT} 条" in current
    assert f"{tasks.RECALL_WINDOW_MIN} 分钟" in current


def test_readme_delivery_implementation_and_boundaries():
    """投递：单通道 + 只重放两个插件钩子 + 不重试不补发 + 内容安全 stage 不跑。"""
    current = _readme_current_section()
    assert "context.send_message" in current, "README 必须写清单通道是 context.send_message"
    assert "on_decorating_result" in current and "on_after_message_sent" in current, (
        "README 必须点名只重放这两个插件钩子")
    assert "不重试" in current and "不补发" in current, "README 必须写「不重试、不补发」"
    assert "内容安全 stage 不重放" in current, "README 必须写「内容安全 stage 不重放」（L3 不交付）"
    assert "L3" in current, "README 必须写清分级（L1 / L2 子集 / 不交付 L3）"
    assert "PlatformNotFound" in current, "README 必须写平台找不到时的记账名"


# ------------------------------------------------------------ 5. CHANGELOG 行为面


def test_changelog_v180_section_covers_the_release_facts():
    """A1 根因 / 两个作废版本 / 零配置变更 / 7 态 / 外层 3 → 5 / 门禁实数。"""
    section = _v180_changelog_section()
    # A1 根因：模型把 action 写成 JSON 字符串 ⇒ 8/8 全拒 R-A1
    assert "R-A1" in section and "JSON 字符串" in section, "CHANGELOG 必须写清 A1 根因"
    assert "八次" in section or "8/8" in section, "CHANGELOG 必须写清生产 8/8 全拒"
    assert "coerce_payload" in section, "CHANGELOG 必须点名边缘容错函数"
    assert "一字未放宽" in section or "仍严格" in section, "CHANGELOG 必须写清闸门未放宽"
    # 作废版本
    assert "v1.7.6" in section and "v1.7.7" in section, "CHANGELOG 必须点名作废的两个版本"
    assert "不合并" in section and "不部署" in section and "不打 tag" in section, (
        "CHANGELOG 必须写清作废版本的不合并/不部署/不打 tag")
    # 配置与行为
    assert "零新增叶" in section and "无迁移" in section, "CHANGELOG 必须写清 v6 不变、无迁移"
    assert "7 态" in section or "7 态任务簿" in section, "CHANGELOG 必须写 7 态任务簿"
    assert "3 → 5" in section, "CHANGELOG 必须写外层工具面 3 → 5"
    # 门禁实数（现读现比，不写死历史值）
    passed = re.findall(r"(\d+) passed", section)
    assert passed, "CHANGELOG v1.8.0 节必须写本轮全量用例实数（形如 `NNN passed`）"
    assert int(passed[0]) >= 800, f"门禁条数看起来是旧的：{passed[0]}"


def test_changelog_v180_section_has_no_stale_gate_numbers():
    """v1.8.0 节不许留上一轮的 786/785 与上一轮的测试文件数。"""
    section = _v180_changelog_section()
    for stale in ("786 passed", "785 passed", "781 passed", "32 个 `test_*.py`"):
        assert stale not in section, f"CHANGELOG v1.8.0 节仍有旧数字「{stale}」"


def test_changelog_keeps_the_v175_section_intact():
    """历史节必须还在（抬版不是删史）；v1.7.5 的行为变化文案仍可查。"""
    section = _v175_changelog_section()
    assert "all × core" in section and "all × plugin" in section
    assert "插件模板生成的一条终态消息" in section


# --------------------------------------------------- 6. 不许回流的旧口径（README）


def test_readme_current_part_has_no_deleted_modules_or_legacy_numbers():
    """现行部分不得把已删模块 / 已删键 / 旧数字当现状（历史章节里允许）。"""
    current = _readme_current_section()
    forbidden = (
        "core/background.py",
        "search.mode",
        "browser.default_url",
        "is_background_task",
        "1128",
        "786 passed",
        "配置叶 54 条",
        "migrate_config_v3.py",
        "migrate_config_v4.py",
    )
    for needle in forbidden:
        for line in current.splitlines():
            if needle not in line:
                continue
            assert any(word in line for word in ("已删", "删除", "退役", "不再", "已取消")), (
                f"README 现行部分把已删的 {needle} 当现状写：{line.strip()[:120]}")


def test_readme_module_list_points_at_real_files():
    """README 现行部分提到的每个 `core/*.py` / `tools/*.py` 都必须真实存在。"""
    current = _readme_current_section()
    mentioned = set(re.findall(r"(?:core|tools)/[a-z_0-9]+\.py", current))
    assert mentioned, "README 现行部分没有模块清单"
    for module in sorted(mentioned):
        assert (ROOT / module).exists(), f"README 指向不存在的模块 {module}"
    for module in ("core/tasks.py", "core/proactive.py", "core/config_map.py",
                   "core/a11y.py", "core/action_model.py", "core/executor.py", "core/verify.py"):
        assert module in mentioned, f"README 现行部分缺模块 {module}"


def test_readme_test_file_count_matches_disk():
    """README 的测试文件数必须等于磁盘实数（这类漂移没人会自然发现）。"""
    current = _readme_current_section()
    on_disk = len(list((ROOT / "tests").glob("test_*.py")))
    stated = re.findall(r"(\d+) 个 `test_\*\.py`", current)
    assert stated, "README 现行部分必须写「NN 个 `test_*.py`」"
    assert {int(n) for n in stated} == {on_disk}, (
        f"README 写 {stated}，磁盘实际 {on_disk} 个 test_*.py")


def test_readme_action_list_is_generated_from_action_spec():
    """README 的 16 动作清单必须是 `ACTION_SPEC` 的镜像（手抄必错）。"""
    from astrbot_plugin_browser_llm.core import action_model

    current = _readme_current_section()
    names = [spec.name.lower() for spec in action_model.ACTION_SPEC]
    assert len(names) == 16, names
    for name in names:
        assert f"`{name}`" in current, f"README 现行部分缺动作 {name}"
    para_start = current.index("16 个动作")
    para = current[para_start : para_start + 500]
    for stale in ("`uncheck`", "`wait_for`", "`links`", "`media`", "`hover``"):
        assert stale not in para, f"README 的动作清单残留了 {stale}"


def test_readme_observe_views_and_act_prefix_match_code():
    """`browse_observe` 视图名与 `browse_act` 前缀必须与代码一致。"""
    current = _readme_current_section()
    for view in main_module.OBSERVE_VIEWS:
        assert f"`{view}`" in current, f"README 缺 observe 视图 {view}"
    assert "`compact`" not in current, "`compact` 不是合法视图"
    assert "【动作】" in current, "README 必须写对 browse_act 的前缀"
    assert "【执行】" not in current, "【执行】是旧前缀，代码已改成【动作】"


def test_platform_facts_are_documented():
    """两条最容易踩的平台事实必须写进 README（否则下一个人照样踩）。

    ① 核心 `register_llm_tool` **只解析 docstring 的 `Args:` 块、不读函数签名**；
    ② 参数类型只能写 `number`（核心 `SUPPORTED_TYPES` 没有 `integer`，写了注册即抛）。
    """
    current = _readme_current_section()
    assert "Args" in current and "docstring" in current, "README 必须写「只解析 docstring 的 Args 段」"
    assert "register_llm_tool" in current, "README 必须点名核心 API"
    assert "number" in current and "integer" in current, "README 必须写清 number vs integer"
    assert "ValueError" in current, "README 必须写清写 integer 会抛"


def test_readme_history_section_is_marked_and_reachable():
    history = _readme_history_section()
    assert "v1.7.5 及更早" in history
    assert "一律以前者为准" in history or "以其为准" in history
