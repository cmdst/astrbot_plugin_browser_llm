#!/usr/bin/env python3
"""browser_llm 配置迁移器（v4 -> v5：45 叶 -> 54 叶；兼容扁平 v3 直迁）。

**为什么必须独立于插件运行**：核心 ``AstrBotConfig.__init__`` 会在插件
``__init__`` **之前**按 schema 删除未知键并落盘（``astrbot_config.py:113``、
``:258-266``、``:277-279``、``:127-131``），所以"插件内迁移"一定太晚。
由此得到写死的不变量：

    任意时刻 ``键集合(磁盘配置) ⊆ 键集合(当前部署代码的 schema)``。

违反 ⇒ 核心删键并落盘 ⇒ **值永久丢失**（此刻插件还没跑起来，没有任何备份）。

**v5 = 纯新增**（``core/config_map.py`` 的 ``CONFIG_V5_NEW_PATHS``）：
``search.mode`` + ``agent`` 组 8 叶 = 9 条新叶，其余 45 叶原位保留、0 改名 0 删除。
严格说核心也会自动补齐这 9 叶（"缺键插默认"分支），但仍要求先跑本迁移器，理由三条：

1. 让"INV-1 已满足"这件事发生在**备份存在**的前提下；
2. 核心自动补叶会重写整个文件（顺序/缩进/空行都变），运营方拿 .bak 对比会看到
   一堆噪声 diff；本迁移器按 ``_conf_schema.json`` 的声明顺序写，``--verify``
   能给出"45 个旧叶值一字不变"的机械结论；
3. 生产配置的形态属性（BOM / ``0600`` / ``indent=2``）必须被显式保住，不能
   依赖核心重写的默认行为。

**成对回滚（升级方向）**：``--apply`` -> 换代码，顺序不可换。
**成对回滚（降级方向）**：``--restore-from <备份>``（或 ``--strip-v5``）->
换代码，顺序同样不可换。四种"只回滚其一"的后果矩阵见规格 §6.5。

四个模式（规格 §6.6；另有 ``--strip-v5`` 供降级方向用）：

====================  ==================================================  ======  ====
模式                  语义                                                写盘    退出码
====================  ==================================================  ======  ====
``--dry-run``（默认）  只打印计划：读到 N 叶 / 目标 M 叶 / 将新增的叶**名** 否      0/2
                      / 未知键名 / 是否幂等
``--apply``           先备份（同目录 ``*.bak-pre-v5-<TS>``，0600）-> 原子替换 是      0/2/3
``--verify``          ``--against`` 严格（键集合+值全等）/``--baseline``     否      0/2/3
                      只要求 45 个旧叶值不变 + 9 个新叶存在
``--restore-from``    用备份**逐字节**重建（不校验 schema）                  是      0/2
``--strip-v5``        删掉 9 个 v5 新叶（降级方向，无备份文件时用）          是      0/2/3
====================  ==================================================  ======  ====

``--from-backup`` 是 ``--restore-from`` 的别名（派单里两种写法都出现过）。

安全约束（与 v3/v4 迁移器一致，另加注明的两条）：

- **任何模式都不打印配置值**，只打印键名 / 计数 / 分组名 / 值指纹
  （``sha256(str(value))[:8]``）。特例：``search.providers`` 的元素里有
  ``api_key``，连指纹都不打印（指纹同样可被当作泄露面），只打 ``providers=<n> 项``；
  ``*.api_key`` 一律打 ``***``；
- 读一律 ``encoding="utf-8-sig"``（生产配置**确实带 BOM**；``json.load(open(p))``
  会抛 ``Unexpected UTF-8 BOM``，这是 v1.5.0 验收踩过的坑）；写回**保留 BOM 形态**
  （源有 BOM 就写 BOM，源没有就不写）+ ``indent=2`` + 末尾换行 + ``0600`` +
  同目录 ``tempfile`` + ``os.replace`` 原子替换；
- 叶子顺序 = ``_conf_schema.json`` 的声明顺序（由 ``core.config_map`` 承载），
  避免核心因顺序变化白写一次配置；
- ``search.providers`` 整值当**不透明叶子**搬运，**不展开**；路径段数 > 2 一律拒写；
- 幂等：已是 7 组 54 叶 ⇒ ``--apply`` **不覆盖任何已有值**，只在缺叶时补默认；
  内容无变化时不再替换文件（``idempotent=1``）；
- 未知键默认**拒绝写盘**（退出码 2 并列出键名），``--allow-unknown`` 才保留并继续；
- 已知旧形态全部可吃：**扁平 v3（25 顶层键）** -> 分组 v4（45 叶）-> v5（54 叶），
  链式迁移复用同一张映射表（``core.config_map.KEY_MAP``），不另写一份；
- 值类型不符（``"30"`` / ``"true"`` / ``"off"`` 这类字符串）在写盘前按叶类型归一
  （``int`` / ``bool``），归一动作只打印**路径名与类型方向**，不打印值；
- 本工具**不 import astrbot**（只 import ``core.config_map`` 与 ``core`` 的纯函数），
  因此无核心环境（CI / 本机）也能跑。

输出最后一行是机器可读结论（派单核对用）：
``RESULT mode=<dry-run|apply|verify|restore|strip> keys=<n> groups=<n> new=<k> unknown=<m> idempotent=<0|1> rc=<0|2|3>``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config_map import (  # noqa: E402
    CONFIG_V5_NEW_PATHS,
    EFFECT_LABELS,
    GROUPS,
    KEY_MAP,
    LEGACY_PATHS,
    LEAF_ORDER,
    LEAF_TYPES,
    default_of,
    effect_of,
)

BOM = "utf-8-sig"

#: 输出顺序 = ``core.config_map.GROUPS``（7 组）。
#: 必须**派生**，不能另写一份：audit §7.4-① 的坑就是"只给 KEY_MAP 加了新组、
#: 组顺序表还是硬编码的旧 4 组，``nest()`` 静默丢整组"。
GROUP_ORDER: tuple[str, ...] = tuple(GROUPS)

#: 路径 -> 旧扁平键（25 条）；纯新增路径（29 条）不在表里。
_PATH_TO_FLAT: dict[str, str] = {p: v for p, v in KEY_MAP.items() if v is not None}
_FLAT_TO_PATH: dict[str, str] = {v: p for p, v in _PATH_TO_FLAT.items()}

#: 全部当前路径（54 条，声明顺序）。
V5_PATHS: tuple[str, ...] = tuple(KEY_MAP)

#: v1.7.0 新增的 9 条（必须补默认值的叶）。
NEW_PATHS: tuple[str, ...] = tuple(CONFIG_V5_NEW_PATHS)

#: 迁移基线（45 条）：``--verify --baseline`` 只看这些路径的值。
BASELINE_PATHS: tuple[str, ...] = tuple(LEGACY_PATHS)

#: 值指纹不算值的配置叶（元素里含 api_key，指纹同样算泄露面）。
_OPAQUE_VALUE_PATHS: frozenset[str] = frozenset({"search.providers"})

#: 退出码（与 v3/v4 迁移器同口径）。
RC_OK = 0
RC_REFUSE = 2
RC_MISMATCH = 3

#: 值类型不符时的归一方向（只有 int / bool 两类需要归一）。
_INT_TYPES = frozenset({"int"})
_BOOL_TYPES = frozenset({"bool"})

#: 归一失败（保留原值）的哨兵。
_NO_COERCE = object()

#: bool 字符串的假值集合 —— 与 ``core.as_bool`` **逐字相同**（见
#: ``tests/test_migrate_config_v5.py::test_coercion_matches_core_helpers``）。
_BOOL_FALSE_STRS = frozenset({"", "false", "0", "off", "no"})


class MigrationError(RuntimeError):
    """迁移前置条件不满足（未知键、结构超深、根不是对象等）。"""


# --------------------------------------------------------------------
# 纯函数（可单测；全部不打印值）
# --------------------------------------------------------------------


def load_config(path: Path) -> dict:
    """读配置（``utf-8-sig``，剥 BOM）。"""
    return json.loads(Path(path).read_text(encoding=BOM))


def has_bom(path: Path) -> bool:
    """文件首 3 字节是否 UTF-8 BOM（``EF BB BF``）。

    写回时按这个结果决定是否写 BOM —— 生产配置带 BOM，必须原样保住。
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def flatten(config: dict) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """把扁平/分组（可混合）结构归一成 ``路径 -> 值``；同时收集未知项。

    - 分组形态：``{"browser": {"type": "chromium"}}`` -> ``browser.type``；
    - 扁平形态（v3）：``{"browser_type": "chromium"}`` -> ``browser.type``；
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
    """按「组顺序（GROUP_ORDER）-> schema 声明序（LEAF_ORDER）-> KEY_MAP 兜底」排路径。"""
    paths: list[str] = []
    seen: set[str] = set()
    for group in GROUP_ORDER:
        for leaf in LEAF_ORDER.get(group, ()):
            path = f"{group}.{leaf}"
            if path in KEY_MAP and path not in seen:
                paths.append(path)
                seen.add(path)
    # 顺序表落后时补齐剩余路径（不让"顺序表"决定"搬哪些键"）。
    for path in KEY_MAP:
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def nest(canonical: dict[str, Any]) -> dict[str, Any]:
    """``路径 -> 值`` -> 按组顺序的嵌套结构；叶子顺序与 ``_conf_schema.json`` 对齐。"""
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


def _coerce_int(value: Any) -> Any:
    """把值静默转成 int；做不到就返回 :data:`_NO_COERCE`。

    语义对齐 ``core.as_int`` 的**成功路径**（``int(value)``），但**不记日志**：
    ``core.as_int`` 的 warning 会把值本身打进 stderr（``配置项数值非法（%r）``），
    而本工具的不变量是"任何模式都不打印配置值"（值可能是搜索 API key）。
    空串/None/布尔不猜（返回哨兵，保留原值，运行时读取方自会回退默认）。
    """
    if isinstance(value, bool) or value is None or value == "":
        return _NO_COERCE
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return _NO_COERCE


def _coerce_bool(value: Any) -> Any:
    """把值静默转成 bool；做不到就返回 :data:`_NO_COERCE`。

    字符串归一规则与 ``core.as_bool`` 逐字相同（``""/"false"/"0"/"off"/"no"``
    -> False，其余字符串 -> True）；数值按 Python 真值语义；容器类型不猜。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _BOOL_FALSE_STRS
    if isinstance(value, (int, float)):
        return bool(value)
    return _NO_COERCE


