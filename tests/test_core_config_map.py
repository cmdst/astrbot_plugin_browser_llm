"""core/config_map.py 的最小守卫：键映射完备性 + 分层读取语义。"""


from core.config_map import (
    FLAT_KEYS,
    GROUPS,
    KEY_MAP,
    V4_ONLY_PATHS,
    ConfigView,
    flat_key,
    nested_paths,
)


def test_key_map_covers_all_25_flat_keys():
    """25 条 v3 旧键一条不少，外加 16 条 v4 纯新增键（映射为 None）。

    v4 起 ``KEY_MAP`` 的值可能是 ``None``：表示"该路径没有 v3 扁平旧键"。
    因此旧键集合必须用 ``sorted(v for v in ... if v)`` 过滤后再比。
    """
    assert len(FLAT_KEYS) == 25
    assert sorted(v for v in KEY_MAP.values() if v) == list(FLAT_KEYS)
    assert len(set(FLAT_KEYS)) == 25  # 无重复映射
    assert sum(1 for v in KEY_MAP.values() if v is None) == 16
    assert len(KEY_MAP) == 41
    # 16 条 None 全部落在 search 组（v4 只做加法，不改既有路径）
    assert V4_ONLY_PATHS and all(p.startswith("search.") for p in V4_ONLY_PATHS)
    assert set(V4_ONLY_PATHS) == {p for p, v in KEY_MAP.items() if v is None}


def test_mapped_groups_are_declared():
    """双向守卫：映射用的组 == 声明的组（既不多声明、也不漏声明）。

    单向（``used ⊆ GROUPS``）会放过两种错误：①``GROUPS`` 里塞了没有键的
    死组（async/delivery/crawler，audit §7.4-② 的坑）；②新路径写到了未声明
    的组上。因此这里用相等断言。
    """
    used = {path.split(".", 1)[0] for path in KEY_MAP}
    assert set(GROUPS) == {"browser", "limits", "security", "perception", "search"}
    assert used == set(GROUPS)
    for dead in ("async", "delivery", "crawler", "vision"):
        assert dead not in GROUPS, f"{dead} 是死组（零键零读取点），不应出现在 GROUPS"


def test_v4_paths_have_no_flat_fallback():
    """v4 纯新增路径**不**做扁平兜底：只有扁平旧键的配置里必须"缺键"。"""
    flat_cfg = {key: f"v-{key}" for key in FLAT_KEYS}
    view = ConfigView(flat_cfg)
    for path in V4_ONLY_PATHS:
        value, found = view.lookup(path, "default")
        assert found is False, f"{path} 不应命中扁平兜底"
        assert value == "default"


def test_nested_wins_over_flat():
    cfg = {"browser": {"type": "firefox"}, "browser_type": "chromium"}
    view = ConfigView(cfg)
    assert view.get("browser.type") == "firefox"
    assert view.lookup("browser.type")[1] is True


def test_flat_fallback_used_when_nested_missing():
    cfg = {"browser_type": "firefox"}
    view = ConfigView(cfg)
    assert view.get("browser.type") == "firefox"


def test_empty_list_counts_as_found():
    cfg = {"session_whitelist": []}
    view = ConfigView(cfg)
    value, found = view.lookup("security.session_whitelist")
    assert value == []
    assert found is True  # 存在且为空 != 缺键


def test_missing_key_is_not_found():
    view = ConfigView({})
    value, found = view.lookup("security.session_whitelist")
    assert value is None
    assert found is False
    assert view.has("security.session_whitelist") is False


def test_unknown_path_returns_default():
    view = ConfigView({"a": 1})
    assert view.get("nope.nothing", "fallback") == "fallback"
    assert view.has("nope.nothing") is False


def test_non_dict_intermediate_falls_back_to_flat():
    cfg = {"browser": "not-a-dict", "browser_type": "firefox"}
    assert ConfigView(cfg).get("browser.type") == "firefox"


def test_get_list_normalizes_none_and_scalar():
    view = ConfigView({"security": {"banned_words": None}, "max_links": "12"})
    assert view.get_list("security.banned_words") == ([], True)
    assert view.get_list("limits.max_links") == (["12"], True)


def test_flat_key_helper():
    assert flat_key("browser.type") == "browser_type"
    assert flat_key("unknown.path") is None
    assert nested_paths() == sorted(KEY_MAP)


def test_view_tolerates_non_dict_config():
    view = ConfigView(None)  # type: ignore[arg-type]
    assert view.get("browser.type", "chromium") == "chromium"
    assert view.has("browser.type") is False
