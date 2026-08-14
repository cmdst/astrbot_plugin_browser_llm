"""
astrbot_plugin_browser_llm — LLM 浏览器插件（Agent 驱动的网页浏览）

架构：工具化子代理（tool-based subagent）。15 个 browse_* 浏览工具不
直接暴露给主 LLM，而是作为「浏览器子代理」的工具集；主 LLM 只看到
一个入口工具 browse_web，需要真实网页交互时调用它，由子代理（
tool_loop_agent + 15 个 FunctionTool）自主完成浏览。

核心组件在 initialize() 中组装：BrowserCore（浏览器驱动）、
SessionManager（会话隔离与回收）、ContentExtractor（内容提取）、
SafetyFilter（禁词过滤与 SSRF 防护），以及 15 个 FunctionTool 构建
的 ToolSet（子代理工具集）。
"""

import asyncio
import hashlib
import re
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote, urljoin

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .core.browser import BrowserCore
from .core.extract import ContentExtractor
from .core.safety import SafetyFilter
from .core.session import SessionManager

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查
    from playwright.async_api import Page

# 插件注册名，与 metadata.yaml 的 name 一致；日志与管理命令判权时使用。
METADATA_NAME = "astrbot_plugin_browser_llm"

# 插件版本，与 metadata.yaml 的 version 保持一致（发布版本变更时两处同步修改；
# 真实运行时 Star 实例无 self.metadata 属性，无法动态读取，故集中为单一常量）。
PLUGIN_VERSION = "v1.2.0"

# terminate 资源清理总超时（秒）：超过则放弃等待并强制收尾，防止插件重载
# 被悬挂的 close 阻塞（曾实测 browser.close 悬挂数小时，旧实例 chromium 进程
# 与 playwright driver 长期残留）。底层 BrowserCore 已带单步超时，此为兜底。
_TERMINATE_TIMEOUT = 20.0

# 数据目录：截图等资源保存位置。
_DATA_DIR = Path(__file__).resolve().parent / "data"

# 浏览器子代理名称（tool_loop_agent 内层 LLM 的 system_prompt 标识）。
_BROWSER_AGENT_NAME = "browser_agent"

# ---------------------------------------------------------------
# browse_local_page（本地页面渲染预览，面向子代理）安全白名单常量
# ---------------------------------------------------------------
# 允许渲染查看的根目录：AstrBot 工作区 + 插件 data 目录。
# 子代理（frontend/engineer 等）仅能预览这两处目录下的 HTML 文件，
# 其余路径一律拒绝（防路径穿越与越权读取）。运行时会用
# get_astrbot_workspaces_path() 解析真实工作区路径，解析失败回退此默认值。
# 默认值由插件安装位置平台无关推导：插件位于 <astrbot_root>/data/plugins/
# <plugin>/，其上级两级即 <astrbot_root>/data，同级即部署环境的 workspaces
# 目录；不硬编码任何真实服务器路径。
_LOCAL_PAGE_WORKSPACE_FALLBACK = (_DATA_DIR.parents[2] / "workspaces").resolve()
# 允许渲染的文件扩展名。
_LOCAL_PAGE_ALLOWED_EXTS = (".html", ".htm")
# 渲染完成后等待 JS 执行的默认时长（毫秒），保证动态内容渲染完成后再截图。
_LOCAL_PAGE_DEFAULT_WAIT_MS = 500

# ---------------------------------------------------------------
# 识图拒识检测：纯文本模型拒识文案识别
# ---------------------------------------------------------------
# 部分纯文本模型（如 deepseek-v4-flash）收到图片请求时返回固定拒识文案
# （如 "[Unsupported Image]"）而非真实视觉描述。命中任一特征即判定识图
# 失败，返回明确提示而非把拒识文本静默透传为「视觉描述」。正则大小写不敏感，
# 覆盖中英文常见拒识表达。
_VISION_REJECTION_HINT = "识图模型不支持图片，请更换多模态模型或清空识图配置"
_VISION_REJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # deepseek 等纯文本模型的固定拒识文案
        r"\[unsupported image\]",
        # 英文：无法查看/处理图片
        r"(?:cannot|can'?t|unable to|not able to)\s+(?:view|see|process)\s+"
        r"(?:the\s+)?(?:image|picture)s?",
        # 英文：不支持/不接受/不理解图片或视觉输入
        r"(?:do|does|did|am|is|are|was|were)\s+not\s+(?:support|accept|"
        r"understand|process)\s+(?:the\s+)?(?:image|picture|vision|multimodal)s?",
        # 英文：无视觉/多模态能力
        r"no\s+(?:vision|multimodal|image)\s+(?:support|capabilit)",
        # 英文：纯文本模型自述
        r"text[- ]?only\s+(?:model|llm|ai)",
        # 中文：纯文本模型自述
        r"纯文本(?:模型|llm|ai)?",
        # 中文：图片无法/不能/不支持（处理/识别/查看/理解）
        r"图片?(?:无法|不能|不支持)(?:处理|识别|查看|理解)?",
        r"无法(?:处理|识别|查看|理解)(?:图片|图像|截图)",
        r"不支持(?:图片|图像|多模态|视觉)",
        r"不是(?:多模态|视觉)(?:模型)?",
        r"没有(?:视觉|识图|图像处理)(?:能力|功能)?",
    )
)


