"""v1.5.0 后台工具链测试（路线 D）：交接文档 §5.2 的 T1–T7 + 三处实现纠偏。

覆盖：
- T1 ``background_mode=off`` 时三个工具**同步**返回真实结果（防回退）；
- T2 ``browse_only`` 时 ``browse_web`` < 1s 返回且带 task_id（前台不再等）；
- T3 任务完成后 ``context.send_message`` 被调用**恰好一次**，消息含结果摘要；
- T4 任务抛错 / 超时也发终态消息（失败可见），且不编造部分结果；
- T5 同一 UMO 第二个后台任务被拒（单飞），不排队、不 await；
- T6 ``terminate()`` 取消在跑任务且不悬挂；
- T7 ``tools.*`` 三键的 schema ⇄ KEY_MAP ⇄ 读取点三方一致；
- F1 入口第一行 ``_refresh_config()``：改配置后**只调入口一次**即生效（开/关双向）；
- F2 媒体按扩展名分流（视频走 Video），缺失文件计入「附件缺失 N 个」；
- F3 日志口径：``后台任务受理`` / ``后台任务终态 status=`` 两条各管一件事。

依赖 conftest.py 的 astrbot 桩（含 MessageChain / Plain / Image / Video）。
"""

from __future__ import annotations

import asyncio
import ast
import json
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as main_module
from astrbot.api.message_components import Image, Plain, Video
from core.background import (
    HEADERS,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    BackgroundRegistry,
    render_accepted_text,
    render_rejected_text,
    render_terminal_text,
    truncate_result,
)
from core.config_map import KEY_MAP, V5_ONLY_PATHS
from main import BrowserLLMPlugin, _BROWSE_WEB_TOOL_NAME, _LOCAL_PAGE_TOOL_NAME

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SRC)

UMO = "aiocqhttp:GroupMessage:123456"
TASK_ID_RE = re.compile(r"task_id=(bg-\d+)")


# --------------------------------------------------------------------
# 夹具：最小 Context / Event / 录制日志
# --------------------------------------------------------------------


class FakeContext:
    """记录 ``send_message`` 调用；可配置子代理与 provider 行为。"""

    def __init__(self, *, reply: str = "子代理结果：示例页正文", delay: float = 0.0,
                 raises: Exception | None = None):
        self.sent: list[tuple[str, object]] = []
        self.reply = reply
        self.delay = delay
        self.raises = raises
        self.tool_calls = 0

    async def get_current_chat_provider_id(self, umo=None):
        return "prov-1"

    async def tool_loop_agent(self, **kwargs):
        self.tool_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(completion_text=self.reply)

    async def send_message(self, session, message_chain):
        self.sent.append((session, message_chain))
        return True


class FakeEvent:
    """最小事件桩：``send`` 会计数（M2 要断言后台态直发次数为 0）。"""

    unified_msg_origin = UMO
    send_calls = 0

    def __init__(self):
        self.send_calls = 0

    def get_group_id(self):
        return "123456"

    def get_sender_id(self):
        return "789"

    def image_result(self, path):
        return f"image:{path}"

    def chain_result(self, chain):
        return f"chain:{chain}"

    async def send(self, msg):
        type(self).send_calls += 1
        self.send_calls += 1
        return None


class RecordingLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def info(self, *a, **k):
        self._add("info", a)

    def warning(self, *a, **k):
        self._add("warning", a)

    def error(self, *a, **k):
        self._add("error", a)

    def debug(self, *a, **k):
        self._add("debug", a)

    def lines(self, level: str = "info") -> list[str]:
        return [text for lvl, text in self.records if lvl == level]

    @property
    def text(self) -> str:
        return "\n".join(text for _lvl, text in self.records)


@pytest.fixture()
def logger_recorder(monkeypatch):
    rec = RecordingLogger()
    monkeypatch.setattr(main_module, "logger", rec)
    return rec


def tools_config(mode: str = "off", **overrides) -> dict:
    tools = {
        "background_mode": mode,
        "background_timeout": 240,
        "background_max_chars": 1500,
    }
    tools.update(overrides)
    return {"tools": tools}


_TMP_DIR = Path(tempfile.mkdtemp(prefix="browser_llm_bg_"))


def make_plugin(config: dict | None = None, context=None) -> BrowserLLMPlugin:
    """构造插件实例：只补齐 ``initialize()`` 才会有的运行期属性。

    这里不调 ``initialize()``（那会真的起浏览器/回收任务），所以手工注入
    子代理工具集与截图目录——它们只是被执行体读，不参与本文件要钉的语义。
    """
    plugin = BrowserLLMPlugin(
        context if context is not None else FakeContext(), config or {}
    )
    plugin.context = context if context is not None else FakeContext()
    plugin._browser_tools = []
    plugin._browser_toolset = SimpleNamespace(tools=[])
    plugin._screenshot_dir = _TMP_DIR / "screenshots"
    plugin._screenshot_dir.mkdir(parents=True, exist_ok=True)
    plugin._media_dir = _TMP_DIR / "media"
    plugin._media_dir.mkdir(parents=True, exist_ok=True)
    # terminate() 会在 initialize() 之前读到这两个句柄（幂等清理的入口）
    plugin._cache_cleanup_task = None
    plugin._vision_sync_task = None
    return plugin


def freeze_budget(plugin: BrowserLLMPlugin, seconds: float) -> None:
    """把整任务硬预算钉到 ``seconds``（毫秒级用例用）。

    为什么不能只改配置：入口第一行 ``_refresh_config()`` 会按 schema 的钳制
    区间（30-900s）重新读配置，属性级的小数值会被覆盖。这里停掉热更新，
    让"预算到点被中断"这条路径能被毫秒级用例覆盖；生产路径不受影响。
    """
    plugin._refresh_config = lambda: None  # type: ignore[assignment]
    plugin.background_timeout = seconds


def run(coro):
    return asyncio.run(coro)


def task_id_of(text: str) -> str:
    match = TASK_ID_RE.search(text)
    assert match is not None, f"返回文案里没有 task_id: {text!r}"
    return match.group(1)


def message_text(chain) -> str:
    return "\n".join(part.text for part in chain.chain if isinstance(part, Plain))


# --------------------------------------------------------------------
# T1：off 时同步返回真实结果（防回退）
# --------------------------------------------------------------------


def test_t1_off_browse_web_returns_real_result_synchronously():
    ctx = FakeContext()
    plugin = make_plugin(tools_config("off"), ctx)
    out = run(plugin.browse_web(FakeEvent(), "打开示例页并读标题"))
    assert out == ctx.reply, "off 模式下必须同步返回子代理真实结果"
    assert ctx.sent == [], "off 模式不得主动投递任何后台消息"
    assert len(plugin._background) == 0, "off 模式不得登记后台任务"


