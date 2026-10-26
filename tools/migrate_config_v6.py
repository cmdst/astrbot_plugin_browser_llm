#!/usr/bin/env python3
"""browser_llm 配置迁移器（v5 -> v6：54 叶 -> 58 叶；兼容分组 v4 / 扁平 v3 直迁）。

**为什么必须独立于插件运行**：核心 ``AstrBotConfig.__init__`` 会在插件
``__init__`` **之前**按 schema 删除未知键并落盘（``astrbot_config.py:113``、
``:258-266``、``:277-279``、``:127-131``），所以"插件内迁移"一定太晚。
由此得到写死的不变量：

    任意时刻 ``键集合(磁盘配置) ⊆ 键集合(当前部署代码的 schema)``。

**v6 是改名 + 合并 + 删除**（v5 的 54 个键里 **46 个**在 v6 schema 里不存在：
12 rename + 1 move + 7 merge + 8 drop）⇒ **代码先行 = 灾难**：v6 代码一上线，
核心立刻把磁盘上 46 个 v5 键判为 stale 并**删掉落盘**，其中 ``search.providers``
内含 ``api_key`` ⇒ 这是**凭据丢失**，不是"设置没了"。备份文件
（``*.bak-pre-v6-*``）是唯一退路。

因此升级顺序写死为**先迁配置、再换代码**；降级顺序为**先回滚配置、再回滚代码**。
四种"只做一半"的后果见规格 §7.7 的成对矩阵。

五个模式（规格 §7.6；``--dry-run`` 是默认模式）：

=====================  ==================================================  ======  =====
模式                   语义                                                写盘    退出码
=====================  ==================================================  ======  =====
``--dry-run``（默认）   只打印计划：读到 N 叶 / 目标 M 叶 / 将改名·合并·删除的 否      0/2
                       **键名** / 未知键名 / 是否幂等
``--apply``            先备份（同目录 ``*.bak-pre-v6-<TS>``，0600）-> 原子替换 是      0/2/3
``--verify``           ``--against`` 严格（键集合 + 值全等）/ ``--baseline``     否      0/2/3
                       只要求 43 条承接叶值不变 + 15 条新叶存在
``--restore-from``     用备份**逐字节**重建（不校验 schema；回滚用）          是      0/2
``--strip-v6``         删掉 15 条 v6 新叶（无备份时的降级方向）                是*     0/2/3
=====================  ==================================================  ======  =====

``*`` ``--strip-v6`` 默认写盘（先备份）；加 ``--dry-run`` 只报计划。
``--rebuild-from-backup`` / ``--from-backup`` 都是 ``--restore-from`` 的别名。

安全约束（与 v3/v4/v5 迁移器一致，另加注明的两条）：

- **任何模式都不打印配置值**，只打印键名 / 计数 / 分组名 / 值指纹
  （``sha256(str(value))[:8]``）。特例：``search.providers`` 的元素里有
  ``api_key``，**连指纹都不打印**（指纹同样可被当作泄露面），只打
  ``providers=<n> 项``；``*.api_key`` 一律打 ``***``；
- 读一律 ``encoding="utf-8-sig"``（生产配置**确实带 BOM**；``json.load(open(p))``
  会抛 ``Unexpected UTF-8 BOM``，这是 v1.5.0 验收踩过的坑）；写回**保留 BOM 形态**
  （源有 BOM 就写 BOM，源没有就不写）+ ``indent=2`` + 末尾换行 + ``0600`` +
  同目录 ``tempfile`` + ``os.replace`` 原子替换；
- 叶子顺序 = ``_conf_schema.json`` 的声明顺序（由 ``core.config_map`` 承载），
  避免核心因顺序变化白写一次配置（``astrbot_config.py:268-275``）；
- ``search.providers`` 整值当**不透明叶子**搬运，**不展开**；路径段数 > 2 一律拒写；
- 幂等：已是 7 组 58 叶 ⇒ ``--apply`` **不覆盖任何已有值**，只在缺叶时补默认；
  内容无变化时不再替换文件（``idempotent=1``）；
- 未知键默认**拒绝写盘**（退出码 2 并列出键名），``--allow-unknown`` 才保留并继续；
- 已知旧形态全部可吃：**扁平 v3（25 顶层键）** -> 分组 v4（45 叶）->
  v5（54 叶）-> v6（58 叶），链式迁移复用 ``core.config_map`` 的同一张映射表
  （``KEY_MAP_V5`` / ``V5_DISPOSITION``），不另写一份；
- 值类型不符（``"30"`` / ``"true"`` / ``"off"`` 这类字符串）在写盘前按**目标叶**类型
  归一（``int`` / ``bool``），归一动作只打印**路径名与类型方向**，不打印值；
- **干跑允许 ``--config`` 指向空文件或不存在路径**（等价于空配置，只打印计划、
  不写盘、rc=0）；写盘模式一律要求常规文件；
- 本工具**不 import astrbot**（只 import ``core.config_map``），因此无核心环境
  （CI / 本机）也能跑。

输出最后一行是机器可读结论（派单核对用）：
``RESULT mode=<dry-run|apply|verify|restore|strip> keys=<n> groups=<n> new=<k>
renamed=<r> dropped=<d> unknown=<m> idempotent=<0|1> rc=<0|2|3>``
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
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config_map import (  # noqa: E402
    CARRIED_PATHS,
    CONFIG_V6_NEW_PATHS,
    EFFECT_LABELS,
    GROUPS,
    GROUPS_V5,
    KEY_MAP,
    KEY_MAP_V5,
    LEAF_ORDER,
    LEAF_TYPES,
    MIGRATION_DROP,
    MIGRATION_MERGE,
    V5_DISPOSITION,
    V5_SOURCE_TO_V6,
    clamp_of,
    default_of,
    effect_of,
)

BOM = "utf-8-sig"

#: 输出顺序 = ``core.config_map.GROUPS``（7 组）。
#: 必须**派生**，不能另写一份：audit §7.4-① 的坑就是"只给 KEY_MAP 加了新组、
#: 组顺序表还是硬编码的旧 4 组，``nest()`` 静默丢整组"。
GROUP_ORDER: tuple[str, ...] = tuple(GROUPS)

#: v6 的全部路径（58 条，声明顺序）。
V6_PATHS: tuple[str, ...] = tuple(KEY_MAP)

#: v5 路径 -> 旧扁平键（25 条有值）；扁平 v3 配置靠它归一。
_PATH_TO_FLAT_V5: dict[str, str] = {p: v for p, v in KEY_MAP_V5.items() if v is not None}
_FLAT_TO_PATH_V5: dict[str, str] = {v: p for p, v in _PATH_TO_FLAT_V5.items()}

#: 值指纹不算值的配置叶（元素里含 api_key，指纹同样算泄露面）。
_OPAQUE_VALUE_PATHS: frozenset[str] = frozenset({"search.providers"})

#: 退出码（与 v3/v4/v5 迁移器同口径）。
RC_OK = 0
RC_REFUSE = 2
RC_MISMATCH = 3

#: 值类型不符时的归一方向（只有 int / bool 两类需要归一）。
_INT_TYPES = frozenset({"int"})
_BOOL_TYPES = frozenset({"bool"})

#: 归一失败（保留原值）的哨兵。
_NO_COERCE = object()

#: bool 字符串的假值集合 —— 与 ``core.as_bool`` **逐字相同**。
_BOOL_FALSE_STRS = frozenset({"", "false", "0", "off", "no"})

#: ``tools.background_timeout`` 的取值优先级（规格 §7.3 的 merge_value_rule）：
#: ``tools.background_timeout`` > ``agent.task_timeout`` > 常量 300，再夹到 [60,1800]。
#: 注意 ``limits.agent_tool_timeout`` **只作为语义来源**列在 merge_from 里，
#: 不参与取值（它是"单步上限"，语义与整任务预算不同）。
_BUDGET_FALLBACK = 300


class MigrationError(RuntimeError):
    """迁移前置条件不满足（未知键、结构超深、根不是对象等）。"""


# --------------------------------------------------------------------
# 纯函数：读 / 写 / 指纹（全部不打印值；单测直接调这些函数）
# --------------------------------------------------------------------


def read_text(path: Path | str) -> str:
    """按 ``utf-8-sig`` 读文件（**带 BOM 也能读**；这是本插件配置的常态）。"""
    return Path(path).read_text(encoding=BOM)


def read_config(path: Path | str) -> dict:
    """读配置并解析（``utf-8-sig``，剥 BOM）。

    空文件 / 只有空白 ⇒ 视为空配置 ``{}``（干跑时 ``--config /dev/null`` 走这条路）；
    非空但非法 JSON ⇒ 抛 ``ValueError``（调用方转成 rc=2，不打印内容）。
    """
    text = read_text(path)
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise MigrationError("config root must be an object")
    return parsed


def has_bom(path: Path | str) -> bool:
    """文件首 3 字节是否 UTF-8 BOM（``EF BB BF``）。

    写回时按这个结果决定是否写 BOM —— 生产配置带 BOM，必须原样保住。
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def codecs_bom() -> bytes:
    """UTF-8 BOM 的字节形态（``EF BB BF``）。"""
    return b"\xef\xbb\xbf"


