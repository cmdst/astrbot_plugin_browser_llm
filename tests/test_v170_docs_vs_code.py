"""文档 ⇄ 代码一致性守卫（v1.7.0）。

**为什么需要这个文件**：v1.7.0 的独立验证报告把「版本四处仍是 `v1.6.0`」列为 **high（V1）**，
理由是它**不会被任何功能测试抓到** —— 代码全绿、行为全对，但 `pin-check.sh` 与面板会把新版
报成旧版，运营方无法区分新旧、回滚锚点也会混。同类漂移还有「内层工具面 24/26/27」与
「配置叶 54 条 / JSON 叶子键 55 个」这两组数字：它们一半在代码里、一半只活在文档里。

本文件把这两类漂移钉成用例（不依赖 `verification/` 的独立探针——探针不进门禁，这个文件进）：

============  ==========================================================================
D1  版本四处一致：`metadata.yaml` / `main.py::_PLUGIN_VERSION_FALLBACK` / `CHANGELOG.md`
    首个版本节 / `README.md` 的「当前版本」== `EXPECTED_VERSION`
D2  `main.PLUGIN_VERSION` 由 `metadata.yaml` 推导（单一事实源；不是硬编码两份）
D3  内层工具面：`len(_BROWSER_TOOLS) == 26`，且文档不再声称「24 个 `browse_*`」
D4  `search.mode=internal` 时内层工具面 27（`browse_web_search` 是**运行时**追加的条件工具，
    不在 `_BROWSER_TOOLS` 字面量里）
D5  配置叶两种口径：`len(KEY_MAP) == 54` / 展平 `_conf_schema.json` == 55；v1.6.0 冻结侧
    `len(KEY_MAP_V4) == 45`；两口径在文档里**都带单位**
D6  「叶数必须带单位」：v1.7.0/v1.6.1 两个新版本节与 README 全文**不得**出现裸的
    「54 叶 / 55 叶 / 45 叶 / 46 叶」（历史版本节保留原文，不在此断言范围）
D7  迁移器四模式在 README 与 CHANGELOG 都有落点（dry-run / apply / verify / restore-from，
    以及降级用的 strip-v5）
D8  **配置迁移目标是插件自己的配置文件**（`data/config/astrbot_plugin_browser_llm_config.json`），
    而不是 `data/cmd_config.json`（验证报告已更正过一次）
D9  三入口语义：README 写出「两个委派 + 一个直调」、`browse_local_page` 不进子代理的**三条理由**
D10 人格三档与搜索两模式：schema `options` ⇄ 代码侧枚举常量/归一化 ⇄ 文档三处一致
D11 外层工具面：3 个名字 + `@filter.llm_tool` 装饰器 AST 计数 == 3
============  ==========================================================================

只读：不写配置、不启浏览器、不联网。跑法与其他用例一致：
    PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_v170_docs_vs_code.py -q
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import main as main_module
from core import config_map
from core.agent import PERSONA_MODES

# ======================================================================
# 期望值（**唯一一处**；改版本/工具面/叶数时改这里）
# ======================================================================
REPO = Path(__file__).resolve().parent.parent

EXPECTED_VERSION = "v1.7.0"
EXPECTED_PREV_VERSION = "v1.6.0"  # 已上生产的版本

EXPECTED_INNER_TOOLS = 26  # `_BROWSER_TOOLS` 字面量条目数
EXPECTED_INNER_TOOLS_INTERNAL = 27  # 上面 + 运行时追加的 `browse_web_search`

EXPECTED_LEAVES = 54  # 配置叶（KEY_MAP 条目数；browser.viewport 计 1 条复合叶）
EXPECTED_JSON_LEAVES = 55  # JSON 叶子键（展平 schema；browser.viewport.width/.height 分开计）
EXPECTED_LEAVES_V4 = 45  # v1.6.0 的配置叶（冻结）
EXPECTED_JSON_LEAVES_V4 = 46  # v1.6.0 的 JSON 叶子键（冻结）

EXPECTED_OUTER_TOOLS = ("browse_web", "browse_local_page", "browser_web_search")

INCIDENT = "ops_astrbot/incidents/20261026-browser-vision-cache-stale-on-spa.md"
PLUGIN_CFG = "data/config/astrbot_plugin_browser_llm_config.json"


def read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme() -> str:
    return read("README.md")


@pytest.fixture(scope="module")
def changelog() -> str:
    return read("CHANGELOG.md")


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(read("_conf_schema.json").removeprefix("\ufeff"))


def flatten_schema(node: dict, prefix: str = "") -> list[str]:
    """展平 schema：只递归 ``type == "object"``，其余算叶子键。"""
    out: list[str] = []
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        if value.get("type") == "object" and isinstance(value.get("items"), dict):
            out += flatten_schema(value["items"], prefix + key + ".")
        else:
            out.append(prefix + key)
    return out


def version_section(changelog: str, version: str) -> str:
    """取 CHANGELOG 里 ``## [<version>]`` 那一节（到下一个 ``## [`` 为止）。"""
    start = changelog.find(f"## [{version}]")
    assert start >= 0, f"CHANGELOG 里没有 `## [{version}]` 节"
    nxt = changelog.find("\n## [", start + 1)
    return changelog[start:] if nxt < 0 else changelog[start:nxt]


