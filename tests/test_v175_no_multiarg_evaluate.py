"""D1/D8 回归门禁：`Page.evaluate` 只收 **1 个**位置参数（表达式之外的 arg）。

平台事实（规格 §8.2，可复算）：

    inspect.signature(Page.evaluate)  ->  (self, expression, arg=None)

因此绑定方法调用最多只能传 2 个位置实参（``js`` + 一个 ``arg``）。v1.6.0 的
``_INTERACTIVE_JS`` / ``_POINT_ELEMENT_JS`` 各传 3 个（``(maxItems, roleFilter)``
/ ``(x, y)`` 形式的多参函数），生产上必然抛

    TypeError: Page.evaluate() takes from 2 to 3 positional arguments but 4 were given

这就是 D1（``browse_get_interactive`` 全废）与 D8（``browse_click_coords`` 全废）
的根因。本门禁按 AST 扫全仓：

1. 任何 ``*.evaluate(...)`` 调用的位置实参**不得超过 2 个**（表达式 + arg）；
2. 同时给出位置 arg 与 ``arg=`` 关键字实参也算违规（同一个位置传两遍）；
3. 三个旧 JS 常量与两条坐标/枚举旧链在非测试代码里**必须为 0**。

只扫 ``main.py`` / ``core/`` / ``tools/``：``tests/`` 里的 fake 是测试替身，
不是生产调用点。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("main.py", "core", "tools")
SKIP_PARTS = {"tests", "__pycache__", ".git"}


def _iter_py() -> list[Path]:
    out: list[Path] = []
    for entry in SCAN_DIRS:
        target = ROOT / entry
        if target.is_file() and target.suffix == ".py":
            out.append(target)
            continue
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*.py")):
            if SKIP_PARTS & set(path.parts):
                continue
            out.append(path)
    return out


def _evaluate_calls(path: Path) -> list[tuple[int, int, bool]]:
    """返回 ``[(行号, 位置实参个数, 是否同时给了 arg=)]``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else None)
        )
        if name != "evaluate":
            continue
        has_arg_kw = any(kw.arg == "arg" for kw in node.keywords)
        found.append((node.lineno, len(node.args), has_arg_kw))
    return found


def test_no_evaluate_call_passes_more_than_one_argument():
    """D1/D8 的门禁本体：位置实参 > 2，或位置 arg 与 arg= 同时出现，即失败。"""
    bad: list[str] = []
    for path in _iter_py():
        for lineno, nargs, has_arg_kw in _evaluate_calls(path):
            rel = path.relative_to(ROOT)
            if nargs > 2:
                bad.append(f"{rel}:{lineno} 位置实参={nargs}（>2）")
            elif nargs == 2 and has_arg_kw:
                bad.append(f"{rel}:{lineno} 位置 arg 与 arg= 重复")
    assert not bad, "evaluate 调用把多个参数塞进位置实参（D1/D8 根因）：\n" + "\n".join(bad)


LEGACY_CONSTS = ("_INTERACTIVE_JS", "_POINT_ELEMENT_JS", "_CLICK_TEXT_CANDIDATES_JS")
LEGACY_METHODS = ("browse_click_coords", "browse_get_interactive")


def test_legacy_js_constants_are_not_defined():
    """三个旧 JS 常量必须从生产代码里消失（模块级赋值一律不许再出现）。"""
    hits: list[str] = []
    for path in _iter_py():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            targets = (
                node.targets if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                name = getattr(target, "id", None)
                if name in LEGACY_CONSTS:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert not hits, "旧 JS 常量仍在定义：\n" + "\n".join(hits)


def test_legacy_tool_methods_are_not_defined():
    """坐标点击 / DOM 枚举两条旧链的方法名必须整条删除（不是保留薄壳）。"""
    hits: list[str] = []
    for path in _iter_py():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = getattr(node, "name", None)
            if name in LEGACY_METHODS and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno} def {name}")
    assert not hits, "旧动作方法仍在定义：\n" + "\n".join(hits)


def test_evaluate_call_sites_are_few_and_each_takes_one_arg():
    """调用点数量与形态：每处最多带 1 个 arg（防止用 `*args` 绕过形状检查）。"""
    sites = [
        (p.relative_to(ROOT), nargs)
        for p in _iter_py()
        for _lineno, nargs, _kw in _evaluate_calls(p)
    ]
    assert sites, "全仓找不到任何 evaluate 调用 —— 扫描口径可能崩了"
    for rel, nargs in sites:
        assert nargs <= 2, f"{rel} 有 {nargs} 个位置实参"
    # 规格 §8.2 的口径是"入口层（main.py）不该再有 DOM 枚举"：main.py 现在 0 处，
    # 留 2 的余量只拦回退；core 里的 evaluate（正文/滚动/SoM overlay/native setter）
    # 属于感知与执行层的合法用法，按 arity 管（上面的断言）。
    main_sites = [rel for rel, _ in sites if str(rel) == "main.py"]
    assert len(main_sites) <= 2, f"main.py 里又有 {len(main_sites)} 处 evaluate"
