"""配置键映射与读取视图 —— **v5 结构的单一事实源**。

本模块回答三个问题，且只在这里回答一次（其他地方一律 import 本模块）：

1. **有哪些配置叶**：``LEAVES``（54 条，按 ``_conf_schema.json`` 的声明顺序排列）；
2. **每个叶子的来历**：``since``（哪个版本引入）+ ``flat``（v3 扁平旧键，没有则是纯新增）
   + ``BACK_TRACE``（v5 路径 -> v4 路径的回溯映射，纯新增为 ``None``）；
3. **每个叶子什么时候生效**：``EFFECT_TIMING``（三档，见下），不再是散落在
   ``main.py`` 注释里的口头约定。

结构演进（为什么 v5 是"纯新增"）：

- v3：**扁平** 25 个顶层键（``browser_type`` / ``max_chars`` / …）；
- v4（2026-10-21）：改成 **6 组 / 45 叶**（``browser``/``limits``/``security``/
  ``perception``/``search``/``tools``），25 个扁平键搬进分组同名叶（``KEY_MAP``
  的值就是那 25 条旧键名），``search.*`` 16 条是新功能、没有扁平旧键；
- v1.5.0 / v1.6.0：``tools`` 组 +4（后台模式/预算/截断/引擎），仍是纯新增；
- **v5（v1.7.0）**：``search.mode`` + ``agent`` 组 8 叶 = **+9**，共 **54 叶 / 7 组**。

**v5 一条都不改名、不删除、不搬家**（45/45 原位保留，映射率 100%）。这不是懒，
是成本核算：任何改名都会制造一个"磁盘键集合 ⊄ 代码键集合"的窗口，而核心
``AstrBotConfig.check_config_integrity()`` 在插件 ``__init__`` **之前**就会把
schema 里没有的键删掉并落盘（``astrbot_config.py:113`` / ``:258-266`` / ``:277-279``
/ ``:127-131``），窗口期 = 永久丢值。见规格 §6.5 的不变量 INV-1：

    INV-1：任意时刻 ``键集合(磁盘配置) ⊆ 键集合(当前部署代码的 schema)``。

因此升级顺序写死为**先迁配置、再换代码**（``tools/migrate_config_v5.py``），
降级顺序为**先回滚配置、再回滚代码**；两者都不可只做一半。

生效时机三档（``EFFECT_*``，与规格 §6.2 的"热 / 新建 / 导入"一一对应）：

- ``EFFECT_HOT``：每次工具调用 ``_refresh_config()`` 重读 ⇒ **面板保存后下一次调用**生效；
- ``EFFECT_NEW_CONTEXT``：只影响**之后新建**的浏览器 context（已在用的 context
  不摘除已装的路由拦截 —— 安全方向不因关开关而后退）；
- ``EFFECT_IMPORT``：只在 ``_load_config()``（``__init__`` / 插件重载）时读 ⇒
  运行期改了要**重载插件**（不是重启服务）。

本模块是纯 Python，**不 import astrbot**，可独立单测、也能在没有核心的环境里
被迁移器直接使用（规格 §6.6 不变量 12）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 生效时机（三档，口径见模块 docstring）
# ---------------------------------------------------------------------------

#: 热更新：每次工具调用 ``_refresh_config()`` 重读 ⇒ 面板保存后**下一次调用**生效。
EFFECT_HOT = "hot"
#: 只影响之后**新建**的浏览器 context（已在用的不摘除拦截）。
EFFECT_NEW_CONTEXT = "new_context"
#: 只在 ``_load_config()`` 读 ⇒ 运行期改了要**重载插件**。
EFFECT_IMPORT = "import"

#: 三档的人类可读说明（给文档 / 面板提示 / 迁移器输出用，务必与实现一致）。
EFFECT_LABELS: dict[str, str] = {
    EFFECT_HOT: "下次调用生效（_refresh_config 每次工具调用重读）",
    EFFECT_NEW_CONTEXT: "面板即时保存，但只对之后新建的浏览器会话生效",
    EFFECT_IMPORT: "重载插件（或重启）后生效（仅在 _load_config 读取）",
}

# ---------------------------------------------------------------------------
# 迁移时代（= 该叶子最早出现在哪个版本的结构里）
# ---------------------------------------------------------------------------

SINCE_V3 = "v3"  # 扁平 25 键时代就存在（搬迁进入分组，键名未变）
SINCE_V4 = "v4"  # 2026-10-21 分组化时新增（search.* 16 条）
SINCE_V150 = "v1.5.0"  # 后台任务化新增（tools.* 3 条）
SINCE_V160 = "v1.6.0"  # 双后台引擎新增（tools.background_engine）
SINCE_V170 = "v1.7.0"  # v5 配置结构新增（search.mode + agent.* 8 条 = 9 条）

#: v1.7.0 之前就存在的"迁移基线"版本集合（= v4 的 45 叶）。
_SINCE_LEGACY: frozenset[str] = frozenset(
    {SINCE_V3, SINCE_V4, SINCE_V150, SINCE_V160}
)


@dataclass(frozen=True)
class Leaf:
    """一个配置叶子的完整描述（**唯一声明处**）。

    Attributes:
        group: 组名（= ``_conf_schema.json`` 顶层键）。
        leaf: 叶名（组内键名）。
        type: schema 类型（``string`` / ``int`` / ``bool`` / ``list`` /
            ``object`` / ``text`` / ``template_list``）。
        default: 默认值。``object`` 叶取"各子项默认值组合"（核心
            ``_config_schema_to_default_config`` 就是这么递归出来的）。
            可变对象（list/dict）**不要原地修改**，要副本请用 :func:`default_of`。
        since: 引入版本（:data:`SINCE_V3` 等），用于区分"迁移基线 / v5 新增"。
        effect: 生效时机（:data:`EFFECT_HOT` / :data:`EFFECT_NEW_CONTEXT` /
            :data:`EFFECT_IMPORT`）。
        flat: v3 扁平旧键名；``None`` = 该叶没有扁平旧形态（纯新增）。
        clamp: 读取方应施加的数值钳制区间 ``(low, high)``；``None`` = 不钳制
            （只做类型容错，非法值回退默认）。
        choices: 枚举取值；``None`` = 自由文本/数值。``("",)`` 表示面板初值
            只有一个空选项、运行期由插件动态同步（如 provider 下拉）。
    """

    group: str
    leaf: str
    type: str
    default: Any
    since: str
    effect: str
    flat: str | None = None
    clamp: tuple[int, int] | None = None
    choices: tuple[str, ...] | None = None

    @property
    def path(self) -> str:
        """点号路径（``group.leaf``）。"""
        return f"{self.group}.{self.leaf}"


#: 54 个配置叶，顺序 = ``_conf_schema.json`` 的声明顺序（迁移产物按此写盘，
#: 避免核心把"顺序不一致"当成一次变更再白写一遍配置：
#: ``astrbot_config.py:268-275``）。每个 ``Leaf(...)`` 的三个位置参数依次是
#: **类型 / 默认值 / 引入版本**，随后是 **生效时机**，最后是可选关键字。
LEAVES: tuple[Leaf, ...] = (
    # ---- browser（6，v3 起即有；判据 §6.2） ----
    Leaf(
        "browser",
        "type",
        "string",
        "chromium",
        SINCE_V3,
        EFFECT_IMPORT,
        flat="browser_type",
        choices=("chromium", "firefox", "webkit"),
    ),
    Leaf("browser", "default_url", "string", "https://www.baidu.com", SINCE_V3, EFFECT_HOT, flat="default_url"),
    Leaf(
        "browser",
        "default_search_engine",
        "string",
        "必应搜索",
        SINCE_V3,
        EFFECT_HOT,
        flat="default_search_engine",
        choices=("必应搜索", "百度搜索", "谷歌搜索", "B站搜索"),
    ),
    Leaf("browser", "proxy", "string", "", SINCE_V3, EFFECT_IMPORT, flat="proxy"),
    Leaf("browser", "viewport", "object", {"width": 1280, "height": 800}, SINCE_V3, EFFECT_IMPORT, flat="viewport"),
    Leaf("browser", "timeout", "int", 30, SINCE_V3, EFFECT_HOT, flat="timeout"),
    # ---- limits（7，v3 起即有；判据 §6.2） ----
    Leaf("limits", "max_chars", "int", 4000, SINCE_V3, EFFECT_HOT, flat="max_chars"),
    Leaf("limits", "max_links", "int", 20, SINCE_V3, EFFECT_HOT, flat="max_links"),
    Leaf("limits", "max_pages", "int", 5, SINCE_V3, EFFECT_HOT, flat="max_pages"),
    Leaf("limits", "idle_timeout", "int", 1800, SINCE_V3, EFFECT_HOT, flat="idle_timeout"),
    Leaf("limits", "cache_days", "int", 3, SINCE_V3, EFFECT_HOT, flat="cache_days"),
    Leaf("limits", "agent_max_steps", "int", 70, SINCE_V3, EFFECT_HOT, flat="agent_max_steps"),
    Leaf("limits", "agent_tool_timeout", "int", 1200, SINCE_V3, EFFECT_HOT, flat="agent_tool_timeout"),
    # ---- security（5，v3 起即有；判据 §6.2） ----
    Leaf(
        "security",
        "banned_words",
        "list",
        ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"],
        SINCE_V3,
        EFFECT_HOT,
        flat="banned_words",
    ),
    Leaf("security", "enable_banned_filter", "bool", True, SINCE_V3, EFFECT_HOT, flat="enable_banned_filter"),
    Leaf("security", "block_internal_ip", "bool", True, SINCE_V3, EFFECT_NEW_CONTEXT, flat="block_internal_ip"),
    Leaf("security", "session_whitelist", "list", [], SINCE_V3, EFFECT_HOT, flat="session_whitelist"),
    Leaf("security", "session_blacklist", "list", [], SINCE_V3, EFFECT_HOT, flat="session_blacklist"),
    # ---- perception（7，v3 起即有；v1.7.0 只收窄 3 个 vision 键的语义；判据 §6.2） ----
    Leaf("perception", "enable_screenshot", "bool", True, SINCE_V3, EFFECT_HOT, flat="enable_screenshot"),
    Leaf("perception", "silent_mode", "bool", True, SINCE_V3, EFFECT_HOT, flat="silent_mode"),
    Leaf(
        "perception",
        "page_perception",
        "string",
        "text_image",
        SINCE_V3,
        EFFECT_HOT,
        flat="page_perception",
        choices=("text", "text_image", "image"),
    ),
    Leaf("perception", "perception_rules", "list", [], SINCE_V3, EFFECT_HOT, flat="perception_rules"),
    Leaf(
        "perception",
        "vision_provider_id",
        "string",
        "",
        SINCE_V3,
        EFFECT_HOT,
        flat="vision_provider_id",
        choices=("",),
    ),
    Leaf(
        "perception",
        "vision_prompt",
        "string",
        "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。",
        SINCE_V3,
        EFFECT_HOT,
        flat="vision_prompt",
    ),
    Leaf("perception", "vision_cache_ttl", "int", 60, SINCE_V3, EFFECT_HOT, flat="vision_cache_ttl"),
    # ---- search（17 = v4 的 16 + v1.7.0 的 mode；判据 §6.2） ----
    Leaf("search", "enabled", "bool", False, SINCE_V4, EFFECT_HOT),
    Leaf("search", "providers", "template_list", [], SINCE_V4, EFFECT_HOT),
    Leaf("search", "max_parallel", "int", 3, SINCE_V4, EFFECT_HOT, clamp=(1, 10)),
    Leaf("search", "max_results", "int", 8, SINCE_V4, EFFECT_HOT, clamp=(1, 50)),
    Leaf("search", "max_rounds", "int", 2, SINCE_V4, EFFECT_HOT, clamp=(1, 5)),
    Leaf("search", "provider_timeout", "int", 15, SINCE_V4, EFFECT_HOT, clamp=(3, 60)),
    Leaf("search", "total_timeout", "int", 60, SINCE_V4, EFFECT_HOT, clamp=(5, 300)),
    Leaf("search", "cooldown_seconds", "int", 60, SINCE_V4, EFFECT_HOT, clamp=(0, 3600)),
    Leaf("search", "cache_ttl", "int", 120, SINCE_V4, EFFECT_HOT, clamp=(0, 3600)),
    Leaf("search", "fetch_pages", "bool", False, SINCE_V4, EFFECT_HOT),
    Leaf("search", "fetch_top_n", "int", 3, SINCE_V4, EFFECT_HOT, clamp=(0, 5)),
    Leaf("search", "fetch_timeout", "int", 12, SINCE_V4, EFFECT_HOT, clamp=(3, 60)),
    Leaf("search", "fetch_max_chars", "int", 2000, SINCE_V4, EFFECT_HOT, clamp=(200, 20000)),
    Leaf("search", "region", "string", "", SINCE_V4, EFFECT_HOT),
    Leaf("search", "safesearch", "string", "moderate", SINCE_V4, EFFECT_HOT, choices=("off", "moderate", "strict")),
    Leaf("search", "proxy", "string", "", SINCE_V4, EFFECT_HOT),
    Leaf("search", "mode", "string", "external", SINCE_V170, EFFECT_HOT, choices=("external", "internal")),
    # ---- tools（4 = v1.5.0 的 3 + v1.6.0 的 engine；判据 §6.2） ----
    Leaf("tools", "background_mode", "string", "off", SINCE_V150, EFFECT_HOT, choices=("off", "browse_only", "all")),
    Leaf("tools", "background_timeout", "int", 240, SINCE_V150, EFFECT_HOT, clamp=(30, 900)),
    Leaf("tools", "background_max_chars", "int", 1500, SINCE_V150, EFFECT_HOT, clamp=(200, 8000)),
    Leaf("tools", "background_engine", "string", "plugin", SINCE_V160, EFFECT_HOT, choices=("plugin", "core")),
    # ---- agent（8，v1.7.0 新增整组；判据 §6.2） ----
    Leaf("agent", "provider_id", "string", "", SINCE_V170, EFFECT_HOT, choices=("",)),
    Leaf(
        "agent",
        "persona_mode",
        "string",
        "default",
        SINCE_V170,
        EFFECT_HOT,
        choices=("default", "current", "custom"),
    ),
    Leaf("agent", "persona_prompt", "text", "", SINCE_V170, EFFECT_HOT),
    Leaf("agent", "task_timeout", "int", 600, SINCE_V170, EFFECT_HOT, clamp=(60, 1800)),
    Leaf("agent", "image_max_side", "int", 1280, SINCE_V170, EFFECT_HOT, clamp=(320, 2048)),
    Leaf("agent", "image_quality", "int", 80, SINCE_V170, EFFECT_HOT, clamp=(40, 95)),
    Leaf("agent", "result_max_chars", "int", 3000, SINCE_V170, EFFECT_HOT, clamp=(500, 20000)),
    Leaf("agent", "ledger_max_lines", "int", 24, SINCE_V170, EFFECT_HOT, clamp=(0, 60)),
)


# ---------------------------------------------------------------------------
# 派生视图（全部由 LEAVES 推出，不许另写一份 —— 否则就是第二个事实源）
# ---------------------------------------------------------------------------

def _ordered_groups() -> tuple[str, ...]:
    """按 ``LEAVES`` 的首现顺序返回组名（= schema 顶层顺序）。"""
    seen: list[str] = []
    for item in LEAVES:
        if item.group not in seen:
            seen.append(item.group)
    return tuple(seen)


#: 7 个目标分组（与 ``_conf_schema.json`` 的顶层键一一对应，顺序一致）。
GROUPS: tuple[str, ...] = _ordered_groups()

#: v4 的 6 个分组（**冻结**：``tools/migrate_config_v4.py`` 与它的测试按这一份
#: 校验"v4 产物只有 6 组"，v5 加了 ``agent`` 组不该让 v4 的历史契约漂移）。
GROUPS_V4: tuple[str, ...] = tuple(group for group in GROUPS if group != "agent")

#: 全部叶路径（54 条，声明顺序）。
LEAF_PATHS: tuple[str, ...] = tuple(item.path for item in LEAVES)

#: 组 -> 组内叶名（声明顺序）。迁移器用它决定写盘顺序，不必再读 schema 文件。
LEAF_ORDER: dict[str, tuple[str, ...]] = {
    group: tuple(item.leaf for item in LEAVES if item.group == group)
    for group in GROUPS
}

#: 路径 -> schema 类型。
LEAF_TYPES: dict[str, str] = {item.path: item.type for item in LEAVES}

#: 路径 -> 默认值（只读；需要可变副本用 :func:`default_of`）。
LEAF_DEFAULTS: dict[str, Any] = {item.path: item.default for item in LEAVES}

#: 路径 -> 旧扁平键。``None`` 表示该路径是纯新增键（没有 v3 扁平旧键）。
KEY_MAP: dict[str, str | None] = {item.path: item.flat for item in LEAVES}

#: 旧扁平键全集（25 条；v3 迁移基线，测试钉住它恒为 25）。
FLAT_KEYS: tuple[str, ...] = tuple(sorted(k for k in KEY_MAP.values() if k))

#: v4 的 45 条路径（迁移基线 / ``--verify --baseline`` 的判定范围）。
V4_LEAF_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES if item.since in _SINCE_LEGACY
)

#: v4 的 45 条路径 -> 旧扁平键（**冻结**的 v4 契约，供 v4 迁移器与它的测试使用）。
KEY_MAP_V4: dict[str, str | None] = {path: KEY_MAP[path] for path in V4_LEAF_PATHS}

#: v5 新增的 9 条路径（``search.mode`` + ``agent.*`` 8 条）。
CONFIG_V5_NEW_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES if item.since == SINCE_V170
)

#: ``agent`` 组 8 条路径（v1.7.0 新增整组）。
AGENT_PATHS: tuple[str, ...] = tuple(item.path for item in LEAVES if item.group == "agent")

#: v5 路径 -> v4 路径的**回溯映射**；v1.7.0 新增的 9 条为 ``None``。
#: v5 一条都没改名，因此 45 条的值就是它自己（这条断言由
#: ``tests/test_v170_no_rename.py::test_v5_no_rename_no_drop`` 钉住）。
BACK_TRACE: dict[str, str | None] = {
    item.path: (item.path if item.since in _SINCE_LEGACY else None) for item in LEAVES
}

#: 路径 -> 生效时机（三档）。
EFFECT_TIMING: dict[str, str] = {item.path: item.effect for item in LEAVES}

#: 迁移基线（45 条）：``--verify --baseline`` 只比这些路径的值，不看 9 个新叶。
LEGACY_PATHS: tuple[str, ...] = V4_LEAF_PATHS

#: v4 纯新增路径（``search.*`` 16 条）。守卫用：它们**不得**从扁平键兜底
#: （缺键 ≠ 空值）。**收窄到 search 前缀**——``tools.*`` 是 v1.5.0 的新键。
V4_ONLY_PATHS: tuple[str, ...] = tuple(
    sorted(
        path
        for path, flat in KEY_MAP.items()
        if flat is None and path.startswith("search.") and path in KEY_MAP_V4
    )
)

#: v1.5.0/v1.6.0 纯新增路径（``tools.*`` 4 条）：同样不做扁平兜底。
V5_ONLY_PATHS: tuple[str, ...] = tuple(
    sorted(path for path, flat in KEY_MAP.items() if flat is None and path.startswith("tools."))
)

#: 所有"无 v3 扁平旧键"的路径（29 条 = 54 − 25）；守卫遍历它，新增键才不会被漏掉。
NO_FLAT_PATHS: tuple[str, ...] = tuple(sorted(path for path, flat in KEY_MAP.items() if flat is None))

_LEAF_BY_PATH: dict[str, Leaf] = {item.path: item for item in LEAVES}

_MISSING = object()


def leaf_meta(path: str) -> Leaf | None:
    """返回某个路径的 :class:`Leaf` 描述（未知路径返回 ``None``）。"""
    return _LEAF_BY_PATH.get(path)


def effect_of(path: str) -> str | None:
    """返回某个路径的生效时机（三档之一；未知路径返回 ``None``）。"""
    return EFFECT_TIMING.get(path)


def effect_label(path: str) -> str:
    """返回某个路径生效时机的中文说明（未知路径返回"未知路径"）。"""
    return EFFECT_LABELS.get(EFFECT_TIMING.get(path, ""), "未知路径")


def back_trace(path: str) -> str | None:
    """v5 路径 -> v4 路径；纯新增（或未知路径）返回 ``None``。

    注意：未知路径与"v5 新增键"同值（``None``）是已知取舍 —— 判断
    "路径是否受支持"必须用 ``path in KEY_MAP`` 成员判断。
    """
    return BACK_TRACE.get(path)


def is_v5_new(path: str) -> bool:
    """是否 v1.7.0 新增的叶（9 条之一）。"""
    return path in CONFIG_V5_NEW_PATHS


def default_of(path: str, fallback: Any = None) -> Any:
    """返回默认值的**独立副本**（list/dict 会被深拷贝，随便改不影响本模块）。

    未知路径返回 ``fallback``。
    """
    if path not in LEAF_DEFAULTS:
        return fallback
    return copy.deepcopy(LEAF_DEFAULTS[path])


def clamp_of(path: str) -> tuple[int, int] | None:
    """返回数值钳制区间；不钳制或未知路径返回 ``None``。"""
    item = _LEAF_BY_PATH.get(path)
    return item.clamp if item else None


def choices_of(path: str) -> tuple[str, ...] | None:
    """返回枚举取值；自由文本/数值或未知路径返回 ``None``。"""
    item = _LEAF_BY_PATH.get(path)
    return item.choices if item else None


def nested_paths() -> list[str]:
    """返回所有受支持的点号路径（排序后）。"""
    return sorted(KEY_MAP)


def flat_key(path: str) -> str | None:
    """点号路径对应的旧扁平键；未知路径**或**纯新增路径返回 None。

    注意：这两种情况同值（``None``）是已知取舍——判断"路径是否受支持"
    必须用 ``path in KEY_MAP`` 成员判断，不能用本函数的返回值。
    """
    return KEY_MAP.get(path)


def schema_leaf(schema: dict | None, path: str) -> dict | None:
    """在 schema 中定位一个叶子定义（同时兼容分组与扁平两种形态）。

    分组 schema：``{"perception": {"type": "object", "items": {...}}}``；
    扁平 schema（过渡期）：``{"vision_provider_id": {...}}``。

    Returns:
        叶子定义 dict；找不到或类型不符时返回 None。
    """
    if not isinstance(schema, dict):
        return None

    node: Any = schema
    for part in path.split("."):
        if not isinstance(node, dict):
            node = _MISSING
            break
        if isinstance(node.get(part), dict):
            node = node[part]
        elif isinstance(node.get("items"), dict) and isinstance(
            node["items"].get(part), dict
        ):
            node = node["items"][part]
        else:
            node = _MISSING
            break
    if isinstance(node, dict):
        return node

    flat = KEY_MAP.get(path)
    if flat and isinstance(schema.get(flat), dict):
        return schema[flat]
    return None


class ConfigView:
    """分层配置读取：嵌套优先 -> 旧扁平键兜底 -> 默认值。

    与 ``dict.get`` 的关键区别：``lookup()`` 返回 ``(value, found)``，
    调用方可以判断"键是否存在"。缺键与空值必须分开处理。
    """

    def __init__(self, cfg: dict | None = None, key_map: dict[str, str] | None = None):
        self._cfg: dict = cfg if isinstance(cfg, dict) else {}
        self._map: dict[str, str] = KEY_MAP if key_map is None else key_map

    def _nested(self, path: str) -> Any:
        cur: Any = self._cfg
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return _MISSING
            cur = cur[part]
        return cur

    def lookup(self, path: str, default: Any = None) -> tuple[Any, bool]:
        """返回 ``(值, 是否命中)``；未命中时值为 ``default``。"""
        value = self._nested(path)
        if value is not _MISSING:
            return value, True
        flat = self._map.get(path)
        if flat is not None and flat in self._cfg:
            return self._cfg[flat], True
        return default, False

    def get(self, path: str, default: Any = None) -> Any:
        return self.lookup(path, default)[0]

    def has(self, path: str) -> bool:
        return self.lookup(path)[1]

    def get_list(self, path: str, default: tuple = ()) -> tuple[list, bool]:
        """列表语义读取：``None`` 归一为空列表，标量归一为单元素列表。"""
        value, found = self.lookup(path, list(default))
        if value is None:
            return [], found
        if isinstance(value, (list, tuple)):
            return list(value), found
        if value == "":
            return [], found
        return [value], found
