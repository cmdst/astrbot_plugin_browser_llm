"""迁移器 v6（v5 -> v6，兼容分组 v4 / 扁平 v3）+ 五模式行为守卫。

对应规格 §7.6 的迁移器契约与 §7.9 的判据 5–6。四条主线：

1. **可迁移**：v5（54 叶）/ v4（45 叶）/ 扁平 v3（25 键）都能一次吃到 v6
   （7 组 / 58 叶），且**迁移结果键集 == v6 schema 键集**（drop 键不残留）；
2. **五模式**：``--dry-run`` / ``--apply`` / ``--verify``（``--against`` 严格、
   ``--baseline`` 43 承接叶 + 15 新叶）/ ``--restore-from``（逐字节）/ ``--strip-v6``，
   退出码与写盘行为逐条钉住；
3. **可回滚且不泄值**：BOM 保留、``0600``、原子替换、备份命名不覆盖；任何模式都
   不打印配置值（``search.providers`` 连指纹都不打印，只给条数）；
4. **语义正确的搬运**：rename/move 直搬、merge 按规格的 merge_value_rule 求值、
   ``silent_mode`` -> ``screenshot_to_chat`` **取反**（规格 §7.4-2 原文）。

迁移器不在包内（``tools/`` 无 ``__init__.py``），用 importlib 按路径加载。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path

import pytest

from core.config_map import (
    CONFIG_V6_NEW_PATHS,
    GROUPS,
    LEAVES_V5,
    V5_DISPOSITION,
    V5_DROPPED_PATHS,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "migrate_config_v6", ROOT / "tools" / "migrate_config_v6.py"
)
mig = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mig)

SCHEMA: dict = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))

#: 值里埋的 marker：任何模式都不该出现在输出里（值可能是搜索 API key）。
MARKER = "MARKER_SECRET_9f2c"
API_KEY_MARKER = "sk-LEAKME-8a1d"
#: 数值 marker：同样不该出现在输出里（输出只有计数与指纹）。
INT_MARKER = 412345


# ---------------------------------------------------------------------------
# 夹具：生产现值形态的 v5 配置（BOM + 0600）
# ---------------------------------------------------------------------------


def v5_values() -> dict[str, object]:
    """v5 的 54 叶 -> 生产现值形态的值（含 marker，便于"值零变化/不泄露"断言）。"""
    return {
        "browser.type": "chromium",
        "browser.default_url": "https://www.baidu.com",
        "browser.default_search_engine": "必应搜索",
        "browser.proxy": "",
        "browser.viewport": {"width": 1280, "height": 800},
        "browser.timeout": 30,
        "limits.max_chars": INT_MARKER,
        "limits.max_links": 20,
        "limits.max_pages": 3,
        "limits.idle_timeout": 900,
        "limits.cache_days": 1,
        "limits.agent_max_steps": 80,
        "limits.agent_tool_timeout": 800,
        "security.banned_words": [],
        "security.enable_banned_filter": False,
        "security.block_internal_ip": True,
        "security.session_whitelist": [],
        "security.session_blacklist": [],
        "perception.enable_screenshot": True,
        "perception.silent_mode": True,
        "perception.page_perception": "text_image",
        "perception.perception_rules": [{"match": "tester", "perception": "text", "note": MARKER}],
        "perception.vision_provider_id": "cpa_Gemini/SENTINEL-VISION",
        "perception.vision_prompt": MARKER,
        "perception.vision_cache_ttl": 60,
        "search.enabled": True,
        "search.providers": [
            {"__template_key": "brave", "api_key": API_KEY_MARKER, "weight": 70},
        ],
        "search.max_parallel": 2,
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
        "search.region": MARKER,
        "search.safesearch": "moderate",
        "search.proxy": "",
        "search.mode": "external",
        "tools.background_mode": "browse_only",
        "tools.background_timeout": 360,
        "tools.background_max_chars": 800,
        "tools.background_engine": "core",
        "agent.provider_id": "cpa_Gemini/SENTINEL-AGENT",
        "agent.persona_mode": "default",
        "agent.persona_prompt": "",
        "agent.task_timeout": 600,
        "agent.image_max_side": 1280,
        "agent.image_quality": 80,
        "agent.result_max_chars": 3000,
        "agent.ledger_max_lines": 24,
    }


def group_of(path: str) -> str:
    return path.split(".", 1)[0]


def v5_config(values: dict[str, object] | None = None) -> dict:
    """``路径 -> 值`` 按 v5 的组顺序整理成嵌套结构。"""
    source = v5_values() if values is None else values
    out: dict[str, dict] = {}
    for leaf in LEAVES_V5:
        out.setdefault(leaf.group, {})[leaf.leaf] = source[leaf.path]
    return out


def v4_config(values: dict[str, object] | None = None) -> dict:
    """v4 形态：去掉 v1.7.0 新增的 9 条（``search.mode`` + ``agent.*``）。"""
    source = v5_values() if values is None else values
    out = v5_config(source)
    out.pop("agent", None)
    out["search"].pop("mode", None)
    return out


def flat_v3_config(values: dict[str, object] | None = None) -> dict:
    """扁平 v3：25 个顶层旧键。"""
    source = v5_values() if values is None else values
    flat: dict[str, object] = {}
    for leaf in LEAVES_V5:
        if leaf.flat:
            flat[leaf.flat] = source[leaf.path]
    return flat


def write_config(path: Path, data: dict, *, bom: bool = True, mode: int = 0o600) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    payload = text.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    path.write_bytes(payload)
    os.chmod(path, mode)


def read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def leaves_of(config: dict) -> dict[str, object]:
    """把任意形态展开成 v6 ``路径 -> 值``（用迁移器自己的 flatten）。"""
    return mig.flatten(config)[0]


def schema_paths() -> set[str]:
    out: set[str] = set()
    for group, meta in SCHEMA.items():
        for leaf in meta["items"]:
            out.add(f"{group}.{leaf}")
    return out


def run_cli(*argv: str) -> int:
    return mig.main(list(argv))


# ---------------------------------------------------------------------------
# 线 1：迁移结果 = v6 schema（drop 键不残留）
# ---------------------------------------------------------------------------


def test_migrate_v5_produces_exact_v6_keyset():
    """v5 迁移后键集 **逐键相等** v6 schema（多一个、少一个都算失败）。"""
    migrated = mig.migrate(v5_config())
    paths = set(leaves_of(migrated))
    assert paths == schema_paths()
    assert len(paths) == 58
    assert set(migrated) == set(GROUPS)
    assert sum(len(group) for group in migrated.values()) == 58
    # 组内叶顺序 = schema 声明顺序（迁移器按它写盘，避免核心白写一次配置）
    for group, meta in SCHEMA.items():
        assert list(migrated[group]) == list(meta["items"]), group


def test_dropped_v5_keys_do_not_survive():
    """8 个 drop 键在迁移结果里**一个都不留**（否则核心会静默删键）。"""
    migrated = mig.migrate(v5_config())
    paths = set(leaves_of(migrated))
    for path in V5_DROPPED_PATHS:
        assert path not in paths, path
        group, leaf = path.split(".", 1)
        assert leaf not in migrated.get(group, {}), path
    text = json.dumps(migrated, ensure_ascii=False)
    for leaf in (
        "default_url",
        "agent_max_steps",
        "page_perception",
        "vision_prompt",
        "vision_cache_ttl",
        "background_engine",
    ):
        assert leaf not in text, leaf
    assert '"mode"' not in text  # search.mode 也不残留
    assert "browser" not in migrated and "limits" not in migrated and "agent" not in migrated


def test_carried_values_are_preserved_leaf_by_leaf():
    """26 keep + 12 rename + 1 move = 39 条单来源叶的值逐条不变。"""
    values = v5_values()
    migrated = mig.migrate(v5_config())
    leaves = leaves_of(migrated)
    checked = 0
    for source_path, (action, target) in V5_DISPOSITION.items():
        if target is None or action == mig.MIGRATION_MERGE:
            continue
        if target == "perception.screenshot_to_chat":
            continue  # 取反，单独测
        assert leaves[target] == values[source_path], f"{source_path} -> {target}"
        checked += 1
    # 46 个有后代的 v5 键 − 7 个 merge = 39 条单来源；其中 silent_mode 取反（单独测）
    assert checked == 38
    # 关键几条手工再钉一遍（防止上面那条循环被"同源同值"掩盖）
    assert leaves["resources.max_tabs"] == 3
    assert leaves["resources.idle_timeout_s"] == 900
    assert leaves["resources.cache_days"] == 1
    assert leaves["resources.max_text_chars"] == INT_MARKER
    assert leaves["resources.engine"] == "chromium"
    assert leaves["search.engine"] == "必应搜索"
    assert leaves["tools.background_mode"] == "browse_only"
    assert leaves["model.image_quality"] == 80
    assert leaves["security.enable_banned_filter"] is False
    assert leaves["search.providers"] == values["search.providers"]
    assert leaves["search.providers"][0]["api_key"] == API_KEY_MARKER


def test_merge_provider_priority():
    """``model.provider_id`` ← agent.provider_id 非空取它；否则 vision；都空为 ""。"""
    rule = mig.MERGE_RULES["model.provider_id"]
    assert rule({"agent.provider_id": "A", "perception.vision_provider_id": "V"}) == "A"
    assert rule({"agent.provider_id": "", "perception.vision_provider_id": "V"}) == "V"
    assert rule({"agent.provider_id": "   ", "perception.vision_provider_id": "V"}) == "V"
    assert rule({}) == ""
    migrated = mig.migrate(v5_config())
    assert leaves_of(migrated)["model.provider_id"] == "cpa_Gemini/SENTINEL-AGENT"
    values = v5_values() | {"agent.provider_id": ""}
    assert leaves_of(mig.migrate(v5_config(values)))["model.provider_id"] == (
        "cpa_Gemini/SENTINEL-VISION"
    )


def test_merge_budget_priority_and_clamp():
    """``resources.total_budget_s`` ← background_timeout > task_timeout > 300，夹 [60,1800]。"""
    rule = mig.MERGE_RULES["resources.total_budget_s"]
    assert rule({"tools.background_timeout": 360, "agent.task_timeout": 600}) == 360
    assert rule({"agent.task_timeout": 600}) == 600
    assert rule({}) == 300
    assert rule({"tools.background_timeout": 5000}) == 1800  # 上限
    assert rule({"tools.background_timeout": 5}) == 60  # 下限
    assert rule({"tools.background_timeout": "420"}) == 420  # 字符串数字也能吃
    migrated = mig.migrate(v5_config())
    assert leaves_of(migrated)["resources.total_budget_s"] == 360  # 生产现值


def test_merge_viewport_and_screenshot_fallback():
    """``resources.viewport`` ← 宽高直搬 + DPR 补 1；``screenshot_fallback`` 直搬。"""
    viewport_rule = mig.MERGE_RULES["resources.viewport"]
    assert viewport_rule({"browser.viewport": {"width": 1111, "height": 777}}) == {
        "width": 1111,
        "height": 777,
        "device_scale_factor": 1,
    }
    assert viewport_rule({}) == {"width": 1280, "height": 800, "device_scale_factor": 1}
    assert viewport_rule({"browser.viewport": {"width": "640", "height": 480}})["width"] == 640
    assert mig.MERGE_RULES["perception.screenshot_fallback"](
        {"perception.enable_screenshot": False}
    ) is False
    migrated = mig.migrate(v5_config())
    leaves = leaves_of(migrated)
    assert leaves["resources.viewport"] == {
        "width": 1280,
        "height": 800,
        "device_scale_factor": 1,
    }
    assert leaves["perception.screenshot_fallback"] is True


def test_silent_mode_is_inverted():
    """``silent_mode`` -> ``screenshot_to_chat`` **取反**（否则升级后开始往群里发截图）。"""
    for source, expected in ((True, False), (False, True)):
        values = v5_values() | {"perception.silent_mode": source}
        leaves = leaves_of(mig.migrate(v5_config(values)))
        assert leaves["perception.screenshot_to_chat"] is expected
        assert leaves["perception.screenshot_fallback"] is True  # 另一条不受影响
    # 字符串形态同样取反
    values = v5_values() | {"perception.silent_mode": "true"}
    assert leaves_of(mig.migrate(v5_config(values)))["perception.screenshot_to_chat"] is False


def test_flat_v3_and_grouped_v4_inputs_migrate_to_v6():
    """链式迁移：扁平 v3（25 键）/ 分组 v4（45 叶）都能一次吃到 v6（58 叶）。"""
    for config in (flat_v3_config(), v4_config()):
        migrated = mig.migrate(config)
        paths = set(leaves_of(migrated))
        assert paths == schema_paths()
        assert len(paths) == 58
    # 扁平 v3 里 browser.type 等旧键名搬到了 v6 叶上
    leaves = leaves_of(mig.migrate(flat_v3_config()))
    assert leaves["resources.engine"] == "chromium"
    assert leaves["resources.max_tabs"] == 3
    assert leaves["model.persona_mode"] == "default"  # agent.* 在 v3/v4 里不存在 -> 默认


def test_migrate_is_idempotent_on_v6_input():
    """已经是 v6 的配置再迁移一次：键集与值都不变（幂等）。"""
    once = mig.migrate(v5_config())
    twice = mig.migrate(once)
    assert once == twice
    assert mig.is_migrated(once) is True
    assert mig.is_migrated(v5_config()) is False


# ---------------------------------------------------------------------------
# 线 2：五模式（dry-run / apply / verify / restore-from / strip-v6）
# ---------------------------------------------------------------------------


def test_dry_run_never_writes(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    before = path.read_bytes()
    rc = run_cli("--dry-run", "--config", str(path))
    out = capsys.readouterr()
    assert rc == 0
    assert path.read_bytes() == before
    assert "RESULT mode=dry-run" in out.out
    assert "idempotent=0" in out.out
    assert "[DRY-RUN] no file written" in out.out


def test_apply_preserves_bom_and_mode_and_takes_backup(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config(), bom=True, mode=0o600)
    before = path.read_bytes()
    assert mig.has_bom(path) is True
    rc = run_cli("--apply", "--config", str(path))
    out = capsys.readouterr()
    assert rc == 0
    assert "RESULT mode=apply" in out.out
    assert mig.has_bom(path) is True, "写回必须保留 BOM 形态"
    assert mig.mode_of(path) == 0o600
    backups = sorted(tmp_path.glob("cfg.json.bak-pre-v6-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before, "备份必须是升级前的逐字节副本"
    assert mig.mode_of(backups[0]) == 0o600
    assert set(leaves_of(read_config(path))) == schema_paths()


def test_apply_without_bom_stays_without_bom(tmp_path):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config(), bom=False)
    assert mig.has_bom(path) is False
    assert run_cli("--apply", "--config", str(path)) == 0
    assert mig.has_bom(path) is False


def test_apply_is_idempotent_second_time(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    assert run_cli("--apply", "--config", str(path)) == 0
    capsys.readouterr()
    first = path.read_bytes()
    assert run_cli("--apply", "--config", str(path)) == 0
    out = capsys.readouterr()
    assert "idempotent=1" in out.out
    assert path.read_bytes() == first, "幂等重跑不得改动字节"


def test_verify_strict_against(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    assert run_cli("--apply", "--config", str(path)) == 0
    copy = tmp_path / "copy.json"
    copy.write_bytes(path.read_bytes())
    capsys.readouterr()
    assert run_cli("--verify", "--against", str(copy), "--config", str(path)) == 0
    assert "[OK] verify" in capsys.readouterr().out
    # 改一个值 -> 严格校验必须失败（rc=3）
    tampered = read_config(path)
    tampered["resources"]["max_tabs"] = 19
    write_config(copy, tampered)
    assert run_cli("--verify", "--against", str(copy), "--config", str(path)) == 3


def test_verify_baseline_checks_43_carried_and_15_new(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    baseline = tmp_path / "v5.json"
    baseline.write_bytes(path.read_bytes())
    assert run_cli("--apply", "--config", str(path)) == 0
    capsys.readouterr()
    # 显式给基线（v5 文件）
    assert run_cli("--verify", "--baseline", str(baseline), "--config", str(path)) == 0
    out = capsys.readouterr().out
    assert "[OK] baseline" in out
    assert "carried_missing=[]" in out
    assert "new_missing=[]" in out
    assert "value_diff=[]" in out
    # 省略路径 -> 自动用同目录最新的 *.bak-pre-v6-*
    assert run_cli("--verify", "--baseline", "--config", str(path)) == 0
    capsys.readouterr()
    # 改一条承接叶的值 -> 基线校验失败
    tampered = read_config(path)
    tampered["resources"]["idle_timeout_s"] = 1234
    write_config(path, tampered)
    assert run_cli("--verify", "--baseline", str(baseline), "--config", str(path)) == 3


def test_restore_from_backup_is_byte_identical(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config(), bom=True, mode=0o600)
    before = path.read_bytes()
    assert run_cli("--apply", "--config", str(path)) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v6-*"))[0]
    capsys.readouterr()
    rc = run_cli("--restore-from", str(backup), "--config", str(path))
    assert rc == 0
    assert path.read_bytes() == before, "回滚必须是逐字节重建（BOM/缩进/顺序都不能变）"
    assert mig.mode_of(path) == 0o600
    assert mig.has_bom(path) is True
    out = capsys.readouterr().out
    assert "RESULT mode=restore" in out
    assert "回滚必须成对" in out
    # 别名同样可用
    assert run_cli("--rebuild-from-backup", str(backup), "--config", str(path)) == 0
    assert run_cli("--from-backup", str(backup), "--config", str(path)) == 0


def test_restore_from_missing_backup_is_refused(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    assert run_cli("--restore-from", str(tmp_path / "nope.json"), "--config", str(path)) == 2
    assert "backup not found" in capsys.readouterr().err


def test_strip_v6_removes_15_new_leaves(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    assert run_cli("--apply", "--config", str(path)) == 0
    capsys.readouterr()
    # 干跑不写盘
    before = path.read_bytes()
    assert run_cli("--strip-v6", "--dry-run", "--config", str(path)) == 0
    assert path.read_bytes() == before
    assert "[DRY-RUN] no file written" in capsys.readouterr().out
    # 落盘：58 -> 43
    assert run_cli("--strip-v6", "--config", str(path)) == 0
    out = capsys.readouterr().out
    assert "RESULT mode=strip" in out
    stripped = leaves_of(read_config(path))
    assert len(stripped) == 43
    for path_name in CONFIG_V6_NEW_PATHS:
        assert path_name not in stripped
    assert "这不是 v5 降级" in out


def test_unknown_key_refused_by_default(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    config = v5_config()
    config["resources"] = {"mystery": 1}
    write_config(path, config)
    before = path.read_bytes()
    rc = run_cli("--apply", "--config", str(path))
    out = capsys.readouterr()
    assert rc == 2
    assert "unknown keys, refusing to write" in out.err
    assert path.read_bytes() == before, "拒写时不得改动文件"


def test_unknown_key_kept_with_allow_unknown(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    config = v5_config()
    config["resources"] = {"mystery": 1}
    write_config(path, config)
    rc = run_cli("--apply", "--allow-unknown", "--config", str(path))
    capsys.readouterr()
    assert rc == 0
    after = read_config(path)
    assert after["resources"]["mystery"] == 1
    assert set(leaves_of(after)) == schema_paths()


def test_structure_too_deep_is_refused(tmp_path, capsys):
    path = tmp_path / "cfg.json"
    write_config(path, {"resources": {"nested": {"deep": 1}}})
    rc = run_cli("--apply", "--config", str(path))
    assert rc == 2
    assert "结构层级超过 2 段" in capsys.readouterr().err


def test_dry_run_on_empty_config_is_ok(capsys):
    """``--dry-run --config /dev/null``：空配置也能出计划，rc=0（派单里的复跑命令）。"""
    rc = run_cli("--dry-run", "--config", os.devnull)
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESULT mode=dry-run keys=58 groups=7" in out
    assert "new=15" in out


# ---------------------------------------------------------------------------
# 线 3：任何模式都不打印配置值
# ---------------------------------------------------------------------------


def test_no_value_printed_in_any_mode(tmp_path, capsys):
    """五模式跑一遍：marker（字符串 / 数字 / api_key）都不得出现在输出里。"""
    path = tmp_path / "cfg.json"
    write_config(path, v5_config())
    baseline = tmp_path / "v5.json"
    baseline.write_bytes(path.read_bytes())

    collected: list[str] = []

    def _run(*argv: str) -> int:
        rc = run_cli(*argv)
        captured = capsys.readouterr()
        collected.append(captured.out)
        collected.append(captured.err)
        return rc

    assert _run("--dry-run", "--config", str(path)) == 0
    assert _run("--apply", "--config", str(path)) == 0
    assert _run("--verify", "--against", str(path), "--config", str(path)) == 0
    assert _run("--verify", "--baseline", str(baseline), "--config", str(path)) == 0
    backup = sorted(tmp_path.glob("cfg.json.bak-pre-v6-*"))[0]
    assert _run("--restore-from", str(backup), "--config", str(path)) == 0
    assert _run("--strip-v6", "--dry-run", "--config", str(path)) == 0

    joined = "\n".join(collected)
    for secret in (MARKER, API_KEY_MARKER, str(INT_MARKER)):
        assert secret not in joined, f"输出泄露了配置值: {secret}"
    # 连"默认值字符串"也不该出现（迁移器只打键名/计数/分组名/指纹）
    for default_text in (
        "请用中文描述这张网页截图的重点内容",  # v5 vision_prompt 的默认值（已 drop）
        "必应搜索",  # search.engine 的值
        "browse_only",  # tools.background_mode 的值
        "chromium",  # resources.engine 的值
    ):
        assert default_text not in joined, f"输出出现了配置值: {default_text}"
    # 值指纹允许出现（sha256 前 8 位），但 providers **连指纹都不打印**，只给条数
    assert "fp search.providers=<1 项，值不打印>" in joined
    assert "fp search.providers=***" not in joined


def test_value_fingerprint_masks_providers_and_api_key():
    assert mig.value_fingerprint("search.providers", [1, 2, 3]) == "<3 项，值不打印>"
    assert mig.value_fingerprint("search.providers", "not-a-list") == "<0 项，值不打印>"
    assert mig.value_fingerprint("x.api_key", API_KEY_MARKER) == "***"
    assert len(mig.value_fingerprint("search.region", MARKER)) == 8
    assert mig.fingerprint(MARKER) == mig.fingerprint(MARKER)
    assert mig.fingerprint(MARKER) != mig.fingerprint(MARKER + "x")


# ---------------------------------------------------------------------------
# 线 4：可测的 reader / writer 与安全边界
# ---------------------------------------------------------------------------


def test_reader_helpers_handle_bom(tmp_path):
    path = tmp_path / "cfg.json"
    write_config(path, {"a": 1}, bom=True)
    assert mig.has_bom(path) is True
    assert mig.read_config(path) == {"a": 1}
    # 平台事实：普通 json.load(open(...)) 会抛 Unexpected UTF-8 BOM
    with pytest.raises(json.JSONDecodeError):
        with open(path, encoding="utf-8") as handle:
            json.load(handle)
    # 空文件 = 空配置
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert mig.read_config(empty) == {}
    # 非法 JSON 抛 ValueError（由 CLI 收敛成 rc=2）
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        mig.read_config(bad)


def test_writer_helpers_bom_mode_atomic(tmp_path):
    path = tmp_path / "cfg.json"
    payload = mig.render({"a": 1}, bom=True)
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.endswith(b"\n")
    assert not mig.render({"a": 1}, bom=False).startswith(b"\xef\xbb\xbf")
    mig.write_atomic(path, payload)
    assert path.read_bytes() == payload
    assert mig.mode_of(path) == 0o600
    assert list(tmp_path.glob(".migrate-v6-*")) == [], "临时文件必须被清掉"


def test_backup_names_never_collide(tmp_path):
    path = tmp_path / "cfg.json"
    write_config(path, {"a": 1})
    first = mig.backup(path)
    second = mig.backup(path)
    assert first != second
    assert first.exists() and second.exists()
    assert mig.mode_of(first) == 0o600
    assert mig.latest_backup(path) is not None


def test_migrator_does_not_import_astrbot():
    """迁移器必须在无核心环境可跑：AST 层面不得 import astrbot（docstring 提到不算）。"""
    source = (ROOT / "tools" / "migrate_config_v6.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.split(".")[0] == "astrbot"], imported
    assert "core.config_map" in imported


def test_coerce_types_reports_paths_only():
    canonical = {
        "resources.max_tabs": "7",
        "tools.deliver_once": "off",
        "perception.rules": "not-a-list",
        "resources.engine": "firefox",
    }
    out, fixed, failed = mig.coerce_types(canonical)
    assert out["resources.max_tabs"] == 7
    assert out["tools.deliver_once"] is False
    assert out["perception.rules"] == "not-a-list"  # 不做类型归一
    assert fixed == ["resources.max_tabs", "tools.deliver_once"]
    assert failed == []


def test_restore_requires_regular_file(tmp_path, capsys):
    rc = run_cli("--restore-from", str(tmp_path / "x.json"), "--config", os.devnull)
    assert rc == 2
    capsys.readouterr()


# ------------------------------------------------ R12：幂等不备份 + v6 基线拒校验


def _run_cli(args, tmp_path):
    """跑迁移器 CLI，返回 (rc, stdout, stderr)。"""
    import subprocess
    import sys as _sys

    proc = subprocess.run(
        [_sys.executable, str(ROOT / "tools" / "migrate_config_v6.py"), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _v5_config() -> dict:
    from astrbot_plugin_browser_llm.core import config_map

    out: dict = {}
    for path in config_map.KEY_MAP_V5:
        cursor = out
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = 1
    out.setdefault("search", {})["providers"] = []
    return out


def test_idempotent_apply_creates_no_second_backup(tmp_path):
    """R12：第二次 apply（幂等）不得再生成备份，否则 baseline 会挑到 v6 内容的备份。"""
    cfg = tmp_path / "cfg.json"
    cfg.write_text(__import__("json").dumps(_v5_config()), encoding="utf-8")

    rc1, _out1, _err1 = _run_cli(["--config", str(cfg), "--apply"], tmp_path)
    assert rc1 == 0
    backups_after_first = sorted(tmp_path.glob("cfg.json.bak-pre-v6-*"))
    assert backups_after_first, "第一次（有变更）必须备份"

    rc2, out2, _err2 = _run_cli(["--config", str(cfg), "--apply"], tmp_path)
    assert rc2 == 0
    assert "idempotent=1" in out2, out2
    backups_after_second = sorted(tmp_path.glob("cfg.json.bak-pre-v6-*"))
    assert backups_after_second == backups_after_first, "幂等 apply 不许再生成备份"


def test_verify_baseline_rejects_a_v6_backup(tmp_path):
    """R12：v6 形态的基线 ⇒ rc=2 + 明确报错（而不是静默 OK）。"""
    import json

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_v5_config()), encoding="utf-8")
    rc, _out, _err = _run_cli(["--config", str(cfg), "--apply"], tmp_path)
    assert rc == 0
    # 造一个 v6 形态的基线（模拟"旧版本留下的 v6 备份"）
    v6_backup = tmp_path / "cfg.json.bak-pre-v6-9999999999"
    v6_backup.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
    rc2, _out2, err2 = _run_cli(
        ["--config", str(cfg), "--verify", "--baseline", str(v6_backup)], tmp_path
    )
    assert rc2 == 2, (rc2, err2)
    assert "已经是 v6 形态" in err2


def test_verify_baseline_auto_skips_v6_shaped_backups(tmp_path):
    """R12：`--baseline` 无参（自动挑最新）时也必须跳到 v5 形态那一份。"""
    import json

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_v5_config()), encoding="utf-8")
    rc, _out, _err = _run_cli(["--config", str(cfg), "--apply"], tmp_path)
    assert rc == 0
    # 更晚的 v6 形态备份（时间更新）
    import os
    import time

    newest = tmp_path / "cfg.json.bak-pre-v6-9999999999"
    newest.write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(newest, (time.time() + 10, time.time() + 10))
    rc2, out2, _err2 = _run_cli(["--config", str(cfg), "--verify", "--baseline"], tmp_path)
    assert rc2 == 0, out2
    assert "9999999999" not in out2, "自动挑选必须跳过 v6 形态的备份"
