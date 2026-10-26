"""迁移器 v5（v4 -> v5，兼容扁平 v3 直迁）+ v5 配置结构一致性守卫。

对应规格 §6.6 的 14 条实现不变量与 §6 的六条【判据】。三条主线：

1. **不漏叶**：``_conf_schema.json`` ⇄ ``core.config_map`` 的 54 叶逐叶相等
   （组名 / 叶名 / 类型 / 默认值 / 枚举 / 声明顺序），45 条基线路径 0 改名；
2. **可迁移**：``--dry-run`` / ``--apply`` / ``--verify`` / ``--restore-from``
   （+ 降级用的 ``--strip-v5``）四模式行为与退出码逐条钉住；任意形态（扁平 v3 /
   分组 v4）都能一次吃到 54 叶；
3. **可回滚且不泄值**：BOM / ``0600`` / 原子替换 / 备份命名；任何模式都不打印
   配置值（``search.providers`` 连指纹都不打印，只给条数）。

迁移器不在包内（``tools/`` 无 ``__init__.py``），用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from core.config_map import (
    CONFIG_V5_NEW_PATHS,
    GROUPS,
    KEY_MAP,
    LEAF_ORDER,
    LEAF_PATHS,
    LEAF_TYPES,
    LEAVES,
    LEGACY_PATHS,
    V4_LEAF_PATHS,
    default_of,
    effect_of,
    flat_key,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "migrate_config_v5", ROOT / "tools" / "migrate_config_v5.py"
)
mig = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mig)

SCHEMA: dict = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

#: 值里埋的 marker：任何模式都不该出现在输出里（值可能是搜索 API key）。
MARKER = "MARKER_SECRET_9f2c"
API_KEY_MARKER = "sk-LEAKME-8a1d"


# ---------------------------------------------------------------------------
# 夹具：三种真实形态（扁平 v3 / 分组 v4 / 已经 v5）
# ---------------------------------------------------------------------------


def v4_leaves() -> dict[str, object]:
    """v4 的 45 叶 -> 各不相同、可辨识的值（便于"值零变化"断言）。"""
    values: dict[str, object] = {}
    for path in V4_LEAF_PATHS:
        kind = LEAF_TYPES[path]
        if path == "search.providers":
            values[path] = [{"__template_key": "brave", "api_key": API_KEY_MARKER, "weight": 70}]
        elif path == "browser.viewport":
            values[path] = {"width": 1111, "height": 777}
        elif kind == "int":
            values[path] = 41
        elif kind == "bool":
            values[path] = True
        elif kind == "list":
            values[path] = [f"{MARKER}-{path}"]
        else:
            values[path] = f"{MARKER}-{path}"
    return values


def grouped(config_paths: dict[str, object]) -> dict:
    """``路径 -> 值`` 按 schema 声明顺序整理成嵌套结构（只保留给定路径）。"""
    out: dict[str, dict] = {group: {} for group in GROUPS}
    for group in GROUPS:
        for leaf in LEAF_ORDER[group]:
            path = f"{group}.{leaf}"
            if path in config_paths:
                out[group][leaf] = config_paths[path]
    return {group: leaves for group, leaves in out.items() if leaves}


def v4_config() -> dict:
    return grouped(v4_leaves())


def flat_v3_config() -> dict:
    """扁平 v3：25 个顶层旧键（v4 之前的真实形态）。"""
    values = v4_leaves()
    return {flat_key(path): values[path] for path in V4_LEAF_PATHS if flat_key(path)}


def write(path: Path, data: dict, *, bom: bool = True) -> None:
    encoding = "utf-8-sig" if bom else "utf-8"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding=encoding)
    os.chmod(path, 0o600)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def leaves_of(config: dict) -> dict[str, object]:
    """把任意形态展开成 ``路径 -> 值``（用迁移器自己的 flatten）。"""
    return mig.flatten(config)[0]


# ---------------------------------------------------------------------------
# 线 1：schema ⇄ core.config_map —— 54 叶逐叶相等（不漏叶的唯一证据）
# ---------------------------------------------------------------------------


def schema_leaves() -> list[tuple[str, str, dict]]:
    """按组声明顺序展开 schema 的叶子（组, 叶, 定义）。"""
    out = []
    for group, meta in SCHEMA.items():
        assert meta["type"] == "object", group
        for leaf, definition in meta["items"].items():
            out.append((group, leaf, definition))
    return out


def schema_default(group: str, leaf: str, definition: dict):
    """schema 叶的默认值；``object`` 叶取各子项默认值的组合（核心就是这么递归的）。"""
    if "default" in definition:
        return definition["default"]
    assert definition["type"] == "object", (group, leaf)
    return {key: sub["default"] for key, sub in definition["items"].items()}


def test_schema_and_key_map_are_bijective():
    """schema 的 54 叶 == KEY_MAP 的 54 条路径（双向，且组数/叶数写死）。"""
    pairs = schema_leaves()
    assert len(SCHEMA) == 7
    assert sorted(SCHEMA) == sorted(GROUPS)
    assert len(pairs) == 54
    assert len(KEY_MAP) == 54
    schema_paths = {f"{group}.{leaf}" for group, leaf, _ in pairs}
    assert schema_paths == set(KEY_MAP)
    assert schema_paths == set(LEAF_PATHS)
    assert len(LEAF_PATHS) == 54
    # 逐组叶数（规格 §6.1）
    per_group = {group: len(meta["items"]) for group, meta in SCHEMA.items()}
    assert per_group == {
        "browser": 6,
        "limits": 7,
        "security": 5,
        "perception": 7,
        "search": 17,
        "tools": 4,
        "agent": 8,
    }
    assert sum(per_group.values()) == 54


def test_leaf_table_matches_schema_leaf_by_leaf():
    """``LEAVES`` 与 schema **逐叶**一致：类型 / 默认值 / 枚举 / 声明顺序。"""
    pairs = schema_leaves()
    assert [item.path for item in LEAVES] == [f"{g}.{leaf}" for g, leaf, _ in pairs]
    for (group, leaf, definition), item in zip(pairs, LEAVES, strict=True):
        path = f"{group}.{leaf}"
        assert item.path == path
        assert item.type == definition["type"], path
        assert item.default == schema_default(group, leaf, definition), path
        if definition.get("options"):
            assert item.choices == tuple(definition["options"]), path
        else:
            assert item.choices is None, path
        # 面板可读性：每个叶都必须有 description 与 hint
        assert definition.get("description"), path
        assert definition.get("hint"), path
        # 生效时机三档之一（规格 §6.2）
        assert effect_of(path) in {"hot", "new_context", "import"}, path


def test_legacy_paths_are_untouched_by_v5():
    """45 条基线路径 0 改名 0 删除；新增恰好 9 条（规格 §6.3）。"""
    assert len(V4_LEAF_PATHS) == 45
    assert len(LEGACY_PATHS) == 45
    assert len(CONFIG_V5_NEW_PATHS) == 9
    assert set(V4_LEAF_PATHS) | set(CONFIG_V5_NEW_PATHS) == set(KEY_MAP)
    assert not set(V4_LEAF_PATHS) & set(CONFIG_V5_NEW_PATHS)
    # 每个旧路径在 schema 里仍然存在、类型未变（改名/改类型都会红）
    pairs = {f"{g}.{leaf}": definition for g, leaf, definition in schema_leaves()}
    for path in V4_LEAF_PATHS:
        assert path in pairs, path
        assert pairs[path]["type"] == LEAF_TYPES[path], path


def test_migrator_does_not_import_astrbot():
    """不变量 12：迁移器只 import ``core.config_map``，无核心环境也能跑。"""
    import ast

    tree = ast.parse((ROOT / "tools" / "migrate_config_v5.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.append(node.module or "")
    assert not [name for name in imported if name.split(".")[0] == "astrbot"]
    assert not [name for name in imported if name in {"main"}]
    assert "core.config_map" in imported


# ---------------------------------------------------------------------------
# 线 2：四个模式（+ 降级用的 --strip-v5）
# ---------------------------------------------------------------------------


def test_dry_run_does_not_touch_file(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--dry-run"]) == 0
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("cfg.json.bak-*"))
    out = capsys.readouterr().out
    assert "RESULT mode=dry-run" in out
    assert "rc=0" in out


def test_apply_adds_nine_and_keeps_45_values(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    source = read(path)
    assert mig.main(["--config", str(path), "--apply"]) == 0
    after = read(path)
    assert list(after) == list(GROUPS)
    assert sum(len(leaves) for leaves in after.values()) == 54
    before_leaves = leaves_of(source)
    after_leaves = leaves_of(after)
    assert len(before_leaves) == 45
    assert len(after_leaves) == 54
    # 45 条旧叶值一字不变
    for path_ in LEGACY_PATHS:
        assert after_leaves[path_] == before_leaves[path_], path_
    # 9 个新叶 = 默认值
    for path_ in CONFIG_V5_NEW_PATHS:
        assert after_leaves[path_] == default_of(path_), path_
    # 新叶里最容易出错的两个：mode 必须 external，agent 组必须整组出现
    assert after["search"]["mode"] == "external"
    assert after["agent"] == {
        leaf: default_of(f"agent.{leaf}") for leaf in LEAF_ORDER["agent"]
    }


def test_apply_preserves_bom_and_permissions(tmp_path):
    """不变量 1/11：写回保留 BOM 形态、权限 0600、备份也是 0600。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config(), bom=True)
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    backups = list(tmp_path.glob("cfg.json.bak-pre-v5-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
    # 备份内容 = 迁移前的逐字节
    assert read(backups[0]) == v4_config()


def test_apply_without_bom_keeps_file_without_bom(tmp_path):
    """源没有 BOM 时不自作主张加 BOM（"保留形态"的另一半）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config(), bom=False)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_apply_is_idempotent_byte_for_byte(tmp_path, capsys):
    """不变量 5/13：连跑两次逐字节一致；每次都留一份备份（幂等时 wrote=0）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    first = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert path.read_bytes() == first
    out = capsys.readouterr().out
    assert "already migrated" in out
    assert "wrote=0" in out
    assert "idempotent=1" in out
    backups = sorted(tmp_path.glob("cfg.json.bak-pre-v5-*"))
    assert len(backups) == 2, "同一秒内的第二次备份不能覆盖第一次（回滚锚点）"
    assert len({p.name for p in backups}) == 2


def test_apply_is_order_stable_against_schema(tmp_path):
    """不变量 3：产物叶顺序 = schema 声明顺序（否则核心首次加载会白写一次）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    data = read(path)
    assert list(data) == list(GROUPS)
    for group, meta in SCHEMA.items():
        assert list(data[group]) == list(meta["items"]), group


def test_restore_from_backup_is_byte_identical(tmp_path):
    """不变量 9：``--restore-from`` 逐字节重建（不校验 schema，回滚不是迁移）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    original = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v5-*"))[0]
    assert mig.main(["--config", str(path), "--restore-from", str(backup)]) == 0
    assert path.read_bytes() == original
    # 回滚自己也要留一份 safety 备份
    assert list(tmp_path.glob("cfg.json.bak-pre-restore-*"))


def test_from_backup_alias_works(tmp_path):
    """``--from-backup`` 是 ``--restore-from`` 的别名（派单里两种写法都出现过）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    original = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v5-*"))[0]
    assert mig.main(["--config", str(path), "--from-backup", str(backup)]) == 0
    assert path.read_bytes() == original


def test_verify_baseline_ok_and_mismatch(tmp_path, capsys):
    """``--verify --baseline``：旧值不变 + 9 新叶在 ⇒ 0；旧值被改 ⇒ 3。"""
    path = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    write(path, v4_config())
    baseline.write_bytes(path.read_bytes())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert mig.main(["--config", str(path), "--verify", "--baseline", str(baseline)]) == 0
    out = capsys.readouterr().out
    assert "legacy_fingerprint=" in out and "value_diff=[]" in out
    assert "fp browser.type=" in out  # 逐叶指纹（值本身不打印）

    # 把一条基线叶的值改掉 ⇒ rc=3
    tampered = read(path)
    tampered["browser"]["type"] = "firefox"
    write(path, tampered)
    assert mig.main(["--config", str(path), "--verify", "--baseline", str(baseline)]) == 3


def test_verify_baseline_detects_missing_new_leaf(tmp_path):
    """少一个 v5 新叶也要报不一致（不然"迁移成功"是假的）。"""
    path = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    write(path, v4_config())
    baseline.write_bytes(path.read_bytes())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    stripped = read(path)
    stripped["agent"].pop("task_timeout")
    write(path, stripped)
    assert mig.main(["--config", str(path), "--verify", "--baseline", str(baseline)]) == 3


def test_verify_against_is_strict(tmp_path):
    """``--against``：键集合 + 值全等（只有 9 个新叶差异也判失败，rc=3）。"""
    path = tmp_path / "cfg.json"
    baseline = tmp_path / "base.json"
    write(path, v4_config())
    baseline.write_bytes(path.read_bytes())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert mig.main(["--config", str(path), "--verify", "--against", str(baseline)]) == 3
    # 与自己的副本比 ⇒ 0
    same = tmp_path / "same.json"
    same.write_bytes(path.read_bytes())
    assert mig.main(["--config", str(path), "--verify", "--against", str(same)]) == 0


def test_verify_requires_against_or_baseline(tmp_path):
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    assert mig.main(["--config", str(path), "--verify"]) == 2


# ---------------------------------------------------------------------------
# 线 3：三类异常输入（缺失键 / 未知键 / 已迁移键）+ 类型不符
# ---------------------------------------------------------------------------


def test_missing_keys_and_groups_are_filled_with_defaults(tmp_path, capsys):
    """缺失键/缺失组：判定=可确定性修复，补齐默认，退出码 0 + WARN 点名。"""
    path = tmp_path / "cfg.json"
    partial = v4_config()
    partial.pop("tools")  # 整组缺失
    partial["limits"].pop("max_chars")  # 单叶缺失
    write(path, partial)
    assert mig.main(["--config", str(path), "--apply"]) == 0
    data = read(path)
    assert sum(len(leaves) for leaves in data.values()) == 54
    assert data["tools"]["background_engine"] == "plugin"
    assert data["limits"]["max_chars"] == 4000
    out = capsys.readouterr().out
    # 缺失键/缺失组都要被点名（tools 整组缺 => 4 叶 + limits.max_chars）
    assert "[WARN] baseline_missing_before=" in out
    assert "'limits.max_chars'" in out
    assert "'tools.background_engine'" in out


def test_unknown_key_refused_by_default(tmp_path, capsys):
    """未知键（v5 已废弃/运营手加）：默认拒写，rc=2，只报键名。"""
    path = tmp_path / "cfg.json"
    payload = v4_config()
    payload["ghost_key"] = f"{MARKER}-ghost"
    write(path, payload)
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 2
    assert path.read_bytes() == before
    err = capsys.readouterr()
    assert "ghost_key" in (err.out + err.err)
    assert MARKER not in (err.out + err.err)


def test_unknown_key_kept_with_allow_unknown(tmp_path):
    """``--allow-unknown``：保留未知键并继续（运营手加过键的场景）。"""
    path = tmp_path / "cfg.json"
    payload = v4_config()
    payload["ghost_key"] = "ghost-value"
    write(path, payload)
    assert mig.main(["--config", str(path), "--apply", "--allow-unknown"]) == 0
    data = read(path)
    assert data["ghost_key"] == "ghost-value"
    assert sum(len(v) for v in data.values() if isinstance(v, dict)) == 54


def test_already_migrated_reports_idempotent(tmp_path, capsys):
    """已迁移键（已是 v5）：判定=幂等，rc=0 + idempotent=1，不改任何值。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    snapshot = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 0
    assert path.read_bytes() == snapshot
    out = capsys.readouterr().out
    assert "RESULT mode=apply keys=54 groups=7 new=9 unknown=0 idempotent=1 rc=0" in out


def test_deep_structure_refused(tmp_path):
    """路径深度 > 2：拒写，rc=2（``search.providers`` 是不透明叶，不算超深）。"""
    path = tmp_path / "cfg.json"
    payload = v4_config()
    payload["future"] = {"deep": {"deeper": 1}}
    write(path, payload)
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--apply"]) == 2
    assert path.read_bytes() == before
    # 已知的 object 叶不被当成超深
    assert mig.structure_too_deep(v4_config()) == []


def test_type_mismatch_is_normalized_without_printing_values(tmp_path, capsys):
    """值类型不符（string↔int、bool 字符串）：归一 + 只报路径，不打印值。"""
    path = tmp_path / "cfg.json"
    payload = v4_config()
    payload["limits"]["max_chars"] = "5000"  # 字符串数字
    payload["security"]["block_internal_ip"] = "FALSE"  # 布尔字符串
    payload["perception"]["silent_mode"] = "off"
    payload["tools"]["background_timeout"] = " 300 "
    payload["browser"]["timeout"] = "not-a-number"  # 归一不了 ⇒ 保留原值
    write(path, payload)
    assert mig.main(["--config", str(path), "--apply"]) == 0
    data = read(path)
    assert data["limits"]["max_chars"] == 5000
    assert isinstance(data["limits"]["max_chars"], int)
    assert data["security"]["block_internal_ip"] is False
    assert data["perception"]["silent_mode"] is False
    assert data["tools"]["background_timeout"] == 300
    assert data["browser"]["timeout"] == "not-a-number"
    out = capsys.readouterr().out
    assert "type-normalized" in out
    assert "'limits.max_chars'" in out
    assert "type-uncoercible" in out and "'browser.timeout'" in out
    # 归一后的值与 core 的解析语义一致（读取方最终拿到的就是它）
    from core import as_bool, as_int

    assert as_int("5000", 4000) == 5000
    assert as_bool("FALSE", True) is False


def test_coercion_matches_core_helpers():
    """迁移器的静默归一与 ``core.as_int`` / ``core.as_bool`` **同语义**。

    为什么不用 ``core.as_*`` 本体：它们的 warning 会把值打进 stderr
    （``配置项数值非法（%r）``），与本工具"不打印配置值"的不变量冲突。
    """
    from core import as_bool, as_int

    for raw in ("30", " 30 ", 30, 0, "0", "-1"):
        assert mig._coerce_int(raw) == as_int(raw, 0)
    for raw in ("true", "false", "TRUE", "False", "yes", "no", "1", "0", "off", ""):
        expect = as_bool(raw, True)
        got = mig._coerce_bool(raw)
        assert got is not mig._NO_COERCE and got is expect, raw
    # 明确"不猜"的三类（与 core 的差异点，写进 docstring 的理由）
    for raw in (None, "", True, False):
        assert mig._coerce_int(raw) is mig._NO_COERCE
    for raw in (None, [1], {"a": 1}):
        assert mig._coerce_bool(raw) is mig._NO_COERCE


# ---------------------------------------------------------------------------
# 线 4：不吃值、不泄密钥、链式迁移、降级
# ---------------------------------------------------------------------------


def test_never_prints_config_values(tmp_path, capsys):
    """任何模式都不打印配置值（只打键名/计数/指纹）。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    baseline = tmp_path / "base.json"
    baseline.write_bytes(path.read_bytes())
    modes = [
        ["--dry-run"],
        ["--apply"],
        ["--verify", "--baseline", str(baseline)],
        ["--verify", "--against", str(baseline)],
        ["--strip-v5"],
        ["--restore-from", str(baseline)],
    ]
    for mode in modes:
        capsys.readouterr()
        mig.main(["--config", str(path), *mode])
        captured = capsys.readouterr()
        blob = captured.out + captured.err
        assert MARKER not in blob, mode
        assert API_KEY_MARKER not in blob, mode
        assert "LEAKME" not in blob, mode


def test_providers_are_counted_not_fingerprinted(tmp_path, capsys):
    """``search.providers`` 连指纹都不打印（指纹同样算值泄露面），只给条数。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    baseline = tmp_path / "base.json"
    baseline.write_bytes(path.read_bytes())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    capsys.readouterr()
    assert mig.main(["--config", str(path), "--verify", "--baseline", str(baseline)]) == 0
    out = capsys.readouterr().out
    assert "fp search.providers=<1 项，值不打印>" in out
    # 别把整个 providers 列表哈希出来冒充"指纹"
    assert mig.fingerprint([{"api_key": API_KEY_MARKER}]) not in out


def test_flat_v3_migrates_straight_to_v5(tmp_path):
    """链式迁移：扁平 v3（25 顶层键）一次吃到 54 叶（规格 §6.6 不变量 7）。"""
    path = tmp_path / "cfg.json"
    flat = flat_v3_config()
    assert len(flat) == 25
    write(path, flat)
    assert mig.main(["--config", str(path), "--apply"]) == 0
    data = read(path)
    assert list(data) == list(GROUPS)
    assert sum(len(leaves) for leaves in data.values()) == 54
    leaves = leaves_of(data)
    flat_leaves = leaves_of(flat)
    # 25 条扁平旧键的值原样落到对应的分组叶上
    for path_ in V4_LEAF_PATHS:
        if flat_key(path_):
            assert leaves[path_] == flat_leaves[path_], path_
    assert data["browser"]["type"] == f"{MARKER}-browser.type"


def test_strip_v5_removes_exactly_the_nine(tmp_path):
    """降级方向（无备份文件时）：删掉 9 个新叶，45 条基线与顺序原样保留。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    assert mig.main(["--config", str(path), "--apply"]) == 0
    v5_bytes = path.read_bytes()
    # 先干跑：不写盘
    assert mig.main(["--config", str(path), "--strip-v5"]) == 0
    assert path.read_bytes() == v5_bytes
    assert mig.main(["--config", str(path), "--strip-v5", "--apply"]) == 0
    data = read(path)
    assert "agent" not in data
    assert "mode" not in data["search"]
    assert sum(len(leaves) for leaves in data.values()) == 45
    assert list(data) == [group for group in GROUPS if group != "agent"]
    assert leaves_of(data) == leaves_of(v4_config())
    # 备份是"删之前"的 v5 形态（可再 --restore-from 回来）
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v5-*"))[-1]
    assert len(leaves_of(read(backup))) == 54


def test_strip_v5_on_v4_is_noop(tmp_path, capsys):
    """已是 v4（45 叶）时 strip 无事可做：rc=0 + idempotent=1，不写盘。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    before = path.read_bytes()
    assert mig.main(["--config", str(path), "--strip-v5", "--apply"]) == 0
    assert path.read_bytes() == before
    assert "already v4-shaped" in capsys.readouterr().out


def test_to_writes_elsewhere_and_leaves_source_alone(tmp_path):
    """``--to``：写另一个文件，源文件一个字节都不动。"""
    source = tmp_path / "cfg.json"
    target = tmp_path / "out" / "cfg.json"
    write(source, v4_config())
    before = source.read_bytes()
    assert mig.main(["--config", str(source), "--to", str(target), "--apply"]) == 0
    assert source.read_bytes() == before
    assert sum(len(leaves) for leaves in read(target).values()) == 54
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


@pytest.mark.parametrize(
    "argv",
    [
        ["--config", "/nonexistent/definitely-missing.json", "--dry-run"],
        ["--config", "/nonexistent/definitely-missing.json", "--apply"],
        ["--config", "/nonexistent/definitely-missing.json", "--restore-from", "/tmp"],
    ],
)
def test_bad_paths_return_code_not_traceback(argv, capsys):
    """不变量 10：异常路径返回退出码，不抛 traceback。"""
    assert mig.main(argv) == 2
    captured = capsys.readouterr()
    assert "[ERR]" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_json_returns_code_not_traceback(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert mig.main(["--config", str(path), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert "[ERR]" in captured.err
    assert "Traceback" not in captured.err


def test_result_line_is_machine_readable(tmp_path, capsys):
    """不变量 14：每个模式都打一行 ``RESULT ...``，字段齐全。"""
    path = tmp_path / "cfg.json"
    write(path, v4_config())
    expected = {
        "--dry-run": "mode=dry-run",
        "--apply": "mode=apply",
    }
    for flag, marker in expected.items():
        capsys.readouterr()
        mig.main(["--config", str(path), flag])
        lines = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("RESULT ")
        ]
        assert lines, flag
        line = lines[-1]
        assert marker in line
        for field in ("keys=", "groups=", "new=", "unknown=", "idempotent=", "rc="):
            assert field in line, (flag, field)


def test_help_works(capsys):
    """``--help`` 正常打印并 rc=0（不被 ``SystemExit(0)`` 误判成拒绝）。"""
    assert mig.main(["--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--apply", "--verify", "--restore-from", "--from-backup"):
        assert flag in out


def test_pure_functions_are_reusable():
    """纯函数直接可单测：flatten / nest / migrate / is_migrated / diff。"""
    flat = flat_v3_config()
    canonical, unknown = mig.flatten(flat)
    assert unknown == []
    assert len(canonical) == 25
    migrated = mig.migrate(flat)
    assert mig.is_migrated(migrated) is True
    assert mig.is_migrated(v4_config()) is False
    nested = mig.nest(canonical)
    assert list(nested) == list(GROUPS[:4])  # browser/limits/security/perception
    assert mig.diff_keys(migrated, migrated) == {
        "left_only": [],
        "right_only": [],
        "unknown_left": [],
        "unknown_right": [],
    }
    # 扁平 v3 里 search/perception 的部分路径本来就不存在（v3 没有这些键），
    # 所以指纹与迁移产物**不同**是正确行为；能比的是"再迁一次不变"。
    assert mig.legacy_fingerprint(flat) != mig.legacy_fingerprint(migrated)
    assert mig.legacy_fingerprint(migrated) == mig.legacy_fingerprint(mig.migrate(migrated))
    assert mig.missing_paths(migrated) == []
