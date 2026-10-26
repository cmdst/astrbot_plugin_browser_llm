"""v1.7.5 配置接线守卫（四方一致：schema ⇄ `config_map.LEAVES` ⇄ main.py 读取点 ⇄ 迁移器）。

钉六件事：

1. `main.py` 里每一个点号路径读取都在 v6 叶表里（多一个就是第二个事实源）；
2. v5 的 **drop 键**（v6 里不存在）在 `main.py` 里**没有读取点**，`agent_max_steps` 归零；
3. `silent_mode → screenshot_to_chat` 是**取反**（代码里读新键、用 `not ...` 判是否发群）；
4. `resources.viewport.device_scale_factor` 同时决定 SoM 图与快照首行的 dpr；
5. 单一 `resources.total_budget_s` 同时供前台 `wait_for` 与后台 `budget_s=`；
6. 单一代理键（`resources.proxy`）+ 搜索单独（`search.proxy`）；`model.*` 单组（无 vision 键）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from astrbot_plugin_browser_llm.core import config_map  # noqa: E402

MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
RUNTIME_SOURCES = {
    rel: (ROOT / rel).read_text(encoding="utf-8")
    for rel in (
        "main.py",
        "core/agent.py",
        "core/a11y.py",
        "core/action_model.py",
        "core/background.py",
        "core/browser.py",
        "core/config_map.py",
        "core/executor.py",
        "core/session.py",
        "core/verify.py",
    )
}


def _read_paths() -> set[str]:
    tree = ast.parse(MAIN_SRC)
    out: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "get_list", "has")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and "." in node.args[0].value
        ):
            out.add(node.args[0].value)
    return out


def test_every_read_point_exists_in_v6_leaf_table():
    leaves = {leaf.path for leaf in config_map.LEAVES}
    paths = _read_paths()
    assert paths, "没扫到任何点号路径读取 —— 口径崩了"
    missing = sorted(path for path in paths if path not in leaves)
    assert not missing, f"main.py 读了 v6 叶表里没有的键：{missing}"


def test_dropped_v5_keys_have_no_read_point():
    drops = [src for src, (kind, _dst) in config_map.V5_DISPOSITION.items()
             if kind == config_map.MIGRATION_DROP]
    assert drops, "v5 处置表里应当有 drop 桶"
    leaves = {leaf.path for leaf in config_map.LEAVES}
    leaked = [path for path in drops if path in leaves]
    assert not leaked, f"drop 键不该出现在 v6 叶表里：{leaked}"
    for rel, text in RUNTIME_SOURCES.items():
        if rel == "core/config_map.py":  # 冻结视图必须留着 v5 名字
            continue
        for path in drops:
            assert f'"{path}"' not in text, f"{rel} 仍在读已 drop 的 v5 键 {path}"


def test_agent_max_steps_is_gone_from_sources():
    """v5 的 `limits.agent_max_steps` 必须消失（步数护栏改名 `derive_max_steps` 以免误命中）。"""
    pattern = re.compile(r"(?:^|[^\w])agent_max_steps")
    for rel, text in RUNTIME_SOURCES.items():
        if rel == "core/config_map.py":  # v5 冻结视图（迁移器要用）
            continue
        hits = [
            line for line in text.splitlines()
            if pattern.search(line)
        ]
        assert not hits, f"{rel} 仍引用已取消的 v5 键：{hits[:2]}"


def test_screenshot_to_chat_is_an_inversion_not_a_rename():
    """R2：迁移时取反一次（`silent_mode → not silent_mode`），**读取点用真值**。

    键的语义是"截图是否发到会话"，所以实现必须是 `if bool(self.screenshot_to_chat)`
    才发群；`not bool(...)` 会把语义再翻回去（默认发群 + 开了反而静默）。
    """
    assert "perception.screenshot_to_chat" in MAIN_SRC
    assert "silent_mode" not in MAIN_SRC, "silent_mode 只应作为历史注释存在，不能是读取点"
    assert "if bool(self.screenshot_to_chat):" in MAIN_SRC, "发群判定必须是真值"
    assert "not bool(self.screenshot_to_chat)" not in MAIN_SRC, "R2：这里不许再取反"
    assert "sent_to_user=bool(self.screenshot_to_chat)" in MAIN_SRC


def test_device_scale_factor_drives_som_and_header():
    assert "device_scale_factor" in MAIN_SRC, "dpr 必须来自 resources.viewport"
    snapshot_body = MAIN_SRC[MAIN_SRC.index("    async def _snapshot("):]
    snapshot_body = snapshot_body[: snapshot_body.index("\n    async def ", 10)]
    assert "dpr=dpr" in snapshot_body, "a11y.snapshot 必须收到 dpr"
    shot_body = MAIN_SRC[MAIN_SRC.index("    async def _screenshot_impl("):]
    shot_body = shot_body[: shot_body.index("\n    async def ", 10)]
    assert "dpr=snap.dpr" in shot_body, "SoM 图必须用同一个 dpr"


def test_single_total_budget_for_front_and_background():
    paths = _read_paths()
    budget_keys = [path for path in paths if "budget" in path]
    assert budget_keys == ["resources.total_budget_s"], budget_keys
    front = MAIN_SRC[MAIN_SRC.index("    async def _browse_web_run("):]
    front = front[: front.index("\n    async def ", 10)]
    assert "float(self.total_budget_s)" in front, "前台预算必须用同一个值"
    back = MAIN_SRC[MAIN_SRC.index("    async def _submit_background("):]
    back = back[: back.index("\n    async def ", 10)]
    assert "budget_s=float(self.total_budget_s)" in back, "后台预算必须用同一个值"


def test_single_proxy_and_model_group():
    paths = _read_paths()
    assert "resources.proxy" in paths
    assert "search.proxy" in paths, "搜索代理是**单独**的键（与插件代理分开）"
    assert not [p for p in paths if p.startswith("vision.")], "不该再有 vision.* 读取点"
    assert "model.provider_id" in paths, "内层模型与识图合并成 model.provider_id 一个键"
    assert "vision_provider_id" not in MAIN_SRC, "识图 provider 键必须已删除"


def test_schema_and_leaf_table_are_in_sync():
    import json

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    assert set(schema) == set(config_map.GROUPS), "schema 顶层组与 LEAVES 分组必须逐一对应"
    assert len(config_map.KEY_MAP) == 57
    assert len(config_map.JSON_LEAF_KEYS) == 59
    # 每个 v6 叶都能在 schema 里找到（schema_leaf 只有一个事实源）
    missing = [leaf.path for leaf in config_map.LEAVES
               if config_map.schema_leaf(schema, leaf.path) is None]
    assert not missing, f"schema 里缺这些叶：{missing[:5]}…"
