"""v1.7.0 配置结构 v5 的两种「叶」口径 + 迁移器自检恢复（规格 §6，captain 第 2 点）。

「叶」有两个口径，**两者都对、且必须同时成立**：

| 口径 | 数 | 复算方式 |
| --- | --- | --- |
| ``core.config_map.KEY_MAP`` 条目 | **54** | ``len(KEY_MAP)``（``browser.viewport`` 算 1 条复合叶） |
| 展平 ``_conf_schema.json`` 得到的 JSON 叶子键 | **55** | ``browser.viewport.items.{width,height}`` 分开计 |

v4 侧同理是 45 / 46。**不要**为了凑 55 去拆 ``browser.viewport``，也**不要**
改 schema —— 两种口径服务两个不同的问题（前者 = 代码读了多少个点号路径；
后者 = 面板上有多少个可填的框）。

另外钉住 captain 追加的收尾项：``tools/migrate_config_v4.py::is_migrated()``
必须用**自己那套冻结表**（``GROUPS_V4`` / ``KEY_MAP_V4``），否则它对自家
v4 产物恒返回 ``False``（永远自称"还没迁移"，每次 ``--apply`` 白写一遍）。
"""

from __future__ import annotations

import importlib.util
import json

from pathlib import Path

import pytest

from core.config_map import (
    AGENT_PATHS,
    CONFIG_V5_NEW_PATHS,
    GROUPS,
    GROUPS_V4,
    KEY_MAP,
    KEY_MAP_V4,
    V4_LEAF_PATHS,
)

ROOT = Path(__file__).resolve().parent.parent


def _schema() -> dict:
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _flatten_leaf_keys(node: dict) -> list[str]:
    """展平 schema：``type=object`` 且有 ``items`` 的组继续下钻，其余算一个叶。"""
    out: list[str] = []
    for name, meta in node.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
            out.extend(f"{name}.{leaf}" for leaf in _flatten_leaf_keys(meta["items"]))
        elif "type" in meta:
            out.append(name)
    return out