def render(config: dict, *, bom: bool = True) -> bytes:
    """序列化配置（``indent=2`` + 末尾换行 + 可选 BOM）。"""
    text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    return (codecs_bom() + text.encode("utf-8")) if bom else text.encode("utf-8")


def write_atomic(path: Path | str, payload: bytes) -> None:
    """同目录临时文件 + ``os.replace`` 原子替换；权限 ``0600``。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".migrate-v6-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def backup(path: Path | str, tag: str = "pre-v6") -> Path:
    """同目录备份 ``<name>.bak-<tag>-<YYYYmmddHHMMSS>``，权限 ``0600``。

    同一秒内连续备份（幂等重跑、apply 后紧跟 restore）时**不覆盖**已有备份，
    而是追加 ``-2`` / ``-3`` 后缀：回滚锚点被后一次备份吃掉就等于没有锚点。
    """
    source = Path(path)
    stamp = time.strftime("%Y%m%d%H%M%S")
    target = source.with_name(f"{source.name}.bak-{tag}-{stamp}")
    serial = 2
    while target.exists():
        target = source.with_name(f"{source.name}.bak-{tag}-{stamp}-{serial}")
        serial += 1
    shutil.copy2(source, target)
    os.chmod(target, 0o600)
    return target


def mode_of(path: Path | str) -> int:
    """文件权限位（整数），用于报告 0600 是否被保住。"""
    return stat.S_IMODE(os.stat(path).st_mode)


def latest_backup(path: Path | str, tag: str = "pre-v6") -> Path | None:
    """同目录里最新的 ``*.bak-<tag>-*`` 备份（``--verify --baseline`` 无参时用）。"""
    source = Path(path)
    candidates = sorted(
        source.parent.glob(f"{source.name}.bak-{tag}-*"),
        key=lambda item: item.stat().st_mtime,
    )
    # R12：只认**非 v6 形态**的备份（v6 备份当基线会让校验恒真）。
    for item in reversed(candidates):
        try:
            data = read_config(item)
        except Exception:  # noqa: BLE001 — 读不了的备份跳过
            continue
        if not is_migrated(data):
            return item
    return None


def fingerprint(value: Any) -> str:
    """值指纹（``sha256(json)[:8]``）—— **不打印值**，只用于"值没变"的机械核对。"""
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


# --------------------------------------------------------------------
# 纯函数：类型归一（只报路径，不报值）
# --------------------------------------------------------------------


def _coerce_int(value: Any) -> Any:
    """把值静默转成 int；做不到就返回 :data:`_NO_COERCE`。

    语义对齐 ``core.as_int`` 的**成功路径**（``int(value)``），但**不记日志**：
    ``core.as_int`` 的 warning 会把值本身打进 stderr，而本工具的不变量是
    "任何模式都不打印配置值"（值可能是搜索 API key）。空串/None/布尔不猜。
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


