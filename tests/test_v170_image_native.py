"""v1.7.0 影像原生：截图/裁剪以 ``ImageContent`` 直达内层模型（规格 §2）。

机制链（每条都在本文件里被验证一次）：

1. 内层工具 handler 返回 ``mcp.types.CallToolResult(content=[TextContent,
   ImageContent])`` —— 执行器允许（``ToolExecResult``，``agent/tool.py:16``），
   且 ``_make_browser_tool._handler`` 原样透传（不做 str() 化）；
2. 核心 runner 把 ``ImageContent`` 写进 ``tool_image_cache`` 并在**下一轮**
   以 user 消息追加进内层上下文（``tool_loop_agent_runner.py:1310-1325`` /
   ``:1107-1141``）⇒ 图片"迟到一步但一定可见"；
3. 内层模型**不是**多模态时走 caption 兜底（返回 ``str``，用 T2 修过的
   内容哈希缓存）—— 这是 ``perception.vision_provider_id`` 的唯一用途；
4. 纯文本 + 未配识图兜底 ⇒ 明确 ``【不可用】``（不许静默返回空串）。
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import mcp.types as mcp_types

from core import agent as agent_core
from main import BrowserLLMPlugin

UMO = "aiocqhttp:GroupMessage:123"

#: 1x1 透明 PNG（真实字节：``encode_image`` 读的是文件内容，不是后缀）。
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Cfg(dict):
    def __init__(self, data=None, schema=None):
        super().__init__(data or {})
        self.schema = schema if schema is not None else {}


class _Event:
    unified_msg_origin = UMO

    def __init__(self):
        self.sent = []

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def image_result(self, path):
        return ("image", path)

    async def send(self, msg):
        self.sent.append(msg)
        return None


def _plugin(config=None, ctx=None, tmp_path=None):
    cfg = config if isinstance(config, _Cfg) else _Cfg(config or {})
    plugin = BrowserLLMPlugin(ctx or SimpleNamespace(), cfg)
    plugin.config = cfg
    plugin.sessions = None
    plugin._screenshot_dir = tmp_path
    plugin._media_dir = tmp_path
    plugin._vision_cache = {}
    plugin.extractor = SimpleNamespace()
    plugin.max_chars = 4000
    return plugin


def _shot(plugin, name="browse_deadbeef_1.png") -> str:
    path = plugin._screenshot_dir / name
    path.write_bytes(PNG_1X1)
    return str(path)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------
# encode_image / async_image_content（插件侧唯一图片出站函数）
# ---------------------------------------------------------------


def test_encode_image_reads_real_bytes(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(PNG_1X1)
    b64, mime = agent_core.encode_image(str(path))
    assert base64.b64decode(b64) == PNG_1X1
    assert mime == "image/png"
    jpg = tmp_path / "b.jpg"
    jpg.write_bytes(PNG_1X1)
    assert agent_core.encode_image(str(jpg))[1] == "image/jpeg"


def test_encode_image_rejects_missing_or_oversized(tmp_path):
    assert agent_core.encode_image(str(tmp_path / "nope.png")) is None
    big = tmp_path / "big.png"
    big.write_bytes(b"\x00" * (agent_core.LLM_IMAGE_MAX_B64 * 3 // 4 + 10))
    assert agent_core.encode_image(str(big)) is None, "超 base64 硬上限必须返回 None"


def test_async_image_content_falls_back_to_original_without_core(tmp_path):
    """无核心 ``compress_image`` ⇒ 用原图（fail-soft，不许因为压不动就丢图）。"""
    path = tmp_path / "a.png"
    path.write_bytes(PNG_1X1)
    out = _run(agent_core.async_image_content(str(path), max_side=1280, quality=80))
    assert out is not None
    assert base64.b64decode(out[0]) == PNG_1X1


def test_call_result_with_image_shape():
    result = agent_core.call_result_with_image("文字摘要", ("QUJD", "image/jpeg"))
    assert isinstance(result, mcp_types.CallToolResult)
    assert result.content[0].type == "text"
    assert result.content[0].text == "文字摘要"
    assert result.content[1].type == "image"
    assert result.content[1].mimeType == "image/jpeg"
    assert base64.b64decode(result.content[1].data) == b"ABC"
    # 没有图片时仍是合法 CallToolResult（只有文本）
    text_only = agent_core.call_result_with_image("只有文字", None)
    assert len(text_only.content) == 1


# ---------------------------------------------------------------
# 影像原生路径（browse_screenshot / browse_zoom_crop）
# ---------------------------------------------------------------


class _Browser:
    def __init__(self, path):
        self.path = path
        self.calls = 0

    async def screenshot(self, page, save_path):
        self.calls += 1
        from pathlib import Path

        Path(save_path).write_bytes(PNG_1X1)
        return str(save_path)


class _Page:
    url = "https://example.com/spa"
    viewport_size = {"width": 1280, "height": 800}

    async def screenshot(self, clip=None, path=""):
        from pathlib import Path

        Path(path).write_bytes(PNG_1X1)

    async def evaluate(self, script, *args):
        return []


def _prepare(plugin, *, native=True):
    plugin.browser = _Browser(None)

    async def _get_page(event):
        return _Page(), ""

    plugin._get_page = _get_page
    plugin._lock_for = lambda event: _NullLock()
    plugin._vision_cache_invalidate = lambda *a, **k: 0
    plugin.silent_mode = True
    run = agent_core.AgentRun(
        provider_id="prov-inner",
        native_image=native,
        result_max_chars=3000,
        image_max_side=1280,
        image_quality=80,
    )
    return run


class _NullLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def test_screenshot_returns_image_content_when_inner_model_multimodal(tmp_path):
    plugin = _plugin(tmp_path=tmp_path)
    run = _prepare(plugin, native=True)
    with agent_core.run_scope(run):
        out = _run(plugin.browse_screenshot(_Event()))
    assert isinstance(out, mcp_types.CallToolResult), "影像原生必须返回 CallToolResult"
    assert out.content[0].type == "text"
    assert "截图已获取（1280x800" in out.content[0].text
    image = out.content[1]
    assert image.type == "image"
    assert base64.b64decode(image.data) == PNG_1X1, "data 必须是合法 base64"
    assert image.mimeType in ("image/png", "image/jpeg")
    # 直接调工具（绕过 handler）时台账还没记这一步；计数由 handler 路径的
    # Ledger.add 统一做（见 test_handler_...），避免双计把 40 张变成 20 张。
    assert run.ledger.images == 0


def test_screenshot_text_mentions_sent_when_not_silent(tmp_path):
    plugin = _plugin(tmp_path=tmp_path)
    _prepare(plugin, native=True)
    plugin.silent_mode = False
    event = _Event()
    with agent_core.run_scope(agent_core.AgentRun(native_image=True)):
        out = _run(plugin.browse_screenshot(event))
    assert "截图已发送给用户，同时附在本条结果之后。" in out.content[0].text
    assert event.sent, "silent_mode=false 时仍要向会话发图（_emit_media 不变）"


def test_zoom_crop_returns_image_content_with_crop_label(tmp_path):
    plugin = _plugin(tmp_path=tmp_path)
    run = _prepare(plugin, native=True)
    with agent_core.run_scope(run):
        out = _run(plugin.browse_zoom_crop(_Event(), 10, 20, 300, 200))
    assert isinstance(out, mcp_types.CallToolResult)
    assert "裁剪区域 (x,y,w,h)=(10,20,300,200)" in out.content[0].text
    assert out.content[1].type == "image"


def test_text_inner_model_falls_back_to_caption_with_cache(tmp_path):
    """纯文本内层模型 ⇒ 返回 str ``识图结果：`` + 写 1 条内容哈希缓存。"""
    seen = {}

    class _Ctx:
        async def llm_generate(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(completion_text="页面是登录表单")

    plugin = _plugin(
        {"perception": {"vision_provider_id": "prov-vision", "vision_cache_ttl": 60}},
        _Ctx(),
        tmp_path=tmp_path,
    )
    _prepare(plugin, native=False)
    with agent_core.run_scope(agent_core.AgentRun(native_image=False)):
        out = _run(plugin.browse_screenshot(_Event()))
    assert isinstance(out, str)
    assert "识图结果：" in out and "页面是登录表单" in out
    assert len(plugin._vision_cache) == 1
    # 键的第 5 段是**图像内容哈希**（T2 修事故的那个维度）
    key = next(iter(plugin._vision_cache))
    assert key[4] == plugin._vision_image_hash(
        str(plugin._screenshot_dir / _latest_shot_name(plugin))
    )
    assert seen["chat_provider_id"] == "prov-vision"


def _latest_shot_name(plugin) -> str:
    """取截图目录里最新的一张（文件名带时间戳，测试不硬编码）。"""
    files = sorted(plugin._screenshot_dir.glob("browse_*.png"))
    return files[-1].name


def test_caption_cache_key_contains_image_content_hash(tmp_path):
    """兜底路径的缓存键第 5 段 == sha256(图字节)[:32]（T2 的内容哈希）。"""
    class _Ctx:
        async def llm_generate(self, **kwargs):
            return SimpleNamespace(completion_text="描述A")

    plugin = _plugin(
        {"perception": {"vision_provider_id": "prov-vision", "vision_cache_ttl": 60}},
        _Ctx(),
        tmp_path=tmp_path,
    )
    _prepare(plugin, native=False)
    with agent_core.run_scope(agent_core.AgentRun(native_image=False)):
        _run(plugin.browse_screenshot(_Event()))
    key = next(iter(plugin._vision_cache))
    import hashlib

    assert key[4] == hashlib.sha256(PNG_1X1).hexdigest()[:32]


def test_caption_path_does_not_touch_cache_when_inner_model_native(tmp_path):
    """影像原生路径**不读不写** caption 缓存（结构性消灭串号，规格 §2.4.2）。"""
    plugin = _plugin(
        {"perception": {"vision_provider_id": "prov-vision", "vision_cache_ttl": 60}},
        SimpleNamespace(),
        tmp_path=tmp_path,
    )
    run = _prepare(plugin, native=True)
    with agent_core.run_scope(run):
        _run(plugin.browse_screenshot(_Event()))
    assert plugin._vision_cache == {}


def test_unavailable_text_when_no_image_and_no_caption(tmp_path):
    """影像原生失败 + 无识图兜底 ⇒ 【不可用】明确说明（不许静默空串）。"""
    plugin = _plugin(tmp_path=tmp_path)
    run = _prepare(plugin, native=True)
    # 让图片"做不成"：超过 base64 硬上限
    plugin._screenshot_dir.joinpath("big.png")
    run.image_max_side = 320

    async def _huge(path, *, max_side, quality):
        return None

    original = agent_core.async_image_content
    agent_core.async_image_content = _huge
    try:
        with agent_core.run_scope(run):
            out = _run(plugin.browse_screenshot(_Event()))
    finally:
        agent_core.async_image_content = original
    assert isinstance(out, str)
    assert out.startswith("【不可用】")
    assert "perception=text" in out


def test_image_native_refuses_path_outside_screenshot_dir(tmp_path):
    """安全红线：``_image_for_llm`` 只接受本插件截图目录下的路径（§9.3-P6）。"""
    plugin = _plugin(tmp_path=tmp_path)
    _prepare(plugin, native=True)
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(PNG_1X1)
    assert plugin._is_own_screenshot(str(outside)) is False
    inside = _shot(plugin)
    assert plugin._is_own_screenshot(inside) is True
    run = agent_core.AgentRun(native_image=True)
    with agent_core.run_scope(run):
        out = _run(
            plugin._image_native_result(
                str(outside),
                run=run,
                size_label="1x1",
                sent_to_user=False,
                crop=None,
            )
        )
    assert out is None, "非截图目录的路径不得变成模型可读图片"
    assert run.ledger.images == 0


def test_image_budget_cap_reports_text_only(tmp_path):
    """同一任务图片数达上限 ⇒ 返回纯文本说明（不静默丢图）。"""
    plugin = _plugin(tmp_path=tmp_path)
    run = _prepare(plugin, native=True)
    shot = _shot(plugin)
    run.ledger.images = agent_core.MAX_IMAGES_PER_TASK
    out = _run(
        plugin._image_native_result(
            shot, run=run, size_label="1x1", sent_to_user=False, crop=None
        )
    )
    assert isinstance(out, str)
    assert "【预算】" in out and str(agent_core.MAX_IMAGES_PER_TASK) in out


def test_handler_passes_call_tool_result_through(tmp_path):
    """``_make_browser_tool._handler`` 必须原样透传 CallToolResult（不做 str()）。"""
    plugin = _plugin(tmp_path=tmp_path)
    _prepare(plugin, native=True)
    plugin._rebuild_browser_toolset()
    tool = next(t for t in plugin._browser_tools if t.name == "browse_screenshot")
    run = agent_core.AgentRun(native_image=True)
    with agent_core.run_scope(run):
        out = _run(tool.handler(_Event()))
    assert isinstance(out, mcp_types.CallToolResult)
    assert run.ledger.steps == 1, "每步都要进台账"
    assert run.ledger.images == 1, "一张图恰好计一次（双计会让 40 张变 20 张）"