def _is_vision_rejection(text: str) -> bool:
    """判断模型返回文本是否为「拒识」而非真实视觉描述。

    命中任一拒识特征（大小写不敏感、中英文）返回 True；空文本返回 False。
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _VISION_REJECTION_PATTERNS)


def _resolve_local_page_roots() -> tuple[Path, ...]:
    """解析 browse_local_page 允许访问的根目录白名单（去重保序）。

    默认以任务约定的工作区绝对路径为准（由插件安装位置推导的 AstrBot
    工作区目录，标准部署下与服务实际工作区一致）；仅当该路径不存在
    （如 AstrBot 重定位到其他根目录）时，才用运行时解析值兜底——避免
    进程 CWD 不确定导致白名单根漂移（get_astrbot_root() 默认取 CWD）。
    插件 data 目录始终在列（路径由插件自身位置推导，天然稳定）。
    """
    roots: list[Path] = [_LOCAL_PAGE_WORKSPACE_FALLBACK]
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_workspaces_path,
        )

        live = Path(get_astrbot_workspaces_path()).resolve()
    except Exception:  # noqa: BLE001 — 解析失败忽略，仅用默认值
        live = None
    if (
        live is not None
        and live != _LOCAL_PAGE_WORKSPACE_FALLBACK
        and not _LOCAL_PAGE_WORKSPACE_FALLBACK.is_dir()
        and live.is_dir()
    ):
        roots.append(live)
    roots.append(_DATA_DIR.resolve())
    # dict.fromkeys 去重且保序（工作区与插件 data 目录重叠时只保留一份）。
    return tuple(dict.fromkeys(roots))


def _params(
    required: dict[str, tuple[str, str]],
    optional: dict[str, tuple[str, str]] | None = None,
) -> dict:
    """构造 JSON Schema parameters。

    required/optional 为 name -> (description, type) 映射，
    如 {"query": ("搜索关键词", "string")}。
    """
    properties = {}
    required_list = []
    for name, (desc, ptype) in required.items():
        properties[name] = {"type": ptype, "description": desc}
        required_list.append(name)
    for name, (desc, ptype) in (optional or {}).items():
        properties[name] = {"type": ptype, "description": desc}
    return {
        "type": "object",
        "properties": properties,
        "required": required_list,
    }


# 15 个浏览工具配置：name / description（展示给子 Agent LLM）/
# parameters（JSON Schema）/ method（插件实例方法名）。
_BROWSER_TOOLS = [
    {
        "name": "browse_search",
        "description": "打开搜索引擎结果页以便后续在页面上继续交互（如点击进入具体网页）。仅当需要真实浏览器交互时才使用；纯搜索问答请用其他搜索工具。",
        "parameters": _params(
            {"query": ("要搜索的关键词，如北京天气", "string")},
            {"engine": ("搜索引擎名称，可选：必应搜索/百度搜索/谷歌搜索/B站搜索，留空用默认", "string")},
        ),
        "method": "browse_search",
    },
    {
        "name": "browse_open",
        "description": "网页交互场景专用：打开指定网页并返回该页面的文本内容。",
        "parameters": _params(
            {"url": ("目标网页完整地址，须以 http:// 或 https:// 开头", "string")},
        ),
        "method": "browse_open",
    },
    {
        "name": "browse_current_page",
        "description": "获取当前浏览会话所在页面的 URL、标题与正文摘要。",
        "parameters": _params({}),
        "method": "browse_current_page",
    },
    {
        "name": "browse_get_text",
        "description": "获取当前页面的正文纯文本（可自定义最大字符数）。",
        "parameters": _params(
            {}, {"max_chars": ("返回文本的最大字符数，<=0 使用插件默认值", "number")}
        ),
        "method": "browse_get_text",
    },
    {
        "name": "browse_get_links",
        "description": "获取当前页面上的链接编号列表，供后续 browse_click_link 点击。",
        "parameters": _params(
            {}, {"max_items": ("最多返回的链接条数，<=0 使用插件默认值", "number")}
        ),
        "method": "browse_get_links",
    },
    {
        "name": "browse_click_link",
        "description": "网页交互场景专用：点击当前页面上编号为 index 的链接并跳转（编号从 1 开始）。",
        "parameters": _params(
            {"index": ("链接编号，从 1 开始，对应 browse_get_links 返回的列表编号", "number")},
        ),
        "method": "browse_click_link",
    },
    {
        "name": "browse_scroll",
        "description": "在当前页面按指定方向滚动，返回滚动后的页面内容。",
        "parameters": _params(
            {},
            {"direction": ("滚动方向，down/下 向下滚动，up/上 向上滚动", "string"),
             "pixels": ("滚动像素数，默认 800", "number")},
        ),
        "method": "browse_scroll",
    },
    {
        "name": "browse_go_back",
        "description": "返回上一页，返回跳转后的页面内容。",
        "parameters": _params({}),
        "method": "browse_go_back",
    },
    {
        "name": "browse_go_forward",
        "description": "前进到下一页，返回跳转后的页面内容。",
        "parameters": _params({}),
        "method": "browse_go_forward",
    },
    {
        "name": "browse_new_tab",
        "description": "新开一个标签页打开指定网址，返回新标签页的内容摘要。",
        "parameters": _params(
            {"url": ("新标签页要打开的网址，须以 http:// 或 https:// 开头", "string")},
        ),
        "method": "browse_new_tab",
    },
    {
        "name": "browse_switch_tab",
        "description": "切换到指定编号的标签页（编号从 1 开始），返回切换后的内容摘要。",
        "parameters": _params(
            {"index": ("目标标签页编号，从 1 开始", "number")},
        ),
        "method": "browse_switch_tab",
    },
    {
        "name": "browse_close_tab",
        "description": "关闭指定编号的标签页（编号从 1 开始），返回剩余标签数。",
        "parameters": _params(
            {"index": ("要关闭的标签页编号，从 1 开始", "number")},
        ),
        "method": "browse_close_tab",
    },
    {
        "name": "browse_input",
        "description": "网页交互场景专用：在页面上定位 CSS 选择器指向的输入框并输入文本。",
        "parameters": _params(
            {"selector": ("CSS 选择器，如 #search-input 或 input[name=q]", "string"),
             "text": ("要输入的文本内容", "string")},
        ),
        "method": "browse_input",
    },
    {
        "name": "browse_press_key",
        "description": "在当前页面按下键盘按键（如 Enter 提交搜索、Escape 关闭弹窗）。",
        "parameters": _params(
            {"key": ("按键名，支持 Enter/Escape/ArrowDown/ArrowUp/ArrowLeft/ArrowRight/Tab/Backspace/Home/End", "string")},
        ),
        "method": "browse_press_key",
    },
    {
        "name": "browse_screenshot",
        "description": "截取当前页面截图发送给用户，若配置识图模型则附上识图描述。",
        "parameters": _params({}),
        "method": "browse_screenshot",
    },
    {
        "name": "browse_click_text",
        "description": "网页交互场景专用：按文本内容精准点击页面元素（如按钮、选项），用于非链接的 JS 元素。",
        "parameters": _params(
            {"text": ("要点击的元素包含的文本，如确认并继续", "string")},
        ),
        "method": "browse_click_text",
    },
    {
        "name": "browse_set_slider",
        "description": "网页交互场景专用：设置当前页面的滑块（input[type=range]）值为指定数值。",
        "parameters": _params(
            {"value": ("滑块目标值", "number")},
        ),
        "method": "browse_set_slider",
    },
    {
        "name": "browse_select_option",
        "description": "网页交互场景专用：选择下拉框（select）的选项。",
        "parameters": _params(
            {"selector": ("下拉框的 CSS 选择器，如 #age", "string"),
             "value": ("选项值（label/value/index 均可）", "string")},
        ),
        "method": "browse_select_option",
    },
    {
        "name": "browse_click_coords",
        "description": "网页交互场景专用：按视口坐标点击页面（识图后定位无文本元素时使用）。",
        "parameters": _params(
            {"x": ("视口横坐标（像素）", "number"),
             "y": ("视口纵坐标（像素）", "number")},
        ),
        "method": "browse_click_coords",
    },
    {
        "name": "browse_hover",
        "description": "网页交互场景专用：悬停包含指定文本的元素（用于触发悬浮菜单）。",
        "parameters": _params(
            {"text": ("要悬停的元素包含的文本", "string")},
        ),
        "method": "browse_hover",
    },
    {
        "name": "browse_reload",
        "description": "刷新当前页面并返回刷新后的内容摘要。",
        "parameters": _params({}),
        "method": "browse_reload",
    },
    {
        "name": "browse_checkbox",
        "description": "网页交互场景专用：设置复选框/单选框的选中状态。",
        "parameters": _params(
            {"selector": ("复选框的 CSS 选择器", "string"),
             "checked": ("目标选中状态（true=勾选，false=取消）", "boolean")},
        ),
        "method": "browse_checkbox",
    },
    {
        "name": "browse_zoom_crop",
        "description": "网页交互场景专用：裁剪页面指定区域并放大识图（豆包式局部放大，用于查看无文本/小字区域）。",
        "parameters": _params(
            {"x": ("裁剪区域左上角横坐标（视口像素）", "number"),
             "y": ("裁剪区域左上角纵坐标（视口像素）", "number"),
             "width": ("裁剪区域宽度（像素）", "number"),
             "height": ("裁剪区域高度（像素）", "number")},
        ),
        "method": "browse_zoom_crop",
    },
    {
        "name": "browse_sniff_media",
        "description": "从当前页面嗅探图片/视频资源并下载发送到群聊（用户主动请求媒体时使用）。",
        "parameters": _params(
            {},
            {"media_type": ("媒体类型：image/video/all，默认 all", "string"),
             "max_items": ("最多下载数量，默认 5", "number")},
        ),
        "method": "browse_sniff_media",
    },
]


def _make_browser_tool(plugin: "BrowserLLMPlugin", spec: dict):
    """工厂：为一条 _BROWSER_TOOLS 配置生成 FunctionTool 实例。

    使用 handler 字段（官方一等公民）：执行器以 handler(event, **kwargs)
    调用，等效于 task 描述的子类覆写 call 方案，且避免 pydantic dataclass
    子类化的兼容风险。handler 绑定插件实例方法，签名与内部 browse_*
    方法一致（self, event, **kwargs）。

    v1.2.0：handler 外层包一层配置热更新（_refresh_config），使 Dashboard
    修改的配置（黑名单、截图开关、内网拦截等）在子代理工具调用时即生效，
    无需重启；开销为若干次 dict 读取，可忽略。
    """
    from astrbot.core.agent.tool import FunctionTool  # noqa: PLC0415 — 延迟导入

    method = getattr(plugin, spec["method"])

    async def _handler(event, **kwargs):
        # 工具入口轻量热更新配置（重读共享 config dict）。
        plugin._refresh_config()
        return await method(event, **kwargs)

    return FunctionTool(
        name=spec["name"],
        description=spec["description"],
        parameters=spec["parameters"],
        handler=_handler,
    )


# 浏览器子代理的操作指引（作为 tool_loop_agent 的 system_prompt，即子代理
# 内层 LLM 的指令）：工具清单 + 索引点击原则 + 消化结果 + 失败调整。
_BROWSER_AGENT_INSTRUCTION = (
    "你是网页浏览器子代理（browser_agent），负责在真实浏览器中完成网页"
    "交互任务。可用工具：\n"
    "- browse_search：打开搜索引擎结果页；\n"
    "- browse_open：打开指定网址；\n"
    "- browse_current_page / browse_get_text / browse_get_links：查看当前"
    "页面、提取正文与链接列表；\n"
    "- browse_click_link：按编号点击链接继续浏览；\n"
    "- browse_click_text：按文本内容精准点击元素（按钮/选项）；\n"
    "- browse_set_slider / browse_select_option / browse_checkbox：滑块/"
    "下拉框/复选框设置；\n"
    "- browse_click_coords：按视口坐标点击（识图后定位无文本元素）；\n"
    "- browse_zoom_crop：裁剪页面区域放大识图（查看小字/无文本区域）；\n"
    "- browse_sniff_media：嗅探页面图片/视频资源并下载发送到群聊；\n"
    "- browse_hover：悬停元素触发悬浮菜单；\n"
    "- browse_new_tab / browse_switch_tab / browse_close_tab：标签页管理；\n"
    "- browse_input / browse_press_key：在页面输入文本 / 按键提交；\n"
    "- browse_scroll：滚动页面查看更多内容；\n"
    "- browse_reload：刷新页面；\n"
    "- browse_zoom_crop：裁剪页面指定区域放大识图（读小字/图标/图表细节时用）；\n"
    "- browse_go_back / browse_go_forward：后退 / 前进；\n"
    "- browse_screenshot：截取页面截图发送给用户。\n\n"
    "操作原则：\n"
    "⚠️ 图形验证码红线（最高优先级）：遇到图形验证码（点选图标、滑块"
    "拼图、字符识别等反机器人验证）时，立即停止当前操作，绝对不要尝试"
    "点击/猜测/盲试，直接返回说明页面遇到图形验证码，需要用户手动"
    "完成。\n"
    "1. 需要继续阅读时，优先用 browse_get_links 获取链接编号，再用 "
    "browse_click_link 按编号点击，不要凭空猜测 URL；\n"
    "2. 优先用 browse_get_text / browse_current_page 读取页面内容，"
    "不要每次都用 browse_screenshot（识图较慢，仅在需要查看布局或无文本"
    "元素时用）；\n"
    "3. 能用 browse_click_text / browse_set_slider 等精准工具，就少用 "
    "browse_press_key 盲操作；\n"
    "4. 连续操作时减少不必要的页面重读，消化工具返回的结果后再决定"
    "下一步，不要重复相同的操作；\n"
    "5. 页面交互（点击/滚动/翻页/填表/按键/标签页切换）通过对应工具完成；\n"
    "6. 需要向用户展示页面外观时用 browse_screenshot；需要读取页面小字、"
    "图标、图表数据等细节时，先 browse_screenshot 全图定位，再用 "
    "browse_zoom_crop 裁剪放大关键区域识别；\n"
    "7. 图形验证码（人机验证/点选图标验证/滑块拼图验证）无法自动可靠"
    "通过：遇到时立即停止操作，不要尝试点击、刷新、重试或盲猜坐标；"
    "如实报告『遇到图形验证码，需要用户手动处理』并返回当前页面状态；\n"
    "8. 浏览失败时根据错误信息调整策略，必要时更换搜索词或链接；\n"
    "9. 完成后用简洁中文总结浏览结果（URL、关键内容），不要暴露工具调用"
    "过程细节。\n\n"
    "提交后等待结果（重要）：\n"
    "- 点击生成/提交按钮（如最后一题的『生成我的模型』）后，绝对不要"
    "刷新页面、不要重开测试、不要后退，否则结果页会丢失；\n"
    "- 用 browse_get_text / browse_current_page 反复读取当前页面，等待"
    "结果出现。出现『模型匹配』『八维』『Human Model ID』『指纹』等"
    "关键词即成功；每次读取间隔 5-8 秒，最多等待 60 秒；\n"
    "- 若页面仍在生成中（进度指示/空白），继续等待重读，不要中途放弃；\n"
    "- 拿到结果后，原样返回结果页文字（包含 Human Model ID 等关键信息）。"
)


class BrowserLLMPlugin(Star):
    """LLM 浏览器插件主入口。

    AstrBot 会以 (context, config) 实例化本类；config 为 _conf_schema.json
    定义的配置项（含默认值）。__init__ 只读配置，核心组件在
    initialize() 中创建。
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        """初始化插件：读取全部配置项到 self.* 属性。

        Args:
            context: AstrBot 插件上下文，提供事件注册与消息发送能力。
            config: 插件配置对象，对应 _conf_schema.json 中定义的
                22 个配置项（含默认值）；未传入时使用空字典兜底。
        """
        super().__init__(context)
        self.config = config or {}
        self.metadata_name = METADATA_NAME
        self._load_config()

        # 核心组件在 initialize() 中创建；此处预置 None 供 terminate 容错。
        self.browser: BrowserCore | None = None
        self.sessions: SessionManager | None = None
        self.extractor: ContentExtractor | None = None
        self.safety: SafetyFilter | None = None

        # browse_local_page 专用的 per-umo 锁（独立于 SessionManager，
        # 本地页面预览不走浏览会话，避免与既有 browse_* 会话互相污染）。
        # v1.2.0：改用弱引用字典——渲染结束、锁对象无任何强引用（无持锁/
        # 无等待协程）时条目自动移除，防止长期运行后字典无限增长；弱引用
        # 语义天然避免「手动 pop 与并发取锁」之间的清理竞态。
        self._local_page_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # 本地页面预览允许访问的根目录白名单（initialize 时解析真实路径）。
        self._local_page_allowed_roots: tuple[Path, ...] = _resolve_local_page_roots()

        # 搜索引擎模板：{keyword} 会被 URL 编码后替换。
        self._search_engines = {
            "必应搜索": "https://cn.bing.com/search?q={keyword}&FORM=BESBTB&ensearch=1",
            "百度搜索": "https://www.baidu.com/s?wd={keyword}",
            "谷歌搜索": "https://www.google.com.hk/search?&q={keyword}",
            "B站搜索": "https://search.bilibili.com/all?keyword={keyword}",
        }

    def _load_config(self) -> None:
        """将 _conf_schema.json 中的配置项读取为实例属性。

        全部 22 个配置项：浏览器引擎 / 默认页 / 搜索引擎 / 禁词 /
        内网拦截 / 提取与链接上限 / 超时 / 页数上限 / 空闲回收 /
        会话黑白名单 / 截图与识图 / 代理 / 视口。
        """
        cfg = self.config

        # 浏览器基础
        self.browser_type: str = cfg.get("browser_type", "chromium")
        self.default_url: str = cfg.get("default_url", "https://www.baidu.com")
        self.default_search_engine: str = cfg.get("default_search_engine", "必应搜索")

        # 安全与过滤
        self.banned_words: list = cfg.get(
            "banned_words",
            ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"],
        )
        self.block_internal_ip: bool = cfg.get("block_internal_ip", True)

        # 提取与资源控制
        self.max_chars: float = cfg.get("max_chars", 4000)
        self.max_links: float = cfg.get("max_links", 20)
        self.timeout: float = cfg.get("timeout", 30)
        self.max_pages: float = cfg.get("max_pages", 5)
        self.idle_timeout: float = cfg.get("idle_timeout", 1800)

        # 会话权限控制
        self.session_whitelist: list = cfg.get("session_whitelist", [])
        self.session_blacklist: list = cfg.get("session_blacklist", [])

        # 截图与识图
        self.enable_screenshot: bool = cfg.get("enable_screenshot", True)
        self.vision_provider_id: str = cfg.get("vision_provider_id", "")
        self.vision_prompt: str = str(
            cfg.get(
                "vision_prompt",
                "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。",
            )
        )
        # 静默模式：开启后截图仅内部识图，不发到群聊。
        self.silent_mode: bool = bool(cfg.get("silent_mode", True))
        # 页面感知方式：text / text_image / image。
        self.page_perception: str = str(cfg.get("page_perception", "text_image"))
        # 媒体缓存保留天数：超过自动清理，节省磁盘。
        self.cache_days: int = int(cfg.get("cache_days", 3))

        # 网络与视口
        self.proxy: str = cfg.get("proxy", "")
        self.viewport: dict = cfg.get("viewport", {"width": 1280, "height": 800})

        # 浏览器子 Agent 控制（agent-as-tool）
        self.agent_max_steps: int = int(cfg.get("agent_max_steps", 70))
        self.agent_tool_timeout: int = int(cfg.get("agent_tool_timeout", 1200))

    def _refresh_config(self) -> None:
        """轻量热更新配置：工具入口重读共享 config dict 并同步固化组件。

        Dashboard 保存配置时 AstrBot 会就地 update 传入插件的同一 config
        dict（面板保存即更新，并触发插件热重载兜底）；_load_config 把最新
        值刷入实例属性，本方法再同步进 SessionManager / SafetyFilter /
        BrowserCore 的运行期参数（max_pages、idle_timeout、default_url、
        禁词、内网拦截），使黑名单、内网拦截、截图开关等配置修改无需
        重启即生效。仅浏览工具入口调用，开销为若干次 dict 读取。

        说明：既有 context 的 SSRF 兜底路由安装与否按创建时的开关决定，
        此处同步 BrowserCore.block_internal_ip 仅影响之后新建的 context；
        关闭开关不会摘除已装拦截（保持安全方向）。
        """
        self._load_config()
        # max_pages / idle_timeout / default_url 固化在 SessionManager，
        # 同步热更新：新额度/新阈值对新标签与下一轮回收立即生效。
        if self.sessions is not None:
            self.sessions.max_pages = int(self.max_pages)
            self.sessions.idle_timeout = float(self.idle_timeout)
            self.sessions.default_url = self.default_url
        if self.safety is not None:
            self.safety.update_config(
                banned_words=self.banned_words,
                block_internal_ip=bool(self.block_internal_ip),
            )
        if self.browser is not None:
            self.browser.block_internal_ip = bool(self.block_internal_ip)
        # 页面感知方式变化后重建子代理指令（下次 browse_web 生效）。
        if getattr(self, "_browser_instruction", None) is not None:
            self._browser_instruction = self._build_subagent_instruction()

    # ================================================================
    # 生命周期
    # ================================================================

    async def initialize(self) -> None:
        """插件激活时组装核心组件并启动空闲回收任务。"""
        # 组装核心组件：配置注入 data_dir（截图保存目录）。
        browser_config = dict(self.config)
        browser_config.setdefault("data_dir", str(_DATA_DIR))
        self.browser = BrowserCore(browser_config)
        self.sessions = SessionManager(
            self.browser,
            max_pages=int(self.max_pages),
            idle_timeout=float(self.idle_timeout),
            default_url=self.default_url,
        )
        self.extractor = ContentExtractor()
        self.safety = SafetyFilter(self.banned_words, self.block_internal_ip)
        # 本地页面预览白名单：运行时解析真实工作区路径（防硬编码漂移）。
        self._local_page_allowed_roots = _resolve_local_page_roots()
        # 截图保存目录：data/screenshots/。
        self._screenshot_dir = Path(__file__).resolve().parent / "data" / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        # 媒体下载目录：data/media/。
        self._media_dir = Path(__file__).resolve().parent / "data" / "media"
        self._media_dir.mkdir(parents=True, exist_ok=True)
        # 首次清理过期缓存 + 启动定期清理任务（每 6 小时，独立于会话 sweeper）。
        self._cache_cleanup_task: Optional[asyncio.Task] = None
        try:
            self._cleanup_cache()
        except Exception as e:  # noqa: BLE001 — 清理失败不影响启动
            logger.warning(f"[{self.metadata_name}] 首次缓存清理失败: {e}")
        self._start_cache_cleanup_task()
        # 后台空闲回收任务（每 60s 关闭超时未活动的会话）。
        await self.sessions.start_sweeper()
        # 构建 15 个浏览工具（FunctionTool）与子 Agent 工具集。
        self._browser_tools = [_make_browser_tool(self, s) for s in _BROWSER_TOOLS]
        self._browser_toolset = self._build_toolset(self._browser_tools)
        # 按页面感知方式动态生成子代理指令（基础模板 + 感知方式段）。
        self._browser_instruction = self._build_subagent_instruction()
        # 识图 provider 下拉同步：把 AstrBot 已加载 provider 写进内存 schema
        # 的 options（Dashboard 面板每次请求实时读取该 schema，无需重启）。
        # 注意 AstrBot 启动顺序是「插件加载先于 provider 初始化」，此处可能
        # 暂时拿不到 provider 列表，由后台轮询任务兜底补齐。
        self._vision_sync_task: Optional[asyncio.Task] = None
        self._sync_vision_provider_options()
        self._start_vision_provider_sync_task()
        logger.info(f"[{self.metadata_name}] 浏览器子代理工具集: "
                    f"{len(self._browser_tools)} 个浏览工具")
        logger.info(f"[{self.metadata_name}] 页面感知方式: {self.page_perception}")
        logger.info(f"[{self.metadata_name}] 插件 {PLUGIN_VERSION} 已激活")
        logger.info(f"[{self.metadata_name}] 浏览器引擎: {self.browser_type}")
        logger.info(f"[{self.metadata_name}] 默认搜索引擎: {self.default_search_engine}")

    @staticmethod
    def _build_toolset(tools: list) -> object | None:
        """把 FunctionTool 列表包装成 ToolSet（子代理工具集）。"""
        from astrbot.core.agent.tool import ToolSet  # noqa: PLC0415 — 延迟导入

        return ToolSet(tools=tools)

    def _build_subagent_instruction(self) -> str:
        """动态生成浏览器子代理指令（基础模板 + 页面感知方式段）。

        基础模板为模块级常量 _BROWSER_AGENT_INSTRUCTION（工具清单 +
        操作原则 + 验证码红线 + 等待结果策略）；按 page_perception
        配置追加『页面感知方式』说明，指导子代理用文字还是截图感知页面。
        """
        perception_map = {
            "text": (
                "页面感知方式：仅文字。只用 browse_get_text / "
                "browse_current_page 读取页面文字，绝对不要使用 "
                "browse_screenshot（节省时间）。通过文字判断页面状态与选项。"
            ),
            "text_image": (
                "页面感知方式：文字为主，截图辅助。优先用 browse_get_text "
                "读取文字；仅当需要看布局、无文本元素、或 get_text 无法"
                "确认状态时才用 browse_screenshot。"
            ),
            "image": (
                "页面感知方式：截图为主。主要用 browse_screenshot 截图识图"
                "理解页面，辅以 browse_get_text。"
            ),
        }
        mode = (self.page_perception or "").strip()
        perception_note = perception_map.get(mode, perception_map["text_image"])
        return f"{_BROWSER_AGENT_INSTRUCTION}\n\n{perception_note}"

    def _cleanup_cache(self) -> int:
        """清理超过 cache_days 天的媒体/截图缓存文件，返回删除数。

        遍历 data/media/ 与 data/screenshots/ 下的文件，删除 mtime
        早于 (now - cache_days) 的旧文件；目录本身保留。
        """
        cutoff = time.time() - self.cache_days * 86400
        removed = 0
        for base in (getattr(self, "_media_dir", None), getattr(self, "_screenshot_dir", None)):
            if base is None or not base.is_dir():
                continue
            for f in base.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except Exception as e:  # noqa: BLE001 — 单文件失败不影响其余
                    logger.debug(f"清理缓存文件失败 {f}: {e}")
        if removed:
            logger.info(f"[{self.metadata_name}] 缓存清理完成，删除 {removed} 个过期文件")
        return removed

    def _start_cache_cleanup_task(self) -> None:
        """启动定期缓存清理任务（每 6 小时一次，独立于会话 sweeper）。"""
        if self._cache_cleanup_task is not None and not self._cache_cleanup_task.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(6 * 3600)
                    self._cleanup_cache()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — 单轮失败不退出
                    logger.warning(f"[{self.metadata_name}] 定期缓存清理异常: {e}")

        self._cache_cleanup_task = asyncio.create_task(
            _loop(), name="browser-cache-cleanup"
        )
        logger.info(f"[{self.metadata_name}] 缓存定期清理任务已启动（每 6 小时）")

    # ------------------------------------------------------------
    # 识图 provider 下拉动态同步（Bug #1 修复）
    # ------------------------------------------------------------

    def _collect_provider_ids(self) -> list[str]:
        """从 ProviderManager 提取已加载聊天 Provider 的 ID 列表（保序去重）。

        优先 provider_insts（仅聊天 Provider，识图用 llm_generate 需要
        聊天 Provider；inst_map 还混有 STT/TTS/Embedding/Rerank 实例）；
        provider_insts 不可用时退回 inst_map 键（仅用于面板展示兜底）。
        """
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return []
        ids: list[str] = []
        for inst in getattr(pm, "provider_insts", None) or []:
            pid = ""
            try:
                pid = inst.meta().id
            except Exception:  # noqa: BLE001 — meta() 异常时尝试属性兜底
                pid = getattr(inst, "provider_id", "") or ""
            if pid:
                ids.append(str(pid))
        if not ids:
            inst_map = getattr(pm, "inst_map", None)
            if isinstance(inst_map, dict):
                ids = [str(pid) for pid in inst_map if pid]
        return list(dict.fromkeys(ids))

    def _sync_vision_provider_options(self) -> list[str]:
        """把已加载 provider 同步到配置面板的识图模型下拉（内存 schema）。

        AstrBot Dashboard 的 ConfigDisplayService.get_plugin_config() 每次
        请求都直接返回 plugin_md.config.schema（与 self.config.schema 是
        同一对象），因此就地更新 options 即可让面板实时生效，无需重启。
        始终保留 "" 空选项（留空 = 不做识图）；default 若已不在候选中则
        修正为空串，避免 Dashboard「恢复默认」写回一个不存在的 provider。

        Returns:
            list[str]: 当前生效的 options 列表（含 "" 空选项）。
        """
        ids = self._collect_provider_ids()
        options = list(dict.fromkeys([""] + ids))
        schema = getattr(self.config, "schema", None)
        if isinstance(schema, dict):
            item = schema.get("vision_provider_id")
            if isinstance(item, dict) and isinstance(item.get("options"), list):
                if item["options"] != options:
                    item["options"] = options
                    if item.get("default") not in options:
                        item["default"] = ""
        return options

    def _start_vision_provider_sync_task(self) -> None:
        """启动识图 provider 下拉同步的启动期兜底任务。

        AstrBot 启动顺序为「插件 reload 先于 provider 初始化」，因此
        initialize() 内的首次同步可能拿不到 provider 列表；本任务每 5s
        轮询一次，直到 provider 就绪后同步一次即退出（最长等待 2 分钟），
        保证启动后面板下拉自动填充，无需任何浏览活动触发。
        """
        if self._vision_sync_task is not None and not self._vision_sync_task.done():
            return

        async def _loop() -> None:
            deadline = time.monotonic() + 120
            while True:
                if self._collect_provider_ids():
                    options = self._sync_vision_provider_options()
                    logger.info(
                        f"[{self.metadata_name}] 识图 provider 下拉已同步 "
                        f"({len(options) - 1} 个 Provider)"
                    )
                    return
                if time.monotonic() > deadline:
                    return
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise

        self._vision_sync_task = asyncio.create_task(
            _loop(), name="browser-vision-provider-sync"
        )

    def _stop_vision_provider_sync_task(self) -> None:
        """停止识图 provider 下拉同步任务（幂等）。"""
        task = self._vision_sync_task
        if task is None:
            return
        self._vision_sync_task = None
        if not task.done():
            task.cancel()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._await_task(task))  # 不阻塞 terminate
            except Exception:  # noqa: BLE001
                pass

    def _provider_loaded(self, provider_id: str) -> bool:
        """provider_id 是否在 ProviderManager 已加载实例中。"""
        if not provider_id:
            return False
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            # 无 ProviderManager（如单测桩）：不做存在性校验，信任配置值。
            return True
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict):
            return provider_id in inst_map
        for inst in getattr(pm, "provider_insts", None) or []:
            try:
                if str(inst.meta().id) == provider_id:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _live_vision_provider_id(self) -> str:
        """实时读取识图 provider 配置值。

        优先读共享 config dict（AstrBotConfig 与 Dashboard 保存为同一实例，
        面板改配置立即生效，不必等插件重载）；仅在 config 无此键时回退
        __init__ 快照属性（测试/手动装配场景）。
        """
        cfg = self.config
        if isinstance(cfg, dict) and "vision_provider_id" in cfg:
            return str(cfg.get("vision_provider_id") or "").strip()
        return str(getattr(self, "vision_provider_id", "") or "").strip()

    def _live_vision_prompt(self) -> str:
        """实时读取识图提示词（同 _live_vision_provider_id 的时效语义）。"""
        cfg = self.config
        if isinstance(cfg, dict) and "vision_prompt" in cfg:
            return str(cfg.get("vision_prompt") or "").strip()
        return str(getattr(self, "vision_prompt", "") or "").strip()

    async def _resolve_vision_provider_id(
        self, event: AstrMessageEvent | None = None
    ) -> str:
        """解析实际使用的识图 provider：配置实时生效 + 失效自动降级。

        逻辑：
        1. 实时读取 vision_provider_id（Dashboard 保存立即生效）；
        2. 顺带同步下拉选项（面板与运行一致性，廉价操作）；
        3. 配置值已加载 → 直接使用；
        4. 配置值为空（留空 = 显式关闭识图）→ 不做回退，返回空串；
        5. 配置值失效（被删除/改名）→ 警告日志 + 回退当前会话聊天 Provider；
        6. 全部不可用 → 返回空串（调用方不做识图）。

        Args:
            event: 消息事件（回退 provider 时用于确定会话），可为 None。

        Returns:
            str: 实际 provider_id；空串表示不做识图。
        """
        provider_id = self._live_vision_provider_id()
        # 顺带同步下拉（面板每次识图相关调用后保持最新）。
        self._sync_vision_provider_options()
        if provider_id and self._provider_loaded(provider_id):
            return provider_id
        if not provider_id:
            # 留空 = 用户显式关闭识图，不触发回退。
            return ""
        # 配置值失效（被删除/改名）：警告 + 回退当前会话 provider。
        logger.warning(
            f"[{self.metadata_name}] vision_provider_id={provider_id!r} "
            "未在已加载 Provider 中，识图自动回退到当前会话聊天 Provider。"
            "请确认该 Provider 已在 AstrBot 模型配置中启用，或重新选择识图模型。"
        )
        if event is not None:
            try:
                fallback = await self.context.get_current_chat_provider_id(
                    umo=self._umo_of(event)
                )
            except Exception as e:  # noqa: BLE001 — 回退失败不抛异常
                logger.warning(
                    f"[{self.metadata_name}] 识图回退 provider 获取失败: {e}"
                )
                return ""
            if fallback and self._provider_loaded(fallback):
                return str(fallback)
        return ""

    def _stop_cache_cleanup_task(self) -> None:
        """停止定期缓存清理任务（幂等）。"""
        task = self._cache_cleanup_task
        if task is None:
            return
        self._cache_cleanup_task = None
        if not task.done():
            task.cancel()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._await_task(task))  # 不阻塞 terminate
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _await_task(task: asyncio.Task) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _shutdown_browser_resources(self) -> None:
        """按序释放浏览器资源（任一步失败/超时不中断后续清理）。

        顺序说明：先停后台会话回收任务（快路径），再关浏览器
        （browser.close 会顺带关闭全部页面/context，且 BrowserCore 内部
        自带单步超时），最后清理会话内存映射（此时页面已随浏览器关闭，
        仅剩内存态收尾，不会悬挂）。避免旧顺序下单个 page.close 悬挂
        阻塞到 browser.shutdown 之前、导致 playwright driver 永不停止。
        """
        tag = f"[{self.metadata_name}]"
        if self.sessions is not None:
            try:
                await self.sessions.stop_sweeper()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{tag} 停止会话回收任务失败: {e}")
        if self.browser is not None:
            try:
                await self.browser.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{tag} 关闭浏览器失败: {e}")
        if self.sessions is not None:
            try:
                await self.sessions.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{tag} 关闭会话管理器失败: {e}")

    async def terminate(self) -> None:
        """AstrBot 禁用/重载时释放全部浏览器资源（带总超时保护）。

        总超时兜底：即使底层某步意外悬挂，terminate 也会在
        _TERMINATE_TIMEOUT 秒后放弃等待并完成收尾，不阻塞插件重载，
        避免旧实例 chromium 进程残留。超时以 error 级日志上报。
        """
        tag = f"[{self.metadata_name}]"
        self._stop_cache_cleanup_task()
        self._stop_vision_provider_sync_task()
        try:
            await asyncio.wait_for(
                self._shutdown_browser_resources(), timeout=_TERMINATE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(
                f"{tag} 资源清理总超时（%.1fs），部分资源可能未释放",
                _TERMINATE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"{tag} 资源清理异常: {e}")
        finally:
            # 本地页面预览锁表清空（弱引用字典常规会自动清理，此处显式兜底）。
            self._local_page_locks.clear()
        logger.info(f"{tag} 已终止")

    # ================================================================
    # 浏览器子代理入口（工具化子代理：browse_web）
    # ================================================================

    @filter.llm_tool(name="browse_web")
    async def browse_web(self, event: AstrMessageEvent, input: str = "") -> str:
        """委托浏览器子代理完成网页浏览任务（打开网页、页面交互、提取内容或截图）。

        Args:
            input(string): 给浏览器子代理的任务描述，说明要打开什么网址、查找什么内容、是否需要点击/滚动/填表等交互。

        注意：本入口不持 per-umo 锁（asyncio.Lock 不可重入——若在此持锁，
        子 Agent 工具 browse_* 内部再 acquire 同一锁会死锁）。串行化由
        子 Agent 工具内部持有锁完成（同一会话的浏览器操作被串行化）。
        """
        try:
            logger.debug(f"[{self.metadata_name}] browse_web 进入，input={input!r}")
            # 轻量热更新配置：Dashboard 保存的配置（黑名单/截图/内网拦截等）
            # 在下次入口调用即生效，无需重启。
            self._refresh_config()
            # 顺带同步识图 provider 下拉（面板每次请求实时读取内存 schema，
            # 此入口被主 LLM 高频调用，可保持面板选项跟随运行时 provider 变化）。
            self._sync_vision_provider_options()
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=self._umo_of(event)
                )
            except Exception as e:  # noqa: BLE001 — ProviderNotFoundError 等
                logger.warning(
                    f"[{self.metadata_name}] 获取当前对话 Provider 失败: {e}"
                )
                return "【错误】无法确定当前对话模型，浏览器子代理不可用。"
            if not provider_id:
                return "【错误】无法确定当前对话模型，浏览器子代理不可用。"
            logger.debug(f"[{self.metadata_name}] browse_web prov_id={provider_id!r}")
            resp = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=input or "请根据用户请求浏览网页并返回结果",
                system_prompt=self._browser_instruction,
                tools=self._browser_toolset,
                max_steps=self.agent_max_steps,
                tool_call_timeout=self.agent_tool_timeout,
            )
            text = getattr(resp, "completion_text", "") or ""
            logger.debug(
                f"[{self.metadata_name}] browse_web tool_loop_agent 返回，len={len(text)}"
            )
            return text or "（浏览器子代理未返回内容）"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.metadata_name}] browse_web 执行异常: {e}")
            return f"【错误】浏览器子代理执行失败：{e}"

    # ================================================================
    # 本地页面渲染预览入口（面向子代理：browse_local_page）
    # ================================================================

    @filter.llm_tool(name="browse_local_page")
    async def browse_local_page(
        self,
        event: AstrMessageEvent,
        path: str = "",
        full_page: bool = False,
        wait_ms: int = 0,
    ) -> str:
        """渲染本地 HTML 页面并返回渲染结果的视觉描述（本地页面预览工具，面向子代理）。

        用于子代理在无视觉模型时「查看」渲染后的本地 HTML 页面：以无头浏览器真实
        渲染（含 CSS/JS 执行），截图后调用视觉模型（如 mimo-v2.5）生成中文视觉描述
        （页面结构/主要文字/样式渲染异常）；视觉模型不可用时自动降级为页面文本提取，
        保证工具不空转。本工具不经过浏览会话管理器，每次渲染使用独立页面用完即关，
        不影响既有 browse_* 浏览会话状态。

        Args:
            path(string): 本地 HTML 文件的绝对路径。仅允许 AstrBot 工作区目录
                与插件 data 目录下的 .html/.htm 文件，其余路径一律拒绝。
            full_page(boolean): 是否截取整页长截图（默认 false，仅截首屏视口）。可省略
            wait_ms(number): 页面加载后等待 JS 渲染的毫秒数（默认 500）。可省略
        """
        try:
            # 轻量热更新配置：黑名单/截图/内网拦截等修改无需重启即生效。
            self._refresh_config()
            # 会话权限：与既有浏览工具一致，过白/黑名单（默认配置为空即放行）。
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"

            # 1. 参数与路径安全校验（白名单 + 防路径穿越，越权路径直接拒绝）。
            raw = str(path or "").strip()
            if not raw:
                return (
                    "【错误】path 不能为空：请传入要渲染查看的本地 HTML 文件绝对路径。"
                )
            if raw.lower().startswith("file://"):
                raw = raw[len("file://"):]
            try:
                target = Path(raw).expanduser().resolve()
            except Exception as e:  # noqa: BLE001
                return f"【错误】路径解析失败：{e}"
            ok, reason = self._check_local_page_path(target)
            if not ok:
                return f"【拒绝】{reason}"
            if not target.is_file():
                return f"【错误】文件不存在或不是普通文件：{target}"
            if target.suffix.lower() not in _LOCAL_PAGE_ALLOWED_EXTS:
                return f"【拒绝】仅支持 .html/.htm 文件：{target.name}"

            # 2. per-umo 串行化（同一会话的本地页面渲染不并发）。
            async with self._local_lock_for(self._umo_of(event)):
                # 3. 独立页面渲染：不经过 SessionManager，不占浏览会话额度。
                page = await self.browser.new_page()
                try:
                    file_uri = target.as_uri()
                    await page.goto(
                        file_uri,
                        wait_until="domcontentloaded",
                        timeout=int(self.timeout) * 1000,
                    )
                    try:
                        wait = int(wait_ms)
                    except (TypeError, ValueError):
                        wait = 0
                    if wait <= 0:
                        wait = _LOCAL_PAGE_DEFAULT_WAIT_MS
                    # 等待 JS 执行完成，保证动态渲染内容进入截图/文本。
                    await page.wait_for_timeout(wait)

                    # 4. 截图（保存到插件 data/screenshots/，可作交付附件路径）。
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    umo_hash = hashlib.md5(
                        self._umo_of(event).encode("utf-8")
                    ).hexdigest()[:8]
                    path_hash = hashlib.md5(
                        str(target).encode("utf-8")
                    ).hexdigest()[:8]
                    save_path = str(
                        self._screenshot_dir
                        / f"localpage_{umo_hash}_{path_hash}_{ts}.png"
                    )
                    screenshot_ok = False
                    if bool(full_page):
                        try:
                            await page.screenshot(path=save_path, full_page=True)
                            screenshot_ok = True
                        except Exception as e:  # noqa: BLE001 — 整页截图失败降级视口
                            logger.debug(f"整页截图失败，降级视口截图: {e}")
                    if not screenshot_ok:
                        screenshot_ok = bool(
                            await self.browser.screenshot(page, save_path)
                        )

                    info = await self.extractor.extract_page_info(page)
                    text = await self.extractor.extract_text(
                        page, max_chars=int(self.max_chars)
                    )

                    # 5. 视觉描述（mimo-v2.5 等）：成功返回描述；失败降级文本。
                    desc = ""
                    if screenshot_ok:
                        desc = await self._describe_screenshot(save_path, event)
                    if desc:
                        banned = self._check_banned(desc)
                        if banned:
                            return (
                                f"【拒绝】页面识图结果包含违禁内容：{banned}，"
                                "已拒绝输出。"
                            )
                        return (
                            f"【本地页面预览】{target}\n"
                            f"标题: {info['title']}\n"
                            f"截图: {save_path}\n"
                            f"渲染视觉描述：\n{desc}"
                        )

                    # 6. 降级路径：视觉模型不可用/截图失败 → 文本提取，保证不空转。
                    if text:
                        banned = self._check_banned(text)
                        if banned:
                            return (
                                f"【拒绝】页面内容包含违禁内容：{banned}，"
                                "已拒绝输出。"
                            )
                        return (
                            f"【本地页面预览·文本模式】{target}\n"
                            f"标题: {info['title']}\n"
                            f"（视觉模型不可用或截图失败，已降级为文本提取）\n"
                            f"页面文本：\n{text}"
                        )
                    if screenshot_ok:
                        return (
                            f"【本地页面预览】{target}\n"
                            "页面渲染成功，但未提取到文本内容"
                            f"（可能为纯图形页面）。截图: {save_path}"
                        )
                    return f"【错误】页面渲染成功但截图与文本提取均失败：{target}"
                finally:
                    # 用完即关：独立页面不残留，不影响既有浏览会话。
                    await self.browser.close_page(page)
        except Exception as e:  # noqa: BLE001 — LLM 工具必须容错
            logger.warning(f"[{METADATA_NAME}] browse_local_page 失败: {e}")
            return f"【错误】本地页面渲染预览失败：{e}"

    def _local_lock_for(self, umo: str) -> asyncio.Lock:
        """返回该会话的本地页面预览专用锁（不存在则创建）。

        锁表为弱引用字典：browse_local_page 结束、锁无任何强引用时
        条目自动清理（见 __init__ 注释），无需手动 pop。
        """
        lock = self._local_page_locks.get(umo)
        if lock is None:
            lock = asyncio.Lock()
            self._local_page_locks[umo] = lock
        return lock

    def _check_local_page_path(self, target: Path) -> tuple[bool, str]:
        """本地页面白名单校验：仅允许工作区与插件 data 目录下的路径。

        target 已 resolve()（消解 .. 与符号链接），此处用 is_relative_to
        做前缀白名单判断，杜绝路径穿越与软链逃逸。

        Returns:
            tuple[bool, str]: (是否允许, 拒绝原因或空串)。
        """
        roots = getattr(
            self, "_local_page_allowed_roots", None
        ) or _resolve_local_page_roots()
        for root in roots:
            try:
                if target.is_relative_to(root):
                    return True, ""
            except Exception:  # noqa: BLE001 — is_relative_to 异常按不匹配处理
                continue
        allowed_desc = "、".join(str(r) for r in roots)
        return (
            False,
            f"路径不在允许范围内（仅允许 {allowed_desc} 下的 .html/.htm 文件），"
            f"已拒绝访问：{target}",
        )

    # ================================================================
    # 私有辅助方法
    # ================================================================

    @staticmethod
    def _umo_of(event: AstrMessageEvent) -> str:
        """提取会话标识（unified_msg_origin），空时用群号/发送者兜底。"""
        umo = event.unified_msg_origin or ""
        if umo:
            return umo
        return f"{event.get_group_id() or ''}|{event.get_sender_id() or 'unknown'}"

    def _is_session_allowed(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """会话白/黑名单检查（参考 AtTool 实现）。

        匹配规则（v1.2.0 起精确匹配，修复子串误伤）：
        - 条目与 umo 按分隔符（: / |）切分后的字段精确比对，黑名单
          "123" 不再误伤群 "1234"（旧实现为子串匹配）；
        - 兼容完整 UMO 条目：条目与 umo 整体精确相等也命中（_conf_schema
          提示支持填写完整 UMO，保留该写法）；
        - 群号精确比对兜底。

        Returns:
            tuple[bool, str]: (是否允许, 拒绝原因或空串)。
        """
        umo = self._umo_of(event)
        group_id = event.get_group_id() or ""
        # umo 标准格式 platform:type:session_id；_umo_of 兜底格式
        # group|sender。切分为字段集合后精确比对，杜绝子串误伤。
        umo_fields = {
            field
            for segment in umo.split(":")
            for field in segment.split("|")
            if field
        }

        if self.session_blacklist:
            for entry in self.session_blacklist:
                entry = entry.strip()
                if not entry:
                    continue
                if (
                    entry in umo_fields
                    or entry == umo
                    or (group_id and entry == group_id)
                ):
                    return False, "此会话已被列入浏览器功能黑名单"

        if self.session_whitelist:
            allowed = False
            for entry in self.session_whitelist:
                entry = entry.strip()
                if not entry:
                    continue
                if (
                    entry in umo_fields
                    or entry == umo
                    or (group_id and entry == group_id)
                ):
                    allowed = True
                    break
            if not allowed:
                return False, "此会话未在白名单中，浏览器功能已禁用"

        return True, ""

    async def _get_page(self, event: AstrMessageEvent) -> tuple[Optional["Page"], str]:
        """取该会话关联的页面：先过白/黑名单，再向 SessionManager 取页。

        Returns:
            tuple[Page|None, str]: 成功返回 (page, '')；失败返回
            (None, 原因文本)。
        """
        allowed, deny_reason = self._is_session_allowed(event)
        if not allowed:
            return None, deny_reason
        if self.sessions is None:
            return None, "插件尚未初始化，请稍后再试"
        page = await self.sessions.ensure_page(self._umo_of(event))
        if page is None:
            return None, "会话数已达上限或页面创建失败，请稍后再试"
        return page, ""

    def _check_banned(self, text: str) -> str | None:
        """禁词检查：命中返回禁词原文，未命中返回 None。"""
        if self.safety is None:
            return None
        ok, word = self.safety.check_text(text)
        return None if ok else word

    async def _page_summary(self, page) -> str:
        """提取页面基本信息与正文，生成紧凑摘要文本。"""
        info = await self.extractor.extract_page_info(page)
        text = await self.extractor.extract_text(page, max_chars=int(self.max_chars))
        text = text or "（页面无文本内容）"
        return f"URL: {info['url']}\n标题: {info['title']}\n\n正文摘要: {text}"

    def _lock_for(self, event: AstrMessageEvent) -> asyncio.Lock:
        """返回该会话的专用锁（串行化同一会话的并发工具调用）。"""
        return self.sessions.get_lock(self._umo_of(event))

    # ================================================================
    # LLM 工具：浏览能力（全部返回 str，异常兜底，绝不抛出）
    # ================================================================

    async def browse_search(
        self, event: AstrMessageEvent, query: str, engine: str = ""
    ) -> str:
        """打开搜索引擎结果页以便后续在页面上继续交互（如点击进入具体网页）。仅当需要真实浏览器交互时才使用；纯搜索问答请用其他搜索工具。

        Args:
            query(string): 要搜索的关键词，如"北京天气"。
            engine(string): 搜索引擎名称，可选：必应搜索/百度搜索/谷歌搜索/B站搜索，留空用默认。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                banned = self._check_banned(query)
                if banned:
                    return f"【拒绝】搜索关键词包含违禁内容：{banned}"
                name = engine.strip() or self.default_search_engine
                template = self._search_engines.get(name)
                if not template:
                    options = "/".join(self._search_engines)
                    return f"【错误】未知搜索引擎：{name}，可选：{options}"
                url = template.format(keyword=quote(query))
                await page.goto(url, wait_until='domcontentloaded')
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001 — LLM 工具必须容错
            logger.warning(f"[{METADATA_NAME}] browse_search 失败: {e}")
            return f"【错误】搜索失败：{e}"

    async def browse_open(self, event: AstrMessageEvent, url: str) -> str:
        """网页交互场景专用：打开指定网页并返回该页面的文本内容。

        Args:
            url(string): 目标网页完整地址，须以 http:// 或 https:// 开头，如 https://example.com。
        """
        try:
            logger.debug(f"[{self.metadata_name}] browse_open 进入 url={url!r}")
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                ok, reason = await self.safety.acheck_url(url)
                if not ok:
                    return f"【拒绝】{reason}"
                banned = self._check_banned(url)
                if banned:
                    return f"【拒绝】URL 包含违禁内容：{banned}"
                await page.goto(url, wait_until='domcontentloaded')
                text = await self._page_summary(page)
                logger.debug(f"[{self.metadata_name}] browse_open 返回 len={len(text)}")
                return text
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.metadata_name}] browse_open 异常: {e}")
            return f"【错误】打开网页失败：{e}"

    async def browse_current_page(self, event: AstrMessageEvent) -> str:
        """获取当前浏览会话所在页面的 URL、标题与正文摘要（无需参数）。"""
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_current_page 失败: {e}")
            return f"【错误】读取当前页面失败：{e}"

    async def browse_get_text(self, event: AstrMessageEvent, max_chars: int = 0) -> str:
        """获取当前页面的正文纯文本（可自定义最大字符数）。

        Args:
            max_chars(number): 返回文本的最大字符数，<=0 使用插件默认值（4000）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                limit = int(max_chars) if max_chars and max_chars > 0 else int(self.max_chars)
                text = await self.extractor.extract_text(page, max_chars=limit)
                if not text:
                    return "当前页面没有可提取的文本内容。"
                return text
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_get_text 失败: {e}")
            return f"【错误】提取正文失败：{e}"

    async def browse_get_links(
        self, event: AstrMessageEvent, max_items: int = 0
    ) -> str:
        """获取当前页面上的链接编号列表，供后续 browse_click_link 点击。

        Args:
            max_items(number): 最多返回的链接条数，<=0 使用插件默认值（20）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                limit = int(max_items) if max_items and max_items > 0 else int(self.max_links)
                links = await self.extractor.extract_links(page, max_links=limit)
                if not links:
                    return "当前页面没有可点击的链接。"
                return links
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_get_links 失败: {e}")
            return f"【错误】提取链接失败：{e}"

    async def browse_click_link(self, event: AstrMessageEvent, index: int) -> str:
        """网页交互场景专用：点击当前页面上编号为 index 的链接并跳转（编号从 1 开始）。

        Args:
            index(number): 链接编号，从 1 开始，对应 browse_get_links 返回的列表编号。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                links = await self.extractor.extract_links(
                    page, max_links=int(self.max_links)
                )
                target = None
                for line in links.splitlines():
                    if line.startswith(f"[{index}]"):
                        parts = line.rsplit(" → ", 1)
                        if len(parts) == 2:
                            target = parts[1].strip()
                        break
                if not target:
                    return (
                        f"【错误】找不到编号为 {index} 的链接，"
                        "请先用 browse_get_links 查看当前页面的可选链接。"
                    )
                # SSRF 防护：链接来自页面内容（可能被攻陷），goto 前必须
                # 做协议白名单 + 内网地址拦截校验，防止诱导访问内网资源。
                ok, reason = await self.safety.acheck_url(target)
                if not ok:
                    return f"【拒绝】链接 {index} 未通过安全校验：{reason}"
                # 链接已是绝对 URL（playwright evaluate 已绝对化），直接 goto。
                await page.goto(target, wait_until='domcontentloaded')
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_click_link 失败: {e}")
            return f"【错误】点击链接失败：{e}"

    async def browse_scroll(
        self, event: AstrMessageEvent, direction: str = "down", pixels: int = 800
    ) -> str:
        """在当前页面按指定方向滚动，返回滚动后的页面内容。

        Args:
            direction(string): 滚动方向，down/下 向下滚动，up/上 向上滚动。
            pixels(number): 滚动像素数，默认 800。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                sign_map = {"down": 1, "下": 1, "up": -1, "上": -1}
                sign = sign_map.get(str(direction).strip())
                if sign is None:
                    return "【错误】direction 仅支持 down/下 或 up/上。"
                y = sign * int(pixels)
                await page.evaluate(f"window.scrollBy(0, {y})")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_scroll 失败: {e}")
            return f"【错误】滚动页面失败：{e}"

    async def browse_go_back(self, event: AstrMessageEvent) -> str:
        """返回上一页，返回跳转后的页面内容（无需参数）。"""
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                try:
                    await page.go_back()
                except Exception as e:  # noqa: BLE001
                    return f"【错误】后退失败：{e}"
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_go_back 失败: {e}")
            return f"【错误】后退失败：{e}"

    async def browse_go_forward(self, event: AstrMessageEvent) -> str:
        """前进到下一页，返回跳转后的页面内容（无需参数）。"""
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                try:
                    await page.go_forward()
                except Exception as e:  # noqa: BLE001
                    return f"【错误】前进失败：{e}"
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_go_forward 失败: {e}")
            return f"【错误】前进失败：{e}"

    # ================================================================
    # LLM 工具：进阶浏览能力（多标签 / 输入 / 截图）
    # ================================================================

    async def browse_new_tab(self, event: AstrMessageEvent, url: str) -> str:
        """新开一个标签页打开指定网址，返回新标签页的内容摘要。

        Args:
            url(string): 新标签页要打开的网址，须以 http:// 或 https:// 开头。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                ok, reason = await self.safety.acheck_url(url)
                if not ok:
                    return f"【拒绝】{reason}"
                banned = self._check_banned(url)
                if banned:
                    return f"【拒绝】URL 包含违禁内容：{banned}"
                umo = self._umo_of(event)
                new_page = await self.sessions.new_tab(umo, url)
                if new_page is None:
                    return "【错误】标签页数量已达上限，无法新开标签。"
                summary = await self._page_summary(new_page)
                return f"已新开标签页（当前共 {self.sessions.tab_count(umo)} 个标签）。\n{summary}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_new_tab 失败: {e}")
            return f"【错误】新开标签页失败：{e}"

    async def browse_switch_tab(self, event: AstrMessageEvent, index: int) -> str:
        """切换到指定编号的标签页（编号从 1 开始），返回切换后的内容摘要。

        Args:
            index(number): 目标标签页编号，从 1 开始，对应新开标签页时提示的编号。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                umo = self._umo_of(event)
                total = self.sessions.tab_count(umo)
                if total == 0:
                    return "【错误】当前会话没有任何标签页。"
                target = await self.sessions.switch_tab(umo, index)
                if target is None:
                    return f"【错误】标签页编号 {index} 不存在（当前共 {total} 个，编号从 1 开始）。"
                summary = await self._page_summary(target)
                return f"已切换到标签 {index}（共 {total} 个）。\n{summary}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_switch_tab 失败: {e}")
            return f"【错误】切换标签页失败：{e}"

    async def browse_close_tab(self, event: AstrMessageEvent, index: int) -> str:
        """关闭指定编号的标签页（编号从 1 开始），返回剩余标签数。

        Args:
            index(number): 要关闭的标签页编号，从 1 开始。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                umo = self._umo_of(event)
                total = self.sessions.tab_count(umo)
                if total == 0:
                    return "【错误】当前会话没有任何标签页。"
                closed = await self.sessions.close_tab(umo, index)
                if not closed:
                    return f"【错误】标签页编号 {index} 不存在（当前共 {total} 个，编号从 1 开始）。"
                remaining = self.sessions.tab_count(umo)
                return f"已关闭标签 {index}，剩余 {remaining} 个标签。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_close_tab 失败: {e}")
            return f"【错误】关闭标签页失败：{e}"

    async def browse_input(
        self, event: AstrMessageEvent, selector: str, text: str
    ) -> str:
        """网页交互场景专用：在页面上定位 CSS 选择器指向的输入框并输入文本。

        Args:
            selector(string): CSS 选择器，如 #search-input 或 input[name=q]。
            text(string): 要输入的文本内容。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                try:
                    await page.fill(selector, text)
                except Exception as e:  # noqa: BLE001 — fill 失败降级 type
                    logger.debug(f"page.fill 失败，降级 page.type: {e}")
                    await page.type(selector, text)
                return f"已在 {selector} 输入文本：{text}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_input 失败: {e}")
            return f"【错误】输入文本失败：{e}"

    async def browse_press_key(self, event: AstrMessageEvent, key: str) -> str:
        """在当前页面按下键盘按键（如 Enter 提交搜索、Escape 关闭弹窗）。

        Args:
            key(string): 按键名，支持 Enter/Escape/ArrowDown/ArrowUp/ArrowLeft/ArrowRight/Tab/Backspace/Home/End。
        """
        allowed_keys = {
            "Enter", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft",
            "ArrowRight", "Tab", "Backspace", "Home", "End",
        }
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                key = str(key).strip()
                if key not in allowed_keys:
                    return (
                        f"【错误】不支持的按键：{key}。"
                        f"支持：{'/'.join(sorted(allowed_keys))}"
                    )
                await page.keyboard.press(key)
                return f"已按下按键 {key}。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_press_key 失败: {e}")
            return f"【错误】按键操作失败：{e}"

    async def browse_screenshot(self, event: AstrMessageEvent) -> str:
        """截取当前页面截图并发送给用户，若配置识图模型则附上识图描述（无需参数）。

        子 Agent 工具上下文（FunctionTool.call）不能 yield 消息，因此发图
        改用 event.send(image_result(path))，返回文本供 LLM 阅读。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # 毫秒级，防同秒覆盖
                umo_hash = hashlib.md5(self._umo_of(event).encode("utf-8")).hexdigest()[:8]
                save_path = str(self._screenshot_dir / f"browse_{umo_hash}_{ts}.png")
                result = await self.browser.screenshot(page, save_path)
                if not result:
                    return "【错误】截图失败。"
                # 静默模式：截图仅内部识图，不发到群聊（减少刷屏）。
                if not self.silent_mode:
                    try:
                        await event.send(event.image_result(result))
                    except Exception as e:  # noqa: BLE001 — 发图失败不阻塞识图
                        logger.warning(f"[{METADATA_NAME}] 发送截图失败: {e}")
                # 识图（可选）：配置了 vision_provider_id 才尝试。
                desc = await self._describe_screenshot(result, event)
                if desc:
                    prefix = "截图已识图（静默模式，未发送）" if self.silent_mode else "截图已发送给用户"
                    return f"{prefix}。识图结果：\n{desc}"
                prefix = "截图已识图（静默模式，未发送）" if self.silent_mode else "截图已发送给用户"
                return f"{prefix}。如需识图请配置 vision_provider_id 多模态模型。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_screenshot 失败: {e}")
            return f"【错误】截图失败：{e}"

    async def browse_click_text(self, event: AstrMessageEvent, text: str) -> str:
        """网页交互场景专用：按文本内容精准点击页面元素（按钮/选项等非链接 JS 元素）。

        Args:
            text(string): 要点击的元素包含的文本内容，如"确认并继续"。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                text = str(text).strip()
                if not text:
                    return "【错误】text 不能为空。"
                clicked = False
                # 优先 Playwright 的 get_by_text（语义化文本定位）。
                if hasattr(page, "get_by_text"):
                    try:
                        await page.get_by_text(text, exact=False).first.click(timeout=10000)
                        clicked = True
                    except Exception as e:  # noqa: BLE001 — 未找到或不可点，降级
                        logger.debug(f"get_by_text 点击失败，降级 locator('text='): {e}")
                if not clicked:
                    try:
                        await page.locator(f"text={text}").first.click(timeout=10000)
                        clicked = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[{METADATA_NAME}] 未找到可点击元素（文本: {text}）: {e}")
                        return f"【错误】页面上找不到包含文本「{text}」的可点击元素。"
                logger.info(f"[{METADATA_NAME}] 已按文本点击: {text}")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_click_text 失败: {e}")
            return f"【错误】点击元素失败：{e}"

    async def browse_set_slider(self, event: AstrMessageEvent, value: int) -> str:
        """网页交互场景专用：设置当前页面的滑块（input[type=range]）为指定数值。

        Args:
            value(number): 滑块目标值。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                target = int(value)
                # 优先 playwright 原生 fill：对 React 受控组件有效
                # （直接 e.value 赋值不会更新 React 状态）。
                try:
                    await page.locator("input[type=range]").fill(str(target))
                    logger.info(f"[{METADATA_NAME}] 已用 fill 设置滑块值: {target}")
                    return await self._page_summary(page)
                except Exception as e:  # noqa: BLE001 — fill 失败（不可交互等）降级
                    logger.debug(f"滑块 fill 失败，降级 native setter: {e}")
                # 降级：用 native value setter 设值（绕过 React 的 value 拦截）
                # + 触发 input/change 事件。
                js = (
                    "(v)=>{const e=document.querySelector('input[type=range]');"
                    "if(!e)return false;"
                    "const setter=Object.getOwnPropertyDescriptor("
                    "window.HTMLInputElement.prototype,'value').set;"
                    "setter.call(e,v);"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));"
                    "return true;}"
                )
                ok = await page.evaluate(js, target)
                if not ok:
                    return "【错误】页面上没有找到滑块（input[type=range]）。"
                logger.info(f"[{METADATA_NAME}] 已用 native setter 设置滑块值: {target}")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_set_slider 失败: {e}")
            return f"【错误】设置滑块失败：{e}"

    async def browse_select_option(
        self, event: AstrMessageEvent, selector: str, value: str
    ) -> str:
        """网页交互场景专用：选择下拉框（select）的选项。

        Args:
            selector(string): 下拉框的 CSS 选择器，如 #age。
            value(string): 选项值（支持 label/value/index 字符串）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                await page.select_option(selector, value)
                logger.info(f"[{METADATA_NAME}] 已选择下拉项: {selector} = {value}")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_select_option 失败: {e}")
            return f"【错误】选择下拉选项失败：{e}"

    async def browse_click_coords(self, event: AstrMessageEvent, x: int, y: int) -> str:
        """网页交互场景专用：按视口坐标点击页面（识图后定位无文本元素时使用）。

        Args:
            x(number): 视口横坐标（像素）。
            y(number): 视口纵坐标（像素）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                await page.mouse.click(int(x), int(y))
                logger.info(f"[{METADATA_NAME}] 已点击坐标: ({x}, {y})")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_click_coords 失败: {e}")
            return f"【错误】坐标点击失败：{e}"

    async def browse_hover(self, event: AstrMessageEvent, text: str) -> str:
        """网页交互场景专用：悬停包含指定文本的元素（用于触发悬浮菜单）。

        Args:
            text(string): 要悬停的元素包含的文本。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                text = str(text).strip()
                if not text:
                    return "【错误】text 不能为空。"
                hovered = False
                if hasattr(page, "get_by_text"):
                    try:
                        await page.get_by_text(text, exact=False).first.hover(timeout=5000)
                        hovered = True
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"get_by_text 悬停失败，降级 locator('text='): {e}")
                if not hovered:
                    try:
                        await page.locator(f"text={text}").first.hover()
                        hovered = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[{METADATA_NAME}] 未找到可悬停元素（文本: {text}）: {e}")
                        return f"【错误】页面上找不到包含文本「{text}」的可悬停元素。"
                logger.info(f"[{METADATA_NAME}] 已悬停: {text}")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_hover 失败: {e}")
            return f"【错误】悬停元素失败：{e}"

    async def browse_reload(self, event: AstrMessageEvent) -> str:
        """刷新当前页面并返回刷新后的内容摘要（无需参数）。"""
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                logger.info(f"[{METADATA_NAME}] 已刷新页面")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_reload 失败: {e}")
            return f"【错误】刷新页面失败：{e}"

    async def browse_checkbox(
        self, event: AstrMessageEvent, selector: str, checked: bool
    ) -> str:
        """网页交互场景专用：设置复选框/单选框的选中状态。

        Args:
            selector(string): 复选框的 CSS 选择器。
            checked(boolean): 目标选中状态（true=勾选，false=取消）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                loc = page.locator(selector)
                is_checked = loc.is_checked()
                if bool(is_checked) != bool(checked):
                    await loc.click()
                logger.info(f"[{METADATA_NAME}] 已设置复选状态: {selector} -> {checked}")
                return await self._page_summary(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_checkbox 失败: {e}")
            return f"【错误】设置复选框状态失败：{e}"

    async def browse_zoom_crop(
        self, event: AstrMessageEvent, x: int, y: int, width: int, height: int
    ) -> str:
        """网页交互场景专用：裁剪页面指定区域并放大识图（查看无文本/小字区域）。

        Args:
            x(number): 裁剪区域左上角横坐标（视口像素）。
            y(number): 裁剪区域左上角纵坐标（视口像素）。
            width(number): 裁剪区域宽度（像素）。
            height(number): 裁剪区域高度（像素）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                # 裁剪区域 clamp 到视口大小，避免超出页面。
                viewport = page.viewport_size or {"width": 1280, "height": 800}
                vw, vh = int(viewport.get("width", 1280)), int(viewport.get("height", 800))
                cx = max(0, min(int(x), vw))
                cy = max(0, min(int(y), vh))
                cw = max(1, min(int(width), vw - cx))
                ch = max(1, min(int(height), vh - cy))
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                umo_hash = hashlib.md5(self._umo_of(event).encode("utf-8")).hexdigest()[:8]
                save_path = str(
                    self._screenshot_dir / f"browse_{umo_hash}_{ts}_crop.png"
                )
                await page.screenshot(
                    clip={"x": cx, "y": cy, "width": cw, "height": ch}, path=save_path
                )
                logger.info(
                    f"[{METADATA_NAME}] 已裁剪截图 ({cx},{cy},{cw}x{ch}) -> {save_path}"
                )
                # 识图该区域（配置了 vision_provider_id 才执行）。
                desc = await self._describe_screenshot(save_path, event)
                if desc:
                    return f"裁剪区域识图结果：\n{desc}"
                return f"已裁剪区域截图并保存：{save_path}（如需识图请配置 vision_provider_id 多模态模型）。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_zoom_crop 失败: {e}")
            return f"【错误】裁剪识图失败：{e}"

    async def browse_sniff_media(
        self, event: AstrMessageEvent, media_type: str = "all", max_items: int = 5
    ) -> str:
        """从当前页面嗅探图片/视频资源，下载并发送到群聊（用户主动请求媒体时使用）。

        Args:
            media_type(string): 媒体类型，image/video/all，默认 all。
            max_items(number): 最多下载数量，默认 5。
        """
        try:
            async with self._lock_for(event):
                # 参数校验前置：非法参数不开页面直接返回（_get_page 之前）。
                mtype = (media_type or "all").strip().lower()
                if mtype not in ("image", "video", "all"):
                    return "【错误】media_type 仅支持 image/video/all。"
                try:
                    limit = int(max_items) if max_items else 5
                except (TypeError, ValueError):
                    return f"【错误】max_items 参数无效：{max_items!r}，应为数字。"
                if limit <= 0:
                    limit = 5
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                # 收集页面媒体并带标签类型：img[src]、video[src]/source[src]、
                # a[href]（媒体后缀）。标签类型用于无后缀 URL 的兜底分类。
                js = """
                () => {
                  const out = [];
                  const seen = new Set();
                  const push = (u, tag) => {
                    if (u && !seen.has(u)) { seen.add(u); out.push({url: u, tag: tag}); }
                  };
                  document.querySelectorAll('img[src]').forEach(i => push(i.currentSrc || i.src, 'img'));
                  document.querySelectorAll('video[src]').forEach(v => push(v.currentSrc || v.src, 'video'));
                  document.querySelectorAll('video source[src], source[src]').forEach(s => push(s.src, 'video'));
                  document.querySelectorAll('a[href]').forEach(a => push(a.href, 'a'));
                  return out;
                }
                """
                raw_items = await page.evaluate(js)
                items = [
                    i for i in (raw_items or [])
                    if isinstance(i, dict) and isinstance(i.get("url"), str) and i["url"].strip()
                ]
                # 过滤：按 URL 后缀分类；无法分类时按标签类型兜底；
                # 跳过 m3u8 流媒体。
                img_exts = ("jpg", "jpeg", "png", "gif", "webp")
                vid_exts = ("mp4", "webm", "mov")

                def _classify(url: str, tag: str):
                    path = url.split("?", 1)[0].lower().rstrip("/")
                    if path.endswith(img_exts):
                        return "image"
                    if path.endswith(vid_exts):
                        return "video"
                    # 兜底：按来源标签推断类型（img→image，video→video）。
                    if tag == "img":
                        return "image"
                    if tag == "video":
                        return "video"
                    return None  # a[href] 无后缀无法判断，跳过

                wanted = []
                skipped = 0
                for item in items:
                    u, tag = item["url"], item.get("tag", "a")
                    if "m3u8" in u.lower():
                        skipped += 1
                        continue
                    cls = _classify(u, tag)
                    if cls is None:
                        skipped += 1
                        continue
                    if mtype in ("all", cls):
                        wanted.append((u, cls))
                if not wanted:
                    hint = f"（跳过 {skipped} 个无法分类/流媒体资源）" if skipped else ""
                    return f"当前页面没有嗅探到{'目标类型' if mtype != 'all' else ''}图片/视频资源{hint}。"
                # 下载最多 limit 个到 data/media/ 并发送。遍历 wanted 全部，
                # 被安全拦截/下载失败的继续消费下一个，直到凑够 limit 个
                # 成功项——避免被攻陷页面塞满内网 URL 挤占合法资源名额。
                downloaded = []
                blocked = 0
                for (u, cls) in wanted:
                    if len(downloaded) >= limit:
                        break
                    # SSRF 防护：URL 来自页面内容（可能被攻陷），下载前必须
                    # 校验协议白名单 + 内网地址拦截，未通过则跳过并计数。
                    ok, reason = await self.safety.acheck_url(u)
                    if not ok:
                        blocked += 1
                        logger.warning(
                            f"[{METADATA_NAME}] 媒体 URL 未通过安全校验，跳过: {u} ({reason})"
                        )
                        continue
                    try:
                        path = await self._download_media(u, len(downloaded) + 1, cls)
                        if not path:
                            continue
                        downloaded.append(path)
                        if cls == "image":
                            await event.send(event.image_result(path))
                        else:
                            from astrbot.api.message_components import Video  # noqa: PLC0415
                            await event.send(event.chain_result([Video.fromFileSystem(path)]))
                    except Exception as e:  # noqa: BLE001 — 单个失败继续
                        logger.warning(f"[{METADATA_NAME}] 下载/发送媒体失败 {u}: {e}")
                if blocked:
                    logger.info(f"[{METADATA_NAME}] 媒体嗅探跳过 {blocked} 个未通过安全校验的 URL")
                if not downloaded:
                    hint = f"。另有 {blocked} 个 URL 因安全校验被拦截" if blocked else ""
                    return f"媒体下载失败，请检查网络或资源可访问性{hint}。"
                lines = [f"已下载并发送 {len(downloaded)} 个媒体："]
                lines.extend(f"- {p}" for p in downloaded)
                if blocked:
                    lines.append(f"（另有 {blocked} 个 URL 被安全拦截，未下载）")
                return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_sniff_media 失败: {e}")
            return f"【错误】媒体嗅探失败：{e}"

    async def _download_media(self, url: str, index: int, media_class: str = "image") -> str:
        """下载单个媒体 URL 到 data/media/，返回本地路径（失败返回空串）。

        调用方负责 SSRF 校验（acheck_url）；此处做下载大小上限保护
        （50MB，Content-Length 预检 + 流式累计）。

        Args:
            url: 媒体 URL（已通过安全校验）。
            index: 序号（用于文件名）。
            media_class: 媒体类型（image/video），决定兜底扩展名。
        """
        # 扩展名：按 URL 后缀；无法推断时按媒体类型兜底（不用 .bin）。
        ext = Path(url.split("?", 1)[0]).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mov"):
            ext = ".png" if media_class == "image" else ".mp4"
        umo_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_path = str(self._media_dir / f"media_{umo_hash}_{ts}_{index}{ext}")
        # 下载大小上限（字节）。
        max_bytes = 50 * 1024 * 1024
        # 重定向跳数上限：aiohttp 默认自动跟随重定向，可绕过调用方 acheck_url
        # 的前置 SSRF 校验直达内网；此处关闭自动跟随，逐跳手动跟随并每跳
        # 重新过安全校验（协议白名单 + 内网拦截）。
        max_redirects = 5
        current_url = url
        try:
            import aiohttp  # noqa: PLC0415 — 延迟导入（沙箱无 aiohttp 时不影响模块加载）

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as session:
                tmp_path = save_path + ".part"

                def _cleanup_tmp() -> None:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass

                downloaded_ok = False
                for _hop in range(max_redirects + 1):
                    async with session.get(
                        current_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                        allow_redirects=False,
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            location = resp.headers.get("Location")
                            if not location:
                                logger.warning(
                                    f"[{METADATA_NAME}] 媒体重定向缺少 Location，终止: {current_url}"
                                )
                                return ""
                            next_url = urljoin(current_url, location)
                            ok, reason = await self.safety.acheck_url(next_url)
                            if not ok:
                                logger.warning(
                                    f"[{METADATA_NAME}] 媒体重定向目标未通过安全校验，"
                                    f"终止下载: {next_url} ({reason})"
                                )
                                return ""
                            current_url = next_url
                            continue
                        if resp.status != 200:
                            logger.warning(
                                f"[{METADATA_NAME}] 媒体下载非 200: {current_url} -> {resp.status}"
                            )
                            return ""
                        # Content-Length 预检：超限直接跳过。
                        length = resp.content_length
                        if length is not None and length > max_bytes:
                            logger.warning(
                                f"[{METADATA_NAME}] 媒体超 {max_bytes // 1024 // 1024}MB 上限，跳过: {current_url} ({length} bytes)"
                            )
                            return ""
                        # 流式写入并累计大小，超过上限中止并删除半成品。
                        total = 0
                        try:
                            with open(tmp_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    total += len(chunk)
                                    if total > max_bytes:
                                        logger.warning(
                                            f"[{METADATA_NAME}] 媒体流式累计超 {max_bytes // 1024 // 1024}MB 上限，中止: {current_url}"
                                        )
                                        _cleanup_tmp()
                                        return ""
                                    f.write(chunk)
                        except Exception:
                            # 异常时清理半成品文件。
                            _cleanup_tmp()
                            raise
                        if total == 0:
                            return ""
                        downloaded_ok = True
                        break
                if not downloaded_ok:
                    # 重定向跳数超限：未拿到最终 200 响应。
                    _cleanup_tmp()
                    logger.warning(
                        f"[{METADATA_NAME}] 媒体重定向超过 {max_redirects} 跳，终止: {url}"
                    )
                    return ""
            # 流式写入成功：临时文件改名成正式路径。
            Path(tmp_path).rename(save_path)
            return save_path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] 媒体下载失败 {url}: {e}")
            return ""

    async def _describe_screenshot(
        self, image_path: str, event: AstrMessageEvent | None = None
    ) -> str:
        """用多模态模型描述截图（配置了 vision_provider_id 才执行）。

        行为：配置的识图 provider 未加载/失效时自动回退当前会话聊天
        Provider（带警告日志）；未配置或全部不可用时返回空串（不抛异常）。
        模型返回拒识文案（纯文本模型不支持图片，如 "[Unsupported Image]"）
        时记警告日志并返回明确提示文案，不把拒识文本误当视觉描述。
        双通道降级：contexts（ImageURLPart）失败后回退 image_urls。

        Args:
            image_path: 截图本地路径。
            event: 消息事件（失效回退时确定会话），可为 None。

        Returns:
            str: 识图描述文本；未配置或调用失败返回空串；拒识时返回提示
            文案（均不抛异常）。
        """
        provider_id = await self._resolve_vision_provider_id(event)
        if not provider_id:
            return ""
        # 识图提示词：优先用用户配置的 vision_prompt（实时读取），空则回退
        # 默认提示词。
        prompt = self._live_vision_prompt() or (
            "请用中文描述这张网页截图的重点内容，"
            "包括主要文字、按钮、链接和页面结构。"
        )
        try:
            # 延迟导入：沙箱/无 astrbot 包环境不触发。
            from astrbot.core.agent.message import (  # noqa: PLC0415
                ImageURLPart,
                UserMessageSegment,
            )
            from astrbot.core.agent.message import (
                TextPart as VisionTextPart,
            )
            img = ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_path))
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    contexts=[UserMessageSegment(content=[img, VisionTextPart(text=prompt)])],
                ),
                timeout=60,
            )
            text = getattr(resp, "completion_text", "") or ""
            if text.strip():
                if _is_vision_rejection(text):
                    logger.warning(
                        f"[{self.metadata_name}] 识图返回拒识文本，判定模型不支持图片"
                        f"（provider={provider_id}, text={text.strip()[:60]!r}）"
                    )
                    return _VISION_REJECTION_HINT
                return text.strip()
        except Exception as e:  # noqa: BLE001 — contexts 方式失败，降级 image_urls
            logger.debug(f"[{self.metadata_name}] 识图 contexts 方式失败，降级 image_urls: {e}")
        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[image_path],
                ),
                timeout=60,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            if _is_vision_rejection(text):
                logger.warning(
                    f"[{self.metadata_name}] 识图返回拒识文本，判定模型不支持图片"
                    f"（provider={provider_id}, text={text[:60]!r}）"
                )
                return _VISION_REJECTION_HINT
            return text
        except Exception as e:  # noqa: BLE001 — 识图失败必须降级不抛异常
            logger.warning(f"[{self.metadata_name}] 识图失败: {e}")
            return ""

    # ================================================================
    # LLM 请求注入：浏览指引
    # ================================================================

    @filter.on_llm_request()
    async def inject_browser_instruction(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """在 LLM 请求前注入网页浏览工具使用指引。"""
        allowed, deny_reason = self._is_session_allowed(event)
        if not allowed:
            req.system_prompt = (req.system_prompt or "") + (
                f"\n\n【注意】{deny_reason}，本会话不允许使用网页浏览功能。"
                "\n不要调用任何 browse_* 工具；如用户询问网页内容，"
                "请直接用自然语言说明无法执行。"
            )
            return

        instruction = self._build_browser_instruction()
        req.system_prompt = (req.system_prompt or "") + instruction

    def _build_browser_instruction(self) -> str:
        """构建浏览委托指引（注入给主 LLM 的文本）。

        定位：浏览器子代理（browser_agent）是重操作，主 LLM 仅在需要
        真实网页交互时调用 browse_web 委托；纯搜索/问答不要调用。
        """
        return (
            "\n\n## 网页浏览委托指引（浏览器子代理）\n"
            "你可以用 browse_web 工具把网页浏览任务委托给浏览器子代理"
            "（参数 input 为给子代理的任务描述）。\n\n"
            "【不要用】纯信息查询 / 搜索问答（如『今天天气』『xx是什么』"
            "『搜一下 xxx 新闻』）应优先使用其他搜索 / 联网工具或直接回答，"
            "不要调用 browse_web。浏览器是重操作（打开真实"
            "页面、耗时数秒、占用资源），仅为纯搜索启动是浪费。\n\n"
            "【要用】以下场景才调用 browse_web：\n"
            "① 用户明确要求打开 / 查看某个具体网址或网页内容；\n"
            "② 需要在页面上交互：点击链接 / 按钮、滚动加载、翻页、填表、"
            "按键、标签页切换；\n"
            "③ 搜索结果摘要不足以回答，需要进入具体网页阅读详情；\n"
            "④ 需要向用户展示页面外观（截图）；\n"
            "⑤ 页面是动态渲染、其他工具抓不到正文。\n\n"
            "委托时给子代理清晰的任务描述（要打开什么、查找什么、"
            "是否要交互），子代理会自主调用 browse_* 工具完成浏览并"
            "返回结果；你消化结果后再回复用户，不要暴露工具调用过程细节。"
        )
