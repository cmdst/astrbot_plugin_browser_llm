#!/usr/bin/env python3
"""browser_llm 配置迁移器（v3 -> 当前版：4 组/25 叶 -> 6 组/45 叶）。

**为什么必须独立于插件运行**：核心 ``AstrBotConfig.__init__`` 会在插件
``__init__`` 之前按 schema 删除未知键并落盘（``astrbot_config.py:113``、
``:260-278``），所以"插件内迁移"一定太晚。

**两种来源都支持**（生产实际存在两份，形态不同）：

1. **扁平 v3**：25 个顶层键（正式插件目录 `astrbot_plugin_browser_llm`
   当前的配置文件就是这种形态）；
2. **分组 v3**：``browser``/``limits``/``security``/``perception`` 四组
   （`astrbot_plugin_browser_llm_tmp` 副本插件的配置文件形态）。

两种文件都可能是 **UTF-8 BOM**（核心读写用 ``utf-8-sig``），本迁移器读时
剥 BOM、写时保留 BOM。

**成对回滚 / 配置先于代码（顺序不变量）**

> 任意时刻必须满足：``键集合(磁盘配置) ⊆ 键集合(当前代码 schema)``。
> 违反 ⇒ 核心 ``check_config_integrity`` 把多出来的键删掉并落盘 ⇒ 值丢失。

- 本插件的生产现实是**扁平 v3**，而 v4 schema 是 5 个分组 ⇒ 属"改名/删键"
  类变更：**必须先跑本迁移器把配置改成 v4，再替换插件代码**；
- 回滚**成对**：``--restore-from <备份>`` 把配置退回原形态（扁平 v3 或分组
  v3，逐字节来自备份），**然后**再把插件目录回滚到旧版；只回其一 = 静默丢设置
  （旧代码会把 ``search`` 组当未知键删掉；新代码会把扁平旧键当未知键删掉）。
- 代码侧先落、配置侧后补只适用于"纯新增键"；本插件 v4 两者都有（新增 search
  组 + 把扁平键搬进分组），所以按更严的那条走：**配置先迁、代码后换**。

安全约束（与 v3 迁移器一致，另加两条）：

- 任何模式都**不打印配置值**，只打印键名 / 计数 / 分组名 / 叶值哈希；
- ``--apply`` 先备份再原子替换，文件权限 0600、保留 BOM；
- 未知键默认拒绝写盘（``--allow-unknown`` 才保留并继续）；
- 幂等：已是 6 组齐全的当前结构时 no-op（只补缺失叶子，不覆盖已有值）；
- 任何路径段数 > 2 ⇒ 拒绝写盘（退出码 2）：本迁移器只支持"组 -> 叶"两层，
  ``providers`` 整值当**不透明叶子**搬运、不展开。

退出码：0 成功；2 拒绝（未知键/参数/结构超深）；3 校验不一致。
"""

from __future__ import annotations

import argparse
import hashlib
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

from core.config_map import GROUPS, GROUPS_V4, KEY_MAP, KEY_MAP_V4  # noqa: E402

BOM = "utf-8-sig"

#: 输出顺序 = ``core.config_map.GROUPS``（browser/limits/security/perception/search）。
#: 注意：它**必须**由 KEY_MAP 派生，不能另写一份 —— audit §7.4-① 的坑就是
#: "只给 KEY_MAP 加了 search.*，GROUP_ORDER 还是硬编码 4 组，nest() 静默丢整组"。
GROUP_ORDER: tuple[str, ...] = tuple(GROUPS)

#: 路径 -> 旧扁平键（25 条）与纯新增路径（16 条，值为 None）。
_PATH_TO_FLAT: dict[str, str] = {p: v for p, v in KEY_MAP.items() if v is not None}
_FLAT_TO_PATH: dict[str, str] = {v: p for p, v in _PATH_TO_FLAT.items()}