def test_t1_off_browse_web_keeps_provider_error_text():
    """off 的既有语义：拿不到模型是**同步错误文本**（不是异步终态）。"""
    plugin = make_plugin(tools_config("off"), FakeContext())

    async def no_provider(umo=None):
        return ""

    plugin.context.get_current_chat_provider_id = no_provider  # type: ignore[assignment]
    out = run(plugin.browse_web(FakeEvent(), "任务"))
    assert out.startswith("【错误】无法确定当前对话模型")
    assert plugin.context.sent == []


def test_t1_off_local_page_and_search_stay_foreground():
    plugin = make_plugin(tools_config("off"))
    out = run(plugin.browse_local_page(FakeEvent(), path=""))
    assert out.startswith("【错误】path 不能为空"), out
    payload = json.loads(
        run(plugin.browser_web_search(FakeEvent(), query="测试"))
    )
    assert payload["status"] == "disabled", "搜索工具默认关闭 ⇒ 同步 disabled"


# --------------------------------------------------------------------
# T2：browse_only ⇒ browse_web <1s 返回且带 task_id
# --------------------------------------------------------------------


def test_t2_browse_only_returns_immediately_with_task_id(logger_recorder):
    ctx = FakeContext(delay=0.3)  # 子代理 0.3s；入口必须不等它
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        began = time.monotonic()
        out = await plugin.browse_web(FakeEvent(), "长任务")
        elapsed = time.monotonic() - began
        task_id = task_id_of(out)
        await plugin._background.task_of(task_id)  # 收尾，避免悬挂
        return out, elapsed, task_id

    out, elapsed, task_id = run(flow())
    assert elapsed < 1.0, f"入口必须立刻返回（实测 {elapsed:.3f}s）"
    assert out.startswith("【已受理】"), out
    assert task_id.startswith("bg-")
    # 主模型不得把这句话当成已有结果（§2.6(a) 硬要求）
    assert "还没有" in out and "不要总结" in out
    # 生产判据之一：受理日志行
    assert any("后台任务受理" in line and task_id in line
               for line in logger_recorder.lines("info"))


def test_t2_browse_only_leaves_local_page_and_search_foreground():
    plugin = make_plugin(tools_config("browse_only"))
    out = run(plugin.browse_local_page(FakeEvent(), path=""))
    assert out.startswith("【错误】path 不能为空"), "browse_only 不后台化本地渲染"


# --------------------------------------------------------------------
# T3：完成后 send_message 恰好一次，消息含结果摘要
# --------------------------------------------------------------------


def test_t3_success_delivers_exactly_one_message(logger_recorder):
    ctx = FakeContext(reply="示例页标题是 Example Domain")
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "读标题")
        task_id = task_id_of(out)
        await plugin._background.task_of(task_id)
        return task_id

    task_id = run(flow())
    assert len(ctx.sent) == 1, f"必须恰好投递一次，实测 {len(ctx.sent)} 次"
    session, chain = ctx.sent[0]
    assert session == UMO
    text = message_text(chain)
    assert text.startswith(HEADERS[STATUS_OK]), text
    assert "示例页标题是 Example Domain" in text
    assert task_id in text
    assert len(plugin._background) == 0, "终态后必须出表（同 UMO 可再提交）"
    # F3：终态日志一条一任务，用 `后台任务终态 status=` 口径
    terminal = [ln for ln in logger_recorder.lines("info") if "后台任务终态" in ln]
    assert len(terminal) == 1 and "status=ok" in terminal[0]
    # 终态消息首行另打一条 info 供对账
    assert any(HEADERS[STATUS_OK] in line for line in logger_recorder.lines())


def test_t3_result_is_truncated_by_configured_chars():
    ctx = FakeContext(reply="X" * 5000)
    plugin = make_plugin(
        tools_config("browse_only", background_max_chars=200), ctx
    )

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "长结果")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert "已省略" in text, "超过 background_max_chars 必须截断并说明"
    assert len(text) < 1500, len(text)


# --------------------------------------------------------------------
# T4：失败 / 超时 / 中断都发终态消息
# --------------------------------------------------------------------


def test_t4_failure_sends_terminal_message():
    ctx = FakeContext(raises=RuntimeError("浏览器已关闭"))
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "会失败的任务")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    assert len(ctx.sent) == 1
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_FAILED]), text
    assert "浏览器已关闭" in text and "本次没有可用结果" in text
    assert "已得部分结果" not in text, "browse_web 没有部分结果，不得编造"


def test_t4_timeout_sends_terminal_message_without_fabricating():
    ctx = FakeContext(delay=5.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)
    freeze_budget(plugin, 0.05)  # 提交时快照进任务

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "超时任务")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_TIMEOUT]), text
    assert "硬预算，任务被中断" in text
    assert "已得部分结果" not in text, "没有 progress 时不得编造部分结果"
    # O-2：超时且无半成品必须明说「没有可用结果」（README 已写死这条口径）
    assert "本次没有可用结果。" in text


def test_t4_error_prefixed_text_is_reported_as_failure():
    """工具以【错误】前缀当文本返回时，不能报成「完成」（否则用户看到完成里包错误）。"""
    ctx = FakeContext(reply="【错误】无法确定当前对话模型，浏览器子代理不可用。")
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "任务")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_FAILED]), text
    assert "无法确定当前对话模型" in text


def test_t4_cancelled_sends_interrupt_message():
    ctx = FakeContext(delay=5.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "会被取消")
        task = plugin._background.task_of(task_id_of(out))
        await asyncio.sleep(0)  # 让任务先跑到第一个 await（取消"未启动"的任务没有终态）
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_CANCELLED]), text
    assert "任务被取消" in text


# --------------------------------------------------------------------
# T4 附加：部分结果（local_page progress / search progress）
# --------------------------------------------------------------------


class _HangingPage:
    def __init__(self):
        self.screenshot_calls = 0

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        return None

    async def screenshot(self, path=None, full_page=False):
        self.screenshot_calls += 1
        await asyncio.sleep(30)  # 挂在截图阶段：文本已拿到，截图没完成

    def is_closed(self):
        return False


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def close_page(self, page):
        return None

    async def screenshot(self, page, save_path):
        await asyncio.sleep(30)
        return ""


class _FakeExtractor:
    async def extract_page_info(self, page):
        return {"title": "本地页标题"}

    async def extract_text(self, page, max_chars=4000):
        return "本地页面已渲染出来的正文"