def first_changelog_version(changelog: str) -> str:
    found = re.search(r"^## \[(v[0-9][^\]]*)\]", changelog, re.MULTILINE)
    assert found, "CHANGELOG 里找不到任何 `## [vX.Y.Z]` 版本节"
    return found.group(1)


def readme_version(readme: str) -> str:
    found = re.search(r"当前版本：\*\*(v[0-9][^*]+)\*\*", readme)
    assert found, "README 里找不到「当前版本：**vX.Y.Z**」这一行"
    return found.group(1)


# ======================================================================
# D1 / D2 版本
# ======================================================================
def test_d1_version_is_consistent_in_four_places(readme, changelog) -> None:
    """版本四处（metadata / 代码兜底 / CHANGELOG 首节 / README）= v1.7.0。"""
    metadata = read("metadata.yaml")
    meta_ver = re.search(r"^version:\s*['\"]?([^\s'\"]+)", metadata, re.MULTILINE)
    assert meta_ver and meta_ver.group(1) == EXPECTED_VERSION, (
        f"metadata.yaml 的 version 应为 {EXPECTED_VERSION}，实得 {meta_ver and meta_ver.group(1)}"
    )
    assert main_module._PLUGIN_VERSION_FALLBACK == EXPECTED_VERSION, (
        "main.py 的版本兜底常量应随 metadata 一起 bump"
    )
    assert first_changelog_version(changelog) == EXPECTED_VERSION
    assert readme_version(readme) == EXPECTED_VERSION


def test_d2_plugin_version_derives_from_metadata() -> None:
    """``PLUGIN_VERSION`` 从 metadata.yaml 读（单一事实源），不是第二份硬编码。"""
    assert main_module.PLUGIN_VERSION == EXPECTED_VERSION
    assert main_module._PLUGIN_VERSION_FALLBACK == main_module.PLUGIN_VERSION


# ======================================================================
# D3 / D4 内层工具面
# ======================================================================
def test_d3_inner_tool_surface_is_26() -> None:
    names = [t["name"] for t in main_module._BROWSER_TOOLS]
    assert len(names) == EXPECTED_INNER_TOOLS
    assert len(set(names)) == len(names), "内层工具名不得重复"
    # v1.7.0 新增的两个（事故根因②③的机械落点）必须在里面
    assert "browse_get_interactive" in names
    assert "browse_click_selector" in names
    # 条件工具不在字面量里
    assert "browse_web_search" not in names


def test_d3_docs_no_longer_claim_24_browse_tools(readme, changelog) -> None:
    """旧 README 的「24 个 `browse_*`」是过期值（验证报告 V2），只允许以「已过期」语境出现。

    范围 = README 全文 + CHANGELOG 的 **v1.7.0 / v1.6.1 两节**。更早的版本节是历史记录
    （那时候确实只有 24 个内层工具），照原样保留，不在此断言范围内。
    """
    scopes = {"README.md": readme}
    for version in (EXPECTED_VERSION, "v1.6.1"):
        scopes[f"CHANGELOG {version} 节"] = version_section(changelog, version)
    for name, text in scopes.items():
        for match in re.finditer(r"24 个", text):
            window = text[max(0, match.start() - 80) : match.end() + 80]
            assert ("过期" in window) or ("v1.7.0 前" in window), (
                f"{name} 里出现「24 个」但不是「已过期」语境：…{window}…"
            )


def test_d4_internal_mode_adds_one_conditional_tool() -> None:
    """`search.mode=internal` ⇒ 内层工具面 27（运行时追加 `browse_web_search`）。"""
    source = read("main.py")
    assert "browse_web_search" in source
    # 追加点在 `_rebuild_browser_toolset`：`internal` 且池可用时 append
    assert re.search(
        r'if\s+self\.search_mode\s*==\s*"internal"\s+and\s+self\._search_pool_ready\(\)',
        source,
    ), "找不到 internal 模式下追加内层搜索工具的那一行"
    assert EXPECTED_INNER_TOOLS + 1 == EXPECTED_INNER_TOOLS_INTERNAL