#: v4 纯新增的 search 组默认值（没有 v3 扁平旧键，只能取默认）。
#: 与 ``_conf_schema.json["search"]["items"][k]["default"]`` 逐条相等，
#: 由 ``tests/test_search_migration_v4.py`` 钉住（防止迁移器与 schema 漂移）。
SEARCH_DEFAULTS: dict[str, Any] = {
    "search.enabled": False,
    "search.providers": [],
    "search.max_parallel": 3,
    "search.max_results": 8,
    "search.max_rounds": 2,
    "search.provider_timeout": 15,
    "search.total_timeout": 60,
    "search.cooldown_seconds": 60,
    "search.cache_ttl": 120,
    "search.fetch_pages": False,
    "search.fetch_top_n": 3,
    "search.fetch_timeout": 12,
    "search.fetch_max_chars": 2000,
    "search.region": "",
    "search.safesearch": "moderate",
    "search.proxy": "",
}

#: v1.5.0 纯新增的 tools 组默认值 + v1.6.0 的 ``tools.background_engine``
#: （同样无 v3 扁平旧键）。
#: 与 ``_conf_schema.json["tools"]["items"][k]["default"]`` 逐条相等，由
#: ``tests/test_search_migration_v4.py`` 钉住。默认 off ⇒ 迁移后的配置行为
#: 与 v1.4.0 一致（这是"默认关闭"落在迁移产物上的那一面）。
TOOLS_DEFAULTS: dict[str, Any] = {
    "tools.background_mode": "off",
    "tools.background_timeout": 240,
    "tools.background_max_chars": 1500,
    # v1.6.0：后台**机制**维度。默认 plugin ⇒ 迁移产物的行为与 v1.5.0 一致；
    # core 需要核心修复 + 重启，绝不做成默认值。
    "tools.background_engine": "plugin",
}

#: 全部"v3 没有的新键"默认值（迁移时整组补齐；设计 §1.3-⑨ 命名的
#: ``DEFAULTED_PATHS``）。新增键只加在这里，``migrate()`` 与
#: ``is_migrated()`` 自然跟上（不再各写一份），``V4_PATHS`` 保持
#: ``tuple(KEY_MAP)`` 不动 —— 冻结成 41 条会让 ``is_migrated()``
#: 对自己的产物返回 False。
DEFAULTED_PATHS: dict[str, Any] = {**SEARCH_DEFAULTS, **TOOLS_DEFAULTS}

#: 必须有值的叶集合：v3 的 25 条（迁移基线）。缺任一条 = 迁移源不完整。
LEGACY_PATHS: tuple[str, ...] = tuple(sorted(_PATH_TO_FLAT))

#: v4 的 45 条路径（**冻结**，来自 ``core.config_map.KEY_MAP_V4``）。
#:
#: v1.7.0 起这个列表**不能再跟活 ``KEY_MAP`` 走**：活表已经 54 条（v5 新增
#: ``search.mode`` + ``agent.*`` 9 条），跟着走会让 ``is_migrated()``
#: 对**它自己产出的 45 叶 v4 配置**恒返回 ``False``（永远自称"还没迁移"），
#: 于是 ``--apply`` 每次都白写一次内容等价的文件、也不再打印 already-migrated。
V4_PATHS: tuple[str, ...] = tuple(KEY_MAP_V4)

#: v4 的 6 个组（**冻结**）：``is_migrated()`` 的"组齐全"判定只用它。
V4_GROUP_ORDER: tuple[str, ...] = tuple(GROUPS_V4)

#: 活表里"v4 之后新增"的路径（v5 的 9 条）。``is_migrated()`` 对它们**不敏感**：
#: 一份已经是 v5 的配置在 v4 意义上同样是"v4 迁移做完了"，不该被判成未迁移。
POST_V4_PATHS: frozenset[str] = frozenset(KEY_MAP) - frozenset(KEY_MAP_V4)


def _load_leaf_order() -> dict[str, tuple[str, ...]]:
    """从 ``_conf_schema.json`` 取每组叶子的**声明顺序**（缺失时返回空表）。

    为什么要在意顺序：核心 ``check_config_integrity`` 会把"顺序不一致"也算一次
    变更（``astrbot_config.py:270-274``），于是首次加载会白写一次配置文件。
    让迁移产物直接按 schema 顺序排列，可以避免这次无谓的落盘。
    """
    try:
        schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    order: dict[str, tuple[str, ...]] = {}
    for group, meta in schema.items():
        if isinstance(meta, dict) and isinstance(meta.get("items"), dict):
            order[group] = tuple(meta["items"])
    return order