def test_t4_local_page_timeout_carries_partial_text(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<html><body>hi</body></html>", encoding="utf-8")
    ctx = FakeContext()
    plugin = make_plugin(tools_config("all"), ctx)
    freeze_budget(plugin, 0.05)
    plugin._local_page_allowed_roots = (tmp_path,)
    plugin.browser = _FakeBrowser(_HangingPage())
    plugin.extractor = _FakeExtractor()

    async def flow():
        out = await plugin.browse_local_page(FakeEvent(), path=str(html))
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_TIMEOUT]), text
    assert "已得部分结果" in text and "本地页面已渲染出来的正文" in text


def test_t4_search_timeout_carries_partial_results(monkeypatch):
    """搜索的增量快照（progress）必须能被超时终态渲染出来。"""
    ctx = FakeContext()
    plugin = make_plugin(
        {"search": {"enabled": True}, "tools": {"background_mode": "all"}}, ctx
    )
    freeze_budget(plugin, 0.05)

    async def runner(event, progress=None, **kwargs):
        # 入口把 event 位置传参、progress 关键字传参（与真实签名同形）
        assert isinstance(progress, dict)
        progress["results"] = [SimpleNamespace(title="部分标题", url="https://a.example")]
        await asyncio.sleep(5)

    plugin._run_browser_web_search = runner  # type: ignore[assignment]

    async def flow():
        out = await plugin.browser_web_search(FakeEvent(), query="测试")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_TIMEOUT]), text
    assert "已得部分结果" in text and "部分标题" in text


# --------------------------------------------------------------------
# T5：同 UMO 单飞（不排队、不 await）
# --------------------------------------------------------------------


