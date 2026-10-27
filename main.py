"""astrbot_plugin_browser_llm — LLM 浏览器插件（v1.7.5：感知/动作/执行/验证四层重做）。

**架构（规格 §1）**：主 LLM 只做委派，插件内置一个子代理在真实浏览器里干活。
- 外层 **3 个 LLM 工具**（名字一律不改，生产人格按名硬白名单）：
  ``browse_web``（唯一委派入口）/ ``browse_local_page``（本地 HTML 渲染预览，
  不进子代理）/ ``browser_web_search``（搜索 API 池）。
- 内层 **5 个工具**（``INNER_TOOLS``，规格 §9.2）：``browse_observe`` /
  ``browse_act`` / ``browse_screenshot`` / ``browse_media`` / ``browse_search``。
  它们只进 ``self._browser_toolset``，**不在** ``func_list`` 里，因此不受人格
  白名单 / ``inactivated_llm_tools`` 影响。
- 六层各只有一个事实源：感知 ``core/a11y.py``（CDP AX 树 + SoM 兜底）/
  动作 ``core/action_model.py``（``ACTION_SPEC`` + ``R-A1..R-A14``）/
  执行 ``core/executor.py``（locator 定位 + CDP 受信任事件）/
  验证 ``core/verify.py``（expect / 差分 / 无变化 / 重复守卫 / 观测块渲染）/
  后台 ``core/background.py`` / 配置 ``core/config_map.py``。

**本文件的职责只有四件**：鉴权与预算快照 → 前台/后台分派 → 把五层接到一起
（``_snapshot`` / ``_act`` / ``_observe``）+ 结果合成。**不做**页面读写原语：
像素坐标、selector 枚举、截图识图描述三条旧链（v1.7.0 的三段注入 JS）已整条
删除 —— 它们是 D1/D8 的根因（``Page.evaluate`` 只收一个位置参数）。

**终态投递（v1.7.6 起）**：后台终态不再 ``event.send`` 直捅适配器，而是走
``self._deliver_terminal`` → :func:`core.active_send.deliver`（合成事件 +
重放 ``on_decorating_result``/``on_after_message_sent`` + ``Context.send_message``，
平台不可用时才用提交时抓下的 ``event.send`` 回退）。记账拆成
``attempted`` / ``succeeded`` / ``accepted_by_platform`` / ``error`` / ``sent_via``，
回给 ``core.background`` 的 ``TerminalOutcome.delivery``。

**导入纪律**：本文件一律用**包内相对导入**（``from .core.x import …``）。测试用
``astrbot_plugin_browser_llm`` 包名 + 顶层别名统一实例；写成 ``from core.x``
会造出第二份模块对象，``Rejection`` 之类的 ``isinstance`` 会静默失效。
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import types
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote, urljoin, urlsplit

if __package__ in ("", None):  # pragma: no cover — 裸 `python -c "import main"` 冒烟
    # 顶层导入时本文件的相对导入没有父包。做法：把插件目录注册成包，并把
    # `__package__` 指过去 —— 后续 import 全部走包内，**不会**造出第二份模块
    # 实例（这正是 v1.4 踩过的同名坑）。
    _pkg_name = "astrbot_plugin_browser_llm"
    if _pkg_name not in sys.modules:
        _pkg_mod = types.ModuleType(_pkg_name)
        _pkg_mod.__path__ = [str(Path(__file__).resolve().parent)]
        sys.modules[_pkg_name] = _pkg_mod
    __package__ = _pkg_name

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .core import a11y, as_bool, as_float, as_int
from .core import action_model as am
from .core import agent as agent_core
from .core import executor as ex
from .core import verify as vf
from .core.background import DELIVERY_CHANNEL_TERMINAL, BudgetWatchdog
from .core.browser import BrowserCore, validate_browser_type
from .core.config_map import ConfigView, clamp_of, default_of, schema_leaf
from .core.crawler import AiohttpFetcher, CrawlerLimits, FetchOutcome
from .core import active_send
from .core.active_send import DeliveryReceipt
from .core.delivery import artifact_sink, collect_artifact
from .core.extract import ContentExtractor
from .core.safety import SafetyFilter, check_url_literal
from .core.search import (
    PAYLOAD_MAX_BYTES,
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
_PLUGIN_VERSION_FALLBACK = "v1.7.5"

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

# 本插件注册的三个**外层** LLM 工具名（唯一事实源：装饰器字面量必须有且仅有
# 这三个；内层 5 个工具不在 ``func_list`` 里，见 ``INNER_TOOL_NAMES``）。
_ALL_TOOL_NAMES = (_BROWSE_WEB_TOOL_NAME, _LOCAL_PAGE_TOOL_NAME, _SEARCH_TOOL_NAME)

# 后台任务（v1.5.0 路线 D）：
# - ``tools.background_mode`` 三取值：off（默认，行为与 v1.4.0 完全一致）/
#   browse_only（只把 browse_web 后台化）/ all（三个工具都后台化）；
# - 终态消息**四态四前缀**（设计 §4.7 唯一口径，D3 裁决）：成功
#   「【后台任务完成】」/ 失败「【后台任务失败】」/ 超时「【后台任务超时】」/
#   取消「【后台任务已取消】」——**不要**回落成单前缀，否则失败会被算成成功；
# - 生产判据两套口径各管一件事：``grep -c "后台任务受理"`` 与
#   ``grep -c "后台任务终态 status="``（四态合计）+ 按前缀数成功数；
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

#: ``persona_mode=current`` 的人格缓存 TTL（秒，v1.7.0）。超时视为未命中，
#: 重新走 persona_manager 次路径 —— 人格可能在会话里被切换，缓存太久会用旧人格。
_PERSONA_CACHE_TTL = 30 * 60

#: 会被误读成"插件自己报的"三个前缀（v1.7.0 repair，评审 V4）。只用于
#: **人读的终态消息**加一行消歧说明；模型侧的前缀语义由结构化 meta 决定。
_TRUSTED_LOOKING_PREFIXES = ("【错误】", "【拒绝】", "【不可用】")

#: 页面/内层模型原文以「像插件的错误前缀」开头时，给人加的一行消歧说明。
_ECHO_NOTE = "（以下为内层模型或页面原文，不是插件判定的错误）"

#: 内层模型不支持图片时的**明确**文案（规格 §9.2 第 3 行：无多模态 ⇒ ``【不可用】``）。
_IMAGE_UNAVAILABLE_TEXT = (
    "【不可用】当前内层模型不支持图片（或图片超出体积上限），本张 SoM 图未附上。"
    "请改用 browse_observe 的结构视图，或让主 LLM 换一个多模态内层模型"
    "（model.provider_id）。"
)


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


# ---------------------------------------------------------------------------
# 内层工具面：26 → 5（规格 §9.2）—— 两个分发器 + 三个专用
# ---------------------------------------------------------------------------

#: 内层工具名（顺序即注册顺序）。与 ``_ALL_TOOL_NAMES``（外层 3 个）**不相交**。
INNER_TOOL_NAMES: tuple[str, ...] = (
    "browse_observe",
    "browse_act",
    "browse_screenshot",
    "browse_media",
    "browse_search",
)

#: §5.5 的三句硬话（逐字来自 ``core.agent.INNER_RULES``：写一处、两处用）。
_HARD_RULES = "\n".join(agent_core.INNER_RULES)

#: ``browse_observe`` 的视图枚举（顺序即文档顺序）。
OBSERVE_VIEWS: tuple[str, ...] = ("structure", "text", "links", "full")

#: ``browse_observe`` / ``browse_act`` 的 description（**由 ACTION_SPEC 生成**，
#: 禁止在别处再手写第二份动作清单 —— 铁律 3）。
_OBSERVE_DESC = (
    "看当前页面（A11y 结构视图 = 唯一状态来源）。\n"
    "view=structure（默认）：每行 `- [role] name`，首行 `【观测 #n】URL | 标题 | "
    "视口@dpr | 滚动位置 | frames | nodes`；view=text：正文（截断）；"
    "view=links：链接表（带 name，可直接喂 target{role:\"link\",name:…}）；"
    "view=full：全量节点（带 #编号，仅供对照）。\n"
    "读了观测才知道页面上有什么；点了以后也读【观测】确认状态。\n" + _HARD_RULES
)

_ACT_DESC = (
    "执行**一个**结构化动作（16 种，字段与语义见目录），返回【观测】+【动作】+【验证】。\n"
    '例：{"action":"click","target":{"role":"button","name":"提交"}}；'
    '{"action":"type","target":{"label":"搜索"},"value":"北京 天气","submit":true}。\n'
    "target 六选一：role+name / label / text / testid / css / mark（截图标签编号）；"
    "index 从 0 起算（同一动作多命中时才需要）；expect 可选，用来声明本次动作的"
    "期望结果（url_contains / text_contains / role+name / state / count）。\n"
    + am.render_action_catalog() + "\n" + _HARD_RULES
)

#: 内层工具定义（``_make_browser_tool`` 消费；description 是给内层模型看的唯一契约）。
INNER_TOOLS: list[dict] = [
    {
        "name": "browse_observe",
        "description": _OBSERVE_DESC,
        "parameters": _params(
            {"view": ("structure / text / links / full（默认 structure）", "string")},
            {"max_items": ("本次最多给多少条（0=用配置预算）", "number")},
        ),
        "method": "browse_observe",
    },
    {
        "name": "browse_act",
        "description": _ACT_DESC,
        "parameters": {
            "type": "object",
            "properties": {
                # 直接给冻结契约本体（不复制、不覆写：ACTION_JSON_SCHEMA 自带
                # description；复制会让"工具参数 == 规格 Schema"这条判据失效）。
                "action": am.ACTION_JSON_SCHEMA,
            },
            "required": ["action"],
        },
        "method": "browse_act",
    },
    {
        "name": "browse_screenshot",
        "description": (
            "兜底通道：截当前页面并叠加 Set-of-Mark 编号（canvas / 图片按钮 / "
            "验证码 / 结果核对时才用）。返回标签表 + 图；点图里的元素用 "
            '{"action":"click","target":{"mark":N}}（编号只在本张图有效）。'
            "target 给一个元素（role/name 等）时只截该元素并放大局部。\n" + _HARD_RULES
        ),
        "parameters": _params(
            {},
            {
                "target": ("可选：{\"role\":…,\"name\":…} 等，截该元素局部", "object"),
                "full": ("是否整页长截图（默认 false；整页图不带编号）", "boolean"),
            },
        ),
        "method": "browse_screenshot",
    },
    {
        "name": "browse_media",
        "description": (
            "列出当前页面里的图片/视频并下载可发的媒体（路径 + 是否已发布）。"
            "media_type=all/image/video；max_items 默认 5。"
        ),
        "parameters": _params(
            {},
            {
                "media_type": ("all / image / video（默认 all）", "string"),
                "max_items": ("最多下载几个（默认 5）", "number"),
            },
        ),
        "method": "browse_media",
    },
    {
        "name": "browse_search",
        "description": (
            "网页搜索。默认走插件配置的搜索 API 池（返回结构化 JSON，不开页面）；"
            "open_page=true 时走真实浏览器：打开 search.engine 的结果页，"
            "随后用 browse_observe / browse_act 继续交互。"
        ),
        "parameters": _params(
            {"query": ("检索词（必填，空串会被拒绝）", "string")},
            {
                "max_results": ("返回条数上限（0=用配置）", "number"),
                "rounds": ("并行波次上限（0=用配置）", "number"),
                "timeout": ("本次调用总预算秒（0=用配置）", "number"),
                "fetch_pages": ("是否抓取结果正文", "boolean"),
                "fetch_top_n": ("抓取前 N 条（0=用配置）", "number"),
                "open_page": ("true=打开引擎结果页（浏览器交互路径）", "boolean"),
            },
        ),
        "method": "browse_search",
    },
]


@dataclass
class _TaskState:
    """一个浏览任务的感知/动作状态（**按任务**隔离，跨任务绝不复用）。

    - ``refs``：ref 编号分配器（跨快照**不**复用；新任务才 ``reset()``）；
    - ``marks``：SoM 标记纪元（换图/改变页面的动作/超时/新任务都作废）；
    - ``guard``：重复动作守卫（同一动作连续 N 次无变化 ⇒ 拒绝）；
    - ``step``：本次任务内的观测序号（``【观测 #n】`` 的 n）；
    - ``pending``：待登记进台账的验证状态（``verified/changed/unchanged/…``）。
    """

    refs: a11y.RefAllocator = field(default_factory=a11y.RefAllocator)
    marks: a11y.MarkEpoch = field(default_factory=a11y.MarkEpoch)
    guard: vf.RepeatGuard = field(default_factory=vf.RepeatGuard)
    step: int = 0
    snapshot: object | None = None
    mark_nodes: dict = field(default_factory=dict)
    pending: str = ""
    #: 任务键（`claim_page` 用它"按任务取页"；第一次 `reset_task` 才真正赋值）
    task_key: str = ""
    #: 本任务的墙钟截止时刻（monotonic；0 = 没有预算，不显示【预算】行）
    deadline: float = 0.0
    #: 最后使用时刻（monotonic；R31：淘汰策略按它做 LRU，而不是按插入序）
    last_used: float = 0.0


def _make_browser_tool(plugin: "BrowserLLMPlugin", spec: dict):
    """工厂：为一条 _BROWSER_TOOLS 配置生成 FunctionTool 实例。

    使用 handler 字段（官方一等公民）：执行器以 ``handler(event, *args, **kwargs)``
    调用（``astr_agent_tool_exec.py:794-814``），等效于 task 描述的子类覆写
    ``call`` 方案，且避免 pydantic dataclass 子类化的兼容风险。handler 绑定
    插件实例方法，签名与内部 browse_* 方法一致（self, event, **kwargs）。

    包装三层（顺序即正确性，规格 §8.3）：

    1. ``_refresh_config()``：配置热更新（v1.2.0），使 Dashboard 改的黑名单/
       截图开关/内网拦截在**下一次**子代理工具调用时即生效；
    2. ``agent_core.record_and_truncate``：**先**记台账（拿到的是原始结果）、
       再按 ``agent.result_max_chars`` 截断文本、超累计预算时压实到 800 字符。
       台账必须在截断**之前**记，否则台账里的 ``brief`` 会带上截断尾巴；
    3. 返回值原样透传：``str`` 或 ``mcp.types.CallToolResult``（影像原生）。
       执行器自己区分两者（``astr_agent_tool_exec.py:744-756``）。
    """
    method = getattr(plugin, spec["method"])

    async def _handler(event, **kwargs):
        # 工具入口轻量热更新配置（重读共享 config dict）。
        plugin._refresh_config()
        raw = await method(event, **kwargs)
        return agent_core.record_and_truncate(
            raw,
            tool=spec["name"],
            args=kwargs,
            run=agent_core.current_run(),
            # v1.7.5：台账多一列验证状态（verified/changed/unchanged/failed/
            # rejected）——"点了但没变化"因此在台账里留得下痕（D2 的观测点）。
            status=plugin._ledger_status_of(event),
        )

    return agent_core.build_function_tool(
        name=spec["name"],
        description=spec["description"],
        parameters=spec["parameters"],
        handler=_handler,
    )


#: 终态附件按扩展名分流用的视频后缀（`Image` 发视频会失败）。
_VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv")

#: 任务文本里的显式网址（``http(s)://…``）或裸域名（``example.com/…``）。
#: v1.7.5 移除起始页后，"打开网页"类任务**必须**自带目标，否则插件不猜。
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s，。；、\"'」』]+", re.IGNORECASE)
_DOMAIN_IN_TEXT_RE = re.compile(
    r"(?<![\w.])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:/\S*)?",
    re.IGNORECASE,
)

#: 没有显式网址、会话里也没有活页面时的**明确**提示（不打开任何默认站点）。
_NEED_URL_TEXT = (
    "【需要网址】v1.7.5 起不再有「起始页」：请给出要打开的网址（例如 "
    "https://example.com/page），或先用 browse_search 搜索后点结果。"
    "本次**没有**打开任何默认站点，也没有产生浏览行为。"
)


def _has_explicit_url(text: str) -> bool:
    """任务文本是否自带网址/域名（拆出纯函数便于单测）。"""
    body = str(text or "")
    return bool(_URL_IN_TEXT_RE.search(body) or _DOMAIN_IN_TEXT_RE.search(body))


#: 传给核心 ``tool_loop_agent`` 的步数上限**不是预算口径**（v1.7.5 只用一条墙钟
#: 预算收口）。按规格 §7.4-9 的公式派生：``clamp(64, 512, total_budget_s * 2)`` ——
#: 墙钟是主闸，这个值只做"防死循环"的护栏，且**不可配置**。
AGENT_MAX_STEPS_MIN = 64
AGENT_MAX_STEPS_MAX = 512

#: 每会话任务状态的**软上限**（到此开始淘汰空闲状态）与**硬上限**（全 live 时兜底
#: 按 LRU 淘汰，保证内存不无限增长）。R31：两级上限都要有，否则"全是 live"就只增不减。
_TASK_STATE_SOFT_CAP = 64
_TASK_STATE_HARD_CAP = 256
_TASK_STATE_EVICT_BATCH = 32


def derive_max_steps(total_budget_s: float) -> int:
    """从总预算派生步数护栏（纯函数，spec §7.4-9 的公式）。"""
    try:
        budget = float(total_budget_s)
    except (TypeError, ValueError):
        budget = 0.0
    return int(max(AGENT_MAX_STEPS_MIN, min(AGENT_MAX_STEPS_MAX, int(budget * 2))))


@dataclass
class _TabsView:
    """把 ``SessionManager`` 收口成 executor 期望的标签页接口（按会话绑定 umo）。

    executor 只认 ``new_tab/switch_tab/close_tab/list_tabs`` 四个方法，且标签
    编号**1 起算**（与 ``SessionManager`` 的用户视角一致）。
    """

    sessions: object
    umo: str

    async def new_tab(self, url: str = "about:blank"):
        return await self.sessions.new_tab(self.umo, url)

    async def switch_tab(self, index: int):
        return await self.sessions.switch_tab(self.umo, int(index))

    async def close_tab(self, index: int):
        return await self.sessions.close_tab(self.umo, int(index))

    def list_tabs(self) -> list[str]:
        count = int(self.sessions.tab_count(self.umo))
        return [f"第 {i} 个标签" for i in range(1, count + 1)]


#: 媒体候选的 HTML 属性扫描（**不用** ``page.evaluate``：JS 枚举链已退役）。
_MEDIA_ATTR_RE = re.compile(
    r"""<(img|video|source|a)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def _scan_media_urls(html: str, base_url: str) -> list[tuple[str, str]]:
    """从 HTML 扫媒体候选 ``(绝对 URL, tag)``（去重保序；相对地址按页面补全）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag, raw in _MEDIA_ATTR_RE.findall(html or ""):
        url = str(raw or "").strip()
        if not url or url.startswith(("data:", "javascript:")):
            continue
        absolute = urljoin(base_url, url)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, tag.lower()))
    return out


def _classify_media(url: str, tag: str) -> str | None:
    """按后缀分类；无后缀时按来源标签兜底（``a[href]`` 无法判断 ⇒ ``None``）。"""
    path = url.split("?", 1)[0].lower().rstrip("/")
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "image"
    if path.endswith((".mp4", ".webm", ".mov")):
        return "video"
    if tag == "img":
        return "image"
    if tag in ("video", "source"):
        return "video"
    return None


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

    # 后台任务的类级默认（与 _conf_schema.json 的 default 一致）：默认 off ⇒
    # 三个外层工具保持前台同步行为（v1.7.5 只剩插件自管后台，core 引擎已退役）。
    background_mode: str = "off"
    background_max_chars: int = 1500

    def __init__(self, context: Context, config: AstrBotConfig = None):
        """初始化插件：读取配置 + 预置运行期容器（组件在 ``initialize()`` 建）。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置对象（v6：7 组 / 58 叶；未传入时用空字典兜底）。
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

        # 感知层的 CDP 会话池（**每插件一个**：跨快照复用 Accessibility.enable）。
        self._cdp_pool = a11y.CdpSessionPool()
        # 任务状态表：umo -> _TaskState（ref 分配器 / mark 纪元 / 重复守卫 / 步数）。
        self._tasks: dict[str, _TaskState] = {}

        # browse_local_page 专用的 per-umo 锁（弱引用：无持锁者时自动回收）。
        self._local_page_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._local_page_allowed_roots: tuple[Path, ...] = _resolve_local_page_roots()

        # 搜索工具运行期状态：ProviderEntry 池 + 冷却 + TTL 缓存。
        self._search_service: SearchService | None = None
        self._search_service_signature: tuple | None = None

        # 搜索引擎模板：{keyword} 会被 URL 编码后替换（``search.engine`` 的取值域）。
        self._search_engines = {
            "必应搜索": "https://cn.bing.com/search?q={keyword}&FORM=BESBTB&ensearch=1",
            "百度搜索": "https://www.baidu.com/s?wd={keyword}",
            "谷歌搜索": "https://www.google.com.hk/search?&q={keyword}",
            "B站搜索": "https://search.bilibili.com/all?keyword={keyword}",
        }

        # 后台任务登记表（插件自管后台；同 UMO 单飞 + 全局并发闸）。
        # 后台设施（v1.7.5）：任务表 + 预算看门狗 + 去重 + 单次投递 + 模板终态，
        # 全在 core.background.BudgetWatchdog 里（U1–U4 的唯一实现处）。
        # 默认值只是构造参数；每次 submit 都用当前配置覆盖（热更新即时生效）。
        self._watchdog = BudgetWatchdog(
            budget_s=float(self.total_budget_s),
            max_chars=int(self.background_max_chars),
            dedupe_window_s=float(self.dedupe_window_s),
            max_parallel=int(self.max_parallel),
        )

        # 内层子代理：5 个 FunctionTool 与它们的 ToolSet（构建一次，不再按配置重建）。
        self._browser_tools: list = []
        self._browser_toolset: object | None = None
        self._agent_sync_task: asyncio.Task | None = None
        self._persona_cache: dict[str, tuple[float, str | None, str]] = {}
        self._fg_inflight: dict[str, int] = {}

    def _load_config(self) -> None:
        """把 v6 配置读成实例属性（**唯一**读取点；键名来自 ``core/config_map.LEAVES``）。

        分组（7 组 / 58 叶）：``resources``（引擎/视口/代理/上限/统一超时与预算）、
        ``perception``（A11y 感知预算 + 截图兜底 + 会话规则）、``model``（内层模型 +
        人格 + 图片与结果上限）、``actions``（受信任事件/验证/重试/重复守卫）、
        ``search``（搜索 API 池）、``security``（禁词/内网/会话黑白名单）、
        ``tools``（后台四件套）。

        非法值一律 fail-closed 到默认并记 warning：手改 config.json 或第三方写坏
        类型时插件仍能加载（不抛异常阻断初始化）。
        """
        view = ConfigView(self.config)

        # ---- resources：浏览器与资源 ----
        self.browser_type: str = str(view.get("resources.engine", default_of("resources.engine", "chromium")))
        self.headless_mode: str = str(view.get("resources.headless", default_of("resources.headless", "new")))
        raw_viewport = view.get("resources.viewport", default_of("resources.viewport", {})) or {}
        if not isinstance(raw_viewport, dict):
            raw_viewport = default_of("resources.viewport", {}) or {}
        self.viewport: dict = dict(raw_viewport)
        self.device_scale_factor: float = self._as_float(
            self.viewport.get("device_scale_factor", 1), 1.0
        )
        self.proxy: str = str(view.get("resources.proxy", "") or "").strip()
        self.max_pages: int = self._as_clamped_int(
            "resources.max_tabs", view.get("resources.max_tabs", 5), 5
        )
        self.idle_timeout: float = self._as_float(
            view.get("resources.idle_timeout_s", 1800), 1800.0
        )
        self.cache_days: int = self._as_int(view.get("resources.cache_days", 3), 3)
        self.max_text_chars: int = self._as_clamped_int(
            "resources.max_text_chars", view.get("resources.max_text_chars", 4000), 4000
        )
        # v1.7.0 的旧名（正文抽取与既有调用点仍按这个读；同一个键，不是第二份事实源）。
        self.max_chars: int = self.max_text_chars
        self.max_links: int = self._as_clamped_int(
            "resources.max_links", view.get("resources.max_links", 20), 20
        )
        self.max_parallel: int = self._as_clamped_int(
            "resources.max_parallel", view.get("resources.max_parallel", 4), 4
        )
        # 统一的总操作超时：前台一次任务 / 后台一次任务共用同一条墙钟预算。
        self.total_budget_s: float = float(
            self._as_clamped_int(
                "resources.total_budget_s",
                view.get("resources.total_budget_s", 300),
                300,
            )
        )
        self.goto_timeout_s: float = float(
            self._as_clamped_int(
                "resources.goto_timeout_s", view.get("resources.goto_timeout_s", 15), 15
            )
        )
        self.action_timeout_s: float = float(
            self._as_clamped_int(
                "resources.action_timeout_s",
                view.get("resources.action_timeout_s", 10),
                10,
            )
        )

        # ---- perception：A11y 感知预算 + 截图兜底 ----
        self.a11y_max_nodes: int = self._as_clamped_int(
            "perception.a11y_max_nodes", view.get("perception.a11y_max_nodes", 300), 300
        )
        self.a11y_max_name_chars: int = self._as_clamped_int(
            "perception.a11y_max_name_chars",
            view.get("perception.a11y_max_name_chars", 120),
            120,
        )
        self.som_max_marks: int = self._as_clamped_int(
            "perception.som_max_marks", view.get("perception.som_max_marks", 60), 60
        )
        self.enable_screenshot: bool = self._as_bool(
            view.get("perception.screenshot_fallback", True), True
        )
        # v6 用「截图是否发到会话」一个开关取代 v5 的旧静默开关（语义取反）。
        self.screenshot_to_chat: bool = self._as_bool(
            view.get("perception.screenshot_to_chat", False), False
        )
        raw_rules = view.get("perception.rules", [])
        self.perception_rules: list = list(raw_rules or []) if isinstance(raw_rules, (list, tuple)) else []

        # ---- model：内层模型 / 人格 / 图片与结果上限 ----
        self.agent_provider_id: str = str(view.get("model.provider_id", "") or "").strip()
        self.agent_persona_mode: str = self._as_enum(
            "model.persona_mode",
            view.get("model.persona_mode", default_of("model.persona_mode", "default")),
            "default",
            agent_core.PERSONA_MODES,
        )
        self.agent_persona_prompt: str = str(
            view.get("model.persona_prompt", default_of("model.persona_prompt", "")) or ""
        )
        self.agent_image_max_side: int = self._as_clamped_int(
            "model.image_max_side", view.get("model.image_max_side", 1280), 1280
        )
        self.agent_image_quality: int = self._as_clamped_int(
            "model.image_quality", view.get("model.image_quality", 80), 80
        )
        self.agent_result_max_chars: int = self._as_clamped_int(
            "model.result_max_chars", view.get("model.result_max_chars", 3000), 3000
        )
        self.agent_ledger_max_lines: int = self._as_clamped_int(
            "model.ledger_max_lines", view.get("model.ledger_max_lines", 24), 24
        )

        # ---- actions：受信任事件 / 验证 / 重试 / 重复守卫 ----
        self.trusted_events: bool = self._as_bool(
            view.get("actions.trusted_events", True), True
        )
        self.actions_verify: bool = self._as_bool(view.get("actions.verify", True), True)
        self.action_retries: int = self._as_clamped_int(
            "actions.max_retries", view.get("actions.max_retries", 1), 1
        )
        self.repeat_guard_limit: int = self._as_clamped_int(
            "actions.repeat_guard", view.get("actions.repeat_guard", 3), 3
        )

        # ---- security ----
        self.banned_words: list = list(
            view.get(
                "security.banned_words",
                [
                    "pornhub", "色情", "成人", "赌博", "暴力",
                    "政治", "反动", "恐怖", "谣言", "诈骗", "病毒",
                ],
            )
            or []
        )
        self.block_internal_ip: bool = self._as_bool(
            view.get("security.block_internal_ip", True), True
        )
        self.enable_banned_filter: bool = self._as_bool(
            view.get("security.enable_banned_filter", True), True
        )
        self.session_whitelist: list = list(view.get("security.session_whitelist", []) or [])
        self.session_blacklist: list = list(view.get("security.session_blacklist", []) or [])

        # ---- search：搜索 API 池（v6 只剩一套引擎/代理口径）----
        self.search_enabled: bool = self._as_bool(
            view.get("search.enabled", default_of("search.enabled", False)), False
        )
        raw_providers = view.get("search.providers", [])
        self.search_providers: list = (
            list(raw_providers) if isinstance(raw_providers, (list, tuple)) else []
        )
        self.default_search_engine: str = str(
            view.get("search.engine", default_of("search.engine", "必应搜索")) or "必应搜索"
        )
        self.search_max_parallel: int = _clamp(
            self._as_int(view.get("search.max_parallel", 3), 3), 1, 10
        )
        self.search_max_results: int = _clamp(
            self._as_int(view.get("search.max_results", 8), 8), 1, 50
        )
        self.search_max_rounds: int = _clamp(
            self._as_int(view.get("search.max_rounds", 2), 2), 1, 5
        )
        self.search_provider_timeout: float = _clamp(
            self._as_float(view.get("search.provider_timeout_s", 15), 15), 3, 60
        )
        self.search_total_timeout: float = _clamp(
            self._as_float(view.get("search.total_timeout_s", 60), 60), 5, 300
        )
        self.search_cooldown_seconds: float = _clamp(
            self._as_float(view.get("search.cooldown_s", 60), 60), 0, 3600
        )
        self.search_cache_ttl: float = _clamp(
            self._as_float(view.get("search.cache_ttl_s", 120), 120), 0, 3600
        )
        self.search_fetch_pages: bool = self._as_bool(
            view.get("search.fetch_pages", False), False
        )
        self.search_fetch_top_n: int = _clamp(
            self._as_int(view.get("search.fetch_top_n", 3), 3), 0, 5
        )
        self.search_fetch_timeout: float = _clamp(
            self._as_float(view.get("search.fetch_timeout_s", 12), 12), 3, 60
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
        self.search_proxy: str = str(view.get("search.proxy", "") or "").strip()

        # ---- tools：后台四件套（机制只剩插件自管；core 引擎已退役）----
        mode = str(view.get("tools.background_mode", "off") or "off").strip().lower()
        if mode not in _BACKGROUND_MODES:
            logger.warning(
                f"[{self.metadata_name}] tools.background_mode={mode!r} 非法"
                f"（可选 {'/'.join(_BACKGROUND_MODES)}），已按 off 处理（前台同步）。"
            )
            mode = "off"
        self.background_mode: str = mode
        self.background_max_chars: int = _clamp(
            self._as_int(view.get("tools.background_max_chars", 1500), 1500), 200, 8000
        )
        self.dedupe_window_s: int = _clamp(
            self._as_int(view.get("tools.dedupe_window_s", 600), 600), 0, 86400
        )
        self.isolate_context: bool = self._as_bool(
            view.get("tools.isolate_context", True), True
        )
        self.narrow_tools: bool = self._as_bool(
            view.get("tools.narrow_tools", True), True
        )

    def _as_enum(
        self,
        path: str,
        value,
        default: str,
        allowed: tuple[str, ...],
    ) -> str:
        """枚举配置解析：非法值 fail-closed 到默认并记 warning（口径同 §6.2）。"""
        text = str(value or default).strip().lower()
        if text not in allowed:
            logger.warning(
                f"[{self.metadata_name}] {path}={value!r} 非法"
                f"（可选 {'/'.join(allowed)}），已按 {default} 处理。"
            )
            return default
        return text

    def _as_clamped_int(self, path: str, value, default: int) -> int:
        """数值配置解析：类型容错 + 按 ``core.config_map.clamp_of`` 钳制。"""
        number = self._as_int(value, default)
        bounds = clamp_of(path)
        if bounds is None:
            return number
        return _clamp(number, bounds[0], bounds[1])

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
        """工具入口轻量热更新：重读共享 config dict 并同步固化组件。

        Dashboard 保存配置时核心就地 update 同一 dict；本方法把最新值刷进实例
        属性，再同步进 ``SessionManager`` / ``SafetyFilter`` / ``BrowserCore`` 的
        运行期参数。只做 dict 读取与赋值，不开页面、不写盘。
        """
        self._load_config()
        if self.sessions is not None:
            self.sessions.max_pages = int(self.max_pages)
            self.sessions.idle_timeout = float(self.idle_timeout)
        if self.safety is not None:
            self.safety.update_config(
                banned_words=self.banned_words,
                block_internal_ip=bool(self.block_internal_ip),
            )
        if self.browser is not None:
            self.browser.block_internal_ip = bool(self.block_internal_ip)


    # ================================================================
    # 生命周期
    # ================================================================

    async def initialize(self) -> None:
        """插件激活：组装核心组件、构建 5 个内层工具、起后台维护任务。"""
        browser_config = dict(self.config)
        browser_config.setdefault("data_dir", str(_DATA_DIR))
        self.browser = BrowserCore(browser_config)
        self.sessions = SessionManager(
            self.browser,
            max_pages=int(self.max_pages),
            idle_timeout=float(self.idle_timeout),
            on_page_closed=self._forget_page_caches,
        )
        self.extractor = ContentExtractor()
        self.safety = SafetyFilter(self.banned_words, self.block_internal_ip)
        self._local_page_allowed_roots = _resolve_local_page_roots()
        # 运行期产物目录：截图与媒体都挂 _DATA_DIR（不写插件安装目录）。
        self._screenshot_dir = _DATA_DIR / "screenshots"
        self._media_dir = _DATA_DIR / "media"
        for _dir in (self._screenshot_dir, self._media_dir):
            _dir.mkdir(parents=True, exist_ok=True)
        self._cache_cleanup_task: asyncio.Task | None = None
        try:
            self._cleanup_cache()
        except Exception as e:  # noqa: BLE001 — 清理失败不影响启动
            logger.warning(f"[{self.metadata_name}] 首次缓存清理失败: {e}")
        self._start_cache_cleanup_task()
        await self.sessions.start_sweeper()
        # 5 个内层工具（v1.7.5：26 → 5，签名固定，不再随配置重建）。
        self._rebuild_browser_toolset()
        # 内层模型下拉同步（只列「多模态 + tool_use」候选）。
        self._sync_agent_provider_options()
        self._start_agent_provider_sync_task()
        logger.info(
            f"[{self.metadata_name}] 内层工具集: {len(self._browser_tools)} 个"
            f"（{'/'.join(INNER_TOOL_NAMES)}）"
        )
        logger.info(
            f"[{self.metadata_name}] 感知: A11y 树（最多 {self.a11y_max_nodes} 节点，"
            f"SoM 兜底={'开' if self.enable_screenshot else '关'}）"
        )
        logger.info(
            f"[{self.metadata_name}] 内容审核（禁词过滤）: "
            f"{'开启' if self.enable_banned_filter else '关闭'}"
        )
        logger.info(f"[{self.metadata_name}] 插件 {PLUGIN_VERSION} 已激活")
        logger.info(f"[{self.metadata_name}] 浏览器引擎: {self.browser_type}")
        logger.info(
            f"[{self.metadata_name}] 默认搜索引擎: {self.default_search_engine}"
        )


    @staticmethod
    def _build_toolset(tools: list) -> object | None:
        """把 FunctionTool 列表包装成 ToolSet（内层代理的 tools= 实参）。"""
        from astrbot.core.agent.tool import ToolSet  # noqa: PLC0415 — 延迟导入

        return ToolSet(tools=tools)

    def _rebuild_browser_toolset(self) -> None:
        """（重）建内层工具集：``INNER_TOOLS`` 的 5 个 FunctionTool。

        v1.7.5 起工具面**固定**（不随搜索模式变化）：搜索由 ``browse_search``
        一个工具统一承接，因此这里只在 ``initialize()`` 调一次。
        """
        self._browser_tools = [_make_browser_tool(self, s) for s in INNER_TOOLS]
        # R4：tools.narrow_tools 承诺的三件事 —— 构造断言 + 运行期过滤 + WARN。
        # 关掉时三件都不做（排障用：允许临时多挂工具再看模型行为）。
        if bool(getattr(self, "narrow_tools", True)):
            names = [str(getattr(tool, "name", "")) for tool in self._browser_tools]
            missing = [name for name in INNER_TOOL_NAMES if name not in names]
            extra = [name for name in names if name not in INNER_TOOL_NAMES]
            if missing or extra:
                logger.warning(
                    f"[{self.metadata_name}] 内层工具面与 INNER_TOOL_NAMES 不一致："
                    f"缺 {missing} 多 {extra}（已按窄口径过滤）"
                )
            self._browser_tools = [
                tool for tool in self._browser_tools
                if str(getattr(tool, "name", "")) in INNER_TOOL_NAMES
            ]
        self._browser_toolset = self._build_toolset(self._browser_tools)


    def _build_subagent_instruction(self, perception: str | None = None) -> str:
        """生成内层指令的 L2+L3 段（工具面 + 感知契约 + 动作契约）。

        ``perception`` 只为兼容旧调用点保留（v1.7.5 不再有感知档位，传什么都忽略）。

        三段全部**由事实源生成**，不手写第二份：
        - L2 感知契约：``core/a11y`` 的观测块格式（``【观测 #n】`` + `- [role] name`）；
        - L3 动作契约：``ACTION_SPEC`` 的动作目录 + 两个逐字示例 + 索引口径；
        - 工具面：5 个内层工具名（``INNER_TOOL_NAMES``）。
        """
        budget = int(self.total_budget_s)
        return (
            "你是网页浏览器子代理（browser_agent），在真实浏览器里完成网页任务。\n"
            f"可用工具（{len(INNER_TOOL_NAMES)} 个）：{'、'.join(INNER_TOOL_NAMES)}。\n"
            "browse_observe 看页面（structure/text/links/full）；browse_act 执行一个动作；"
            "browse_screenshot 是看图兜底（SoM 编号）；browse_media 取图片/视频；"
            "browse_search 做检索（open_page=true 才开页面）。\n"
            "感知契约：每步之后返回的【观测 #n】首行是 URL/标题/视口/滚动位置/节点数，"
            "节点行格式 `- [role] 名称`（带 value/checked/expanded/disabled 状态）。"
            "【观测】是你唯一的状态来源，不要臆造页面上没出现过的元素。\n"
            "动作契约（每步只输出一个动作）：\n"
            + am.render_action_catalog()
            + "\n示例："
            '{"action":"click","target":{"role":"button","name":"提交"}}；'
            '{"action":"type","target":{"label":"搜索"},"value":"北京 天气","submit":true}。\n'
            "索引口径：元素 index 从 0 起算；标签页 index 从 1 起算。\n"
            f"预算：整任务墙钟 {budget} 秒（剩余时间会以【预算】行提示，快用完时收尾）。"
        )


    # ------------------------------------------------------------
    # 内层人格三档与分层提示词（v1.7.0，规格 §5）
    # ------------------------------------------------------------

    def _persona_prompt_by_id(self, persona_id: str | None) -> str:
        """按 persona_id 直接取人格正文（``current`` 档的**主路径**，v1.7.0 repair）。

        为什么不再靠切 ``req.system_prompt``：核心在同一个人格块后面紧接着 append
        skills 段（``astr_main_agent.py:629-642``），文本切分要么带上尾巴、要么
        依赖"下一个标题行"这种脆弱约定。``persona_manager.get_persona_v3_by_id``
        是**同步**的纯查表（``persona_mgr.py:73``），拿到 prompt 就是 prompt。

        取不到（无管理器 / id 为空 / 表里没有）返回空串 ⇒ 调用方回落文本切分。
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return ""
        pm = getattr(self.context, "persona_manager", None)
        getter = getattr(pm, "get_persona_v3_by_id", None)
        if not callable(getter):
            return ""
        try:
            persona = getter(pid)
        except Exception as e:  # noqa: BLE001 — 查表失败只影响主路径
            logger.debug(f"[{self.metadata_name}] get_persona_v3_by_id 失败: {e}")
            return ""
        prompt = ""
        if isinstance(persona, dict):
            prompt = str(persona.get("prompt") or "")
        else:
            prompt = str(getattr(persona, "prompt", "") or "")
        return prompt.strip()

    def _remember_persona(
        self, umo: str, system_prompt: str | None, persona_id: str | None = None
    ) -> str:
        """截留"本轮真实生效的人格"并缓存（``current`` 档的主/次路径合体）。

        两条路径，**顺序不可换**：

        1. **主路径**：``persona_id`` → ``persona_manager.get_persona_v3_by_id``
           直接取 prompt（无文本切分、无尾巴）。``req.conversation.persona_id``
           就是本轮真正生效的那条人格（核心按"会话 > agent_runner 默认"解析完
           才写进 req）。
        2. **次路径**：从 ``system_prompt`` 里切 ``# Persona Instructions`` 块，
           并截界到下一个标题行之前（见 ``core.agent.extract_persona_text``）。

        只缓存文本：**不落日志、不写文件**（规格 §9.4 的禁止清单第一条）。
        每 umo 一条、每次请求覆盖、TTL 30min（超时视为未命中 ⇒ 走运行期次路径）。
        返回拿到的人格正文（都没有返回空串）。
        """
        text = self._persona_prompt_by_id(persona_id)
        if not text:
            text = agent_core.extract_persona_text(system_prompt)
        if not text or not umo:
            return text
        cache = self._persona_store()
        cache[umo] = (time.monotonic(), persona_id, text)
        return text

    def _persona_store(self) -> dict:
        """人格缓存字典（按需建：手工装配的实例没有 __init__ 跑过的字段）。"""
        cache = getattr(self, "_persona_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._persona_cache = cache
        return cache

    def _cached_persona(self, umo: str) -> str:
        """读人格缓存（超过 30min 视为未命中并丢弃该条）。"""
        cache = self._persona_store()
        item = cache.get(umo)
        if not item:
            return ""
        ts, _pid, text = item
        if time.monotonic() - ts > _PERSONA_CACHE_TTL:
            cache.pop(umo, None)
            return ""
        return text

    async def _current_persona_text(self, event: AstrMessageEvent | None) -> str:
        """``persona_mode=current`` 取"当前生效人格提示词"：三条路径 + 可见降级。

        1. **主路径**：on_llm_request 缓存（``_remember_persona`` 在钩子里写入）。
           它的顺序保证见 ``core.agent.extract_persona_text`` 的注释
           （核心先拼人格、后触发 OnLLMRequestEvent）。
        2. **次路径**：``persona_manager.resolve_selected_persona``（拿不到会话
           persona_id 时走这条，核心的规则是"会话规则 > agent_runner 默认"，
           因此**必须**记 WARNING —— 解析出来的可能与会话自己的 persona 不同）。
        3. **末路径**：``get_default_persona_v3(umo)``（默认人格）。
        4. 三条都拿不到 ⇒ 空串 + WARNING（等价 default，**可见**降级）。
        """
        umo = self._umo_of(event) if event is not None else ""
        cached = self._cached_persona(umo)
        if cached:
            return cached
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            logger.warning(
                f"[{self.metadata_name}] persona_mode=current 无法获取当前人格"
                "提示词（无 persona_manager），已回落 default 档"
            )
            return ""
        resolver = getattr(pm, "resolve_selected_persona", None)
        if callable(resolver):
            try:
                platform = ""
                try:
                    platform = event.get_platform_name() if event is not None else ""
                except Exception:  # noqa: BLE001 — 桩事件没有该方法
                    platform = ""
                pid, persona, _forced, _webchat = await resolver(
                    umo=umo,
                    conversation_persona_id=None,
                    platform_name=platform,
                    provider_settings=None,
                )
                text = str((persona or {}).get("prompt") or "").strip()
                if text:
                    logger.warning(
                        f"[{self.metadata_name}] persona_mode=current 未捕获到"
                        f"当前请求的人格块，改用 persona_manager 解析（pid={pid}）"
                    )
                    self._persona_store()[umo] = (time.monotonic(), pid, text)
                    return text
            except Exception as e:  # noqa: BLE001 — 次路径失败继续走末路径
                logger.debug(
                    f"[{self.metadata_name}] resolve_selected_persona 失败: {e}"
                )
        getter = getattr(pm, "get_default_persona_v3", None)
        if callable(getter):
            try:
                persona = await getter(umo)
                text = str((persona or {}).get("prompt") or "").strip()
                if text:
                    logger.warning(
                        f"[{self.metadata_name}] persona_mode=current 未捕获到"
                        "当前请求的人格块，改用默认人格（get_default_persona_v3）"
                    )
                    return text
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    f"[{self.metadata_name}] get_default_persona_v3 失败: {e}"
                )
        logger.warning(
            f"[{self.metadata_name}] persona_mode=current 无法获取当前人格提示词，"
            "已回落 default 档"
        )
        return ""

    async def _compose_inner_prompt(
        self,
        perception: str | None = None,  # 兼容旧调用点：v1.7.5 已无"感知模式"档位
        *,
        base: str | None = None,
        event: AstrMessageEvent | None = None,
    ) -> str:
        """合成内层 system_prompt（六段，顺序写死在 ``core.agent`` 里）。

        - L0 红线 / L4 验证硬话 / L5 输出契约：常量（``INNER_RED_LINES`` /
          ``INNER_RULES`` / ``INNER_OUTPUT_CONTRACT``）；
        - L1 人格：``default`` 空 / ``current`` 当前会话人格 / ``custom`` 配置文本；
        - L2+L3：``base``（``_build_subagent_instruction`` 生成）。

        分层是**结构性**保证：人格层被包裹在红线之后、工具面之前，改不动工具清单。
        """
        base_text = base if base is not None else self._build_subagent_instruction()
        mode = str(self.agent_persona_mode or "default")
        persona_text = ""
        if mode == "current":
            persona_text = await self._current_persona_text(event)
            if not persona_text:
                logger.warning(
                    f"[{self.metadata_name}] persona_mode=current 无可用人格文本，"
                    "本次按 default 档（仅工具层）执行"
                )
        elif mode == "custom":
            persona_text = str(self.agent_persona_prompt or "").strip()
            if not persona_text:
                logger.warning(
                    f"[{self.metadata_name}] persona_mode=custom 但 model.persona_prompt "
                    "为空，本次按 default 档执行"
                )
        return agent_core.compose_inner_prompt(
            base=base_text, persona_mode=mode, persona_text=persona_text
        )


    def _cleanup_cache(self) -> int:
        """清理超过 ``resources.cache_days`` 天的媒体/截图缓存文件，返回删除数。

        只遍历两个基目录的**顶层文件**（``iterdir``，不递归）：这两处只有本插件
        自己写的平铺文件，把删除语义改成递归会误删运维手工放进子目录的东西。
        ``cache_days <= 0`` ⇒ 不清理（避免 0 被误当作"立即清空"）。
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
            for path in base.iterdir():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except Exception as e:  # noqa: BLE001 — 单文件失败不影响其余
                    logger.debug(f"清理缓存文件失败 {path}: {e}")
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

    # ------------------------------------------------------------
    # 内层 provider（v1.7.0，规格 §3）：唯一解析点 + 面板候选同步
    # ------------------------------------------------------------

    def _live_agent_provider_id(self) -> str:
        """实时读取 ``model.provider_id``（面板保存即生效，不必等重载）。

        空串 = **继承当前会话模型**（默认），非空 = 运营方显式指定。
        v6 把"识图模型"与"子代理模型"合并成这一个键（caption 兜底已删）。
        """
        view = ConfigView(self.config)
        value, found = view.lookup("model.provider_id")
        if found:
            return str(value or "").strip()
        return str(getattr(self, "agent_provider_id", "") or "").strip()

    def _collect_provider_configs(self) -> list[tuple[str, object]]:
        """``[(provider_id, provider_config)]``：面板候选与能力判定共用一份数据。

        数据源同 ``_collect_provider_ids``（优先 ``provider_insts``，即仅聊天
        Provider；为空时才退 ``inst_map``）。``provider_config`` 取不到时给
        ``{}`` —— 调用方按"未声明 modalities"（fail-open）处理。
        """
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return []
        out: list[tuple[str, object]] = []
        for inst in getattr(pm, "provider_insts", None) or []:
            pid = ""
            try:
                pid = str(inst.meta().id)
            except Exception:  # noqa: BLE001 — meta() 异常时尝试属性兜底
                pid = str(getattr(inst, "provider_id", "") or "")
            if not pid:
                continue
            cfg = getattr(inst, "provider_config", None)
            out.append((pid, cfg if isinstance(cfg, dict) else {}))
        if out:
            seen: set[str] = set()
            deduped: list[tuple[str, object]] = []
            for pid, cfg in out:
                if pid in seen:
                    continue
                seen.add(pid)
                deduped.append((pid, cfg))
            return deduped
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict):
            for pid, inst in inst_map.items():
                if not pid:
                    continue
                cfg = getattr(inst, "provider_config", None)
                out.append((str(pid), cfg if isinstance(cfg, dict) else {}))
        return out

    def _provider_config_of(self, provider_id: str) -> object:
        """查一个 provider 的 ``provider_config``（查不到返回 ``{}``）。"""
        target = str(provider_id or "")
        if not target:
            return {}
        for pid, cfg in self._collect_provider_configs():
            if pid == target:
                return cfg
        pm = getattr(self.context, "provider_manager", None)
        inst_map = getattr(pm, "inst_map", None) if pm is not None else None
        if isinstance(inst_map, dict):
            inst = inst_map.get(target)
            if inst is not None:
                cfg = getattr(inst, "provider_config", None)
                return cfg if isinstance(cfg, dict) else {}
        return {}

    def _sync_agent_provider_options(self) -> list[str]:
        """把「多模态 + 可函数调用」的 provider 同步到面板下拉（内存 schema）。

        为什么只列这两种：内层子代理要**看图**（影像原生）与**调工具**。
        列出纯文本模型会让运营方选到一个必然走 caption 兜底甚至被拒的模型，
        那是把"运行期才发现的错"推给运行期（规格 §3.1）。

        与面板其它下拉同步同一做法：Dashboard 每次请求直接
        读 ``plugin_md.config.schema``（同一对象），就地改 ``options`` 即生效；
        ``""`` 空选项恒保留（= 继承当前会话），default 不在候选里时归零。
        """
        options = [""] + agent_core.agent_provider_candidates(
            self._collect_provider_configs()
        )
        options = list(dict.fromkeys(options))
        schema = getattr(self.config, "schema", None)
        if isinstance(schema, dict):
            item = schema_leaf(schema, "model.provider_id")
            if isinstance(item, dict):
                if item.get("options") != options:
                    item["options"] = options
                if item.get("default") not in options:
                    item["default"] = ""
        return options

    def _start_agent_provider_sync_task(self) -> None:
        """启动内层 provider 下拉同步的启动期兜底任务（同识图那套，5s×120s）。

        AstrBot 的启动顺序是「插件加载先于 provider 初始化」，因此
        ``initialize()`` 里的首次同步可能拿到空列表；本任务轮询到 provider
        就绪后同步一次即退出。
        """
        if self._agent_sync_task is not None and not self._agent_sync_task.done():
            return

        async def _loop() -> None:
            deadline = time.monotonic() + 120
            while True:
                if self._collect_provider_configs():
                    options = self._sync_agent_provider_options()
                    logger.info(
                        f"[{self.metadata_name}] 内层模型下拉已同步 "
                        f"({len(options) - 1} 个多模态 Provider)"
                    )
                    return
                if time.monotonic() > deadline:
                    return
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise

        self._agent_sync_task = asyncio.create_task(
            _loop(), name="browser-agent-provider-sync"
        )

    def _stop_agent_provider_sync_task(self) -> None:
        """停止内层 provider 下拉同步任务（幂等）。"""
        task = self._agent_sync_task
        if task is None:
            return
        self._agent_sync_task = None
        if not task.done():
            task.cancel()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._await_task(task))  # 不阻塞 terminate
            except Exception:  # noqa: BLE001
                pass

    async def _current_chat_provider_id(self, event: AstrMessageEvent | None) -> str:
        """**唯一**的「当前会话聊天 provider」解析点（v1.7.0 收口）。

        为什么要收口：v1.6.0 有两处各自调 ``context.get_current_chat_provider_id``
        （识图回退、browse_web 入口）。同一个语义两个调用点 ⇒ 一处加了缓存/
        兜底另一处就漂移；守卫 ``grep -c`` 也数不清"到底有几个解析点"。
        现在两处都走这里，``get_current_chat_provider_id`` 在 main.py 只出现一次。
        """
        umo = self._umo_of(event) if event is not None else ""
        return await self.context.get_current_chat_provider_id(umo=umo)

    async def _resolve_agent_provider(
        self, event: AstrMessageEvent | None
    ) -> agent_core.ProviderDecision:
        """解析内层 provider（规格 §3.3 八格），失败**不回落**当前会话模型。

        决策本身是纯函数（``core.agent.decide_provider``）：这里只负责取三件
        事实（配置值 / 实际 id / 是否已加载 / provider_config），然后把日志按
        决策给的级别打出去。取不到模型（继承档）时表现为一条错误文本 ——
        前台/后台各自的投递语义由调用方决定（规格 §3.4）。
        """
        configured = self._live_agent_provider_id()
        resolved = configured
        if not resolved:
            try:
                resolved = await self._current_chat_provider_id(event)
            except Exception as e:  # noqa: BLE001 — ProviderNotFoundError 等
                logger.warning(
                    f"[{self.metadata_name}] 获取当前对话 Provider 失败: {e}"
                )
                resolved = ""
        loaded = self._provider_loaded(resolved) if configured else True
        config = self._provider_config_of(resolved) if resolved else {}
        decision = agent_core.decide_provider(
            configured=configured,
            resolved=resolved,
            loaded=loaded,
            provider_config=config,
            persona_mode=self.agent_persona_mode,
        )
        # 先打本格的诊断行（ERROR/WARNING/INFO/DEBUG 随格而定），再打"内层模型
        # 选定"那一行 INFO —— 后者是**每次任务一行**的固定口径
        # （``内层模型= / 影像原生= / 人格档=``，规格 §1.2.5），故障排查按它 grep。
        if decision.log_text:
            level = decision.log_level
            log_line = f"[{self.metadata_name}] {decision.log_text}"
            if level == "error":
                logger.error(log_line)
            elif level == "warning":
                logger.warning(log_line)
            elif level == "info":
                logger.info(log_line)
            else:
                logger.debug(log_line)
        if decision.selection_log and decision.selection_log != decision.log_text:
            logger.info(f"[{self.metadata_name}] {decision.selection_log}")
        if decision.ok and not decision.multimodal:
            # v1.7.5：caption 兜底整条删除 ⇒ 非多模态模型**只能**用结构视图，
            # 截图会返回【不可用】。运营方必须能从日志看到这个事实。
            logger.info(
                f"[{self.metadata_name}] 内层模型非多模态：结构视图照常，"
                f"browse_screenshot 将返回【不可用】（内层模型={decision.provider_id}）"
            )
        return decision

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
        self._stop_agent_provider_sync_task()
        # 后台任务（v1.5.0 路线 D）：先取消在跑任务并等一小段——否则浏览器关了
        # 它们才抛错，终态消息面对的是已销毁的浏览器/页面，重载还多一批悬挂协程。
        # 后台任务（v1.7.5）：取消全部在跑任务（协作式取消 + 有限等待），
        # 终态由 watchdog 兜底补投；先做这一步，否则浏览器关了它们才抛错。
        try:
            cancelled = await self._watchdog.shutdown(
                timeout=_BACKGROUND_TERMINATE_TIMEOUT, reason="plugin_reload"
            )
            if cancelled:
                logger.info(f"{f'[{self.metadata_name}]'} 已取消 {cancelled} 个后台任务")
        except Exception as e:  # noqa: BLE001 — 重载路径不许因取消失败而悬挂
            logger.warning(f"[{self.metadata_name}] 后台任务取消异常: {e}")
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
            # 感知层 CDP 会话池与任务状态表清空（防重载后残留旧页会话）。
            self._cdp_pool.clear()
            self._tasks.clear()
            # 人格正文缓存清空（v1.7.0 repair，评审 R8）：人格是**运营方写的文本**，
            # 与聊天正文同级的隐私内容，重载/禁用后不该留在内存里。
            self._persona_store().clear()
            # 搜索服务缓存/冷却清空（密钥随服务实例一起释放）。
            if self._search_service is not None:
                self._search_service.clear_cache()
            self._search_service = None
            self._search_service_signature = None
            # 后台任务登记表兜底清空：shutdown 已解占，这里再清一次是防
            # "表非空但无在跑任务"那类残留把会话永久锁在单飞里。
            try:
                self._watchdog.registry.clear()
            except Exception:  # noqa: BLE001
                pass
        logger.info(f"{tag} 已终止")

    # ================================================================
    # 后台任务（v1.7.5：插件内闭环，与核心后台机制无耦合）
    # ================================================================

    #: 视频类媒体扩展名（``browse_sniff_media`` 会把视频交给 ``_emit_media``，
    #: 统一按 Image 发会失败，必须按扩展名分流 —— 见 ``_deliver_terminal``）。
    _VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv")

    #: 终态链最多携带的媒体附件数（spec §14-F-4：``progress["artifacts"]`` ≤12 条）。
    _MAX_TERMINAL_ARTIFACTS = 12

    def _background_enabled_for(self, tool_name: str) -> bool:
        """该工具本次是否走 **plugin 引擎**的后台路径（v1.5.0 语义 + 一条新闸）。

        - ``off``（默认）：三个工具一律前台同步，行为与 v1.4.0 完全一致；
        - ``browse_only``：只后台化 ``browse_web``——它是唯一 68s 量级的长
          任务（一次调用跑整套子 Agent），也是唯一"用户要等一整段多步浏览"
          的入口；
        - ``all``：三个工具都后台化（``browser_web_search`` 常态 1-3s，后台化
          等于多一次投递，只在明确要求"任何工具都不阻塞会话"时才开）。

        v1.7.5：平台原生后台（core 引擎）已整体退役（U1–U4 在核心侧，插件
        无法闭环），因此这条判断只看 ``tools.background_mode``。
        """
        mode = getattr(self, "background_mode", "off")
        if mode == "all":
            return True
        if mode == "browse_only":
            return tool_name == _BROWSE_WEB_TOOL_NAME
        return False

    # ----------------------------------------------------------------
    # 后台引擎（v1.6.0）：置标志 / 重放 / 能力探测 / 降级
    # ----------------------------------------------------------------

    # ----------------------------------------------------------------
    # C 模式执行体与媒体发布（媒体边界 A：只给路径，禁止 event.send）
    # ----------------------------------------------------------------

    @staticmethod
    def _umo_digest(umo: str) -> str:
        """日志用的会话摘要：只记 8 位 md5，不把原始 UMO 写进后台任务日志行。"""
        return hashlib.md5(str(umo or "").encode("utf-8")).hexdigest()[:8]

    async def _submit_background(
        self,
        *,
        event: AstrMessageEvent,
        tool_name: str,
        runner: Callable[[dict], Awaitable[str]],
        task_text: str = "",
    ) -> str:
        """提交后台任务：三闸（单飞/并发/去重）全在 watchdog 里，返回受理文案（spec §6）。

        预算口径：``resources.total_budget_s`` 一个值同时供前台 ``wait_for`` 与后台预算；
        ``tools.background_max_chars`` 只截断终态；去重两把钥匙（v1.7.6）：触发消息 ID
        （"同一句话"）+ ``task_key`` 内容指纹（"同一措辞"）。

        **事件生命周期纪律（§6 C1/C3）**：这里只做两件与事件相关的事 —— 抓一次
        :class:`core.active_send.DeliverySnapshot`、抓 ``event.send`` 当平台回退句柄；
        此后后台只读快照，投递层不持有事件、不调 ``get_group``/``bot.api.*``。
        """
        handle = self._watchdog.submit(
            runner=self._artifact_collecting(runner),
            deliver=self._terminal_deliverer(event),
            umo=self._umo_of(event),
            tool=tool_name,
            task_text=task_text,
            budget_s=float(self.total_budget_s),
            max_chars=int(self.background_max_chars),
            dedupe_window_s=float(self.dedupe_window_s),
            max_parallel=int(self.max_parallel),
            trigger_message_id=self._trigger_message_id(event),
        )
        kind = "duplicate_trigger" if handle.decision.trigger_hit else handle.decision.kind
        logger.info(
            f"[{self.metadata_name}] 后台提交 tool={tool_name} kind={kind} "
            f"task_id={handle.task_id or '-'} "
            f"umo={self._umo_digest(self._umo_of(event))} "
            f"budget={float(self.total_budget_s):.0f}s"
        )
        return handle.reply_text

    @staticmethod
    def _trigger_message_id(event: AstrMessageEvent) -> Optional[str]:
        """触发消息 ID（去重键，spec §7.2）：取不到给 ``None``（退回 ``task_key``）。"""
        message_obj = getattr(event, "message_obj", None)
        raw = str(getattr(message_obj, "message_id", "") or "").strip()
        return raw or None

    def _terminal_deliverer(
        self, event: AstrMessageEvent
    ) -> Callable[..., Awaitable[Any]]:
        """把投递所需的一切固定成回调：快照 + 平台回退句柄（§6 C1/C3）。

        回调里**没有** ``event``：后台拿不到事件对象，就不可能误用它的平台能力。
        唯一跨轮持有的是 ``fallback_send``（``event.send`` 绑定方法），只在
        ``_deliver_terminal`` 里、L1 不可用时用一次。
        """
        snapshot = active_send.snapshot_from_event(event, umo=self._umo_of(event))
        return partial(
            self._deliver_terminal, snapshot=snapshot, fallback_send=event.send
        )

    @classmethod
    def _artifact_collecting(cls, runner: Callable[[dict], Awaitable[str]]):
        """执行体包一层 artifact 收集（F-G4）：后台媒体只登记路径，不发。

        路径写进 ``progress``（= job 快照），**不靠 ContextVar 跨任务隐式传递**；
        取消/异常同样落盘（``finally``），中断前的截图也会随终态一条发出。
        """

        async def run(progress: dict) -> str:
            with artifact_sink() as bucket:
                try:
                    return await runner(progress)
                finally:
                    if bucket:
                        progress["artifacts"] = [
                            str(path) for path in bucket
                        ][: cls._MAX_TERMINAL_ARTIFACTS]

        return run

    def _terminal_artifacts(self, job) -> list:
        """终态附件组件：从 job 快照的 ``artifacts`` 造（缺失/构造失败只跳过）。"""
        components: list = []
        progress = getattr(job, "progress", None) or {}
        for path in list(progress.get("artifacts") or []):
            if not os.path.isfile(str(path)):
                continue
            component = self._media_component(path)
            if component is not None:
                components.append(component)
        return components

    async def _deliver_terminal(
        self, job, status: str, body: str, *, snapshot, fallback_send
    ) -> DeliveryReceipt:
        """终态投递：**闸 → 适配器（合成事件 + 钩子重放 + L1/回退）→ 真账**（v1.7.6）。

        顺序即契约（spec §3.2）：① ``note_delivery(terminal)`` 先取额度（取不到直接
        返回：不建事件、不重放钩子、不发送）；② ``core.active_send.deliver`` 重放
        ``on_decorating_result`` → ``Context.send_message``（失败/不可用才回退）→
        条件重放 ``on_after_message_sent``；③ 返回 ``DeliveryOutcome`` 交给后台记账。

        旧契约"终态只做 ``event.send``"已作废（spec §16 取代声明）：``event.send``
        只在**平台回退**分支用一次（``fallback_send``，§6 C3 唯一例外）；本函数
        **没有** ``event`` 参数 —— 投递层只读提交时快照（§6 C1/C2）。
        """
        granted = self._watchdog.registry.note_delivery(
            job.task_id, channel=DELIVERY_CHANNEL_TERMINAL
        )
        if not granted:
            logger.warning(
                f"[{self.metadata_name}] 后台终态额度已用尽 task_id={job.task_id}"
                "（终态只允许一条，本次不再发送）"
            )
            return DeliveryReceipt(
                attempted=False,
                error="DeliveryBlocked",
                sent_via=active_send.CHANNEL_NONE,
                task_id=str(job.task_id),
            )
        chain: list = [Plain(body), *self._terminal_artifacts(job)]
        return await active_send.deliver(
            self.context,
            snapshot,
            chain=chain,
            fallback_send=fallback_send,
            log=logger,
            task_id=str(job.task_id),
            status=status,
        )

    def _media_component(self, path: str):
        """媒体路径 → 消息链组件（视频走 ``Video``，其余走 ``Image``；失败返回 None）。"""
        try:
            if str(path).lower().endswith(_VIDEO_SUFFIXES):
                return Video.fromFileSystem(path)
            return Image.fromFileSystem(path)
        except Exception as e:  # noqa: BLE001 — 单个附件失败不该拖垮整条终态
            logger.warning(f"[{self.metadata_name}] 终态附件构造失败: {e}")
            return None

    # ================================================================
    # 浏览器子代理入口（工具化子代理：browse_web）
    # ================================================================

    def _screenshot_allowed_for(self, umo: str) -> bool:
        """本次任务是否允许截图（``perception.rules`` 命中第一条即生效）。

        R4：v5 的"按会话设定感知方式"被整段删掉后又保留了键与迁移，等于承诺了
        一个不存在的功能。这里按 v5 的优先级恢复最小闭环：规则列表按顺序匹配
        ``umo`` 子串（大小写不敏感），命中第一条后看它的 ``perception``：
        ``text`` ⇒ 不开截图（纯结构视图）；其它值 ⇒ 开。没有命中就用全局
        ``perception.screenshot_fallback``。
        """
        target = str(umo or "").casefold()
        for rule in list(getattr(self, "perception_rules", []) or []):
            if not isinstance(rule, dict):
                continue
            needle = str(
                rule.get("umo") or rule.get("match") or rule.get("session") or ""
            ).casefold()
            if needle and needle in target:
                mode = str(rule.get("perception") or "").strip().lower()
                return mode != "text"
        return bool(getattr(self, "enable_screenshot", True))

    def _missing_url_hint(self, event: AstrMessageEvent, task_input: str) -> str:
        """无起始页之后的准入判定：没有目标网址就**要网址**，不静默开默认站。

        放行条件（满足其一）：① 任务文本自带网址/域名；② 本会话已有一个
        **活着的**页面（URL 不是 ``about:*``）——"在这个页面上继续操作"是合法任务，
        页面来自上一轮（此时不允许再落到任何"默认站"）。
        """
        if _has_explicit_url(task_input):
            return ""
        page = None
        if self.sessions is not None:
            try:
                page = self.sessions.current_page(self._umo_of(event))
            except Exception:  # noqa: BLE001 — 探针失败按"没有页面"处理
                page = None
        url = str(getattr(page, "url", "") or "") if page is not None else ""
        if url and not url.startswith("about:"):
            return ""
        logger.info(
            f"[{self.metadata_name}] 拒绝无网址的浏览任务（无起始页，不打开默认站）"
        )
        return _NEED_URL_TEXT

    def _busy_browse_reason(self, event: AstrMessageEvent) -> str:
        """同会话已有浏览任务时返回拒绝文案（R21 的单飞真闸）。

        两个来源都算"在跑"：前台 ``_fg_inflight`` 计数、后台 watchdog 登记表。
        这样"后台 + 前台"与"两个前台"都不会互相踩页面、抢 ref 与 mark 纪元。
        """
        umo = self._umo_of(event)
        inflight = getattr(self, "_fg_inflight", None)
        if isinstance(inflight, dict) and int(inflight.get(umo, 0) or 0) > 0:
            return (
                "【拒绝】同一会话已有一个浏览任务在跑（前台）：等它结束再提交，"
                "或把它合并成一次任务。"
            )
        watchdog = getattr(self, "_watchdog", None)
        if watchdog is not None:
            try:
                active = watchdog.registry.active_task_id(umo)
            except Exception:  # noqa: BLE001 — 查询失败不拦路
                active = None
            if active:
                return (
                    f"【拒绝】同一会话已有一个后台任务在跑（task_id={active}）："
                    "等它结束再提交（结果会单独发一条消息）。"
                )
        return ""

    @filter.llm_tool(name="browse_web")
    async def browse_web(self, event: AstrMessageEvent, input: str = "") -> str:
        """委托浏览器子代理完成网页任务（打开网页、页面交互、提取内容或截图）。

        Args:
            input(string): 给浏览器子代理的任务描述（打开什么网址、查找什么、
                是否需要点击/滚动/填表等交互）。

        三条与顺序相关的约定：① 本入口**不持** per-umo 锁（``asyncio.Lock`` 不可
        重入，持锁会让内层工具再 acquire 时死锁）；② 后台分支必须排在热更新 /
        鉴权 / 禁词之后（否则"被拒绝"会变成异步失败消息）；③ 返回体 = 不可信前缀
        + 结论 + 台账（前缀由执行体在源侧加，这里不再 wrap）。
        """
        try:
            self._refresh_config()
            logger.debug(f"[{self.metadata_name}] browse_web 进入，input={input!r}")
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"
            banned = self._check_banned(input)
            if banned:
                return f"【拒绝】任务描述包含违禁内容：{banned}"
            need_url = self._missing_url_hint(event, input)
            if need_url:
                return need_url
            # R21：前台并发 + 按任务换页会互相踩页面（后到的关掉先到的）。
            # 规格 §10.2 允许"不拦只 WARN"，但那样两个任务会抢同一张页面，
            # 因此把"同 UMO 只允许一个 browse_web（含后台）"做成**真闸**。
            busy = self._busy_browse_reason(event)
            if busy:
                return busy
            if self._background_enabled_for(_BROWSE_WEB_TOOL_NAME):
                return await self._submit_background(
                    event=event,
                    tool_name=_BROWSE_WEB_TOOL_NAME,
                    runner=lambda progress: self._browse_web_run(
                        event,
                        input,
                        progress=progress,
                        background=True,
                    ),
                    task_text=input,
                )
            meta: dict = {}
            body = await self._browse_web_run(event, input, meta=meta)
            ledger_block = agent_core.render_ledger(
                meta.get("ledger"), int(self.agent_ledger_max_lines)
            )
            parts = [body]
            if ledger_block:
                parts.append(ledger_block)
            return "\n\n".join(part for part in parts if part)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.metadata_name}] browse_web 执行异常: {e}")
            return f"【错误】浏览器子代理执行失败：{e}"

    async def _browse_web_run(
        self,
        event: AstrMessageEvent,
        task_input: str,
        perception_mode: str | None = None,  # 兼容旧调用点（v1.7.5 已无感知档位）
        meta: dict | None = None,
        progress: dict | None = None,
        background: bool = False,
    ) -> str:
        """browse_web 的执行体：provider 决策 + 内层代理 + 台账（前台/后台共用）。

        预算口径（v1.7.5）：只有**一条墙钟**（``resources.total_budget_s``）——
        ``asyncio.wait_for`` 包住整个 ``tool_loop_agent``，到点取消；步数不再
        是预算口径（v1.7.0 的步数键已随配置 v6 删除），
        ``max_steps`` 只作核心 API 的兜底上限传入。

        ``meta`` 是结构化回传通道：``plugin_failure``（失败是不是插件自述的）
        与 ``ledger``（台账对象），避免靠文本前缀猜语义。
        """
        if isinstance(perception_mode, dict) and meta is None:
            # 旧调用点把 meta 作为第 3 个位置参数传进来（v1.7.0 是 perception_mode）
            meta = perception_mode
        if isinstance(meta, dict) and progress is None:
            progress = meta  # 兼容：meta 同时充当 progress 通道
        umo = self._umo_of(event)
        inflight = getattr(self, "_fg_inflight", None)
        if not isinstance(inflight, dict):
            inflight = {}
            self._fg_inflight = inflight
        depth = inflight.get(umo, 0)
        if depth:
            logger.warning(
                f"[{self.metadata_name}] 同会话并发 browse_web（n={depth + 1}，"
                f"umo={self._umo_digest(umo)}）——前台不单飞，仅观测"
            )
        inflight[umo] = depth + 1
        budget = float(self.total_budget_s)
        state = self.reset_task(umo, budget_s=budget)
        try:
            if getattr(self, "_browser_toolset", None) is None:
                self._rebuild_browser_toolset()  # 手工装配实例的兜底
            decision = await self._resolve_agent_provider(event)
            if not decision.ok:
                if meta is not None:
                    meta["plugin_failure"] = True
                    meta["ledger"] = None
                return decision.error
            instruction = await self._compose_inner_prompt(event=event)
            run = agent_core.AgentRun(
                provider_id=decision.provider_id,
                native_image=decision.native_image,
                result_max_chars=int(self.agent_result_max_chars),
                image_max_side=int(self.agent_image_max_side),
                image_quality=int(self.agent_image_quality),
                char_budget=0,  # v1.7.5：墙钟预算取代"步数 × 结果上限"的字符预算
                redact=_redact_url,
            )
            plugin_failure = False
            with agent_core.run_scope(run) as scoped:
                try:
                    resp = await asyncio.wait_for(
                        self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=decision.provider_id,
                            prompt=task_input or "请根据用户请求浏览网页并返回结果",
                            system_prompt=instruction,
                            tools=self._browser_toolset,
                            # 步数护栏按 total_budget_s 派生（spec §7.4-9）；预算仍是墙钟
                            max_steps=derive_max_steps(self.total_budget_s),
                            tool_call_timeout=int(self.action_timeout_s * 6),
                        ),
                        timeout=budget,
                    )
                    text = getattr(resp, "completion_text", "") or ""
                    text = text or "（浏览器子代理未返回内容）"
                except asyncio.TimeoutError:
                    plugin_failure = True
                    steps = scoped.ledger.steps
                    hint = (
                        f"（已完成 {steps} 步，见台账）"
                        if steps
                        else "（尚未产生任何内层工具调用）"
                    )
                    text = f"【错误】浏览任务超过 {budget:.0f} 秒硬上限被中止{hint}"
                    if isinstance(progress, dict):
                        # R11：超时也要把半成品、步数与台账交给终态（§6.2/§6.7）
                        progress["partial"] = text
                        progress["steps"] = int(steps)
                        progress["ledger"] = agent_core.render_ledger(
                            scoped.ledger, int(self.agent_ledger_max_lines)
                        )
                    if background:
                        # R22：本工具自己的超时不能被记成"完成" —— 抛出去让
                        # watchdog 的 _run 映射成 STATUS_TIMEOUT。
                        raise
                except Exception as e:  # noqa: BLE001 — 取消/浏览器已关等
                    logger.warning(
                        f"[{self.metadata_name}] browse_web 内层执行异常: {e}"
                    )
                    plugin_failure = True
                    text = f"【错误】浏览器子代理执行失败：{e}（已中止）"
                logger.debug(
                    f"[{self.metadata_name}] 台账 steps={scoped.ledger.steps} "
                    f"provider={scoped.provider_id!r}"
                )
                ledger_obj = scoped.ledger
            if meta is not None:
                meta["plugin_failure"] = plugin_failure
                meta["ledger"] = ledger_obj
        finally:
            inflight = getattr(self, "_fg_inflight", None)
            if isinstance(inflight, dict):
                left = inflight.get(umo, 1) - 1
                if left > 0:
                    inflight[umo] = left
                else:
                    inflight.pop(umo, None)
            state.deadline = 0.0
        payload = "\n\n".join(
            part for part in (decision.notice, text) if part and not plugin_failure
        ) or text
        if not plugin_failure:
            payload = agent_core.with_untrusted_prefix(payload)
        parts: list[str] = [payload]
        if meta is None:
            ledger_block = agent_core.render_ledger(
                ledger_obj, int(self.agent_ledger_max_lines)
            )
            if ledger_block:
                parts.append(ledger_block)
        logger.debug(
            f"[{self.metadata_name}] browse_web tool_loop_agent 返回，len={len(text)}"
        )
        return "\n\n".join(part for part in parts if part)
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
        """渲染本地 HTML 页面并返回**渲染后的结构视图**（本地页面预览工具）。

        用途：让调用方（主 LLM 或子代理）看到"这份 HTML 渲染出来长什么样/有什么
        元素"——用无头浏览器真实渲染（含 CSS/JS），再用 A11y 树给出结构视图 +
        正文摘要 + 截图路径。**不再走"截图 → 识图模型写描述"那条链**（v1.7.5 删）。

        三条与安全相关的约定：

        1. 路径校验**同步**拒绝（四道：白名单根 + 真实路径 + 后缀 + 存在性），
           越权路径不进入后台任务（否则会退化成一条异步失败消息）；
        2. 不进浏览会话管理器（每次独立页面、用完即关），不占浏览额度；
        3. 不进子代理（生产人格显式列了它；合并=静默剥夺能力）。

        Args:
            path(string): 本地 HTML 文件的绝对路径（仅白名单根下的 .html/.htm）。
            full_page(boolean): 是否整页长截图（默认仅首屏）。
            wait_ms(number): 页面加载后等待 JS 渲染的毫秒数（默认 500）。
            perception(string): 兼容旧参数名；``text`` 只返回文本+结构、不截图。
        """
        try:
            self._refresh_config()
            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return f"【拒绝】{deny_reason}"
            target, err = self._resolve_local_page_target(path)
            if err:
                return err
            text_only = str(perception or "").strip().lower() == "text"
            if self._background_enabled_for(_LOCAL_PAGE_TOOL_NAME):
                return await self._submit_background(
                    event=event,
                    tool_name=_LOCAL_PAGE_TOOL_NAME,
                    runner=lambda progress: self._local_page_render(
                        event, target, full_page, wait_ms, text_only, progress
                    ),
                    # R3：task_key = sha1(umo|tool|task_text) —— 不带 path 时同会话
                    # 所有本地页共用一个键，all 模式会把别人的结果投给当前用户。
                    task_text=(
                        f"{target}|full_page={full_page}|wait_ms={wait_ms}"
                        f"|perception={perception}"
                    ),
                )
            return agent_core.truncate_text(
                await self._local_page_render(
                    event, target, full_page, wait_ms, text_only, {}
                ),
                int(self.agent_result_max_chars),
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
        text_only: bool,
        progress: dict | None = None,
    ) -> str:
        """渲染段执行体（持 per-UMO 本地锁）：new_page → goto → A11y →（截图）→ close。

        ``progress``（可选 dict）：拿到正文就写 ``progress["text"]``，后台任务
        超时/失败时据此发「已得部分结果」（前台传空 dict，无副作用）。
        """
        async with self._local_lock_for(self._umo_of(event)):
            page = await self.browser.new_page()
            try:
                await page.goto(
                    target.as_uri(),
                    wait_until="domcontentloaded",
                    timeout=int(self.goto_timeout_s * 1000),
                )
                wait = self._as_int(wait_ms, 0)
                await page.wait_for_timeout(
                    max(0, min(10000, wait)) or _LOCAL_PAGE_DEFAULT_WAIT_MS
                )
                state = _TaskState()  # 一次性任务状态：不污染会话的 ref/mark 纪元
                snap = await self._snapshot(page, state)
                if progress is not None and snap.text:
                    # watchdog 的 _partial_of 只认这三个键（v1.7.5 契约）
                    progress["partial"] = snap.text
                banned = self._check_banned(snap.text or "") or self._check_banned(
                    snap.title or ""
                )
                if banned:
                    return f"【拒绝】页面内容包含违禁内容：{banned}，已拒绝输出。"
                parts = [f"【本地页面预览】{target}", f"标题：{snap.title}"]
                parts.append(
                    a11y.render_snapshot(snap, max_lines=80).split("\n", 1)[-1]
                )
                if snap.text:
                    parts.append(
                        "页面文本（前 800 字）：\n" + snap.text[:800]
                    )
                if not text_only:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    umo_hash = hashlib.md5(
                        self._umo_of(event).encode("utf-8")
                    ).hexdigest()[:8]
                    save_path = str(
                        self._screenshot_dir / f"localpage_{umo_hash}_{ts}.png"
                    )
                    shot_ok = False
                    try:
                        if bool(full_page):
                            await page.screenshot(path=save_path, full_page=True)
                            shot_ok = True
                    except Exception as e:  # noqa: BLE001 — 整页失败降级视口
                        logger.debug(f"整页截图失败，降级视口截图: {e}")
                    if not shot_ok:
                        shot_ok = bool(await self.browser.screenshot(page, save_path))
                    if shot_ok:
                        collect_artifact(save_path)  # 后台模式下进附件
                        parts.append(f"截图：{save_path}")
                return "\n".join(part for part in parts if part)
            finally:
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

    def _forget_page_caches(self, page) -> None:
        """页面关闭后摘掉所有按 ``id(page)`` 建的缓存（感知池 + 执行层 CDP 缓存）。"""
        try:
            self._cdp_pool.drop(page)
        except Exception:  # noqa: BLE001 — 池是纯内存结构，失败不值得打断关页
            pass
        try:
            ex.drop_cdp(page)
        except Exception:  # noqa: BLE001
            pass

    async def _get_page(
        self, event: AstrMessageEvent
    ) -> tuple[Optional["Page"], str]:
        """取本会话当前任务的页面（鉴权 → 浏览器配置 → 按任务取页）。

        v1.7.5：改走 ``SessionManager.claim_page`` —— **按任务**取页（换任务先
        关旧任务页），且新页落在 ``about:blank``（不再有起始页配置）。
        """
        allowed, deny_reason = self._is_session_allowed(event)
        if not allowed:
            return None, deny_reason
        if self.sessions is None:
            return None, "插件尚未初始化，请稍后再试"
        if getattr(self, "browser", None) is not None:
            try:
                validate_browser_type(self.browser.browser_type)
            except ValueError as e:
                return None, str(e)
        umo = self._umo_of(event)
        state = self._task_state(umo)
        # `tools.isolate_context` 真正影响控制流（t12 R4 的返工）：开 = 按任务换页
        # （新任务关旧页）；关 = **跨任务复用同一张页**，task_key 退化成 umo 常量，
        # `claim_page` 因此永远不会触发换页。风险见 errata E10（上一任务的表单值/
        # 滚动位置/登录态会带进下一任务，且 mark/ref 纪元仍按任务作废 ⇒ 旧编号失效）。
        if bool(getattr(self, "isolate_context", True)):
            claim_key = str(state.task_key or umo)
        else:
            claim_key = str(umo)
        page = await self.sessions.claim_page(umo, task_key=claim_key)
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

    def _lock_for(self, event: AstrMessageEvent) -> asyncio.Lock:
        """返回该会话的专用锁（串行化同一会话的并发工具调用）。"""
        return self.sessions.get_lock(self._umo_of(event))

    # ================================================================
    # LLM 工具：浏览能力（全部返回 str，异常兜底，绝不抛出）
    # ================================================================

    # ================================================================
    # 感知 / 动作 / 验证层的装配（v1.7.5：页面读写的**唯一**入口）
    #
    # 一条链写死在这里（规格 §1.4.3）：
    #   guard.before → parse/resolve → apply（受信任事件）→
    #   check(before/after, snapshot, candidates) → a11y.snapshot → 观测块
    # 五层各自只有一个事实源：动作在 action_model、定位与事件在 executor、
    # 判定与渲染在 verify、节点与首行在 a11y —— 本文件只做**装配**。
    # ================================================================

    # ---------------------------- 任务状态 ----------------------------

    def _task_state(self, umo: str) -> "_TaskState":
        """取（或建）该会话的任务状态；任务边界由 :meth:`reset_task` 划。"""
        tasks = getattr(self, "_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._tasks = tasks
        state = tasks.get(umo)
        if state is not None:
            state.last_used = time.monotonic()  # R31：LRU 的"最后使用"要真的被更新
        if state is None:
            if len(tasks) >= _TASK_STATE_SOFT_CAP:  # 长跑实例的任务表不该无限增长
                # R20：按"最后使用时间"淘汰**空闲**状态（deadline==0）；绝不踢掉
                # 有预算在身的状态，否则它的 ref/mark 会与手上那张 SoM 图对不上。
                idle = sorted(
                    (float(getattr(other, "last_used", 0.0)), key)
                    for key, other in tasks.items()
                    if float(getattr(other, "deadline", 0) or 0) <= 0
                )
                for _stamp, key in idle[:_TASK_STATE_EVICT_BATCH]:
                    tasks.pop(key, None)
                # 兜底硬上限（R31）：全是 live（deadline>0）时也得收敛，否则长跑
                # 实例的内存只增不减。按 last_used 升序丢最久未用的（并记 WARN）。
                if len(tasks) > _TASK_STATE_HARD_CAP:
                    overflow = sorted(
                        (float(getattr(item, "last_used", 0.0)), key)
                        for key, item in tasks.items()
                    )
                    drop = len(tasks) - _TASK_STATE_SOFT_CAP
                    for _stamp, key in overflow[:drop]:
                        tasks.pop(key, None)
                    logger.warning(
                        f"[{self.metadata_name}] 任务状态表达硬上限"
                        f"（{_TASK_STATE_HARD_CAP}），已按 LRU 淘汰 {drop} 个在跑状态"
                    )
            state = _TaskState(
                # R4：actions.repeat_guard 之前读了没人用；阈值必须来自配置
                # （0 = 关闭重复守卫，由 RepeatGuard 自己处理 <=0）。
                guard=vf.RepeatGuard(
                    threshold=int(getattr(self, "repeat_guard_limit", 3))
                ),
                last_used=time.monotonic(),
            )
            tasks[umo] = state
        return state

    def reset_task(self, umo: str, *, budget_s: float = 0.0) -> "_TaskState":
        """新任务边界：ref 回 1、mark 作废、重复守卫清零、预算重新起算。"""
        state = self._task_state(umo)
        state.refs.reset()
        state.marks.invalidate("新任务")
        state.guard.reset()
        state.mark_nodes = {}
        state.snapshot = None
        state.step = 0
        state.pending = ""
        state.task_key = f"{umo}#{time.monotonic():.6f}"
        state.deadline = (
            time.monotonic() + float(budget_s) if float(budget_s) > 0 else 0.0
        )
        return state

    def _remaining_s(self, umo: str) -> float | None:
        """剩余墙钟预算（无任务预算时返回 None ⇒ 观测块不显示【预算】行）。"""
        state = getattr(self, "_tasks", {}).get(umo) if isinstance(getattr(self, "_tasks", None), dict) else None
        deadline = float(getattr(state, "deadline", 0) or 0) if state else 0.0
        if deadline <= 0:
            return None
        return max(0.0, deadline - time.monotonic())

    def _a11y_limits(self) -> a11y.A11yLimits:
        """感知预算（``perception.a11y_*``；越界值由 ``A11yLimits`` 自己钳制）。"""
        return a11y.A11yLimits(
            max_nodes=int(self.a11y_max_nodes),
            max_name_chars=int(self.a11y_max_name_chars),
            som_max_marks=int(self.som_max_marks),
        )

    def _viewport_of(self, page) -> tuple[tuple[int, int], float]:
        """视口与 DPR（观测首行与 SoM 图尺寸都用它；取不到用 v6 默认值）。"""
        view = getattr(page, "viewport_size", None) or {}
        width = int(view.get("width") or 0)
        height = int(view.get("height") or 0)
        if width <= 0 or height <= 0:
            configured = self.viewport if isinstance(self.viewport, dict) else {}
            width = int(configured.get("width") or 1280)
            height = int(configured.get("height") or 800)
        try:
            dpr = float(getattr(self, "device_scale_factor", 1) or 1)
        except (TypeError, ValueError):
            dpr = 1.0
        return (width, height), dpr

    async def _text_of(self, page) -> str:
        """正文抽取（``a11y.snapshot`` 的 ``text_fn``；失败由感知层兜底为空）。"""
        if self.extractor is None:
            return ""
        return await self.extractor.extract_text(page, max_chars=int(self.max_text_chars))

    def _ssrf_count(self, page) -> int:
        """本页被 SSRF 兜底拦截的请求数（D9-①的观测点）。"""
        try:
            return int(self.browser.ssrf_blocked(page)) if self.browser is not None else 0
        except Exception:  # noqa: BLE001 — 计数失败不该影响感知
            return 0

    async def _snapshot(self, page, state: "_TaskState") -> a11y.PageSnapshot:
        """取一次快照：感知层唯一入口（CDP 会话池跨调用复用，ref 按任务自增）。"""
        viewport, dpr = self._viewport_of(page)
        snap = await a11y.snapshot(
            page,
            limits=self._a11y_limits(),
            pool=self._cdp_pool,
            allocator=state.refs,
            boxes={},
            text_fn=self._text_of,
            max_text_chars=int(self.max_text_chars),
            ssrf_blocked=self._ssrf_count(page),
            viewport=viewport,
            dpr=dpr,
        )
        state.snapshot = snap
        if snap.degraded:
            logger.debug(
                f"[{self.metadata_name}] 感知降级: {'; '.join(snap.degraded)}"
            )
        return snap

    async def _mark_boxes(
        self, page, snap: a11y.PageSnapshot, limit: int
    ) -> tuple[dict, int]:
        """给 SoM 采集框 —— **只走 executor 的 locator 层**（与点击同一套定位）。

        两条平台事实（决定了这里的形态）：

        1. ``DOM.getBoxModel`` 给的是**帧内坐标**，叠在顶层截图上会错位 ⇒ 子帧
           节点（``frame_path`` 非空）不发 mark，只计数并在返回体里如实说明；
        2. 逐节点容错：detached / 隐藏 / 零尺寸 / 定位失败都只跳过并计数，绝不
           让整块感知抛异常。

        Returns:
            ``(boxes, skipped)``；``boxes`` 形如 ``{node_id: (x, y, w, h)}``。
        """
        boxes: dict[int, tuple[float, float, float, float]] = {}
        skipped = 0
        picked = 0
        for node in getattr(snap, "nodes", ()) or ():
            if picked >= max(1, int(limit)):
                break
            if not getattr(node, "actionable", False):
                continue
            if getattr(node, "frame_path", ()):  # 子帧坐标不能叠到顶层截图
                skipped += 1
                continue
            role = str(getattr(node, "role", "") or "")
            name = str(getattr(node, "name", "") or "")
            node_id = int(getattr(node, "node_id", 0) or 0)
            if not role or not name or not node_id:
                skipped += 1
                continue
            picked += 1
            try:
                target = am.Target(role=role, name=name)
                resolved = await ex.resolve(
                    page, snap, am.Action(kind=am.ActionKind.CLICK, target=target)
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单节点失败只跳过
                skipped += 1
                continue
            box = getattr(resolved, "box", None) if getattr(resolved, "ok", False) else None
            if not box:
                skipped += 1
                continue
            boxes[node_id] = tuple(float(v) for v in box)
        return boxes, skipped

    @staticmethod
    def _with_boxes(snap: a11y.PageSnapshot, boxes: dict) -> a11y.PageSnapshot:
        """把 locator 层的框贴回节点（A11yNode 是 frozen，用 ``replace`` 造新对象）。"""
        if not boxes:
            return snap
        nodes = tuple(
            replace(node, box=boxes[int(node.node_id)])
            if int(getattr(node, "node_id", 0) or 0) in boxes
            else node
            for node in snap.nodes
        )
        return replace(snap, nodes=nodes)

    def _action_url_policy(self, url: str) -> str | None:
        """``navigate`` 的 URL 安全策略（R-A11 的唯一实现；返回 None = 放行）。"""
        banned = self._check_banned(url)
        if banned:
            return f"URL 含违禁内容：{banned}"
        ok, reason = check_url_literal(
            url, block_internal_ip=bool(self.block_internal_ip)
        )
        return None if ok else (reason or "URL 未通过安全策略")

    # ---------------------------- 观测块 ----------------------------

    def _links_block(self, snap: a11y.PageSnapshot) -> str:
        """链接表（``view=links``）：只给 role=link 的 name，禁词逐条过滤。"""
        limit = max(1, int(self.max_links or 20))
        lines: list[str] = []
        for node in snap.nodes:
            if str(getattr(node, "role", "")).lower() != "link":
                continue
            name = str(getattr(node, "name", "") or "")
            if not name or self._check_banned(name):
                continue
            lines.append(f"- [link] {name}")
            if len(lines) >= limit:
                break
        body = "\n".join(lines) if lines else "（本页没有可点的链接）"
        return "【链接】\n" + body

    def _render_obs(
        self,
        snap: a11y.PageSnapshot,
        *,
        umo: str,
        step: int,
        action: am.Action | None = None,
        verdict: vf.Verdict | None = None,
        view: str = "structure",
        max_items: int = 0,
        note: str = "",
    ) -> str:
        """观测块（**渲染只发生在 a11y + verify**，这里只决定给哪几段）。"""
        extra: list[str] = []
        if note:
            extra.append(note)
        if view == "text":
            body = agent_core.truncate_text(snap.text or "", int(self.max_text_chars))
            lines: list[str] = []
            extra.insert(0, "【正文】\n" + (body or "（未取到正文，可改用 browse_screenshot）"))
        elif view == "links":
            lines = []
            extra.insert(0, self._links_block(snap))
        else:
            limit = int(max_items or 0) or (240 if view == "full" else 60)
            lines = a11y.render_node_lines(
                snap.nodes, show_ref=(view == "full"), max_lines=limit
            ).splitlines()
        return vf.render_observation(
            step=step,
            snapshot=snap,
            node_lines=lines,
            action=action,
            verdict=verdict,
            remaining_s=self._remaining_s(umo),
            note="\n".join(part for part in extra if part),
        )

    # ---------------------------- 观测 ----------------------------

    async def _observe_impl(
        self, event: AstrMessageEvent, *, view: str = "structure", max_items: int = 0
    ) -> str:
        """观测执行体（**已在会话锁内**）：快照 → 观测块（不改页面、不作废 mark）。"""
        page, err = await self._get_page(event)
        if page is None:
            return f"【错误】{err}"
        umo = self._umo_of(event)
        state = self._task_state(umo)
        mode = str(view or "structure").strip().lower()
        if mode not in OBSERVE_VIEWS:
            return f"【拒绝】view 只能是 {'/'.join(OBSERVE_VIEWS)}（收到 {view!r}）"
        snap = await self._snapshot(page, state)
        if mode in ("structure", "full"):
            banned = self._check_banned(snap.title or "")
            if banned:
                return f"【拒绝】页面标题包含违禁内容：{banned}，已拒绝输出。"
        elif mode == "text":
            banned = self._check_banned(snap.text or "")
            if banned:
                return f"【拒绝】页面正文包含违禁内容：{banned}，已拒绝输出。"
        return self._render_obs(
            snap, umo=umo, step=max(1, state.step), view=mode, max_items=max_items
        )

    # ---------------------------- 动作 ----------------------------

    def _ledger_status_of(self, event: AstrMessageEvent) -> str:
        """取走本步待登记的验证状态（台账 ``status`` 的唯一来源）。"""
        state = getattr(self, "_tasks", {}).get(self._umo_of(event)) if isinstance(getattr(self, "_tasks", None), dict) else None
        if state is None:
            return ""
        status, state.pending = state.pending, ""
        return status

    async def _run_action(
        self,
        page,
        action: am.Action,
        snap: a11y.PageSnapshot,
        marks: dict,
        umo: str,
    ) -> tuple[ex.ExecutionResult, int]:
        """执行一步动作（E2 定位失败按 ``actions.max_retries`` 重试）。"""
        cdp = None
        if bool(self.trusted_events):
            try:
                # R23：CDP 会话只由感知层的 CdpSessionPool 持有（每页一条），
                # executor 的模块级缓存不再参与 ⇒ 不会有 id(page) 复用返回死会话。
                cdp = await self._cdp_session(page)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 拿不到 CDP 就退回 locator 事件
                logger.warning(
                    f"[{self.metadata_name}] 取 CDP 会话失败，退回 locator 事件: {e}"
                )
        tabs = _TabsView(self.sessions, umo) if self.sessions is not None else None
        attempts = 1 + max(0, int(self.action_retries))
        result: ex.ExecutionResult | None = None
        for attempt in range(1, attempts + 1):
            result = await ex.apply(
                page,
                action,
                cdp=cdp,
                trusted_events=bool(self.trusted_events),
                snapshot=snap,
                mark_epoch=marks,
                tabs=tabs,
                action_timeout_s=float(self.action_timeout_s),
                goto_timeout_s=float(self.goto_timeout_s),
                read_limit=int(self.max_text_chars),
            )
            if result.ok:
                return result, attempt
            # 只重试"没找到/歧义"（页面可能还在渲染）；动作已发出但报错的不重试，
            # 避免重复副作用（规格 §1.5 的 E2 与 E3 分开计）。
            if result.used != "none" or "【未找到】" not in str(result.reason):
                return result, attempt
        assert result is not None  # attempts >= 1
        return result, attempts

    async def _verify(
        self,
        page,
        action: am.Action,
        before: vf.State,
        after: vf.State,
        snap: a11y.PageSnapshot,
        result: ex.ExecutionResult,
    ) -> vf.Verdict:
        """三档判定（期望 > 差分 > 无变化）；``actions.verify=false`` 时整体跳过。"""
        if not bool(self.actions_verify):
            return vf.Verdict(
                ok=True, outcome=vf.OUTCOME_SKIPPED, reason="验证已关闭（actions.verify=false）"
            )
        if not result.ok:
            return vf.Verdict(ok=False, outcome=vf.OUTCOME_FAILED, reason=result.render())
        expect = action.expect
        if expect is not None and not expect.is_empty():
            resolution = result.resolution
            if resolution is not None and getattr(resolution, "locator", None) is not None:
                return await vf.wait_for_expect(
                    page,
                    action,
                    resolution,
                    action_timeout_s=min(5.0, float(self.action_timeout_s)),
                )
        return vf.check(
            action,
            before,
            after,
            snapshot=snap,
            candidates=[cand.render() for cand in result.candidates],
        )

    async def _act_impl(self, event: AstrMessageEvent, payload) -> str:
        """一步动作的完整链路（**已在会话锁内**，规格 §1.4.3 的顺序逐条照抄）。"""
        page, err = await self._get_page(event)
        if page is None:
            return f"【错误】{err}"
        umo = self._umo_of(event)
        state = self._task_state(umo)
        marks = dict(state.mark_nodes) if state.marks.active else {}
        parsed = am.parse_action(
            payload,
            ctx=am.ParseContext(
                mark_epoch=marks,
                url_policy=self._action_url_policy,
                remaining_s=self._remaining_s(umo),
                steps=state.step,
            ),
        )
        if isinstance(parsed, am.Rejection):
            state.pending = "rejected"
            snap = state.snapshot or await self._snapshot(page, state)
            return self._render_obs(snap, umo=umo, step=max(1, state.step), note=parsed.render())
        action: am.Action = parsed

        # 只读类动作直接走观测/截图（同一套感知，但不需要差分与台账 status）。
        if action.kind is am.ActionKind.READ:
            view = "links" if str(action.value or "") == "links" else "text"
            return await self._observe_impl(event, view=view)
        if action.kind is am.ActionKind.SCREENSHOT:
            return await self._screenshot_impl(
                event, target=None, full=str(action.value or "") == "full"
            )

        state.step += 1
        step = state.step
        blocked = state.guard.before(action)  # 顺序：guard 在 resolve/apply 之前
        snap = state.snapshot or await self._snapshot(page, state)
        if blocked is not None:
            state.pending = "rejected"
            return self._render_obs(
                snap,
                umo=umo,
                step=step,
                action=action,
                verdict=vf.Verdict(
                    ok=False, outcome=vf.OUTCOME_REJECTED, rejection=blocked
                ),
            )
        before = await vf.capture(page)
        result, attempt = await self._run_action(page, action, snap, marks, umo)
        state.marks.on_action(action.kind.value)  # 改变页面的动作作废 mark 纪元
        state.mark_nodes = dict(state.mark_nodes) if state.marks.active else {}
        after = await vf.capture(page)
        after_snap = await self._snapshot(page, state)
        verdict = await self._verify(page, action, before, after, after_snap, result)
        state.guard.note(action.digest(), verdict.outcome)
        state.pending = vf.ledger_status(verdict)
        note = ""
        if not result.ok:
            # R10（选①）：D7 要求"返回体固定带 elapsed={ms}ms attempt={n}"，
            # 这里把它渲染进模型可见的失败行；成功路径不刷噪声（结构化字段仍在
            # ExecutionResult / to_payload 里）。
            elapsed = int(getattr(result, "elapsed_ms", 0) or 0)
            tried = int(getattr(result, "attempt", attempt) or attempt)
            note = (
                f"{result.render()}"
                f"（elapsed={elapsed}ms attempt={tried}"
                + (f"，已重试 {attempt - 1} 次" if attempt > 1 else "")
                + "）"
            )
        return self._render_obs(
            after_snap, umo=umo, step=step, action=action, verdict=verdict, note=note
        )

    # ---------------------------- 五个内层工具 ----------------------------

    async def browse_observe(
        self, event: AstrMessageEvent, view: str = "structure", max_items: int = 0
    ) -> str:
        """看页面（结构/正文/链接/全量）——A11y 树是唯一状态来源。"""
        try:
            async with self._lock_for(event):
                return await self._observe_impl(event, view=view, max_items=max_items)
        except Exception as e:  # noqa: BLE001 — LLM 工具必须容错
            logger.warning(f"[{METADATA_NAME}] browse_observe 失败: {e}")
            return f"【错误】观测失败：{e}"

    async def browse_act(self, event: AstrMessageEvent, action: dict = None) -> str:
        """执行一个结构化动作（schema → resolve → trusted → verify → 观测块）。"""
        try:
            async with self._lock_for(event):
                return await self._act_impl(event, action)
        except Exception as e:  # noqa: BLE001 — LLM 工具必须容错
            logger.warning(f"[{METADATA_NAME}] browse_act 失败: {e}")
            return f"【错误】动作执行失败：{e}"

    async def browse_media(
        self, event: AstrMessageEvent, media_type: str = "all", max_items: int = 5
    ) -> str:
        """列出/下载当前页面的图片与视频（旧 ``browse_sniff_media``，语义不变）。"""
        try:
            return await self._media_impl(event, media_type=media_type, max_items=max_items)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_media 失败: {e}")
            return f"【错误】媒体嗅探失败：{e}"

    async def browse_search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        max_results: int = 0,
        rounds: int = 0,
        timeout: int = 0,
        fetch_pages: bool = False,
        fetch_top_n: int = 0,
        open_page: bool = False,
    ) -> str:
        """网页搜索：默认走搜索 API 池；``open_page=true`` 时打开引擎结果页。"""
        try:
            text = str(query or "").strip()
            if not text:
                return "【拒绝】query 不能为空。"
            banned = self._check_banned(text)
            if banned:
                return f"【拒绝】检索词包含违禁内容：{banned}"
            if bool(open_page):
                return await self._open_engine_page(event, text)
            # R1：v5 的 limits.agent_tool_timeout 已并入 resources.total_budget_s，
            # 读取点必须跟着改（旧名从来不存在 ⇒ 一调就 AttributeError）。
            budget = min(float(self.search_total_timeout), float(self.total_budget_s))
            try:
                given = int(timeout or budget)
            except (TypeError, ValueError):
                given = int(budget)
            budget = min(budget, float(_clamp(given, 5, 300)))
            raw = await self._run_browser_web_search(
                event,
                max_bytes=PAYLOAD_MAX_BYTES,
                timeout=int(_clamp(budget, 5, 300)),
                query=text,
                max_results=max_results,
                rounds=rounds,
                fetch_pages=bool(fetch_pages),
                fetch_top_n=fetch_top_n,
            )
            return agent_core.record_and_truncate(
                raw, tool="browse_search", args={"query": text}, run=agent_core.current_run()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_search 失败: {e}")
            return f"【错误】搜索失败：{e}"

    async def _open_engine_page(self, event: AstrMessageEvent, query: str) -> str:
        """打开搜索引擎结果页（浏览器交互路径；navigate 走 executor，不直调 page.goto）。"""
        async with self._lock_for(event):
            page, err = await self._get_page(event)
            if page is None:
                return f"【错误】{err}"
            name = str(self.default_search_engine or "必应搜索").strip()
            template = self._search_engines.get(name)
            if not template:
                return f"【错误】未知搜索引擎：{name}，可选：{'/'.join(self._search_engines)}"
            umo = self._umo_of(event)
            state = self._task_state(umo)
            state.step += 1
            action = am.Action(kind=am.ActionKind.NAVIGATE, value=template.format(keyword=quote(query)))
            snap = state.snapshot or await self._snapshot(page, state)
            result, _attempt = await self._run_action(page, action, snap, {}, umo)
            after_snap = await self._snapshot(page, state)
            verdict = vf.Verdict(
                ok=bool(result.ok),
                outcome=vf.OUTCOME_CHANGED if result.ok else vf.OUTCOME_FAILED,
                reason=result.render(),
            )
            state.pending = vf.ledger_status(verdict)
            return self._render_obs(
                after_snap, umo=umo, step=state.step, action=action, verdict=verdict
            )


    # ================================================================
    # LLM 工具：进阶浏览能力（多标签 / 输入 / 截图）
    # ================================================================

    async def _emit_media(
        self, event: AstrMessageEvent, path: str, kind: str = "image"
    ) -> None:
        """媒体发送出口（T-M0）。

        **后台**（``_submit_background`` 已进 ``artifact_sink`` 上下文）**只登记路径**、
        绝不 ``event.send``：媒体随终态那**一条**消息作为附件发出（F-G4 / spec §6 C5）。
        前台保持原有行为：截图/媒体立即发到当前会话（行为不变，回归用例钉住）。
        """
        if collect_artifact(path):
            return
        if kind == "image":
            await event.send(event.image_result(path))
            return
        from astrbot.api.message_components import Video  # noqa: PLC0415

        await event.send(event.chain_result([Video.fromFileSystem(path)]))

    async def browse_screenshot(
        self, event: AstrMessageEvent, target: dict | None = None, full: bool = False
    ):
        """SoM 兜底截图（**唯一合法的"看图"通道**，规格 §2.4）。

        三件事按顺序：拿快照 → 给可点节点采框（executor locator 层）→ 画编号 +
        截图（`core.a11y.som_screenshot`，**必定**摘除 overlay）。标记纪元随图
        更换；模型只回编号（``target={"mark":N}``），不回像素坐标。

        Returns:
            ``CallToolResult``（TextContent[标签表] + ImageContent）或纯文本；
            无多模态/图片超限 ⇒ 文本里带 ``【不可用】`` 说明（**不许**静默丢图）。
        """
        try:
            async with self._lock_for(event):
                return await self._screenshot_impl(event, target=target, full=full)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_screenshot 失败: {e}")
            return f"【错误】截图失败：{e}"

    async def _screenshot_impl(
        self, event: AstrMessageEvent, *, target: dict | None, full: bool
    ) -> str:
        """截图执行体（**已在会话锁内**）。"""
        if not self._screenshot_allowed_for(self._umo_of(event)):
            return (
                "【不可用】截图兜底已关闭"
                "（perception.screenshot_fallback=false，或被 perception.rules 关掉）。"
            )
        page, err = await self._get_page(event)
        if page is None:
            return f"【错误】{err}"
        umo = self._umo_of(event)
        state = self._task_state(umo)
        snap = await self._snapshot(page, state)
        clip = None
        marks: tuple = ()
        skipped = 0
        truncated = False
        if isinstance(target, dict) and target:
            probe = am.parse_action(
                {"action": "screenshot", "target": target},
                ctx=am.ParseContext(
                    mark_epoch=state.mark_nodes if state.marks.active else {}
                ),
            )
            if isinstance(probe, am.Rejection):
                state.pending = "rejected"
                return probe.render()
            resolved = await ex.resolve(page, snap, probe)
            if not resolved.ok:
                state.pending = "rejected"
                return resolved.render()
            clip = resolved.box
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_path = str(self._screenshot_dir / f"som_{self._umo_digest(umo)}_{ts}.png")

        async def _shoot(**kwargs):
            kwargs.pop("path", None)
            return await page.screenshot(path=save_path, **kwargs)

        if bool(full):
            await _shoot(full_page=True)
            head = (
                f"【截图】整页长截图 image={snap.viewport[0]}x{snap.scroll_height}"
                f"@dpr{snap.dpr:g}（整页图**不叠**编号；要编号请裁回首屏）"
            )
        else:
            boxes, skipped = await self._mark_boxes(
                page, snap, int(self.som_max_marks)
            )
            snap = self._with_boxes(snap, boxes)
            marks, truncated = a11y.select_marks(
                snap.nodes, max_marks=int(self.som_max_marks), clip=clip
            )
            result = await a11y.som_screenshot(
                page,
                marks=marks,
                clip=clip,
                viewport=snap.viewport,
                dpr=snap.dpr,
                screenshot_fn=_shoot,
            )
            state.marks.adopt(result)
            state.mark_nodes = {mid: node for mid, node in result.targets}
            head = result.table()
            if result.overlay_error:
                head += f"\n（画框异常：{result.overlay_error}）"
        notes: list[str] = []
        if not full and not marks:
            # **不许静默给空表**：一张没有任何标签的 SoM 图对模型毫无信息量，
            # 它只会开始编坐标。说清为什么没有标签，并给出可走的路。
            notes.append(
                "（本张图**没有可标注的可交互节点**：可能全是 canvas/图片、"
                "被隐藏，或感知降级/取不到框 —— 请改用 browse_observe 的 "
                "[role]名称 定位；确实只能看图时，让主 LLM 换多模态内层模型）"
            )
        if skipped:
            notes.append(
                f"（{skipped} 个子帧/无框节点未标号：帧内坐标不能叠到顶层截图，"
                "请用 browse_observe 的 [role]名称 定位）"
            )
        if truncated:
            notes.append(
                "（标签数已达上限，未标的元素请用 browse_observe 的 [role]名称 定位）"
            )
        # R2：v6 语义是"截图**是否**发到会话"（false = 只给模型看、不刷屏）。
        # v5 的旧静默开关迁过来时已经取反，所以这里必须用**真值**判断。
        if bool(self.screenshot_to_chat):
            try:
                await self._emit_media(event, save_path, "image")
            except Exception as e:  # noqa: BLE001 — 发图失败不阻塞识图
                logger.warning(f"[{METADATA_NAME}] 发送截图失败: {e}")
        text = "\n".join([head, *notes])
        payload = await self._image_result(
            save_path,
            text=text,
            size_label=self._viewport_label(page),
            sent_to_user=bool(self.screenshot_to_chat),
        )
        return payload if payload is not None else text + "\n" + _IMAGE_UNAVAILABLE_TEXT


    async def _cdp_session(self, page):
        """取该页**唯一**的 CDP 会话（感知层池子；失败返回 None）。"""
        pool = getattr(self, "_cdp_pool", None)
        if pool is None:
            return None
        call = await pool.session_for(page)
        return getattr(call, "data", None) if getattr(call, "ok", False) else None

    def _viewport_label(self, page) -> str:
        """截图尺寸标签（``1280x800``）：取自视口，取不到用配置兜底。"""
        viewport = getattr(page, "viewport_size", None) or {}
        width = int(viewport.get("width") or 0)
        height = int(viewport.get("height") or 0)
        if width <= 0 or height <= 0:
            configured = self.viewport if isinstance(self.viewport, dict) else {}
            width = int(configured.get("width") or 1280)
            height = int(configured.get("height") or 800)
        return f"{width}x{height}"

    async def _image_result(
        self,
        path: str,
        *,
        text: str,
        size_label: str,
        sent_to_user: bool,
    ):
        """把截图包成 ``CallToolResult``（影像原生的**唯一**出站处）。

        规则：每次最多 1 张 ``ImageContent``；全任务最多
        ``agent.MAX_IMAGES_PER_TASK`` 张；``path`` 必须在本插件截图目录下
        （否则就是任意文件读取原语）。返回 ``None`` ⇒ 调用方给【不可用】文案。
        """
        run = agent_core.current_run()
        if not self._is_own_screenshot(path):
            logger.error(
                f"[{METADATA_NAME}] 影像原生拒绝非截图目录路径"
                f"（{Path(str(path)).name}）"
            )
            return None
        if run is not None and run.ledger.images >= agent_core.MAX_IMAGES_PER_TASK:
            return (
                f"【预算】本次任务图片数已达上限（{agent_core.MAX_IMAGES_PER_TASK} "
                "张），本张不再附图；请改用 browse_observe 获取信息。"
            )
        image = await agent_core.async_image_content(
            path,
            max_side=int(self.agent_image_max_side),
            quality=int(self.agent_image_quality),
        )
        if image is None:
            return None
        prefix = (
            "截图已发送给用户，同时附在本条结果之后。"
            if sent_to_user
            else f"截图已获取（{size_label}）。图片附在本条结果之后。"
        )
        return agent_core.call_result_with_image(
            f"{prefix}\n{text}" if text else prefix, image
        )


    def _is_own_screenshot(self, path: str) -> bool:
        """该路径是否在本插件的截图目录下（影像原生的安全边界）。"""
        root = getattr(self, "_screenshot_dir", None)
        if root is None:
            return False
        try:
            Path(str(path)).resolve().relative_to(Path(root).resolve())
        except (ValueError, OSError):
            return False
        return True


    async def _media_impl(
        self, event: AstrMessageEvent, *, media_type: str, max_items: int
    ) -> str:
        """媒体嗅探执行体（旧 ``browse_sniff_media``，语义不变）。

        资源地址只能从 HTML 属性来（A11y 树只给 role/name）⇒ 读一次
        ``page.content()`` 扫属性，**不用** ``page.evaluate``（JS 枚举链已退役）。
        """
        mtype = str(media_type or "all").strip().lower()
        if mtype not in ("image", "video", "all"):
            return "【错误】media_type 仅支持 image/video/all。"
        limit = self._as_int(max_items, 5) or 5
        async with self._lock_for(event):
            page, err = await self._get_page(event)
            if page is None:
                return f"【错误】{err}"
            html = await page.content()
            base = str(getattr(page, "url", "") or "")
            items = _scan_media_urls(html, base)
            wanted: list[tuple[str, str]] = []
            skipped = 0
            for url, kind in items:
                if "m3u8" in url.lower():
                    skipped += 1
                    continue
                cls = _classify_media(url, kind)
                if cls is None or (mtype not in ("all", cls)):
                    skipped += 1
                    continue
                wanted.append((url, cls))
            if not wanted:
                hint = f"（跳过 {skipped} 个无法分类/流媒体资源）" if skipped else ""
                return f"当前页面没有嗅探到图片/视频资源{hint}。"
            downloaded: list[str] = []
            blocked = 0
            for url, cls in wanted:
                if len(downloaded) >= limit:
                    break
                ok, reason = await self.safety.acheck_url(url)
                if not ok:
                    blocked += 1
                    logger.warning(
                        f"[{METADATA_NAME}] 媒体 URL 未通过安全校验，跳过: "
                        f"{_redact_url(url)} ({reason})"
                    )
                    continue
                try:
                    path = await self._download_media(url, len(downloaded) + 1, cls)
                    if not path:
                        continue
                    downloaded.append(path)
                    await self._emit_media(event, path, "image" if cls == "image" else "video")
                except Exception as e:  # noqa: BLE001 — 单个失败继续
                    logger.warning(
                        f"[{METADATA_NAME}] 下载/发送媒体失败 {_redact_url(url)}: {e}"
                    )
            if not downloaded:
                hint = f"。另有 {blocked} 个 URL 因安全校验被拦截" if blocked else ""
                return f"媒体下载失败，请检查网络或资源可访问性{hint}。"
            lines = [f"已下载并发送 {len(downloaded)} 个媒体："]
            lines.extend(f"- {path}" for path in downloaded)
            if blocked:
                lines.append(f"（另有 {blocked} 个 URL 被安全拦截，未下载）")
            return "\n".join(lines)


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

    def _search_pool_ready(self) -> bool:
        """池是否就绪：``search.enabled`` + 至少一个可用 provider（**与模式无关**）。

        v1.7.0 起"池是否就绪"和"外层工具该不该可见"分开了：internal 档下池
        就绪（内层要用它）但外层工具必须不可见。混成一个函数会让
        ``_rebuild_browser_toolset`` 在 internal 档永远拿不到池（自相矛盾）。

        ``getattr`` 兜底：本方法在内层工具集签名与若干"手工装配的插件实例"
        （单测用 ``BrowserLLMPlugin.__new__`` 造的那种）里都会被调到，属性缺失
        时按"未启用"处理，不要把 AttributeError 抛进工具链。
        """
        if not getattr(self, "search_enabled", False):
            return False
        return bool(self._search_usable_entries())

    def _search_available(self) -> bool:
        """**外层** ``browser_web_search`` 是否应当可见：池就绪 + 模式为 external。

        v1.7.5：``search.mode`` 已删除（池搜索统一由内层 ``browse_search``
        承接，规格 §9.2），因此只看池是否就绪。
        """
        return self._search_pool_ready()

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
        """用插件自有的搜索 API 池做联网检索；与内置 web_search_<provider> 并存，同一问题只选其一。

        Args:
            query(string): 检索词。
            max_results(number): 最多返回多少条（0 = 用配置默认）。
            rounds(number): 多轮检索轮数（0 = 用配置默认）。
            timeout(number): 本次总秒数（0 = 用配置默认；不会超过总预算）。
            providers(string): 逗号分隔的 provider 白名单（空 = 全部启用项）。
            fetch_pages(boolean): 是否抓取前 N 条正文。
            fetch_top_n(number): 抓正文条数（0 = 用配置默认）。

        只做「检索 + 摘要」，返回结构化 JSON：`status` / `results[]`
        （url/title/snippet，可选 fetched.text）/ `error_codes` / `notes`。
        需要登录、点击、翻页或看页面外观的检索请改用 `browse_web` 委托子代理。
        `tools.background_mode` 打开时本工具改为后台执行：先回一行
        「【已受理】task_id=…」，结果随后单独发一条消息 —— 此时**不要**用这次
        返回值回答事实问题，只回一句"已开始搜索，结果稍后单独发送"。
        """
        # 后台模式热更新（v1.5.0）：入口第一行刷新配置，Dashboard 把
        # background_mode 改回 off 时下一次调用即生效（F1）。
        self._refresh_config()
        allowed, _deny_reason = self._is_session_allowed(event)
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
            async def _search_job(progress: dict) -> str:
                raw = await self._run_browser_web_search(
                    event,
                    query=query,
                    max_results=max_results,
                    rounds=rounds,
                    timeout=timeout,
                    providers=providers,
                    fetch_pages=fetch_pages,
                    fetch_top_n=fetch_top_n,
                    progress=progress,
                    refresh=False,
                )
                text = self._search_terminal_text(raw)
                # 预算到点时 watchdog 取这个半成品进终态（U1 的二次证据）
                progress["partial"] = text
                return text

            return await self._submit_background(
                event=event,
                tool_name=_SEARCH_TOOL_NAME,
                runner=_search_job,
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
        max_bytes: int = PAYLOAD_MAX_BYTES,
    ) -> str:
        """browser_web_search 的实际执行体（前台同步调用与后台任务共用）。

        ``progress``：增量快照容器（后台任务会传入自己的那份，超时时据此
        渲染「已得部分结果」）；``refresh``：是否在开头重读配置——入口已刷新
        时传 False（后台分支），避免一次调用读两遍配置；``max_bytes``：返回
        体体积上限（外层默认 24KB，**内层工具传 6000**，规格 §4.3 —— 内层
        还要再叠 ``agent.result_max_chars`` 截断）。

        双模式（v1.7.0）：外层 ``browser_web_search`` 与内层 ``browse_web_search``
        走的是**同一个**执行体（同一套 ``_search_url_guard`` / 禁词过滤 /
        冷却 / TTL 缓存），绝不另写一条 HTTP 路径 —— 安全防线只有一处实现。
        """
        began = time.monotonic()

        def _payload(**kwargs) -> str:
            """本次调用的返回体渲染（统一挂 max_bytes，避免某个分支漏传）。"""
            return self._search_payload(max_bytes=max_bytes, **kwargs)

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
                return _payload(
                    status="disabled",
                    code="disabled",
                    summary="【拒绝】搜索工具已在插件配置中关闭（search.enabled=false）",
                    query=text,
                    hint="如需使用，请在插件配置「搜索 API（browser_web_search）」里打开 enabled 并至少填写一个 provider 的 api_key。",
                )

            allowed, deny_reason = self._is_session_allowed(event)
            if not allowed:
                return _payload(
                    status="failed",
                    code="session_denied",
                    summary=f"【拒绝】{deny_reason}",
                    query=text,
                )

            if not text:
                return _payload(
                    status="failed",
                    code="empty_query",
                    summary="【错误】检索词为空，请提供要搜索的问题或关键词",
                    query=text,
                )

            banned = self._check_banned(text)
            if banned:
                return _payload(
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
                return _payload(
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
            return _payload(
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
                return _payload(
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
            return _payload(
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
            return _payload(
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
    def _search_terminal_text(raw: str) -> str:
        """搜索返回体（JSON）→ **人读**终态文本。

        后台终态是发给人的消息（不是给模型的工具结果），把 JSON 原样发出去
        等于让人读一堆花括号；这里只取结论行 + 前 6 条（标题/链接/摘要）+ notes。
        解析失败就原样返回（fail-soft：宁可难看，也不要丢结果）。
        """
        text = str(raw or "")
        try:
            payload = json.loads(text) if text else {}
        except (TypeError, ValueError):
            return text
        if not isinstance(payload, dict):
            return text
        lines: list[str] = []
        head = str(payload.get("summary") or "").strip()
        if head:
            lines.append(head)
        for index, item in enumerate(payload.get("results") or [], 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            lines.append(f"{index}. {title}\n{url}" + (f"\n{snippet}" if snippet else ""))
            if index >= 6:
                break
        lines.extend(str(note) for note in (payload.get("notes") or []) if note)
        return "\n".join(lines) if lines else (text or "（搜索未返回内容）")

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
        if self._background_enabled_for(_SEARCH_TOOL_NAME):
            # 后台模式下本工具返回【已受理】而不是 JSON（设计 §2.4）：主模型
            # 若照着这条返回值回答，等于凭空编造搜索结果，必须点明。
            # 用 ``_background_enabled_for`` 而不是 ``background_mode == "all"``：
            # v1.6.0 的 ``all × engine=core`` 下本工具是**前台同步**（D3 永久
            # 排除），此时再说"本工具此刻是后台执行"就是在骗模型。
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
        """在 LLM 请求前注入网页浏览工具使用指引；顺带截留当前人格块。

        两件事、两个理由：

        1. **委托指引**（v1.3.0 起）：告诉主 LLM 什么时候该调 ``browse_web``、
           什么时候不该（纯搜索别开浏览器）；
        2. **人格截留**（v1.7.0，``agent.persona_mode=current`` 的**主路径**）：
           核心先 ``_ensure_persona_and_skills`` 把人格 append 成
           ``\\n# Persona Instructions\\n\\n{prompt}\\n``（``astr_main_agent.py:599-603``），
           **之后**才触发 ``OnLLMRequestEvent``（``internal.py:271`` → ``:307``）。
           所以在这里切人格块拿到的就是"本轮真实生效的人格"，比事后问
           persona_manager 更准（后者拿不到会话 persona_id 时会走别的规则）。
           只缓存文本：不落日志、不写文件（规格 §9.4）。

        会话被拒绝时也照样截留人格（拒绝只影响指引，不影响"人格是什么"这一事实）。
        """
        try:
            self._remember_persona(
                self._umo_of(event),
                getattr(req, "system_prompt", "") or "",
                getattr(getattr(req, "conversation", None), "persona_id", None),
            )
        except Exception as e:  # noqa: BLE001 — 截留失败绝不能影响主链路
            logger.debug(f"[{self.metadata_name}] 人格块截留失败（忽略）: {e}")
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
        if self._search_pool_ready():
            text += self._build_search_instruction()
        return text