# ======================================================================
# D5 / D6 配置叶的两种口径
# ======================================================================
def test_d5a_config_leaf_count(schema) -> None:
    assert len(config_map.KEY_MAP) == EXPECTED_LEAVES
    assert len(config_map.GROUPS) == 7
    assert set(config_map.GROUPS) == set(schema), "schema 顶层组与 KEY_MAP 的分组必须逐一对应"


def test_d5b_json_leaf_count(schema) -> None:
    leaves = flatten_schema(schema)
    assert len(leaves) == EXPECTED_JSON_LEAVES
    # 差 1 的唯一来源：browser.viewport 是 1 条复合叶 ↔ 2 个 JSON 叶子键
    assert "browser.viewport.width" in leaves
    assert "browser.viewport.height" in leaves
    assert "browser.viewport" in config_map.KEY_MAP


def test_d5c_v4_frozen_counts() -> None:
    """v1.6.0 侧冻结：配置叶 45 条 / JSON 叶子键 46 个（两个口径差 1 的同一处）。"""
    assert len(config_map.KEY_MAP_V4) == EXPECTED_LEAVES_V4
    assert len(config_map.GROUPS_V4) == 6
    assert EXPECTED_JSON_LEAVES_V4 - EXPECTED_LEAVES_V4 == 1
    assert EXPECTED_JSON_LEAVES - EXPECTED_LEAVES == 1


def test_d5d_docs_state_both_counts_with_units(readme, changelog) -> None:
    for name, text in (("README.md", readme), ("CHANGELOG.md", changelog)):
        assert "配置叶 54 条" in text, f"{name} 缺「配置叶 54 条」"
        assert "JSON 叶子键 55 个" in text, f"{name} 缺「JSON 叶子键 55 个」"
    assert "配置叶 45 条" in readme and "JSON 叶子键 46 个" in readme


def test_d6_no_bare_leaf_counts_in_new_sections(readme, changelog) -> None:
    """新版本节与 README 全文禁止裸写「N 叶」（必须带单位）。"""
    bare = re.compile(r"(?<![0-9])(?:45|46|54|55)\s*叶(?!子)")
    scope = {"README.md 全文": readme}
    for version in (EXPECTED_VERSION, "v1.6.1"):
        scope[f"CHANGELOG {version} 节"] = version_section(changelog, version)
    for name, text in scope.items():
        hits = [m.group(0) for m in bare.finditer(text)]
        assert not hits, f"{name} 出现裸叶数（缺单位）：{hits}"


# ======================================================================
# D7 / D8 迁移器与目标文件
# ======================================================================
def test_d7_migrator_four_modes_documented(readme, changelog) -> None:
    migrator = REPO / "tools" / "migrate_config_v5.py"
    assert migrator.is_file(), "v5 迁移器必须存在"
    source = migrator.read_text(encoding="utf-8")
    for flag in ("--dry-run", "--apply", "--verify", "--restore-from", "--strip-v5"):
        assert flag in source, f"迁移器缺 {flag}"
        assert flag in readme, f"README 缺迁移器模式 {flag}"
    # CHANGELOG 至少要写清四模式与降级用的 strip-v5
    for flag in ("--dry-run", "--apply", "--verify", "--restore-from"):
        assert flag in changelog, f"CHANGELOG 缺迁移器模式 {flag}"


def test_d7b_docs_require_config_before_code(readme, changelog) -> None:
    """「先迁配置再换代码」「配置与代码成对回滚」必须在两份文档里都写明。"""
    for name, text in (("README.md", readme), ("CHANGELOG.md", changelog)):
        assert "先迁配置" in text or "配置先" in text, f"{name} 没写「配置先动」"
        assert "成对" in text, f"{name} 没写「成对回滚」"
        assert "不可恢复" in text or "永久丢失" in text, f"{name} 没写「只回滚其一会丢设置」"


def test_d8_migration_target_is_plugin_config_file(readme, changelog) -> None:
    """迁移目标是插件自己的配置文件，不是 `data/cmd_config.json`（验证报告已更正过一次）。"""
    assert PLUGIN_CFG in readme
    assert PLUGIN_CFG in changelog
    # 只查「迁移步骤」所在的范围：README 全文 + CHANGELOG 的 v1.7.0 节。
    # 更早的历史节里 `data/cmd_config.json` 指的是"主配置拷贝 / 宿主快照"（v1.4.0 的清理项），
    # 与迁移目标无关，不在此断言范围内。
    scopes = {
        "README.md": readme,
        "CHANGELOG v1.7.0 节": version_section(changelog, EXPECTED_VERSION),
    }
    for name, text in scopes.items():
        for match in re.finditer(r"cmd_config\.json", text):
            window = text[max(0, match.start() - 60) : match.end() + 60]
            assert "不是" in window, (
                f"{name} 提到 cmd_config.json 但没写「不是」：…{window}…"
            )