def test_t5_second_task_same_umo_is_rejected():
    ctx = FakeContext(delay=5.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        first = await plugin.browse_web(FakeEvent(), "第一个任务")
        await asyncio.sleep(0.01)  # 让第一个任务真的跑起来
        began = time.monotonic()
        second = await plugin.browse_web(FakeEvent(), "第二个任务")
        elapsed = time.monotonic() - began
        first_id = task_id_of(first)
        await plugin._background.cancel_all(timeout=2.0)
        return first, second, elapsed, first_id

    first, second, elapsed, first_id = run(flow())
    assert first.startswith("【已受理】")
    assert second.startswith("【拒绝】"), second
    assert first_id in second, "拒绝文案要带上在跑任务的 task_id"
    assert elapsed < 1.0, f"单飞裁决必须立刻返回（实测 {elapsed:.3f}s）"
    assert "不要换参数重试" in second
    assert ctx.tool_calls == 1, "被拒绝的请求不得真的开始执行"


def test_t5_single_flight_isolated_per_umo():
    ctx = FakeContext(delay=5.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    class OtherEvent(FakeEvent):
        unified_msg_origin = "aiocqhttp:GroupMessage:999999"

        def get_group_id(self):
            return "999999"

    async def flow():
        first = await plugin.browse_web(FakeEvent(), "任务A")
        other = await plugin.browse_web(OtherEvent(), "任务B")
        await plugin._background.cancel_all(timeout=2.0)
        return first, other

    first, other = run(flow())
    assert first.startswith("【已受理】") and other.startswith("【已受理】"), (
        "不同会话互不影响"
    )


def test_t5_background_chain_never_takes_locks():
    """结构断言：后台执行链里不得出现 _lock_for / get_lock（否则只是把死锁挪个位置）。"""
    chain = ("_submit_background", "_run_background_job", "_browse_web_run")
    for name in chain:
        node = next(
            (n for n in ast.walk(MAIN_TREE)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
            None,
        )
        assert node is not None, f"缺少后台执行链方法 {name}"
        body = ast.get_source_segment(MAIN_SRC, node) or ""
        assert "_lock_for(" not in body, f"{name} 不得取 per-UMO 锁"
        assert "get_lock(" not in body, f"{name} 不得取会话锁"


# --------------------------------------------------------------------
# T6：terminate 取消且不悬挂
# --------------------------------------------------------------------


def test_t6_terminate_cancels_running_task_without_hanging():
    ctx = FakeContext(delay=30.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "长任务")
        task = plugin._background.task_of(task_id_of(out))
        began = time.monotonic()
        await plugin.terminate()
        elapsed = time.monotonic() - began
        return task, elapsed

    task, elapsed = run(flow())
    assert elapsed < 10.0, f"terminate 不得悬挂（实测 {elapsed:.1f}s）"
    assert task is not None and task.done(), "在跑任务必须被取消"
    assert plugin._background.running() == []
    assert len(plugin._background) == 0, "terminate 后登记表必须清空（§6.1 兜底）"
    # 取消也是终态：中断消息 best-effort 发出去（有就断言前缀）
    if ctx.sent:
        assert message_text(ctx.sent[-1][1]).startswith(HEADERS[STATUS_CANCELLED])


def test_t6_cancel_all_is_safe_when_nothing_runs():
    registry = BackgroundRegistry()
    assert run(registry.cancel_all(1.0)) == 0
    assert registry.running() == []


# --------------------------------------------------------------------
# T7：tools.* 三键的 schema ⇄ KEY_MAP ⇄ 读取点 三方一致
# --------------------------------------------------------------------


TOOLS_KEYS = {
    "tools.background_mode": ("background_mode", "off"),
    "tools.background_timeout": ("background_timeout", 240),
    "tools.background_max_chars": ("background_max_chars", 1500),
}


def test_t7_schema_leaves_exist_with_expected_defaults():
    items = SCHEMA["tools"]["items"]
    assert set(items) == {path.split(".", 1)[1] for path in TOOLS_KEYS}
    for path, (_attr, default) in TOOLS_KEYS.items():
        leaf = items[path.split(".", 1)[1]]
        assert leaf.get("description") and leaf.get("hint"), path
        assert leaf["default"] == default, path
    assert set(items["background_mode"]["options"]) == {"off", "browse_only", "all"}
    # 默认值必须是 off：行为与 v1.4.0 一致是硬要求
    assert items["background_mode"]["default"] == "off"


def test_t7_key_map_declares_tools_paths_without_flat_fallback():
    assert set(V5_ONLY_PATHS) == set(TOOLS_KEYS)
    for path in TOOLS_KEYS:
        assert path in KEY_MAP
        assert KEY_MAP[path] is None, "新键没有 v3 扁平旧键"


def test_t7_load_config_reads_every_tools_key_with_same_default():
    """读取点三方一致：`view.get("tools.X", <default>)` 且默认值与 schema 相同。"""
    load_section = MAIN_SRC.split("def _load_config")[1].split("def _refresh_config")[0]
    for path, (_attr, default) in TOOLS_KEYS.items():
        pattern = re.compile(
            r'view\.get\(\s*"' + re.escape(path) + r'"\s*,\s*'
            + (re.escape(json.dumps(default)) if isinstance(default, str)
               else str(default))
            + r"\s*\)"
        )
        assert pattern.search(load_section), f"{path} 的读取点/默认值没跟上 schema"


def test_t7_load_config_populates_attributes():
    plugin = make_plugin(
        {
            "tools": {
                "background_mode": "browse_only",
                "background_timeout": 300,
                "background_max_chars": 800,
            }
        }
    )
    assert plugin.background_mode == "browse_only"
    assert plugin.background_timeout == 300
    assert plugin.background_max_chars == 800


def test_t7_defaults_when_group_missing_and_clamped():
    plugin = make_plugin({})
    assert plugin.background_mode == "off"
    assert plugin.background_timeout == 240
    assert plugin.background_max_chars == 1500
    assert BrowserLLMPlugin.background_mode == "off"
    assert BrowserLLMPlugin.background_timeout == 240
    assert BrowserLLMPlugin.background_max_chars == 1500

    clamped = make_plugin(
        {
            "tools": {
                "background_mode": "ALL",  # 大小写归一
                "background_timeout": 5,  # 钳到 30
                "background_max_chars": 99999,  # 钳到 8000
            }
        }
    )
    assert clamped.background_mode == "all"
    assert clamped.background_timeout == 30
    assert clamped.background_max_chars == 8000


def test_t7_illegal_mode_falls_back_to_off_with_warning(logger_recorder):
    plugin = make_plugin({"tools": {"background_mode": "backgroudn"}})
    assert plugin.background_mode == "off"
    assert any("非法" in line for line in logger_recorder.lines("warning"))


# --------------------------------------------------------------------
# F1：入口第一行刷新配置（开/关两个方向都要一次生效）
# --------------------------------------------------------------------


def test_f1_mode_switch_applies_on_next_single_call():
    ctx = FakeContext(delay=5.0)
    plugin = make_plugin(tools_config("off"), ctx)

    async def flow():
        # ① off → 后台：只调一次入口就生效
        foreground = await plugin.browse_web(FakeEvent(), "第一轮")
        plugin.config["tools"]["background_mode"] = "browse_only"
        accepted = await plugin.browse_web(FakeEvent(), "第二轮")
        await plugin._background.cancel_all(timeout=2.0)
        return foreground, accepted

    foreground, accepted = run(flow())
    assert foreground == ctx.reply, "off 那一轮必须是同步结果"
    assert accepted.startswith("【已受理】"), "改配置后下一次调用就必须后台化"


def test_f1_mode_switch_back_to_off_applies_immediately():
    ctx = FakeContext(delay=0.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        accepted = await plugin.browse_web(FakeEvent(), "第一轮")
        await plugin._background.task_of(task_id_of(accepted))
        plugin.config["tools"]["background_mode"] = "off"
        back = await plugin.browse_web(FakeEvent(), "第二轮")
        return accepted, back

    accepted, back = run(flow())
    assert accepted.startswith("【已受理】")
    assert back == ctx.reply, "改回 off 后必须立刻恢复同步返回（关得掉）"


# --------------------------------------------------------------------
# F2：媒体按扩展名分流 + 缺失文件计数
# --------------------------------------------------------------------


def test_f2_media_dispatch_by_extension(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"png")
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"mp4")
    missing = tmp_path / "gone.png"

    ctx = FakeContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)
    job = main_module.BackgroundJob(
        task_id="bg-0001",
        umo=UMO,
        tool=_BROWSE_WEB_TOOL_NAME,
        started_at=time.monotonic(),
        budget_s=240.0,
        max_chars=1500,
    )

    delivered, first_line = run(
        plugin._deliver_terminal(
            job,
            STATUS_OK,
            "结果正文",
            "",
            1234,
            [str(png), str(mp4), str(missing)],
            "",
        )
    )
    assert delivered is True
    assert first_line.startswith(HEADERS[STATUS_OK])
    chain = ctx.sent[0][1].chain
    assert isinstance(chain[0], Plain)
    assert isinstance(chain[1], Image), "png 必须走 Image"
    assert isinstance(chain[2], Video), "mp4 必须走 Video（browse_sniff_media 会收视频）"
    text = message_text(ctx.sent[0][1])
    assert "附件缺失 1 个" in text, "缺失文件必须计数并提示"


def test_f2_media_only_attached_on_success(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"png")
    ctx = FakeContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)
    job = main_module.BackgroundJob(
        task_id="bg-0002",
        umo=UMO,
        tool=_BROWSE_WEB_TOOL_NAME,
        started_at=time.monotonic(),
    )
    run(plugin._deliver_terminal(job, STATUS_FAILED, "", "boom", 10, [str(png)]))
    assert [type(part) for part in ctx.sent[0][1].chain] == [Plain], (
        "失败态不得附媒体（半成品只会误导）"
    )


# --------------------------------------------------------------------
# F3：两条日志各管一件事（口径不双计）
# --------------------------------------------------------------------


def test_f3_terminal_log_is_status_scoped(logger_recorder):
    ctx = FakeContext(raises=RuntimeError("boom"))
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "失败任务")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    info = logger_recorder.lines("info")
    assert any("后台任务受理" in line for line in info)
    terminal = [line for line in info if "后台任务终态" in line]
    assert len(terminal) == 1
    assert "status=failed" in terminal[0]
    # 失败任务不得出现在「完成」口径里（否则 grep 会把失败算成功）
    assert not any("后台任务完成" in line for line in info)




# --------------------------------------------------------------------
# D2：全局并发上限（进程级硬约束，不是配置键）
# --------------------------------------------------------------------


