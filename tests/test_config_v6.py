"""v6 配置结构的守卫（v1.7.5）：schema ⇄ ``core.config_map`` ⇄ 规格键表三方一致。

本文件钉的是**结构**，不测迁移行为（迁移器在 ``tests/test_migrate_config_v6.py``）。
三条主线，对应规格 §7.9 的判据 1–4：

1. **不漏叶**：``_conf_schema.json`` 展平后的叶子键 ⇄ ``core.config_map.KEY_MAP``
   ⇄ ``spec/v175-config-keys.json`` 三方向逐键相等（两个方向都比，缺一个就红）；
2. **口径**：**配置叶 57 条** = **JSON 叶子键 59 个**（差 2 = ``resources.viewport``
   1↔3），且 v5/v4/v3 的冻结视图不跟着漂移（54 / 45 / 25）；
3. **用户点名的十项**逐条落地：模型合并、页面感知重做、资源与限制重做、搜索引擎精简、
   起始页移除、代理统一（搜索例外）、分辨率统一、总超时统一、取消步数上限、
   搜索工具精简。

``spec/v175-config-keys.json`` 属于 dev lane 的 ``spec/``（不随插件库分发），
因此本文件同时内嵌一份**冻结路径清单**（``EXPECTED_PATHS``）：没有规格文件时结构
守卫照样跑，有规格文件时额外逐叶比 ``type/default/choices/clamp/effect``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.config_map import (
    BACK_TRACE,
    CARRIED_PATHS,
    CONFIG_V5_NEW_PATHS,
    CONFIG_V6_NEW_PATHS,
    EFFECT_HOT,
    EFFECT_IMPORT,
    EFFECT_LABELS,
    EFFECT_NEW_CONTEXT,
    EFFECT_TIMING,
    FLAT_KEYS,
    GROUPS,
    GROUPS_V4,
    GROUPS_V5,
    JSON_LEAF_KEYS,
    KEY_MAP,
    KEY_MAP_V4,
    KEY_MAP_V5,
    LEAF_DEFAULTS,
    LEAF_ORDER,
    LEAF_PATHS,
    LEAF_TYPES,
    LEAVES,
    LEGACY_PATHS,
    MIGRATION_DROP,
    MIGRATION_KEEP,
    MIGRATION_MERGE,
    MIGRATION_MOVE,
    MIGRATION_NEW,
    MIGRATION_RENAME,
    NO_FLAT_PATHS,
    V4_LEAF_PATHS,
    V5_DISPOSITION,
    V5_DISPOSITION_COUNTS,
    V5_DROPPED_PATHS,
    V5_LEAF_PATHS,
    V5_SOURCE_TO_V6,
    V6_MERGED_PATHS,
    ConfigView,
    choices_of,
    clamp_of,
    default_of,
    effect_of,
    leaf_meta,
    migration_of,
    schema_leaf,
    v5_disposition,
    v6_path_for_v5,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA: dict = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))

#: v6 的 57 条路径（**冻结**，顺序 = schema 声明顺序）。这是规格键表的镜像，
#: 内嵌在这里是为了"没有 spec/ 目录时结构守卫照样跑"。
EXPECTED_PATHS: tuple[str, ...] = (
    "resources.engine",
    "resources.headless",
    "resources.viewport",
    "resources.proxy",
    "resources.max_tabs",
    "resources.idle_timeout_s",
    "resources.cache_days",
    "resources.max_text_chars",
    "resources.max_links",
    "resources.max_parallel",
    "resources.total_budget_s",
    "resources.goto_timeout_s",
    "resources.action_timeout_s",
    "perception.a11y_max_nodes",
    "perception.a11y_max_name_chars",
    "perception.screenshot_fallback",
    "perception.som_max_marks",
    "perception.screenshot_to_chat",
    "perception.rules",
    "model.provider_id",
    "model.persona_mode",
    "model.persona_prompt",
    "model.image_max_side",
    "model.image_quality",
    "model.result_max_chars",
    "model.ledger_max_lines",
    "actions.trusted_events",
    "actions.verify",
    "actions.max_retries",
    "actions.repeat_guard",
    "search.enabled",
    "search.engine",
    "search.providers",
    "search.proxy",
    "search.max_parallel",
    "search.max_results",
    "search.max_rounds",
    "search.provider_timeout_s",
    "search.total_timeout_s",
    "search.cooldown_s",
    "search.cache_ttl_s",
    "search.fetch_pages",
    "search.fetch_top_n",
    "search.fetch_timeout_s",
    "search.fetch_max_chars",
    "search.region",
    "search.safesearch",
    "security.banned_words",
    "security.enable_banned_filter",
    "security.block_internal_ip",
    "security.session_whitelist",
    "security.session_blacklist",
    "tools.background_mode",
    "tools.background_max_chars",
    "tools.dedupe_window_s",
    "tools.isolate_context",
    "tools.narrow_tools",
)

#: 规格键表的候选位置（dev lane 的 spec/ 与插件库自带的 spec/）。
_SPEC_CANDIDATES: tuple[Path, ...] = (
    ROOT / "spec" / "v175-config-keys.json",
    Path(
        os.environ.get(
            "V175_CONFIG_KEYS",
            "/root/workspace/astrbot/dev_astrbot_plugins/astrbot_plugin_browser_llm"
            "/spec/v175-config-keys.json",
        )
    ),
)


def schema_leaf_keys() -> list[str]:
    """展平 ``_conf_schema.json``：只递归 ``type == "object"`` 的 ``items``。"""
    out: list[str] = []

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            if not isinstance(value, dict):
                continue
            if value.get("type") == "object" and isinstance(value.get("items"), dict):
                walk(value["items"], prefix + key + ".")
            else:
                out.append(prefix + key)

    walk(SCHEMA)
    return out


def spec_payload() -> dict | None:
    """读规格键表；找不到返回 ``None``（此时只跑内嵌冻结清单的守卫）。"""
    for candidate in _SPEC_CANDIDATES:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def schema_default(group: str, leaf: str, definition: dict):
    """schema 叶的默认值；``object`` 叶取各子项默认值的组合（核心就是这么递归的）。"""
    if "default" in definition:
        return definition["default"]
    assert definition["type"] == "object", (group, leaf)
    return {key: sub["default"] for key, sub in definition["items"].items()}


# ---------------------------------------------------------------------------
# 线 1：schema ⇄ KEY_MAP ⇄ 规格键表
# ---------------------------------------------------------------------------


def test_schema_leaf_keys_equal_frozen_contract():
    """schema 的**配置叶**（object 叶按 1 条计）== 内嵌冻结清单（**两个方向**都比）。"""
    config_leaves = [
        f"{group}.{leaf}" for group, meta in SCHEMA.items() for leaf in meta["items"]
    ]
    assert len(config_leaves) == 57, f"配置叶应为 57 条，实际 {len(config_leaves)}"
    assert set(config_leaves) - set(EXPECTED_PATHS) == set(), "schema 多出未登记的配置叶"
    assert set(EXPECTED_PATHS) - set(config_leaves) == set(), "schema 缺少规格要求的配置叶"
    # 展平口径（JSON 叶子键）是另一件事：60 个，多出来的 2 个全在 resources.viewport
    flattened = schema_leaf_keys()
    assert len(flattened) == 59
    assert set(flattened) - set(config_leaves) == {
        "resources.viewport.width",
        "resources.viewport.height",
        "resources.viewport.device_scale_factor",
    }


def test_key_map_matches_schema_bijective():
    """``KEY_MAP`` ⇄ schema 叶路径一一对应（无多余、无缺失）。"""
    schema_paths = []
    for group, meta in SCHEMA.items():
        assert meta["type"] == "object", group
        for leaf in meta["items"]:
            schema_paths.append(f"{group}.{leaf}")
    assert len(schema_paths) == 57, "配置叶应为 57 条（object 叶按 1 条计）"
    assert set(KEY_MAP) - set(schema_paths) == set(), "KEY_MAP 多出 schema 里没有的路径"
    assert set(schema_paths) - set(KEY_MAP) == set(), "KEY_MAP 缺少 schema 里的路径"
    assert list(KEY_MAP) == schema_paths, "KEY_MAP 顺序必须与 schema 声明顺序一致"
    assert tuple(KEY_MAP) == EXPECTED_PATHS


def test_two_counting_conventions():
    """配置叶 57 条 = JSON 叶子键 59 个；差 2 全部来自 resources.viewport。"""
    assert len(KEY_MAP) == 57
    assert len(schema_leaf_keys()) == 59
    assert len(JSON_LEAF_KEYS) == 59
    assert set(JSON_LEAF_KEYS) == set(schema_leaf_keys())
    assert 60 - 58 == 2
    viewport = leaf_meta("resources.viewport")
    assert viewport is not None
    assert viewport.type == "object"
    assert viewport.children == ("width", "height", "device_scale_factor")
    assert viewport.json_leaf_keys == (
        "resources.viewport.width",
        "resources.viewport.height",
        "resources.viewport.device_scale_factor",
    )
    # 其余 57 条叶都是 1:1（所以差额恒为 2）
    assert sum(1 for leaf in LEAVES if leaf.children) == 1


def test_key_map_matches_spec_json_per_leaf():
    """逐叶比规格键表的 ``type/default/choices/clamp/effect``（规格与代码钉在一起）。"""
    spec = spec_payload()
    if spec is None:
        pytest.skip("spec/v175-config-keys.json 不在（dev lane 的 spec/ 未挂载）")
    spec_leaves = spec["leaves"]
    assert [item["path"] for item in spec_leaves] == list(LEAF_PATHS)
    for item in spec_leaves:
        path = item["path"]
        meta = leaf_meta(path)
        assert meta is not None, path
        assert meta.type == item["type"], f"{path} 类型不一致"
        assert meta.default == item["default"], f"{path} 默认值不一致"
        assert list(meta.choices or []) == list(item["choices"] or []), f"{path} 枚举不一致"
        assert list(meta.clamp or []) == list(item["clamp"] or []), f"{path} 钳制区间不一致"
        assert meta.effect == item["effect"], f"{path} 生效时机不一致"
        assert meta.json_leaf_keys == tuple(item["json_leaf_keys"]), f"{path} JSON 叶子键不一致"


def test_spec_disposition_matches_code():
    """v5 54 键的处置（五桶 + 目标路径）与规格键表逐条一致。"""
    spec = spec_payload()
    if spec is None:
        pytest.skip("spec/v175-config-keys.json 不在")
    buckets = spec["v5_key_disposition"]
    expected: dict[str, tuple[str, str | None]] = {}
    for action in ("keep", "rename", "move", "merge", "drop"):
        for item in buckets[action]:
            expected[item["v5_path"]] = (action, item["v6_path"])
    assert expected == V5_DISPOSITION
    assert spec["v5_key_disposition"]["total"] == 54


def test_schema_group_order_and_leaf_order():
    """组顺序与组内叶顺序都必须是 schema 声明顺序（迁移器按它写盘）。"""
    assert GROUPS == (
        "resources",
        "perception",
        "model",
        "actions",
        "search",
        "security",
        "tools",
    )
    assert tuple(SCHEMA) == GROUPS
    assert len(GROUPS) == 7
    for group in GROUPS:
        assert LEAF_ORDER[group] == tuple(SCHEMA[group]["items"]), group
        assert all(f"{group}.{leaf}" in KEY_MAP for leaf in LEAF_ORDER[group])


# ---------------------------------------------------------------------------
# 线 2：冻结的旧视图（v5 / v4 / v3）不跟着 v6 漂移
# ---------------------------------------------------------------------------


def test_frozen_v5_view():
    assert len(V5_LEAF_PATHS) == 54
    assert len(KEY_MAP_V5) == 54
    assert GROUPS_V5 == ("browser", "limits", "security", "perception", "search", "tools", "agent")
    assert len(CONFIG_V5_NEW_PATHS) == 9
    assert set(CONFIG_V5_NEW_PATHS) == {
        "search.mode",
        "agent.provider_id",
        "agent.persona_mode",
        "agent.persona_prompt",
        "agent.task_timeout",
        "agent.image_max_side",
        "agent.image_quality",
        "agent.result_max_chars",
        "agent.ledger_max_lines",
    }
    # v5 的 7 组在 v6 里只剩 4 组同名（perception/security/search/tools）；
    # browser/limits 并进 resources，agent 并进 model
    assert set(GROUPS_V5) & set(GROUPS) == {"perception", "security", "search", "tools"}
    assert set(GROUPS_V5) - set(GROUPS) == {"browser", "limits", "agent"}
    assert set(GROUPS) - set(GROUPS_V5) == {"resources", "model", "actions"}


def test_frozen_v4_and_v3_views():
    assert len(V4_LEAF_PATHS) == 45
    assert len(KEY_MAP_V4) == 45
    assert GROUPS_V4 == ("browser", "limits", "security", "perception", "search", "tools")
    assert len(FLAT_KEYS) == 25
    assert len(LEGACY_PATHS) == 45
    assert KEY_MAP_V4 == {path: KEY_MAP_V5[path] for path in V4_LEAF_PATHS}


def test_v5_disposition_arithmetic():
    """54 = 26 keep + 12 rename + 1 move + 7 merge + 8 drop；一条不漏。"""
    assert V5_DISPOSITION_COUNTS == {
        MIGRATION_KEEP: 26,
        MIGRATION_RENAME: 12,
        MIGRATION_MOVE: 1,
        MIGRATION_MERGE: 7,
        MIGRATION_DROP: 8,
    }
    assert len(V5_DISPOSITION) == 54
    assert sum(V5_DISPOSITION_COUNTS.values()) == 54
    assert set(V5_DISPOSITION) == set(V5_LEAF_PATHS)
    assert len(V5_SOURCE_TO_V6) == 46  # 26 + 12 + 1 + 7
    assert len(V5_DROPPED_PATHS) == 8
    # v6 58 = 承接 43（46 个 v5 键落到 43 个叶）+ 新增 15
    assert len(CARRIED_PATHS) == 43
    assert len(CONFIG_V6_NEW_PATHS) == 14
    assert 43 + 14 == 57
    assert len(V6_MERGED_PATHS) == 4
    # 7 个 merge 源落到 4 个叶上
    merge_sources = [p for p, (a, _t) in V5_DISPOSITION.items() if a == MIGRATION_MERGE]
    assert len(merge_sources) == 7
    assert len({V5_DISPOSITION[p][1] for p in merge_sources}) == 4


def test_every_carried_leaf_has_a_v5_source():
    """43 条承接叶必须每条都有 v5 来源；15 条新增叶必须没有。"""
    for path in CARRIED_PATHS:
        assert BACK_TRACE[path] is not None, path
        assert migration_of(path) in {
            MIGRATION_KEEP,
            MIGRATION_RENAME,
            MIGRATION_MOVE,
            MIGRATION_MERGE,
        }, path
    for path in CONFIG_V6_NEW_PATHS:
        assert BACK_TRACE[path] is None, path
        assert migration_of(path) == MIGRATION_NEW, path


def test_dropped_v5_keys_have_no_successor():
    """8 个 drop 键：v6 里没有同名叶，也没有任何 v6 叶以它为来源。"""
    for path in V5_DROPPED_PATHS:
        assert v6_path_for_v5(path) is None, path
        assert path not in KEY_MAP, path
        assert v5_disposition(path) == (MIGRATION_DROP, None)
        group, leaf = path.split(".", 1)
        assert leaf not in LEAF_ORDER.get(group, ()), path
        assert path not in BACK_TRACE.values(), path


# ---------------------------------------------------------------------------
# 线 3：用户点名的十项配置处置（逐条可查）
# ---------------------------------------------------------------------------


def test_model_merged_into_single_group():
    """① 视觉模型与子代理模型合并：只有一组 model，只有一条 provider。"""
    assert "model" in GROUPS
    assert "agent" not in GROUPS
    assert "agent" not in tuple(SCHEMA)
    assert not [path for path in KEY_MAP if path.startswith("agent.")]
    assert "perception.vision_provider_id" not in KEY_MAP
    provider = leaf_meta("model.provider_id")
    assert provider is not None
    assert provider.merge_from == ("perception.vision_provider_id", "agent.provider_id")
    # 两套 prompt 也合一：只有一条人格提示词，caption 提示词已删
    assert "model.persona_prompt" in KEY_MAP
    assert "perception.vision_prompt" not in KEY_MAP
    assert "perception.vision_cache_ttl" not in KEY_MAP


def test_perception_group_redone():
    """② 页面感知重做：A11y 预算 + SoM + 截图兜底 + 会话规则。"""
    assert LEAF_ORDER["perception"] == (
        "a11y_max_nodes",
        "a11y_max_name_chars",
        "screenshot_fallback",
        "som_max_marks",
        "screenshot_to_chat",
        "rules",
    )
    for gone in (
        "perception.enable_screenshot",
        "perception.silent_mode",
        "perception.page_perception",
        "perception.perception_rules",
        "perception.vision_prompt",
        "perception.vision_cache_ttl",
        "perception.vision_provider_id",
    ):
        assert gone not in KEY_MAP, gone
    assert leaf_meta("perception.screenshot_fallback").type == "bool"
    assert leaf_meta("perception.screenshot_to_chat").default is False


def test_resources_group_redone():
    """③ 资源部署与限制重做：v5 的 browser + limits 合成 13 叶。"""
    assert LEAF_ORDER["resources"] == (
        "engine",
        "headless",
        "viewport",
        "proxy",
        "max_tabs",
        "idle_timeout_s",
        "cache_days",
        "max_text_chars",
        "max_links",
        "max_parallel",
        "total_budget_s",
        "goto_timeout_s",
        "action_timeout_s",
    )
    assert "browser" not in GROUPS
    assert "limits" not in GROUPS
    assert not [path for path in KEY_MAP if path.startswith(("browser.", "limits."))]


def test_search_engine_simplified():
    """④ 搜索引擎：从 browser 组移入 search，选项 4 → 3（B站 是站内搜索）。"""
    engine = leaf_meta("search.engine")
    assert engine is not None
    assert engine.choices == ("必应搜索", "百度搜索", "谷歌搜索")
    assert "B站搜索" not in (engine.choices or ())
    assert engine.default == "必应搜索"
    assert v5_disposition("browser.default_search_engine") == (
        MIGRATION_MOVE,
        "search.engine",
    )
    assert "search.mode" not in KEY_MAP


def test_start_page_removed():
    """⑤ 移除起始页：browser.default_url 删除，导航一律走 navigate 动作。"""
    assert "browser.default_url" in V5_DROPPED_PATHS
    assert "browser.default_url" not in KEY_MAP
    assert not [path for path in KEY_MAP if "default_url" in path]
    assert "default_url" not in json.dumps(SCHEMA, ensure_ascii=False)


def test_proxy_unified_with_search_exception():
    """⑥ 插件内代理统一为 resources.proxy；搜索工具单独保留 search.proxy。"""
    proxy_paths = sorted(path for path in KEY_MAP if path.endswith(".proxy"))
    assert proxy_paths == ["resources.proxy", "search.proxy"]
    assert v5_disposition("browser.proxy") == (MIGRATION_KEEP, "resources.proxy")
    assert v5_disposition("search.proxy") == (MIGRATION_KEEP, "search.proxy")


def test_resolution_unified():
    """⑦ 分辨率统一为单一 resolution 复合叶（宽/高/DPR）。"""
    viewport_paths = [path for path in KEY_MAP if "viewport" in path or "resolution" in path]
    assert viewport_paths == ["resources.viewport"]
    viewport = leaf_meta("resources.viewport")
    assert viewport.default == {"width": 1280, "height": 800, "device_scale_factor": 1}
    assert viewport.effect == EFFECT_IMPORT
    # 页面分辨率与「发给模型的图片最长边」是两件事，不能合并
    assert "model.image_max_side" in KEY_MAP


def test_total_timeout_unified():
    """⑧ 总操作超时统一：一个整任务预算 + 两级细化，v5 的四个超时键全部消失。"""
    assert "resources.total_budget_s" in KEY_MAP
    assert leaf_meta("resources.total_budget_s").clamp == (60, 1800)
    assert leaf_meta("resources.total_budget_s").merge_from == (
        "limits.agent_tool_timeout",
        "tools.background_timeout",
        "agent.task_timeout",
    )
    assert leaf_meta("resources.goto_timeout_s").clamp == (5, 120)
    assert leaf_meta("resources.action_timeout_s").clamp == (2, 60)
    for gone in (
        "browser.timeout",
        "limits.agent_tool_timeout",
        "agent.task_timeout",
        "tools.background_timeout",
    ):
        assert gone not in KEY_MAP, gone


def test_max_steps_removed():
    """⑨ 不再限制最大步数：agent_max_steps 删除，语义改为墙钟预算。"""
    assert "limits.agent_max_steps" in V5_DROPPED_PATHS
    assert not [path for path in KEY_MAP if "max_steps" in path]
    assert "max_steps" not in json.dumps(SCHEMA, ensure_ascii=False)
    # 完成判据只剩墙钟预算这一个键
    assert "resources.total_budget_s" in KEY_MAP


def test_search_tools_simplified():
    """⑩ 搜索工具精简：search.mode 删除（内外层不再互斥），引擎选项 4 → 3。"""
    assert "search.mode" in V5_DROPPED_PATHS
    assert "search.mode" not in KEY_MAP
    assert "mode" not in LEAF_ORDER["search"]
    assert "search.enabled" in KEY_MAP
    assert "search.providers" in KEY_MAP
    # 搜索组叶数不变（17），但概念数下降：选项 4→3、删 1 键
    assert len(LEAF_ORDER["search"]) == 17


def test_tools_group_background_governance():
    """后台治理：去重 / 单次投递 / 上下文隔离 / 工具集收窄；core 引擎移除。"""
    assert LEAF_ORDER["tools"] == (
        "background_mode",
        "background_max_chars",
                "dedupe_window_s",
        "isolate_context",
        "narrow_tools",
    )
    assert "tools.background_engine" in V5_DROPPED_PATHS
    assert "tools.background_engine" not in KEY_MAP
    assert "tools.background_timeout" not in KEY_MAP


# ---------------------------------------------------------------------------
# schema 自身合法性 + 读取视图
# ---------------------------------------------------------------------------

_SUPPORTED_TYPES = frozenset(
    {"int", "float", "bool", "string", "text", "list", "file", "object", "template_list", "dict"}
)


def test_schema_leaf_types_are_legal():
    """每个叶子的 ``type`` 都在核心 ``DEFAULT_VALUE_MAP`` 支持的类型里。"""
    for group, meta in SCHEMA.items():
        assert meta["type"] == "object", group
        for leaf, definition in meta["items"].items():
            assert definition["type"] in _SUPPORTED_TYPES, f"{group}.{leaf}"
            if definition["type"] == "object":
                assert isinstance(definition.get("items"), dict), f"{group}.{leaf}"


def test_schema_defaults_and_enums_are_consistent():
    """枚举叶的默认值必须在 options 里；钳制叶的默认值必须在区间内。"""
    for group, meta in SCHEMA.items():
        for leaf, definition in meta["items"].items():
            path = f"{group}.{leaf}"
            value = schema_default(group, leaf, definition)
            if "options" in definition:
                assert value in definition["options"], path
                assert choices_of(path) is not None, path
            if clamp_of(path) is not None:
                low, high = clamp_of(path)
                assert isinstance(value, int), path
                assert low <= value <= high, path
            assert value == LEAF_DEFAULTS[path], path


def test_schema_texts_are_chinese_and_non_empty():
    """组与叶都必须有中文 ``description`` 与非空 ``hint``（面板可读性）。"""
    def has_cjk(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    for group, meta in SCHEMA.items():
        assert has_cjk(meta["description"]), group
        assert has_cjk(meta["hint"]), group
        for leaf, definition in meta["items"].items():
            assert has_cjk(definition["description"]), f"{group}.{leaf}"
            assert has_cjk(definition["hint"]), f"{group}.{leaf}"


def test_effect_timing_three_buckets():
    assert set(EFFECT_TIMING.values()) == {EFFECT_HOT, EFFECT_NEW_CONTEXT, EFFECT_IMPORT}
    counts = {name: sum(1 for value in EFFECT_TIMING.values() if value == name)
              for name in (EFFECT_HOT, EFFECT_NEW_CONTEXT, EFFECT_IMPORT)}
    assert counts == {EFFECT_HOT: 52, EFFECT_NEW_CONTEXT: 1, EFFECT_IMPORT: 4}
    assert EFFECT_TIMING["security.block_internal_ip"] == EFFECT_NEW_CONTEXT
    assert effect_of("resources.engine") == EFFECT_IMPORT
    assert effect_of("nope.nope") is None
    assert EFFECT_LABELS[EFFECT_HOT].startswith("下次调用生效")


def test_no_flat_paths_are_declared():
    """39 条"无 v3 扁平旧键"的路径必须与 KEY_MAP 的 None 一一对应。"""
    assert set(NO_FLAT_PATHS) == {path for path, flat in KEY_MAP.items() if flat is None}
    assert len(NO_FLAT_PATHS) == 38
    assert len(FLAT_KEYS) == 25
    v6_flats = {flat for flat in KEY_MAP.values() if flat}
    assert len(v6_flats) == 19, "25 个扁平旧键只落到 19 个 v6 叶上"
    assert v6_flats <= set(FLAT_KEYS)
    # 6 个扁平旧键随 drop 键消失；merge 又把 3+2 个键收进两个叶（所以是 19 不是 25-6）
    assert set(FLAT_KEYS) - v6_flats == {
        "default_url",
        "timeout",
        "agent_max_steps",
        "page_perception",
        "vision_prompt",
        "vision_cache_ttl",
    }
    assert list(FLAT_KEYS) == sorted(FLAT_KEYS)


def test_default_of_returns_independent_copies():
    banned = default_of("security.banned_words")
    banned.append("MARKER")
    assert "MARKER" not in default_of("security.banned_words")
    viewport = default_of("resources.viewport")
    viewport["width"] = 1
    assert default_of("resources.viewport")["width"] == 1280
    assert default_of("nope.nope", "fallback") == "fallback"
    assert LEAF_TYPES["resources.viewport"] == "object"


def test_config_view_reads_nested_v6_paths():
    view = ConfigView({"resources": {"max_tabs": 7}, "search": {"engine": "百度搜索"}})
    assert view.get("resources.max_tabs") == 7
    assert view.lookup("search.engine") == ("百度搜索", True)
    # 已删除的叶（t12 R4：`tools.deliver_once` 不做开关）读出来必须是"没这一项"
    value, found = view.lookup("tools.nope_not_a_leaf")
    assert found is False and value is None
    assert view.get("tools.nope_not_a_leaf", "default") == "default"
    # 扁平兜底仍可用（v3 旧键名），且嵌套优先
    flat_view = ConfigView({"max_pages": 9})
    assert flat_view.get("resources.max_tabs") == 9
    assert ConfigView({"resources": {"max_tabs": 2}, "max_pages": 9}).get("resources.max_tabs") == 2


def test_schema_leaf_locates_object_and_flat_nodes():
    viewport = schema_leaf(SCHEMA, "resources.viewport")
    assert viewport is not None and viewport["type"] == "object"
    assert "device_scale_factor" in viewport["items"]
    providers = schema_leaf(SCHEMA, "search.providers")
    assert providers is not None and providers["type"] == "template_list"
    assert set(providers["templates"]) == {"brave", "bocha", "firecrawl", "tavily", "exa"}
    assert schema_leaf(SCHEMA, "nope.nope") is None
    # 扁平旧键兜底（v3 形态的 schema）
    assert schema_leaf({"max_chars": {"type": "int"}}, "resources.max_text_chars") is not None
