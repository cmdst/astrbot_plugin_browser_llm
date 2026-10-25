"""v4 配置结构守卫：schema 叶子 ⇄ KEY_MAP ⇄ provider 模板 三方一致。

本文件是 spec §4.4「新增两条守卫」的落地，另外补三条形状守卫：

- schema 的**每组叶子** == ``KEY_MAP`` 的全部路径（41 条）—— 新增键漏在
  schema 或漏在 KEY_MAP 都会红；
- ``search.*`` 路径全部恰好 2 段（迁移器只支持「组 -> 叶」两层）；
- provider 模板集合 == 实现类集合 == ``PROVIDER_IDS``（漏一个实现就红）。

守卫自身要能红：``test_guard_detects_missing_schema_leaf`` 用临时 schema
证明少一个叶子时集合断言成立。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config_map import GROUPS, KEY_MAP, V4_ONLY_PATHS
from core.search_providers import PROVIDER_CLASSES, PROVIDER_IDS, TEMPLATE_FIELDS

_ROOT = Path(__file__).resolve().parent.parent


def load_schema() -> dict:
    return json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def schema_paths(schema: dict) -> set[str]:
    """把分组 schema 展开成点号路径集合（组 -> 叶）。"""
    paths: set[str] = set()
    for group, meta in schema.items():
        assert meta["type"] == "object", f"{group} 不是 object 组"
        for leaf in meta.get("items", {}):
            paths.add(f"{group}.{leaf}")
    return paths


def test_schema_leaves_match_key_map():
    """schema 的 54 个叶子 == KEY_MAP 的 54 条路径（双向；v5 起 45 -> 54）。"""
    schema = load_schema()
    paths = schema_paths(schema)
    assert paths == set(KEY_MAP), (
        f"仅 schema 有: {sorted(paths - set(KEY_MAP))}；"
        f"仅 KEY_MAP 有: {sorted(set(KEY_MAP) - paths)}"
    )
    assert len(paths) == 54


def test_group_names_match_groups():
    """顶层组名 == core.config_map.GROUPS == 路径里出现的组。"""
    schema = load_schema()
    used = {path.split(".", 1)[0] for path in KEY_MAP}
    assert set(schema) == set(GROUPS) == used
    assert set(GROUPS) == {
        "browser",
        "limits",
        "security",
        "perception",
        "search",
        "tools",
        "agent",
    }


def test_search_paths_are_two_segments():
    """迁移器只支持 2 层：所有 search.* / tools.* 路径必须恰好 2 段。"""
    assert len(V4_ONLY_PATHS) == 16
    for path in V4_ONLY_PATHS:
        parts = path.split(".")
        assert len(parts) == 2, f"{path} 不是 2 段路径"
        assert parts[0] == "search"
    # 全量路径也不允许出现 3 段（browser.viewport 的值是 dict，但路径只有 2 段）
    for path in KEY_MAP:
        assert len(path.split(".")) == 2, path


def test_search_group_shape():
    """search 组：object + 17 键（v4 的 16 + v1.7.0 的 mode）+ template_list 池。"""
    search = load_schema()["search"]
    assert search["type"] == "object"
    items = search["items"]
    assert len(items) == 17, sorted(items)
    assert set(items) == set({path.split(".", 1)[1] for path in V4_ONLY_PATHS}) | {
        "mode"
    }, "search 组只允许 v4 的 16 键 + v1.7.0 的 mode"

    providers = items["providers"]
    assert providers["type"] == "template_list"
    assert providers["default"] == []
    assert set(providers["templates"]) == set(PROVIDER_IDS)
    assert len(providers["templates"]) == 5

    # 默认值：默认关（v4 代码 + v3 配置行为零变化）
    assert items["enabled"]["default"] is False
    assert items["max_rounds"]["default"] == 2
    assert items["max_parallel"]["default"] == 3
    assert items["total_timeout"]["default"] == 60
    assert items["fetch_pages"]["default"] is False
    assert items["fetch_timeout"]["default"] == 12
    assert items["cache_ttl"]["default"] == 120
    assert set(items["safesearch"]["options"]) == {"off", "moderate", "strict"}


def test_search_templates_match_provider_impls():
    """schema 模板集合 == 实现类集合 == PROVIDER_IDS；每模板 7 个字段。"""
    templates = load_schema()["search"]["items"]["providers"]["templates"]
    assert tuple(templates) == PROVIDER_IDS
    assert set(PROVIDER_CLASSES) == set(PROVIDER_IDS)
    for template_id, meta in templates.items():
        assert set(meta["items"]) == set(TEMPLATE_FIELDS), template_id
        # 每个字段都有类型 + 默认值（Dashboard 保存时会按模板校验类型）
        for field_name, leaf in meta["items"].items():
            assert leaf["type"] in ("string", "int", "bool"), (template_id, field_name)
            assert "default" in leaf, (template_id, field_name)
        # 敏感字段必须有"敏感"提示
        assert "敏感" in meta["items"]["api_key"]["description"]


def test_every_search_leaf_is_documented():
    """每个 search 叶都要有 description 与 hint（防止"加了键没说明"）。"""
    items = load_schema()["search"]["items"]
    for name, leaf in items.items():
        assert leaf.get("description"), name
        assert leaf.get("hint"), name


def test_no_dead_groups_in_schema():
    """schema 里不允许出现零读取点的组（async/delivery/crawler/vision）。"""
    schema = load_schema()
    for dead in ("async", "delivery", "crawler", "vision"):
        assert dead not in schema


def test_guard_detects_missing_schema_leaf():
    """守卫自身能红：删掉一个叶子后集合断言必须不成立。"""
    schema = load_schema()
    schema["search"]["items"].pop("max_rounds")
    paths = schema_paths(schema)
    assert paths != set(KEY_MAP)
    assert paths - set(KEY_MAP) == set()
    assert set(KEY_MAP) - paths == {"search.max_rounds"}


def test_guard_detects_extra_schema_leaf():
    """守卫自身能红：多出一个未映射叶子同样不成立。"""
    schema = load_schema()
    schema["search"]["items"]["ghost_key"] = {"type": "bool", "default": False}
    paths = schema_paths(schema)
    assert paths - set(KEY_MAP) == {"search.ghost_key"}