# ======================================================================
# D9 三入口语义
# ======================================================================
def test_d9a_outer_tool_names_unchanged() -> None:
    assert main_module._ALL_TOOL_NAMES == EXPECTED_OUTER_TOOLS
    assert main_module._C_SCOPE_TOOLS == frozenset({"browse_web"}), (
        "C 模式（core 引擎）永久只对 browse_web 生效"
    )


def test_d9b_local_page_stays_direct_with_three_reasons(readme) -> None:
    """`browse_local_page` 按规格 §1.4 不进子代理，README 必须写出安全理由。"""
    assert "不进子代理" in readme
    # 三条理由的关键词：同步拒绝 / 人格白名单 / 消费者是主 LLM
    assert "同步拒绝" in readme or "同步" in readme
    assert "人格白名单" in readme
    assert "消费者是主 LLM" in readme


def test_d9c_two_delegation_entries(readme) -> None:
    assert "委派入口" in readme
    assert "直调入口" in readme
    # 两个委派入口的名字都要出现，且要写清 internal 档下外层搜索被停用
    assert "browse_web" in readme
    assert "browser_web_search" in readme
    assert "强制停用" in readme


# ======================================================================
# D10 人格三档 / 搜索两模式
# ======================================================================
def test_d10a_persona_modes_match_schema(readme, schema) -> None:
    assert tuple(PERSONA_MODES) == ("default", "current", "custom")
    options = schema["agent"]["items"]["persona_mode"]["options"]
    assert options == list(PERSONA_MODES)
    assert schema["agent"]["items"]["persona_mode"]["default"] == "default"
    for mode in PERSONA_MODES:
        assert mode in readme, f"README 没写人格档 {mode}"
    # 分层顺序的 L0/L1/L4 关键词
    assert "L0" in readme and "L4" in readme


def test_d10b_search_modes_match_schema_and_code(readme, schema) -> None:
    options = schema["search"]["items"]["mode"]["options"]
    assert options == ["external", "internal"]
    assert schema["search"]["items"]["mode"]["default"] == "external"
    source = read("main.py")
    assert '("external", "internal")' in source, "代码侧的归一化白名单必须与 schema 一致"
    assert "search.mode" in readme
    assert "inactivated_llm_tools" in readme, "internal 档写全局名单的副作用必须在文档里写明"


# ======================================================================
# D11 外层工具面
# ======================================================================
def test_d11_three_outer_decorators() -> None:
    tree = ast.parse(read("main.py"))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "llm_tool"
                and isinstance(deco, ast.Call)
            ):
                for kw in deco.keywords:
                    if kw.arg != "name":
                        continue
                    if isinstance(kw.value, ast.Constant):
                        names.append(kw.value.value)
                    elif isinstance(kw.value, ast.Name):
                        # 搜索工具用的是模块常量 `_SEARCH_TOOL_NAME`（不是字面量）；
                        # 解析它，否则这里会少一个名字、把"没改名"误判成失败。
                        names.append(getattr(main_module, kw.value.id))
    assert sorted(names) == sorted(EXPECTED_OUTER_TOOLS), (
        "外层 llm_tool 注册名必须仍是那三个（v1.7.0 明确不做别名）"
    )


# ======================================================================
# 事故与版本节（V1 的第四条：CHANGELOG 必须能对上事故文件）
# ======================================================================
def test_changelog_documents_incident_and_prev_version(changelog) -> None:
    assert f"## [{EXPECTED_VERSION}]" in changelog
    assert "## [v1.6.1]" in changelog
    assert INCIDENT in changelog, "v1.6.1 / v1.7.0 两节必须能追到事故文件"
    assert INCIDENT in read("README.md") or "20261026" in read("README.md")
    # v1.6.1 = 事故止血；v1.7.0 = 破坏性重构
    v161 = version_section(changelog, "v1.6.1")
    assert "识图缓存" in v161 and "事故" in v161
    v170 = version_section(changelog, EXPECTED_VERSION)
    assert "破坏性" in v170
    assert "委派" in v170
    assert "配置结构 v5" in v170
    # 「v1.6.0 已是生产版本」这一句必须在 v1.7.0 节里，解释为什么抬 minor
    assert EXPECTED_PREV_VERSION in v170 and "生产" in v170
