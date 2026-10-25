"""core/config_map.py 的最小守卫：键映射完备性 + 分层读取语义。"""


from core.config_map import (
    AGENT_PATHS,
    BACK_TRACE,
    CONFIG_V5_NEW_PATHS,
    EFFECT_HOT,
    EFFECT_IMPORT,
    EFFECT_LABELS,
    EFFECT_NEW_CONTEXT,
    EFFECT_TIMING,
    FLAT_KEYS,
    GROUPS,
    GROUPS_V4,
    KEY_MAP,
    KEY_MAP_V4,
    LEAF_DEFAULTS,
    LEAF_ORDER,
    LEAF_PATHS,
    LEAF_TYPES,
    LEAVES,
    LEGACY_PATHS,
    NO_FLAT_PATHS,
    V4_LEAF_PATHS,
    V4_ONLY_PATHS,
    V5_ONLY_PATHS,
    ConfigView,
    back_trace,
    clamp_of,
    choices_of,
    default_of,
    effect_of,
    flat_key,
    leaf_meta,
    nested_paths,
)


def test_key_map_covers_all_25_flat_keys():
    """25 条 v3 旧键一条不少，外加 29 条纯新增键（search 17 + tools 4 + agent 8）。

    ``KEY_MAP`` 的值可能是 ``None``：表示"该路径没有 v3 扁平旧键"。
    因此旧键集合必须用 ``sorted(v for v in ... if v)`` 过滤后再比。

    v5（v1.7.0）：54 叶 = 25 有扁平旧键 + 29 纯新增；**0 改名、0 删除**。
    """
    assert len(FLAT_KEYS) == 25
    assert sorted(v for v in KEY_MAP.values() if v) == list(FLAT_KEYS)
    assert len(set(FLAT_KEYS)) == 25  # 无重复映射
    assert sum(1 for v in KEY_MAP.values() if v is None) == 29
    assert len(KEY_MAP) == 54
    # 29 条 None 落在三个"纯新增"来源（既有 25 条路径一条都没改）
    assert V4_ONLY_PATHS and all(p.startswith("search.") for p in V4_ONLY_PATHS)
    assert len(V4_ONLY_PATHS) == 16
    assert V5_ONLY_PATHS and all(p.startswith("tools.") for p in V5_ONLY_PATHS)
    assert len(V5_ONLY_PATHS) == 4
    assert len(AGENT_PATHS) == 8
    assert set(NO_FLAT_PATHS) == {p for p, v in KEY_MAP.items() if v is None}
    assert set(NO_FLAT_PATHS) == (
        set(V4_ONLY_PATHS) | set(V5_ONLY_PATHS) | set(AGENT_PATHS) | {"search.mode"}
    )
    # 25 条旧路径的映射值必须与 v4 冻结表逐条相同（= 0 改名）
    assert KEY_MAP_V4 == {p: KEY_MAP[p] for p in V4_LEAF_PATHS}
    assert len(KEY_MAP_V4) == 45


def test_mapped_groups_are_declared():
    """双向守卫：映射用的组 == 声明的组（既不多声明、也不漏声明）。

    单向（``used ⊆ GROUPS``）会放过两种错误：①``GROUPS`` 里塞了没有键的
    死组（async/delivery/crawler，audit §7.4-② 的坑）；②新路径写到了未声明
    的组上。因此这里用相等断言。
    """
    used = {path.split(".", 1)[0] for path in KEY_MAP}
    assert set(GROUPS) == {
        "browser",
        "limits",
        "security",
        "perception",
        "search",
        "tools",
        "agent",
    }
    assert used == set(GROUPS)
    # 组顺序必须与 schema 声明顺序一致（迁移器按它写盘，顺序漂移会让核心白写一次）
    assert GROUPS == (
        "browser",
        "limits",
        "security",
        "perception",
        "search",
        "tools",
        "agent",
    )
    # v4 的 6 组是冻结契约（tools/migrate_config_v4.py 与它的测试按这份校验）
    assert GROUPS_V4 == GROUPS[:6]
    for dead in ("async", "delivery", "crawler", "vision"):
        assert dead not in GROUPS, f"{dead} 是死组（零键零读取点），不应出现在 GROUPS"


