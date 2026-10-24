"""
astrbot_plugin_browser_llm — LLM 浏览器插件（Agent 驱动的网页浏览）

架构：工具化子代理（tool-based subagent）+ 独立搜索池。

对外暴露 **3 个 LLM 工具**：``browse_web``（网页浏览入口）、
``browse_local_page``（本地 HTML 自检入口）、``browser_web_search``
（v1.4.0 新增，直接调搜索 API 池，返回结构化 JSON）。24 个 ``browse_*``
浏览工具**不**直接暴露给主 LLM，而是作为「浏览器子代理」的工具集：主 LLM
调 ``browse_web`` 后由子代理（tool_loop_agent + 24 个 FunctionTool）自主
完成真实网页交互。配置为 6 个分组 / 44 个叶键（含 v1.4.0 的 `search` 组 16 键与
v1.5.0 的 `tools` 组 3 键），见 ``_conf_schema.json``。

核心组件在 initialize() 中组装：BrowserCore（浏览器驱动）、
SessionManager（会话隔离与回收）、ContentExtractor（内容提取）、
SafetyFilter（禁词过滤与 SSRF 防护），以及 24 个 FunctionTool 构建
的 ToolSet（子代理工具集）。
"""

import asyncio
import functools
import hashlib
import os
import re
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote, urljoin, urlsplit

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .core import as_bool, as_float, as_int
from .core.background import (
    _BG_MAX_CONCURRENT,
    _BG_BUSY_TEXT,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    BackgroundJob,
    BackgroundRegistry,
    render_accepted_text,
    render_rejected_text,
    render_terminal_text,
    truncate_result,
)
from .core.browser import BrowserCore, validate_browser_type
from .core.config_map import ConfigView, schema_leaf
from .core.crawler import AiohttpFetcher, CrawlerLimits, FetchOutcome
from .core.delivery import artifact_sink, collect_artifact
from .core.extract import ContentExtractor
from .core.safety import SafetyFilter, check_url_literal
from .core.search import (
    SearchService,
    redact_secrets,
    render_payload,
    summarize,
)
from .core.search_providers import (
    PROVIDER_CLASSES,
    build_provider_entries,
    is_entry_usable,
    parse_provider_configs,
)
from .core.session import SessionManager

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查
    from playwright.async_api import Page

# 插件注册名，与 metadata.yaml 的 name 一致；日志与管理命令判权时使用。
METADATA_NAME = "astrbot_plugin_browser_llm"

# 插件版本：以 metadata.yaml 的 version 为单一事实源，模块加载时读取（真实
# 运行时 Star 实例无 self.metadata 属性，无法动态读取；metadata.yaml 在插件
# 目录中随发布流程维护，发布时只改这一处）。读取失败（文件缺失/无 version
# 字段）时回退内置兜底值，保证插件在异常部署形态下仍可加载。
_PLUGIN_VERSION_FALLBACK = "v1.4.0"

#: 插件目录与 metadata.yaml 路径（数据目录/标识解析共用，只算一次）。
_PLUGIN_DIR = Path(__file__).resolve().parent
_METADATA_YAML = _PLUGIN_DIR / "metadata.yaml"

try:
    _metadata_text = _METADATA_YAML.read_text(encoding="utf-8")
    _metadata_version = re.search(
        r"^version:\s*['\"]?([^\s'\"]+)", _metadata_text, re.MULTILINE
    )
    PLUGIN_VERSION = (
        _metadata_version.group(1) if _metadata_version else _PLUGIN_VERSION_FALLBACK
    )
except OSError:
    PLUGIN_VERSION = _PLUGIN_VERSION_FALLBACK

# LLM 搜索工具名（v4）。**只在 main.py 定义这一处**，测试与文档都引用它。
#
# 为什么不用 ``web_search`` / ``web_search_<provider>``：核心内置搜索族是
# ``web_search_{baidu,tavily,bocha,brave,firecrawl,exa,anysearch,serpapi,
# deepseek}``（``astrbot/core/tools/web_search_tools.py``），且在请求装配时
# **后加入并覆盖**同名插件工具（``ToolSet.add_tool``），插件删不掉内置项；
# 用同名会变成"两份并存、插件那份被静默挤掉"。``browser_web_search`` 与
# 内置族、与子代理的 ``browse_search`` 都不同名 ⇒ 并存、不接管。
_SEARCH_TOOL_NAME = "browser_web_search"

# 三个工具的名字（v1.5.0 后台模式按名字判开关语义）。装饰器里仍是字面量，
# 这里集中一份供日志/登记/文案引用，避免"改一处漏一处"。
_BROWSE_WEB_TOOL_NAME = "browse_web"
_LOCAL_PAGE_TOOL_NAME = "browse_local_page"

# 后台任务（v1.5.0 路线 D）：
# - ``tools.background_mode`` 三取值：off（默认，行为与 v1.4.0 完全一致）/
#   browse_only（只把 browse_web 后台化）/ all（三个工具都后台化）；
# - 终态消息统一以「后台任务完成」开头，生产判据按前缀 grep（派单 §5.3）；
# - 后台任务在 terminate() 时取消并等待一小段，绝不让插件重载悬挂。
_BACKGROUND_MODES = ("off", "browse_only", "all")

# terminate 资源清理总超时（秒）：超过则放弃等待并强制收尾，防止插件重载
# 被悬挂的 close 阻塞（曾实测 browser.close 悬挂数小时，旧实例 chromium 进程
# 与 playwright driver 长期残留）。底层 BrowserCore 已带单步超时，此为兜底。
_TERMINATE_TIMEOUT = 20.0

# 后台任务取消的等待上限（秒）：取消是协作式的，给一个很短的上限即可——
# 超出就放弃等待（任务随后会随事件循环关闭），优先保证重载不被阻塞。
_BACKGROUND_TERMINATE_TIMEOUT = 5.0

# 数据目录：截图/媒体等资源保存位置。
# 与插件安装目录解耦：优先走核心 StarTools（``data/plugin_data/<metadata.name>``），
# 兜底用同一路径显式拼接。理由有二：插件目录升级时会被**整目录覆盖**（`AGENTS.md`
# §2 记过同类事故），把运行期产物放进去等于随升级丢失；插件目录也可能是只读挂载。
# import 期 StarTools 尚未注册（无 star_map），故两级兜底；测试/离线环境落到
# 插件目录下的 ``data/``（与 v1.3.x 行为一致，测试无需改动）。


def _plugin_identity() -> str:
    """插件标识：优先 metadata.yaml 的 name，读不到时退回目录名。"""
    try:
        import yaml  # 核心依赖，插件运行期可用

        data = yaml.safe_load(_METADATA_YAML.read_text(encoding="utf-8")) or {}
        name = str(data.get("name") or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001 — 缺 yaml/文件损坏时退回目录名
        pass
    return _PLUGIN_DIR.name


def _resolve_data_dir() -> Path:
    """解析插件数据目录；StarTools 在 import 期可能尚未注册，故有兜底。"""
    try:
        from astrbot.core.star.star_tools import StarTools  # noqa: PLC0415 — 延迟导入

        return Path(StarTools.get_data_dir())
    except Exception:  # noqa: BLE001 — import 期无 star_map，走显式拼接
        pass
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_data_path,
        )

        return Path(get_astrbot_data_path()) / "plugin_data" / _plugin_identity()
    except Exception:  # noqa: BLE001 — 测试/离线环境最后的兜底
        return _PLUGIN_DIR / "data"


_DATA_DIR = _resolve_data_dir()

# 浏览器子代理名称（tool_loop_agent 内层 LLM 的 system_prompt 标识）。
_BROWSER_AGENT_NAME = "browser_agent"

# ---------------------------------------------------------------
# browse_local_page（本地页面渲染预览，面向子代理）安全白名单常量
# ---------------------------------------------------------------
# 允许渲染查看的根目录：AstrBot 工作区 + 插件数据目录。
# 子代理（frontend/engineer 等）仅能预览这两处目录下的 HTML 文件，
# 其余路径一律拒绝（防路径穿越与越权读取）。运行时会用
# get_astrbot_workspaces_path() 解析真实工作区路径，解析失败回退此默认值。


def _resolve_workspaces_fallback() -> Path:
    """工作区目录：优先核心 helper，兜底按 <root>/data/workspaces 推导。

    不能再按数据目录上溯两级推导：数据目录已经改到 ``data/plugin_data/<name>``，
    层级完全变了，旧写法会指到 ``<root>/data`` 而不是 ``<root>/data/workspaces``。
    """
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_workspaces_path,
        )

        return Path(get_astrbot_workspaces_path()).resolve()
    except Exception:  # noqa: BLE001 — 老核心无此 helper
        pass
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_data_path,
        )

        return (Path(get_astrbot_data_path()) / "workspaces").resolve()
    except Exception:  # noqa: BLE001 — 测试/离线环境
        # 层级依赖：插件位于 <root>/data/plugins/<name>/main.py，parents[2] 即 <root>/data。
        return (Path(__file__).resolve().parents[2] / "workspaces").resolve()


_LOCAL_PAGE_WORKSPACE_FALLBACK = _resolve_workspaces_fallback()
# 允许渲染的文件扩展名。
_LOCAL_PAGE_ALLOWED_EXTS = (".html", ".htm")
# 渲染完成后等待 JS 执行的默认时长（毫秒），保证动态内容渲染完成后再截图。
_LOCAL_PAGE_DEFAULT_WAIT_MS = 500

# ---------------------------------------------------------------
# 页面感知模式（v1.3.0 精细化）：工具参数级 + 会话规则级
# ---------------------------------------------------------------
# 可选值全集，browse_web 参数前缀 / perception_rules / page_perception
# 三处共用，保证口径一致。
_PERCEPTION_MODES = ("text", "text_image", "image")
# browse_web input 开头的显式感知模式前缀：`perception=<mode>`（大小写
# 不敏感），后跟空格或换行再接任务描述。命中后本次任务覆盖全局配置。
_PERCEPTION_PREFIX_RE = re.compile(
    r"^perception\s*=\s*(text|text_image|image)(?=\s|$)", re.IGNORECASE
)
# 非法前缀识别：出现 `perception=` 形态但值不在可选集（含等号后空值，
# v1.3.3）时，整段剔除并记录警告，回落「规则 > 全局」默认链路，不把
# 前缀当任务内容。剔除范围分支：`=值 + 分隔空白`（值非空）或
# `= 空白`（空值，v1.3.3——仅剔除前缀形态与空白，不吞任务文本）。
_INVALID_PERCEPTION_PREFIX_RE = re.compile(
    r"^perception\s*=(?:\S+\s*|\s*)", re.IGNORECASE
)
# 识图短时缓存防膨胀阈值：写入后缓存条目数超过该值即清理过期项。
_VISION_CACHE_MAX_ENTRIES = 256

# ---------------------------------------------------------------
# 识图拒识检测：纯文本模型拒识文案识别
# ---------------------------------------------------------------
# 部分纯文本模型（如 deepseek-v4-flash）收到图片请求时返回固定拒识文案
# （如 "[Unsupported Image]"）而非真实视觉描述。命中任一特征即判定识图
# 失败，返回明确提示而非把拒识文本静默透传为「视觉描述」。正则大小写不敏感，
# 覆盖中英文常见拒识表达。
#
# 误判防线（v1.3.0 修复，P2）：真实视觉描述正文中可能包含与拒识短语同构的
# 叙述性文本（如"浏览器不支持图像懒加载功能"），全文 search 会误判为拒识、
# 破坏识图缓存链。故单次短语命中须满足以下二者之一才算拒识：
#   A. 响应开头位置约束：命中位置在前缀窗口（_VISION_REJECTION_PREFIX_LEN）
#      内，且短语左侧不是正文引号引用（左引号紧跟短语，如页面错误提示文案
#      "「无法处理图片，请检查网络」"）。真实拒识均为短响应、短语紧邻开头
#      （既有 24 个拒识用例最远命中位置约 18 字符，窗口 60 留 3 倍余量）；
#      真实视觉描述则以结构化导语开头（"这张网页截图的重点内容如下："），
#      叙述性同构短语出现在正文中后部（tester 复现样例实测 ≥140 字符），
#      天然落在窗口之外。
#   B. 整句独立：命中所在的子句（按 。！？!?；; 换行切分）去除拒识短语后
#      仅剩标点/空白/语气软化词（抱歉/对不起/Sorry 等），即该子句本身就是
#      一句纯粹的拒识短句——兜底"长导语后单独一行拒识"等少见场景。
# 实现选择理由：前缀窗口最简单且对既有拒识用例召回 100%；子句独立规则补齐
# 长响应场景。已知残余歧义（文档化，可接受）：短描述开头、未加引号的叙述句
# （如"页面中有一张无法处理图片的占位符"）在字面上与真实拒识开头（"抱歉，
# 无法处理图片内容。"）不可区分，前缀窗口无法排除，属设计取舍，不新增规则。
_VISION_REJECTION_HINT = "识图模型不支持图片，请更换多模态模型或清空识图配置"
# 拒识短语允许出现的响应开头窗口（字符数）：见上方误判防线说明 A。
_VISION_REJECTION_PREFIX_LEN = 60
# 左引号字符：短语左侧近距离内存在左引号视为正文引用（页面提示/说明文案）。
_VISION_REJECTION_OPEN_QUOTES = ("「", "『", "“", '"', "'", "‘")
# 语气软化词：判定「整句独立」时先从子句中剔除。
_VISION_REJECTION_SOFTENERS = (
    "抱歉",
    "对不起",
    "不好意思",
    "很遗憾",
    "遗憾",
    "sorry",
    "unfortunately",
    "apologies",
    "alas",
)
# 子句分隔符：中文句号/问号/叹号/分号 + 英文 !?; + 换行。不切分逗号——
# 逗号连接的多分句拒识（如"图片无法识别，请换一种方式提问。"）仍属同一子句，
# 由前缀窗口规则覆盖；若按逗号切分，叙述句"页面提示：无法处理图片，请重试"
# 会被切出纯拒识子句造成误判。
_VISION_REJECTION_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")

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
        # 中文：图片无法/不能/不支持（处理/识别/查看/理解）——动作词必填，
        # 避免"图片无法加载"等叙述性表达误命中（v1.3.0 收紧）
        r"图片?(?:无法|不能|不支持)(?:处理|识别|查看|理解)",
        r"无法(?:处理|识别|查看|理解)(?:图片|图像|截图)",
        r"不支持(?:图片|图像|多模态|视觉)",
        r"不是(?:多模态|视觉)(?:模型)?",
        r"没有(?:视觉|识图|图像处理)(?:能力|功能)?",
    )
)


def _is_vision_rejection(text: str) -> bool:
    """判断模型返回文本是否为「拒识」而非真实视觉描述。

    单次短语命中须满足「响应开头位置约束」或「整句独立」（见
    _VISION_REJECTION_PATTERNS 上方说明）；空文本返回 False。
    """
    if not text:
        return False
    for pattern in _VISION_REJECTION_PATTERNS:
        for match in pattern.finditer(text):
            if _rejection_match_hit(text, pattern, match):
                return True
    return False


def _rejection_match_hit(
    text: str, pattern: re.Pattern[str], match: re.Match[str]
) -> bool:
    """单次拒识短语命中的判定：条件 A（前缀窗口且非引号引用）或条件 B（整句独立）。"""
    if match.start() <= _VISION_REJECTION_PREFIX_LEN and not _match_is_quoted(
        text, match
    ):
        return True
    return _match_in_pure_clause(text, pattern, match)


