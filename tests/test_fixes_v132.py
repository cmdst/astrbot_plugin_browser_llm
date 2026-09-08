"""v1.3.2 修复回归测试（工程缺陷清理）。

覆盖：
- browse_checkbox 漏 await loc.is_checked()（bool(coroutine) 恒为 True，
  「目标状态与当前状态一致」时误点击导致切换语义反转）——fake locator 的
  is_checked 返回真实协程（与 Playwright async API 行为一致），断言四种
  状态组合的点击行为；
- 子代理指令不再包含一次性任务残留（八维/Human Model ID 等站点关键词），
  browse_zoom_crop 只列一次，通用「提交后等待」策略保留；
- PLUGIN_VERSION 与 metadata.yaml 的 version 一致（单一事实源推导）。
"""

import asyncio
import re
from pathlib import Path

from main import (
    _BROWSER_AGENT_INSTRUCTION,
    PLUGIN_VERSION,
    BrowserLLMPlugin,
)


def _run(coro):
    return asyncio.run(coro)


class FakeEvent:
    unified_msg_origin = "grp:1:GroupMessage:123"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"


class FakeLocator:
    """is_checked 返回真实协程的定位器桩（模拟 Playwright async API）。

    关键点：is_checked 不直接返回 bool，而是返回协程——修复前的
    ``bool(loc.is_checked())`` 对协程恒为 True，本桩使该缺陷可被断言捕获。
    """

    def __init__(self, checked: bool):
        self._checked = bool(checked)
        self.click_count = 0

    async def is_checked(self):
        return self._checked

    async def click(self, timeout=None):
        self.click_count += 1
        self._checked = not self._checked  # 点击即切换（复选框语义）


class FakePage:
    def __init__(self, checked: bool):
        self._locator = FakeLocator(checked)

    def locator(self, selector):
        return self._locator


class FakeExtractor:
    async def extract_page_info(self, page):
        return {"url": "https://example.com", "title": "正常标题"}

    async def extract_text(self, page, max_chars=4000):
        return "正常正文"


class Sessions:
    def __init__(self, page):
        self._page = page

    async def ensure_page(self, umo):
        return self._page

    def get_lock(self, umo):
        return asyncio.Lock()


def _make_plugin(page: FakePage) -> BrowserLLMPlugin:
    plugin = BrowserLLMPlugin.__new__(BrowserLLMPlugin)
    plugin.metadata_name = "astrbot_plugin_browser_llm"
    plugin.safety = None
    plugin.browser = None
    plugin.extractor = FakeExtractor()
    plugin.sessions = Sessions(page)
    plugin.max_chars = 4000
    plugin._umo_of = lambda event: event.unified_msg_origin
    plugin._is_session_allowed = lambda event: (True, "")
    plugin._lock_for = lambda event: asyncio.Lock()
    return plugin


def _run_checkbox(checked_target: bool, current: bool):
    page = FakePage(checked=current)
    plugin = _make_plugin(page)
    result = _run(plugin.browse_checkbox(FakeEvent(), "#cb", checked=checked_target))
    return result, page._locator


# ================================================================
# browse_checkbox：is_checked 必须 await（四种状态组合）
# ================================================================

def test_checkbox_no_click_when_both_unchecked():
    """当前未勾选 + 目标未勾选：不应点击。

    修复前 bool(coroutine)=True != False → 误点击把复选框选上。
    """
    result, loc = _run_checkbox(False, current=False)
    assert loc.click_count == 0
    assert "【错误】" not in result


def test_checkbox_no_click_when_both_checked():
    """当前已勾选 + 目标已勾选：不应点击（幂等）。"""
    result, loc = _run_checkbox(True, current=True)
    assert loc.click_count == 0
    assert "【错误】" not in result


def test_checkbox_clicks_when_target_checked_current_unchecked():
    _, loc = _run_checkbox(True, current=False)
    assert loc.click_count == 1


def test_checkbox_clicks_when_target_unchecked_current_checked():
    _, loc = _run_checkbox(False, current=True)
    assert loc.click_count == 1


# ================================================================
# 子代理指令：一次性任务残留清理
# ================================================================

def test_instruction_has_no_oneoff_task_residue():
    """站点专属关键词（某性格测试任务）不得出现在通用指令中。"""
    for token in ("八维", "Human Model ID", "模型匹配", "指纹", "生成我的模型", "逐题"):
        assert token not in _BROWSER_AGENT_INSTRUCTION, token


def test_instruction_keeps_generic_wait_guidance():
    """泛化后的「提交后等待结果」策略保留（等待重读模式是通用经验）。"""
    assert "提交后等待结果" in _BROWSER_AGENT_INSTRUCTION
    assert "5-8 秒" in _BROWSER_AGENT_INSTRUCTION


def test_instruction_lists_zoom_crop_once():
    """工具清单中 browse_zoom_crop 只列一次（v1.3.1 清单重复项已删）。

    「操作原则」第 6 条对 browse_zoom_crop 的用法提及属正常引用，
    不计入清单重复；故按清单条目前缀 `- browse_zoom_crop：` 计数。
    """
    assert _BROWSER_AGENT_INSTRUCTION.count("- browse_zoom_crop：") == 1


# ================================================================
# 版本单一事实源：PLUGIN_VERSION 从 metadata.yaml 推导
# ================================================================

def test_plugin_version_matches_metadata():
    meta = (
        Path(__file__).resolve().parent.parent / "metadata.yaml"
    ).read_text(encoding="utf-8")
    m = re.search(r"^version:\s*['\"]?([^\s'\"]+)", meta, re.MULTILINE)
    assert m, "metadata.yaml 缺少 version 字段"
    assert PLUGIN_VERSION == m.group(1)