def test_v4_paths_have_no_flat_fallback():
    """纯新增路径（v4 search + v5 tools）**不**做扁平兜底：扁平旧配置里必须"缺键"。"""
    flat_cfg = {key: f"v-{key}" for key in FLAT_KEYS}
    view = ConfigView(flat_cfg)
    for path in NO_FLAT_PATHS:
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


# ---------------------------------------------------------------------------
# v5（v1.7.0）新增守卫：LEAVES 是单一事实源，其余视图全部由它派生
# ---------------------------------------------------------------------------


def test_leaf_table_is_self_consistent():
    """``LEAVES`` 与所有派生视图一一对应（54 条，无重复、无遗漏）。"""
    assert len(LEAVES) == 54
    assert LEAF_PATHS == tuple(item.path for item in LEAVES)
    assert len(set(LEAF_PATHS)) == 54, "路径重复声明"
    assert list(KEY_MAP) == list(LEAF_PATHS)
    assert list(LEAF_TYPES) == list(LEAF_PATHS)
    assert list(LEAF_DEFAULTS) == list(LEAF_PATHS)
    assert list(EFFECT_TIMING) == list(LEAF_PATHS)
    assert list(BACK_TRACE) == list(LEAF_PATHS)
    # 每条 Leaf 的 group 都在 GROUPS 里，且组内叶名与 LEAF_ORDER 一致
    for item in LEAVES:
        assert item.group in GROUPS, item.path
        assert item.leaf in LEAF_ORDER[item.group], item.path
        assert leaf_meta(item.path) is item
    assert sum(len(v) for v in LEAF_ORDER.values()) == 54


def test_effect_timing_is_three_tier_and_labelled():
    """每个叶都有生效时机，且只允许三档；三档都要有中文说明（规格 §6.2）。"""
    assert set(EFFECT_TIMING.values()) == {EFFECT_IMPORT, EFFECT_NEW_CONTEXT, EFFECT_HOT}
    assert set(EFFECT_LABELS) == {EFFECT_IMPORT, EFFECT_NEW_CONTEXT, EFFECT_HOT}
    for path, effect in EFFECT_TIMING.items():
        assert effect in EFFECT_LABELS, path
        assert EFFECT_LABELS[effect], path
        assert effect_of(path) == effect
    # 生效时机与规格 §6.2 的三档逐条对齐（抽查：建 context 时读的与只在 __init__ 读的）
    for path in ("browser.type", "browser.proxy", "browser.viewport"):
        assert effect_of(path) == EFFECT_IMPORT, path
    assert effect_of("security.block_internal_ip") == EFFECT_NEW_CONTEXT
    for path in (
        "browser.default_url",
        "search.mode",
        "agent.provider_id",
        "agent.task_timeout",
    ):
        assert effect_of(path) == EFFECT_HOT, path
    assert effect_of("no.such.path") is None


def test_effect_distribution_matches_spec():
    """三档分布写死：热 50 / 新建 context 1 / 只在 __init__ 读 3。"""
    from collections import Counter

    counts = Counter(EFFECT_TIMING.values())
    assert counts[EFFECT_HOT] == 50
    assert counts[EFFECT_NEW_CONTEXT] == 1
    assert counts[EFFECT_IMPORT] == 3
    assert sum(counts.values()) == 54


def test_back_trace_covers_every_v4_path():
    """回溯映射：45 条 v4 路径 -> 自己；9 条 v1.7.0 新增 -> None（= 0 改名）。"""
    assert set(BACK_TRACE) == set(KEY_MAP)
    mapped = sorted(path for path, old in BACK_TRACE.items() if old is not None)
    assert mapped == sorted(LEGACY_PATHS)
    assert len(mapped) == 45
    for path in mapped:
        assert BACK_TRACE[path] == path, path
        assert back_trace(path) == path
    assert sorted(path for path, old in BACK_TRACE.items() if old is None) == sorted(
        CONFIG_V5_NEW_PATHS
    )
    for path in CONFIG_V5_NEW_PATHS:
        assert back_trace(path) is None
    assert back_trace("no.such.path") is None


