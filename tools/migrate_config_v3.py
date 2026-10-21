#!/usr/bin/env python3
"""browser_llm 配置迁移器 v3（S2a）。

用途：在**换 schema 之前**，把 `astrbot_plugin_browser_llm_config.json`
从 25 个扁平键迁移成 4 个分组（browser / limits / security / perception）。

适用范围提示（2026-10-21 / v4）：本文件是 **v3 历史迁移器**（扁平 -> 4 组），
保留给"只需回到分组 v3"的场景与既有测试；**v3 -> v4（新增 search 组 16 键）
请用 `tools/migrate_config_v4.py`**（它同时吃扁平 v3 与分组 v3 两种来源，
并修掉了 audit §7.4 记录的四个坑）。

为什么必须独立于插件运行：核心 `AstrBotConfig.__init__` 会在插件 `__init__`
之前按 schema 删除未知键并落盘（astrbot_config.py:261-278、:113/:129），
所以"插件内迁移"一定太晚。

安全约束（与评审 K1–K3 一致）：
- 任何模式都不打印配置值，只打印键名与计数；
- `--apply` 先写备份，再原子替换；文件权限 600；
- 未知键默认拒绝写盘（`--allow-unknown` 才保留并继续）；
- 幂等：已是分组结构时 no-op。

退出码：0 成功；2 拒绝（未知键/参数）；3 校验不一致。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config_map import FLAT_KEYS, KEY_MAP  # noqa: E402

BOM = "utf-8-sig"
GROUP_ORDER: tuple[str, ...] = ("browser", "limits", "security", "perception")
# v4 起 ``KEY_MAP`` 里多了 16 条值为 None 的 search.* 路径（无扁平旧键）。
# 本迁移器是**历史 v3 迁移器**（扁平 -> 4 组），只认有扁平旧键的 25 条，
# 因此必须显式过滤 None —— 否则 flatten() 会把 search 叶子映射到 None 键上。
# v3 -> v4（含 search 组）请用 ``tools/migrate_config_v4.py``。
_PATH_TO_FLAT: dict[str, str] = {
    path: flat for path, flat in KEY_MAP.items() if flat is not None
}
_FLAT_TO_PATH = {flat: path for path, flat in _PATH_TO_FLAT.items()}
GROUPS = {path.split(".", 1)[0] for path in _PATH_TO_FLAT}


class MigrationError(RuntimeError):
    """迁移前置条件不满足（未知键、结构异常等）。"""


# --------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------


def load_config(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding=BOM))


def flatten(config: dict) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """把分组/扁平混合结构归一成扁平键 -> 值；同时收集未知项。"""
    flat: dict[str, Any] = {}
    unknown: list[tuple[str, Any]] = []
    if not isinstance(config, dict):
        raise MigrationError("config root must be an object")

    for key, value in config.items():
        if key in GROUPS and isinstance(value, dict):
            for leaf, leaf_value in value.items():
                path = f"{key}.{leaf}"
                if path in _PATH_TO_FLAT:
                    flat[_PATH_TO_FLAT[path]] = leaf_value
                else:
                    unknown.append((path, leaf_value))
        elif key in _FLAT_TO_PATH:
            flat[key] = value
        else:
            unknown.append((key, value))
    return flat, unknown


def nest(flat: dict[str, Any]) -> dict[str, Any]:
    """扁平键 -> 分组结构；保持 GROUP_ORDER 的顺序。"""
    buckets: dict[str, dict[str, Any]] = {group: {} for group in GROUP_ORDER}
    for flat_key, value in flat.items():
        path = _FLAT_TO_PATH.get(flat_key)
        if path is None:
            raise MigrationError(f"unmapped key: {flat_key}")
        group, leaf = path.split(".", 1)
        buckets.setdefault(group, {})[leaf] = value
    return {group: buckets[group] for group in GROUP_ORDER if buckets[group]}


def migrate(config: dict, *, allow_unknown: bool = False) -> dict:
    """返回迁移后的新配置；未涉及的值原样保留。"""
    flat, unknown = flatten(config)
    if unknown and not allow_unknown:
        names = ", ".join(sorted(name for name, _ in unknown))
        raise MigrationError(f"unknown keys, refusing to write: {names}")

    migrated = nest(flat)
    for name, value in unknown:
        if "." in name:
            group, leaf = name.split(".", 1)
            migrated.setdefault(group, {})[leaf] = value
        else:
            migrated[name] = value
    return migrated


def is_migrated(config: dict) -> bool:
    """是否已是纯分组结构（无扁平键）。"""
    if not isinstance(config, dict):
        return False
    if any(key in config for key in _FLAT_TO_PATH):
        return False
    return all(
        isinstance(config.get(group), dict) for group in GROUPS if group in config
    )


def diff_values(left: dict, right: dict) -> list[str]:
    """按值比较两份配置，返回值不同的键名（绝不输出值本身）。

    `diff_keys` 只比键集合；导入/迁移场景必须额外比一次值，
    否则"键都在、值被改"会被误判为成功。
    """
    lflat, _ = flatten(left)
    rflat, _ = flatten(right)
    common = set(lflat) & set(rflat)
    return sorted(key for key in common if lflat[key] != rflat[key])


def diff_keys(left: dict, right: dict) -> dict[str, list[str]]:
    """按键名比较两份配置（绝不输出值）。"""
    lflat, lunknown = flatten(left)
    rflat, runknown = flatten(right)
    return {
        "left_only": sorted(set(lflat) - set(rflat)),
        "right_only": sorted(set(rflat) - set(lflat)),
        "unknown_left": sorted(name for name, _ in lunknown),
        "unknown_right": sorted(name for name, _ in runknown),
    }


# --------------------------------------------------------------------
# 文件操作
# --------------------------------------------------------------------


def _write_atomic(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".migrate-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=BOM) as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _backup(path: Path, tag: str) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S")
    target = path.with_name(f"{path.name}.bak-{tag}-{stamp}")
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def _report_counts(label: str, config: dict) -> str:
    flat, unknown = flatten(config)
    return (
        f"{label}: flat_keys={len(flat)} groups={sorted(k for k in config if k in GROUPS)} "
        f"unknown={len(unknown)}"
    )


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="browser_llm 配置迁移 v3（干跑优先）")
    parser.add_argument("--config", required=True, help="源插件配置文件路径")
    parser.add_argument(
        "--to",
        metavar="PATH",
        default=None,
        help="写入/恢复的目标文件（默认与 --config 相同）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只报告计划（默认）")
    mode.add_argument("--apply", action="store_true", help="备份后写入")
    mode.add_argument("--verify", action="store_true", help="与 --against 比对")
    mode.add_argument(
        "--from-backup",
        metavar="PATH",
        help="从备份重建配置；若之前用 --to 导入，回滚时须带相同的 --to",
    )
    parser.add_argument("--against", metavar="PATH", help="--verify 的基线文件")
    parser.add_argument("--allow-unknown", action="store_true", help="保留未知键并继续")
    return parser


def run(args: argparse.Namespace) -> int:
    path = Path(args.config)
    # --to 省略时目标与源相同：保持既有行为逐字节不变。
    target = Path(args.to) if args.to else path

    if args.from_backup:
        backup = Path(args.from_backup)
        if not backup.is_file():
            print(f"[ERR] backup not found: {backup}")
            return 2
        target.parent.mkdir(parents=True, exist_ok=True)
        safety = _backup(target, "pre-restore") if target.is_file() else None
        shutil.copy2(backup, target)
        os.chmod(target, 0o600)
        print(f"[OK] restored from {backup.name}" + (f" (safety={safety.name})" if safety else ""))
        return 0

    if not path.is_file():
        print(f"[ERR] config not found: {path}")
        return 2

    current = load_config(path)

    if args.verify:
        if not args.against:
            print("[ERR] --verify requires --against")
            return 2
        if not target.is_file():
            print(f"[ERR] target not found: {target}")
            return 2
        baseline = load_config(Path(args.against))
        actual = load_config(target)
        diff = diff_keys(actual, baseline)
        value_diff = diff_values(actual, baseline)
        bad = any(diff.values()) or bool(value_diff)
        report = dict(diff, value_diff=value_diff)
        print(f"[{'FAIL' if bad else 'OK'}] verify: " + json.dumps(report, ensure_ascii=False))
        return 3 if bad else 0

    if is_migrated(current):
        print("[OK] already migrated, nothing to do")
        return 0

    try:
        migrated = migrate(current, allow_unknown=args.allow_unknown)
    except MigrationError as exc:
        print(f"[ERR] {exc}")
        return 2

    flat_before, _ = flatten(current)
    flat_after, _ = flatten(migrated)
    missing = sorted(set(FLAT_KEYS) - set(flat_after))
    print(_report_counts("before", current))
    print(_report_counts("after ", migrated))
    print(f"[PLAN] keys={len(flat_after)} missing={missing}")

    if not args.apply:
        print("[DRY-RUN] no file written")
        return 0 if not missing else 2

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(target, "pre-v3") if target.is_file() else None
    _write_atomic(target, migrated)
    backup_note = f"backup={backup.name}, " if backup else ""
    print(f"[OK] applied ({backup_note}mode=600, target={target.name})")

    # 写入后自检：逐键等值（只报键名）
    reread = load_config(target)
    diff = diff_keys(reread, current)
    value_diff = diff_values(reread, current)
    if any(diff.values()) or value_diff:
        report = dict(diff, value_diff=value_diff)
        print("[FAIL] post-write verify: " + json.dumps(report, ensure_ascii=False))
        return 3
    print("[OK] post-write verify: all keys and values equal")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
