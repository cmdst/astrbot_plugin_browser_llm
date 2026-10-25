"""迁移器 v4（v3 -> v4）测试：两种来源、幂等、值不动、不打印值、成对回滚。

对应 spec §10.2 的四个"坑"与 AC-01/AC-37：

① ``GROUP_ORDER`` 必须由 ``KEY_MAP`` 派生（否则 ``nest()`` 静默丢整组）；
② 只支持 2 段路径，超深拒写（退出码 2）；
③ ``is_migrated()`` 对"缺组/缺叶"敏感；
④ ``--verify`` 必须同时比键（对称差）与值（共有键）。

迁移器不在包内（``tools/`` 无 ``__init__.py``），用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from core.config_map import FLAT_KEYS, GROUPS, GROUPS_V4, KEY_MAP, KEY_MAP_V4

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "migrate_config_v4", ROOT / "tools" / "migrate_config_v4.py"
)
mig = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mig)

SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

#: **冻结的 v4 叶子声明顺序**（v1.7.0 把 schema 加到 54 叶后，v4 迁移器的历史
#: 契约不能跟着漂移：它只产出 6 组 45 叶）。顺序取自 v4 的 ``_conf_schema.json``。
V4_LEAF_ORDER: dict[str, tuple[str, ...]] = {
    "browser": (
        "type",
        "default_url",
        "default_search_engine",
        "proxy",
        "viewport",
        "timeout",
    ),
    "limits": (
        "max_chars",
        "max_links",
        "max_pages",
        "idle_timeout",
        "cache_days",
        "agent_max_steps",
        "agent_tool_timeout",
    ),
    "security": (
        "banned_words",
        "enable_banned_filter",
        "block_internal_ip",
        "session_whitelist",
        "session_blacklist",
    ),
    "perception": (
        "enable_screenshot",
        "silent_mode",
        "page_perception",
        "perception_rules",
        "vision_provider_id",
        "vision_prompt",
        "vision_cache_ttl",
    ),
    "search": (
        "enabled",
        "providers",
        "max_parallel",
        "max_results",
        "max_rounds",
        "provider_timeout",
        "total_timeout",
        "cooldown_seconds",
        "cache_ttl",
        "fetch_pages",
        "fetch_top_n",
        "fetch_timeout",
        "fetch_max_chars",
        "region",
        "safesearch",
        "proxy",
    ),
    "tools": (
        "background_mode",
        "background_timeout",
        "background_max_chars",
        "background_engine",
    ),
}


# ---------------------------------------------------------------
# 夹具：两种 v3 来源
# ---------------------------------------------------------------


def flat_v3() -> dict:
    """扁平 v3：25 个顶层键，值刻意各不相同（便于"值零变化"断言）。"""
    return {key: f"value-{idx}-{key}" for idx, key in enumerate(FLAT_KEYS)}


def grouped_v3() -> dict:
    """分组 v3：4 组 25 叶（= 生产副本插件配置的形态）。"""
    out: dict[str, dict] = {group: {} for group in GROUPS_V4 if group != "search"}
    for path, flat in KEY_MAP.items():
        if flat is None:
            continue
        group, leaf = path.split(".", 1)
        out[group][leaf] = f"value-{FLAT_KEYS.index(flat)}-{flat}"
    return out


def write(path: Path, data: dict, *, bom: bool = True) -> None:
    encoding = "utf-8-sig" if bom else "utf-8"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding=encoding)


# ---------------------------------------------------------------
# 坑 ①：组顺序由 KEY_MAP 派生，组一个不丢
# ---------------------------------------------------------------


def test_group_order_derived_from_key_map():
    assert tuple(mig.GROUP_ORDER) == tuple(GROUPS)
    # v1.7.0 起 GROUPS 是 7 组（多 agent）；v4 迁移器的组顺序仍派生自同一份
    assert set(mig.GROUP_ORDER) == set(GROUPS) == set(GROUPS_V4) | {"agent"}
    assert set(GROUPS_V4) == {
        "browser",
        "limits",
        "security",
        "perception",
        "search",
        "tools",
    }
    # 关键回归：nest() 必须输出**全部**组（只给 KEY_MAP 加新组而忘改
    # 组顺序时，search 整组会被静默丢掉）。用「所有路径都有值」的规范模型来测，
    # 才能覆盖"组里只有新增键"的情形。
    canonical = {path: f"v-{path}" for path in KEY_MAP}
    assert set(mig.nest(canonical)) == set(GROUPS)


def test_migrated_key_order_matches_schema():
    """迁移产物的键顺序与 schema 一致（否则核心首次加载会白写一次配置）。"""
    migrated = mig.migrate(flat_v3())
    assert list(migrated) == [g for g in GROUPS_V4 if g in migrated]
    assert "agent" not in migrated, "v4 迁移器不该产出 v5 的 agent 组"
    for group, leaves in V4_LEAF_ORDER.items():
        assert list(migrated[group]) == list(leaves), group
        # 冻结表必须与 schema 的实际前缀一致（schema 只在尾部追加叶，不重排）
        assert tuple(SCHEMA[group]["items"])[: len(leaves)] == leaves, group


def test_nest_keeps_paths_missing_from_schema_order_table():
    """schema 顺序表缺失时也不能丢键（顺序表只决定顺序）。"""
    canonical = {path: "v" for path in KEY_MAP_V4}
    original = mig.LEAF_ORDER
    try:
        mig.LEAF_ORDER = {}
        nested = mig.nest(canonical)
    finally:
        mig.LEAF_ORDER = original
    assert sum(len(leaves) for leaves in nested.values()) == len(KEY_MAP_V4) == 45


def test_search_defaults_match_schema():
    """迁移器内置的 search 默认值必须与 schema 逐条相等（防两处漂移）。"""
    schema_items = SCHEMA["search"]["items"]
    assert set(mig.SEARCH_DEFAULTS) == {
        f"search.{key}" for key in V4_LEAF_ORDER["search"]
    }
    for path, default in mig.SEARCH_DEFAULTS.items():
        leaf = schema_items[path.split(".", 1)[1]]
        assert leaf["default"] == default, path


def test_tools_defaults_match_schema():
    """v1.5.0 新增的 tools 组默认值必须与 schema 逐条相等（防两处漂移）。

    默认必须 off / 240 / 1500：off 是"行为与 v1.4.0 一致"的档位，
    250 行的 background_timeout 与 1500 字的截断长度由派单 §4.5/§4.7 定。
    """
    schema_items = SCHEMA["tools"]["items"]
    assert set(mig.TOOLS_DEFAULTS) == {f"tools.{key}" for key in V4_LEAF_ORDER["tools"]}
    for path, default in mig.TOOLS_DEFAULTS.items():
        leaf = schema_items[path.split(".", 1)[1]]
        assert leaf["default"] == default, path
    assert mig.TOOLS_DEFAULTS["tools.background_mode"] == "off"
    # 迁移器补的默认值必须覆盖 v4 里每一条"没有扁平旧键"的路径（冻结 20 条）。
    # v1.7.0 的 9 条新叶由 tools/migrate_config_v5.py 负责，不在 v4 的职责里。
    assert set(mig.DEFAULTED_PATHS) == {p for p, v in KEY_MAP_V4.items() if v is None}
    assert len(mig.DEFAULTED_PATHS) == 20


# ---------------------------------------------------------------
# 纯函数：flatten / nest / migrate / is_migrated
# ---------------------------------------------------------------


def test_flatten_flat_and_grouped_are_equivalent():
    flat_paths, flat_unknown = mig.flatten(flat_v3())
    grouped_paths, grouped_unknown = mig.flatten(grouped_v3())
    assert flat_unknown == [] and grouped_unknown == []
    assert flat_paths == grouped_paths
    assert len(flat_paths) == 25


def test_migrate_flat_v3_preserves_every_legacy_value():
    """AC-01：25 条旧路径的值零变化 + 20 条新键取默认。"""
    migrated = mig.migrate(flat_v3())
    paths, unknown = mig.flatten(migrated)
    assert unknown == []
    assert len(paths) == 45
    for path, flat in KEY_MAP_V4.items():
        if flat is None:
            assert paths[path] == mig.DEFAULTED_PATHS[path]
        else:
            assert paths[path] == f"value-{FLAT_KEYS.index(flat)}-{flat}"
    # 指纹在迁移前后一致（形态不同、旧值不变）
    assert mig.legacy_fingerprint(flat_v3()) == mig.legacy_fingerprint(migrated)


def test_migrate_grouped_v3_preserves_every_legacy_value():
    migrated = mig.migrate(grouped_v3())
    paths, _ = mig.flatten(migrated)
    assert len(paths) == 45
    assert mig.legacy_fingerprint(grouped_v3()) == mig.legacy_fingerprint(migrated)


def test_two_sources_produce_identical_result():
    assert mig.migrate(flat_v3()) == mig.migrate(grouped_v3())


def test_providers_moved_as_opaque_leaf():
    providers = [{"__template_key": "brave", "api_key": "sk-x", "weight": 70}]
    cfg = flat_v3()
    migrated = mig.migrate(cfg)
    assert migrated["search"]["providers"] == []  # 源里没有 ⇒ 默认空列表
    cfg2 = grouped_v3()
    cfg2["search"] = {"providers": providers, "enabled": True}
    migrated2 = mig.migrate(cfg2)
    assert migrated2["search"]["providers"] == providers  # 整值搬运、不展开
    assert migrated2["search"]["enabled"] is True


def test_existing_search_values_are_not_overwritten():
    """已填的 search 值（尤其 api_key）不能被默认值覆盖。"""
    cfg = grouped_v3()
    cfg["search"] = {
        "enabled": True,
        "providers": [{"__template_key": "brave", "api_key": "sk-keep-me"}],
        "max_rounds": 5,
    }
    migrated = mig.migrate(cfg)
    assert migrated["search"]["enabled"] is True
    assert migrated["search"]["max_rounds"] == 5
    assert migrated["search"]["providers"][0]["api_key"] == "sk-keep-me"
    # 缺的键补默认
    assert migrated["search"]["max_parallel"] == 3
    assert migrated["search"]["fetch_timeout"] == 12


def test_unknown_key_refused_by_default():
    cfg = flat_v3()
    cfg["legacy_key"] = 1
    with pytest.raises(mig.MigrationError):
        mig.migrate(cfg)


def test_unknown_key_allowed_and_preserved():
    cfg = flat_v3()
    cfg["legacy_key"] = 1
    migrated = mig.migrate(cfg, allow_unknown=True)
    assert migrated["legacy_key"] == 1


def test_unknown_grouped_leaf_preserved_with_allow_unknown():
    cfg = grouped_v3()
    cfg["browser"]["future_key"] = "keep"
    migrated = mig.migrate(cfg, allow_unknown=True)
    assert migrated["browser"]["future_key"] == "keep"


# ---------------------------------------------------------------
# 坑 ③：is_migrated 对缺组 / 缺叶敏感
# ---------------------------------------------------------------


def test_is_migrated_false_for_flat_v3():
    assert mig.is_migrated(flat_v3()) is False


def test_is_migrated_false_when_search_group_missing():
    """4 组/25 叶的 v3 分组配置**不是**已迁移（坑③的核心回归）。"""
    assert mig.is_migrated(grouped_v3()) is False


def test_is_migrated_false_when_one_leaf_missing():
    migrated = mig.migrate(flat_v3())
    migrated["search"].pop("max_rounds")
    assert mig.is_migrated(migrated) is False


def test_is_migrated_false_when_group_is_not_dict():
    migrated = mig.migrate(flat_v3())
    migrated["limits"] = "oops"
    assert mig.is_migrated(migrated) is False


def test_is_migrated_follows_live_key_map_not_frozen_v4():
    """v5 之后：``is_migrated`` 读的是活 ``KEY_MAP``（54 叶），v4 产物只有 45 叶。

    ⇒ 它恒为 ``False``（v4 迁移器不再自称 already-migrated）。这是**已知且有意**
    的降级：v1.7.0 起迁移通道是 ``tools/migrate_config_v5.py``（它直接吃扁平 v3），
    v4 迁移器降级为历史步骤 —— 但它的**产物仍然合法**（6 组 45 叶、值零变化），
    所以在需要"先把扁平 v3 变成分组 v4"的场合仍可安全使用（重跑 = 幂等内容）。
    """
    produced = mig.migrate(flat_v3())
    assert mig.is_migrated(produced) is False
    # 产物本身仍是一个 shape 合法的 v4 配置：6 组 45 叶、每组叶名与冻结表一致
    assert list(produced) == list(GROUPS_V4)
    assert sum(len(leaves) for leaves in produced.values()) == 45
    for group, leaves in V4_LEAF_ORDER.items():
        assert tuple(produced[group]) == leaves, group
    # 而"真的已经是 v5 形态"的配置同样不满足 v4 的判定（54 ≠ 45）
    assert mig.is_migrated({**produced, "agent": {"provider_id": ""}}) is False


# ---------------------------------------------------------------
# 坑 ②/④：结构超深 + --verify 键值都比
# ---------------------------------------------------------------


def test_structure_too_deep_detected():
    """未知键的值是 dict ⇒ 视为"组 -> 子组 -> …"（>2 层）。"""
    deep = mig.structure_too_deep({"browser": {"future_obj": {"deep": 1}}})
    assert deep == ["browser.future_obj"]


def test_known_object_leaf_is_not_too_deep():
    """``browser.viewport`` 是 2 段路径的不透明对象叶，不算超深。"""
    assert mig.structure_too_deep({"browser": {"viewport": {"width": 1}}}) == []


def test_cli_refuses_deep_structure(tmp_path):
    """超深结构默认拒写（退出码 2，文件不动），--allow-unknown 才保留。"""
    path = tmp_path / "cfg.json"
    payload = flat_v3()
    payload["future_obj"] = {"deep": 1}  # 未知顶层键且值是 dict ⇒ 视为 >2 层
    write(path, payload, bom=True)
    assert mig.main(["--config", str(path), "--apply"]) == 2
    assert json.loads(path.read_text(encoding="utf-8-sig")) == payload

    assert mig.main(["--config", str(path), "--apply", "--allow-unknown"]) == 0
    migrated = json.loads(path.read_text(encoding="utf-8-sig"))
    assert migrated["future_obj"] == {"deep": 1}
    assert set(migrated) >= set(GROUPS_V4)
    assert "agent" not in migrated


def test_diff_keys_reports_symmetric_difference():
    left = mig.migrate(flat_v3())
    right = json.loads(json.dumps(left))
    right["search"].pop("max_rounds")
    diff = mig.diff_keys(left, right)
    assert diff["left_only"] == ["search.max_rounds"]
    assert diff["right_only"] == []


def test_diff_values_compares_common_paths_only():
    left = mig.migrate(flat_v3())
    right = json.loads(json.dumps(left))
    right["browser"]["type"] = "firefox"
    assert mig.diff_values(left, right) == ["browser.type"]


# ---------------------------------------------------------------
# CLI：dry-run / apply / verify / from-backup / 幂等 / 权限 / BOM
# ---------------------------------------------------------------


def test_cli_dry_run_does_not_write(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, flat_v3())
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--dry-run"]) == 0
    assert path.read_bytes() == before


def test_cli_apply_writes_v4_with_bom_and_600(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, flat_v3())
    assert mig.main(["--config", str(path), "--apply"]) == 0

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM 保留
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    assert list(data) == list(GROUPS_V4)
    assert len(data["search"]) == 16
    assert "agent" not in data
    backups = list(tmp_path.glob("cfg.json.bak-pre-v4-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600


def test_cli_apply_grouped_v3_source(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, grouped_v3())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    assert set(data) == set(GROUPS_V4)
    assert "agent" not in data
    assert data["search"]["enabled"] is False


def test_cli_is_idempotent(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, flat_v3())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    snapshot = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert path.read_bytes() == snapshot


def test_cli_verify_against_detects_key_loss(tmp_path):
    cfg = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    write(cfg, mig.migrate(flat_v3()))
    broken = mig.migrate(flat_v3())
    broken["search"].pop("max_rounds")
    write(baseline, broken)
    assert mig.main(["--config", str(cfg), "--verify", "--against", str(baseline)]) == 3


def test_cli_verify_against_detects_value_change(tmp_path):
    """坑④：键都在、值被改必须判失败（v3 只比共有键也能抓到，但要与键比同时生效）。"""
    cfg = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    good = mig.migrate(flat_v3())
    write(baseline, good)
    tampered = json.loads(json.dumps(good))
    tampered["browser"]["type"] = "webkit"
    write(cfg, tampered)
    assert mig.main(["--config", str(cfg), "--verify", "--against", str(baseline)]) == 3


def test_cli_verify_against_ok(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, mig.migrate(flat_v3()))
    assert mig.main(["--config", str(path), "--verify", "--against", str(path)]) == 0


def test_cli_verify_baseline_checks_legacy_values_and_prints_fingerprint(tmp_path, capsys):
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    write(src, flat_v3())
    assert mig.main(["--config", str(src), "--to", str(dst), "--apply"]) == 0

    code = mig.main(
        ["--config", str(dst), "--to", str(dst), "--verify", "--baseline", str(src)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "legacy_fingerprint" in out
    assert mig.legacy_fingerprint(flat_v3()) in out


def test_cli_verify_baseline_detects_legacy_value_change(tmp_path):
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    write(src, flat_v3())
    assert mig.main(["--config", str(src), "--to", str(dst), "--apply"]) == 0
    broken = json.loads(dst.read_text(encoding="utf-8-sig"))
    broken["limits"]["max_chars"] = 1
    write(dst, broken)
    assert (
        mig.main(["--config", str(dst), "--to", str(dst), "--verify", "--baseline", str(src)])
        == 3
    )


def test_cli_unknown_key_refused_without_allow(tmp_path):
    path = tmp_path / "cfg.json"
    cfg = flat_v3()
    cfg["legacy_key"] = 1
    write(path, cfg)
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 2
    assert path.read_bytes() == before


def test_cli_from_backup_restores_and_keeps_same_to(tmp_path):
    """回滚必须带同一个 --to（v3 迁移器的坑：--from-backup 忽略 --to）。"""
    src = tmp_path / "old.json"
    dst = tmp_path / "new.json"
    write(src, flat_v3())
    write(dst, flat_v3())  # 目标已存在 ⇒ --apply 会先给它做备份
    assert mig.main(["--config", str(src), "--to", str(dst), "--apply"]) == 0
    backup = sorted(tmp_path.glob("new.json.bak-pre-v4-*"))[0]

    assert (
        mig.main(
            ["--config", str(src), "--to", str(dst), "--restore-from", str(backup)]
        )
        == 0
    )
    restored = json.loads(dst.read_text(encoding="utf-8-sig"))
    assert set(restored) == set(FLAT_KEYS)  # 回到扁平 v3（逐字节来自备份）
    assert restored == flat_v3()
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600


def test_cli_from_backup_alias_works(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, flat_v3())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v4-*"))[0]
    assert mig.main(["--config", str(path), "--from-backup", str(backup)]) == 0
    assert set(json.loads(path.read_text(encoding="utf-8-sig"))) == set(FLAT_KEYS)


# ---------------------------------------------------------------
# 安全：任何模式都不打印配置值
# ---------------------------------------------------------------


def test_cli_never_prints_config_values(tmp_path, capsys):
    sentinel = "VERY-SECRET-SENTINEL-0123456789"
    cfg = flat_v3()
    cfg["banned_words"] = [sentinel]
    path = tmp_path / "cfg.json"
    write(path, cfg)

    for argv in (
        ["--config", str(path), "--dry-run"],
        ["--config", str(path), "--apply"],
        ["--config", str(path), "--verify", "--baseline", str(path)],
    ):
        capsys.readouterr()
        mig.main(argv)
        out = capsys.readouterr()
        assert sentinel not in out.out
        assert sentinel not in out.err


def test_cli_requires_config_argument():
    with pytest.raises(SystemExit):
        mig.build_parser().parse_args([])