def test_d2_global_concurrency_cap_rejects_fifth_task():
    """4 个在跑任务之后，第 5 个（换 UMO）被**同步**拒绝且登记表长度不变。"""
    ctx = FakeContext(delay=30.0)
    plugin = make_plugin(tools_config("browse_only"), ctx)

    class _OtherEvent(FakeEvent):
        def __init__(self, n: int):
            self.unified_msg_origin = f"aiocqhttp:GroupMessage:{n}"

        def get_group_id(self):
            return self.unified_msg_origin.rsplit(":", 1)[-1]

    async def flow():
        accepted = []
        for n in range(main_module._BG_MAX_CONCURRENT if hasattr(
            main_module, "_BG_MAX_CONCURRENT"
        ) else 4):
            accepted.append(await plugin.browse_web(_OtherEvent(n), f"任务{n}"))
        await asyncio.sleep(0.01)  # 让四个任务真的跑起来（running() 依赖 task 状态）
        before = len(plugin._background)
        fourth_umo = f"aiocqhttp:GroupMessage:{999}"
        plugin_event = _OtherEvent(999)
        plugin_event.unified_msg_origin = fourth_umo
        began = time.monotonic()
        rejected = await plugin.browse_web(_OtherEvent(999), "第五个任务")
        elapsed = time.monotonic() - began
        after = len(plugin._background)
        await plugin._background.cancel_all(timeout=2.0)
        return accepted, rejected, before, after, elapsed

    accepted, rejected, before, after, elapsed = run(flow())
    assert len(accepted) == 4 and all(text.startswith("【已受理】") for text in accepted)
    assert rejected.startswith("【拒绝】"), rejected
    assert "并发上限（4）" in rejected and "请稍后再试" in rejected
    assert "task_id" not in rejected, "全局拒绝不占 task_id"
    assert after == before == 4, f"被拒的请求不得入表（{before} -> {after}）"
    assert elapsed < 1.0, f"全局闸必须同步拒绝（实测 {elapsed:.3f}s）"


def test_d2_global_cap_constant_is_not_a_config_key():
    """上限是模块常量（4），schema 不得出现第 4 个 tools 叶。"""
    assert main_module._BG_MAX_CONCURRENT == 4
    assert len(SCHEMA["tools"]["items"]) == 3, sorted(SCHEMA["tools"]["items"])
    assert not any("concurrent" in key for key in SCHEMA["tools"]["items"])


# --------------------------------------------------------------------
# F2 具名钉子（设计 §9.2）：视频分流与缺失附件
# --------------------------------------------------------------------


class _MediaRunner:
    """假执行体：跑的时候经 _emit_media 登记一个媒体（模拟子工具截图/下载）。"""

    def __init__(self, plugin, path: str):
        self.plugin = plugin
        self.path = path

    async def __call__(self, progress):
        await self.plugin._emit_media(FakeEvent(), self.path, "video")
        return "任务结果"


def test_terminal_chain_uses_video_for_video_artifact(tmp_path):
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"mp4")
    png = tmp_path / "shot.png"
    png.write_bytes(b"png")

    for path, expected_type, label in (
        (mp4, Video, "视频必须走 Video"),
        (png, Image, "截图必须走 Image"),
    ):
        ctx = FakeContext()
        plugin = make_plugin(tools_config("browse_only"), ctx)
        plugin._browser_toolset = SimpleNamespace(tools=[])

        async def flow(target=path):
            out = await plugin._submit_background(
                event=FakeEvent(),
                tool_name=_BROWSE_WEB_TOOL_NAME,
                runner=_MediaRunner(plugin, str(target)),
            )
            await plugin._background.task_of(task_id_of(out))

        run(flow())
        chain = ctx.sent[0][1].chain
        assert compare_type(chain[-1]) is expected_type, label
        assert type(chain[-1]).__name__ == expected_type.__name__


def compare_type(component):
    """返回组件类型（避免直接 isinstance 让桩/真实组件差异干扰断言）。"""
    return type(component)


def test_terminal_skips_missing_artifact(tmp_path):
    """文件已被缓存清理/移动：跳过附件并计数，文本必须照发。"""
    gone = tmp_path / "gone.mp4"
    ctx = FakeContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)
    job = main_module.BackgroundJob(
        task_id="bg-0003", umo=UMO, tool=_BROWSE_WEB_TOOL_NAME,
        started_at=time.monotonic(),
    )
    delivered, _first = run(
        plugin._deliver_terminal(job, STATUS_OK, "正文", "", 5, [str(gone)], "")
    )
    assert delivered is True
    chain = ctx.sent[0][1].chain
    assert [type(part).__name__ for part in chain] == ["Plain"], "缺失文件不得构造附件"
    assert "附件缺失 1 个" in message_text(ctx.sent[0][1])


def test_log_keywords_are_unambiguous():
    """F3 静态钉子：日志关键字唯一，消息前缀不得塞回日志行。

    (a) 源码存在 ``后台任务终态 status=``；(b) ``后台任务完成`` 不出现在任何
    ``logger.*`` 调用的字面量里（它只是消息前缀）；(c) ``后台任务受理`` 至少
    出现一次。理由：一旦消息前缀进了日志行，``grep -c`` 会双计，而这类回归
    抓不到任何功能失败。
    """
    for path in (ROOT / "main.py", ROOT / "core" / "background.py"):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        logger_literals: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        logger_literals.append(sub.value)
        joined = "\n".join(logger_literals)
        assert "后台任务完成" not in joined, f"{path.name} 把消息前缀写进了日志行"
    assert "后台任务终态 status=" in MAIN_SRC
    assert "后台任务受理" in MAIN_SRC


# --------------------------------------------------------------------
# 纯函数：文案与截断（不依赖插件实例）
# --------------------------------------------------------------------


def test_accepted_text_forbids_summarizing():
    """受理文案逐字对齐设计 §2.6(a)：没有结果 / 禁止编造 / 唯一动作 / 禁止重复提交。"""
    text = render_accepted_text(task_id="bg-0007", tool="browse_web", budget_s=240)
    assert text.startswith("【已受理】")
    assert "task_id=bg-0007" in text and "整任务硬预算 240 秒" in text
    assert "单独作为一条消息" in text
    for header in HEADERS.values():
        assert header in text, "受理文案要点名四态前缀，便于用户对账"
    assert "【重要】你现在**还没有**任何结果" in text
    assert "不要总结、不要猜测、不要编造浏览内容" in text
    assert '用一句话告诉用户"已开始处理，完成后会单独发结果"' in text
    assert "不要重复提交同一任务" in text


def test_rejected_text_mentions_task_and_action():
    text = render_rejected_text(task_id="bg-0006", tool="browse_web", elapsed_s=12.4)
    assert text.startswith("【拒绝】"), "拒绝统一用【拒绝】，不再造第二种拒绝前缀"
    assert "bg-0006" in text and "已运行 12 秒" in text
    assert "不要换参数重试" in text


def test_four_headers_are_distinct():
    values = list(HEADERS.values())
    assert len(set(values)) == 4, values
    assert HEADERS[STATUS_OK] == "【后台任务完成】"
    assert HEADERS[STATUS_FAILED] == "【后台任务失败】"
    assert HEADERS[STATUS_TIMEOUT] == "【后台任务超时】"
    assert HEADERS[STATUS_CANCELLED] == "【后台任务已取消】"