#: 组 -> 叶子声明顺序（与 schema 对齐；schema 缺失时降级为 KEY_MAP 顺序）。
LEAF_ORDER: dict[str, tuple[str, ...]] = _load_leaf_order()


class MigrationError(RuntimeError):
    """迁移前置条件不满足（未知键、结构超深、根不是对象等）。"""


# --------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------


def load_config(path: Path) -> dict:
    """读配置（剥 BOM）。"""
    return json.loads(Path(path).read_text(encoding=BOM))


def flatten(config: dict) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """把扁平/分组（可混合）结构归一成 ``路径 -> 值``；同时收集未知项。

    - 分组形态：``{"browser": {"type": "chromium"}}`` -> ``browser.type``；
    - 扁平形态：``{"browser_type": "chromium"}`` -> ``browser.type``；
    - ``search.providers`` 是数组，整值搬运（**不展开**成三段路径）。

    Returns:
        ``(canonical, unknown)``；``unknown`` 的键是"没法归一的原始路径"
        （例如 ``legacy_key`` 或 ``browser.unknown_leaf``）。
    """
    canonical: dict[str, Any] = {}
    unknown: list[tuple[str, Any]] = []
    if not isinstance(config, dict):
        raise MigrationError("config root must be an object")

    for key, value in config.items():
        if key in GROUP_ORDER and isinstance(value, dict):
            for leaf, leaf_value in value.items():
                path = f"{key}.{leaf}"
                if path in KEY_MAP:
                    canonical[path] = leaf_value
                else:
                    unknown.append((path, leaf_value))
        elif key in _FLAT_TO_PATH:
            canonical[_FLAT_TO_PATH[key]] = value
        else:
            unknown.append((str(key), value))
    return canonical, unknown


def _ordered_leaf_paths() -> list[str]:
    """按「组顺序（GROUP_ORDER）→ schema 声明序 → KEY_MAP 兜底」排路径。"""
    paths: list[str] = []
    seen: set[str] = set()
    for group in GROUP_ORDER:
        leaves = LEAF_ORDER.get(group, ())
        for leaf in leaves:
            path = f"{group}.{leaf}"
            if path in KEY_MAP and path not in seen:
                paths.append(path)
                seen.add(path)
    # schema 缺失/落后时补齐剩余路径（不让"顺序表"决定"搬哪些键"）。
    for path in KEY_MAP:
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def nest(canonical: dict[str, Any]) -> dict[str, Any]:
    """``路径 -> 值`` -> 按组顺序的嵌套结构；叶子顺序与 ``_conf_schema.json`` 对齐。

    ``search.providers`` 原样搬运（不展开、不校验条目内部结构）。
    """
    buckets: dict[str, dict[str, Any]] = {group: {} for group in GROUP_ORDER}
    for path in _ordered_leaf_paths():
        if path not in canonical:
            continue
        parts = path.split(".")
        if len(parts) != 2:
            # 防回退：任何 >2 段的路径都不该出现在 KEY_MAP 里（也未实现递归）。
            raise MigrationError(f"路径段数 > 2，拒绝写盘: {path}")
        group, leaf = parts
        buckets.setdefault(group, {})[leaf] = canonical[path]
    return {group: buckets[group] for group in GROUP_ORDER if buckets[group]}


