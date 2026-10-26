"""仓库卫生守卫（v1.4.0 冗余清理的回归防线）。

守护三件事，防止"清理过的死重"重新长回来：

1. **删除项不得复活**：``core/{tasks,async_bridge,vision,pages}.py``、
   库内重复副本 ``astrbot_plugin_browser_llm_tmp/``、孤儿资产
   ``data/cmd_config.json`` / ``data/t2i_templates/``；
2. **裁剪项不得回胀**：``core/delivery.py`` 只含 artifact 三件套；
   ``core/search.py`` 不再有 v3 串行入口；``core/crawler.py`` 不再有
   ``pages`` 专用的 ``PageContent/PageBackend/html_links``；
3. **接线项不得回退**：数据目录走 ``_resolve_data_dir()``（StarTools 优先）、
   工作区回退走核心 helper、``main.py`` 不出现"重复插件检测" WARN 块。

对应 spec AC-34 / AC-35 / AC-36 与 t4 验收；删除依据见
``audit/20261021-redundancy-audit.md`` §1/§2 与 spec §11。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"

DELETED_MODULES = ("tasks.py", "async_bridge.py", "vision.py", "pages.py")
DELETED_MODULE_NAMES = ("core.tasks", "core.async_bridge", "core.vision", "core.pages")


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            out.append(root)
        elif root.is_dir():
            out.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _defined_names(path: Path) -> set[str]:
    """文件**模块级**定义的名字（函数/类/顶层赋值），忽略注释与 docstring。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _referenced_names(path: Path) -> set[str]:
    """文件代码里**被引用**的名字（Name / Attribute），忽略注释与 docstring。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


# ------------------------- 删除项不得复活 -------------------------


def test_dead_core_modules_are_gone():
    for name in DELETED_MODULES:
        assert not (CORE / name).exists(), f"{name} 应已删除（audit §1）"


def test_duplicate_plugin_copy_is_gone():
    """库内 ``astrbot_plugin_browser_llm_tmp/`` 与主树逐字节重复（audit §2-R1）。

    其 3 个必须保留的 hunk 已移植进 ``main.py``（见下方接线守卫），副本本身
    等于"同一份代码的第二份副本"，留着只会让 grep/覆盖率双份计数。
    """
    assert not (ROOT / "astrbot_plugin_browser_llm_tmp").exists()


def test_orphan_data_assets_are_gone():
    """孤儿数据资产不再是"仓库内容"，也没有任何消费方。

    注意口径：``data/`` 是 **gitignore 的运行期目录**（``.gitignore`` 的
    ``data/``）。任何一次用真实 AstrBot 在仓库根目录跑导入/冒烟，核心都会在
    ``<cwd>/data`` 重建 ``cmd_config.json`` / ``t2i_templates`` —— 那是**运行时
    产物**，不是被清理的源码资产。因此本守卫钉两件事：
    ① ``data/`` 整体被 gitignore（不会进 commit / 不会被"整目录覆盖"带上线）；
    ② 代码里没有任何 ``t2i`` / ``cmd_config`` 消费方。
    """
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in gi
    for rel in ("main.py", "core/search.py", "core/crawler.py", "tools/migrate_config_v6.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "t2i" not in src, f"{rel} 不应引用 t2i 模板"
        assert "cmd_config" not in src, f"{rel} 不应读取核心 cmd_config"


def test_no_imports_of_deleted_modules():
    """全库（含 tests/tools）不得 import 已删模块 —— 防悬空 import。"""
    offenders: list[str] = []
    for path in _py_files(ROOT / "core", ROOT / "tests", ROOT / "tools", ROOT / "main.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == name or mod.startswith(name + ".") for name in DELETED_MODULE_NAMES):
                    offenders.append(f"{path.name}: from {mod}")
                # 相对导入：from .pages import ...
                if node.level and any(mod == name for name in ("tasks", "async_bridge", "vision", "pages")):
                    offenders.append(f"{path.name}: from .{mod}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(name + ".") for name in DELETED_MODULE_NAMES):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == []


# ------------------------- 裁剪项不得回胀 -------------------------


def test_delivery_is_trimmed_to_artifact_trio():
    path = CORE / "delivery.py"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) <= 45, f"core/delivery.py 回胀到 {len(lines)} 行（spec AC-35 上限 45）"
    defined = _defined_names(path)
    for gone in (
        "DeliveryArbiter",
        "BusyTracker",
        "DeliveryState",
        "DeliverySender",
        "sanitize_text",
        "render_envelope",
        "redact_secrets",
    ):
        assert gone not in defined, f"{gone} 应随 D8 删除"
    assert defined == {"_ARTIFACT_SINK", "artifact_sink", "collect_artifact", "current_artifacts"}


def test_delivery_artifact_api_importable():
    from core.delivery import artifact_sink, collect_artifact, current_artifacts

    assert collect_artifact("/tmp/x.png") is False
    assert current_artifacts() == []
    with artifact_sink() as bucket:
        assert collect_artifact("/tmp/x.png") is True
        assert bucket == ["/tmp/x.png"]


def test_redact_secrets_moved_to_search_module():
    from core.search import redact_secrets

    assert redact_secrets("key=sk-abcd1234", ["sk-abcd1234"]) == "key=[redacted]"
    assert "redact_secrets" not in _defined_names(CORE / "delivery.py")


def test_search_service_has_no_serial_entry():
    """v3 串行 fallback 已删：SearchService 只暴露 search_waves（spec §11.1）。"""
    from core.search import SearchService

    assert hasattr(SearchService, "search_waves")
    assert not hasattr(SearchService, "search")
    assert not hasattr(SearchService, "available_providers")
    path = CORE / "search.py"
    assert "SearchOutcome" not in _defined_names(path)
    assert "SearchOutcome" not in _referenced_names(path)


def test_crawler_has_no_pages_surface():
    from core.crawler import AiohttpFetcher

    defined = _defined_names(CORE / "crawler.py")
    for gone in ("PageContent", "PageBackend", "html_links", "open_page"):
        assert gone not in defined, f"crawler 里的 {gone} 只服务已删的 core/pages.py"
    assert hasattr(AiohttpFetcher, "fetch")
    assert not hasattr(AiohttpFetcher, "open_page")


# ------------------------- 接线项不得回退 -------------------------


def test_main_resolves_data_dir_through_helper():
    import main

    assert main._DATA_DIR == main._resolve_data_dir()
    assert main._plugin_identity() == "astrbot_plugin_browser_llm"
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # hunk 4：截图/媒体目录必须挂在 _DATA_DIR 下（不能写死插件安装目录）
    assert 'self._screenshot_dir = _DATA_DIR / "screenshots"' in src
    assert 'self._media_dir = _DATA_DIR / "media"' in src


def test_main_workspaces_fallback_uses_core_helper():
    import main

    assert main._resolve_workspaces_fallback().name == "workspaces"
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "get_astrbot_workspaces_path" in src
    assert "_DATA_DIR.parents[2]" not in src, "数据目录改到 plugin_data 后旧层级推导会指错目录"


def test_main_has_no_duplicate_plugin_warning():
    """D9：正式版改名后"检测原插件"没有对象，WARN 块不移植。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "get_registered_star" not in src


def test_gitignore_covers_build_junk():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("__pycache__", ".pytest_cache", "*.pyc"):
        assert pattern in gi, f".gitignore 缺 {pattern}"