def test_terminal_text_shapes():
    ok = render_terminal_text(
        task_id="bg-0001", tool=_BROWSE_WEB_TOOL_NAME, status=STATUS_OK,
        text="正文", elapsed_ms=1234, artifact_count=2,
    )
    assert ok.startswith("【后台任务完成】") and "1.2s" in ok and "2 个媒体文件" in ok
    timeout = render_terminal_text(
        task_id="bg-0002", tool=_LOCAL_PAGE_TOOL_NAME, status=STATUS_TIMEOUT,
        budget_s=240, partial="部分文本",
    )
    assert timeout.startswith("【后台任务超时】") and "240 秒" in timeout
    assert "已得部分结果" in timeout and "部分文本" in timeout
    failed = render_terminal_text(
        task_id="bg-0003", tool=_BROWSE_WEB_TOOL_NAME, status=STATUS_FAILED,
        reason="RuntimeError: boom",
    )
    assert failed.startswith("【后台任务失败】") and "boom" in failed
    assert "本次没有可用结果" in failed
    cancelled = render_terminal_text(
        task_id="bg-0004", tool=_BROWSE_WEB_TOOL_NAME, status=STATUS_CANCELLED
    )
    assert cancelled.startswith("【后台任务已取消】")


def test_truncate_result_keeps_short_and_marks_long():
    assert truncate_result("短文本", 10) == ("短文本", False, 0)
    text, truncated, omitted = truncate_result("A" * 20, 5)
    assert truncated is True and omitted == 15
    assert text.startswith("AAAAA") and "已省略 15 字" in text


def test_registry_single_flight_is_a_pure_table_lookup():
    registry = BackgroundRegistry()
    job = main_module.BackgroundJob(
        task_id=registry.next_task_id(), umo=UMO, tool="browse_web", started_at=0.0
    )
    registry.register(job)
    # 已登记但任务还没 attach：按"占用中"处理（宁可不放行也不要双开）
    assert registry.active_task_id(UMO) == job.task_id

    async def flow():
        task = asyncio.create_task(asyncio.sleep(5))
        registry.attach_task(job.task_id, task)
        busy = registry.active_job(UMO)
        assert busy is not None and busy.task_id == job.task_id
        registry.finish(job.task_id)
        assert registry.active_job(UMO) is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(flow())


# ====================================================================
# H1 + H2：搜索终态必须人读化，且可预期失败不得算成功
#   根因是设计 §2.4 的 per-tool 人读钩子（_background_user_text）缺失：
#   返回体是 JSON，直接塞进终态消息既读不懂，失败态还会逃过「【错误】」前缀
#   判定，让 grep -c "后台任务终态 status=ok" 在"搜索全挂"时照样 +1。
# ====================================================================


def search_payload(**kwargs) -> str:
    """用真实的 render_payload 造返回体（字段来源与生产逐字一致）。"""
    from core.search import MergedResult, SearchAttempt, render_payload

    payload = {
        "status": "ok",
        "code": "ok",
        "summary": "找到 2 条结果",
        "query": "北京天气",
        "results": [
            MergedResult(rank=1, title="天气 1", url="https://a.example/1", snippet="晴 26 度"),
            MergedResult(rank=2, title="天气 2", url="https://b.example/2", snippet="多云 24 度"),
        ],
        "attempts": [
            SearchAttempt(provider="brave#1", template="brave", round=0, outcome="ok", code="ok")
        ],
    }
    payload.update(kwargs)
    return render_payload(**payload)


def search_plugin(ctx, mode: str = "all") -> BrowserLLMPlugin:
    plugin = make_plugin(
        {"search": {"enabled": True, "providers": []}, "tools": {"background_mode": mode}},
        ctx,
    )
    plugin._browser_toolset = SimpleNamespace(tools=[])
    return plugin


def run_search_with_body(body: str, ctx=None):
    """把执行体换成"直接返回给返回体"的桩，跑完一个后台搜索任务。"""
    ctx = ctx or FakeContext()
    plugin = search_plugin(ctx)
    plugin._run_browser_web_search = lambda event, **kw: _returning(body)  # type: ignore[assignment]

    async def flow():
        out = await plugin.browser_web_search(FakeEvent(), query="北京天气")
        assert out.startswith("【已受理】"), out
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    return plugin, ctx


async def _returning(body: str) -> str:
    return body


def test_h1_search_success_terminal_is_human_readable():
    """成功态正文必须是摘要，不能是原始 JSON（H1）。"""
    _plugin, ctx = run_search_with_body(search_payload())
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_OK]), text
    body = text.split("\n", 1)[1]
    assert not body.lstrip().startswith("{"), "终态正文不得以 JSON 花括号开头"
    assert '"status"' not in body and '"results"' not in body, "终态正文不得含 JSON 字段名"
    assert "检索词：北京天气" in body
    assert "命中 2 条结果" in body
    assert "1. 天气 1" in body and "https://a.example/1" in body
    assert "晴 26 度" in body
    assert "后端：brave#1/ok/ok" in body


def test_h1_search_truncation_applies_to_rendered_text():
    """截断只作用于渲染后的文本；不得把提示拼进 JSON 中间（H1）。"""
    from core.search import MergedResult

    rows = [
        MergedResult(rank=i, title=f"标题{i}", url=f"https://x.example/{i}", snippet="长" * 200)
        for i in range(1, 9)
    ]
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin.config["tools"]["background_max_chars"] = 300
    plugin._run_browser_web_search = lambda event, **kw: _returning(
        search_payload(results=rows)
    )  # type: ignore[assignment]

    async def flow():
        out = await plugin.browser_web_search(FakeEvent(), query="北京天气")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    text = message_text(ctx.sent[0][1])
    assert "完整结果已省略" in text, "超长人读摘要必须截断并说明"
    body = text.split("\n", 1)[1]
    assert '"status"' not in body and body.count("{") == 0, "截断后的正文里不得残留 JSON"


def test_h2_search_partial_is_success_and_marked_partial():
    """partial → 成功但明确标注「部分结果」（队长裁决）。"""
    _plugin, ctx = run_search_with_body(
        search_payload(status="partial", summary="部分结果：1/2 个 provider 失败")
    )
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_OK]), text
    assert "部分结果" in text


def test_h2_unparseable_search_body_is_failure():
    _plugin, ctx = run_search_with_body("这不是 JSON")
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_FAILED]), text
    assert "返回体无法解析" in text


