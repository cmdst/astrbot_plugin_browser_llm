"""配置键映射与读取视图。

设计目标：让"新分组路径"与"旧扁平键"在过渡期同时可读，并且能区分
「键存在但没有值（[]/None）」与「键根本不存在」——后者必须 fail-closed，
不能静默退化成 allow-all。

v4（2026-10-21）：新增 ``search`` 组 16 个键（无扁平旧键，映射为 ``None``）。
``GROUPS`` 收敛到**实际有读取点的 5 组**：``async``/``delivery``/``crawler``
三组没有任何键与读取点，声明它们只会骗过"分组已声明"类守卫（audit §7.4-②）。

本模块是纯 Python，不 import astrbot，可独立单测。
"""

from __future__ import annotations

from typing import Any

#: 5 个目标分组（与 ``_conf_schema.json`` 的顶层键一一对应）。
GROUPS: tuple[str, ...] = (
    "browser",
    "limits",
    "security",
    "perception",
    "search",
)

#: 新点号路径 -> 旧扁平键。v4 起值为 ``None`` 表示"该路径是纯新增键，
#: 没有对应的 v3 扁平键"（``search.*`` 16 条）；仍是 25 条旧键映射。
#
# 注意：``data_dir`` 不在本表里。它不是 schema 配置键，而是插件
# initialize() 注入给 BrowserCore 的运行时路径；若将来把它做成配置键，
# tests/test_core_config_readpoints.py 的守卫会变红（因为它会被当成
# "读取了 KEY_MAP 之外的扁平键"），届时需要显式加入映射而不是放行。
KEY_MAP: dict[str, str | None] = {
    # ---- v3 旧键（25 条，一个都不改） ----
    "browser.type": "browser_type",
    "browser.default_url": "default_url",
    "browser.default_search_engine": "default_search_engine",
    "browser.proxy": "proxy",
    "browser.viewport": "viewport",
    "browser.timeout": "timeout",
    "limits.max_chars": "max_chars",
    "limits.max_links": "max_links",
    "limits.max_pages": "max_pages",
    "limits.idle_timeout": "idle_timeout",
    "limits.cache_days": "cache_days",
    "limits.agent_max_steps": "agent_max_steps",
    "limits.agent_tool_timeout": "agent_tool_timeout",
    "security.banned_words": "banned_words",
    "security.enable_banned_filter": "enable_banned_filter",
    "security.block_internal_ip": "block_internal_ip",
    "security.session_whitelist": "session_whitelist",
    "security.session_blacklist": "session_blacklist",
    "perception.page_perception": "page_perception",
    "perception.perception_rules": "perception_rules",
    "perception.enable_screenshot": "enable_screenshot",
    "perception.silent_mode": "silent_mode",
    "perception.vision_provider_id": "vision_provider_id",
    "perception.vision_prompt": "vision_prompt",
    "perception.vision_cache_ttl": "vision_cache_ttl",
    # ---- v4 新增 search 组（16 条，无扁平旧键） ----
    "search.enabled": None,
    "search.providers": None,
    "search.max_parallel": None,
    "search.max_results": None,
    "search.max_rounds": None,
    "search.provider_timeout": None,
    "search.total_timeout": None,
    "search.cooldown_seconds": None,
    "search.cache_ttl": None,
    "search.fetch_pages": None,
    "search.fetch_top_n": None,
    "search.fetch_timeout": None,
    "search.fetch_max_chars": None,
    "search.region": None,
    "search.safesearch": None,
    "search.proxy": None,
}

#: 旧扁平键全集（迁移基线；测试钉住它恒为 25 个）。
FLAT_KEYS: tuple[str, ...] = tuple(sorted(k for k in KEY_MAP.values() if k))

#: v4 纯新增路径（没有 v3 扁平旧键），守卫用。
V4_ONLY_PATHS: tuple[str, ...] = tuple(
    sorted(path for path, flat in KEY_MAP.items() if flat is None)
)

_MISSING = object()


def nested_paths() -> list[str]:
    """返回所有受支持的点号路径（排序后）。"""
    return sorted(KEY_MAP)


def flat_key(path: str) -> str | None:
    """点号路径对应的旧扁平键；未知路径**或** v4 纯新增路径返回 None。

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
