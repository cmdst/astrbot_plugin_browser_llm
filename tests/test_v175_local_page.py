"""`browse_local_page` 的路径安全用例（R6，≥15 条）。

为什么这组用例必须在：该工具仍在生产注册，而 `_check_local_page_path`
（`is_relative_to` 前缀白名单）+ `_resolve_local_page_target`（非空 → resolve →
白名单 → 存在 → 扩展名）是**路径穿越与软链逃逸的唯一闸门**。v1.7.0 的
28 条同主题用例在重构中被整模块删掉，等于把这道闸门变成零覆盖。

口径：白名单根由 `_local_page_allowed_roots` 注入（生产来自工作区 + 插件 data
目录 + 环境变量追加），因此这里用 tmp_path 造"允许根"与"外部目录"，不依赖
真实工作区布局。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _path in (str(ROOT.parent), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.path.insert(0, str(ROOT / "tests"))

import test_v175_tools as helpers  # noqa: E402


@pytest.fixture()
def roots(tmp_path):
    """allowed 根 + 一个外部目录，两者都在同一个 tmp 树里（软链可造）。"""
    allowed = tmp_path / "workspace"
    (allowed / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return allowed, outside


@pytest.fixture()
def plugin(tmp_path, roots):
    allowed, _outside = roots
    plugin = helpers._bare_plugin(tmp_path)
    plugin._local_page_allowed_roots = (allowed.resolve(),)
    return plugin


def _html(directory: Path, name: str = "page.html") -> Path:
    target = directory / name
    target.write_text("<html><body><h1>hi</h1></body></html>", encoding="utf-8")
    return target


# ---------------------------------------------------------------- 允许面


def test_allows_file_under_allowed_root(plugin, roots):
    allowed, _ = roots
    target = _html(allowed)
    got, err = plugin._resolve_local_page_target(str(target))
    assert err == "" and got == target.resolve()


def test_allows_nested_file_under_allowed_root(plugin, roots):
    allowed, _ = roots
    target = _html(allowed / "sub")
    _got, err = plugin._resolve_local_page_target(str(target))
    assert err == ""


def test_allows_htm_extension(plugin, roots):
    allowed, _ = roots
    target = _html(allowed, "page.htm")
    _got, err = plugin._resolve_local_page_target(str(target))
    assert err == ""


def test_accepts_file_scheme_prefix(plugin, roots):
    allowed, _ = roots
    target = _html(allowed)
    got, err = plugin._resolve_local_page_target(f"file://{target}")
    assert err == "" and got == target.resolve()


# ---------------------------------------------------------------- 拒绝面


def test_rejects_empty_path(plugin):
    got, err = plugin._resolve_local_page_target("")
    assert got is None and "不能为空" in err


def test_rejects_file_outside_allowed_roots(plugin, roots):
    _allowed, outside = roots
    target = _html(outside)
    got, err = plugin._resolve_local_page_target(str(target))
    assert got is None and err.startswith("【拒绝】") and "允许范围" in err


def test_rejects_parent_traversal_escape(plugin, roots):
    allowed, _ = roots
    escaped = allowed / ".." / "outside" / "page.html"
    (allowed / ".." / "outside" / "page.html").parent.mkdir(exist_ok=True)
    _html(allowed / ".." / "outside")
    got, err = plugin._resolve_local_page_target(str(escaped))
    assert got is None and err.startswith("【拒绝】"), "`..` 必须被 resolve 消解后按白名单拒绝"


def test_rejects_symlink_escape(plugin, roots):
    allowed, outside = roots
    secret = _html(outside, "secret.html")
    link = allowed / "link.html"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("该平台不支持符号链接")
    got, err = plugin._resolve_local_page_target(str(link))
    assert got is None, "软链必须按真实路径判定（否则等于任意文件读取）"
    assert err.startswith("【拒绝】")


def test_rejects_non_html_extension(plugin, roots):
    allowed, _ = roots
    target = allowed / "notes.txt"
    target.write_text("secret", encoding="utf-8")
    got, err = plugin._resolve_local_page_target(str(target))
    assert got is None and ".html" in err


def test_rejects_missing_file(plugin, roots):
    allowed, _ = roots
    got, err = plugin._resolve_local_page_target(str(allowed / "nope.html"))
    assert got is None and "不存在" in err


def test_rejects_directory_path(plugin, roots):
    allowed, _ = roots
    got, err = plugin._resolve_local_page_target(str(allowed / "sub"))
    assert got is None and "不存在" in err


def test_rejects_etc_passwd(plugin):
    got, err = plugin._resolve_local_page_target("/etc/passwd")
    assert got is None and err.startswith("【拒绝】")


def test_rejects_traversal_string_without_resolving(plugin, roots):
    allowed, _ = roots
    _html(allowed)
    got, err = plugin._resolve_local_page_target(str(allowed / "sub" / ".." / ".." / "outside"))
    assert got is None, "目录形式的穿越同样必须拒绝"


# ---------------------------------------------------------------- 入口级（同步拒绝）


def test_tool_returns_reject_text_synchronously(plugin, roots):
    import asyncio

    _allowed, outside = roots
    target = _html(outside)
    event = helpers._FakeEvent()
    out = asyncio.run(plugin.browse_local_page(event, path=str(target)))
    assert out.startswith("【拒绝】"), out


def test_tool_rejects_blacklisted_session(plugin, roots):
    import asyncio

    allowed, _ = roots
    target = _html(allowed)
    event = helpers._FakeEvent()

    def _deny(_event):
        return False, "本会话被禁用"

    plugin._is_session_allowed = _deny  # 模拟会话黑名单命中
    out = asyncio.run(plugin.browse_local_page(event, path=str(target)))
    assert out.startswith("【拒绝】") and "禁用" in out


def test_local_page_content_is_banned_checked():
    """禁词闸在**渲染后的正文**上（不是路径上）：本地页同样不许输出违禁内容。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _local_page_render("):]
    body = body[: body.index("\n    async def ", 10)]
    assert "self._check_banned(snap.text" in body, "本地页渲染体必须过禁词闸"
    assert "【拒绝】页面内容包含违禁内容" in body