def _coerce_by_type(value: Any, kind: str | None) -> Any:
    """按目标叶类型归一单个值；不需要归一或归一失败时返回 ``_NO_COERCE``。"""
    if kind in _INT_TYPES:
        if isinstance(value, int) and not isinstance(value, bool):
            return _NO_COERCE
        return _coerce_int(value)
    if kind in _BOOL_TYPES:
        if isinstance(value, bool):
            return _NO_COERCE
        return _coerce_bool(value)
    return _NO_COERCE


# --------------------------------------------------------------------
# 纯函数：结构归一（扁平 v3 / 分组 v4 / v5 / v6 -> 统一视图）
# --------------------------------------------------------------------


def flatten(config: dict) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any]]]:
    """把任意历史形态归一成 ``(v6 路径 -> 值, v5 路径 -> 值, 未知项)``。

    - **v6 分组**：``{"resources": {"max_tabs": 5}}`` -> v6 ``resources.max_tabs``；
    - **v5 分组**：``{"agent": {"provider_id": "x"}}`` -> v5 ``agent.provider_id``；
    - **v4 分组**：v4 的 45 条路径全部是 v5 的子集，按 v5 处理；
    - **扁平 v3**：``{"max_chars": 4000}`` -> v5 ``limits.max_chars``；
    - ``search.providers`` / ``resources.viewport`` 是数组/对象，整值搬运（**不展开**）。

    Returns:
        ``(v6_canonical, v5_canonical, unknown)``；``unknown`` 的键是"没法归一的
        原始路径"（例如 ``legacy_key`` 或 ``browser.unknown_leaf``）。
    """
    if not isinstance(config, dict):
        raise MigrationError("config root must be an object")

    v6: dict[str, Any] = {}
    v5: dict[str, Any] = {}
    unknown: list[tuple[str, Any]] = []
    group_names = set(GROUPS) | set(GROUPS_V5)

    for key, value in config.items():
        if key in group_names and isinstance(value, dict):
            for leaf, leaf_value in value.items():
                path = f"{key}.{leaf}"
                if path in KEY_MAP:
                    v6[path] = leaf_value
                elif path in V5_DISPOSITION:
                    v5[path] = leaf_value
                else:
                    unknown.append((path, leaf_value))
        elif key in _FLAT_TO_PATH_V5:
            v5[_FLAT_TO_PATH_V5[key]] = value
        else:
            unknown.append((str(key), value))
    return v6, v5, unknown


def structure_too_deep(config: dict) -> list[str]:
    """找出超过 2 层的结构（迁移器只支持「组 -> 叶」，遇到就拒写）。

    判定：**未知**的键如果其值是 dict，说明它是"组 -> 子组 -> …"这种嵌套对象
    （合法未知键的典型形态是标量/列表）；已知路径（如 ``resources.viewport``）
    的值本身是 dict，属于 2 段路径的不透明叶子，不算超深。
    """
    v6, v5, unknown = flatten(config)
    deep = [path for path in list(v6) + list(v5) if len(path.split(".")) != 2]
    deep += [name for name, value in unknown if isinstance(value, dict)]
    return sorted(set(deep))


# --------------------------------------------------------------------
# 纯函数：merge 规则（规格 §7.3 的 merge_value_rule，逐条实现）
# --------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    """宽容取整数（不打印值）；取不到返回 ``None``。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_viewport(v5: dict[str, Any]) -> Any:
    """``resources.viewport`` ← ``browser.viewport`` 的 width/height + DPR 缺省 1。"""
    source = v5.get("browser.viewport")
    default = default_of("resources.viewport", {})
    out = {
        "width": default.get("width", 1280),
        "height": default.get("height", 800),
        "device_scale_factor": default.get("device_scale_factor", 1),
    }
    if isinstance(source, dict):
        width = _as_int(source.get("width"))
        height = _as_int(source.get("height"))
        scale = _as_int(source.get("device_scale_factor"))
        if width is not None:
            out["width"] = width
        if height is not None:
            out["height"] = height
        if scale is not None:
            out["device_scale_factor"] = scale
    return out


def _merge_budget(v5: dict[str, Any]) -> Any:
    """``resources.total_budget_s`` ← background_timeout > task_timeout > 300，夹 [60,1800]。"""
    candidates = (
        _as_int(v5.get("tools.background_timeout")),
        _as_int(v5.get("agent.task_timeout")),
    )
    value = next((item for item in candidates if item is not None), _BUDGET_FALLBACK)
    low, high = clamp_of("resources.total_budget_s") or (60, 1800)
    return max(low, min(high, value))