def migrate(config: dict, *, allow_unknown: bool = False) -> dict:
    """v3（扁平或分组）-> 当前形态（6 组 / 45 叶）。

    - 既有 25 条路径的**值原样搬运**（不做任何类型归一，避免改变语义）；
    - v3 没有的新键（search 组 16 条、tools 组 4 条 = v1.5.0 的 3 条 +
      v1.6.0 的 ``background_engine``）源里没有
      ⇒ 取 ``DEFAULTED_PATHS``（= ``SEARCH_DEFAULTS`` + ``TOOLS_DEFAULTS``）；
    - ``search`` 组已存在时**只补缺失键，不覆盖已有值**（尤其 ``api_key``）；
    - 未知键默认拒绝；``allow_unknown=True`` 时原样保留（分组键放回组内）。
    """
    canonical, unknown = flatten(config)
    if unknown and not allow_unknown:
        names = ", ".join(sorted(name for name, _ in unknown))
        raise MigrationError(f"unknown keys, refusing to write: {names}")

    filled = dict(canonical)
    for path, default in DEFAULTED_PATHS.items():
        if path not in filled:
            filled[path] = default

    migrated = nest(filled)
    for name, value in unknown:
        if "." in name:
            group, leaf = name.split(".", 1)
            if "." in leaf:
                raise MigrationError(f"路径段数 > 2，拒绝写盘: {name}")
            migrated.setdefault(group, {})[leaf] = value
        else:
            migrated[name] = value
    return migrated


def is_migrated(config: dict) -> bool:
    """是否已是完整 **v4** 结构：无扁平旧键 + 6 组齐全 + 45 条路径全在。

    三条判定都用**冻结**表（``V4_GROUP_ORDER`` / ``V4_PATHS``）：

    - 归档边界：v4 迁移器是"历史步骤"，它的"已迁移"必须按它自己那版结构判，
      否则（跟着活 ``KEY_MAP`` 走）它对自家产物恒 ``False``；
    - 向前兼容：一份已经是 v5（7 组 / 54 叶）的配置在 v4 意义上**也是**
      "v4 迁移做完了" ⇒ 多出来的 ``agent`` 组与 ``search.mode`` 不算未知键
      （它们出现在活 ``KEY_MAP`` 里，由 ``flatten`` 归一为已知路径）。

    坑（audit §7.4-③）：v3 时代只要求"无扁平键 + 已存在的组是 dict"，于是
    "缺 search 组的 4 组配置"被判成已迁移、新增组永远进不了迁移。这里对
    **缺组**与**缺叶**都敏感。
    """
    if not isinstance(config, dict):
        return False
    if any(key in config for key in _FLAT_TO_PATH):
        return False
    if set(V4_GROUP_ORDER) - set(config):
        return False
    for group in V4_GROUP_ORDER:
        value = config.get(group)
        if not isinstance(value, dict):
            return False
    canonical, unknown = flatten(config)
    # 只对"v4 之后新增"的路径免疫；真正的未知键仍然判未迁移。
    unknown = [item for item in unknown if item[0] not in POST_V4_PATHS]
    if unknown:
        return False
    return all(path in canonical for path in V4_PATHS)


def diff_keys(left: dict, right: dict) -> dict[str, list[str]]:
    """按键路径比较两份配置（对称差；绝不输出值）。"""
    lpaths, lunknown = flatten(left)
    rpaths, runknown = flatten(right)
    return {
        "left_only": sorted(set(lpaths) - set(rpaths)),
        "right_only": sorted(set(rpaths) - set(lpaths)),
        "unknown_left": sorted(name for name, _ in lunknown),
        "unknown_right": sorted(name for name, _ in runknown),
    }


def diff_values(left: dict, right: dict) -> list[str]:
    """按值比较**共有**路径，返回值不同的路径名（绝不输出值本身）。

    注意：``diff_keys`` 只比键集合，本函数只比共有键的值 —— 所以
    ``--verify`` 必须**两个都调**（audit §7.4-④：只比共有键会把"键丢失 +
    值未变"误判成通过）。
    """
    lpaths, _ = flatten(left)
    rpaths, _ = flatten(right)
    common = set(lpaths) & set(rpaths)
    return sorted(path for path in common if lpaths[path] != rpaths[path])