def _match_is_quoted(text: str, match: re.Match[str]) -> bool:
    """短语左侧近距离内存在左引号 → 视为正文引用（页面提示文案）而非拒识。

    真实拒识响应不会把拒识短语包进引号；而页面错误提示/说明文字常以
    "「无法处理图片」"等形式出现。仅检查短语左侧 6 字符，避免误伤
    "抱歉，无法处理图片内容。"这类短语前带软化词的真实拒识。
    ASCII 撇号（I'm / don't）不视为引号：前后均为 ASCII 字母时跳过。
    """
    if match.start() == 0:
        return False
    prefix_start = max(0, match.start() - 6)
    prefix = text[prefix_start : match.start()]
    for idx, ch in enumerate(prefix):
        if ch not in _VISION_REJECTION_OPEN_QUOTES:
            continue
        if ch == "'":
            before = prefix[idx - 1] if idx > 0 else ""
            after = (
                text[prefix_start + idx + 1]
                if prefix_start + idx + 1 < len(text)
                else ""
            )
            if (
                before.isascii()
                and before.isalpha()
                and after.isascii()
                and after.isalpha()
            ):
                continue  # 撇号（如 I'm），非引号
        return True
    return False


def _match_in_pure_clause(
    text: str, pattern: re.Pattern[str], match: re.Match[str]
) -> bool:
    """整句独立判定：命中所在子句去除拒识短语后仅剩标点/空白/语气软化词。"""
    clause_start = 0
    clause_end = len(text)
    for sep in _VISION_REJECTION_CLAUSE_SPLIT_RE.finditer(text):
        if sep.end() <= match.start():
            clause_start = sep.end()
        elif sep.start() >= match.end():
            clause_end = sep.start()
            break
    clause = text[clause_start:clause_end]
    residual = pattern.sub("", clause)
    residual = _VISION_REJECTION_CLAUSE_SPLIT_RE.sub("", residual)
    residual = re.sub(r"[\s\W_]+", "", residual)  # 去空白/标点（\w 含中文）
    for softener in _VISION_REJECTION_SOFTENERS:
        residual = residual.replace(softener, "")
    return not residual


# 平台工作区候选（子代理产物默认落盘目录，存在即加入白名单）：
# 本平台约定工作区为 /root/workspace（root 部署的 agent 沙箱惯例，与 AstrBot
# 自身 data/workspaces 不同）；非本平台部署时该目录不存在，自然不生效。
# 额外根目录可通过环境变量 BROWSER_LLM_EXTRA_LOCAL_ROOTS（按系统路径分隔符
# 分隔：Linux/macOS 为冒号、Windows 为分号）追加，无需改代码。
_PLATFORM_WORKSPACE_CANDIDATES = (Path("/root/workspace"),)
_EXTRA_LOCAL_ROOTS_ENV = "BROWSER_LLM_EXTRA_LOCAL_ROOTS"


def _clamp(value, low, high):
    """把配置/参数值夹到闭区间 ``[low, high]``（非法值由 _as_* 先行兜底）。"""
    return max(low, min(high, value))


def _redact_url(url: str) -> str:
    """日志脱敏（v1.3.1）：隐藏 URL query/fragment 参数值，防 token/签名泄漏。

    保留 scheme://host/path 与 query 参数名，参数值统一打码为 ***；
    fragment 整体打码。无 query/fragment 时原样返回（host/path 视为
    不敏感）。仅用于日志输出，不影响实际请求。
    """
    url = str(url or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    redacted_query = ""
    if parsed.query:
        segs = []
        for seg in parsed.query.split("&"):
            if not seg:
                continue
            if "=" in seg:
                key, _value = seg.split("=", 1)
                segs.append(f"{key}=***")
            else:
                segs.append(f"{seg}=***")
        redacted_query = "?" + "&".join(segs)
    fragment = "#***" if parsed.fragment else ""
    prefix = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
    return f"{prefix}{parsed.path}{redacted_query}{fragment}"


def _resolve_local_page_roots() -> tuple[Path, ...]:
    """解析 browse_local_page 允许访问的根目录白名单（去重保序）。

    白名单构成：
    1. 默认工作区绝对路径（由插件安装位置推导的 AstrBot 工作区目录，标准
       部署下与服务实际工作区一致）；
    2. 运行时解析的工作区（get_astrbot_workspaces_path() 实时值，恒加入，
       仅要求目录存在——AstrBot 重定位时以此兜底，防进程 CWD 不确定导致
       白名单根漂移；标准部署下与 1 相同，去重后只保留一份）；
    3. 平台工作区候选（_PLATFORM_WORKSPACE_CANDIDATES，存在即加入）；
    4. 环境变量 BROWSER_LLM_EXTRA_LOCAL_ROOTS 指定的额外根目录（按系统
       路径分隔符分隔：Linux/macOS 为冒号、Windows 为分号）；
    5. 插件 data 目录（路径由插件自身位置推导，天然稳定）。
    """
    roots: list[Path] = [_LOCAL_PAGE_WORKSPACE_FALLBACK]
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_workspaces_path,
        )

        live = Path(get_astrbot_workspaces_path()).resolve()
    except Exception:  # noqa: BLE001 — 解析失败忽略，仅用默认值
        live = None
    if live is not None and live.is_dir():
        roots.append(live)
    for cand in _PLATFORM_WORKSPACE_CANDIDATES:
        try:
            if cand.is_dir():
                roots.append(cand.resolve())
        except OSError:  # noqa: BLE001 — 无法访问的候选忽略
            continue
    for part in os.environ.get(_EXTRA_LOCAL_ROOTS_ENV, "").split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            p = Path(part).resolve()
            if p.is_dir():
                roots.append(p)
        except OSError:  # noqa: BLE001 — 非法路径忽略
            continue
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