def _merge_provider(v5: dict[str, Any]) -> Any:
    """``model.provider_id`` ← agent.provider_id 非空取它；否则 vision_provider_id；都空为 ""。"""
    for path in ("agent.provider_id", "perception.vision_provider_id"):
        value = v5.get(path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_direct(source_path: str) -> Callable[[dict[str, Any]], Any]:
    """单来源直搬的 merge（``perception.screenshot_fallback`` ← enable_screenshot）。"""

    def _merge(v5: dict[str, Any]) -> Any:
        return v5.get(source_path, default_of(_TARGET_OF_DIRECT[source_path]))

    return _merge


_TARGET_OF_DIRECT: dict[str, str] = {
    "perception.enable_screenshot": "perception.screenshot_fallback",
}

def _invert_bool(value: Any) -> Any:
    """布尔取反（``silent_mode`` -> ``screenshot_to_chat`` 用）；归一失败返回哨兵。"""
    converted = _coerce_bool(value)
    if converted is _NO_COERCE:
        return _NO_COERCE
    return not converted


#: 单来源直搬但**值语义相反**的 rename（规格 §7.4-2 原文："silent_mode →
#: screenshot_to_chat（**取反**）"）。v5 的 silent_mode=True 表示"静默=截图不发到
#: 会话"，v6 的 screenshot_to_chat=False 才是同一件事；直搬会把生产现值
#: silent_mode=true 变成 screenshot_to_chat=true ⇒ 升级后开始往群里发截图。
#: 注意：spec/v175-config-keys.json 里这一条的 ``migration.action`` 写的是
#: ``rename`` 且没有 ``merge_value_rule``，**取反规则只在规格 §7.4-2 的文字里**
#: （本工具按文字实现，并在输出里点名这条；已作为规格勘误上报）。
VALUE_TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "perception.screenshot_to_chat": _invert_bool,
}

#: v6 merge 叶 -> 取值函数（输入是 v5 视图）。
MERGE_RULES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "resources.viewport": _merge_viewport,
    "resources.total_budget_s": _merge_budget,
    "model.provider_id": _merge_provider,
    "perception.screenshot_fallback": _merge_direct("perception.enable_screenshot"),
}


# --------------------------------------------------------------------
# 纯函数：迁移 / 降级 / 判定
# --------------------------------------------------------------------


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


def migrate(
    config: dict,
    *,
    allow_unknown: bool = False,
    coerce: bool = True,
) -> dict:
    """v3（扁平）/ v4 / v5 / v6 -> v6（7 组 / 58 叶）。

    - 承接自 v5 的 43 条路径：**值原样搬运**（rename / move 直搬；merge 按
      :data:`MERGE_RULES` 求值）；已存在的 v6 值**只补不覆盖**（尤其
      ``search.providers[*].api_key``）；
    - v6 新增的 15 条源里没有 ⇒ 取默认值（``core.config_map`` 的同一份默认）；
    - v5 的 8 个 drop 键**不写盘**（v6 schema 里没有它们）；
    - 未知键默认拒绝；``allow_unknown=True`` 时原样保留（分组键放回组内）。
    """
    v6, v5, unknown = flatten(config)
    if unknown and not allow_unknown:
        names = ", ".join(sorted(name for name, _ in unknown))
        raise MigrationError(f"unknown keys, refusing to write: {names}")

    filled: dict[str, Any] = {}

    # ① merge 叶：先按规则从 v5 求值（v6 里已有值的稍后覆盖回来）
    for path, rule in MERGE_RULES.items():
        if any(source in v5 for source in _merge_source_paths(path)):
            filled[path] = rule(v5)

    # ② 单来源直搬（keep / rename / move）
    for source_path, value in v5.items():
        target = V5_SOURCE_TO_V6.get(source_path)
        if not target:
            continue  # drop 键：不写盘
        action, _ = V5_DISPOSITION[source_path]
        if action == MIGRATION_MERGE:
            continue  # 已由 ① 处理
        transform = VALUE_TRANSFORMS.get(target)
        if transform is not None:
            converted = transform(value)
            # 归一失败 ⇒ 保留原值（运行时读取方回退默认），迁移器不替运营方删数据。
            filled[target] = value if converted is _NO_COERCE else converted
            continue
        filled[target] = value

    # ③ v6 形态的输入优先（已是 v6 的配置重跑时必须逐字节不变）
    for path, value in v6.items():
        filled[path] = value

    if coerce:
        fixed: list[str] = []
        failed: list[str] = []
        for path in list(filled):
            converted = _coerce_by_type(filled[path], LEAF_TYPES.get(path))
            if converted is not _NO_COERCE:
                filled[path] = converted
                fixed.append(path)
        _ = (fixed, failed)  # 报告由 coerce_types() 单独算（保持纯函数无副作用）

    # ④ 缺叶补默认（含 v6 新增的 15 条）
    for path in V6_PATHS:
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


def _merge_source_paths(v6_path: str) -> tuple[str, ...]:
    """某个 v6 merge 叶的全部 v5 来源路径（按 v5 声明顺序）。"""
    return tuple(
        path
        for path, (action, target) in V5_DISPOSITION.items()
        if action == MIGRATION_MERGE and target == v6_path
    )