def _load_v4_migrator():
    path = ROOT / "tools" / "migrate_config_v4.py"
    spec = importlib.util.spec_from_file_location("migrate_config_v4_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------
# 两种口径
# ---------------------------------------------------------------


def test_key_map_has_54_leaves_in_7_groups():
    assert len(KEY_MAP) == 54
    assert len(GROUPS) == 7
    assert GROUPS == (
        "browser",
        "limits",
        "security",
        "perception",
        "search",
        "tools",
        "agent",
    )
    assert len(AGENT_PATHS) == 8
    assert len(CONFIG_V5_NEW_PATHS) == 9


def test_flattened_schema_has_55_leaf_keys():
    leaves = _flatten_leaf_keys(_schema())
    assert len(leaves) == 55, sorted(leaves)
    # 差的那一条就是 browser.viewport 的 width/height 两个子键
    assert "browser.viewport.width" in leaves
    assert "browser.viewport.height" in leaves
    assert "browser.viewport" not in leaves


def test_two_leaf_counts_differ_exactly_by_viewport():
    """54 与 55 的差**恰好**是 viewport 拆成两个子键（不是一个模糊的 1）。"""
    flat = set(_flatten_leaf_keys(_schema()))
    composite = set(KEY_MAP)
    only_flat = flat - composite
    only_composite = composite - flat
    assert only_flat == {"browser.viewport.width", "browser.viewport.height"}
    assert only_composite == {"browser.viewport"}


def test_legacy_counts_are_45_and_46():
    """旧结构同理：KEY_MAP_V4 = 45，展平的 v4 叶 = 46。"""
    assert len(KEY_MAP_V4) == 45
    assert len(V4_LEAF_PATHS) == 45
    v4_groups = set(GROUPS_V4)
    flat = [p for p in _flatten_leaf_keys(_schema()) if p.split(".")[0] in v4_groups]
    # v4 的 45 条里 browser.viewport 展平 +1；v5 新增的 9 条（search.mode +
    # agent.* 8）不属于 v4，必须被排除。
    v5_new = set(CONFIG_V5_NEW_PATHS)
    flat_v4 = [p for p in flat if p not in v5_new]
    assert len(flat_v4) == 46, flat_v4


def test_every_key_map_path_is_a_schema_leaf_or_composite():
    """KEY_MAP 的每条路径都能在 schema 里找到（复合叶指向 object 节点）。"""
    schema = _schema()

    def find(path: str):
        node = schema
        for part in path.split("."):
            if isinstance(node.get(part), dict):
                node = node[part]
            elif isinstance(node.get("items"), dict) and isinstance(
                node["items"].get(part), dict
            ):
                node = node["items"][part]
            else:
                return None
        return node

    for path in KEY_MAP:
        assert find(path) is not None, path


# ---------------------------------------------------------------
# v4 迁移器自检恢复（captain 追加项）
# ---------------------------------------------------------------


def test_v4_migrator_uses_its_own_frozen_maps():
    mig = _load_v4_migrator()
    assert tuple(mig.V4_PATHS) == tuple(KEY_MAP_V4)
    assert tuple(mig.V4_GROUP_ORDER) == tuple(GROUPS_V4)
    assert mig.POST_V4_PATHS == frozenset(KEY_MAP) - frozenset(KEY_MAP_V4)


def test_v4_migrator_is_migrated_true_for_its_own_output(tmp_path):
    """v4 产物 ⇒ ``is_migrated`` True（幂等；CLI 会打印 already migrated）。"""
    mig = _load_v4_migrator()
    flat = {key: f"v-{idx}" for idx, key in enumerate(sorted(mig._PATH_TO_FLAT.values()))}
    produced = mig.migrate(flat)
    assert mig.is_migrated(produced) is True
    # 重跑 migrate 不改变值（幂等内容）
    again = mig.migrate(produced)
    assert again == produced


def test_v4_migrator_is_migrated_true_for_v5_config(tmp_path):
    """已经是 v5（多 agent 组 + search.mode）⇒ 在 v4 意义上也算迁移完成。"""
    mig = _load_v4_migrator()
    flat = {key: f"v-{idx}" for idx, key in enumerate(sorted(mig._PATH_TO_FLAT.values()))}
    produced = mig.migrate(flat)
    v5_like = {
        **produced,
        "search": {**produced["search"], "mode": "external"},
        "agent": {
            "provider_id": "",
            "persona_mode": "default",
            "persona_prompt": "",
            "task_timeout": 600,
            "image_max_side": 1280,
            "image_quality": 80,
            "result_max_chars": 3000,
            "ledger_max_lines": 24,
        },
    }
    assert mig.is_migrated(v5_like) is True


def test_v4_migrator_is_migrated_false_for_missing_or_unknown():
    """缺组 / 真未知键 ⇒ 仍为 False（豁免只有 v4 之后的新路径，不是垃圾桶）。"""
    mig = _load_v4_migrator()
    flat = {key: f"v-{idx}" for idx, key in enumerate(sorted(mig._PATH_TO_FLAT.values()))}
    produced = mig.migrate(flat)
    missing = {k: v for k, v in produced.items() if k != "search"}
    assert mig.is_migrated(missing) is False
    unknown = {**produced, "browser": {**produced["browser"], "future_leaf": 1}}
    assert mig.is_migrated(unknown) is False


@pytest.mark.parametrize("group", list(GROUPS_V4))
def test_each_v4_group_required(group):
    """六个 v4 组逐个都是"必须在"，删任一组都判未迁移。"""
    mig = _load_v4_migrator()
    flat = {key: f"v-{idx}" for idx, key in enumerate(sorted(mig._PATH_TO_FLAT.values()))}
    produced = mig.migrate(flat)
    assert mig.is_migrated({k: v for k, v in produced.items() if k != group}) is False