def coerce_types(canonical: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """按叶类型归一 ``int`` / ``bool``（字符串 ``"30"`` / ``"true"`` / ``"off"``）。

    只管类型，不管取值范围（钳制是读取方的责任，区间见
    ``core.config_map.clamp_of``）；归一失败时**保留原值**并记为 uncoercible ——
    运行时读取方本就有"非法值回退默认"的兜底（``core.as_int`` / ``core.as_bool``），
    迁移器不该替运营方删数据。

    Returns:
        ``(归一后, 归一过的路径名, 归一失败的路径名)`` —— 三者都**不含值**。
    """
    out: dict[str, Any] = dict(canonical)
    fixed: list[str] = []
    failed: list[str] = []
    for path, value in canonical.items():
        kind = LEAF_TYPES.get(path)
        if kind in _INT_TYPES:
            if isinstance(value, int) and not isinstance(value, bool):
                continue  # 本来就对
            converted = _coerce_int(value)
        elif kind in _BOOL_TYPES:
            if isinstance(value, bool):
                continue  # 本来就对
            converted = _coerce_bool(value)
        else:
            continue  # string/list/object/text/template_list 不做类型归一
        if converted is _NO_COERCE:
            failed.append(path)
        else:
            out[path] = converted
            fixed.append(path)
    return out, sorted(fixed), sorted(failed)


def migrate(
    config: dict,
    *,
    allow_unknown: bool = False,
    coerce: bool = True,
) -> dict:
    """v3（扁平/分组）或 v4 -> v5（7 组 / 54 叶）。

    - 既有 45 条路径的**值原样搬运**（可选类型归一，但绝不覆盖已有值）；
    - v5 新增的 9 条源里没有 ⇒ 取默认值（``core.config_map`` 的同一份默认）；
    - 已存在的路径**只补不覆盖**（尤其 ``search.providers[*].api_key``）；
    - 未知键默认拒绝；``allow_unknown=True`` 时原样保留（分组键放回组内）。
    """
    canonical, unknown = flatten(config)
    if unknown and not allow_unknown:
        names = ", ".join(sorted(name for name, _ in unknown))
        raise MigrationError(f"unknown keys, refusing to write: {names}")

    filled = dict(canonical)
    if coerce:
        filled, _fixed, _failed = coerce_types(filled)
    for path in NEW_PATHS:
        if path not in filled:
            filled[path] = default_of(path)
    # 缺组的旧叶同样补默认（缺失组 = 迁移源不完整，仍按默认补齐而不是留空洞）
    for path in V5_PATHS:
        if path not in filled:
            filled[path] = default_of(path)

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


def strip_v5(config: dict) -> dict:
    """降级方向：删掉 9 个 v5 新叶，其余（含顺序）原样保留。

    用于"手上只有 v5 配置文件、没有升级前备份"的场景。删完的键集合必须
    ``⊆ v4 schema``，否则旧代码加载时核心会继续删键并落盘。
    """
    if not isinstance(config, dict):
        raise MigrationError("config root must be an object")
    out: dict[str, Any] = {}
    for group, value in config.items():
        if group == "agent":
            continue
        if isinstance(value, dict):
            out[group] = {
                leaf: leaf_value
                for leaf, leaf_value in value.items()
                if f"{group}.{leaf}" not in NEW_PATHS
            }
        else:
            out[group] = value
    return out


def is_migrated(config: dict) -> bool:
    """是否已是完整的 v5 结构（7 组齐全 + 54 条路径全在 + 无扁平旧键 + 无未知键）。

    坑（audit §7.4-③）：只看"无扁平键 + 已存在的组是 dict"会把"缺 3 组的 4 组
    配置"判成已迁移，新增组永远进不了迁移。这里对**缺组**与**缺叶**都敏感。
    """
    if not isinstance(config, dict):
        return False
    if any(key in config for key in _FLAT_TO_PATH):
        return False
    if set(GROUP_ORDER) - set(config):
        return False
    for group in GROUP_ORDER:
        if not isinstance(config.get(group), dict):
            return False
    canonical, unknown = flatten(config)
    if unknown:
        return False
    return all(path in canonical for path in V5_PATHS)


def missing_paths(config: dict, paths: tuple[str, ...] = V5_PATHS) -> list[str]:
    """返回缺失的路径名（迁移源完整性检查；只给名字）。"""
    canonical, _ = flatten(config)
    return sorted(path for path in paths if path not in canonical)


def extra_paths(config: dict, paths: tuple[str, ...] = V5_PATHS) -> list[str]:
    """返回不在目标结构里的已知形态路径名（v5 里的"多余叶"，只给名字）。"""
    canonical, _ = flatten(config)
    return sorted(path for path in canonical if path not in paths)


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
    """
    canonical, unknown = flatten(config)
    deep = [path for path in canonical if len(path.split(".")) != 2]
    deep += [name for name, value in unknown if isinstance(value, dict)]
    return sorted(set(deep))


def fingerprint(value: Any) -> str:
    """值指纹（``sha256(str(value))[:8]``）—— **不打印值**，只用于"值没变"的机械核对。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def value_fingerprint(path: str, value: Any) -> str:
    """按路径给指纹；含密钥的叶换成计数/掩码，绝不哈希值。"""
    if path in _OPAQUE_VALUE_PATHS:
        count = len(value) if isinstance(value, (list, tuple)) else 0
        return f"<{count} 项，值不打印>"
    if path.rsplit(".", 1)[-1] == "api_key":
        return "***"
    return fingerprint(value)


def legacy_fingerprint(config: dict) -> str:
    """45 条基线路径归一化叶值的 sha256（迁移证据，不含值）。

    排序后逐条 ``path=<fp>`` 拼接再哈希：同一份配置在任何形态（扁平 v3 /
    分组 v4 / v5）下都得到同一个指纹 ⇒ 可用来证明"迁移没动旧值"。
    """
    canonical, _unknown = flatten(config)
    h = hashlib.sha256()
    for path in BASELINE_PATHS:
        if path not in canonical:
            h.update(f"{path}=<missing>\n".encode())
            continue
        h.update(f"{path}={value_fingerprint(path, canonical[path])}\n".encode())
    return h.hexdigest()


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


def baseline_report(actual: dict, baseline: dict) -> dict[str, Any]:
    """``--verify --baseline`` 的判定：45 条旧叶值一字不变 + 9 个新叶存在。

    9 个新叶的**值**允许是默认或被运营改过（新键本来就没有"迁移前值"），
    只要求"存在"。
    """
    return {
        "value_diff": diff_values(actual, baseline),
        "legacy_missing": missing_paths(actual, BASELINE_PATHS),
        "new_missing": missing_paths(actual, NEW_PATHS),
        "legacy_fingerprint": legacy_fingerprint(actual),
        "baseline_fingerprint": legacy_fingerprint(baseline),
    }


# --------------------------------------------------------------------
# 文件操作
# --------------------------------------------------------------------


def render(config: dict, *, bom: bool = True) -> bytes:
    """序列化配置（``indent=2`` + 末尾换行 + 可选 BOM）。"""
    text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    return (codecs_bom() + text.encode("utf-8")) if bom else text.encode("utf-8")


def codecs_bom() -> bytes:
    """UTF-8 BOM 的字节形态（``EF BB BF``）。"""
    return b"\xef\xbb\xbf"


def write_atomic(path: Path, payload: bytes) -> None:
    """同目录临时文件 + ``os.replace`` 原子替换；权限 ``0600``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".migrate-v5-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def backup(path: Path, tag: str = "pre-v5") -> Path:
    """同目录备份 ``<name>.bak-<tag>-<YYYYmmddHHMMSS>``，权限 ``0600``。

    同一秒内连续备份（幂等重跑、apply 后紧跟 restore）时**不覆盖**已有备份，
    而是追加 ``-2`` / ``-3`` 后缀：回滚锚点被后一次备份吃掉就等于没有锚点。
    """
    stamp = time.strftime("%Y%m%d%H%M%S")
    target = path.with_name(f"{path.name}.bak-{tag}-{stamp}")
    serial = 2
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{tag}-{stamp}-{serial}")
        serial += 1
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def mode_of(path: Path) -> int:
    """文件权限位（八进制），用于报告 0600 是否被保住。"""
    return stat.S_IMODE(os.stat(path).st_mode)


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_config_v5.py",
        description=(
            "browser_llm 配置迁移 v5（v4/扁平 v3 -> 7 组 54 叶，干跑优先，"
            "任何模式都不打印配置值）"
        ),
        epilog=(
            "升级顺序：--dry-run -> --apply -> 换代码 -> --verify --baseline；"
            "降级顺序：--restore-from <备份>（或 --strip-v5）-> 换代码。"
            "顺序不可换：只做一半会静默丢值（规格 §6.5 的四格矩阵）。"
        ),
    )
    parser.add_argument("--config", required=True, help="插件配置文件路径（必填）")
    parser.add_argument(
        "--to",
        metavar="PATH",
        default=None,
        help="写入/校验的目标文件（默认与 --config 相同；导入时用过 --to，回滚时也要带同一个）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只报告计划（默认；不写盘）")
    mode.add_argument("--apply", action="store_true", help="备份后原子写入 v5")
    mode.add_argument(
        "--verify",
        action="store_true",
        help="与 --against（严格：键+值全等）或 --baseline（只查 45 旧叶值 + 9 新叶存在）比对",
    )
    mode.add_argument(
        "--restore-from",
        "--from-backup",
        dest="restore_from",
        metavar="PATH",
        help="从备份逐字节重建配置（回滚用；之前用过 --to 时须带相同的 --to）",
    )
    parser.add_argument(
        "--strip-v5",
        dest="strip_v5",
        action="store_true",
        help=(
            "降级方向：删掉 9 个 v5 新叶（默认只报计划，配 --apply 才落盘并先备份），"
            "用于手上没有升级前备份的场景"
        ),
    )
    parser.add_argument("--against", metavar="PATH", help="--verify 的严格基线")
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        help="--verify 的旧值基线：只要求 45 条基线路径的值不变，并打印叶值指纹",
    )
    parser.add_argument("--allow-unknown", action="store_true", help="保留未知键并继续")
    return parser


def _mode_name(args: argparse.Namespace) -> str:
    if args.restore_from:
        return "restore"
    if args.verify:
        return "verify"
    if args.strip_v5:
        return "strip"
    if args.apply:
        return "apply"
    return "dry-run"


def _emit_result(
    args: argparse.Namespace,
    *,
    config: dict | None,
    rc: int,
    idempotent: bool = False,
) -> None:
    """打印机器可读结论行（只含计数，不含值）。"""
    canonical, unknown = flatten(config) if isinstance(config, dict) else ({}, [])
    groups = len({path.split(".", 1)[0] for path in canonical})
    new_count = sum(1 for path in NEW_PATHS if path in canonical)
    print(
        f"RESULT mode={_mode_name(args)} keys={len(canonical)} groups={groups} "
        f"new={new_count} unknown={len(unknown)} "
        f"idempotent={1 if idempotent else 0} rc={rc}"
    )


def _print_leaf_fingerprints(config: dict) -> None:
    """``--verify --baseline`` 的逐叶指纹（45 基线叶 + 9 新叶的存在性）。"""
    canonical, _ = flatten(config)
    for path in BASELINE_PATHS:
        if path in canonical:
            print(f"  fp {path}={value_fingerprint(path, canonical[path])}")
        else:
            print(f"  fp {path}=<missing>")
    for path in NEW_PATHS:
        state = "present" if path in canonical else "missing"
        print(f"  fp {path}=<{state}>")


def run(args: argparse.Namespace) -> int:
    path = Path(args.config)
    target = Path(args.to) if args.to else path

    # ---- 回滚：从备份逐字节重建（不校验 schema；回滚不是迁移）----
    if args.restore_from:
        source = Path(args.restore_from)
        if not source.is_file():
            print(f"[ERR] backup not found: {source.name}", file=sys.stderr)
            _emit_result(args, config=None, rc=RC_REFUSE)
            return RC_REFUSE
        try:
            restored = load_config(source)  # 只校验可解析，不打印内容
        except (OSError, ValueError):
            print(f"[ERR] backup not valid json: {source.name}", file=sys.stderr)
            _emit_result(args, config=None, rc=RC_REFUSE)
            return RC_REFUSE
        target.parent.mkdir(parents=True, exist_ok=True)
        safety = backup(target, "pre-restore") if target.is_file() else None
        write_atomic(target, source.read_bytes())
        print(
            f"[OK] restored from {source.name}"
            + (f" (safety={safety.name})" if safety else "")
            + f" mode={mode_of(target):04o} target={target.name}"
        )
        print(
            "[WARN] 回滚必须成对：配置回到 v4 之后还要把插件目录回到 v1.6.x，"
            "否则新代码会把缺的 9 个新叶按默认补回（agent.* 的运营配置丢失）"
        )
        _emit_result(args, config=restored, rc=RC_OK)
        return RC_OK

    if not path.is_file():
        print(f"[ERR] config not found: {path.name}", file=sys.stderr)
        _emit_result(args, config=None, rc=RC_REFUSE)
        return RC_REFUSE

    try:
        current = load_config(path)
    except (OSError, ValueError) as exc:
        print(f"[ERR] config not valid json: {type(exc).__name__}", file=sys.stderr)
        _emit_result(args, config=None, rc=RC_REFUSE)
        return RC_REFUSE

    # ---- 校验 ----
    if args.verify:
        if not args.against and not args.baseline:
            print("[ERR] --verify requires --against or --baseline", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        if not target.is_file():
            print(f"[ERR] target not found: {target.name}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        actual = load_config(target)
        if args.baseline:
            report = baseline_report(actual, load_config(Path(args.baseline)))
            bad = bool(
                report["value_diff"]
                or report["legacy_missing"]
                or report["new_missing"]
                or report["legacy_fingerprint"] != report["baseline_fingerprint"]
            )
            print(
                f"[{'FAIL' if bad else 'OK'}] baseline: "
                f"legacy_missing={report['legacy_missing']} "
                f"new_missing={report['new_missing']} "
                f"value_diff={report['value_diff']} "
                f"legacy_fingerprint={report['legacy_fingerprint']} "
                f"baseline_fingerprint={report['baseline_fingerprint']}"
            )
            print("[INFO] 逐叶值指纹（值本身不打印）:")
            _print_leaf_fingerprints(actual)
            rc = RC_MISMATCH if bad else RC_OK
            _emit_result(args, config=actual, rc=rc, idempotent=is_migrated(actual))
            return rc
        key_diff = diff_keys(actual, load_config(Path(args.against)))
        value_diff = diff_values(actual, load_config(Path(args.against)))
        bad = any(key_diff.values()) or bool(value_diff)
        print(
            f"[{'FAIL' if bad else 'OK'}] verify: "
            + json.dumps(dict(key_diff, value_diff=value_diff), ensure_ascii=False)
        )
        rc = RC_MISMATCH if bad else RC_OK
        _emit_result(args, config=actual, rc=rc, idempotent=is_migrated(actual))
        return rc

    # ---- 降级：删掉 9 个 v5 新叶 ----
    if args.strip_v5:
        try:
            stripped = strip_v5(current)
        except MigrationError as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        canonical_now, _unknown_now = flatten(current)
        removed = sorted(path for path in NEW_PATHS if path in canonical_now)
        remaining = set(flatten(stripped)[0])
        still_new = sorted(path for path in NEW_PATHS if path in remaining)
        print(f"[PLAN] strip: removed={removed} new_remaining={still_new}")
        if not removed:
            print("[OK] nothing to strip (already v4-shaped)")
            _emit_result(args, config=current, rc=RC_OK, idempotent=True)
            return RC_OK
        if not args.apply:
            print("[DRY-RUN] no file written (加 --apply 才落盘)")
            _emit_result(args, config=stripped, rc=RC_OK)
            return RC_OK
        saved = backup(target, "pre-v5")
        write_atomic(target, render(stripped, bom=has_bom(path)))
        print(
            f"[OK] stripped (backup={saved.name} mode={mode_of(target):04o} "
            f"target={target.name})"
        )
        print("[WARN] 降级必须成对：配置回退后要把插件目录回到 v1.6.x")
        _emit_result(args, config=stripped, rc=RC_OK)
        return RC_OK

    # ---- 迁移（默认干跑）----
    if not args.allow_unknown:
        deep = structure_too_deep(current)
        if deep:
            print(f"[ERR] 结构层级超过 2 段，拒绝写盘: {deep}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE

    canonical_now, unknown_now = flatten(current)
    try:
        migrated = migrate(current, allow_unknown=args.allow_unknown)
    except MigrationError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        _emit_result(args, config=current, rc=RC_REFUSE)
        return RC_REFUSE

    canonical_new, _ = flatten(migrated)
    will_add = sorted(path for path in canonical_new if path not in canonical_now)
    _coerced, fixed, failed = coerce_types(canonical_now)

    print(
        f"[PLAN] read: keys={len(canonical_now)} groups=[{', '.join(group_summary(current))}] "
        f"unknown={len(unknown_now)}"
    )
    print(
        f"[PLAN] target: keys={len(canonical_new)} groups=[{', '.join(group_summary(migrated))}] "
        f"add={will_add}"
    )
    if fixed:
        print(f"[PLAN] type-normalized (只报路径): {fixed}")
    if failed:
        print(f"[WARN] type-uncoercible (保留原值，运行时回退默认): {failed}")
    missing_before = missing_paths(current, BASELINE_PATHS)
    if missing_before:
        print(
            f"[WARN] baseline_missing_before={missing_before} "
            "-> 缺失键/缺失组会按 schema 默认值补齐（核心加载时也会这么做，"
            "这里提前补是为了让补叶发生在备份存在的前提下）"
        )
    print(
        f"[PLAN] baseline_missing_before={missing_before} "
        f"legacy_fingerprint={legacy_fingerprint(current)} "
        f"effects={_effects_summary(will_add)}"
    )

    already = is_migrated(current)
    # BOM 形态跟随**读取源**（生产带 BOM，写回必须仍是 BOM；源没有就不加）。
    payload = render(migrated, bom=has_bom(path))
    try:
        changed = payload != target.read_bytes()
    except OSError:
        changed = True  # --to 指向新文件

    if already:
        print("[OK] already migrated (7 groups / 54 leaves), 只补缺失叶、不覆盖已有值")

    # 幂等 = "本次没有任何值变更"（已是 v5 且字节也不需要重排）。
    idempotent = already and not changed
    if not args.apply:
        print("[DRY-RUN] no file written")
        _emit_result(args, config=migrated, rc=RC_OK, idempotent=idempotent)
        return RC_OK

    target.parent.mkdir(parents=True, exist_ok=True)
    saved = backup(target, "pre-v5") if target.is_file() else None
    if changed:
        write_atomic(target, payload)
        wrote = 1
    else:
        wrote = 0
    print(
        f"[OK] applied (backup={saved.name if saved else 'none'}, wrote={wrote}, "
        f"mode={mode_of(target):04o}, target={target.name})"
    )

    # 写入后自检（只报路径名，不报值）：
    # ① 磁盘 54 叶 == 打算写下去的内容（证明没写歪）；
    # ② 45 条基线路径的值与**源**逐条等值（类型归一过的路径除外，那是有意变更）。
    reread = load_config(target)
    on_disk, _ = flatten(reread)
    mismatched = sorted(
        path for path in V5_PATHS if on_disk.get(path) != canonical_new.get(path)
    )
    coerced_paths = set(fixed)
    value_diff = [
        path
        for path in diff_values(reread, current)
        if path not in coerced_paths
    ]
    legacy_missing_reread = missing_paths(reread, BASELINE_PATHS)
    new_missing_reread = missing_paths(reread, NEW_PATHS)
    if mismatched or value_diff or legacy_missing_reread or new_missing_reread:
        print(
            "[FAIL] post-write verify: "
            + json.dumps(
                {
                    "mismatched_leaves": mismatched,
                    "value_diff": value_diff,
                    "legacy_missing": legacy_missing_reread,
                    "new_missing": new_missing_reread,
                },
                ensure_ascii=False,
            )
        )
        _emit_result(args, config=reread, rc=RC_MISMATCH)
        return RC_MISMATCH
    print(
        "[OK] post-write verify: 45 baseline paths unchanged "
        f"(fingerprint={legacy_fingerprint(reread)}), 9 new paths present"
    )
    _emit_result(args, config=reread, rc=RC_OK, idempotent=idempotent)
    return RC_OK


def _effects_summary(paths: list[str]) -> str:
    """按生效时机归类"将新增的叶"（便于运营判断要不要重启）。"""
    buckets: dict[str, int] = {}
    for path in paths:
        label = EFFECT_LABELS.get(effect_of(path) or "", "unknown")
        buckets[label] = buckets.get(label, 0) + 1
    return ", ".join(f"{label}×{count}" for label, count in sorted(buckets.items())) or "无"


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：任何异常都收敛成退出码，**不打印 traceback**（不变量 10）。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 自己抛的 0（--help）/ 2（参数非法）
        code = exc.code
        return code if isinstance(code, int) else RC_REFUSE
    try:
        return run(args)
    except MigrationError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return RC_REFUSE
    except (OSError, ValueError) as exc:
        print(f"[ERR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_REFUSE


if __name__ == "__main__":
    raise SystemExit(main())