def coerce_types(canonical: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """按**目标叶**类型归一 ``int`` / ``bool``（字符串 ``"30"`` / ``"true"`` / ``"off"``）。

    只管类型，不管取值范围（钳制是读取方的责任，区间见
    ``core.config_map.clamp_of``）；归一失败时**保留原值**并记为 uncoercible ——
    运行时读取方本就有"非法值回退默认"的兜底，迁移器不该替运营方删数据。

    Returns:
        ``(归一后, 归一过的路径名, 归一失败的路径名)`` —— 三者都**不含值**。
    """
    out: dict[str, Any] = dict(canonical)
    fixed: list[str] = []
    failed: list[str] = []
    for path, value in canonical.items():
        kind = LEAF_TYPES.get(path)
        if kind in _INT_TYPES and isinstance(value, int) and not isinstance(value, bool):
            continue  # 本来就对
        if kind in _BOOL_TYPES and isinstance(value, bool):
            continue  # 本来就对
        if kind not in _INT_TYPES and kind not in _BOOL_TYPES:
            continue  # string/list/object/text/template_list 不做类型归一
        converted = _coerce_by_type(value, kind)
        if converted is _NO_COERCE:
            failed.append(path)
        else:
            out[path] = converted
            fixed.append(path)
    return out, sorted(fixed), sorted(failed)


def strip_v6(config: dict) -> dict:
    """降级方向（无备份时用）：删掉 15 条 v6 新叶，其余（含顺序）原样保留。

    **注意这不是 v5 降级**：v6 的 46 个键名与 v5 不同（rename/move/merge），
    删掉新叶之后仍是 v6 键名，只有 v1.7.5 代码能加载。真正的降级请用
    ``--restore-from <备份>``（或先 ``migrate_config_v5.py --restore-from``）。
    """
    if not isinstance(config, dict):
        raise MigrationError("config root must be an object")
    out: dict[str, Any] = {}
    for group, value in config.items():
        if isinstance(value, dict):
            kept = {
                leaf: leaf_value
                for leaf, leaf_value in value.items()
                if f"{group}.{leaf}" not in CONFIG_V6_NEW_PATHS
            }
            if kept:
                out[group] = kept
        else:
            out[group] = value
    return out


def is_migrated(config: dict) -> bool:
    """是否已是完整的 v6 结构（7 组齐全 + 58 条路径全在 + 无 v5 遗留键 + 无未知键）。"""
    if not isinstance(config, dict):
        return False
    if any(key in config for key in _FLAT_TO_PATH_V5):
        return False
    if set(GROUP_ORDER) - set(config):
        return False
    for group in GROUP_ORDER:
        if not isinstance(config.get(group), dict):
            return False
    v6, v5, unknown = flatten(config)
    if v5 or unknown:
        return False
    return all(path in v6 for path in V6_PATHS)


def missing_paths(config: dict, paths: tuple[str, ...] = V6_PATHS) -> list[str]:
    """返回缺失的路径名（迁移源完整性检查；只给名字）。"""
    v6, _v5, _unknown = flatten(config)
    return sorted(path for path in paths if path not in v6)


def extra_paths(config: dict, paths: tuple[str, ...] = V6_PATHS) -> list[str]:
    """返回不在目标结构里的 v6 形态路径名（只给名字）。"""
    v6, _v5, _unknown = flatten(config)
    return sorted(path for path in v6 if path not in paths)


def group_summary(config: dict) -> list[str]:
    """返回分组概况（只给组名与叶数，不给值）。"""
    v6, _v5, unknown = flatten(config)
    out = []
    for group in GROUP_ORDER:
        count = sum(1 for path in v6 if path.split(".", 1)[0] == group)
        if count:
            out.append(f"{group}({count})")
    if unknown:
        out.append(f"unknown({len(unknown)})")
    return out


def diff_keys(left: dict, right: dict) -> dict[str, list[str]]:
    """按键路径比较两份配置（对称差；绝不输出值）。"""
    l6, l5, lunknown = flatten(left)
    r6, r5, runknown = flatten(right)
    return {
        "left_only": sorted(set(l6) - set(r6)),
        "right_only": sorted(set(r6) - set(l6)),
        "v5_left": sorted(l5),
        "v5_right": sorted(r5),
        "unknown_left": sorted(name for name, _ in lunknown),
        "unknown_right": sorted(name for name, _ in runknown),
    }


def diff_values(left: dict, right: dict) -> list[str]:
    """按值比较**共有** v6 路径，返回值不同的路径名（绝不输出值本身）。

    注意：``diff_keys`` 只比键集合，本函数只比共有键的值 —— 所以
    ``--verify --against`` 必须**两个都调**（只比共有键会把"键丢失 + 值未变"
    误判成通过）。
    """
    l6, _l5, _lu = flatten(left)
    r6, _r5, _ru = flatten(right)
    common = set(l6) & set(r6)
    return sorted(path for path in common if l6[path] != r6[path])


def baseline_is_v6(baseline: dict) -> bool:
    """基线是否已经是 v6 形态（R12：这种基线做校验没有意义）。"""
    return bool(is_migrated(baseline))


def baseline_report(actual: dict, baseline: dict) -> dict[str, Any]:
    """``--verify --baseline`` 的判定：**43 条承接叶值不变 + 15 条新叶存在**。

    基线可以是 v5 配置（``.bak-pre-v6-*``）也可以是 v6 配置：都先过一遍
    :func:`migrate` 得到"期望的 v6 值"，再只比 :data:`CARRIED_PATHS`。
    15 条新叶的**值**允许是默认或被运营改过（新键本来就没有"迁移前值"），
    只要求"存在"。
    """
    # R12：基线若已经是 v6，`migrate(v6)==v6` 让本函数恒真 ⇒ 必须在调用方拒绝。
    expected = migrate(baseline, allow_unknown=True)
    a6, _a5, _au = flatten(actual)
    e6, _e5, _eu = flatten(expected)
    return {
        "value_diff": sorted(
            path for path in CARRIED_PATHS if a6.get(path) != e6.get(path)
        ),
        "carried_missing": sorted(path for path in CARRIED_PATHS if path not in a6),
        "new_missing": sorted(path for path in CONFIG_V6_NEW_PATHS if path not in a6),
        "carried_fingerprint": _subset_fingerprint(a6, CARRIED_PATHS),
        "baseline_fingerprint": _subset_fingerprint(e6, CARRIED_PATHS),
    }


def _subset_fingerprint(canonical: dict[str, Any], paths: tuple[str, ...]) -> str:
    """一组路径归一化叶值的 sha256（迁移证据，不含值）。"""
    digest = hashlib.sha256()
    for path in paths:
        if path not in canonical:
            digest.update(f"{path}=<missing>\n".encode())
            continue
        digest.update(f"{path}={value_fingerprint(path, canonical[path])}\n".encode())
    return digest.hexdigest()


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_config_v6.py",
        description=(
            "browser_llm 配置迁移 v6（扁平 v3 / 分组 v4 / v5 -> 7 组 58 叶，"
            "干跑优先，任何模式都不打印配置值）"
        ),
        epilog=(
            "升级顺序：--dry-run -> --apply -> 换代码 -> --verify --baseline；"
            "降级顺序：--restore-from <备份> -> 换代码。"
            "顺序不可换：只做一半会静默丢值（规格 §7.7 的成对矩阵）。"
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
    mode.add_argument("--apply", action="store_true", help="备份后原子写入 v6")
    mode.add_argument(
        "--verify",
        action="store_true",
        help="与 --against（严格：键 + 值全等）或 --baseline（只查 43 旧叶值 + 15 新叶存在）比对",
    )
    mode.add_argument(
        "--restore-from",
        "--rebuild-from-backup",
        "--from-backup",
        dest="restore_from",
        metavar="PATH",
        help="从备份逐字节重建配置（回滚用；之前用过 --to 时须带相同的 --to）",
    )
    parser.add_argument(
        "--strip-v6",
        dest="strip_v6",
        action="store_true",
        help=(
            "降级方向：删掉 15 个 v6 新叶（默认落盘并先备份，配 --dry-run 只报计划）。"
            "注意它产出的是「v6 键名 − 新叶」，只有 v1.7.5 代码能加载；"
            "真正的 v5 降级请用 --restore-from"
        ),
    )
    parser.add_argument("--against", metavar="PATH", help="--verify 的严格基线")
    parser.add_argument(
        "--baseline",
        nargs="?",
        const="__AUTO__",
        metavar="PATH",
        help="--verify 的旧值基线（省略路径 = 用同目录最新的 *.bak-pre-v6-*）",
    )
    parser.add_argument("--allow-unknown", action="store_true", help="保留未知键并继续")
    return parser


def _mode_name(args: argparse.Namespace) -> str:
    if args.restore_from:
        return "restore"
    if args.verify:
        return "verify"
    if args.strip_v6:
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
    v6, v5, unknown = flatten(config) if isinstance(config, dict) else ({}, {}, [])
    all_paths = list(v6) + list(v5)
    groups = len({path.split(".", 1)[0] for path in all_paths})
    new_count = sum(1 for path in CONFIG_V6_NEW_PATHS if path in v6)
    renamed = sum(1 for path in v5 if V5_DISPOSITION[path][1])
    dropped = sum(1 for path in v5 if V5_DISPOSITION[path][0] == MIGRATION_DROP)
    print(
        f"RESULT mode={_mode_name(args)} keys={len(all_paths)} groups={groups} "
        f"new={new_count} renamed={renamed} dropped={dropped} "
        f"unknown={len(unknown)} idempotent={1 if idempotent else 0} rc={rc}"
    )


def _print_leaf_fingerprints(config: dict) -> None:
    """``--verify --baseline`` 的逐叶指纹（43 承接叶 + 15 新叶的存在性）。"""
    v6, _v5, _unknown = flatten(config)
    for path in CARRIED_PATHS:
        if path in v6:
            print(f"  fp {path}={value_fingerprint(path, v6[path])}")
        else:
            print(f"  fp {path}=<missing>")
    for path in CONFIG_V6_NEW_PATHS:
        state = "present" if path in v6 else "missing"
        print(f"  fp {path}=<{state}>")


def _effects_summary(paths: list[str]) -> str:
    """按生效时机归类"将新增的叶"（便于运营判断要不要重载插件）。"""
    buckets: dict[str, int] = {}
    for path in paths:
        label = EFFECT_LABELS.get(effect_of(path) or "", "unknown")
        buckets[label] = buckets.get(label, 0) + 1
    return ", ".join(f"{label}×{count}" for label, count in sorted(buckets.items())) or "无"


def _brief(names: list[str], limit: int = 12) -> str:
    """列表太长时只列前 ``limit`` 项（只含**键名**，不含值）。"""
    if len(names) <= limit:
        return str(names)
    return f"{names[:limit]} ...(+{len(names) - limit})"


def _plan_lines(current: dict, migrated: dict) -> list[str]:
    """干跑计划：只给键名/计数/分组名，**不给值**。"""
    v6_now, v5_now, unknown_now = flatten(current)
    v6_new, _v5_new, _unknown_new = flatten(migrated)
    added = sorted(path for path in v6_new if path not in v6_now)
    renamed = sorted(path for path in v5_now if V5_DISPOSITION[path][1])
    dropped = sorted(path for path in v5_now if V5_DISPOSITION[path][0] == MIGRATION_DROP)
    merged_targets = sorted(
        {V5_DISPOSITION[path][1] for path in v5_now if V5_DISPOSITION[path][0] == MIGRATION_MERGE}
    )
    lines = [
        f"[PLAN] read: keys={len(v6_now)} v5_keys={len(v5_now)} "
        f"groups=[{', '.join(group_summary(current))}] unknown={len(unknown_now)}",
        f"[PLAN] target: keys={len(v6_new)} groups=[{', '.join(group_summary(migrated))}]",
        f"[PLAN] add ({len(added)}): {_brief(added)}",
        f"[PLAN] rename/move ({len(renamed)}): "
        f"{_brief([f'{path}->{V5_DISPOSITION[path][1]}' for path in renamed])}",
        f"[PLAN] merge -> ({len(merged_targets)}): {_brief(merged_targets)}",
        f"[PLAN] drop ({len(dropped)}): {_brief(dropped)}",
    ]
    if added:
        lines.append(f"[PLAN] effects: {_effects_summary(added)}")
    return lines


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
            restored = read_config(source)  # 只校验可解析，不打印内容
        except (OSError, ValueError, MigrationError):
            print(f"[ERR] backup not valid json: {source.name}", file=sys.stderr)
            _emit_result(args, config=None, rc=RC_REFUSE)
            return RC_REFUSE
        if not target.is_file():
            print(f"[ERR] target not a regular file: {target.name}", file=sys.stderr)
            _emit_result(args, config=None, rc=RC_REFUSE)
            return RC_REFUSE
        target.parent.mkdir(parents=True, exist_ok=True)
        safety = backup(target, "pre-restore")
        write_atomic(target, source.read_bytes())
        print(
            f"[OK] restored from {source.name} (safety={safety.name}) "
            f"mode={mode_of(target):04o} target={target.name} "
            f"bom={'yes' if has_bom(target) else 'no'}"
        )
        print(
            "[WARN] 回滚必须成对：配置回到 v5 之后还要把插件目录回到 v1.7.0/v1.6.0 的字节，"
            "否则 v1.7.5 代码会把 46 个 v5 键判为 stale 删掉（含 search.providers 的 api_key）"
        )
        _emit_result(args, config=restored, rc=RC_OK)
        return RC_OK

    # ---- 干跑允许空文件 / 不存在的路径（等价于空配置）；写盘模式要求常规文件 ----
    writes = bool(args.apply or args.strip_v6) and not args.dry_run
    if writes and not target.is_file():
        print(f"[ERR] target not a regular file: {target.name}", file=sys.stderr)
        _emit_result(args, config=None, rc=RC_REFUSE)
        return RC_REFUSE
    if not path.exists() and not args.dry_run:
        print(f"[ERR] config not found: {path.name}", file=sys.stderr)
        _emit_result(args, config=None, rc=RC_REFUSE)
        return RC_REFUSE

    try:
        current = read_config(path) if path.exists() else {}
    except (OSError, ValueError, MigrationError) as exc:
        print(f"[ERR] config not valid json: {type(exc).__name__}", file=sys.stderr)
        _emit_result(args, config=None, rc=RC_REFUSE)
        return RC_REFUSE
    if not current:
        print(
            f"[WARN] config is empty or missing ({path.name}) -> 按空配置出计划"
            "（干跑不写盘；写盘模式要求常规文件）"
        )

    # ---- 校验 ----
    if args.verify:
        if not args.against and args.baseline is None:
            print("[ERR] --verify requires --against or --baseline", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        if not target.is_file():
            print(f"[ERR] target not found: {target.name}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        actual = read_config(target)
        if args.baseline is not None:
            baseline_path = (
                latest_backup(target)
                if args.baseline == "__AUTO__"
                else Path(args.baseline)
            )
            if baseline_path is None or not baseline_path.is_file():
                print(
                    "[ERR] --baseline 无参时需要同目录存在 *.bak-pre-v6-* 备份",
                    file=sys.stderr,
                )
                _emit_result(args, config=actual, rc=RC_REFUSE)
                return RC_REFUSE
            baseline_data = read_config(baseline_path)
            if baseline_is_v6(baseline_data):
                # R12：基线已是 v6 ⇒ 校验恒真，必须明确拒绝（不能静默 OK）
                print(
                    f"[ERR] --baseline {baseline_path.name} 已经是 v6 形态："
                    "用 v6 配置当基线等于校验自己（恒真）。请指定 v5/v4/v3 的备份，"
                    "或改用 --verify --against <期望文件>。",
                    file=sys.stderr,
                )
                _emit_result(args, config=actual, rc=RC_REFUSE)
                return RC_REFUSE
            report = baseline_report(actual, baseline_data)
            bad = bool(
                report["value_diff"]
                or report["carried_missing"]
                or report["new_missing"]
                or report["carried_fingerprint"] != report["baseline_fingerprint"]
            )
            print(
                f"[{'FAIL' if bad else 'OK'}] baseline({baseline_path.name}): "
                f"carried_missing={report['carried_missing']} "
                f"new_missing={report['new_missing']} "
                f"value_diff={report['value_diff']} "
                f"carried_fingerprint={report['carried_fingerprint']} "
                f"baseline_fingerprint={report['baseline_fingerprint']}"
            )
            print("[INFO] 逐叶值指纹（值本身不打印）:")
            _print_leaf_fingerprints(actual)
            rc = RC_MISMATCH if bad else RC_OK
            _emit_result(args, config=actual, rc=rc, idempotent=is_migrated(actual))
            return rc
        other = read_config(Path(args.against))
        key_diff = diff_keys(actual, other)
        value_diff = diff_values(actual, other)
        bad = any(key_diff.values()) or bool(value_diff)
        print(
            f"[{'FAIL' if bad else 'OK'}] verify: "
            + json.dumps(dict(key_diff, value_diff=value_diff), ensure_ascii=False)
        )
        rc = RC_MISMATCH if bad else RC_OK
        _emit_result(args, config=actual, rc=rc, idempotent=is_migrated(actual))
        return rc

    # ---- 降级：删掉 15 个 v6 新叶 ----
    if args.strip_v6:
        try:
            stripped = strip_v6(current)
        except MigrationError as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE
        v6_now, _v5_now, _u_now = flatten(current)
        removed = sorted(path for path in CONFIG_V6_NEW_PATHS if path in v6_now)
        remaining = set(flatten(stripped)[0])
        still_new = sorted(path for path in CONFIG_V6_NEW_PATHS if path in remaining)
        print(f"[PLAN] strip: removed={_brief(removed)} new_remaining={_brief(still_new)}")
        if not removed:
            print("[OK] nothing to strip (already v6-without-new-leaves)")
            _emit_result(args, config=current, rc=RC_OK, idempotent=True)
            return RC_OK
        if args.dry_run:
            print("[DRY-RUN] no file written (去掉 --dry-run 才落盘)")
            _emit_result(args, config=stripped, rc=RC_OK)
            return RC_OK
        saved = backup(target, "pre-strip-v6")
        write_atomic(target, render(stripped, bom=has_bom(path)))
        print(
            f"[OK] stripped (backup={saved.name} mode={mode_of(target):04o} "
            f"target={target.name})"
        )
        print(
            "[WARN] 这不是 v5 降级：剩下的 43 个键仍是 v6 键名，只有 v1.7.5 代码能加载。"
            "要回 v1.7.0/v1.6.0 请用 --restore-from <升级前备份>"
        )
        _emit_result(args, config=stripped, rc=RC_OK)
        return RC_OK

    # ---- 迁移（默认干跑）----
    if not args.allow_unknown:
        deep = structure_too_deep(current)
        if deep:
            print(f"[ERR] 结构层级超过 2 段，拒绝写盘: {deep}", file=sys.stderr)
            _emit_result(args, config=current, rc=RC_REFUSE)
            return RC_REFUSE

    v6_now, v5_now, unknown_now = flatten(current)
    try:
        migrated = migrate(current, allow_unknown=args.allow_unknown)
    except MigrationError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        _emit_result(args, config=current, rc=RC_REFUSE)
        return RC_REFUSE

    v6_new, _v5_new, _unknown_new = flatten(migrated)
    # 类型归一的报告范围：v6 形态的值 + 由 v5 单来源直搬过来的目标值（都按**目标叶**
    # 类型判断）；merge 叶由 MERGE_RULES 自己归一，不在这里重复报。
    pre_coerce: dict[str, Any] = dict(v6_now)
    for source_path, value in v5_now.items():
        action, v6_target = V5_DISPOSITION[source_path]
        if v6_target and action != MIGRATION_MERGE:
            pre_coerce[v6_target] = value
    _coerced, fixed, failed = coerce_types(pre_coerce)
    for line in _plan_lines(current, migrated):
        print(line)
    if fixed:
        print(f"[PLAN] type-normalized (只报路径): {fixed}")
    if failed:
        print(f"[WARN] type-uncoercible (保留原值，运行时回退默认): {failed}")
    missing_before = missing_paths(current)
    if missing_before:
        print(
            f"[WARN] missing_before={_brief(missing_before)} "
            "-> 缺失键/缺失组会按 schema 默认值补齐（核心加载时也会这么做，"
            "这里提前补是为了让补叶发生在备份存在的前提下）"
        )

    already = is_migrated(current)
    # BOM 形态跟随**读取源**（生产带 BOM，写回必须仍是 BOM；源没有就不加）。
    payload = render(migrated, bom=has_bom(path))
    try:
        changed = payload != target.read_bytes()
    except OSError:
        changed = True  # --to 指向新文件

    if already:
        print("[OK] already migrated (7 groups / 58 leaves), 只补缺失叶、不覆盖已有值")

    # 幂等 = "本次没有任何值变更"（已是 v6 且字节也不需要重排）。
    idempotent = already and not changed
    if not args.apply:
        print("[DRY-RUN] no file written")
        _emit_result(args, config=migrated, rc=RC_OK, idempotent=idempotent)
        return RC_OK

    target.parent.mkdir(parents=True, exist_ok=True)
    # R12：**幂等就不备份** —— 否则"再跑一次 apply 确认幂等"会留下一个 v6 内容的
    # `*.bak-pre-v6-*`，随后 `--verify --baseline` 挑中它，43 叶值校验就恒真。
    saved = backup(target, "pre-v6") if (target.is_file() and changed) else None
    if changed:
        write_atomic(target, payload)
        wrote = 1
    else:
        wrote = 0
    print(
        f"[OK] applied (backup={saved.name if saved else 'none'}, wrote={wrote}, "
        f"mode={mode_of(target):04o}, target={target.name}, "
        f"bom={'yes' if has_bom(target) else 'no'})"
    )
    dropped_now = sorted(
        path for path in v5_now if V5_DISPOSITION[path][0] == MIGRATION_DROP
    )
    if saved and dropped_now:
        print(
            f"[INFO] 被删除的 v5 键（值只在备份 {saved.name} 里，恢复用 --restore-from）: "
            f"{_brief(dropped_now)}"
        )

    # 写入后自检（只报路径名，不报值）：
    # ① 磁盘 58 叶 == 打算写下去的内容（证明没写歪）；
    # ② 43 条承接路径的值与**源**（经 v5 视图）逐条等值（类型归一过的路径除外）。
    reread = read_config(target)
    on_disk, _v5_disk, _u_disk = flatten(reread)
    mismatched = sorted(
        path for path in V6_PATHS if on_disk.get(path) != v6_new.get(path)
    )
    coerced_paths = set(fixed)
    value_diff = [
        path
        for path in diff_values(reread, migrated)
        if path not in coerced_paths
    ]
    carried_missing = missing_paths(reread, CARRIED_PATHS)
    new_missing_reread = missing_paths(reread, CONFIG_V6_NEW_PATHS)
    if mismatched or value_diff or carried_missing or new_missing_reread:
        print(
            "[FAIL] post-write verify: "
            + json.dumps(
                {
                    "mismatched_leaves": mismatched,
                    "value_diff": value_diff,
                    "carried_missing": carried_missing,
                    "new_missing": new_missing_reread,
                },
                ensure_ascii=False,
            )
        )
        _emit_result(args, config=reread, rc=RC_MISMATCH)
        return RC_MISMATCH
    print(
        "[OK] post-write verify: 43 carried paths present, 15 new paths present "
        f"(carried_fingerprint={_subset_fingerprint(on_disk, CARRIED_PATHS)})"
    )
    _emit_result(args, config=reread, rc=RC_OK, idempotent=idempotent)
    return RC_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：任何异常都收敛成退出码，**不打印 traceback**。"""
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