# 24 个浏览工具配置：name / description（展示给子 Agent LLM）/
# parameters（JSON Schema）/ method（插件实例方法名）。
_BROWSER_TOOLS = [
    {
        "name": "browse_search",
        "description": "打开搜索引擎结果页以便后续在页面上继续交互（如点击进入具体网页）。仅当需要真实浏览器交互时才使用；纯搜索问答请用其他搜索工具。",
        "parameters": _params(
            {"query": ("要搜索的关键词，如北京天气", "string")},
            {
                "engine": (
                    "搜索引擎名称，可选：必应搜索/百度搜索/谷歌搜索/B站搜索，留空用默认",
                    "string",
                )
            },
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
            {
                "index": (
                    "链接编号，从 1 开始，对应 browse_get_links 返回的列表编号",
                    "number",
                )
            },
        ),
        "method": "browse_click_link",
    },
    {
        "name": "browse_scroll",
        "description": "在当前页面按指定方向滚动，返回滚动后的页面内容。",
        "parameters": _params(
            {},
            {
                "direction": ("滚动方向，down/下 向下滚动，up/上 向上滚动", "string"),
                "pixels": ("滚动像素数，默认 800", "number"),
            },
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
            {
                "selector": ("CSS 选择器，如 #search-input 或 input[name=q]", "string"),
                "text": ("要输入的文本内容", "string"),
            },
        ),
        "method": "browse_input",
    },
    {
        "name": "browse_press_key",
        "description": "在当前页面按下键盘按键（如 Enter 提交搜索、Escape 关闭弹窗）。",
        "parameters": _params(
            {
                "key": (
                    "按键名，支持 Enter/Escape/ArrowDown/ArrowUp/ArrowLeft/ArrowRight/Tab/Backspace/Home/End",
                    "string",
                )
            },
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
            {
                "selector": ("下拉框的 CSS 选择器，如 #age", "string"),
                "value": ("选项值（label/value/index 均可）", "string"),
            },
        ),
        "method": "browse_select_option",
    },
    {
        "name": "browse_click_coords",
        "description": "网页交互场景专用：按视口坐标点击页面（识图后定位无文本元素时使用）。",
        "parameters": _params(
            {
                "x": ("视口横坐标（像素）", "number"),
                "y": ("视口纵坐标（像素）", "number"),
            },
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
            {
                "selector": ("复选框的 CSS 选择器", "string"),
                "checked": ("目标选中状态（true=勾选，false=取消）", "boolean"),
            },
        ),
        "method": "browse_checkbox",
    },
    {
        "name": "browse_zoom_crop",
        "description": "网页交互场景专用：裁剪页面指定区域并放大识图（豆包式局部放大，用于查看无文本/小字区域）。",
        "parameters": _params(
            {
                "x": ("裁剪区域左上角横坐标（视口像素）", "number"),
                "y": ("裁剪区域左上角纵坐标（视口像素）", "number"),
                "width": ("裁剪区域宽度（像素）", "number"),
                "height": ("裁剪区域高度（像素）", "number"),
            },
        ),
        "method": "browse_zoom_crop",
    },
    {
        "name": "browse_sniff_media",
        "description": "从当前页面嗅探图片/视频资源并下载发送到群聊（用户主动请求媒体时使用）。",
        "parameters": _params(
            {},
            {
                "media_type": ("媒体类型：image/video/all，默认 all", "string"),
                "max_items": ("最多下载数量，默认 5", "number"),
            },
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
    "- 点击生成/提交类按钮后，不要刷新页面、不要重新开始、不要后退，"
    "避免结果页丢失；\n"
    "- 用 browse_get_text / browse_current_page 周期性重读当前页面等待"
    "结果出现，每次读取间隔 5-8 秒，最多等待 60 秒；\n"
    "- 若页面仍在生成中（进度指示/空白），继续等待重读，不要中途放弃；\n"
    "- 拿到结果后，原样返回结果页的关键文字。"
)


class BrowserLLMPlugin(Star):
    """LLM 浏览器插件主入口。

    AstrBot 会以 (context, config) 实例化本类；config 为 _conf_schema.json
    定义的配置项（含默认值）。__init__ 只读配置，核心组件在
    initialize() 中创建。
    """

    # 内容审核总开关的类级默认（与 _conf_schema.json 的 default 一致）：
    # 真实实例由 _load_config 读配置覆盖；测试桩经 __new__ 直构、未走
    # _load_config 时也保有安全默认（开启）。
    enable_banned_filter: bool = True

    # 后台任务相关配置的类级默认（v1.5.0，与 _conf_schema.json 的 default
    # 一致）：默认 off ⇒ 三个工具保持 v1.4.0 的前台同步行为。
    background_mode: str = "off"
    background_timeout: float = 240.0
    background_max_chars: int = 1500

    def __init__(self, context: Context, config: AstrBotConfig = None):
        """初始化插件：读取全部配置项到 self.* 属性。

        Args:
            context: AstrBot 插件上下文，提供事件注册与消息发送能力。
            config: 插件配置对象，对应 _conf_schema.json 的 6 个分组 /
                44 个叶键（browser / limits / security / perception /
                search，以及 v1.5.0 新增的 tools 组 3 键）；未传入时用
                空字典兜底。
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

        # 搜索工具运行期状态（v4）：
        # - ``_search_service``：ProviderEntry 池 + 冷却 + TTL 缓存；配置
        #   provider 段变化时重建（冷却随之重置，属预期）；
        # - ``_search_tool_active``：initialize() 对齐后的工具可见性影子值，
        #   供提示词注入判断（不查 manager，避免热重载竞态）。
        self._search_service: SearchService | None = None
        self._search_service_signature: tuple | None = None
        self._search_tool_active: bool = False

        # 识图短时缓存（v1.3.0）：(umo, 规范化URL) -> (写入时间戳, 描述文本)。
        # 并发安全说明：插件运行在 asyncio 单线程事件循环内，dict 读/写天然
        # 原子，无需额外锁；同一 URL 并发首次识图可能重复调用 LLM（无害，
        # 结果一致，仅多一次 token 消耗），不引入锁以保持实现简单。条目按
        # TTL 惰性过期，超过 _VISION_CACHE_MAX_ENTRIES 时清理过期项防膨胀，
        # terminate 时整体清空（防重载残留）。
        self._vision_cache: dict[tuple[str, str], tuple[float, str]] = {}

        # 搜索引擎模板：{keyword} 会被 URL 编码后替换。
        self._search_engines = {
            "必应搜索": "https://cn.bing.com/search?q={keyword}&FORM=BESBTB&ensearch=1",
            "百度搜索": "https://www.baidu.com/s?wd={keyword}",
            "谷歌搜索": "https://www.google.com.hk/search?&q={keyword}",
            "B站搜索": "https://search.bilibili.com/all?keyword={keyword}",
        }

        # 后台任务登记表（v1.5.0 路线 D）：umo -> 任务，同 UMO 单飞。
        # 纯内存、单事件循环内使用；terminate() 时整体取消。
        self._background: BackgroundRegistry = BackgroundRegistry()

    def _load_config(self) -> None:
        """将 _conf_schema.json 中的配置项读取为实例属性。

        全部 6 组 44 叶（``_conf_schema.json``）：``browser``（引擎/默认页/
        搜索引擎/视口/代理/超时）、``limits``（提取与链接上限/页数/空闲回收/
        子代理步数与超时）、``security``（禁词/审核总开关/内网拦截/会话黑白
        名单）、``perception``（感知模式/截图/识图 provider 与缓存）、
        ``search``（v1.4.0 搜索 API 池 16 键：开关/池/并发/步数/超时/抓取）、
        ``tools``（v1.5.0 后台任务 3 键：模式/整任务硬预算/结果截断长度）。

        类型健壮性（v1.3.1）：数值/布尔配置经 _as_int / _as_float /
        _as_bool 归一解析，非法值（字符串、None、类型错误）回退默认值并
        记录 warning——手动编辑 config.json 或第三方写入非法类型时插件
        仍可正常加载，不再抛异常阻断初始化。Dashboard 路径本身有 AstrBot
        类型校验保护，此处理为纵深防御。
        """
        cfg = self.config
        # S1：读取点统一切到点号路径；ConfigView 在过渡期自动回退到旧扁平键，
        # 因此 schema 不变、配置不变时行为与旧实现完全一致。
        view = ConfigView(cfg)

        # 浏览器基础
        self.browser_type: str = str(view.get("browser.type", "chromium"))
        self.default_url: str = str(
            view.get("browser.default_url", "https://www.baidu.com")
        )
        self.default_search_engine: str = str(
            view.get("browser.default_search_engine", "必应搜索")
        )

        # 安全与过滤
        self.banned_words: list = view.get(
            "security.banned_words",
            [
                "pornhub",
                "色情",
                "成人",
                "赌博",
                "暴力",
                "政治",
                "反动",
                "恐怖",
                "谣言",
                "诈骗",
                "病毒",
            ],
        )
        self.block_internal_ip: bool = self._as_bool(
            view.get("security.block_internal_ip", True), True
        )
        # 内容审核总开关（v1.3.3）：关闭后 _check_banned 统一放行，不再因
        # banned_words 拦截正文/标题/搜索词/识图结果（敏感词密集站点适用，
        # 避免整页拒绝）；不影响 SSRF 内网拦截与本地页路径白名单等安全边界。
        self.enable_banned_filter: bool = self._as_bool(
            view.get("security.enable_banned_filter", True), True
        )

        # 提取与资源控制
        self.max_chars: int = self._as_int(view.get("limits.max_chars", 4000), 4000)
        self.max_links: int = self._as_int(view.get("limits.max_links", 20), 20)
        self.timeout: float = self._as_float(view.get("browser.timeout", 30), 30)
        self.max_pages: float = self._as_float(view.get("limits.max_pages", 5), 5)
        self.idle_timeout: float = self._as_float(
            view.get("limits.idle_timeout", 1800), 1800
        )

        # 会话权限控制
        self.session_whitelist: list = view.get("security.session_whitelist", [])
        self.session_blacklist: list = view.get("security.session_blacklist", [])

        # 截图与识图
        self.enable_screenshot: bool = self._as_bool(
            view.get("perception.enable_screenshot", True), True
        )
        self.vision_provider_id: str = str(
            view.get("perception.vision_provider_id", "") or ""
        )
        self.vision_prompt: str = str(
            view.get(
                "perception.vision_prompt",
                "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。",
            )
        )
        # 静默模式：开启后截图仅内部识图，不发到群聊。
        self.silent_mode: bool = self._as_bool(
            view.get("perception.silent_mode", True), True
        )
        # 页面感知方式：text / text_image / image（全局默认）。
        self.page_perception: str = str(
            view.get("perception.page_perception", "text_image")
        )
        # 会话规则级感知模式（v1.3.0）：list[dict]，按 UMO 子串匹配（不区分
        # 大小写）命中第一条；优先级：browse_web 显式参数 > 规则 > 全局。
        self.perception_rules: list = list(
            view.get("perception.perception_rules", []) or []
        )
        # 识图短时缓存 TTL（秒，v1.3.0）：同 URL 截图在 TTL 内复用上次识图
        # 结果；0 表示关闭缓存（空值/None 同样按 0 处理，与旧行为一致）。
        self.vision_cache_ttl: int = self._as_int(
            view.get("perception.vision_cache_ttl", 60) or 0, 60
        )
        # 媒体缓存保留天数：超过自动清理，节省磁盘；0/负值 = 不清理（v1.3.1）。
        self.cache_days: int = self._as_int(view.get("limits.cache_days", 3), 3)

        # 网络与视口
        self.proxy: str = str(view.get("browser.proxy", "") or "")
        self.viewport: dict = view.get(
            "browser.viewport", {"width": 1280, "height": 800}
        )

        # 浏览器子 Agent 控制（agent-as-tool）
        self.agent_max_steps: int = self._as_int(
            view.get("limits.agent_max_steps", 70), 70
        )
        self.agent_tool_timeout: int = self._as_int(
            view.get("limits.agent_tool_timeout", 1200), 1200
        )

        # ---- search 组（v4）：LLM 搜索工具 browser_web_search ----
        # 全部默认值 = 「关 + 空池」⇒ v4 代码 + v3 配置时工具不出现，行为与
        # 旧版完全一致（这是迁移顺序不变量的另一半：新代码必须吃得下旧配置）。
        self.search_enabled: bool = self._as_bool(
            view.get("search.enabled", False), False
        )
        raw_providers = view.get("search.providers", [])
        self.search_providers: list = list(raw_providers or []) if isinstance(
            raw_providers, (list, tuple)
        ) else []
        self.search_max_parallel: int = _clamp(
            self._as_int(view.get("search.max_parallel", 3), 3), 1, 10
        )
        self.search_max_results: int = _clamp(
            self._as_int(view.get("search.max_results", 8), 8), 1, 50
        )
        # 搜索步数 = 波次上限（每波 ≤ max_parallel，最多 max_rounds 波）。
        self.search_max_rounds: int = _clamp(
            self._as_int(view.get("search.max_rounds", 2), 2), 1, 5
        )
        self.search_provider_timeout: float = _clamp(
            self._as_float(view.get("search.provider_timeout", 15), 15), 3, 60
        )
        self.search_total_timeout: float = _clamp(
            self._as_float(view.get("search.total_timeout", 60), 60), 5, 300
        )
        self.search_cooldown_seconds: float = _clamp(
            self._as_float(view.get("search.cooldown_seconds", 60), 60), 0, 3600
        )
        self.search_cache_ttl: float = _clamp(
            self._as_float(view.get("search.cache_ttl", 120), 120), 0, 3600
        )
        self.search_fetch_pages: bool = self._as_bool(
            view.get("search.fetch_pages", False), False
        )
        self.search_fetch_top_n: int = _clamp(
            self._as_int(view.get("search.fetch_top_n", 3), 3), 0, 5
        )
        self.search_fetch_timeout: float = _clamp(
            self._as_float(view.get("search.fetch_timeout", 12), 12), 3, 60
        )
        self.search_fetch_max_chars: int = _clamp(
            self._as_int(view.get("search.fetch_max_chars", 2000), 2000), 200, 20000
        )
        self.search_region: str = str(view.get("search.region", "") or "").strip()
        self.search_safesearch: str = str(
            view.get("search.safesearch", "moderate") or "moderate"
        ).strip().lower()
        if self.search_safesearch not in ("off", "moderate", "strict"):
            self.search_safesearch = "moderate"
        # 搜索与抓取共用（与浏览器代理 browser.proxy 相互独立）。
        self.search_proxy: str = str(view.get("search.proxy", "") or "").strip()

        # ---- tools 组（v1.5.0 路线 D）：后台任务化 ----
        # 三个工具「前台阻塞 → 后台异步」的开关与整任务预算。默认 off ⇒ 与
        # v1.4.0 行为逐字节一致（前台同步返回真实结果），这是本改动的
        # 回滚档位：把 background_mode 改回 off 即回退行为，不需要降版本。
        mode = str(view.get("tools.background_mode", "off") or "off").strip().lower()
        # 非法值 fail-closed 到 off 并记 warning：宁可维持现行为，也不要
        # "写错一个词就变后台"（设计 §1.7-1）。
        if mode not in _BACKGROUND_MODES:
            logger.warning(
                f"[{self.metadata_name}] tools.background_mode={mode!r} 非法"
                f"（可选 {'/'.join(_BACKGROUND_MODES)}），已按 off 处理（前台同步）。"
            )
            mode = "off"
        self.background_mode: str = mode
        # 整任务硬预算（秒）：后台模式必须自带上限——核心 tool_call_timeout
        # 生产是 1000s，后台分支在平台原生路线下更宽，插件层不兜就等于没有上限。
        self.background_timeout: float = _clamp(
            self._as_float(view.get("tools.background_timeout", 240), 240), 30, 900
        )
        # 终态消息的结果截断长度（字符）：browse_web 的文本可能几千字，
        # 直接发会刷屏；钳 200-8000（设计 §1.1，与 search.* 的钳制同风格）。
        self.background_max_chars: int = _clamp(
            self._as_int(view.get("tools.background_max_chars", 1500), 1500),
            200,
            8000,
        )

    def _as_int(self, value, default: int) -> int:
        """int 配置解析：委托 core 共享实现（v1.3.3 收敛双实现）。"""
        return as_int(value, default)

    def _as_float(self, value, default: float) -> float:
        """float 配置解析：委托 core 共享实现（v1.3.3 收敛双实现）。"""
        return as_float(value, default)

    def _as_bool(self, value, default: bool) -> bool:
        """bool 配置解析：委托 core 共享实现（v1.3.3 收敛双实现）。"""
        return as_bool(value, default)

    def _refresh_config(self) -> None:
        """轻量热更新配置：工具入口重读共享 config dict 并同步固化组件。

        Dashboard 保存配置时 AstrBot 会就地 update 传入插件的同一 config
        dict（面板保存即更新，并触发插件热重载兜底）；_load_config 把最新
        值刷入实例属性，本方法再同步进 SessionManager / SafetyFilter /
        BrowserCore 的运行期参数（max_pages、idle_timeout、default_url、
        禁词、内网拦截），使黑名单、内网拦截、截图开关等配置修改无需
        重启即生效。v1.3.0 新增的 perception_rules / vision_cache_ttl 由
        _load_config 一并重读，天然热更新（下次 browse_web 解析规则、
        下次识图用新 TTL）。仅浏览工具入口调用，开销为若干次 dict 读取。

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
        # 页面感知方式变化后重建全局默认子代理指令（browse_web 每次按
        # 解析出的感知模式动态构造，此预构建值仅作全局默认兜底/日志口径）。
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
        # 运行期产物目录：截图与媒体下载都挂到 _DATA_DIR 下（不写插件安装目录，
        # 否则整目录覆盖升级会连产物一起丢掉）。
        self._screenshot_dir = _DATA_DIR / "screenshots"
        self._media_dir = _DATA_DIR / "media"
        for _dir in (self._screenshot_dir, self._media_dir):
            _dir.mkdir(parents=True, exist_ok=True)
        # 首次清理过期缓存 + 启动定期清理任务（每 6 小时，独立于会话 sweeper）。
        self._cache_cleanup_task: asyncio.Task | None = None
        try:
            self._cleanup_cache()
        except Exception as e:  # noqa: BLE001 — 清理失败不影响启动
            logger.warning(f"[{self.metadata_name}] 首次缓存清理失败: {e}")
        self._start_cache_cleanup_task()
        # 后台空闲回收任务（每 60s 关闭超时未活动的会话）。
        await self.sessions.start_sweeper()
        # 构建 24 个浏览工具（FunctionTool）与子 Agent 工具集。
        self._browser_tools = [_make_browser_tool(self, s) for s in _BROWSER_TOOLS]
        self._browser_toolset = self._build_toolset(self._browser_tools)
        # 按页面感知方式动态生成子代理指令（基础模板 + 感知方式段）。
        self._browser_instruction = self._build_subagent_instruction()
        # 识图 provider 下拉同步：把 AstrBot 已加载 provider 写进内存 schema
        # 的 options（Dashboard 面板每次请求实时读取该 schema，无需重启）。
        # 注意 AstrBot 启动顺序是「插件加载先于 provider 初始化」，此处可能
        # 暂时拿不到 provider 列表，由后台轮询任务兜底补齐。
        self._vision_sync_task: asyncio.Task | None = None
        self._sync_vision_provider_options()
        self._start_vision_provider_sync_task()
        logger.info(
            f"[{self.metadata_name}] 浏览器子代理工具集: "
            f"{len(self._browser_tools)} 个浏览工具"
        )
        logger.info(f"[{self.metadata_name}] 页面感知方式: {self.page_perception}")
        logger.info(
            f"[{self.metadata_name}] 内容审核（禁词过滤）: "
            f"{'开启' if self.enable_banned_filter else '关闭'}"
        )
        # 搜索工具可见性对齐（v4）：配置是唯一事实源，initialize() 双向收敛
        # "持久化 inactivated_llm_tools 名单"与配置之间的分叉。
        await self._reconcile_search_tool()
        logger.info(f"[{self.metadata_name}] 插件 {PLUGIN_VERSION} 已激活")
        logger.info(f"[{self.metadata_name}] 浏览器引擎: {self.browser_type}")
        logger.info(
            f"[{self.metadata_name}] 默认搜索引擎: {self.default_search_engine}"
        )

    @staticmethod
    def _build_toolset(tools: list) -> object | None:
        """把 FunctionTool 列表包装成 ToolSet（子代理工具集）。"""
        from astrbot.core.agent.tool import ToolSet  # noqa: PLC0415 — 延迟导入

        return ToolSet(tools=tools)

    def _build_subagent_instruction(self, perception: str | None = None) -> str:
        """动态生成浏览器子代理指令（基础模板 + 页面感知方式段）。

        基础模板为模块级常量 _BROWSER_AGENT_INSTRUCTION（工具清单 +
        操作原则 + 验证码红线 + 等待结果策略）；再按感知方式追加
        『页面感知方式』说明，指导子代理用文字还是截图感知页面。

        Args:
            perception: 本次任务的感知模式（text/text_image/image）。
                None 或非法值时回退全局 page_perception（再回退
                text_image），保证任何输入都能产出可用指令。

        Returns:
            str: 完整子代理 system_prompt。
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
        mode = (perception or "").strip().lower()
        if mode not in _PERCEPTION_MODES:
            # 非法/空显式值：回退全局 page_perception（再回退 text_image）。
            mode = (self.page_perception or "").strip().lower()
        perception_note = perception_map.get(mode, perception_map["text_image"])
        return f"{_BROWSER_AGENT_INSTRUCTION}\n\n{perception_note}"

    def _cleanup_cache(self) -> int:
        """清理超过 cache_days 天的媒体/截图缓存文件，返回删除数。

        遍历 data/media/ 与 data/screenshots/ 下的文件，删除 mtime
        早于 (now - cache_days) 的旧文件；目录本身保留。
        cache_days <= 0 表示「不清理」（与 vision_cache_ttl=0 关闭缓存
        的语义一致），直接返回 0——避免 0 被误当作「立即清理全部」。
        """
        if self.cache_days <= 0:
            return 0
        cutoff = time.time() - self.cache_days * 86400
        removed = 0
        for base in (
            getattr(self, "_media_dir", None),
            getattr(self, "_screenshot_dir", None),
        ):
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
            logger.info(
                f"[{self.metadata_name}] 缓存清理完成，删除 {removed} 个过期文件"
            )
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
            # S2b2a：分组 schema 下叶子在 perception.items 里，不能只查顶层。
            item = schema_leaf(schema, "perception.vision_provider_id")
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
        view = ConfigView(self.config)
        value, found = view.lookup("perception.vision_provider_id")
        if found:
            return str(value or "").strip()
        return str(getattr(self, "vision_provider_id", "") or "").strip()

    def _live_vision_prompt(self) -> str:
        """实时读取识图提示词（同 _live_vision_provider_id 的时效语义）。"""
        view = ConfigView(self.config)
        value, found = view.lookup("perception.vision_prompt")
        if found:
            return str(value or "").strip()
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
        # 后台任务（v1.5.0 路线 D）：先取消在跑任务并等一小段——否则浏览器关了
        # 它们才抛错，终态消息面对的是已销毁的浏览器/页面，重载还多一批悬挂协程。
        await self._cancel_background_tasks()
        try:
            await asyncio.wait_for(
                self._shutdown_browser_resources(), timeout=_TERMINATE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(
                f"{tag} 资源清理总超时（{_TERMINATE_TIMEOUT:.1f}s），部分资源可能未释放"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"{tag} 资源清理异常: {e}")
        finally:
            # 本地页面预览锁表清空（弱引用字典常规会自动清理，此处显式兜底）。
            self._local_page_locks.clear()
            # 识图短时缓存清空（防插件重载/禁用后内存残留）。
            self._vision_cache.clear()
            # 搜索服务缓存/冷却清空（密钥随服务实例一起释放）。
            if self._search_service is not None:
                self._search_service.clear_cache()
            self._search_service = None
            self._search_service_signature = None
            self._search_tool_active = False
        logger.info(f"{tag} 已终止")

    # ================================================================
    # 后台任务（v1.5.0 路线 D：插件内闭环，不依赖核心 is_background_task）
    # ================================================================

    #: 视频类媒体扩展名（``browse_sniff_media`` 会把视频交给 ``_emit_media``，
    #: 统一按 Image 发会失败，必须按扩展名分流 —— 见 ``_deliver_terminal``）。
    _VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv")

    def _background_enabled_for(self, tool_name: str) -> bool:
        """该工具本次是否走后台（按 ``tools.background_mode`` 判定）。

        - ``off``（默认）：三个工具一律前台同步，行为与 v1.4.0 完全一致；
        - ``browse_only``：只后台化 ``browse_web``——它是唯一 68s 量级的长
          任务（一次调用跑整套子 Agent），也是唯一"用户要等一整段多步浏览"
          的入口；
        - ``all``：三个工具都后台化（``browser_web_search`` 常态 1-3s，后台化
          等于多一次投递，只在明确要求"任何工具都不阻塞会话"时才开）。
        """
        mode = getattr(self, "background_mode", "off")
        if mode == "all":
            return True
        if mode == "browse_only":
            return tool_name == _BROWSE_WEB_TOOL_NAME
        return False

    @staticmethod
    def _umo_digest(umo: str) -> str:
        """日志用的会话摘要：只记 8 位 md5，不把原始 UMO 写进后台任务日志行。"""
        return hashlib.md5(str(umo or "").encode("utf-8")).hexdigest()[:8]

    async def _submit_background(
        self,
        *,
        event: AstrMessageEvent,
        tool_name: str,
        runner,
        task_text: str = "",
    ) -> str:
        """把一次工具执行提交为后台任务，立刻返回「已受理 + task_id」。

        裁决顺序（设计 §3.2，任一命中都**同步**返回、不占 task_id、不登记）：
        ① 便宜校验（会话白/黑名单、禁词——禁词对象必须是剔除 ``perception=``
        前缀后的任务文本，与 v1.4.0 口径一致）；② 同 UMO 单飞；③ 全局并发上限。

        Args:
            event: 触发本次工具调用的消息事件（只用于取 UMO）。
            tool_name: 三个工具之一的名字（日志/文案用）。
            runner: ``runner(progress) -> Awaitable[str]``；``progress`` 是本次
                任务专用的半成品容器（供超时/失败时发「已得部分结果」）。
            task_text: 用户可见的任务文本（只用于禁词检查，**不落日志**）。

        Returns:
            str: 立即返回文案（含 task_id 与"你现在没有结果"的显式声明）。
        """
        tag = f"[{self.metadata_name}]"
        umo = self._umo_of(event)
        allowed, deny_reason = self._is_session_allowed(event)
        if not allowed:
            # 越权/黑名单必须让主模型**同步**知道（它不会再重试），不进后台。
            return f"【拒绝】{deny_reason}"
        banned = self._check_banned(task_text)
        if banned:
            return f"【拒绝】请求参数包含违禁内容：{banned}"
        busy = self._background.active_job(umo)
        if busy is not None:
            # 单飞裁决：纯查表、无 await、不持锁、不排队（设计 §2.5）。
            return render_rejected_text(
                task_id=busy.task_id,
                tool=busy.tool,
                elapsed_s=busy.elapsed_s(),
            )
        if len(self._background.running()) >= _BG_MAX_CONCURRENT:
            # 全局闸（D2）：不占 task_id、不登记，只回一句"稍后再试"。
            return _BG_BUSY_TEXT
        task_id = self._background.next_task_id()
        # 提交时快照：运行中改配置不会让在跑的任务突然变预算/变截断（§1.7-3）。
        job = BackgroundJob(
            task_id=task_id,
            umo=umo,
            tool=tool_name,
            started_at=time.monotonic(),
            budget_s=float(self.background_timeout),
            max_chars=int(self.background_max_chars),
        )
        self._background.register(job)
        task = asyncio.create_task(
            self._run_background_job(job, runner),
            name=f"browser_llm:{task_id}",
        )
        self._background.attach_task(task_id, task)
        logger.info(
            f"{tag} 后台任务受理 task_id={task_id} tool={tool_name} "
            f"umo={self._umo_digest(umo)} mode={self.background_mode} "
            f"budget={job.budget_s:.0f}s max_chars={job.max_chars}"
        )
        return render_accepted_text(
            task_id=task_id, tool=tool_name, budget_s=job.budget_s
        )

    async def _run_background_job(self, job: BackgroundJob, runner) -> None:
        """后台执行体：跑任务 → 渲染终态 → 主动投递 → 登记出表。

        四种终态（成功/失败/超时/已取消）都要有**用户可见**的终态消息，只有
        日志等于结果丢了；投递失败也不能把任务悬挂在登记表里。
        """
        tag = f"[{self.metadata_name}]"
        began = time.monotonic()
        status, text, reason = STATUS_OK, "", ""
        artifacts: list[str] = []
        partial = ""
        delivered = False
        progress: dict = {}
        try:
            # artifact_sink 是 ContextVar：create_task 会拷贝当前上下文，因此
            # 在**任务自己的**上下文里进 sink 即可拦住本任务经 event.send 出去的
            # 媒体（_emit_media 优先 collect_artifact），任务结束时统一发送。
            with artifact_sink() as bucket:
                artifacts = bucket
                text = await asyncio.wait_for(
                    runner(progress), timeout=float(job.budget_s)
                )
        except asyncio.TimeoutError:
            status = STATUS_TIMEOUT
            reason = f"超过整任务硬预算 {float(job.budget_s):.0f}s 被中断"
            partial = self._background_partial(job.tool, progress)
        except asyncio.CancelledError:
            status = STATUS_CANCELLED
            reason = "插件重载/禁用"
            partial = self._background_partial(job.tool, progress)
        except Exception as exc:  # noqa: BLE001 — 后台任务不能把异常抛给事件循环
            status = STATUS_FAILED
            reason = f"{type(exc).__name__}: {exc}"
            partial = self._background_partial(job.tool, progress)
        # 工具把"可预期失败"以【错误】/【拒绝】前缀当文本返回（例如取不到当前
        # 对话模型、页面内容命中禁词）：后台模式下必须判成失败，否则用户看到的
        # 是「完成」里包着一条错误。
        if status == STATUS_OK and str(text or "").startswith(("【错误】", "【拒绝】")):
            status, reason = STATUS_FAILED, str(text)
        elapsed_ms = int((time.monotonic() - began) * 1000)
        note = ""
        try:
            if status == STATUS_CANCELLED:
                # 终止路径：投递只作 best-effort（平台对象可能已在销毁），
                # 给一个很短的硬上限，绝不让插件重载被投递拖住。
                try:
                    delivered, note = await asyncio.wait_for(
                        self._deliver_terminal(
                            job, status, text, reason, elapsed_ms, artifacts, partial
                        ),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    delivered = False
            else:
                delivered, note = await self._deliver_terminal(
                    job, status, text, reason, elapsed_ms, artifacts, partial
                )
        finally:
            self._background.finish(job.task_id)
            # 终态日志口径（F3）：一条一任务，`后台任务终态 status=` 供生产
            # 判据 grep；不把四态混进「后台任务完成」这一个词里。
            logger.info(
                f"{tag} 后台任务终态 status={status} task_id={job.task_id} "
                f"tool={job.tool} elapsed_ms={elapsed_ms} chars={len(text)} "
                f"media={len(artifacts)} delivered={bool(delivered)}"
            )
            if note:
                # 终态消息首行另打一条 info，便于按 `【后台任务…】` 对账。
                logger.info(f"{tag} {note}")

    def _background_partial(self, tool_name: str, progress: dict) -> str:
        """中断前的半成品文本（没有就返回空串，绝不编造）。

        - ``browse_local_page``：渲染段拿到页面文本就写 ``progress["text"]``；
        - ``browser_web_search``：``progress`` 是 ``search_waves`` 的增量快照
          （results/attempts），这里渲染成人读的条目；
        - ``browse_web``：子代理只在终局返回 ``completion_text``，**没有**可
          增量获取的部分结果（README/CHANGELOG 同步写明）。
        """
        if not isinstance(progress, dict) or not progress:
            return ""
        if tool_name == _LOCAL_PAGE_TOOL_NAME:
            return str(progress.get("text") or "")
        if tool_name == _SEARCH_TOOL_NAME:
            return self._render_search_partial(progress)
        return ""

    @staticmethod
    def _render_search_partial(progress: dict, limit: int = 5) -> str:
        """把搜索的增量快照渲染成人读的部分结果（title — url，最多 ``limit`` 条）。"""
        results = list(progress.get("results") or [])
        lines: list[str] = []
        for item in results[: max(0, int(limit))]:
            title = str(getattr(item, "title", "") or "").strip() or "(无标题)"
            url = str(getattr(item, "url", "") or "").strip()
            lines.append(f"- {title} {url}".rstrip())
        return "\n".join(lines)

    @staticmethod
    def _is_video_artifact(path: str) -> bool:
        """按扩展名判断媒体类型（``_emit_media`` 用 "video" 发的是这几种）。"""
        return str(path).lower().endswith(BrowserLLMPlugin._VIDEO_SUFFIXES)

    @classmethod
    def _media_component(cls, path: str):
        """媒体路径 -> 消息链组件（视频走 ``Video``，其余走 ``Image``）。

        为什么按扩展名而不是记 kind：``collect_artifact`` 只存路径字符串
        （``core/delivery.py`` 被 AC-35 钉死、不能加 kind 通道），而下载路径的
        扩展名由 ``_download_media`` 按 media_class 决定，可可靠还原。
        """
        if cls._is_video_artifact(path):
            return Video.fromFileSystem(path)
        return Image.fromFileSystem(path)

    async def _deliver_terminal(
        self,
        job: BackgroundJob,
        status: str,
        text: str,
        reason: str,
        elapsed_ms: int,
        artifacts: list[str],
        partial: str = "",
    ) -> tuple[bool, str]:
        """渲染终态消息并主动投递到该会话；媒体只在成功态随消息一起发。

        Returns:
            tuple[bool, str]: (是否投递成功, 消息首行)；首行由调用方再打一条
            info 便于按 ``【后台任务…】`` 对账（F3）。
        """
        body, truncated, omitted = truncate_result(text, int(job.max_chars))
        # 媒体只在成功态附：任务没跑完时半成品只会误导（口径裁决）。
        attachments = [p for p in artifacts] if status == STATUS_OK else []
        existing = [p for p in attachments if os.path.isfile(p)]
        missing = len(attachments) - len(existing)
        message = render_terminal_text(
            task_id=job.task_id,
            tool=job.tool,
            status=status,
            text=body,
            reason=reason,
            elapsed_ms=elapsed_ms,
            budget_s=float(job.budget_s),
            truncated=truncated,
            omitted=omitted,
            artifact_count=len(existing),
            missing_files=missing,
            partial=truncate_result(partial, int(job.max_chars))[0],
        )
        chain: list = [Plain(message)]
        for path in existing:
            try:
                chain.append(self._media_component(path))
            except Exception as e:  # noqa: BLE001 — 单个坏附件不影响文本投递
                logger.warning(f"[{self.metadata_name}] 后台任务媒体附件构造失败: {e}")
        first_line = message.splitlines()[0] if message else ""
        try:
            sent = await self.context.send_message(job.umo, MessageChain(chain=chain))
            return bool(sent), first_line
        except Exception as e:  # noqa: BLE001 — 投递失败只记录，不能让任务悬挂
            logger.error(
                f"[{self.metadata_name}] 后台任务结果投递失败"
                f" task_id={job.task_id}: {e}"
            )
            return False, first_line

    async def _cancel_background_tasks(self) -> None:
        """取消全部在跑的后台任务（插件终止时调用，协作式取消 + 有限等待）。"""
        tag = f"[{self.metadata_name}]"
        running = self._background.running()
        if not running:
            return
        logger.info(
            f"{tag} 取消 {len(running)} 个在跑的后台任务（插件终止）: "
            f"{[job.task_id for job in running]}"
        )
        try:
            await self._background.cancel_all(
                timeout=float(_BACKGROUND_TERMINATE_TIMEOUT)
            )
        except Exception as e:  # noqa: BLE001 — 取消失败也要让 terminate 继续
            logger.warning(f"{tag} 后台任务取消异常: {e}")

    # ================================================================
    # 浏览器子代理入口（工具化子代理：browse_web）
    # ================================================================

    @filter.llm_tool(name="browse_web")
    async def browse_web(self, event: AstrMessageEvent, input: str = "") -> str:
        """委托浏览器子代理完成网页浏览任务（打开网页、页面交互、提取内容或截图）。

        Args:
            input(string): 给浏览器子代理的任务描述，说明要打开什么网址、查找什么内容、是否需要点击/滚动/填表等交互。可选：input 开头可加感知模式前缀 `perception=text|text_image|image`（大小写不敏感，后跟空格或换行再接任务描述），本次任务按此前缀覆盖全局/规则配置。

        感知模式优先级（v1.3.0）：input 前缀显式指定 > perception_rules
        会话规则命中（UMO 子串匹配）> page_perception 全局配置；解析结果
        以 debug 日志记录（可观测）。

        注意：本入口不持 per-umo 锁（asyncio.Lock 不可重入——若在此持锁，
        子 Agent 工具 browse_* 内部再 acquire 同一锁会死锁）。串行化由
        子 Agent 工具内部持有锁完成（同一会话的浏览器操作被串行化）。

        后台模式（v1.5.0 新增，配置 tools.background_mode）：开启后本次调用
        立刻返回「已受理 + task_id」，真正的浏览在后台执行，结果**单独作为
        一条消息**发到本会话，不会出现在这次返回里。
        """
        try:
            # 后台模式热更新（v1.5.0，F1）：入口第一行刷新配置——否则 Dashboard
            # 把 background_mode 改回 off 要"下一次调用"才生效，而"关不掉"比
            # "开不了"更糟。其余配置项（黑名单/截图/内网拦截）同样在此热更新。
            self._refresh_config()
            logger.debug(f"[{self.metadata_name}] browse_web 进入，input={input!r}")
            # 顺带同步识图 provider 下拉（面板每次请求实时读取内存 schema，
            # 此入口被主 LLM 高频调用，可保持面板选项跟随运行时 provider 变化）。
            self._sync_vision_provider_options()
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"
            # 感知模式精细化（v1.3.0）：解析 input 前缀（显式参数）并按下述
            # 优先级取本次模式：显式参数 > perception_rules 会话规则命中 >
            # 全局 page_perception；按本次模式动态构造子代理指令，不依赖
            # 预构建的 self._browser_instruction（仅作全局默认兜底）。
            explicit_mode, task_input = self._parse_perception_prefix(input)
            # 内容安全（v1.3.1）：任务描述命中禁词直接拒绝——任务文本本身
            # 可携带违禁词（纵深防御，下游 browse_open/search 的 URL/query
            # 检查为兜底，入口先拦一道）。
            banned = self._check_banned(task_input)
            if banned:
                return f"【拒绝】任务描述包含违禁内容：{banned}"
            perception_mode = self._resolve_perception_mode(event, explicit_mode)
            logger.debug(
                f"[{self.metadata_name}] browse_web 感知模式={perception_mode}"
                f"（显式={explicit_mode or '无'}）"
            )
            # 后台分支的**唯一合法位置**（v1.5.0，spec §2.2）：必须排在热更新/
            # 会话白黑名单/感知前缀解析/禁词之后，否则"被拒绝"会变成一条异步
            # 失败消息，v1.4.0 的同步拒绝语义就回退了。
            if self._background_enabled_for(_BROWSE_WEB_TOOL_NAME):
                return await self._submit_background(
                    event=event,
                    tool_name=_BROWSE_WEB_TOOL_NAME,
                    # browse_web 没有可增量获取的部分结果（子代理只在终局返回
                    # completion_text），progress 收下但不写。
                    runner=lambda _progress: self._browse_web_run(
                        event, task_input, perception_mode
                    ),
                    task_text=task_input,
                )
            return await self._browse_web_run(event, task_input, perception_mode)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.metadata_name}] browse_web 执行异常: {e}")
            return f"【错误】浏览器子代理执行失败：{e}"

    async def _browse_web_run(
        self,
        event: AstrMessageEvent,
        task_input: str,
        perception_mode: str,
    ) -> str:
        """browse_web 的执行体（provider 解析 + 子 Agent）；前台与后台共用。

        provider 解析放在这里（而不是入口）：后台模式下拿不到模型应表现为
        一条**异步失败终态消息**，而不是「被告知不可用却什么都没发生」。
        ``event`` 只被读取（子工具取 umo / 加锁 / 发媒体由 sink 截流）。
        """
        instruction = self._build_subagent_instruction(perception_mode)
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=self._umo_of(event)
            )
        except Exception as e:  # noqa: BLE001 — ProviderNotFoundError 等
            logger.warning(f"[{self.metadata_name}] 获取当前对话 Provider 失败: {e}")
            return "【错误】无法确定当前对话模型，浏览器子代理不可用。"
        if not provider_id:
            return "【错误】无法确定当前对话模型，浏览器子代理不可用。"
        logger.debug(f"[{self.metadata_name}] browse_web prov_id={provider_id!r}")
        resp = await self.context.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=task_input or "请根据用户请求浏览网页并返回结果",
            system_prompt=instruction,
            tools=self._browser_toolset,
            max_steps=self.agent_max_steps,
            tool_call_timeout=self.agent_tool_timeout,
        )
        text = getattr(resp, "completion_text", "") or ""
        logger.debug(
            f"[{self.metadata_name}] browse_web tool_loop_agent 返回，len={len(text)}"
        )
        return text or "（浏览器子代理未返回内容）"

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
        perception: str = "",
    ) -> str:
        """渲染本地 HTML 页面并返回渲染结果的视觉描述（本地页面预览工具，面向子代理）。

        用于子代理在无视觉模型时「查看」渲染后的本地 HTML 页面：以无头浏览器真实
        渲染（含 CSS/JS 执行），截图后调用视觉模型（如 mimo-v2.5）生成中文视觉描述
        （页面结构/主要文字/样式渲染异常）；视觉模型不可用时自动降级为页面文本提取，
        保证工具不空转。本工具不经过浏览会话管理器，每次渲染使用独立页面用完即关，
        不影响既有 browse_* 浏览会话状态。

        感知模式（v1.3.0 补强）：可选 perception 参数控制本次感知方式，覆盖全局
        page_perception——text 仅页面文本提取（跳过截图与识图调用，全文本子代理读
        文档/报错场景零识图开销、不落盘截图）；text_image 文本 + 截图识图（默认
        行为）；image 以截图识图为主、页面文本为辅。缺省或非法值回落全局
        page_perception（再回落 text_image），与 browse_web 非法前缀行为一致。
        子代理无独立 UMO（继承调用方会话），perception_rules 按 UMO 子串无法区分，
        故本工具不套用会话规则，感知控制依赖此显式参数。

        Args:
            path(string): 本地 HTML 文件的绝对路径。仅允许 AstrBot 工作区目录
                与插件 data 目录下的 .html/.htm 文件，其余路径一律拒绝。
            full_page(boolean): 是否截取整页长截图（默认 false，仅截首屏视口）。可省略
            wait_ms(number): 页面加载后等待 JS 渲染的毫秒数（默认 500）。可省略
            perception(string): 本次感知模式：text / text_image / image（大小写
                不敏感）。可省略：缺省或非法值回落全局 page_perception（再回落
                text_image）。text 模式跳过截图与识图调用，仅返回页面文本提取结果。

        后台模式（v1.5.0）：tools.background_mode=all 时本工具也后台化——立刻
        返回「已受理 + task_id」，渲染结果单独作为一条消息发到本会话。
        """
        try:
            # 后台模式热更新（v1.5.0）：入口第一行就刷新配置——否则 Dashboard
            # 把 background_mode 改回 off 要"下一次调用"才生效，而"关不掉"
            # 比"开不了"更糟（F1）。
            self._refresh_config()
            # 会话权限：与既有浏览工具一致，过白/黑名单（默认配置为空即放行）。
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"

            # 0. 感知模式解析（v1.3.0 补强）：显式 perception 参数 > 全局
            # page_perception；非法显式值记录警告并回落全局。子代理无独立
            # UMO（继承调用方会话），不套用 perception_rules 会话规则，
            # 感知控制靠工具显式参数（见 _resolve_local_page_perception）。
            perception_mode = self._resolve_local_page_perception(perception)
            logger.debug(
                f"[{self.metadata_name}] browse_local_page 感知模式={perception_mode}"
                f"（显式={str(perception or '').strip() or '无'}）"
            )

            # 1. 参数与路径安全校验（白名单 + 防路径穿越，越权路径直接拒绝）。
            # 四道校验必须在**提交后台任务之前**（spec §2.3）：路径非法恰恰是
            # 主模型的自纠输入，不能让它先"已受理"再收到一条异步失败消息。
            target, err = self._resolve_local_page_target(path)
            if err:
                return err

            # 2. 后台分支的唯一合法位置（v1.5.0，spec §2.3）：四道校验之后、
            # 持 per-UMO 本地锁之前。锁仍由渲染段自己持有（跨工具并发允许：
            # 后台 browse_web 与前台本地渲染各用各的页面与锁）。
            if self._background_enabled_for(_LOCAL_PAGE_TOOL_NAME):
                return await self._submit_background(
                    event=event,
                    tool_name=_LOCAL_PAGE_TOOL_NAME,
                    runner=lambda progress: self._local_page_render(
                        event, target, full_page, wait_ms, perception_mode, progress
                    ),
                )
            return await self._local_page_render(
                event, target, full_page, wait_ms, perception_mode, {}
            )
        except Exception as e:  # noqa: BLE001 — LLM 工具必须容错
            logger.warning(f"[{METADATA_NAME}] browse_local_page 失败: {e}")
            return f"【错误】本地页面渲染预览失败：{e}"

    def _resolve_local_page_target(self, path: str) -> tuple[Path | None, str]:
        """本地页面的参数/路径前置校验（非空 → resolve → 白名单 → 存在+扩展名）。

        Returns:
            tuple[Path | None, str]: (已 resolve 的目标路径, 错误文案)；成功时
            错误文案为空串、路径非 None。
        """
        raw = str(path or "").strip()
        if not raw:
            return None, (
                "【错误】path 不能为空：请传入要渲染查看的本地 HTML 文件绝对路径。"
            )
        if raw.lower().startswith("file://"):
            raw = raw[len("file://") :]
        try:
            target = Path(raw).expanduser().resolve()
        except Exception as e:  # noqa: BLE001
            return None, f"【错误】路径解析失败：{e}"
        ok, reason = self._check_local_page_path(target)
        if not ok:
            return None, f"【拒绝】{reason}"
        if not target.is_file():
            return None, f"【错误】文件不存在或不是普通文件：{target}"
        if target.suffix.lower() not in _LOCAL_PAGE_ALLOWED_EXTS:
            return None, f"【拒绝】仅支持 .html/.htm 文件：{target.name}"
        return target, ""

    async def _local_page_render(
        self,
        event: AstrMessageEvent,
        target: Path,
        full_page: bool,
        wait_ms: int,
        perception_mode: str,
        progress: dict | None = None,
    ) -> str:
        """渲染段执行体（持 per-UMO 本地锁）：new_page → goto → 提取 → 截图 → 识图 → close。

        ``progress``（可选 dict）：中断前的半成品放这里——拿到页面文本就写
        ``progress["text"]``，后台任务超时/失败时据此发「已得部分结果」
        （设计 §2.7）。前台传空 dict / None，行为与 v1.4.0 完全一致。
        """
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
                # 信息提取（text / text_image / image 模式都需要；
                # image 模式中页面文本作为识图描述的辅助内容）。
                info = await self.extractor.extract_page_info(page)
                text = await self.extractor.extract_text(
                    page, max_chars=int(self.max_chars)
                )
                # 已得部分结果（v1.5.0）：后台任务被预算掐断时，这段文本是
                # 唯一能回给用户的东西；前台 progress 为空 dict，无副作用。
                if progress is not None and text:
                    progress["text"] = text

                # 4. 感知模式 text：仅文本提取，跳过截图与识图调用
                # （全文本子代理读文档/报错场景零识图开销、不落盘截图）。
                if perception_mode == "text":
                    if not text:
                        return f"【错误】页面渲染成功但未提取到文本内容：{target}"
                    banned = self._check_banned(text)
                    if banned:
                        return (
                            f"【拒绝】页面内容包含违禁内容：{banned}，已拒绝输出。"
                        )
                    return self._format_local_page_text_result(
                        target,
                        info,
                        text,
                        note="感知模式 text：仅文本提取，未截图识图",
                    )

                # 5. 截图（text_image / image 模式；保存到插件
                # data/screenshots/，可作交付附件路径）。
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                umo_hash = hashlib.md5(
                    self._umo_of(event).encode("utf-8")
                ).hexdigest()[:8]
                path_hash = hashlib.md5(str(target).encode("utf-8")).hexdigest()[:8]
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

                # 6. 视觉描述（mimo-v2.5 等，识图缓存正常生效）：成功返回
                # 描述；失败降级文本，保证不空转。
                desc = ""
                if screenshot_ok:
                    desc = await self._describe_screenshot(
                        save_path, event, page_url=target.as_uri()
                    )
                if desc:
                    banned = self._check_banned(desc)
                    if banned:
                        return (
                            f"【拒绝】页面识图结果包含违禁内容：{banned}，"
                            "已拒绝输出。"
                        )
                    if perception_mode == "image" and text:
                        # image 模式：以截图识图为主，页面文本为辅（辅助
                        # 文本同样过禁词，命中则不附带，避免泄露违禁内容）。
                        if not self._check_banned(text):
                            return (
                                f"【本地页面预览·识图模式】{target}\n"
                                f"标题: {info['title']}\n"
                                f"截图: {save_path}\n"
                                f"渲染视觉描述：\n{desc}\n"
                                f"页面文本（辅助）：\n{text}"
                            )
                    return (
                        f"【本地页面预览】{target}\n"
                        f"标题: {info['title']}\n"
                        f"截图: {save_path}\n"
                        f"渲染视觉描述：\n{desc}"
                    )

                # 7. 降级路径：视觉模型不可用/截图失败 → 文本提取，保证不空转。
                if text:
                    banned = self._check_banned(text)
                    if banned:
                        return (
                            f"【拒绝】页面内容包含违禁内容：{banned}，已拒绝输出。"
                        )
                    return self._format_local_page_text_result(
                        target,
                        info,
                        text,
                        note="视觉模型不可用或截图失败，已降级为文本提取",
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

    @staticmethod
    def _format_local_page_text_result(
        target: Path, info: dict, text: str, note: str
    ) -> str:
        """格式化本地页面文本提取结果（text 感知模式与降级路径共用）。

        text 感知模式（跳过截图识图）与视觉不可用/截图失败的降级路径都
        复用本方法，保证两种场景输出结构一致、不重复实现。

        Args:
            target: 渲染的本地 HTML 文件路径。
            info: extract_page_info 返回的页面信息（含 title）。
            text: 提取到的页面文本。
            note: 模式说明（如「感知模式 text：仅文本提取，未截图识图」
                或「视觉模型不可用或截图失败，已降级为文本提取」）。

        Returns:
            str: 【本地页面预览·文本模式】输出文本。
        """
        return (
            f"【本地页面预览·文本模式】{target}\n"
            f"标题: {info['title']}\n"
            f"（{note}）\n"
            f"页面文本：\n{text}"
        )

    def _check_local_page_path(self, target: Path) -> tuple[bool, str]:
        """本地页面白名单校验：仅允许工作区与插件 data 目录下的路径。

        target 已 resolve()（消解 .. 与符号链接），此处用 is_relative_to
        做前缀白名单判断，杜绝路径穿越与软链逃逸。

        Returns:
            tuple[bool, str]: (是否允许, 拒绝原因或空串)。
        """
        roots = (
            getattr(self, "_local_page_allowed_roots", None)
            or _resolve_local_page_roots()
        )
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

    def _parse_perception_prefix(self, raw_input: str) -> tuple[str | None, str]:
        """解析 browse_web input 开头的可选感知模式前缀（v1.3.0）。

        格式：`perception=text|text_image|image`（大小写不敏感），后跟
        空格或换行再接任务描述。返回值：
        - 合法前缀 → (显式模式, 剔除前缀后的任务描述)；
        - 非法值（如 perception=foo / perception= 等号后空值）→ 记录
          警告、剔除该前缀、返回 (None, 剩余描述)——回落「规则 > 全局」
          默认链路，不把前缀当任务内容；
        - 无前缀 → (None, 原输入)。

        Args:
            raw_input: browse_web 的 input 参数原文。

        Returns:
            tuple[str | None, str]: (显式感知模式或 None, 任务描述)。
        """
        raw = raw_input or ""
        m = _PERCEPTION_PREFIX_RE.match(raw)
        if m:
            return m.group(1).lower(), raw[m.end() :].lstrip()
        m = _INVALID_PERCEPTION_PREFIX_RE.match(raw)
        if m:
            logger.warning(
                f"[{self.metadata_name}] browse_web 感知模式前缀非法，已忽略"
                f"并回落默认链路: {raw[: m.end()]!r}"
                f"（支持 perception=text / text_image / image）"
            )
            return None, raw[m.end() :].lstrip()
        return None, raw

    def _resolve_perception_mode(
        self, event: AstrMessageEvent, explicit: str | None = None
    ) -> str:
        """按优先级解析本次浏览任务的页面感知模式（v1.3.0）。

        优先级：显式参数 > perception_rules 会话规则命中（第一条，按
        UMO 子串匹配、不区分大小写）> 全局 page_perception。非法规则
        条目（缺 match / perception 不在可选集）记录警告并跳过。

        Args:
            event: 消息事件（取会话 UMO 匹配规则）。
            explicit: browse_web 前缀解析出的显式模式，非 None 时直接返回。

        Returns:
            str: 规范化感知模式（text / text_image / image），保证
            返回值一定在 _PERCEPTION_MODES 内。
        """
        if explicit:
            return explicit
        umo_lower = (self._umo_of(event) or "").lower()
        for rule in self.perception_rules or []:
            if not isinstance(rule, dict):
                logger.warning(
                    f"[{self.metadata_name}] perception_rules 条目非法，已跳过: {rule!r}"
                )
                continue
            match = str(rule.get("match") or "").strip()
            perception = str(rule.get("perception") or "").strip().lower()
            if not match or perception not in _PERCEPTION_MODES:
                logger.warning(
                    f"[{self.metadata_name}] perception_rules 条目非法，已跳过: {rule!r}"
                )
                continue
            if match.lower() in umo_lower:
                return perception
        mode = (self.page_perception or "").strip().lower()
        return mode if mode in _PERCEPTION_MODES else "text_image"

    def _resolve_local_page_perception(self, explicit: str | None = None) -> str:
        """解析 browse_local_page 本次感知模式（v1.3.0 补强）。

        优先级：显式 perception 参数 > 全局 page_perception（再回落
        text_image）。非法显式值（不在 text/text_image/image 可选集）
        记录警告并回落全局——与 browse_web 非法前缀行为一致。子代理无
        独立 UMO（继承调用方会话），perception_rules 按 UMO 子串无法
        区分，故本地页面预览不套用会话规则，感知控制仅靠工具显式参数
        （persona 指令驱动）。

        Args:
            explicit: browse_local_page 的 perception 参数原文，
                空串/None 表示未显式指定。

        Returns:
            str: 规范化感知模式（text / text_image / image），保证
                返回值一定在 _PERCEPTION_MODES 内。
        """
        raw = (explicit or "").strip().lower()
        if raw and raw not in _PERCEPTION_MODES:
            logger.warning(
                f"[{self.metadata_name}] browse_local_page 感知模式非法，"
                f"已回落全局 page_perception: {raw!r}"
                f"（支持 perception=text / text_image / image）"
            )
            raw = ""
        if not raw:
            mode = (self.page_perception or "").strip().lower()
            return mode if mode in _PERCEPTION_MODES else "text_image"
        return raw

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
            field for segment in umo.split(":") for field in segment.split("|") if field
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
        # 浏览器内核配置校验（v1.3.1）：非法 browser_type 在创建页面前
        # 给出可选值提示（browse_* 工具会把它透传给用户），避免
        # None.launch() 的无提示报错。
        if getattr(self, "browser", None) is not None:
            try:
                validate_browser_type(self.browser.browser_type)
            except ValueError as e:
                return None, str(e)
        page = await self.sessions.ensure_page(self._umo_of(event))
        if page is None:
            return None, "会话数已达上限或页面创建失败，请稍后再试"
        return page, ""

    def _check_banned(self, text: str) -> str | None:
        """禁词检查：命中返回禁词原文，未命中返回 None。

        v1.3.3 起受 enable_banned_filter 总开关控制：关闭时所有拦截点
        （正文/标题/搜索词/URL/识图结果等）统一放行，用于敏感词密集
        站点（论坛等）避免整页拒绝；SSRF 内网拦截、本地页路径白名单
        等安全边界不受此开关影响。
        """
        if not self.enable_banned_filter:
            return None
        if self.safety is None:
            return None
        ok, word = self.safety.check_text(text)
        return None if ok else word

    async def _page_summary(self, page) -> str:
        """提取页面基本信息与正文，生成紧凑摘要文本。

        内容安全（v1.3.1）：标题/正文命中 banned_words 时拒绝返回——
        browse_open / browse_current_page / browse_click_link / scroll 等
        全部经由此方法返回页面文本的工具统一受过滤保护，与 URL/query/
        本地页面链路的禁词检查保持一致。
        """
        info = await self.extractor.extract_page_info(page)
        text = await self.extractor.extract_text(page, max_chars=int(self.max_chars))
        text = text or "（页面无文本内容）"
        for field, value in (("标题", info.get("title") or ""), ("正文", text)):
            banned = self._check_banned(value)
            if banned:
                return f"【拒绝】页面{field}包含违禁内容：{banned}"
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
                await page.goto(url, wait_until="domcontentloaded")
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
            logger.debug(
                f"[{self.metadata_name}] browse_open 进入 url={_redact_url(url)!r}"
            )
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
                await page.goto(url, wait_until="domcontentloaded")
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
                limit = (
                    int(max_chars)
                    if max_chars and max_chars > 0
                    else int(self.max_chars)
                )
                text = await self.extractor.extract_text(page, max_chars=limit)
                if not text:
                    return "当前页面没有可提取的文本内容。"
                # 内容安全（v1.3.1）：正文命中禁词拒绝返回，与 _page_summary 一致。
                banned = self._check_banned(text)
                if banned:
                    return f"【拒绝】页面内容包含违禁内容：{banned}"
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
                limit = (
                    int(max_items)
                    if max_items and max_items > 0
                    else int(self.max_links)
                )
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
                # 健壮解析（v1.3.1）：链接可见文本可能含换行（innerText 保留
                # 换行），若按 splitlines 整段解析，编号行会被换行拆散导致
                # 无法还原 target。改为按「换行 + 编号行开头」切分条目——
                # 条目内部换行不会破坏边界（URL 不含空白，rsplit 取末段安全）。
                target = None
                for entry in re.split(r"\n(?=\[\d+\])", links or ""):
                    entry = entry.strip()
                    m = re.match(r"^\[(\d+)\]", entry)
                    if not m or int(m.group(1)) != index:
                        continue
                    parts = entry[m.end() :].rsplit(" → ", 1)
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
                await page.goto(target, wait_until="domcontentloaded")
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
                # 无会话时直接报错，不隐式创建会话（v1.3.1）：切换针对已有
                # 标签，ensure_page 的「无会话建首标签」副作用在此不适用。
                allowed, deny_reason = self._is_session_allowed(event)
                if not allowed:
                    return f"【拒绝】{deny_reason}"
                umo = self._umo_of(event)
                total = self.sessions.tab_count(umo)
                if total == 0:
                    return "【错误】当前会话没有任何标签页。"
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
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
                # 无会话时直接报错，不隐式创建会话（v1.3.1）：关闭针对已有
                # 标签，ensure_page 的「无会话建首标签」副作用在此不适用。
                allowed, deny_reason = self._is_session_allowed(event)
                if not allowed:
                    return f"【拒绝】{deny_reason}"
                umo = self._umo_of(event)
                total = self.sessions.tab_count(umo)
                if total == 0:
                    return "【错误】当前会话没有任何标签页。"
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
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
            "Enter",
            "Escape",
            "ArrowDown",
            "ArrowUp",
            "ArrowLeft",
            "ArrowRight",
            "Tab",
            "Backspace",
            "Home",
            "End",
        }
        # 大小写不敏感（v1.3.1）：LLM 可能传小写 "enter"，统一按大写
        # 归一后映射回 Playwright 要求的规范按键名。
        key_aliases = {name.upper(): name for name in allowed_keys}
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                raw_key = str(key).strip()
                canonical = key_aliases.get(raw_key.upper())
                if canonical is None:
                    return (
                        f"【错误】不支持的按键：{raw_key}。"
                        f"支持：{'/'.join(sorted(allowed_keys))}"
                    )
                await page.keyboard.press(canonical)
                return f"已按下按键 {canonical}。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_press_key 失败: {e}")
            return f"【错误】按键操作失败：{e}"

    async def _emit_media(
        self, event: AstrMessageEvent, path: str, kind: str = "image"
    ) -> None:
        """媒体发送出口（T-M0）。

        后台任务（artifact_sink 生效）只登记路径，由投递层统一发送；
        前台保持原有行为：截图/媒体立即发到当前会话。
        """
        if collect_artifact(path):
            return
        if kind == "image":
            await event.send(event.image_result(path))
            return
        from astrbot.api.message_components import Video  # noqa: PLC0415

        await event.send(event.chain_result([Video.fromFileSystem(path)]))

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
                umo_hash = hashlib.md5(self._umo_of(event).encode("utf-8")).hexdigest()[
                    :8
                ]
                save_path = str(self._screenshot_dir / f"browse_{umo_hash}_{ts}.png")
                result = await self.browser.screenshot(page, save_path)
                if not result:
                    return "【错误】截图失败。"
                # 静默模式：截图仅内部识图，不发到群聊（减少刷屏）。
                if not self.silent_mode:
                    try:
                        await self._emit_media(event, result, "image")
                    except Exception as e:  # noqa: BLE001 — 发图失败不阻塞识图
                        logger.warning(f"[{METADATA_NAME}] 发送截图失败: {e}")
                # 识图（可选）：配置了 vision_provider_id 才尝试。
                desc = await self._describe_screenshot(
                    result, event, page_url=getattr(page, "url", None)
                )
                if desc:
                    # 内容安全（v1.3.3）：识图结果与页面正文同口径过禁词
                    # （受 enable_banned_filter 总开关控制）。
                    banned = self._check_banned(desc)
                    if banned:
                        return f"【拒绝】截图识图结果包含违禁内容：{banned}"
                    prefix = (
                        "截图已识图（静默模式，未发送）"
                        if self.silent_mode
                        else "截图已发送给用户"
                    )
                    return f"{prefix}。识图结果：\n{desc}"
                prefix = (
                    "截图已识图（静默模式，未发送）"
                    if self.silent_mode
                    else "截图已发送给用户"
                )
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
                        await page.get_by_text(text, exact=False).first.click(
                            timeout=10000
                        )
                        clicked = True
                    except Exception as e:  # noqa: BLE001 — 未找到或不可点，降级
                        logger.debug(
                            f"get_by_text 点击失败，降级 locator('text='): {e}"
                        )
                if not clicked:
                    try:
                        await page.locator(f"text={text}").first.click(timeout=10000)
                        clicked = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            f"[{METADATA_NAME}] 未找到可点击元素（文本: {text}）: {e}"
                        )
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
                logger.info(
                    f"[{METADATA_NAME}] 已用 native setter 设置滑块值: {target}"
                )
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
                        await page.get_by_text(text, exact=False).first.hover(
                            timeout=5000
                        )
                        hovered = True
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            f"get_by_text 悬停失败，降级 locator('text='): {e}"
                        )
                if not hovered:
                    try:
                        await page.locator(f"text={text}").first.hover()
                        hovered = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            f"[{METADATA_NAME}] 未找到可悬停元素（文本: {text}）: {e}"
                        )
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
                # Playwright async API：is_checked() 是协程，必须 await；
                # 漏 await 时 bool(coroutine) 恒为 True，会把「目标状态与
                # 当前状态一致」误判为不一致而误点击（切换语义反转）。
                is_checked = bool(await loc.is_checked())
                if is_checked != bool(checked):
                    await loc.click()
                logger.info(
                    f"[{METADATA_NAME}] 已设置复选状态: {selector} -> {checked}"
                )
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
                vw, vh = (
                    int(viewport.get("width", 1280)),
                    int(viewport.get("height", 800)),
                )
                cx = max(0, min(int(x), vw))
                cy = max(0, min(int(y), vh))
                cw = max(1, min(int(width), vw - cx))
                ch = max(1, min(int(height), vh - cy))
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                umo_hash = hashlib.md5(self._umo_of(event).encode("utf-8")).hexdigest()[
                    :8
                ]
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
                desc = await self._describe_screenshot(
                    save_path, event, page_url=getattr(page, "url", None)
                )
                if desc:
                    # 内容安全（v1.3.3）：识图结果与页面正文同口径过禁词
                    # （受 enable_banned_filter 总开关控制）。
                    banned = self._check_banned(desc)
                    if banned:
                        return f"【拒绝】裁剪区域识图结果包含违禁内容：{banned}"
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
                    i
                    for i in (raw_items or [])
                    if isinstance(i, dict)
                    and isinstance(i.get("url"), str)
                    and i["url"].strip()
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
                    hint = (
                        f"（跳过 {skipped} 个无法分类/流媒体资源）" if skipped else ""
                    )
                    return f"当前页面没有嗅探到{'目标类型' if mtype != 'all' else ''}图片/视频资源{hint}。"
                # 下载最多 limit 个到 data/media/ 并发送。遍历 wanted 全部，
                # 被安全拦截/下载失败的继续消费下一个，直到凑够 limit 个
                # 成功项——避免被攻陷页面塞满内网 URL 挤占合法资源名额。
                downloaded = []
                blocked = 0
                for u, cls in wanted:
                    if len(downloaded) >= limit:
                        break
                    # SSRF 防护：URL 来自页面内容（可能被攻陷），下载前必须
                    # 校验协议白名单 + 内网地址拦截，未通过则跳过并计数。
                    ok, reason = await self.safety.acheck_url(u)
                    if not ok:
                        blocked += 1
                        logger.warning(
                            f"[{METADATA_NAME}] 媒体 URL 未通过安全校验，跳过: {_redact_url(u)} ({reason})"
                        )
                        continue
                    try:
                        path = await self._download_media(u, len(downloaded) + 1, cls)
                        if not path:
                            continue
                        downloaded.append(path)
                        await self._emit_media(
                            event, path, "image" if cls == "image" else "video"
                        )
                    except Exception as e:  # noqa: BLE001 — 单个失败继续
                        logger.warning(
                            f"[{METADATA_NAME}] 下载/发送媒体失败 {_redact_url(u)}: {e}"
                        )
                if blocked:
                    logger.info(
                        f"[{METADATA_NAME}] 媒体嗅探跳过 {blocked} 个未通过安全校验的 URL"
                    )
                if not downloaded:
                    hint = (
                        f"。另有 {blocked} 个 URL 因安全校验被拦截" if blocked else ""
                    )
                    return f"媒体下载失败，请检查网络或资源可访问性{hint}。"
                lines = [f"已下载并发送 {len(downloaded)} 个媒体："]
                lines.extend(f"- {p}" for p in downloaded)
                if blocked:
                    lines.append(f"（另有 {blocked} 个 URL 被安全拦截，未下载）")
                return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_sniff_media 失败: {e}")
            return f"【错误】媒体嗅探失败：{e}"

    async def _download_media(
        self, url: str, index: int, media_class: str = "image"
    ) -> str:
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
        if ext not in (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".mp4",
            ".webm",
            ".mov",
        ):
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

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
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
                                    f"[{METADATA_NAME}] 媒体重定向缺少 Location，终止: {_redact_url(current_url)}"
                                )
                                return ""
                            next_url = urljoin(current_url, location)
                            ok, reason = await self.safety.acheck_url(next_url)
                            if not ok:
                                logger.warning(
                                    f"[{METADATA_NAME}] 媒体重定向目标未通过安全校验，"
                                    f"终止下载: {_redact_url(next_url)} ({reason})"
                                )
                                return ""
                            current_url = next_url
                            continue
                        if resp.status != 200:
                            logger.warning(
                                f"[{METADATA_NAME}] 媒体下载非 200: {_redact_url(current_url)} -> {resp.status}"
                            )
                            return ""
                        # Content-Length 预检：超限直接跳过。
                        length = resp.content_length
                        if length is not None and length > max_bytes:
                            logger.warning(
                                f"[{METADATA_NAME}] 媒体超 {max_bytes // 1024 // 1024}MB 上限，跳过: {_redact_url(current_url)} ({length} bytes)"
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
                                            f"[{METADATA_NAME}] 媒体流式累计超 {max_bytes // 1024 // 1024}MB 上限，中止: {_redact_url(current_url)}"
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
                        f"[{METADATA_NAME}] 媒体重定向超过 {max_redirects} 跳，终止: {_redact_url(url)}"
                    )
                    return ""
            # 流式写入成功：临时文件改名成正式路径。
            Path(tmp_path).rename(save_path)
            return save_path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] 媒体下载失败 {_redact_url(url)}: {e}")
            return ""

    @staticmethod
    def _normalize_vision_cache_url(url: str) -> str:
        """规范化识图缓存 URL：去 fragment、去尾斜杠（保留根路径 '/'）。

        仅按需求规格处理这两类差异；大小写/默认端口等不做归一（实现
        简单优先，避免误合并不同内容页面）。
        """
        url = (url or "").strip()
        if not url:
            return ""
        if "#" in url:
            url = url.split("#", 1)[0]
        while len(url) > 1 and url.endswith("/"):
            url = url[:-1]
        return url

    def _vision_cache_key(
        self, page_url: str | None, event: AstrMessageEvent | None
    ) -> tuple[str, str] | None:
        """构造识图缓存键：(umo, 规范化URL)；缺 URL 或会话时返回 None。

        None 表示本次调用不参与缓存（不读不写），例如调用方未提供页面
        URL、或事件无法确定会话（单测/非会话场景）。
        """
        url = self._normalize_vision_cache_url(page_url or "")
        if not url:
            return None
        umo = self._umo_of(event) if event is not None else ""
        if not umo:
            return None
        return (umo, url)

    def _vision_cache_put(self, key: tuple[str, str] | None, text: str) -> None:
        """写入识图缓存（附带过期项清理，防长期运行内存膨胀）。

        超过 _VISION_CACHE_MAX_ENTRIES 条时惰性清理全部过期项；条目
        数量仍超限则保留（下次写入继续清理），不引入定时任务。
        """
        if key is None or not text:
            return
        self._vision_cache[key] = (time.time(), text)
        if len(self._vision_cache) <= _VISION_CACHE_MAX_ENTRIES:
            return
        now = time.time()
        ttl = int(getattr(self, "vision_cache_ttl", 0) or 0)
        if ttl <= 0:
            return
        expired = [k for k, (ts, _) in self._vision_cache.items() if now - ts > ttl]
        for k in expired:
            del self._vision_cache[k]

    async def _describe_screenshot(
        self,
        image_path: str,
        event: AstrMessageEvent | None = None,
        page_url: str | None = None,
    ) -> str:
        """用多模态模型描述截图（配置了 vision_provider_id 才执行）。

        行为：配置的识图 provider 未加载/失效时自动回退当前会话聊天
        Provider（带警告日志）；未配置或全部不可用时返回空串（不抛异常）。
        模型返回拒识文案（纯文本模型不支持图片，如 "[Unsupported Image]"）
        时记警告日志并返回明确提示文案，不把拒识文本误当视觉描述。
        双通道降级：contexts（ImageURLPart）失败后回退 image_urls。
        识图短时缓存（v1.3.0）：提供 page_url 且 vision_cache_ttl>0 时，
        同 URL（去 fragment/尾斜杠规范化）+ 同会话在 TTL 内重复识图直接
        复用上次结果（返回文本带 [缓存] 前缀，另记 debug 日志）；缓存按
        URL+会话隔离，terminate 时整体清空。

        Args:
            image_path: 截图本地路径。
            event: 消息事件（失效回退时确定会话），可为 None。
            page_url: 截图对应页面的 URL（缓存键来源），可为 None 表示
                不参与缓存。

        Returns:
            str: 识图描述文本；未配置或调用失败返回空串；拒识时返回提示
            文案（均不抛异常）。
        """
        # 识图短时缓存命中检查（TTL=0 或缺少 URL/会话键时不启用）。
        ttl = int(getattr(self, "vision_cache_ttl", 0) or 0)
        cache_key = self._vision_cache_key(page_url, event) if ttl > 0 else None
        if cache_key is not None:
            hit = self._vision_cache.get(cache_key)
            if hit is not None:
                ts, cached_text = hit
                if time.time() - ts <= ttl:
                    logger.debug(
                        f"[{self.metadata_name}] 识图缓存命中（TTL={ttl}s）: {page_url}"
                    )
                    return f"[缓存] {cached_text}"
                del self._vision_cache[cache_key]
        provider_id = await self._resolve_vision_provider_id(event)
        if not provider_id:
            return ""
        # 识图提示词：优先用用户配置的 vision_prompt（实时读取），空则回退
        # 默认提示词。
        prompt = self._live_vision_prompt() or (
            "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。"
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
                    contexts=[
                        UserMessageSegment(content=[img, VisionTextPart(text=prompt)])
                    ],
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
                self._vision_cache_put(cache_key, text.strip())
                return text.strip()
        except Exception as e:  # noqa: BLE001 — contexts 方式失败，降级 image_urls
            logger.debug(
                f"[{self.metadata_name}] 识图 contexts 方式失败，降级 image_urls: {e}"
            )
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
            self._vision_cache_put(cache_key, text)
            return text
        except Exception as e:  # noqa: BLE001 — 识图失败必须降级不抛异常
            logger.warning(f"[{self.metadata_name}] 识图失败: {e}")
            return ""

    # ================================================================
    # LLM 工具：browser_web_search（v4：搜索 API provider 池）
    # ================================================================

    # ---- 配置 -> provider 池 ----

    def _parse_search_providers(self) -> list:
        """把 ``search.providers`` 解析成 ProviderEntry（不建 HTTP 实现、不发请求）。

        用于"池内是否有可用 provider"这类**只读判断**（initialize 的自动闸门、
        handler 兜底）；真正调用时才走 ``_build_search_entries()`` 实例化。
        """
        return parse_provider_configs(self.search_providers)

    def _search_usable_entries(self) -> list:
        """池内可用条目 = 模板已知 + 启用 + 有 key + 无配置错误。"""
        return [entry for entry in self._parse_search_providers() if is_entry_usable(entry)]

    def _search_available(self) -> bool:
        """工具是否应当可用：总开关开 + 池内至少一个可用 provider。

        两条都满足才会 ``activate`` 工具；否则自动闸门关掉（防"开着开关但没
        填 key"时报一堆 missing_key 给模型看）。
        """
        return bool(self.search_enabled) and bool(self._search_usable_entries())

    def _search_secret_values(self) -> list:
        """运行期脱敏表：池内所有非空 api_key（只进内存，不落日志/返回体）。"""
        return [
            str(entry.api_key)
            for entry in self._parse_search_providers()
            if entry.api_key
        ]

    def _search_url_guard(self):
        """结果 URL 的**字面**校验回调（不做 DNS）。

        50 条结果各做一次 DNS 会吃掉整个 ``total_timeout``，因此结果链接只做
        协议白名单 + 本机名 + IP 字面量判定；真正出网抓正文前另走
        ``SafetyFilter.acheck_url``（含 DNS、逐跳重定向）。
        """
        block_internal = bool(self.block_internal_ip)

        def _guard(url: str) -> bool:
            ok, _reason = check_url_literal(url, block_internal_ip=block_internal)
            return ok

        return _guard

    def _build_search_entries(self) -> list:
        """构造 provider 池（含 HTTP 实现）。测试可 monkeypatch 本方法注入假源。"""
        return build_provider_entries(
            self.search_providers,
            region=self.search_region,
            safesearch=self.search_safesearch,
            proxy=self.search_proxy,
            url_guard=self._search_url_guard(),
        )

    def _search_signature(self, entries: list) -> tuple:
        """池签名：provider 段变化才重建 SearchService（冷却/缓存随实例走）。"""
        return tuple(
            (
                entry.name,
                entry.template,
                entry.enabled,
                entry.weight,
                entry.timeout_s,
                entry.limit,
                entry.config_error,
                entry.api_key,
            )
            for entry in entries
        )

    def _get_search_service(self, entries: list) -> SearchService:
        """取（必要时重建）搜索服务实例：并发/轮数/超时/冷却/缓存一次注入。"""
        signature = self._search_signature(entries)
        if (
            self._search_service is not None
            and self._search_service_signature == signature
        ):
            return self._search_service
        service = SearchService(
            entries=entries,
            timeout_s=float(self.search_provider_timeout),
            max_results=int(self.search_max_results),
            rate_limit_cooldown_s=float(self.search_cooldown_seconds),
            secrets=[e.api_key for e in entries if e.api_key],
            cache_ttl_s=float(self.search_cache_ttl),
        )
        self._search_service = service
        self._search_service_signature = signature
        return service

    # ---- 工具可见性（D4：装饰器常驻 + active 对齐 + handler 兜底） ----

    @staticmethod
    def _is_own_module_path(path: str) -> bool:
        """模块路径是否属于本插件（``metadata.module_path`` 或本模块 ``__name__``）。"""
        if not path:
            return False
        return path == __name__ or METADATA_NAME in path

    def _is_own_search_tool(self, func) -> bool:
        """该 FuncTool 是否属于本插件。

        判定顺序（都要过，缺一不可——``handler_module_path`` 只在
        ``star_manager`` 载入插件时（:1282-1310）才绑定，import 期为空）：

        1. ``handler_module_path``（绑定后最准）；
        2. ``handler.__module__``（import 期唯一可用）；
        3. ``functools.partial.func.__module__``（载入后 handler 被 partial
           绑定 ``star_cls``，partial 自身没有 ``__module__``）。
        """
        if self._is_own_module_path(str(getattr(func, "handler_module_path", "") or "")):
            return True
        handler = getattr(func, "handler", None)
        if isinstance(handler, functools.partial):
            handler = handler.func
        module = str(getattr(handler, "__module__", "") or "")
        return self._is_own_module_path(module)

    def _search_tool_registry_state(self) -> tuple[bool, list]:
        """返回 (本插件是否已注册该工具, 同名"别人家"工具列表)。"""
        try:
            from astrbot.core.provider.register import llm_tools  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — 桩/早期环境没有 manager 时按"未注册"处理
            return False, []
        funcs = list(getattr(llm_tools, "func_list", []) or [])
        own = False
        clash: list = []
        for func in funcs:
            if getattr(func, "name", None) != _SEARCH_TOOL_NAME:
                continue
            if self._is_own_search_tool(func):
                own = True
            else:
                clash.append(func)
        return own, clash

    async def _reconcile_search_tool(self) -> None:
        """把工具 ``active`` 态对齐到配置（配置 = 唯一事实源）。

        机制（spec §6.2，captain 裁定 D4）：``@filter.llm_tool`` 在 import 期
        就注册（平台没有"按配置注册"的口子），因此用 ``activate_llm_tool_async``
        / ``deactivate_llm_tool_async`` 切 ``active``；请求装配期核心会剔除
        ``active=False`` 的工具，模型**根本看不到**它（不是"调用后报错"）。

        三条必须知道的副作用：
        1. ``deactivate_llm_tool_async`` 会把工具名写进**全局持久化**的
           ``inactivated_llm_tools``（跨重启保留）；
        2. 因此"配置说开、名单说关"会短暂分叉 —— 本方法是双向收敛点；
        3. 插件卸载/改名后该名字可能残留，下次装回同名工具初值是
           ``active=False``，仍由本方法在 ``initialize()`` 时纠正。
        """
        own, clash = self._search_tool_registry_state()
        if clash:
            # 同名但属别人：**不碰 active**（fail-closed，只报错）。
            logger.error(
                f"[{self.metadata_name}] {_SEARCH_TOOL_NAME} 名称冲突："
                f"{[getattr(f, 'handler_module_path', '') for f in clash]}，"
                "已跳过可见性对齐（请检查是否有其它插件注册了同名工具）"
            )
            return

        want = self._search_available()
        if not own:
            # import 期以外（桩环境/管理器不可见）：只记影子值，入口兜底负责。
            self._search_tool_active = want
            return

        activate = getattr(self.context, "activate_llm_tool_async", None)
        deactivate = getattr(self.context, "deactivate_llm_tool_async", None)
        if not callable(activate) or not callable(deactivate):
            # 兼容降级（<4.29 的 Context）：常驻可见 + handler 入口返回 disabled。
            self._search_tool_active = want
            logger.info(
                f"[{self.metadata_name}] Context 不支持 activate/deactivate_llm_tool_async，"
                f"搜索工具降级为常驻注册（入口按配置兜底，enabled={self.search_enabled}）"
            )
            return

        try:
            if want:
                await activate(_SEARCH_TOOL_NAME)
            else:
                await deactivate(_SEARCH_TOOL_NAME)
        except Exception as exc:  # noqa: BLE001 — 对齐失败不能阻断插件启动
            logger.warning(
                f"[{self.metadata_name}] 搜索工具可见性对齐失败（{type(exc).__name__}），"
                f"保持常驻注册并由入口兜底（enabled={self.search_enabled}）"
            )
            self._search_tool_active = want
            return

        self._search_tool_active = want
        if want:
            logger.info(
                f"[{self.metadata_name}] 搜索工具已启用：{_SEARCH_TOOL_NAME}"
                f"（可用 provider={[e.name for e in self._search_usable_entries()]}）"
            )
        else:
            reason = (
                "search.enabled=false"
                if not self.search_enabled
                else "池内无可用 provider（缺 api_key / 全部禁用 / 端点非法）"
            )
            logger.info(
                f"[{self.metadata_name}] search tool inactive: {_SEARCH_TOOL_NAME}"
                f"（{reason}）"
            )

    # ---- 返回体与安全过滤 ----

    def _search_payload(self, **kwargs) -> str:
        """统一渲染工具返回体（三态同一 schema，``ok`` 由 status 推导）。"""
        return render_payload(**kwargs)

    @staticmethod
    def _parse_providers_arg(raw: str) -> list | None:
        """解析工具参数 ``providers``（逗号/分号分隔）；空 ⇒ None（不过滤）。"""
        text = (raw or "").strip()
        if not text:
            return None
        names = [part.strip() for part in re.split(r"[,;，；]", text) if part.strip()]
        return names or None

    def _filter_banned_results(self, results: list) -> tuple[list, int]:
        """结果级禁词过滤：命中则**只丢该条**（不整单拒绝）。"""
        kept: list = []
        dropped = 0
        for item in results:
            banned = self._check_banned(f"{item.title}\n{item.snippet}")
            if banned:
                dropped += 1
                continue
            kept.append(item)
        for idx, item in enumerate(kept, start=1):
            item.rank = idx
        return kept, dropped

    def _make_search_fetcher(self) -> AiohttpFetcher:
        """构造正文抓取器（测试可 monkeypatch 本方法注入假 transport）。"""
        safety = self.safety
        return AiohttpFetcher(
            limits=CrawlerLimits(
                timeout_s=float(self.search_fetch_timeout),
                max_chars=int(self.search_fetch_max_chars),
                user_agent="AstrBot-BrowserLLM/1.0",
            ),
            url_guard=safety.acheck_url if safety is not None else None,
            proxy=self.search_proxy,
        )

    async def _fetch_search_results(
        self, results: list, top_n: int, *, deadline: float | None = None
    ) -> None:
        """抓取排序前 ``top_n`` 条的正文（逐条隔离，失败只改该条 ``fetched``）。

        Args:
            results: 合并后的结果（就地写 ``fetched``）。
            top_n: 抓取条数。
            deadline: 本次调用的总预算截止时刻（``time.monotonic()`` 基准）；
                单条超时按"剩余预算"二次收敛，防止抓取阶段吃掉全部预算。
        """
        target = results[: max(0, int(top_n))]
        if not target:
            return
        fetcher = self._make_search_fetcher()
        per_timeout = float(self.search_fetch_timeout)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            per_timeout = max(0.5, min(per_timeout, remaining))

        async def fetch_one(item) -> None:
            began = time.monotonic()
            try:
                outcome = await asyncio.wait_for(
                    fetcher.fetch(item.url, timeout=per_timeout),
                    timeout=per_timeout + 0.5,
                )
            except asyncio.TimeoutError:
                outcome = FetchOutcome(
                    ok=False,
                    code="fetch_timeout",
                    url=item.url,
                    elapsed_ms=int((time.monotonic() - began) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 — 抓取失败不影响主结果
                logger.debug(
                    f"[{self.metadata_name}] 搜索正文抓取失败: {_redact_url(item.url)}"
                    f" ({type(exc).__name__})"
                )
                outcome = FetchOutcome(
                    ok=False,
                    code="fetch_failed",
                    url=item.url,
                    elapsed_ms=int((time.monotonic() - began) * 1000),
                )
            if outcome.ok:
                # 正文复用 _check_banned（受 security.enable_banned_filter 控制）：
                # 命中则丢正文、保留 title/url/snippet，便于模型仍能引用来源。
                banned = self._check_banned(outcome.text)
                if banned:
                    outcome.ok = False
                    outcome.code = "fetch_blocked"
                    outcome.text = ""
            item.fetched = outcome.as_dict()

        await asyncio.gather(
            *(fetch_one(item) for item in target), return_exceptions=True
        )

    # ---- 工具本体 ----

    @filter.llm_tool(name=_SEARCH_TOOL_NAME)
    async def browser_web_search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        max_results: int = 0,
        rounds: int = 0,
        timeout: int = 0,
        providers: str = "",
        fetch_pages: bool = False,
        fetch_top_n: int = 0,
    ) -> str:
        """网页搜索（本插件自有搜索 API 池，返回结构化 JSON）；与内置的 web_search_<provider> 不同，同时可见时只选其一。

        并行调用本插件配置的搜索 API（brave/bocha/firecrawl/tavily/exa，
        每种可配多份、各有自己的 api_key），按 URL 去重合并后返回带来源的
        结构化结果，可选抓取前几条的正文。**与 AstrBot 内置的
        ``web_search_<provider>`` 工具不同**：本工具只用插件配置里的 key，
        不读核心 ``provider_settings``，两者不会互相读取 key、也不会互相降级；
        若主 LLM 同时看到内置搜索工具，**只选其一**（不要重复搜同一个问题）。

        适合：事实问答、需要来源链接、只需摘要无需页面交互的检索。
        不适合：需要登录/点击/翻页/表单等真实页面交互（用 browse_web 委托
        浏览器子代理）、需要看页面外观（用 browse_web 截图）。

        返回一个 JSON 字符串：``status``（ok/partial/failed/disabled）、
        ``results[]``（rank/title/url/snippet/sources/fetched）、``attempts[]``
        （每个 provider 的 outcome/code/耗时）、``error_codes``、``notes``。
        回答用户时请依据 ``results[].snippet`` 或 ``fetched.text``，并给出链接。

        Args:
            query(string): 检索词。必填，空串会被拒绝。
            max_results(number): 返回条数上限（1-50）。0 表示用插件配置值。可省略
            rounds(number): 搜索步数 = 并行波次上限（1-5）。0 表示用插件配置值。可省略
            timeout(number): 本次调用总预算秒数（5-300）。0 表示用插件配置值。可省略
            providers(string): 逗号分隔的 provider 名（如 "brave#1,bocha#1"），只允许池内已启用项，用于覆盖默认顺序。可省略
            fetch_pages(boolean): 是否抓取结果正文（需插件配置 search.fetch_pages 已开启，取「与」）。可省略
            fetch_top_n(number): 抓取前 N 条（0-5）。0 表示用插件配置值。可省略

        后台模式（v1.5.0）：tools.background_mode=all 时本工具后台化——立刻
        返回「已受理 + task_id」**纯文本**（不是 JSON），搜索结果单独作为一条
        消息发到本会话。因此后台模式下**不要**用这次返回值回答事实问题。
        """
        # 后台模式热更新（v1.5.0）：入口第一行刷新配置，Dashboard 把
        # background_mode 改回 off 时下一次调用即生效（F1）。
        self._refresh_config()
        if not self.search_enabled:
            return self._search_payload(
                status="disabled",
                code="disabled",
                summary="【拒绝】搜索工具已在插件配置中关闭（search.enabled=false）",
                query=(query or "").strip(),
                hint=(
                    "如需使用，请在插件配置「搜索 API（browser_web_search）」里"
                    "打开 enabled 并至少填写一个 provider 的 api_key。"
                ),
            )
        if self._background_enabled_for(_SEARCH_TOOL_NAME):
            return await self._submit_background(
                event=event,
                tool_name=_SEARCH_TOOL_NAME,
                runner=lambda prog: self._run_browser_web_search(
                    event,
                    query=query,
                    max_results=max_results,
                    rounds=rounds,
                    timeout=timeout,
                    providers=providers,
                    fetch_pages=fetch_pages,
                    fetch_top_n=fetch_top_n,
                    progress=prog,
                    refresh=False,
                ),
                task_text=query,
            )
        return await self._run_browser_web_search(
            event,
            query=query,
            max_results=max_results,
            rounds=rounds,
            timeout=timeout,
            providers=providers,
            fetch_pages=fetch_pages,
            fetch_top_n=fetch_top_n,
        )

    async def _run_browser_web_search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        max_results: int = 0,
        rounds: int = 0,
        timeout: int = 0,
        providers: str = "",
        fetch_pages: bool = False,
        fetch_top_n: int = 0,
        progress: dict | None = None,
        refresh: bool = True,
    ) -> str:
        """browser_web_search 的实际执行体（前台同步调用与后台任务共用）。

        ``progress``：增量快照容器（后台任务会传入自己的那份，超时时据此
        渲染「已得部分结果」）；``refresh``：是否在开头重读配置——入口已刷新
        时传 False（后台分支），避免一次调用读两遍配置。
        """
        began = time.monotonic()
        text = (query or "").strip()
        notes: list[str] = []
        # 搜索阶段的**增量快照**容器（t6 M-1）：search_waves 每波结束都把
        # 已得 results/attempts/rounds_used/status 写进来；外层 wait_for 超时
        # 会取消协程并丢掉返回值，这里保留最后一次快照用于渲染 partial。
        # v1.5.0：后台任务传入自己那份（超时后由调用方读它渲染「已得部分结果」）。
        progress = progress if progress is not None else {}
        try:
            # 入口兜底（captain 要求）：①面板单独停用了该工具、②外部直接改
            # 配置文件但没触发 reload —— 两种情况都在这里返回 disabled。
            if refresh:
                self._refresh_config()
            if not self.search_enabled:
                return self._search_payload(
                    status="disabled",
                    code="disabled",
                    summary="【拒绝】搜索工具已在插件配置中关闭（search.enabled=false）",
                    query=text,
                    hint="如需使用，请在插件配置「搜索 API（browser_web_search）」里打开 enabled 并至少填写一个 provider 的 api_key。",
                )

            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return self._search_payload(
                    status="failed",
                    code="session_denied",
                    summary=f"【拒绝】{deny_reason}",
                    query=text,
                )

            if not text:
                return self._search_payload(
                    status="failed",
                    code="empty_query",
                    summary="【错误】检索词为空，请提供要搜索的问题或关键词",
                    query=text,
                )

            banned = self._check_banned(text)
            if banned:
                return self._search_payload(
                    status="failed",
                    code="banned",
                    summary=f"【拒绝】检索词包含违禁内容：{banned}",
                    query=text,
                )

            entries = self._build_search_entries()
            # 结果 URL 的字面校验在这里统一兜底挂上去：即使池是外部注入的
            # （没有走 build_provider_entries），也不会漏掉 SSRF 过滤。
            guard = self._search_url_guard()
            for entry in entries:
                if entry.url_guard is None:
                    entry.url_guard = guard
            usable = [entry for entry in entries if is_entry_usable(entry)]
            if not usable:
                return self._search_payload(
                    status="failed",
                    code="no_provider",
                    summary="【错误】没有可用的搜索后端（池内无已填写 api_key 的 provider）",
                    query=text,
                    hint="请在插件配置「搜索 API（browser_web_search）」→ provider 池中添加模板并填写 api_key。",
                    attempts=self._pool_attempts(entries),
                )

            limit = _clamp(max_results or self.search_max_results, 1, 50)
            rounds_cap = _clamp(rounds or self.search_max_rounds, 1, 5)
            budget = float(_clamp(timeout or self.search_total_timeout, 5, 300))
            top_n = _clamp(fetch_top_n or self.search_fetch_top_n, 0, 5)
            want_fetch = bool(fetch_pages) and bool(self.search_fetch_pages) and top_n > 0
            provider_filter = self._parse_providers_arg(providers)

            # 抓取阶段留 40% 预算（spec §7.3），搜索阶段不超 60%。
            search_budget = budget * 0.6 if want_fetch else max(1.0, budget - 1.0)
            service = self._get_search_service(entries)
            wave = await asyncio.wait_for(
                service.search_waves(
                    text,
                    max_results=limit,
                    max_rounds=rounds_cap,
                    max_parallel=int(self.search_max_parallel),
                    provider_timeout=float(self.search_provider_timeout),
                    time_budget_s=search_budget,
                    provider_filter=provider_filter,
                    progress=progress,
                ),
                timeout=budget + 1.0,
            )
            notes.extend(wave.notes)

            # 非 brave 的 provider 忽略 region：给运营方一个可观测提示。
            if self.search_region:
                for entry in usable:
                    if entry.template != "brave":
                        tag = f"region_unsupported:{entry.template}"
                        if tag not in notes:
                            notes.append(tag)

            results, banned_dropped = self._filter_banned_results(list(wave.results))
            if banned_dropped:
                notes.append(f"banned_results:{banned_dropped}")

            if want_fetch and results:
                await self._fetch_search_results(
                    results, top_n, deadline=began + budget
                )

            status = wave.status
            code = wave.code
            fetch_failed = any(
                isinstance(item.fetched, dict) and not item.fetched.get("ok", False)
                for item in results
            )
            if results:
                degraded = status == "partial" or fetch_failed or bool(banned_dropped)
                status = "partial" if degraded else "ok"
                code = "ok"
            else:
                status = "failed"
                if banned_dropped:
                    code = "banned"

            summary = summarize(
                status=status,
                code=code,
                results=results,
                attempts=wave.attempts,
                query=text,
            )
            if status == "failed" and code == "banned":
                summary = "【拒绝】搜索结果全部命中禁词，已丢弃（可调整 security.banned_words）"
            hint = ""
            if status == "failed":
                hint = self._search_failure_hint(code)
            return self._search_payload(
                status=status,
                code=code,
                summary=summary,
                query=text,
                rounds_used=wave.rounds_used,
                max_rounds=wave.max_rounds,
                providers_effective=wave.providers_effective,
                cache=wave.cache,
                elapsed_ms=int((time.monotonic() - began) * 1000),
                results=results,
                attempts=wave.attempts,
                error_codes=wave.error_codes,
                notes=notes,
                hint=hint,
            )
        except asyncio.TimeoutError:
            # 外层兜底触发（搜索阶段被预算切断）：**已有结果必须保留**，
            # 渲染成 partial 而不是把整批结果丢掉（t6 M-1）。
            partial = list(progress.get("results") or [])
            partial, dropped = self._filter_banned_results(partial)
            attempts = list(progress.get("attempts") or [])
            timeout_notes = notes + ["total_timeout"]
            if dropped:
                timeout_notes.append(f"banned_results:{dropped}")
            if partial:
                return self._search_payload(
                    status="partial",
                    code="ok",
                    summary=summarize(
                        status="partial",
                        code="ok",
                        results=partial,
                        attempts=attempts,
                        query=text,
                    ),
                    query=text,
                    rounds_used=int(progress.get("rounds_used") or 0),
                    max_rounds=int(progress.get("max_rounds") or 0),
                    providers_effective=list(progress.get("providers_effective") or []),
                    elapsed_ms=int((time.monotonic() - began) * 1000),
                    results=partial,
                    attempts=attempts,
                    notes=timeout_notes,
                )
            return self._search_payload(
                status="failed",
                code="total_timeout",
                summary=f"【错误】搜索超过总预算（{self.search_total_timeout:.0f}s）被中断",
                query=text,
                rounds_used=int(progress.get("rounds_used") or 0),
                max_rounds=int(progress.get("max_rounds") or 0),
                elapsed_ms=int((time.monotonic() - began) * 1000),
                attempts=attempts,
                notes=timeout_notes,
                hint="可调大插件配置 search.total_timeout，或减小 max_parallel/max_rounds。",
            )
        except Exception as exc:  # noqa: BLE001 — 工具契约：绝不把异常抛给模型
            message = redact_secrets(
                f"{type(exc).__name__}: {exc}", self._search_secret_values()
            )
            logger.warning(
                f"[{self.metadata_name}] browser_web_search 执行异常: {message}"
            )
            return self._search_payload(
                status="failed",
                code="internal_error",
                summary="【错误】搜索工具内部异常，请稍后重试或改用 browse_web",
                query=text,
                elapsed_ms=int((time.monotonic() - began) * 1000),
                notes=notes,
            )

    def _pool_attempts(self, entries: list) -> list:
        """把"池内无可用 provider"的原因展开成 attempts（供模型/运营方判断）。"""
        from .core.search import SearchAttempt  # noqa: PLC0415 — 避免顶层多导一个符号

        out = []
        for entry in entries:
            code = "ok"
            if entry.config_error:
                code = entry.config_error
            elif entry.template not in PROVIDER_CLASSES:
                code = "unknown_template"
            elif not entry.enabled:
                code = "disabled"
            elif not entry.api_key:
                code = "missing_key"
            if code == "ok":
                continue
            out.append(
                SearchAttempt(
                    provider=entry.name,
                    template=entry.template,
                    round=0,
                    outcome="skipped",
                    code=code,
                )
            )
        return out

    @staticmethod
    def _search_failure_hint(code: str) -> str:
        """失败时给运营方的处置建议（只提配置位置，不提任何值）。"""
        hints = {
            "auth": "某个 provider 鉴权失败（401/403）：请检查该 provider 的 api_key 是否正确/过期。",
            "rate_limited": "某个 provider 触发限流（429）：已进入冷却，请稍后再试或调大 search.cooldown_seconds。",
            "http_5xx": "某个 provider 上游异常（5xx）：稍后重试即可。",
            "timeout": "某个 provider 响应超时：可调大 search.provider_timeout 或检查网络/代理。",
            "network": "网络不可达：请检查 search.proxy 与服务器出网情况。",
            "bad_response": "某个 provider 返回结构不符：可能是端点改动或反代返回了错误页，请检查 base_url。",
            "empty": "provider 返回 0 条结果：换个检索词或换 provider。",
            "no_provider": "请在插件配置「搜索 API（browser_web_search）」里添加 provider 并填写 api_key。",
            "banned": "结果命中 security.banned_words：请调整禁词表后再试。",
        }
        return hints.get(code, "请检查插件配置「搜索 API（browser_web_search）」的 provider 池与超时设置。")

    def _build_search_instruction(self) -> str:
        """搜索工具的使用指引（仅在工具 active 时注入，避免让模型去调不存在的工具）。"""
        text = (
            "\n\n## 搜索工具指引（browser_web_search）\n"
            "你可以用 browser_web_search 直接做联网检索：它并行调用本插件配置的"
            "搜索 API 池，返回 JSON（results 里带 url/snippet，可选抓正文）。\n\n"
            "【要用】事实问答、需要来源链接、只需摘要的检索；"
            "回答时依据 results[].snippet 或 results[].fetched.text，并给出链接。\n\n"
            "【不要用】需要登录、点击、翻页、填表或看页面外观的任务 —— 那些用"
            "browse_web 委托浏览器子代理。\n\n"
            "【与内置搜索并存】browser_web_search 与 AstrBot 内置的"
            " web_search_<provider> 是两套独立实现（各自的 key、不互相降级）；"
            "同时可见时**只选其一**，不要对同一个问题重复搜两遍。"
        )
        if self.background_mode == "all":
            # 后台模式下本工具返回【已受理】而不是 JSON（设计 §2.4）：主模型
            # 若照着这条返回值回答，等于凭空编造搜索结果，必须点明。
            text += (
                "\n\n【后台模式已开启】本工具此刻是**后台**执行：拿到的是"
                "「【已受理】+ task_id」纯文本，不是搜索结果。"
                "此时不要用这次返回值回答事实问题，只回一句"
                "“已开始搜索，结果稍后单独发送”。"
            )
        return text

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
        text = (
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
        # 搜索工具仅在 active 时注入指引（active=False 时模型根本看不到该
        # 工具，再写指引只会诱导幻觉调用）。
        if self._search_tool_active:
            text += self._build_search_instruction()
        return text
