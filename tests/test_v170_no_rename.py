"""v1.7.0 配置结构 v5 的「不改名、不丢弃」守卫（规格 §6.3 + 判据 §6-2）。

v5 的**风险预算全部花在行为上**（影像原生 / 工具面 / 人格 / 搜索模式），不花在
键名美化上。理由是硬事实链：核心 ``check_config_integrity()`` 会在插件
``__init__`` 之前把 schema 里没有的键删掉并落盘（``astrbot_config.py:113`` /
``:258-266`` / ``:277-279`` / ``:127-131``），所以任何改名/搬家都会制造一个
"磁盘键集合 ⊄ 代码键集合"的窗口，窗口期的值**永久丢失**。

因此本文件把「45 叶原位保留」钉成机械结论：

1. ``V4_SNAPSHOT`` 是 **v1.6.1（``2fc2cdd``）schema 的只读快照**（路径 -> 类型,
   默认值, 枚举）。它是一份**冻结的字节级契约**，不许跟着当前 schema 走 ——
   否则"没改名"这件事就没法证明了；
2. 45 条全部仍在 schema 里、同名、同类型、同默认值、同枚举（0 改名 / 0 删除 /
   0 改类型 / 0 换默认）；
3. 新增恰好 9 条，且这 9 条在 v4 快照里**不存在**（= 纯新增，不是"改了个名的旧键"）；
4. 允许改写的只有**文案**（``description`` / ``hint``），且必须落在白名单里：
   规格 §6.2/§6.3 明确要改写的 3 个 vision 键（语义收窄为 caption 兜底）+ 承载它们的
   ``perception`` 组 hint。别的地方动一个字都红。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.config_map import (
    CONFIG_V5_NEW_PATHS,
    GROUPS,
    KEY_MAP,
    LEAF_TYPES,
    LEAVES,
    LEGACY_PATHS,
    V4_LEAF_PATHS,
    default_of,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA: dict = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

#: v1.6.1（``2fc2cdd``）的 45 叶快照：路径 -> (type, default, options)。
#: **不许改这份表**（改它等于取消守卫）：要改先改规格 §6.3。
V4_SNAPSHOT: dict[str, tuple[str, object, list[str] | None]] = {
    "browser.type": ("string", "chromium", ["chromium", "firefox", "webkit"]),
    "browser.default_url": ("string", "https://www.baidu.com", None),
    "browser.default_search_engine": (
        "string",
        "必应搜索",
        ["必应搜索", "百度搜索", "谷歌搜索", "B站搜索"],
    ),
    "browser.proxy": ("string", "", None),
    "browser.viewport": ("object", {"width": 1280, "height": 800}, None),
    "browser.timeout": ("int", 30, None),
    "limits.max_chars": ("int", 4000, None),
    "limits.max_links": ("int", 20, None),
    "limits.max_pages": ("int", 5, None),
    "limits.idle_timeout": ("int", 1800, None),
    "limits.cache_days": ("int", 3, None),
    "limits.agent_max_steps": ("int", 70, None),
    "limits.agent_tool_timeout": ("int", 1200, None),
    "security.banned_words": (
        "list",
        ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"],
        None,
    ),
    "security.enable_banned_filter": ("bool", True, None),
    "security.block_internal_ip": ("bool", True, None),
    "security.session_whitelist": ("list", [], None),
    "security.session_blacklist": ("list", [], None),
    "perception.enable_screenshot": ("bool", True, None),
    "perception.silent_mode": ("bool", True, None),
    "perception.page_perception": (
        "string",
        "text_image",
        ["text", "text_image", "image"],
    ),
    "perception.perception_rules": ("list", [], None),
    "perception.vision_provider_id": ("string", "", [""]),
    "perception.vision_prompt": (
        "string",
        "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。",
        None,
    ),
    "perception.vision_cache_ttl": ("int", 60, None),
    "search.enabled": ("bool", False, None),
    "search.providers": ("template_list", [], None),
    "search.max_parallel": ("int", 3, None),
    "search.max_results": ("int", 8, None),
    "search.max_rounds": ("int", 2, None),
    "search.provider_timeout": ("int", 15, None),
    "search.total_timeout": ("int", 60, None),
    "search.cooldown_seconds": ("int", 60, None),
    "search.cache_ttl": ("int", 120, None),
    "search.fetch_pages": ("bool", False, None),
    "search.fetch_top_n": ("int", 3, None),
    "search.fetch_timeout": ("int", 12, None),
    "search.fetch_max_chars": ("int", 2000, None),
    "search.region": ("string", "", None),
    "search.safesearch": ("string", "moderate", ["off", "moderate", "strict"]),
    "search.proxy": ("string", "", None),
    "tools.background_mode": ("string", "off", ["off", "browse_only", "all"]),
    "tools.background_timeout": ("int", 240, None),
    "tools.background_max_chars": ("int", 1500, None),
    "tools.background_engine": ("string", "plugin", ["plugin", "core"]),
}

#: 规格 §6.2/§6.3 明确要改写**文案**的叶子（语义收窄，不是改名）。
REWORDED_LEAVES = frozenset(
    {
        "perception.vision_provider_id",
        "perception.vision_prompt",
        "perception.vision_cache_ttl",
    }
)


def schema_leaves() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for group, meta in SCHEMA.items():
        for leaf, definition in meta["items"].items():
            out[f"{group}.{leaf}"] = definition
    return out


def schema_default(definition: dict):
    if "default" in definition:
        return definition["default"]
    assert definition["type"] == "object"
    return {key: sub["default"] for key, sub in definition["items"].items()}


# ---------------------------------------------------------------------------
# 判据 §6-2：映射率 45/45 = 100%，且 54 − 45 = 9
# ---------------------------------------------------------------------------


def test_v5_no_rename_no_drop():
    """``set(旧 45 路径) ⊆ set(KEY_MAP)`` 且 ``len(KEY_MAP) - 45 == 9``。"""
    assert set(V4_SNAPSHOT) == set(V4_LEAF_PATHS) == set(LEGACY_PATHS)
    assert len(V4_SNAPSHOT) == 45
    assert set(V4_SNAPSHOT) <= set(KEY_MAP)
    assert len(KEY_MAP) == 54
    assert len(KEY_MAP) - len(V4_SNAPSHOT) == 9
    assert len(CONFIG_V5_NEW_PATHS) == 9
    assert set(KEY_MAP) == set(V4_SNAPSHOT) | set(CONFIG_V5_NEW_PATHS)
    # 45 条全部仍在 schema 里（"没删"的另一面：schema 是磁盘键集合的上界）
    assert set(V4_SNAPSHOT) <= set(schema_leaves())


def test_forty_five_leaves_keep_type_default_and_choices():
    """0 改名 / 0 改类型 / 0 换默认值 / 0 改枚举（逐条与 v1.6.1 快照比对）。"""
    leaves = schema_leaves()
    problems: list[str] = []
    for path, (kind, default, options) in V4_SNAPSHOT.items():
        definition = leaves.get(path)
        if definition is None:
            problems.append(f"{path}: 叶子被删了")
            continue
        if definition["type"] != kind:
            problems.append(f"{path}: 类型 {kind} -> {definition['type']}")
        if schema_default(definition) != default:
            problems.append(f"{path}: 默认值变了")
        got_options = definition.get("options")
        if (got_options or None) != (options or None):
            problems.append(f"{path}: 枚举变了 {options} -> {got_options}")
        # LEAVES 表必须与 schema 同步（两处漂移会骗过 panel）
        if LEAF_TYPES[path] != kind:
            problems.append(f"{path}: LEAVES 里类型写成 {LEAF_TYPES[path]}")
        if default_of(path) != default:
            problems.append(f"{path}: LEAVES 里默认值写错")
    assert problems == []


def test_nine_new_leaves_are_pure_additions():
    """9 条新叶在 v4 快照里**不存在**（纯新增，不是"旧键换了个名字"）。"""
    leaks = sorted(path for path in CONFIG_V5_NEW_PATHS if path in V4_SNAPSHOT)
    assert leaks == [], f"这些'新叶'在 v1.6.1 里就有，说明是改名/搬家: {leaks}"
    # 新叶必须真的在 schema 里、并且有默认值（核心补叶时用它）
    leaves = schema_leaves()
    for path in CONFIG_V5_NEW_PATHS:
        assert path in leaves, path
        assert default_of(path) is not None or default_of(path) in ("", 0, False), path
    assert sorted(CONFIG_V5_NEW_PATHS) == sorted(
        ["search.mode"]
        + [f"agent.{leaf}" for leaf in SCHEMA["agent"]["items"]]
    )


def test_forty_five_paths_are_present_in_the_leaf_table():
    """``LEAVES`` 里 45 条旧叶的路径与 v4 快照逐条同名（顺序也一致）。"""
    ordered = [item.path for item in LEAVES if item.path in V4_SNAPSHOT]
    assert len(ordered) == 45
    # 组内相对顺序不许变（核心比对顺序，顺序变了会白写一次配置）
    for group in GROUPS:
        v4_order = [path for path in V4_SNAPSHOT if path.split(".", 1)[0] == group]
        now_order = [path for path in ordered if path.split(".", 1)[0] == group]
        assert now_order == v4_order, group


def test_only_the_three_vision_hints_may_be_reworded():
    """文案改动只允许落在规格 §6.2/§6.3 指定的 3 个 vision 键（+ perception 组 hint）。

    这些键的语义在 v1.7.0 **收窄**了（只在 caption 兜底路径生效），面板文案必须
    跟着改 —— 否则运营方会以为它们还是"截图进模型的唯一通道"。其余 42 个键的
    ``description`` 一个字都不许动。
    """
    leaves = schema_leaves()
    # 45 条旧叶的面板标题（= v1.6.1 的 ``description``，逐字冻结）
    v4_descriptions = {
        "browser.type": "浏览器引擎类型",
        "browser.default_url": "默认起始页",
        "browser.default_search_engine": "默认搜索引擎（浏览器 SERP 模板）",
        "browser.proxy": "浏览器代理地址",
        "browser.viewport": "浏览器视口尺寸",
        "browser.timeout": "页面操作超时（秒）",
        "limits.max_chars": "单页正文提取上限（字符）",
        "limits.max_links": "单页链接提取上限",
        "limits.max_pages": "所有会话标签页总数上限",
        "limits.idle_timeout": "会话空闲回收时间（秒）",
        "limits.cache_days": "媒体缓存保留天数",
        "limits.agent_max_steps": "浏览器子代理最大工具调用步数",
        "limits.agent_tool_timeout": "浏览器子代理单次工具调用超时（秒）",
        "security.banned_words": "内容禁词列表",
        "security.enable_banned_filter": "启用内容审核（禁词过滤）",
        "security.block_internal_ip": "拦截内网地址",
        "security.session_whitelist": "会话白名单",
        "security.session_blacklist": "会话黑名单",
        "perception.enable_screenshot": "启用页面截图",
        "perception.silent_mode": "静默模式",
        "perception.page_perception": "页面感知方式",
        "perception.perception_rules": "会话级页面感知规则",
        "perception.vision_provider_id": "多模态识图模型 Provider ID",
        "perception.vision_prompt": "识图提示词模板",
        "perception.vision_cache_ttl": "识图缓存时间（秒）",
        "search.enabled": "启用搜索工具",
        "search.providers": "搜索 API provider 池",
        "search.max_parallel": "单波并发上限",
        "search.max_results": "返回结果条数上限",
        "search.max_rounds": "搜索步数（波次上限）",
        "search.provider_timeout": "单源超时（秒）",
        "search.total_timeout": "单次调用总预算（秒）",
        "search.cooldown_seconds": "失败源冷却（秒）",
        "search.cache_ttl": "结果缓存时间（秒）",
        "search.fetch_pages": "抓取结果正文",
        "search.fetch_top_n": "抓取条数（0-5）",
        "search.fetch_timeout": "单条抓取超时（秒）",
        "search.fetch_max_chars": "抓取正文长度上限（字符）",
        "search.region": "地区（仅 brave）",
        "search.safesearch": "安全搜索（仅 brave）",
        "search.proxy": "搜索与抓取代理",
        "tools.background_mode": "后台模式",
        "tools.background_timeout": "后台任务整任务硬预算（秒）",
        "tools.background_max_chars": "后台结果截断长度（字符）",
        "tools.background_engine": "后台任务引擎",
    }
    assert set(v4_descriptions) == set(V4_SNAPSHOT)
    mismatched = {
        path: leaves[path]["description"]
        for path, title in v4_descriptions.items()
        if path not in REWORDED_LEAVES and leaves[path]["description"] != title
    }
    assert mismatched == {}, f"非白名单叶的面板标题被改了: {mismatched}"
    # 白名单里的 3 个键，务必真的写明了"caption 兜底 / 语义收窄"
    for path in REWORDED_LEAVES:
        hint = leaves[path]["hint"]
        assert ("caption" in hint) or ("语义收窄" in hint), path
    assert "语义收窄" in SCHEMA["perception"]["hint"]
    # 白名单必须是真的"被改写"（防有人把白名单留着但文案没动 → 说明白名单过期了）
    for path in REWORDED_LEAVES:
        assert leaves[path]["hint"] != "", path