def test_h2_search_missing_status_is_failure():
    _plugin, ctx = run_search_with_body(json.dumps({"code": "whatever"}))
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_FAILED]), text
    assert "返回体无法解析" in text


FAILURE_BODIES = {
    "empty_query": {"status": "failed", "code": "empty_query", "summary": "【错误】检索词为空"},
    "session_denied": {"status": "failed", "code": "session_denied", "summary": "【拒绝】黑名单"},
    "banned": {"status": "failed", "code": "banned", "summary": "【拒绝】禁词"},
    "no_provider": {"status": "failed", "code": "no_provider", "summary": "【错误】没有可用后端"},
    "internal_error": {"status": "failed", "code": "internal_error", "summary": "【错误】内部异常"},
    "wave_failed": {"status": "failed", "code": "auth", "summary": "【错误】全部 provider 鉴权失败"},
}


@pytest.mark.parametrize("name,fields", sorted(FAILURE_BODIES.items()))
def test_h2_expected_search_failures_are_failures(name, fields, logger_recorder):
    """七类可预期失败里除 total_timeout 外都必须报「失败」而不是「完成」。"""
    _plugin, ctx = run_search_with_body(search_payload(**fields))
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_FAILED]), (name, text)
    terminal = [ln for ln in logger_recorder.lines("info") if "后台任务终态" in ln]
    assert terminal and "status=failed" in terminal[-1], (name, terminal)


def test_h2_search_total_timeout_is_reported_as_timeout(logger_recorder):
    """total_timeout → 超时（不是完成、也不是普通失败）。"""
    _plugin, ctx = run_search_with_body(
        search_payload(
            status="failed",
            code="total_timeout",
            summary="【错误】搜索超过总预算被中断",
            hint="可调大 search.total_timeout",
        )
    )
    text = message_text(ctx.sent[0][1])
    assert text.startswith(HEADERS[STATUS_TIMEOUT]), text
    assert "total_timeout" in text
    terminal = [ln for ln in logger_recorder.lines("info") if "后台任务终态" in ln]
    assert terminal and "status=timeout" in terminal[-1]
    # O-2：超时且无半成品时明说「本次没有可用结果」
    assert "本次没有可用结果。" in text


def test_h2_search_failures_never_count_as_success(logger_recorder):
    """统计型断言：失败场景下 status=ok 与「【后台任务完成】」计数都必须为 0。"""
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    bodies = [search_payload(**fields) for fields in FAILURE_BODIES.values()]

    async def flow():
        for index, body in enumerate(bodies):
            plugin._run_browser_web_search = (
                lambda event, _b=body, **kw: _returning(_b)
            )  # type: ignore[assignment]
            out = await plugin.browser_web_search(FakeEvent(), query=f"q{index}")
            await plugin._background.task_of(task_id_of(out))

    run(flow())
    info = logger_recorder.lines("info")
    ok_lines = [ln for ln in info if "后台任务终态 status=ok" in ln]
    done_lines = [ln for ln in info if HEADERS[STATUS_OK] in ln]
    assert ok_lines == [], f"失败场景不得出现 status=ok: {ok_lines}"
    assert done_lines == [], f"失败场景不得出现【后台任务完成】: {done_lines}"
    sent_texts = [message_text(chain) for _s, chain in ctx.sent]
    assert len(sent_texts) == len(bodies)
    assert all(not text.startswith(HEADERS[STATUS_OK]) for text in sent_texts)


def test_h2_real_path_empty_query_and_no_provider_are_failures(logger_recorder):
    """不桩执行体，走真实代码路径：空检索词 / 无可用 provider 都必须是失败。"""
    ctx = FakeContext()
    plugin = search_plugin(ctx)

    async def flow():
        first = await plugin.browser_web_search(FakeEvent(), query="   ")
        await plugin._background.task_of(task_id_of(first))
        second = await plugin.browser_web_search(FakeEvent(), query="北京天气")
        await plugin._background.task_of(task_id_of(second))

    run(flow())
    texts = [message_text(chain) for _s, chain in ctx.sent]
    assert len(texts) == 2
    assert all(text.startswith(HEADERS[STATUS_FAILED]) for text in texts), texts
    assert "empty_query" in texts[0] and "no_provider" in texts[1]
    terminal = [ln for ln in logger_recorder.lines("info") if "后台任务终态" in ln]
    assert len(terminal) == 2 and all("status=failed" in ln for ln in terminal)


# ====================================================================
# M1：all 模式下的同步拒绝（不得先「已受理」再异步失败）
# ====================================================================


def _assert_sync_rejection(plugin, coro_factory, expect_prefixes):
    async def flow():
        began = time.monotonic()
        out = await coro_factory()
        elapsed = time.monotonic() - began
        return out, elapsed, len(plugin._background)

    out, elapsed, registered = run(flow())
    assert out.startswith(expect_prefixes), out
    assert elapsed < 1.0, f"同步拒绝必须立刻返回（实测 {elapsed:.3f}s）"
    assert "task_id" not in out, f"拒绝不得携带 task_id: {out}"
    assert registered == 0, "拒绝不得登记任务"
    return out


def test_m1_all_mode_session_blacklist_is_synchronous(tmp_path):
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin.config["security"] = {"session_blacklist": ["123456"]}
    _assert_sync_rejection(
        plugin, lambda: plugin.browse_web(FakeEvent(), "越权会话的任务"), ("【拒绝】",)
    )


def test_m1_all_mode_banned_task_is_synchronous():
    from core.safety import SafetyFilter

    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin.safety = SafetyFilter(["赌博"], True)
    out = _assert_sync_rejection(
        plugin,
        # perception 前缀必须被剔除后再做禁词判定（与 v1.4.0 口径一致）
        lambda: plugin.browse_web(FakeEvent(), "perception=text 帮我查赌博网站"),
        ("【拒绝】",),
    )
    assert "赌博" in out


def test_m1_all_mode_local_page_missing_file_is_synchronous(tmp_path):
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin._local_page_allowed_roots = (tmp_path,)
    _assert_sync_rejection(
        plugin,
        lambda: plugin.browse_local_page(FakeEvent(), path=str(tmp_path / "nope.html")),
        ("【错误】", "【拒绝】"),
    )


