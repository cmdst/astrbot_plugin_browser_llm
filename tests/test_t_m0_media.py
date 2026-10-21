"""T-M0：媒体发送出口。

前台必须保持"直接发、不登记 artifacts"；后台只登记、由投递层发送。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

from astrbot.api.event import AstrMessageEvent

# 用与 main.py 相同的包路径导入：conftest 只把 ``core`` 包注册为别名，
# 子模块 ``core.delivery`` 与 ``astrbot_plugin_browser_llm.core.delivery``
# 会各自加载一次；ContextVar 必须来自 main 实际使用的那个模块实例。
from astrbot_plugin_browser_llm.core.delivery import (
    artifact_sink,
    collect_artifact,
    current_artifacts,
)
from main import BrowserLLMPlugin

ROOT = Path(__file__).resolve().parent.parent


def make_event():
    event = AstrMessageEvent()
    event._sent = None
    return event


def test_artifact_sink_collects_only_inside_context():
    """只在 ``artifact_sink()`` 上下文里登记；上下文外一律返回 False。

    （v1.4.0 清理：从 ``tests/test_core_delivery.py`` 迁来 —— 该文件随
    ``core/delivery.py`` 裁剪删除，但这条守护的正是保留下来的
    ``collect_artifact`` 语义，故随实现一起搬进本文件。）
    """
    assert collect_artifact("/tmp/a.png") is False
    with artifact_sink() as bucket:
        assert collect_artifact("/tmp/b.png") is True
        assert current_artifacts() == ["/tmp/b.png"]
        assert bucket == ["/tmp/b.png"]
    assert collect_artifact("/tmp/c.png") is False
    assert current_artifacts() == []


def test_foreground_image_sends_directly_and_records_nothing():
    plugin = BrowserLLMPlugin(None, {})
    event = make_event()
    asyncio.run(plugin._emit_media(event, "/tmp/a.png", "image"))
    assert event._sent == "image:/tmp/a.png"
    assert current_artifacts() == []


def test_background_image_collects_without_sending():
    plugin = BrowserLLMPlugin(None, {})
    event = make_event()
    with artifact_sink() as bucket:
        asyncio.run(plugin._emit_media(event, "/tmp/b.png", "image"))
    assert event._sent is None
    assert bucket == ["/tmp/b.png"]
    assert current_artifacts() == []


def test_foreground_video_sends_directly(monkeypatch):
    module = types.ModuleType("astrbot.api.message_components")

    class Video:
        @staticmethod
        def fromFileSystem(path):
            return f"video:{path}"

    module.Video = Video
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", module)

    plugin = BrowserLLMPlugin(None, {})
    event = make_event()
    asyncio.run(plugin._emit_media(event, "/tmp/v.mp4", "video"))
    assert "video:/tmp/v.mp4" in event._sent
    assert current_artifacts() == []


def test_no_direct_media_send_left_in_source():
    """两个工具都必须走 _emit_media，源码里不得残留直接发送。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # 只允许 _emit_media 内部直接发图；工具层不得再出现直接发送
    assert src.count("await event.send(event.image_result") == 1
    assert src.count("event.chain_result([Video.fromFileSystem") == 1
    assert src.count("_emit_media(") >= 3