def test_default_of_returns_independent_copy():
    """``default_of`` 必须给独立副本：调用方原地改它不能污染单一事实源。"""
    banned = default_of("security.banned_words")
    assert banned == LEAF_DEFAULTS["security.banned_words"]
    banned.append("MUTATED")
    assert "MUTATED" not in LEAF_DEFAULTS["security.banned_words"]
    providers = default_of("search.providers")
    providers.append({"api_key": "x"})
    assert LEAF_DEFAULTS["search.providers"] == []
    assert default_of("no.such.path", "fallback") == "fallback"


def test_clamp_and_choices_follow_spec():
    """钳制区间与枚举取值按规格 §6.2 写死（读取方照此实现，测试照此钉）。"""
    assert clamp_of("search.max_parallel") == (1, 10)
    assert clamp_of("search.max_results") == (1, 50)
    assert clamp_of("search.max_rounds") == (1, 5)
    assert clamp_of("search.provider_timeout") == (3, 60)
    assert clamp_of("search.total_timeout") == (5, 300)
    assert clamp_of("search.cooldown_seconds") == (0, 3600)
    assert clamp_of("search.cache_ttl") == (0, 3600)
    assert clamp_of("search.fetch_top_n") == (0, 5)
    assert clamp_of("search.fetch_timeout") == (3, 60)
    assert clamp_of("search.fetch_max_chars") == (200, 20000)
    assert clamp_of("tools.background_timeout") == (30, 900)
    assert clamp_of("tools.background_max_chars") == (200, 8000)
    assert clamp_of("agent.task_timeout") == (60, 1800)
    assert clamp_of("agent.image_max_side") == (320, 2048)
    assert clamp_of("agent.image_quality") == (40, 95)
    assert clamp_of("agent.result_max_chars") == (500, 20000)
    assert clamp_of("agent.ledger_max_lines") == (0, 60)
    # 无钳制的键返回 None（不是 (0, 0) 之类的假区间）
    assert clamp_of("limits.max_chars") is None
    assert clamp_of("browser.timeout") is None
    assert clamp_of("no.such.path") is None

    assert choices_of("search.mode") == ("external", "internal")
    assert choices_of("agent.persona_mode") == ("default", "current", "custom")
    assert choices_of("tools.background_mode") == ("off", "browse_only", "all")
    assert choices_of("tools.background_engine") == ("plugin", "core")
    assert choices_of("browser.type") == ("chromium", "firefox", "webkit")
    assert choices_of("search.safesearch") == ("off", "moderate", "strict")
    assert choices_of("perception.page_perception") == ("text", "text_image", "image")
    # 两个 provider 下拉的初值只有一个空选项（运行期由插件动态同步）
    assert choices_of("agent.provider_id") == ("",)
    assert choices_of("perception.vision_provider_id") == ("",)
    # 自由文本/数值没有枚举
    assert choices_of("browser.default_url") is None
    assert choices_of("limits.max_chars") is None
    assert choices_of("no.such.path") is None


def test_v5_new_paths_are_exactly_nine():
    """v5 新增恰好 9 条：search.mode + agent.* 8 条（规格 §6.1/§6.3）。"""
    assert len(CONFIG_V5_NEW_PATHS) == 9
    assert "search.mode" in CONFIG_V5_NEW_PATHS
    assert set(AGENT_PATHS) == {p for p in CONFIG_V5_NEW_PATHS if p.startswith("agent.")}
    assert len(V4_LEAF_PATHS) == 45
    assert len(LEGACY_PATHS) == 45
    assert set(V4_LEAF_PATHS) | set(CONFIG_V5_NEW_PATHS) == set(KEY_MAP)
    assert not set(V4_LEAF_PATHS) & set(CONFIG_V5_NEW_PATHS)
