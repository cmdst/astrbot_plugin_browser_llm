"""配置键映射与读取视图 —— **v6 结构的单一事实源**（v1.7.5）。

本模块回答四个问题，且只在这里回答一次（其它地方一律 import 本模块）：

1. **有哪些配置叶**：``LEAVES``（**58 条**，按 ``_conf_schema.json`` 的声明顺序排列）；
2. **每个叶子的来历**：``since``（哪个版本引入）+ ``v5`` / ``migration`` /
   ``merge_from``（v5 → v6 的处置：keep / rename / move / merge / new）；
3. **每个叶子什么时候生效**：``EFFECT_TIMING``（三档，见下）；
4. **旧结构长什么样**：``KEY_MAP_V5``（v5 的 54 叶**冻结视图**）、``KEY_MAP_V4``
   （v4 的 45 叶冻结视图）、``FLAT_KEYS``（v3 的 25 个扁平键）。

结构演进（为什么 v6 是**破坏性**的）：

- v3：**扁平** 25 个顶层键（``browser_type`` / ``max_chars`` / …）；
- v4（2026-10-21）：改成 **6 组 / 45 叶**（``browser``/``limits``/``security``/
  ``perception``/``search``/``tools``），25 个扁平键搬进分组同名叶；
- v1.5.0 / v1.6.0：``tools`` 组 +4（后台模式/预算/截断/引擎）；
- v5（v1.7.0）：``search.mode`` + ``agent`` 组 8 叶 = **+9**，共 **54 叶 / 7 组**；
  v5 **一条都不改名、不删除、不搬家**；
- **v6（v1.7.5）**：**改名 + 合并 + 删除**。v5 的 54 个键里 **46 个**在 v6 schema 里
  不存在（12 rename + 1 move + 7 merge + 8 drop），最终 **58 叶 / 7 组**
  （``resources`` / ``perception`` / ``model`` / ``actions`` / ``search`` /
  ``security`` / ``tools``）。逐条去向见 :data:`V5_DISPOSITION`。

**两种计数口径（写文档必须带单位，禁止裸写"58 叶"）**：

============  ===========================================================  =====  =====
名字           定义                                                          v5     v6
============  ===========================================================  =====  =====
**配置叶**     ``len(KEY_MAP)``：一条 ``Leaf`` 记 1；object 叶按 1 条计          54     58
**JSON 叶子键**  展平 ``_conf_schema.json`` 后不再分支的键；object 叶子项分开计   55     60
============  ===========================================================  =====  =====

两口径的差：v5 = 1、v6 = **2**，全部来自 ``viewport`` 一处
（``browser.viewport`` 1↔2；``resources.viewport`` 1↔3，因为 v6 显式化了
``device_scale_factor``）。其余 6 组两口径逐组相等。别把 ``FLAT_KEYS = 25``
（v3 扁平旧键）当成第三个计数 —— 它既不是配置叶也不是 JSON 叶子键。

**平台事实（v1.7.5 的硬约束）**：核心 ``AstrBotConfig.check_config_integrity()``
在插件 ``__init__`` **之前**就会把 schema 里没有的键删掉并落盘
（``astrbot_config.py:113`` / ``:258-266`` / ``:277-279`` / ``:127-131``）。由此：

    INV-1：任意时刻 ``键集合(磁盘配置) ⊆ 键集合(当前部署代码的 schema)``。

v5 是"纯新增"（核心会自动补叶），**v6 是改名/合并/删除** ⇒ 代码先行会被核心
判 46 个 stale 键并落盘，其中 ``search.providers`` 内含 ``api_key`` ⇒ **凭据丢失**。
因此升级顺序写死为**先迁配置、再换代码**（``tools/migrate_config_v6.py``），
降级顺序为**先回滚配置、再回滚代码**；两者都不可只做一半（成对回滚矩阵见
规格 §7.7）。

生效时机三档（``EFFECT_*``，与规格 §7.3 的"热 / 新建 / 导入"一一对应）：

- ``EFFECT_HOT``：每次工具调用 ``_refresh_config()`` 重读 ⇒ **面板保存后下一次调用**生效；
- ``EFFECT_NEW_CONTEXT``：只影响**之后新建**的浏览器 context（已在用的 context
  不摘除已装的路由拦截 —— 安全方向不因关开关而后退）；
- ``EFFECT_IMPORT``：只在 ``_load_config()``（``__init__`` / 插件重载）时读 ⇒
  运行期改了要**重载插件**（不是重启服务）。

本模块是纯 Python，**不 import astrbot**，可独立单测、也能在没有核心的环境里
被迁移器直接使用（规格 §7.6 安全约束 3）。
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
SINCE_V160 = "v1.6.0"  # 双后台引擎新增（tools.background_engine，v6 删除）
SINCE_V170 = "v1.7.0"  # v5 配置结构新增（search.mode + agent.* 8 条 = 9 条）
SINCE_V175 = "v1.7.5"  # v6 配置结构新增（15 条）

# ---------------------------------------------------------------------------
# v5 → v6 的处置动作（五档；drop 不落在 LEAVES 上，只落在 V5_DISPOSITION）
# ---------------------------------------------------------------------------

MIGRATION_KEEP = "keep"  # 原位保留（组名/叶名都没变）
MIGRATION_RENAME = "rename"  # 改名（含单位入名 *_s）
MIGRATION_MOVE = "move"  # 换组
MIGRATION_MERGE = "merge"  # 并入新键（多对一）
MIGRATION_NEW = "new"  # v6 纯新增（v5 无来源）
MIGRATION_DROP = "drop"  # 无后继（v6 schema 里不存在）


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
        since: 该叶子（沿其 v5 祖先回溯）最早出现的版本，用于区分
            "迁移基线 / v6 新增"。
        effect: 生效时机（三档之一）。
        flat: v3 扁平旧键名；``None`` = 该叶没有扁平旧形态。
        clamp: 读取方应施加的数值钳制区间 ``(low, high)``；``None`` = 不钳制
            （只做类型容错，非法值回退默认）。
        choices: 枚举取值；``None`` = 自由文本/数值。``("",)`` 表示面板初值
            只有一个空选项、运行期由插件动态同步（如 provider 下拉）。
        v5: 主要 v5 来源路径（``merge`` 取第一个来源；``new`` 为 ``None``）。
        migration: v5 → v6 的处置动作（``MIGRATION_*`` 之一）。
        merge_from: ``merge`` 叶的全部 v5 来源路径（其余动作至多一条）。
        children: ``object`` 叶的子项名（JSON 叶子键展开用）；其余叶为空。
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
    v5: str | None = None
    migration: str = MIGRATION_NEW
    merge_from: tuple[str, ...] = ()
    children: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        """点号路径（``group.leaf``）。"""
        return f"{self.group}.{self.leaf}"

    @property
    def json_leaf_keys(self) -> tuple[str, ...]:
        """该叶在展平后的 ``_conf_schema.json`` 里占几个键（object 叶会多于 1）。"""
        if self.children:
            return tuple(f"{self.path}.{child}" for child in self.children)
        return (self.path,)


#: **58 个配置叶**，顺序 = ``_conf_schema.json`` 的声明顺序（迁移产物按此写盘，
#: 避免核心把"顺序不一致"当成一次变更再白写一遍配置：
#: ``astrbot_config.py:268-275``）。位置参数依次是
#: **组 / 叶 / 类型 / 默认值 / 引入版本 / 生效时机**，随后是可选关键字。
LEAVES: tuple[Leaf, ...] = (
    # ---- resources（13：资源部署 + 限制，重做；吸收 v5 的 browser 6 + limits 7） ----
    Leaf(
        "resources",
        "engine",
        "string",
        "chromium",
        SINCE_V3,
        EFFECT_IMPORT,
        flat="browser_type",
        choices=("chromium", "firefox", "webkit"),
        v5="browser.type",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "resources",
        "headless",
        "string",
        "new",
        SINCE_V175,
        EFFECT_IMPORT,
        choices=("new", "headful", "shell"),
    ),
    Leaf(
        "resources",
        "viewport",
        "object",
        {"width": 1280, "height": 800, "device_scale_factor": 1},
        SINCE_V3,
        EFFECT_IMPORT,
        flat="viewport",
        v5="browser.viewport",
        migration=MIGRATION_MERGE,
        merge_from=("browser.viewport",),
        children=("width", "height", "device_scale_factor"),
    ),
    Leaf(
        "resources",
        "proxy",
        "string",
        "",
        SINCE_V3,
        EFFECT_IMPORT,
        flat="proxy",
        v5="browser.proxy",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "resources",
        "max_tabs",
        "int",
        5,
        SINCE_V3,
        EFFECT_HOT,
        flat="max_pages",
        clamp=(1, 20),
        v5="limits.max_pages",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "resources",
        "idle_timeout_s",
        "int",
        1800,
        SINCE_V3,
        EFFECT_HOT,
        flat="idle_timeout",
        clamp=(60, 86400),
        v5="limits.idle_timeout",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "resources",
        "cache_days",
        "int",
        3,
        SINCE_V3,
        EFFECT_HOT,
        flat="cache_days",
        clamp=(0, 30),
        v5="limits.cache_days",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "resources",
        "max_text_chars",
        "int",
        4000,
        SINCE_V3,
        EFFECT_HOT,
        flat="max_chars",
        clamp=(200, 50000),
        v5="limits.max_chars",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "resources",
        "max_links",
        "int",
        20,
        SINCE_V3,
        EFFECT_HOT,
        flat="max_links",
        clamp=(1, 200),
        v5="limits.max_links",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "resources",
        "max_parallel",
        "int",
        4,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(1, 16),
    ),
    Leaf(
        "resources",
        "total_budget_s",
        "int",
        300,
        SINCE_V3,
        EFFECT_HOT,
        flat="agent_tool_timeout",
        clamp=(60, 1800),
        v5="limits.agent_tool_timeout",
        migration=MIGRATION_MERGE,
        merge_from=(
            "limits.agent_tool_timeout",
            "tools.background_timeout",
            "agent.task_timeout",
        ),
    ),
    Leaf(
        "resources",
        "goto_timeout_s",
        "int",
        15,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(5, 120),
    ),
    Leaf(
        "resources",
        "action_timeout_s",
        "int",
        10,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(2, 60),
    ),
    # ---- perception（6：页面感知重做 = A11y 树 + 文本 + 截图兜底） ----
    Leaf(
        "perception",
        "a11y_max_nodes",
        "int",
        300,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(50, 1000),
    ),
    Leaf(
        "perception",
        "a11y_max_name_chars",
        "int",
        120,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(20, 500),
    ),
    Leaf(
        "perception",
        "screenshot_fallback",
        "bool",
        True,
        SINCE_V3,
        EFFECT_HOT,
        flat="enable_screenshot",
        v5="perception.enable_screenshot",
        migration=MIGRATION_MERGE,
        merge_from=("perception.enable_screenshot",),
    ),
    Leaf(
        "perception",
        "som_max_marks",
        "int",
        60,
        SINCE_V175,
        EFFECT_HOT,
        clamp=(5, 200),
    ),
    Leaf(
        "perception",
        "screenshot_to_chat",
        "bool",
        False,
        SINCE_V3,
        EFFECT_HOT,
        flat="silent_mode",
        v5="perception.silent_mode",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "perception",
        "rules",
        "list",
        [],
        SINCE_V3,
        EFFECT_HOT,
        flat="perception_rules",
        v5="perception.perception_rules",
        migration=MIGRATION_RENAME,
    ),
    # ---- model（7：视觉模型与子代理模型合并后的唯一模型组） ----
    Leaf(
        "model",
        "provider_id",
        "string",
        "",
        SINCE_V3,
        EFFECT_HOT,
        flat="vision_provider_id",
        choices=("",),
        v5="perception.vision_provider_id",
        migration=MIGRATION_MERGE,
        merge_from=("perception.vision_provider_id", "agent.provider_id"),
    ),
    Leaf(
        "model",
        "persona_mode",
        "string",
        "default",
        SINCE_V170,
        EFFECT_HOT,
        choices=("default", "current", "custom"),
        v5="agent.persona_mode",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "model",
        "persona_prompt",
        "text",
        "",
        SINCE_V170,
        EFFECT_HOT,
        v5="agent.persona_prompt",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "model",
        "image_max_side",
        "int",
        1280,
        SINCE_V170,
        EFFECT_HOT,
        clamp=(320, 2048),
        v5="agent.image_max_side",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "model",
        "image_quality",
        "int",
        80,
        SINCE_V170,
        EFFECT_HOT,
        clamp=(40, 95),
        v5="agent.image_quality",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "model",
        "result_max_chars",
        "int",
        3000,
        SINCE_V170,
        EFFECT_HOT,
        clamp=(500, 20000),
        v5="agent.result_max_chars",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "model",
        "ledger_max_lines",
        "int",
        24,
        SINCE_V170,
        EFFECT_HOT,
        clamp=(0, 60),
        v5="agent.ledger_max_lines",
        migration=MIGRATION_KEEP,
    ),
    # ---- actions（4：新增组 —— 结构化动作与受信任事件） ----
    Leaf("actions", "trusted_events", "bool", True, SINCE_V175, EFFECT_HOT),
    Leaf("actions", "verify", "bool", True, SINCE_V175, EFFECT_HOT),
    Leaf("actions", "max_retries", "int", 1, SINCE_V175, EFFECT_HOT, clamp=(0, 3)),
    Leaf("actions", "repeat_guard", "int", 3, SINCE_V175, EFFECT_HOT, clamp=(0, 10)),
    # ---- search（17：搜索引擎与搜索工具精简） ----
    Leaf(
        "search",
        "enabled",
        "bool",
        False,
        SINCE_V4,
        EFFECT_HOT,
        v5="search.enabled",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "engine",
        "string",
        "必应搜索",
        SINCE_V3,
        EFFECT_HOT,
        flat="default_search_engine",
        choices=("必应搜索", "百度搜索", "谷歌搜索"),
        v5="browser.default_search_engine",
        migration=MIGRATION_MOVE,
    ),
    Leaf(
        "search",
        "providers",
        "template_list",
        [],
        SINCE_V4,
        EFFECT_HOT,
        v5="search.providers",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "proxy",
        "string",
        "",
        SINCE_V4,
        EFFECT_HOT,
        v5="search.proxy",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "max_parallel",
        "int",
        3,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(1, 10),
        v5="search.max_parallel",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "max_results",
        "int",
        8,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(1, 50),
        v5="search.max_results",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "max_rounds",
        "int",
        2,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(1, 5),
        v5="search.max_rounds",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "provider_timeout_s",
        "int",
        15,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(3, 60),
        v5="search.provider_timeout",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "search",
        "total_timeout_s",
        "int",
        60,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(5, 300),
        v5="search.total_timeout",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "search",
        "cooldown_s",
        "int",
        60,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(0, 3600),
        v5="search.cooldown_seconds",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "search",
        "cache_ttl_s",
        "int",
        120,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(0, 3600),
        v5="search.cache_ttl",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "search",
        "fetch_pages",
        "bool",
        False,
        SINCE_V4,
        EFFECT_HOT,
        v5="search.fetch_pages",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "fetch_top_n",
        "int",
        3,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(0, 5),
        v5="search.fetch_top_n",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "fetch_timeout_s",
        "int",
        12,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(3, 60),
        v5="search.fetch_timeout",
        migration=MIGRATION_RENAME,
    ),
    Leaf(
        "search",
        "fetch_max_chars",
        "int",
        2000,
        SINCE_V4,
        EFFECT_HOT,
        clamp=(200, 20000),
        v5="search.fetch_max_chars",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "region",
        "string",
        "",
        SINCE_V4,
        EFFECT_HOT,
        v5="search.region",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "search",
        "safesearch",
        "string",
        "moderate",
        SINCE_V4,
        EFFECT_HOT,
        choices=("off", "moderate", "strict"),
        v5="search.safesearch",
        migration=MIGRATION_KEEP,
    ),
    # ---- security（5：不变） ----
    Leaf(
        "security",
        "banned_words",
        "list",
        ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"],
        SINCE_V3,
        EFFECT_HOT,
        flat="banned_words",
        v5="security.banned_words",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "security",
        "enable_banned_filter",
        "bool",
        True,
        SINCE_V3,
        EFFECT_HOT,
        flat="enable_banned_filter",
        v5="security.enable_banned_filter",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "security",
        "block_internal_ip",
        "bool",
        True,
        SINCE_V3,
        EFFECT_NEW_CONTEXT,
        flat="block_internal_ip",
        v5="security.block_internal_ip",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "security",
        "session_whitelist",
        "list",
        [],
        SINCE_V3,
        EFFECT_HOT,
        flat="session_whitelist",
        v5="security.session_whitelist",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "security",
        "session_blacklist",
        "list",
        [],
        SINCE_V3,
        EFFECT_HOT,
        flat="session_blacklist",
        v5="security.session_blacklist",
        migration=MIGRATION_KEEP,
    ),
    # ---- tools（6：后台任务的去重 / 隔离 / 收窄） ----
    Leaf(
        "tools",
        "background_mode",
        "string",
        "off",
        SINCE_V150,
        EFFECT_HOT,
        choices=("off", "browse_only", "all"),
        v5="tools.background_mode",
        migration=MIGRATION_KEEP,
    ),
    Leaf(
        "tools",
        "background_max_chars",
        "int",
        1500,
        SINCE_V150,
        EFFECT_HOT,
        clamp=(200, 8000),
        v5="tools.background_max_chars",
        migration=MIGRATION_KEEP,
    ),
    Leaf("tools", "dedupe_window_s", "int", 600, SINCE_V175, EFFECT_HOT, clamp=(0, 3600)),
    Leaf("tools", "isolate_context", "bool", True, SINCE_V175, EFFECT_HOT),
    Leaf("tools", "narrow_tools", "bool", True, SINCE_V175, EFFECT_HOT),
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

#: 全部叶路径（58 条，声明顺序）。
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

#: 路径 -> 旧扁平键。``None`` 表示该路径没有 v3 扁平旧键（纯新增，或 v4 之后才有）。
KEY_MAP: dict[str, str | None] = {item.path: item.flat for item in LEAVES}

#: 路径 -> 生效时机（三档）。
EFFECT_TIMING: dict[str, str] = {item.path: item.effect for item in LEAVES}

#: 路径 -> 主要 v5 来源路径（``new`` 叶为 ``None``）。
BACK_TRACE: dict[str, str | None] = {item.path: item.v5 for item in LEAVES}

#: 路径 -> 处置动作（keep/rename/move/merge/new）。
MIGRATION_OF: dict[str, str] = {item.path: item.migration for item in LEAVES}

#: 路径 -> merge 的全部 v5 来源（非 merge 叶为空元组）。
MERGE_SOURCES: dict[str, tuple[str, ...]] = {item.path: item.merge_from for item in LEAVES}

#: v6 纯新增的 **15** 条路径（``--strip-v6`` 与"新叶存在性"判据用）。
CONFIG_V6_NEW_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES if item.migration == MIGRATION_NEW
)

#: v6 由 v5 **合并**而来的 4 条路径（``--verify --baseline`` 要按 merge 规则比对）。
V6_MERGED_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES if item.migration == MIGRATION_MERGE
)

#: 承接自 v5 的 **43** 条路径（= 58 − 15 新增）；``--verify --baseline`` 的比对范围。
CARRIED_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES if item.migration != MIGRATION_NEW
)

#: 全部 JSON 叶子键（**60** 个，展平 schema 的口径；object 叶的子项分开计）。
JSON_LEAF_KEYS: tuple[str, ...] = tuple(
    key for item in LEAVES for key in item.json_leaf_keys
)

_LEAF_BY_PATH: dict[str, Leaf] = {item.path: item for item in LEAVES}

_MISSING = object()


# ---------------------------------------------------------------------------
# v5 冻结视图（**历史契约，不许跟着 v6 漂移**）
#
# 为什么冻结：``tools/migrate_config_v5.py``（v1.7.0 时代的迁移器）、
# ``tools/migrate_config_v4.py``、``tools/migrate_config_v3.py`` 与它们的历史测试
# 都按"当时的 54 / 45 / 25 条"校验产物；v6 改了当前结构，不该让历史契约漂移。
# v6 的迁移器同样靠 ``KEY_MAP_V5`` 读旧配置（v5 → v6 的映射表就是
# ``V5_DISPOSITION``）。
# ---------------------------------------------------------------------------

#: v5 的 54 个配置叶（**冻结**，声明顺序 = v1.7.0 的 ``_conf_schema.json``）。
LEAVES_V5: tuple[Leaf, ...] = (
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
    Leaf(
        "browser",
        "default_url",
        "string",
        "https://www.baidu.com",
        SINCE_V3,
        EFFECT_HOT,
        flat="default_url",
    ),
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
    Leaf(
        "browser",
        "viewport",
        "object",
        {"width": 1280, "height": 800},
        SINCE_V3,
        EFFECT_IMPORT,
        flat="viewport",
        children=("width", "height"),
    ),
    Leaf("browser", "timeout", "int", 30, SINCE_V3, EFFECT_HOT, flat="timeout"),
    Leaf("limits", "max_chars", "int", 4000, SINCE_V3, EFFECT_HOT, flat="max_chars"),
    Leaf("limits", "max_links", "int", 20, SINCE_V3, EFFECT_HOT, flat="max_links"),
    Leaf("limits", "max_pages", "int", 5, SINCE_V3, EFFECT_HOT, flat="max_pages"),
    Leaf("limits", "idle_timeout", "int", 1800, SINCE_V3, EFFECT_HOT, flat="idle_timeout"),
    Leaf("limits", "cache_days", "int", 3, SINCE_V3, EFFECT_HOT, flat="cache_days"),
    Leaf("limits", "agent_max_steps", "int", 70, SINCE_V3, EFFECT_HOT, flat="agent_max_steps"),
    Leaf(
        "limits",
        "agent_tool_timeout",
        "int",
        1200,
        SINCE_V3,
        EFFECT_HOT,
        flat="agent_tool_timeout",
    ),
    Leaf(
        "security",
        "banned_words",
        "list",
        ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"],
        SINCE_V3,
        EFFECT_HOT,
        flat="banned_words",
    ),
    Leaf(
        "security",
        "enable_banned_filter",
        "bool",
        True,
        SINCE_V3,
        EFFECT_HOT,
        flat="enable_banned_filter",
    ),
    Leaf(
        "security",
        "block_internal_ip",
        "bool",
        True,
        SINCE_V3,
        EFFECT_NEW_CONTEXT,
        flat="block_internal_ip",
    ),
    Leaf("security", "session_whitelist", "list", [], SINCE_V3, EFFECT_HOT, flat="session_whitelist"),
    Leaf("security", "session_blacklist", "list", [], SINCE_V3, EFFECT_HOT, flat="session_blacklist"),
    Leaf(
        "perception",
        "enable_screenshot",
        "bool",
        True,
        SINCE_V3,
        EFFECT_HOT,
        flat="enable_screenshot",
    ),
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
    Leaf(
        "perception",
        "perception_rules",
        "list",
        [],
        SINCE_V3,
        EFFECT_HOT,
        flat="perception_rules",
    ),
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
    Leaf(
        "search",
        "safesearch",
        "string",
        "moderate",
        SINCE_V4,
        EFFECT_HOT,
        choices=("off", "moderate", "strict"),
    ),
    Leaf("search", "proxy", "string", "", SINCE_V4, EFFECT_HOT),
    Leaf(
        "search",
        "mode",
        "string",
        "external",
        SINCE_V170,
        EFFECT_HOT,
        choices=("external", "internal"),
    ),
    Leaf(
        "tools",
        "background_mode",
        "string",
        "off",
        SINCE_V150,
        EFFECT_HOT,
        choices=("off", "browse_only", "all"),
    ),
    Leaf("tools", "background_timeout", "int", 240, SINCE_V150, EFFECT_HOT, clamp=(30, 900)),
    Leaf("tools", "background_max_chars", "int", 1500, SINCE_V150, EFFECT_HOT, clamp=(200, 8000)),
    Leaf(
        "tools",
        "background_engine",
        "string",
        "plugin",
        SINCE_V160,
        EFFECT_HOT,
        choices=("plugin", "core"),
    ),
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

#: v5 的 7 个分组（冻结，顺序 = v1.7.0 的 schema 顶层顺序）。
GROUPS_V5: tuple[str, ...] = (
    "browser",
    "limits",
    "security",
    "perception",
    "search",
    "tools",
    "agent",
)

#: v5 的 54 条路径（冻结，声明顺序）。
V5_LEAF_PATHS: tuple[str, ...] = tuple(item.path for item in LEAVES_V5)

#: v5 路径 -> 旧扁平键（**冻结**；v6 迁移器读扁平 v3 配置时用它）。
KEY_MAP_V5: dict[str, str | None] = {item.path: item.flat for item in LEAVES_V5}

#: v5 路径 -> 类型（冻结；迁移器按它做类型归一）。
LEAF_TYPES_V5: dict[str, str] = {item.path: item.type for item in LEAVES_V5}

#: v5 路径 -> 默认值（冻结；历史工具与测试用它）。
LEAF_DEFAULTS_V5: dict[str, Any] = {item.path: item.default for item in LEAVES_V5}

#: v5 新增的 9 条路径（冻结：``search.mode`` + ``agent.*`` 8 条）。
CONFIG_V5_NEW_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES_V5 if item.since == SINCE_V170
)

#: v4 的 6 个分组（**冻结**：``tools/migrate_config_v4.py`` 与它的测试按这一份
#: 校验"v4 产物只有 6 组"）。
GROUPS_V4: tuple[str, ...] = GROUPS_V5[:6]

#: v4 的 45 条路径（冻结，迁移基线）。= v5 的 54 条减去 v1.7.0 新增的 9 条
#: （``search.mode`` + ``agent`` 组 8 条）；**不是**只减 ``agent`` 组（那是 46）。
V4_LEAF_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES_V5 if item.since != SINCE_V170
)

#: v4 的 45 条路径 -> 旧扁平键（**冻结**的 v4 契约）。
KEY_MAP_V4: dict[str, str | None] = {path: KEY_MAP_V5[path] for path in V4_LEAF_PATHS}

#: v4 迁移基线（45 条）：``migrate_config_v5.py --verify --baseline`` 只比这些路径。
LEGACY_PATHS: tuple[str, ...] = V4_LEAF_PATHS

#: 旧扁平键全集（25 条；v3 迁移基线，测试钉住它恒为 25）。
FLAT_KEYS: tuple[str, ...] = tuple(sorted(k for k in KEY_MAP_V5.values() if k))

#: v5 的 ``agent`` 组 8 条路径（**冻结视图**）。v6 已把该组并入 ``model``，
#: 这个名字只为历史测试（``test_core_config_map`` / ``test_v170_config_v5_counts``）
#: 保留，**不要**在新代码里用它判断"当前有哪些键"——那要看 ``KEY_MAP``。
AGENT_PATHS: tuple[str, ...] = tuple(
    item.path for item in LEAVES_V5 if item.group == "agent"
)

#: v5 时代的"纯新增"分组（**冻结**，仅供历史测试引用；v6 用 CONFIG_V6_NEW_PATHS）。
V4_ONLY_PATHS: tuple[str, ...] = tuple(
    sorted(p for p, flat in KEY_MAP_V4.items() if flat is None and p.startswith("search."))
)
V5_ONLY_PATHS: tuple[str, ...] = tuple(
    sorted(p for p, flat in KEY_MAP_V5.items() if flat is None and p.startswith("tools."))
)

#: 全部"无 v3 扁平旧键"的 v6 路径（**39** 条 = 58 − 19）。25 个扁平旧键只落到
#: 19 个 v6 叶上：6 个随 drop 键消失（``default_url`` / ``timeout`` /
#: ``agent_max_steps`` / ``page_perception`` / ``vision_prompt`` /
#: ``vision_cache_ttl``），merge 又把 3 个并进 ``total_budget_s``、2 个并进
#: ``model.provider_id``。守卫遍历它，新增键才不会被漏掉。
NO_FLAT_PATHS: tuple[str, ...] = tuple(
    sorted(path for path, flat in KEY_MAP.items() if flat is None)
)


# ---------------------------------------------------------------------------
# v5 → v6 处置表（**54 条一条不漏**；规格 §7.5 与 spec/v175-config-keys.json 同源）
# ---------------------------------------------------------------------------

#: v5 路径 -> ``(处置, v6 路径或 None)``。五桶合计恒为 54：
#: 26 keep + 12 rename + 1 move + 7 merge + 8 drop。
V5_DISPOSITION: dict[str, tuple[str, str | None]] = {
    # ---- keep（26：组名/叶名都没变） ----
    "browser.proxy": (MIGRATION_KEEP, "resources.proxy"),
    "limits.cache_days": (MIGRATION_KEEP, "resources.cache_days"),
    "security.banned_words": (MIGRATION_KEEP, "security.banned_words"),
    "security.enable_banned_filter": (MIGRATION_KEEP, "security.enable_banned_filter"),
    "security.block_internal_ip": (MIGRATION_KEEP, "security.block_internal_ip"),
    "security.session_whitelist": (MIGRATION_KEEP, "security.session_whitelist"),
    "security.session_blacklist": (MIGRATION_KEEP, "security.session_blacklist"),
    "search.enabled": (MIGRATION_KEEP, "search.enabled"),
    "search.providers": (MIGRATION_KEEP, "search.providers"),
    "search.max_parallel": (MIGRATION_KEEP, "search.max_parallel"),
    "search.max_results": (MIGRATION_KEEP, "search.max_results"),
    "search.max_rounds": (MIGRATION_KEEP, "search.max_rounds"),
    "search.fetch_pages": (MIGRATION_KEEP, "search.fetch_pages"),
    "search.fetch_top_n": (MIGRATION_KEEP, "search.fetch_top_n"),
    "search.fetch_max_chars": (MIGRATION_KEEP, "search.fetch_max_chars"),
    "search.region": (MIGRATION_KEEP, "search.region"),
    "search.safesearch": (MIGRATION_KEEP, "search.safesearch"),
    "search.proxy": (MIGRATION_KEEP, "search.proxy"),
    "tools.background_mode": (MIGRATION_KEEP, "tools.background_mode"),
    "tools.background_max_chars": (MIGRATION_KEEP, "tools.background_max_chars"),
    "agent.persona_mode": (MIGRATION_KEEP, "model.persona_mode"),
    "agent.persona_prompt": (MIGRATION_KEEP, "model.persona_prompt"),
    "agent.image_max_side": (MIGRATION_KEEP, "model.image_max_side"),
    "agent.image_quality": (MIGRATION_KEEP, "model.image_quality"),
    "agent.result_max_chars": (MIGRATION_KEEP, "model.result_max_chars"),
    "agent.ledger_max_lines": (MIGRATION_KEEP, "model.ledger_max_lines"),
    # ---- rename（12：改名 / 单位入名） ----
    "browser.type": (MIGRATION_RENAME, "resources.engine"),
    "limits.max_chars": (MIGRATION_RENAME, "resources.max_text_chars"),
    "limits.max_links": (MIGRATION_RENAME, "resources.max_links"),
    "limits.max_pages": (MIGRATION_RENAME, "resources.max_tabs"),
    "limits.idle_timeout": (MIGRATION_RENAME, "resources.idle_timeout_s"),
    "perception.silent_mode": (MIGRATION_RENAME, "perception.screenshot_to_chat"),
    "perception.perception_rules": (MIGRATION_RENAME, "perception.rules"),
    "search.provider_timeout": (MIGRATION_RENAME, "search.provider_timeout_s"),
    "search.total_timeout": (MIGRATION_RENAME, "search.total_timeout_s"),
    "search.cooldown_seconds": (MIGRATION_RENAME, "search.cooldown_s"),
    "search.cache_ttl": (MIGRATION_RENAME, "search.cache_ttl_s"),
    "search.fetch_timeout": (MIGRATION_RENAME, "search.fetch_timeout_s"),
    # ---- move（1：换组） ----
    "browser.default_search_engine": (MIGRATION_MOVE, "search.engine"),
    # ---- merge（7：并入 4 个新键） ----
    "browser.viewport": (MIGRATION_MERGE, "resources.viewport"),
    "limits.agent_tool_timeout": (MIGRATION_MERGE, "resources.total_budget_s"),
    "perception.enable_screenshot": (MIGRATION_MERGE, "perception.screenshot_fallback"),
    "perception.vision_provider_id": (MIGRATION_MERGE, "model.provider_id"),
    "tools.background_timeout": (MIGRATION_MERGE, "resources.total_budget_s"),
    "agent.provider_id": (MIGRATION_MERGE, "model.provider_id"),
    "agent.task_timeout": (MIGRATION_MERGE, "resources.total_budget_s"),
    # ---- drop（8：无后继；**不写盘**，值只在备份里） ----
    "browser.default_url": (MIGRATION_DROP, None),
    "browser.timeout": (MIGRATION_DROP, None),
    "limits.agent_max_steps": (MIGRATION_DROP, None),
    "perception.page_perception": (MIGRATION_DROP, None),
    "perception.vision_prompt": (MIGRATION_DROP, None),
    "perception.vision_cache_ttl": (MIGRATION_DROP, None),
    "search.mode": (MIGRATION_DROP, None),
    "tools.background_engine": (MIGRATION_DROP, None),
}

#: v5 路径 -> v6 路径（46 条：keep 26 + rename 12 + move 1 + merge 7）。
V5_SOURCE_TO_V6: dict[str, str] = {
    path: target for path, (_action, target) in V5_DISPOSITION.items() if target
}

#: v5 的 drop 键（8 条，无后继；迁移后**不得**留在配置里）。
V5_DROPPED_PATHS: tuple[str, ...] = tuple(
    sorted(path for path, (action, _t) in V5_DISPOSITION.items() if action == MIGRATION_DROP)
)

#: 处置五桶的条数（恒为 54；测试钉住算术）。
V5_DISPOSITION_COUNTS: dict[str, int] = {
    action: sum(1 for a, _t in V5_DISPOSITION.values() if a == action)
    for action in (
        MIGRATION_KEEP,
        MIGRATION_RENAME,
        MIGRATION_MOVE,
        MIGRATION_MERGE,
        MIGRATION_DROP,
    )
}


# ---------------------------------------------------------------------------
# 查询函数（全部只读；未知路径一律返回"没有"而不是抛异常）
# ---------------------------------------------------------------------------

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
    """v6 路径 -> 主要 v5 来源路径；``new`` 叶（或未知路径）返回 ``None``。

    注意：未知路径与"v6 新增叶"同值（``None``）是已知取舍 —— 判断
    "路径是否受支持"必须用 ``path in KEY_MAP`` 成员判断。
    """
    return BACK_TRACE.get(path)


def migration_of(path: str) -> str | None:
    """返回某个路径的 v5 → v6 处置动作（keep/rename/move/merge/new）。"""
    return MIGRATION_OF.get(path)


def merge_sources_of(path: str) -> tuple[str, ...]:
    """返回某个 merge 叶的全部 v5 来源路径（非 merge 叶返回空元组）。"""
    return MERGE_SOURCES.get(path, ())


def is_v6_new(path: str) -> bool:
    """是否 v6 新增的叶（15 条之一）。"""
    return path in CONFIG_V6_NEW_PATHS


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


def v6_path_for_v5(v5_path: str) -> str | None:
    """v5 路径 -> v6 路径；drop 键或未知路径返回 ``None``。"""
    return V5_SOURCE_TO_V6.get(v5_path)


def v5_disposition(v5_path: str) -> tuple[str, str | None] | None:
    """v5 路径 -> ``(处置, v6 路径或 None)``；未知 v5 路径返回 ``None``。"""
    return V5_DISPOSITION.get(v5_path)


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