def test_m1_all_mode_local_page_bad_extension_is_synchronous(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("x", encoding="utf-8")
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin._local_page_allowed_roots = (tmp_path,)
    out = _assert_sync_rejection(
        plugin,
        lambda: plugin.browse_local_page(FakeEvent(), path=str(target)),
        ("【拒绝】",),
    )
    assert ".html" in out


def test_m1_all_mode_local_page_path_escape_is_synchronous(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("<html></html>", encoding="utf-8")
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin._local_page_allowed_roots = (allowed,)
    out = _assert_sync_rejection(
        plugin,
        lambda: plugin.browse_local_page(FakeEvent(), path=str(outside)),
        ("【拒绝】",),
    )
    assert "路径不在允许范围内" in out


def test_m1_all_mode_search_denied_session_is_synchronous():
    """搜索工具在 all 模式下也必须在提交前拒绝黑名单会话。"""
    ctx = FakeContext()
    plugin = search_plugin(ctx)
    plugin.config["security"] = {"session_blacklist": ["123456"]}
    _assert_sync_rejection(
        plugin,
        lambda: plugin.browser_web_search(FakeEvent(), query="北京天气"),
        ("【拒绝】",),
    )


# ====================================================================
# M2：artifact_sink 必须在任务体内；后台态 event.send 直发为 0
# ====================================================================


def test_m2_artifact_sink_is_inside_background_job():
    """结构断言：sink 必须开在 _run_background_job 里，入口/提交侧一律不许开。

    为什么不能只靠行为断言：设计 §5.3-B 的**反模式**（入口 `with artifact_sink()`
    后立刻退出，再让任务体在无 sink 的上下文里跑）在"只断言 chain 末段类型"的
    用例下同样绿 —— 只有结构断言能把它打红。
    """
    node = next(
        (n for n in ast.walk(MAIN_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_background_job"),
        None,
    )
    assert node is not None
    assert "with artifact_sink(" in (ast.get_source_segment(MAIN_SRC, node) or "")
    for forbidden in (
        "_submit_background",
        "browse_web",
        "browse_local_page",
        "browser_web_search",
    ):
        dead = next(
            (n for n in ast.walk(MAIN_TREE)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == forbidden),
            None,
        )
        assert dead is not None, forbidden
        body = ast.get_source_segment(MAIN_SRC, dead) or ""
        assert "artifact_sink(" not in body, f"{forbidden} 不得开 sink（§5.3-B 反模式）"


def test_m2_background_never_calls_event_send(tmp_path):
    """后台任务里 event.send 直发次数必须为 0（媒体只能经 sink 进附件）。"""
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"mp4")
    event = FakeEvent()
    event.send_calls = 0
    ctx = FakeContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin._submit_background(
            event=event,
            tool_name=_BROWSE_WEB_TOOL_NAME,
            runner=_MediaRunner(plugin, str(mp4)),
        )
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    assert event.send_calls == 0, "后台执行体不得直接 event.send（event 已终结）"
    chain = ctx.sent[0][1].chain
    assert type(chain[-1]).__name__ == "Video"


# ====================================================================
# M3：R7 —— 终态投递抛错后仍可再次提交（单飞键不泄漏）
# ====================================================================


class _BadSendContext(FakeContext):
    async def send_message(self, session, message_chain):
        raise ValueError("不合法的 session 字符串")


def test_t5b_delivery_error_still_frees_single_flight(logger_recorder):
    ctx = _BadSendContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        first = await plugin.browse_web(FakeEvent(), "第一轮")
        await plugin._background.task_of(task_id_of(first))
        left = len(plugin._background)
        second = await plugin.browse_web(FakeEvent(), "第二轮")
        await plugin._background.task_of(task_id_of(second))
        return left, second

    left, second = run(flow())
    assert left == 0, "投递抛异常后登记表必须已清空（否则该会话永久被拒）"
    assert second.startswith("【已受理】"), second
    assert any("投递失败" in line for line in logger_recorder.lines("error"))


# ====================================================================
# L4（T8-8）：本地页截图在后台模式下进附件；前台行为不变
# ====================================================================


class _WritablePage:
    """能真的落一张截图的假页面（不挂起）。"""

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        return None

    async def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"fake-png")

    def is_closed(self):
        return False


class _ShootingBrowser:
    async def new_page(self):
        return _WritablePage()

    async def close_page(self, page):
        return None

    async def screenshot(self, page, save_path):
        Path(save_path).write_bytes(b"fake-png")
        return save_path


def _prepared_local_page(tmp_path, mode: str):
    html = tmp_path / "page.html"
    html.write_text("<html><body>hi</body></html>", encoding="utf-8")
    event = FakeEvent()
    event.send_calls = 0
    ctx = FakeContext()
    plugin = make_plugin(tools_config(mode), ctx)
    plugin._local_page_allowed_roots = (tmp_path,)
    plugin.browser = _ShootingBrowser()
    plugin.extractor = _FakeExtractor()
    return plugin, ctx, event, html


def test_local_page_screenshot_goes_to_attachments_in_background(tmp_path):
    plugin, ctx, _event, html = _prepared_local_page(tmp_path, "all")

    async def flow():
        out = await plugin.browse_local_page(FakeEvent(), path=str(html))
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    chain = ctx.sent[0][1].chain
    assert type(chain[0]).__name__ == "Plain"
    assert any(type(part).__name__ == "Image" for part in chain[1:]), (
        "后台模式下本地页截图必须进附件随终态消息发"
    )
    assert "1 个媒体文件" in message_text(ctx.sent[0][1])


def test_local_page_screenshot_stays_text_only_in_foreground(tmp_path):
    """前台（off）行为与 v1.4.0 一致：只给截图路径，不发消息、不直发 event。"""
    plugin, ctx, event, html = _prepared_local_page(tmp_path, "off")

    async def _describe(_path, _event, page_url=None):
        return "页面描述"

    plugin._describe_screenshot = _describe
    out = run(plugin.browse_local_page(event, path=str(html)))
    assert "截图:" in out
    assert ctx.sent == [], "前台不得主动投递消息"
    assert event.send_calls == 0, "本地页前台从不 event.send（v1.4.0 行为）"


# ====================================================================
# L2：终态日志带 umo 摘要（三元组 task_id + tool + umo_digest）
# ====================================================================


def test_l2_terminal_log_carries_umo_digest(logger_recorder):
    ctx = FakeContext()
    plugin = make_plugin(tools_config("browse_only"), ctx)

    async def flow():
        out = await plugin.browse_web(FakeEvent(), "任务")
        await plugin._background.task_of(task_id_of(out))

    run(flow())
    terminal = [ln for ln in logger_recorder.lines("info") if "后台任务终态" in ln]
    assert len(terminal) == 1
    assert f"umo={plugin._umo_digest(UMO)}" in terminal[0]
