"""静态扫描守卫：读取点必须与 KEY_MAP 一一对应。

S1 之后 main.py / core/browser.py 已切换到 ConfigView 点号路径。
守卫分三层：
1) 点号路径覆盖：KEY_MAP 的全部路径（v4：41 条 = 25 旧 + 16 search 新）
   必须全部出现，且不得出现未知路径；
2) 扁平键残留：仍允许过渡期读取，但读到的键必须是已映射的旧键；
3) 运行时回退：只给扁平键的配置，ConfigView 仍能解析 25 条**有扁平旧键**的
   路径（v4 纯新增的 16 条必须"缺键"，见 test_flat_only_config_keeps_v4_paths_missing）。

守卫自身要"能红"：用临时文件造缺失/未知/成员判断三类负例。

本守卫只覆盖「读到的键必须被映射」这一侧；"schema 里的键必须都被读到"
由 ``tests/test_qa_complement.py::test_conf_schema_keys_match_load_config``
负责。两者必须同时存在：删除任一个前先确认另一个能独立覆盖。
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.config_map import FLAT_KEYS, KEY_MAP, V4_ONLY_PATHS, ConfigView

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
BROWSER = ROOT / "core" / "browser.py"

# core/browser.py 里唯一允许的扁平读取：插件注入的运行时路径，不是配置键
BROWSER_RAW_FLAT_KEYS = {"data_dir"}

DOTTED_RECEIVERS = {"view", "cfg_view", "config_view"}
DOTTED_METHODS = {"get", "lookup", "get_list"}
FLAT_RECEIVERS = {"cfg", "config"}
MEMBER_RECEIVERS = {"cfg", "config", "view"}


def _receiver_name(node: ast.AST) -> str | None:
    name = getattr(node, "id", None)
    if isinstance(name, str):
        return name
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def scan_reads(path: Path) -> tuple[set[str], set[str], set[str]]:
    """返回 (点号路径读取, 扁平键读取, 成员判断键)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dotted: set[str] = set()
    flat: set[str] = set()
    members: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.args
        ):
            receiver = _receiver_name(node.func.value)
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            key = first.value
            if receiver in DOTTED_RECEIVERS and node.func.attr in DOTTED_METHODS:
                if "." in key:
                    dotted.add(key)
            elif receiver in FLAT_RECEIVERS and node.func.attr == "get":
                if "." not in key:
                    flat.add(key)
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
        ):
            left, right = node.left, node.comparators[0]
            if (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and _receiver_name(right) in MEMBER_RECEIVERS
            ):
                members.add(left.value)

    return dotted, flat, members


# ------------------------- main.py -------------------------


def test_main_covers_every_mapped_path():
    dotted, _, _ = scan_reads(MAIN)
    missing = sorted(set(KEY_MAP) - dotted)
    assert missing == [], f"main.py 未读取这些点号路径: {missing}"


def test_main_reads_no_unmapped_path():
    dotted, _, _ = scan_reads(MAIN)
    extra = sorted(dotted - set(KEY_MAP))
    assert extra == [], f"main.py 读取了 KEY_MAP 之外的点号路径: {extra}"


def test_main_flat_reads_are_mapped_or_absent():
    """过渡期允许残留扁平读取，但键必须已映射（S8 删除旧键后此集合为空）。"""
    _, flat, _ = scan_reads(MAIN)
    assert sorted(flat - set(FLAT_KEYS)) == []


def test_main_membership_checks_are_mapped():
    _, _, members = scan_reads(MAIN)
    assert sorted(members - set(FLAT_KEYS)) == []


def test_flat_only_config_resolves_every_path():
    """只有扁平旧键的配置（v3 生产现状）：25 条旧路径必须全部解析成功。

    v4 纯新增的 ``search.*`` 跳过（它们没有扁平旧键），由下一条守卫钉住。
    """
    flat_cfg = {key: f"v-{key}" for key in FLAT_KEYS}
    view = ConfigView(flat_cfg)
    for path, key in KEY_MAP.items():
        if key is None:
            continue
        value, found = view.lookup(path)
        assert found is True, path
        assert value == f"v-{key}", path


def test_flat_only_config_keeps_v4_paths_missing():
    """反向守卫：v4 纯新增路径**不得**从扁平键兜底（缺键 ≠ 空值）。"""
    assert len(V4_ONLY_PATHS) == 16
    flat_cfg = {key: f"v-{key}" for key in FLAT_KEYS}
    view = ConfigView(flat_cfg)
    for path in V4_ONLY_PATHS:
        _value, found = view.lookup(path)
        assert found is False, f"{path} 不该被扁平兜底命中"


def test_grouped_v4_config_resolves_search_paths():
    """分组 v4 配置（生产迁移后的形态）：search 组 16 条必须全部读得到。"""
    from core.config_map import GROUPS

    cfg = {group: {} for group in GROUPS}
    for path in V4_ONLY_PATHS:
        group, leaf = path.split(".", 1)
        cfg[group][leaf] = f"v-{path}"
    view = ConfigView(cfg)
    for path in V4_ONLY_PATHS:
        value, found = view.lookup(path)
        assert found is True, path
        assert value == f"v-{path}", path


# ------------------------- core/browser.py -------------------------


def test_browser_core_reads_only_known_keys():
    dotted, flat, _ = scan_reads(BROWSER)
    assert sorted(dotted) == [
        "browser.proxy",
        "browser.timeout",
        "browser.type",
        "browser.viewport",
        "perception.enable_screenshot",
        "security.block_internal_ip",
    ]
    assert flat == BROWSER_RAW_FLAT_KEYS


# ------------------------- 负例：守卫必须能红 -------------------------


def test_guard_detects_missing_path(tmp_path: Path):
    body = "\n".join(
        f'    val = view.get("{path}")' for path in KEY_MAP if path != "limits.max_links"
    )
    sample = tmp_path / "partial.py"
    sample.write_text(f"def f(view):\n{body}\n", encoding="utf-8")
    dotted, _, _ = scan_reads(sample)
    assert sorted(set(KEY_MAP) - dotted) == ["limits.max_links"]


def test_guard_detects_unknown_path(tmp_path: Path):
    sample = tmp_path / "unknown.py"
    sample.write_text(
        'def f(view):\n    return view.get("browser.legacy_type")\n', encoding="utf-8"
    )
    dotted, _, _ = scan_reads(sample)
    assert sorted(dotted - set(KEY_MAP)) == ["browser.legacy_type"]


def test_guard_detects_unknown_flat_key(tmp_path: Path):
    sample = tmp_path / "flat.py"
    sample.write_text(
        'def f(cfg):\n    return cfg.get("legacy_browser_type")\n', encoding="utf-8"
    )
    _, flat, _ = scan_reads(sample)
    assert sorted(flat - set(FLAT_KEYS)) == ["legacy_browser_type"]


def test_guard_detects_membership_key(tmp_path: Path):
    sample = tmp_path / "member.py"
    sample.write_text(
        'def f(cfg):\n    return "legacy_flag" in cfg\n', encoding="utf-8"
    )
    _, _, members = scan_reads(sample)
    assert members == {"legacy_flag"}


def test_guard_ignores_non_config_receivers(tmp_path: Path):
    """schema/result 等对象的 get 不是配置读取点。"""
    sample = tmp_path / "noise.py"
    sample.write_text(
        "def f(schema, result):\n"
        '    a = schema.get("vision_provider_id")\n'
        '    b = result.get("success")\n'
        "    return a, b\n",
        encoding="utf-8",
    )
    dotted, flat, _ = scan_reads(sample)
    assert dotted == set()
    assert flat == set()