def legacy_fingerprint(config: dict) -> str:
    """25 条旧路径归一化叶值的 sha256（迁移证据，不含值）。

    排序后逐条 ``path=json(value)`` 拼接再哈希：同一份配置在任何形态
    （扁平 v3 / 分组 v3 / v4）下都得到同一个指纹 ⇒ 可用来证明"迁移没动旧值"。
    """
    canonical, _unknown = flatten(config)
    h = hashlib.sha256()
    for path in LEGACY_PATHS:
        if path not in canonical:
            h.update(f"{path}=<missing>\n".encode("utf-8"))
            continue
        payload = json.dumps(canonical[path], ensure_ascii=False, sort_keys=True)
        h.update(f"{path}={payload}\n".encode("utf-8"))
    return h.hexdigest()


def missing_legacy_paths(config: dict) -> list[str]:
    """返回缺失的旧路径（迁移源的完整性检查）。"""
    canonical, _ = flatten(config)
    return sorted(path for path in LEGACY_PATHS if path not in canonical)


def group_summary(config: dict) -> list[str]:
    """返回分组概况（只给组名与叶数，不给值）。"""
    canonical, unknown = flatten(config)
    out = []
    for group in GROUP_ORDER:
        count = sum(1 for path in canonical if path.split(".", 1)[0] == group)
        if count:
            out.append(f"{group}({count})")
    if unknown:
        out.append(f"unknown({len(unknown)})")
    return out


def structure_too_deep(config: dict) -> list[str]:
    """找出超过 2 层的结构（迁移器只支持「组 -> 叶」，遇到就拒写）。

    判定：**未知**的键如果其值是 dict，说明它是"组 -> 子组 -> …"这种嵌套对象
    （合法未知键的典型形态是标量/列表）；已知路径（如 ``browser.viewport``）
    的值本身是 dict，属于 2 段路径的不透明叶子，不算超深。

    遇到超深结构默认拒绝写盘（退出码 2）；``--allow-unknown`` 时原样保留。
    """
    canonical, unknown = flatten(config)
    deep = [path for path in canonical if len(path.split(".")) != 2]
    deep += [name for name, value in unknown if isinstance(value, dict)]
    return sorted(set(deep))


# --------------------------------------------------------------------
# 文件操作
# --------------------------------------------------------------------


def _write_atomic(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".migrate-v4-", suffix=".tmp"
    )
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
    canonical, unknown = flatten(config)
    return (
        f"{label}: paths={len(canonical)} groups=[{', '.join(group_summary(config))}] "
        f"unknown={len(unknown)}"
    )


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="browser_llm 配置迁移 v4（v3 -> v4，干跑优先，不打印配置值）"
    )
    parser.add_argument("--config", required=True, help="插件配置文件路径（必填）")
    parser.add_argument(
        "--to",
        metavar="PATH",
        default=None,
        help="写入/校验的目标文件（默认与 --config 相同）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只报告计划（默认）")
    mode.add_argument("--apply", action="store_true", help="备份后写入")
    mode.add_argument(
        "--verify",
        action="store_true",
        help="与 --against（严格：键+值）或 --baseline（只查 25 旧键值与指纹）比对",
    )
    mode.add_argument(
        "--restore-from",
        "--from-backup",
        dest="restore_from",
        metavar="PATH",
        help="从备份重建配置（回滚用；若之前用 --to 导入，回滚时须带相同的 --to）",
    )
    parser.add_argument("--against", metavar="PATH", help="--verify 的严格基线")
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        help="--verify 的旧值基线：只要求 25 条旧路径的值不变，并打印叶值指纹",
    )
    parser.add_argument(
        "--allow-unknown", action="store_true", help="保留未知键并继续"
    )
    return parser


def run(args: argparse.Namespace) -> int:
    path = Path(args.config)
    target = Path(args.to) if args.to else path

    # ---- 回滚：从备份重建（必须带同一个 --to/--config 定位）----
    if args.restore_from:
        backup = Path(args.restore_from)
        if not backup.is_file():
            print(f"[ERR] backup not found: {backup.name}")
            return 2
        try:
            load_config(backup)  # 只校验可解析，不打印内容
        except (OSError, ValueError):
            print(f"[ERR] backup not valid json: {backup.name}")
            return 2
        target.parent.mkdir(parents=True, exist_ok=True)
        safety = _backup(target, "pre-restore") if target.is_file() else None
        shutil.copy2(backup, target)
        os.chmod(target, 0o600)
        print(
            f"[OK] restored from {backup.name}"
            + (f" (safety={safety.name})" if safety else "")
            + " —— 回滚须成对：配置回退后还要把插件目录回退到同一版本"
        )
        return 0

    if not path.is_file():
        print(f"[ERR] config not found: {path}")
        return 2

    current = load_config(path)

    # ---- 校验 ----
    if args.verify:
        if not args.against and not args.baseline:
            print("[ERR] --verify requires --against or --baseline")
            return 2
        if not target.is_file():
            print(f"[ERR] target not found: {target}")
            return 2
        actual = load_config(target)
        if args.baseline:
            baseline = load_config(Path(args.baseline))
            diff = diff_values(actual, baseline)
            key_diff = diff_keys(actual, baseline)
            legacy_missing = missing_legacy_paths(actual)
            bad = bool(diff) or bool(legacy_missing)
            print(
                f"[{'FAIL' if bad else 'OK'}] baseline: legacy_missing={legacy_missing} "
                f"value_diff={diff} legacy_fingerprint={legacy_fingerprint(actual)} "
                f"baseline_fingerprint={legacy_fingerprint(baseline)}"
            )
            if key_diff["unknown_left"] or key_diff["unknown_right"]:
                print(
                    "[INFO] unknown keys: "
                    f"left={key_diff['unknown_left']} right={key_diff['unknown_right']}"
                )
            return 3 if bad else 0
        baseline = load_config(Path(args.against))
        key_diff = diff_keys(actual, baseline)
        value_diff = diff_values(actual, baseline)
        bad = any(key_diff.values()) or bool(value_diff)
        print(
            f"[{'FAIL' if bad else 'OK'}] verify: "
            + json.dumps(dict(key_diff, value_diff=value_diff), ensure_ascii=False)
        )
        return 3 if bad else 0

    # ---- 迁移（默认干跑）----
    if is_migrated(current):
        print("[OK] already migrated (5 groups / 41 paths), nothing to do")
        return 0

    # 超深结构（未知键的值还是 dict）默认拒写；--allow-unknown 时按"保留未知"
    # 的原样搬运（migrate 内部仍会校验段数）。
    if not args.allow_unknown:
        deep = structure_too_deep(current)
        if deep:
            print(f"[ERR] 结构层级超过 2 段，拒绝写盘: {deep}")
            return 2

    try:
        migrated = migrate(current, allow_unknown=args.allow_unknown)
    except MigrationError as exc:
        print(f"[ERR] {exc}")
        return 2

    missing_before = missing_legacy_paths(current)
    missing_after = missing_legacy_paths(migrated)
    print(_report_counts("before", current))
    print(_report_counts("after ", migrated))
    print(
        f"[PLAN] paths={len(flatten(migrated)[0])} "
        f"legacy_missing_before={missing_before} legacy_missing_after={missing_after} "
        f"legacy_fingerprint={legacy_fingerprint(current)}"
    )

    if not args.apply:
        print("[DRY-RUN] no file written")
        return 0 if not missing_before else 2

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(target, "pre-v4") if target.is_file() else None
    _write_atomic(target, migrated)
    backup_note = f"backup={backup.name}, " if backup else ""
    print(f"[OK] applied ({backup_note}mode=600, target={target.name})")

    # 写入后自检：25 旧路径的值必须逐条等值（只报路径名，不报值）。
    reread = load_config(target)
    value_diff = diff_values(reread, current)
    legacy_missing_reread = missing_legacy_paths(reread)
    if value_diff or legacy_missing_reread:
        print(
            "[FAIL] post-write verify: "
            + json.dumps(
                {"value_diff": value_diff, "legacy_missing": legacy_missing_reread},
                ensure_ascii=False,
            )
        )
        return 3
    if legacy_fingerprint(reread) != legacy_fingerprint(current):
        print("[FAIL] post-write verify: legacy fingerprint changed")
        return 3
    print(
        "[OK] post-write verify: 25 legacy paths and values unchanged "
        f"(fingerprint={legacy_fingerprint(reread)})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
