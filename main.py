"""
astrbot_plugin_browser_llm — LLM 浏览器插件（内嵌多模态子代理）

架构（v1.7.0）：**主 LLM 只做委派，插件内置一个多模态子代理干活**。

- 外层 **3 个 LLM 工具**（名字一律不改，生产有 6 条人格按名硬白名单）：
  ``browse_web``（唯一委派入口）、``browse_local_page``（本地 HTML 渲染预览，
  不进子代理）、``browser_web_search``（v1.4.0 的搜索 API 池；``search.mode=
  external`` 时作为外层工具存在，``internal`` 时强制停用、改由内层
  ``browse_web_search`` 承载）。
- 内层 **26 个 ``browse_*`` 工具**（``_BROWSER_TOOLS``，``search.mode=internal``
  时 +1 = 27）只进 ``self._browser_toolset``，直接作为 ``tools=`` 传给
  ``context.tool_loop_agent``；它们**不在** ``func_list`` 里 ⇒ 不受人格白名单 /
  ``inactivated_llm_tools`` / ``_PermissionGuardedTool`` 影响。
- 内层模型可由 ``agent.provider_id`` 指定（只列"多模态 + 支持 tool_use"的
  候选；留空 = 继承当前会话）。它**原生看图**：``browse_screenshot`` /
  ``browse_zoom_crop`` 返回 ``mcp.types.CallToolResult``（TextContent +
  ImageContent），图片由核心缓存后在下一轮塞进内层上下文；纯文本内层模型
  则走 ``perception.vision_provider_id`` 的 caption 兜底。
- 内层人格三档（``agent.persona_mode``）：``default``（只有工具提示词）/
  ``current``（当前会话人格，截断 2000 字）/ ``custom``（配置文本）。分层顺序
  固定为 **L0 红线 → L1 人格 → L2 工具 → L3 感知 → L4 输出契约**：人格被
  **包裹**，改不动红线与工具清单。
- **执行台账**（``【执行台账】``）由插件按实际工具调用确定性生成、附在结论
  之后：内层模型改不了它，主 LLM 因此有一个不可伪造的旁证（关闭"收尾失真"）。
- 配置为 **7 个分组 / 54 个叶键**（``_conf_schema.json`` / ``core/config_map.
  KEY_MAP``：``browser``6 / ``limits``7 / ``security``5 / ``perception``7 /
  ``search``17 / ``tools``4 / ``agent``8），由 ``tools/migrate_config_v5.py``
  从 v3/v4 形态迁移（**先迁配置、再换代码**）。

分层落点：本文件 = 入口 + 后台分派 + 结果合成；``core/agent.py`` = 内层子代理
执行体（provider 决策 / 人格分层 / 工具集装配 / 预算与台账 / 结果归类）；
``core/*`` 其余模块 = 浏览器驱动、会话、提取、安全、搜索、配置映射。
"""

import asyncio
import difflib
import functools
import hashlib
import json
import os
import re
import shutil
import time
import weakref
from collections.abc import Awaitable, Callable
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
from .core import agent as agent_core
from .core.background import (
    _BG_MAX_CONCURRENT,
    _BG_BUSY_TEXT,
    C_UNTRUSTED_PREFIX,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_TIMEOUT,
    BackgroundJob,
    BackgroundRegistry,
    render_accepted_text,
    render_c_media_block,
    render_rejected_text,
    render_terminal_text,
    truncate_result,
)
from .core.browser import BrowserCore, validate_browser_type
from .core.config_map import ConfigView, clamp_of, default_of, schema_leaf
from .core.crawler import AiohttpFetcher, CrawlerLimits, FetchOutcome
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
_PLUGIN_VERSION_FALLBACK = "v1.7.0"

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
# - 终态消息**四态四前缀**（设计 §4.7 唯一口径，D3 裁决）：成功
#   「【后台任务完成】」/ 失败「【后台任务失败】」/ 超时「【后台任务超时】」/
#   取消「【后台任务已取消】」——**不要**回落成单前缀，否则失败会被算成成功；
# - 生产判据两套口径各管一件事：``grep -c "后台任务受理"`` 与
#   ``grep -c "后台任务终态 status="``（四态合计）+ 按前缀数成功数；
# - 后台任务在 terminate() 时取消并等待一小段，绝不让插件重载悬挂。
_BACKGROUND_MODES = ("off", "browse_only", "all")

# 后台**机制**维度（v1.6.0，配置 ``tools.background_engine``）：
# - plugin（默认）：沿用 v1.5.0 路线 D，插件自持任务与终态投递；
# - core：把（且只把）``browse_web`` 的 ``is_background_task`` 置真，由核心
#   派发、核心在完成后唤醒一轮主 LLM 交付。
# 两个维度**正交**：``background_mode`` 管"范围"，本键管"机制"——把 core 塞进
# mode 会同时坏掉「off × core 无法表达」「回滚两个动作耦合」「六格要重写文档」
# 三件事。
_BACKGROUND_ENGINES = ("plugin", "core")

# C 模式（平台原生后台）的**永久**范围（D3 冻结）：只有 browse_web。
# - browse_local_page 永久排除：四道路径校验是**安全相关**，越权路径必须
#   保持"当场同步拒绝"，不能退化成唤醒轮里的一条异步失败；
# - browser_web_search 排除：常态 1-3s，后台化 = 多一轮完整主模型调用（净亏）。
_C_SCOPE_TOOLS = frozenset({_BROWSE_WEB_TOOL_NAME})

# 本插件注册的三个 LLM 工具名（置 is_background_task 与归属识别都用它）。
_ALL_TOOL_NAMES = (_BROWSE_WEB_TOOL_NAME, _LOCAL_PAGE_TOOL_NAME, _SEARCH_TOOL_NAME)

# 核心修复指引（探测失败时逐字进 ERROR，方便 fix 对照行号）。两个行号范围都
# 写全：214-250 是 ``_PermissionGuardedTool`` 类与 ``__init__`` 的整段（生产
# 4.29.1），228-244 是实际漏镜像 ``is_background_task`` 的那几行。
_CORE_FIX_HINT = (
    "astrbot/core/provider/func_tool_manager.py:214-250"
    "（``_PermissionGuardedTool``，构造体见 func_tool_manager.py:228-244）"
    "未镜像 is_background_task；修法一行："
    'self.is_background_task = getattr(tool, "is_background_task", False)；'
    "改完必须重启 astrbot.service（核心模块不会随插件重载重新导入）"
)

# 拒绝启用 core 的日志标记（生产判据 grep 它，规格 §3.8）。
_BG_ENGINE_REFUSED = "tools.background_engine=core 拒绝启用"


def c_scope_targets(*, engine: str, mode: str) -> frozenset[str]:
    """本次配置下**应当**置 ``is_background_task=True`` 的工具集合（纯函数）。

    三条矩阵不变量（规格 §1.3）：
    - ``mode == "off"`` ⇒ 空集（D2：机制无关，不探测、不告警、不置标志）；
    - ``engine != "core"`` ⇒ 空集（plugin 引擎**从不**写 flag）；
    - 否则只有 ``_C_SCOPE_TOOLS``（当前恒为 ``{browse_web}``，D3 永久排除 L/S）。
    """
    if mode == "off" or engine != "core":
        return frozenset()
    return _C_SCOPE_TOOLS


def resolve_effective_engine(*, engine: str, mode: str, probe_ok: bool) -> str:
    """生效引擎：只有「想开 core + 有范围 + 探测通过」三者同时成立才是 core。"""
    if mode == "off" or engine != "core":
        return "plugin"
    return "core" if probe_ok else "plugin"


def _c_media_public_root() -> Path | None:
    """C 模式媒体发布根（核心 ``send_message_to_user`` **能发**的目录）。

    为什么不能直接给插件 data 目录下的绝对路径（规格 §6.5）：唤醒轮的
    ``send_message_to_user`` 对本地文件只允许「会话工作区 + ``data/temp`` +
    系统 temp/``.astrbot``」，而本插件的截图落在 ``data/plugin_data/<name>/``；
    生产 ``computer_use_runtime=local`` 且 member 档 ``filesystem_scope`` 不是
    ``host`` ⇒ 那里会被 ``raise PermissionError``。``data/temp`` 对 member/admin
    两档都在允许根内，因此发布到 ``<data>/temp/browser_llm_c_media``。

    取不到（单测桩/旧核心没有该 helper）返回 ``None`` ⇒ 调用方退回原始路径
    （fail-soft：宁可转发可能被拒，也不要静默丢媒体）。
    """
    try:
        from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415 — 延迟导入
            get_astrbot_temp_path,
        )

        return Path(get_astrbot_temp_path()) / "browser_llm_c_media"
    except Exception:  # noqa: BLE001 — 桩环境/旧核心没有该 helper
        return None


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
# 识图缓存键的图像内容哈希位数（v1.6.1）：sha256(截图文件字节) 十六进制
# **前 32 位**（= 128 bit）。写死为常量、不做成配置项：它只用于「是不是
# 同一张图」的判定，32 位在 256 条缓存量级下碰撞概率可忽略，且比全 64 位
# 省一半内存。**不许用时间戳/序号/文件路径冒充**——2026-10-26 事故的根因
# 就是「同 URL 共用一个槽」，路径与序号都区分不出画面变化。
_VISION_IMAGE_HASH_LEN = 32
# 识图缓存键的提示词哈希位数（v1.6.1）：运营方改 perception.vision_prompt
# 后必须重新识图，否则会命中「按旧提示词生成的描述」。
_VISION_PROMPT_HASH_LEN = 8
# 识图默认提示词（v1.6.1 提到模块级）：缓存键要对**实际使用的提示词**取
# 哈希，抽成常量避免默认提示词字面量在多处漂移。
_DEFAULT_VISION_PROMPT = (
    "请用中文描述这张网页截图的重点内容，包括主要文字、按钮、链接和页面结构。"
)
# browse_click_text 失败时的相近候选（v1.6.1）：采集上限 / 展示条数 /
# 单条文本长度上限（过长的容器文本不是「可点击标签」，属噪声）。
_CLICK_TEXT_CANDIDATE_SCAN_LIMIT = 400
_CLICK_TEXT_CANDIDATE_TOP_N = 5
_CLICK_TEXT_CANDIDATE_MAX_LEN = 40

#: ``persona_mode=current`` 的人格缓存 TTL（秒，v1.7.0）。超时视为未命中，
#: 重新走 persona_manager 次路径 —— 人格可能在会话里被切换，缓存太久会用旧人格。
_PERSONA_CACHE_TTL = 30 * 60

#: 会被误读成"插件自己报的"三个前缀（v1.7.0 repair，评审 V4）。只用于
#: **人读的终态消息**加一行消歧说明；模型侧的前缀语义由结构化 meta 决定。
_TRUSTED_LOOKING_PREFIXES = ("【错误】", "【拒绝】", "【不可用】")

#: 页面/内层模型原文以「像插件的错误前缀」开头时，给人加的一行消歧说明。
_ECHO_NOTE = "（以下为内层模型或页面原文，不是插件判定的错误）"

#: C 模式（唤醒轮）台账的行数上限（v1.7.0）：比面板的 ``agent.ledger_max_lines``
#: 更紧，因为这一份要和结论挤在同一个返回体里（结论已经被
#: ``tools.background_max_chars`` 截断，台账再占 24 行就把"结论"淹了）。
_C_MODE_LEDGER_MAX_LINES = 12

#: 影像原生失败且没有可用的 caption 兜底时的**明确**文案（逐字，规格 §2.3
#: 第 3 行）：**不许**静默返回空串让内层模型瞎猜页面长什么样。
_IMAGE_UNAVAILABLE_TEXT = (
    "【不可用】内层模型不支持图片，且未配置可用的识图兜底模型"
    "（perception.vision_provider_id）。请改用 perception=text 或 "
    "browse_get_text 完成任务。"
)

#: ``browse_get_interactive`` 的元素枚举脚本（v1.7.0，规格 §1.3 #25）。
#:
#: 安全边界（规格 §9.3）：**只做 DOM 枚举**——不发网络请求、不读 innerHTML
#: 全文、不执行任何页面给的字符串。返回的 ``selector`` 由注入 JS 计算的 CSS
#: 路径（``#id`` 优先，否则 ``tag:nth-of-type`` 链），可直接喂给
#: ``browse_click_selector``：这条"枚举 → 确定性落点"的链路正是关闭事故
#: 根因②（交互原语太粗、模型只能猜文本）的关键。
_INTERACTIVE_JS = r"""(maxItems, roleFilter) => {
  const sel = [
    'a', 'button', 'input', 'select', 'textarea', 'summary', 'label',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=option]', '[role=switch]',
    '[role=combobox]', '[onclick]', '[tabindex]',
  ].join(',');
  const path = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      if (node.id) { parts[0] = '#' + CSS.escape(node.id); break; }
      node = parent;
    }
    return parts.join(' > ');
  };
  const stateOf = (el) => {
    for (const attr of ['aria-checked', 'aria-pressed', 'aria-selected']) {
      const v = el.getAttribute(attr);
      if (v === 'true' || v === 'false') return v;
    }
    if (el.type === 'checkbox' || el.type === 'radio') {
      return el.checked ? 'true' : 'false';
    }
    return '';
  };
  const out = [];
  const seen = new Set();
  for (const el of Array.from(document.querySelectorAll(sel))) {
    const role = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();
    if (roleFilter && role !== String(roleFilter).toLowerCase()) continue;
    const name = String(
      el.innerText || el.value || el.getAttribute('aria-label')
      || el.getAttribute('title') || el.getAttribute('name') || ''
    ).replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0
      && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
    const selector = path(el);
    const key = selector + '|' + name;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      role: role,
      name: name.slice(0, 60),
      type: (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase(),
      state: stateOf(el),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      selector: selector,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      visible: visible,
    });
    if (out.length >= Math.max(1, Math.min(200, maxItems || 40))) break;
  }
  return out;
}"""

#: ``browse_click_coords`` 读回"点到了什么"的脚本（v1.7.0，规格 §1.3 #19）：
#: ``elementsFromPoint`` 的第一个元素标签（用于坐标点击后的自我纠正）。
_POINT_ELEMENT_JS = r"""(x, y) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return '';
  const name = String(el.innerText || el.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim().slice(0, 40);
  const role = el.getAttribute('role') || el.tagName.toLowerCase();
  return name ? role + '「' + name + '」' : role;
}"""

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
        "description": "网页交互场景专用：按文本内容精准点击页面元素（如按钮、选项），用于非链接的 JS 元素；多个元素含同一文本时用 index 指定第几个。",
        "parameters": _params(
            {"text": ("要点击的元素包含的文本，如确认并继续", "string")},
            {
                "index": (
                    "多个元素包含同一文本时的序号，从 0 开始，默认 0（第一个）",
                    "number",
                )
            },
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
        "description": "网页交互场景专用：设置复选框/单选框/按钮式选项的选中状态（多匹配时用 index 指定第几个）。",
        "parameters": _params(
            {"selector": ("复选框的 CSS 选择器", "string")},
            {
                "checked": (
                    "目标选中状态（true=勾选，false=取消），默认 true",
                    "boolean",
                ),
                "index": (
                    "多个元素匹配同一选择器时的序号，从 0 开始，默认 0（第一个）",
                    "number",
                ),
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
    {
        "name": "browse_get_interactive",
        "description": "列出当前页面上**真实存在**的可交互元素（按钮/链接/选项/输入框等），带可直接点击的 selector 与中心坐标。交互类任务的推荐起手式：先看清单再动手，避免臆造标签。",
        "parameters": _params(
            {},
            {
                "max_items": ("最多返回多少个元素，默认 40", "number"),
                "role_filter": (
                    "只列该 role 的元素（如 button/link/checkbox/radio/tab），留空=全部",
                    "string",
                ),
            },
        ),
        "method": "browse_get_interactive",
    },
    {
        "name": "browse_click_selector",
        "description": "按 CSS 选择器点击元素（browse_get_interactive 给出的 selector 可直接用；多匹配用 index 指定第几个）。点击后读回 aria-checked/aria-pressed 状态。",
        "parameters": _params(
            {"selector": ("CSS 选择器，通常直接取自 browse_get_interactive", "string")},
            {
                "index": (
                    "多个元素匹配同一选择器时的序号，从 0 开始，默认 0（第一个）",
                    "number",
                )
            },
        ),
        "method": "browse_click_selector",
    },
]

#: 内层池搜索工具名（``search.mode=internal`` 时追加的第 27 个内层工具）。
#:
#: 三个名字刻意互不相同，且都与核心内置 ``web_search_*`` 族不同名
#: （``web_search_tools.py`` 会在请求装配时**后加入并覆盖**同名项 —— 见
#: ``main.py`` 顶部 ``_SEARCH_TOOL_NAME`` 的历史教训注释）：
#: - ``browser_web_search``：外层工具（主 LLM 直调）；
#: - ``browse_web_search``：内层工具（子代理调用）；
#: - ``browse_search``：打开搜索引擎结果页（内层，始终存在）。
_INNER_SEARCH_TOOL_NAME = "browse_web_search"

#: 内层搜索返回体上限（字节，规格 §4.3：比外层 24KB 更紧）。
_INNER_SEARCH_MAX_BYTES = 6000

#: 内层池搜索工具定义（参数 = 外层去掉 ``providers``：不让内层模型覆盖
#: provider 顺序，那是运营配置）。
_INNER_SEARCH_TOOL_SPEC = {
    "name": _INNER_SEARCH_TOOL_NAME,
    "description": (
        "网页搜索（本插件自有搜索 API 池，返回结构化结果，无需开页面）。"
        "适合：事实问答、需要来源链接、只需摘要无需页面交互的检索。"
        "不适合：需要登录/点击/翻页/表单的真实页面交互（那用 browse_open / "
        "browse_get_interactive 等）。返回 JSON 字符串：status（ok/partial/"
        "failed/disabled）、results[]（rank/title/url/snippet/fetched）、"
        "error_codes、notes；回答时依据 results[].snippet 或 fetched.text 并给出链接。"
    ),
    "parameters": _params(
        {"query": ("检索词。必填，空串会被拒绝。", "string")},
        {
            "max_results": ("返回条数上限（1-50）。0 表示用插件配置值。", "number"),
            "rounds": ("搜索步数 = 并行波次上限（1-5）。0 表示用插件配置值。", "number"),
            "timeout": ("本次调用总预算秒数（5-300，且不超过内层单步超时）。0 表示用插件配置值。", "number"),
            "fetch_pages": ("是否抓取结果正文（需插件配置 search.fetch_pages 已开启）。", "boolean"),
            "fetch_top_n": ("抓取前 N 条（0-5）。0 表示用插件配置值。", "number"),
        },
    ),
}


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
        )

    return agent_core.build_function_tool(
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
    "- browse_get_interactive：列出页面上**真实存在**的可交互元素（角色/名称/"
    "选中态/是否禁用/可直接点击的 selector/中心坐标）——交互类任务先看它；\n"
    "- browse_click_selector：按 browse_get_interactive 给出的 selector 点击"
    "（多匹配用 index 指定第几个）；\n"
    "- browse_click_text：按文本内容精准点击元素（按钮/选项）；\n"
    "- browse_set_slider / browse_select_option / browse_checkbox：滑块/"
    "下拉框/复选框设置（browse_checkbox 多匹配时用 index 指定第几个，"
    "从 0 开始，按钮式选项也可用）；\n"
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
    "9. 标签必须逐字取自页面：点击/选择任何选项前，其文本必须能在 "
    "browse_get_interactive / browse_get_text / browse_current_page 的返回里"
    "找到**原文字符串**；不要用近义词、同义改写或凭印象编造的标签（臆造标签"
    "必然点击失败并浪费等待时间）。工具返回「相近可点击文本」时，只能从中"
    "挑选重试；\n"
    "10. 交互类任务的标准动线：先 browse_get_interactive 看清有哪些元素 ⇒ "
    "用 browse_click_selector（或 browse_click_text）点 ⇒ 用 browse_get_text "
    "读回结果确认状态；不要靠猜坐标或猜文本硬点；\n"
    "11. 完成后用简洁中文总结浏览结果（URL、关键内容），不要暴露工具调用"
    "过程细节。\n\n"
    "提交后等待结果（重要）：\n"
    "- 点击生成/提交类按钮后，不要刷新页面、不要重新开始、不要后退，"
    "避免结果页丢失；\n"
    "- 用 browse_get_text / browse_current_page 周期性重读当前页面等待"
    "结果出现，每次读取间隔 5-8 秒，最多等待 60 秒；\n"
    "- 若页面仍在生成中（进度指示/空白），继续等待重读，不要中途放弃；\n"
    "- 拿到结果后，原样返回结果页的关键文字。"
)

# browse_click_text 失败时的「相近可点击文本」采集脚本（v1.6.1）。
# 只收**看起来可点**的元素（标签 / role / onclick / tabindex），文本取
# innerText→value→aria-label 后压空白；按小写文本去重。长度上限与展示
# 条数在 Python 侧用 _CLICK_TEXT_CANDIDATE_* 常量控制（JS 里不掺 Python
# 值，避免字符串插值）。每条带 tag（含 role）与 visible（尺寸 +
# display/visibility/opacity 判定）——事故里模型最缺的就是「页面上真实
# 存在的文本长什么样、能不能点」。
_CLICK_TEXT_CANDIDATES_JS = r"""() => {
  const sels = [
    'a', 'button', 'label', 'option', 'summary',
    'input[type=submit]', 'input[type=button]', 'input[type=radio]',
    'input[type=checkbox]',
    '[role=button]', '[role=option]', '[role=radio]', '[role=checkbox]',
    '[role=tab]', '[role=menuitem]', '[role=link]', '[onclick]', '[tabindex]',
  ].join(',');
  const out = [];
  const seen = new Set();
  for (const el of Array.from(document.querySelectorAll(sels))) {
    const raw = el.innerText || el.value || el.getAttribute('aria-label') || '';
    const text = String(raw).replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const r = el.getBoundingClientRect();
    const cs = window.getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0
      && cs.visibility !== 'hidden' && cs.display !== 'none'
      && cs.opacity !== '0';
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    out.push({
      text: text,
      type: role ? tag + '[' + role + ']' : tag,
      visible: visible,
    });
    if (out.length >= 2000) break;
  }
  return out;
}"""


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

    # 后台**机制**（v1.6.0）与探测态的类级默认：真实实例由 ``_load_config`` /
    # ``_sync_background_engine`` 覆盖；测试直接构造实例（不走 initialize）时
    # 也保有安全默认（plugin ⇒ 与 v1.5.0 行为逐字节一致）。
    background_engine: str = "plugin"
    background_engine_effective: str = "plugin"
    _bg_core_ok: bool = True
    _bg_probe_reason: str = ""
    _bg_probe_sig: tuple | None = None
    _bg_clash_warned: frozenset = frozenset()

    def __init__(self, context: Context, config: AstrBotConfig = None):
        """初始化插件：读取全部配置项到 self.* 属性。

        Args:
            context: AstrBot 插件上下文，提供事件注册与消息发送能力。
            config: 插件配置对象，对应 _conf_schema.json 的 6 个分组 /
                45 个叶键（browser / limits / security / perception /
                search，以及 tools 组 4 键：v1.5.0 的 3 个后台键 + v1.6.0
                的 background_engine）；未传入时用空字典兜底。
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

        # 识图短时缓存（v1.3.0；v1.6.1 换键）：键是七元组
        # (umo, 规范化URL, 工具种类, 裁剪参数, 图像内容哈希, 提示词哈希,
        #  provider_id) -> (写入时间戳, 描述文本)，见 _vision_cache_key。
        # v1.6.1 之前键只有 (umo, 规范化URL)：同一 URL 下所有截图/裁剪共用
        # 一个槽，交互改变画面后仍返回上一张（甚至局部裁剪）的描述
        # （2026-10-26 事故主因）。
        # 并发安全说明：插件运行在 asyncio 单线程事件循环内，dict 读/写天然
        # 原子，无需额外锁；同一键并发首次识图可能重复调用 LLM（无害，
        # 结果一致，仅多一次 token 消耗），不引入锁以保持实现简单。条目按
        # TTL 惰性过期，超过 _VISION_CACHE_MAX_ENTRIES 时清理过期项防膨胀，
        # terminate 时整体清空（防重载残留）。
        self._vision_cache: dict[tuple, tuple[float, str]] = {}

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

        # 后台引擎探测态（v1.6.0）：类属性给了默认值，这里给**实例**副本，
        # 避免多实例共享可变对象；``_bg_probe_sig`` 为 None 表示"还没探过"。
        self._bg_core_ok = True
        self._bg_probe_reason = ""
        self._bg_probe_sig = None
        self._bg_clash_warned: set[str] = set()

        # 内层子代理（v1.7.0）：
        # - ``_browser_tools`` / ``_browser_toolset``：26（internal 搜索时 27）
        #   个 FunctionTool 与它们的 ToolSet，由 ``_rebuild_browser_toolset()``
        #   按 ``(search.mode, 池可用性)`` 签名重建；
        # - ``_toolset_sig``：上一次构建用的签名（签名不变就不重建——否则每次
        #   工具调用都要重建 26 个 FunctionTool）；
        # - ``_persona_cache``：umo -> (monotonic 时间, persona_id, 人格正文)。
        #   ``current`` 档从 on_llm_request 的 system_prompt 里切人格块后缓存，
        #   **只缓存文本**：不落日志、不写文件（规格 §5.2 / §9.4）；
        # - ``_fg_inflight``：umo -> 正在跑的**前台** browse_web 数（只观测告警，
        #   不做单飞拦截：前台入口持锁会与内层工具锁互死，见 main.py 入口注释）。
        self._browser_tools: list = []
        self._browser_toolset: object | None = None
        self._toolset_sig: tuple | None = None
        self._agent_sync_task: asyncio.Task | None = None
        self._persona_cache: dict[str, tuple[float, str | None, str]] = {}
        self._fg_inflight: dict[str, int] = {}

    def _load_config(self) -> None:
        """将 _conf_schema.json 中的配置项读取为实例属性。

        全部 7 组 54 叶（``_conf_schema.json`` / ``core.config_map.KEY_MAP``）：
        ``browser``（引擎/默认页/搜索引擎/视口/代理/超时）、``limits``（提取与
        链接上限/页数/空闲回收/子代理步数与超时）、``security``（禁词/审核总开关/
        内网拦截/会话黑白名单）、``perception``（感知模式/截图/识图 provider 与
        缓存；v1.7.0 起三个 vision 键**只在 caption 兜底路径**生效）、
        ``search``（v1.4.0 的搜索 API 池 16 键 + v1.7.0 的 ``mode``）、
        ``tools``（v1.5.0 后台 3 键 + v1.6.0 的 ``background_engine``）、
        ``agent``（v1.7.0 新增整组 8 键：内嵌多模态子代理）。

        数值/布尔/枚举配置经 _as_int / _as_float / _as_bool / _as_enum 归一解析，
        非法值（字符串、None、类型错误、越界）回退默认并记录 warning——手动编辑
        config.json 或第三方写入非法类型时插件仍可正常加载，不再抛异常阻断初始化。

        生效时机（``core.config_map.EFFECT_TIMING``）：本方法读取的绝大多数键是
        ``hot``（``_refresh_config()`` 每次工具调用重读），``browser.type`` /
        ``browser.proxy`` / ``browser.viewport`` 是 ``import``（要重载插件），
        ``security.block_internal_ip`` 是 ``new_context``。
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

        # ---- search.mode（v1.7.0）：池搜索挂在哪一层 ----
        # external（默认）= 外层工具 browser_web_search，主 LLM 直调（= v1.6.0
        # 行为逐字节一致）；internal = 内层工具 browse_web_search，外层强制
        # active=False。两档**互斥**：同时有会让主 LLM 与内层都去搜、结果重复、
        # token 翻倍，还多一个"该用哪个"的决策面（规格 §4.1）。
        # 非法值 fail-closed 到 external 并记 warning（宁可维持现状，也不要
        # "写错一个词就把搜索挪进子代理"）。
        raw_search_mode = view.get("search.mode", default_of("search.mode", "external"))
        self.search_mode: str = self._as_enum(
            "search.mode", raw_search_mode, "external", ("external", "internal")
        )

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
        # 后台**机制**（v1.6.0）：plugin（默认，v1.5.0 路线 D）/ core（平台原生
        # is_background_task）。与 background_mode 正交（mode 管范围、engine 管
        # 机制）。非法值 fail-closed 到 plugin：宁可维持已验收的行为，也不要
        # "写错一个词就换机制"（换了机制，回滚路径与交付确定性全都变）。
        raw_engine = view.get("tools.background_engine", "plugin")
        engine = str(raw_engine or "plugin").strip().lower()
        if engine not in _BACKGROUND_ENGINES:
            logger.warning(
                f"[{self.metadata_name}] tools.background_engine={raw_engine!r} 非法"
                f"（可选 {'/'.join(_BACKGROUND_ENGINES)}），已按 plugin 处理（插件自管后台）。"
            )
            engine = "plugin"
        self.background_engine: str = engine

        # ---- agent 组（v1.7.0）：内嵌多模态子代理的 8 个键 ----
        # 全部 hot（走既有 _refresh_config 路径）。默认值与
        # ``core.config_map.LEAF_DEFAULTS`` 逐条相等（由 tests 钉住），
        # 因此"旧配置 + 新代码"的行为与 v1.6.0 一致。
        self.agent_provider_id: str = str(
            view.get("agent.provider_id", default_of("agent.provider_id", "")) or ""
        ).strip()
        self.agent_persona_mode: str = self._as_enum(
            "agent.persona_mode",
            view.get("agent.persona_mode", default_of("agent.persona_mode", "default")),
            "default",
            agent_core.PERSONA_MODES,
        )
        self.agent_persona_prompt: str = str(
            view.get("agent.persona_prompt", default_of("agent.persona_prompt", "")) or ""
        )
        self.agent_task_timeout: int = self._as_clamped_int(
            "agent.task_timeout",
            view.get("agent.task_timeout", default_of("agent.task_timeout", 600)),
            600,
        )
        self.agent_image_max_side: int = self._as_clamped_int(
            "agent.image_max_side",
            view.get("agent.image_max_side", default_of("agent.image_max_side", 1280)),
            1280,
        )
        self.agent_image_quality: int = self._as_clamped_int(
            "agent.image_quality",
            view.get("agent.image_quality", default_of("agent.image_quality", 80)),
            80,
        )
        self.agent_result_max_chars: int = self._as_clamped_int(
            "agent.result_max_chars",
            view.get("agent.result_max_chars", default_of("agent.result_max_chars", 3000)),
            3000,
        )
        self.agent_ledger_max_lines: int = self._as_clamped_int(
            "agent.ledger_max_lines",
            view.get("agent.ledger_max_lines", default_of("agent.ledger_max_lines", 24)),
            24,
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
        # 后台引擎（v1.6.0）：必须紧随 ① 读配置之后——置 is_background_task 标志、
        # 必要时（签名变化）探测能力、core 不可用时**先清 flag 再**降级到 plugin。
        # 方法内部已 try/except 收口，不会因引擎问题打断热更新（规格 §4.2）。
        self._sync_background_engine()
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
        # 构建 26 个浏览工具（FunctionTool）与子 Agent 工具集；``search.mode=
        # internal`` 时追加第 27 个（内层池搜索）。重建逻辑只在这里与
        # ``_refresh_config`` 的签名变化处调用（单一定义 _rebuild_browser_toolset）。
        self._rebuild_browser_toolset()
        # 按页面感知方式动态生成子代理指令（基础模板 + 感知方式段）。
        self._browser_instruction = self._build_subagent_instruction()
        # 识图 provider 下拉同步：把 AstrBot 已加载 provider 写进内存 schema
        # 的 options（Dashboard 面板每次请求实时读取该 schema，无需重启）。
        # 注意 AstrBot 启动顺序是「插件加载先于 provider 初始化」，此处可能
        # 暂时拿不到 provider 列表，由后台轮询任务兜底补齐。
        self._vision_sync_task: asyncio.Task | None = None
        self._sync_vision_provider_options()
        self._start_vision_provider_sync_task()
        # 内层模型下拉同步（v1.7.0）：只列「多模态 + 支持 tool_use」的候选
        # （规格 §3.1）。同一套启动期兜底轮询。
        self._sync_agent_provider_options()
        self._start_agent_provider_sync_task()
        logger.info(
            f"[{self.metadata_name}] 浏览器子代理工具集: "
            f"{len(self._browser_tools)} 个浏览工具"
            f"（search.mode={self.search_mode}）"
        )
        logger.info(f"[{self.metadata_name}] 页面感知方式: {self.page_perception}")
        logger.info(
            f"[{self.metadata_name}] 内容审核（禁词过滤）: "
            f"{'开启' if self.enable_banned_filter else '关闭'}"
        )
        # 搜索工具可见性对齐（v4）：配置是唯一事实源，initialize() 双向收敛
        # "持久化 inactivated_llm_tools 名单"与配置之间的分叉。
        # v1.7.0：want 里多了 ``search.mode == "external"`` 这一条（internal
        # 档外层**恒**停用，模型根本看不到它 —— 规格 §4.2）。
        await self._reconcile_search_tool()
        self._search_reconcile_sig = self._toolset_signature()
        # 后台引擎（v1.6.0）：重载后 ``llm_tools.func_list`` 是**全新对象**
        # （核心 remove_func(name) + append），旧对象上的 flag 不会被继承 ⇒
        # 必须重放；并在加载后强探测一次（核心修复 + 重启后靠它自动恢复 core）。
        self._bg_probe_sig = None
        self._sync_background_engine(force_probe=True)
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

    # ------------------------------------------------------------
    # 内层工具集装配（v1.7.0，规格 §1.3 / §4.2）
    # ------------------------------------------------------------

    def _toolset_signature(self) -> tuple:
        """内层工具集的**签名**：只有它变了才需要重建 26 个 FunctionTool。

        两个维度：``search.mode``（决定第 27 个条件工具在不在）与"池是否
        可用"（``search.enabled`` + 至少一个可用 provider）。签名不变就不重建
        —— 否则每次工具调用都要重建一整套 FunctionTool 对象（纯浪费）。
        """
        return (
            getattr(self, "search_mode", "external"),
            bool(self._search_pool_ready()),
        )

    def _rebuild_browser_toolset(self) -> None:
        """（重）建内层工具集：26 个 ``browse_*`` + 条件的内层池搜索。

        ``_BROWSER_TOOLS`` 里**不**含 ``browse_web_search``：它是运行时按
        ``search.mode=internal`` 追加的第 27 个（判据 §1.3-1 明确要求
        ``len(_BROWSER_TOOLS) == 26``，条件工具不进那个字面量）。
        """
        self._browser_tools = [_make_browser_tool(self, s) for s in _BROWSER_TOOLS]
        if self.search_mode == "internal" and self._search_pool_ready():
            self._browser_tools.append(self._make_inner_search_tool())
        self._browser_toolset = self._build_toolset(self._browser_tools)
        self._toolset_sig = self._toolset_signature()

    def _apply_toolset_signature(self) -> bool:
        """签名变化时重建内层工具集；返回是否真的重建了。"""
        if getattr(self, "_toolset_sig", None) == self._toolset_signature():
            return False
        self._rebuild_browser_toolset()
        return True

    async def _sync_search_mode(self) -> None:
        """按 ``search.mode`` 收敛两处状态（幂等；三个外层入口开头调用）。

        1. 内层工具集（``internal`` 时第 27 个工具在/不在）；
        2. 外层 ``browser_web_search`` 的 ``active``（internal ⇒ 恒停用）。

        为什么不在 ``_refresh_config()`` 里做第 2 步：那一步是 async
        （``activate/deactivate_llm_tool_async``），而 ``_refresh_config`` 被
        内层工具 handler 在**每次调用**时同步调用；在这里做既保持入口的
        确定性（await 完再执行任务），也不会让内层工具把外层工具翻来翻去。

        **性能/副作用闸**：``activate/deactivate_llm_tool_async`` 会写**全局
        持久化**名单 ``inactivated_llm_tools``（DB 写）。因此签名没变就**不**再
        调一次 —— 否则每次 ``browse_web`` 都白写一次配置。签名 = ``(mode, 池可用
        性)``，与内层工具集共用同一份定义。
        """
        sig = self._toolset_signature()
        rebuilt = self._apply_toolset_signature()
        if not rebuilt and getattr(self, "_search_reconcile_sig", None) == sig:
            return
        await self._reconcile_search_tool()
        self._search_reconcile_sig = sig

    def _make_inner_search_tool(self):
        """构造内层池搜索工具 ``browse_web_search``（仅在 internal 档注册）。

        三条设计约束（规格 §4.2-2 / §4.3）：

        1. **同一个执行体**：直接复用 ``_run_browser_web_search``（含
           ``_search_url_guard`` / ``_filter_banned_results`` / 冷却 / TTL
           缓存），绝不另写一条 HTTP 路径；参数比外层少一个 ``providers``
           （provider 顺序是运营配置，不让内层模型覆盖）；
        2. **体积更紧**：``max_bytes=6000``（外层 ``PAYLOAD_MAX_BYTES``=24KB），
           内层还要再叠 ``agent.result_max_chars`` 截断；
        3. **永不后台化**：内层工具不在 ``llm_tools.func_list`` 里，核心的
           ``is_background_task`` 机制看不到它；它的执行发生在
           ``tool_loop_agent`` 之内，后台粒度只到外层工具（规格 §4.4）。
        """
        spec = _INNER_SEARCH_TOOL_SPEC

        async def _handler(event, **kwargs):
            self._refresh_config()
            # timeout 必须先``pop`` 再合成：内层模型的 schema 里**有** ``timeout``
            # 参数（见 ``_INNER_SEARCH_TOOL_SPEC``），若直接 ``**kwargs`` 透传又
            # 同时显式传 ``timeout=``，Python 会抛
            # ``TypeError: got multiple values for keyword argument 'timeout'``
            # —— 模型一旦真的传了它，整个内层搜索就变成一条工具异常。
            given = kwargs.pop("timeout", 0)
            budget = min(
                float(self.search_total_timeout), float(self.agent_tool_timeout)
            )
            try:
                given_n = int(given or budget)
            except (TypeError, ValueError):
                given_n = int(budget)
            # 合成**一次**：模型给的预算先夹进 [5,300]，再与插件预算取小；
            # 结果再夹一次（``budget`` 可能比 5 还小，例如 agent_tool_timeout=3）。
            given_n = int(_clamp(given_n, 5, 300))
            budget = min(budget, given_n)
            raw = await self._run_browser_web_search(
                event,
                max_bytes=_INNER_SEARCH_MAX_BYTES,
                timeout=int(_clamp(budget, 5, 300)),
                **kwargs,
            )
            return agent_core.record_and_truncate(
                raw,
                tool=spec["name"],
                args=kwargs,
                run=agent_core.current_run(),
            )

        return agent_core.build_function_tool(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["parameters"],
            handler=_handler,
        )

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
        base = f"{_BROWSER_AGENT_INSTRUCTION}\n\n{perception_note}"
        # v1.7.0 repair（评审 R9）：``browse_web_search`` 是**条件工具**（只有
        # ``search.mode=internal`` 且池就绪时才在内层工具集里），因此它不在
        # ``_BROWSER_AGENT_INSTRUCTION`` 的固定清单里，必须在这里按当前配置
        # 动态补一行 —— 否则模型看得到工具却不知道它是什么（工具清单与指令
        # 不一致是幻觉调用的来源）。
        if self.search_mode == "internal" and self._search_pool_ready():
            base = (
                f"{base}\n\n"
                "池搜索工具（本部署 search.mode=internal，已挂在本工具集里）：\n"
                "- browse_web_search：用配置的搜索 API 池检索（返回结构化结果，"
                "不打开页面）"
            )
        return base

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
        perception: str | None = None,
        *,
        base: str | None = None,
        event: AstrMessageEvent | None = None,
    ) -> str:
        """合成内层 system_prompt：**L0 红线 → L1 人格 → L2 工具 → L3 感知 → L4 契约**。

        分层顺序写死在 ``core.agent.compose_inner_prompt``（结构性保证：人格层
        没有位置去改写工具清单或覆盖红线，它是被**包裹**而不是替换）。本方法
        只负责准备 L1 的文本：

        - ``default``：L1 空（= v1.6.0 行为逐字节一致）；
        - ``current``：当前会话人格（截断 2000 字符；失败可见降级到 default）；
        - ``custom``：``agent.persona_prompt``（截断 4000 字符；空值 ⇒ 等价
          default + WARNING，不静默）。

        Args:
            perception: 本次感知模式（L3 用）。
            base: 预构建的 L2+L3 文本；None 时按 perception 现算。
            event: 消息事件（``current`` 档取会话人格用），可为 None。
        """
        base_text = base if base is not None else self._build_subagent_instruction(
            perception
        )
        mode = self.agent_persona_mode
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
                    f"[{self.metadata_name}] persona_mode=custom 但 "
                    "agent.persona_prompt 为空，本次按 default 档执行"
                )
        return agent_core.compose_inner_prompt(
            base=base_text,
            persona_mode=mode,
            persona_text=persona_text,
        )

    def _cleanup_cache(self) -> int:
        """清理超过 cache_days 天的媒体/截图缓存文件，返回删除数。

        两段，遍历口径**刻意不同**：

        1. 既有基目录 ``data/media/`` 与 ``data/screenshots/``：只看**顶层
           文件**（``iterdir``，与 v1.5.0 逐字一致）。这两处只有本插件自己写的
           平铺文件，把删除语义悄悄改成"递归"会误删运维手工放进子目录的东西，
           也超出 v1.6.0 的落点范围（规格 §6.5/§10 只要求"遍历基目录加入发布
           根"）；
        2. v1.6.0 的 C 模式**发布根** ``data/temp/browser_llm_c_media/``：它
           按 ``<umo8>/`` 分目录，因此必须 ``rglob`` 递归；删完过期文件后把
           空的分目录也回收（否则 data/temp 会剩一堆空目录）。

        目录本身保留；``cache_days <= 0`` 表示「不清理」（与
        ``vision_cache_ttl=0`` 关闭缓存的语义一致），直接返回 0——避免 0 被
        误当作「立即清理全部」。
        """
        if self.cache_days <= 0:
            return 0
        cutoff = time.time() - self.cache_days * 86400
        removed = 0
        # ① 既有基目录：保持 v1.5.0 的"只删顶层文件"语义（不递归）。
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
        # ② C 模式发布根：按 <umo8> 分目录，必须递归；清空后回收空分目录。
        publish_root = _c_media_public_root()
        if publish_root is not None and publish_root.is_dir():
            for f in sorted(publish_root.rglob("*")):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except Exception as e:  # noqa: BLE001 — 单文件失败不影响其余
                    logger.debug(f"清理缓存文件失败 {f}: {e}")
            for child in sorted(publish_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    child.rmdir()
                except OSError:  # 非空/竞态：下一轮再收
                    continue
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

    # ------------------------------------------------------------
    # 内层 provider（v1.7.0，规格 §3）：唯一解析点 + 面板候选同步
    # ------------------------------------------------------------

    def _live_agent_provider_id(self) -> str:
        """实时读取 ``agent.provider_id``（面板保存即生效，不必等重载）。

        语义与 ``_live_vision_provider_id`` 一致：空串 = **继承当前会话模型**
        （默认，等于 v1.6.0 行为），非空 = 运营方显式指定。
        """
        view = ConfigView(self.config)
        value, found = view.lookup("agent.provider_id")
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

        与 ``_sync_vision_provider_options`` 同款做法：Dashboard 每次请求直接
        读 ``plugin_md.config.schema``（同一对象），就地改 ``options`` 即生效；
        ``""`` 空选项恒保留（= 继承当前会话），default 不在候选里时归零。
        """
        options = [""] + agent_core.agent_provider_candidates(
            self._collect_provider_configs()
        )
        options = list(dict.fromkeys(options))
        schema = getattr(self.config, "schema", None)
        if isinstance(schema, dict):
            item = schema_leaf(schema, "agent.provider_id")
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
            # v1.7.0 repair（评审 R6）：运营方必须能在日志里看到"谁在兜底识图"。
            # 生产观测清单（verification/20261026-v170-repair-t10-report.md）里有
            # 这条的 grep 口径。
            caption_id = self._live_vision_provider_id()
            logger.info(
                f"[{self.metadata_name}] 内层模型非多模态，改用识图兜底 "
                f"provider={caption_id or '（空=未配置，截图将不可用）'}"
                f"（内层模型={decision.provider_id}）"
            )
        return decision

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
                fallback = await self._current_chat_provider_id(event)
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
        # v1.7.0 repair（评审 R8）：内层模型下拉同步任务与识图那个是**两个**任务，
        # 只停一个会在重载后留下一个最长跑 120s 的轮询协程。
        self._stop_agent_provider_sync_task()
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
            # 人格正文缓存清空（v1.7.0 repair，评审 R8）：人格是**运营方写的文本**，
            # 与聊天正文同级的隐私内容，重载/禁用后不该留在内存里。
            self._persona_store().clear()
            # 搜索服务缓存/冷却清空（密钥随服务实例一起释放）。
            if self._search_service is not None:
                self._search_service.clear_cache()
            self._search_service = None
            self._search_service_signature = None
            self._search_tool_active = False
            # 后台任务登记表兜底清空（设计 §6.1）：cancel_all 已清过，这里再清
            # 一次是防"表非空但无在跑任务"那类残留把会话永久锁在单飞里。
            self._background.clear()
        logger.info(f"{tag} 已终止")

    # ================================================================
    # 后台任务（v1.5.0 路线 D：插件内闭环，不依赖核心 is_background_task）
    # ================================================================

    #: 视频类媒体扩展名（``browse_sniff_media`` 会把视频交给 ``_emit_media``，
    #: 统一按 Image 发会失败，必须按扩展名分流 —— 见 ``_deliver_terminal``）。
    _VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv")

    def _background_enabled_for(self, tool_name: str) -> bool:
        """该工具本次是否走 **plugin 引擎**的后台路径（v1.5.0 语义 + 一条新闸）。

        - ``off``（默认）：三个工具一律前台同步，行为与 v1.4.0 完全一致；
        - ``browse_only``：只后台化 ``browse_web``——它是唯一 68s 量级的长
          任务（一次调用跑整套子 Agent），也是唯一"用户要等一整段多步浏览"
          的入口；
        - ``all``：三个工具都后台化（``browser_web_search`` 常态 1-3s，后台化
          等于多一次投递，只在明确要求"任何工具都不阻塞会话"时才开）。

        v1.6.0 新闸：``background_engine_effective != "plugin"`` 时**恒 False**
        —— core 生效时插件登记表必须恒空、不得出现【已受理】（互斥契约）。
        因此 ``mode=all × engine=core`` 下 L/S 走前台同步，**不**回落本路径。
        """
        if getattr(self, "background_engine_effective", "plugin") != "plugin":
            return False
        mode = getattr(self, "background_mode", "off")
        if mode == "all":
            return True
        if mode == "browse_only":
            return tool_name == _BROWSE_WEB_TOOL_NAME
        return False

    # ----------------------------------------------------------------
    # 后台引擎（v1.6.0）：置标志 / 重放 / 能力探测 / 降级
    # ----------------------------------------------------------------

    def _background_c_mode_for(self, tool_name: str) -> bool:
        """该工具本次是否走 **C 路径**（平台原生后台任务）。

        条件源与 flag 同源（``c_scope_targets``），另加一条 ``effective=="core"``：
        探测失败时 flag 已被清、effective 已回落 plugin，这里自然为 False。
        """
        targets = c_scope_targets(
            engine=getattr(self, "background_engine", "plugin"),
            mode=getattr(self, "background_mode", "off"),
        )
        return (
            bool(targets)
            and getattr(self, "background_engine_effective", "plugin") == "core"
            and tool_name in _C_SCOPE_TOOLS
        )

    def _own_llm_tools(self) -> dict[str, object]:
        """name -> 本插件的**原始** ``FuncTool``（只含 ``_ALL_TOOL_NAMES`` 三个名字）。

        归属判定沿用既有实现（``_is_own_search_tool`` 的三级判定：模块路径 →
        ``handler.__module__`` → ``partial.func.__module__``），不新造第二套。
        同名但属别人者**跳过**（fail-closed：绝不碰别人注册的对象），每个名字
        只记一次 warning（本方法在热路径上，每次工具调用都会跑）。
        """
        try:
            from astrbot.core.provider.register import llm_tools  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — 桩/早期环境没有注册器
            return {}
        own: dict[str, object] = {}
        clashed: list[str] = []
        for func in list(getattr(llm_tools, "func_list", []) or []):
            name = str(getattr(func, "name", "") or "")
            if name not in _ALL_TOOL_NAMES:
                continue
            if not self._is_own_llm_tool(func):
                clashed.append(name)
                continue
            own[name] = func  # 后注册覆盖前一个（与 ToolSet.add_tool 同向）
        if clashed:
            warned = getattr(self, "_bg_clash_warned", None)
            if not isinstance(warned, set):
                warned = set()
                self._bg_clash_warned = warned
            fresh = sorted(set(clashed) - warned)
            if fresh:
                warned.update(fresh)
                logger.warning(
                    f"[{self.metadata_name}] 这些同名工具不属于本插件，"
                    f"已跳过后台标志置位：{fresh}（不碰其它插件的对象）"
                )
        return own

    def _apply_background_flags(self, targets: frozenset[str]) -> int:
        """把 ``targets`` 里的工具置 True、其余本插件工具置 False；返回改动数。

        只在值不同时赋值：``_refresh_config`` 让本方法在**每次工具调用**都会
        被调到，零改动必须是零写入。置位失败只记 warning 不抛——由
        ``_probe_background_capability`` 的前置分支判成"不可用"并降级。
        """
        changed = 0
        for name, tool in self._own_llm_tools().items():
            want = name in targets
            if bool(getattr(tool, "is_background_task", False)) == want:
                continue
            try:
                tool.is_background_task = want
                changed += 1
            except Exception as exc:  # noqa: BLE001 — 只读/被替换的对象
                logger.warning(
                    f"[{self.metadata_name}] 置 {name}.is_background_task={want} 失败："
                    f"{type(exc).__name__}"
                )
        return changed

    def _bg_epoch(self) -> tuple:
        """本插件三个原始工具的对象身份快照。

        用 ``id()`` 而不是 ``handler``/``repr``：插件重载后核心走
        ``remove_func(name)`` + ``append``（新对象），``id`` 必变；同一对象在
        生命周期内 ``id`` 稳定。空 dict（桩环境/注册未完成）⇒ ``()``，探测会走
        "找不到原始对象"分支（fail-closed），符合预期。
        """
        own = self._own_llm_tools()
        return tuple(sorted((name, id(tool)) for name, tool in own.items()))

    def _probe_background_capability(self) -> tuple[bool, str]:
        """探测平台原生后台是否真的可用。返回 ``(是否可用, 失败原因)``。

        **为什么必须探测**：生产核心的 ``_PermissionGuardedTool.__init__``
        （``func_tool_manager.py:228-244``）只镜像 ``active`` 与
        ``handler_module_path``，**不镜像** ``is_background_task``；而
        ``get_full_tool_set()`` 每次调用都重建包装器。于是"插件把原始
        ``FuncTool`` 的 flag 置真"对 ``tools=null`` 人格（生产主力）**恒无效
        且静默**——工具照旧前台阻塞。探测就是量这件事：原始 True / 包装 False
        ⇒ 核心未修。

        三分支（**任何取不到的东西都算不可用**，D5 fail-closed）：
        - A 可用：每个 target 的原始 flag ``is True`` **且**包装对象 flag ``is True``；
        - B 核心未修：原始 True / 包装 False；
        - C 无法探测：任一取不到（``None`` / 属性缺失 / 抛异常 / 同名冲突）。
        """
        targets = c_scope_targets(
            engine=getattr(self, "background_engine", "plugin"),
            mode=getattr(self, "background_mode", "off"),
        )
        if not targets:
            # 没有目标：不探测（D2）。有效引擎由 mode 决定，永不报错。
            return True, ""

        raw_map = self._own_llm_tools()
        for name in sorted(targets):
            raw = raw_map.get(name)
            if raw is None:
                return False, f"func_list 中找不到本插件的原始工具 {name}（注册未完成？）"
            if getattr(raw, "is_background_task", None) is not True:
                return False, (
                    f"原始工具 {name} 的 is_background_task 未被置真"
                    f"（置位失败或被重注册覆盖）"
                )

        try:
            tmgr = self.context.get_llm_tool_manager()
        except Exception as exc:  # noqa: BLE001
            return False, f"get_llm_tool_manager() 不可用：{type(exc).__name__}"
        if tmgr is None:
            return False, "get_llm_tool_manager() 返回 None"
        try:
            toolset = tmgr.get_full_tool_set()
        except Exception as exc:  # noqa: BLE001
            return False, f"get_full_tool_set() 抛错：{type(exc).__name__}"
        if toolset is None or not hasattr(toolset, "get_tool"):
            return False, "get_full_tool_set() 返回值不具备 get_tool()"

        for name in sorted(targets):
            wrapper = toolset.get_tool(name)
            if wrapper is None:
                return False, f"get_full_tool_set() 中找不到包装对象 {name}"
            wrapper_flag = getattr(wrapper, "is_background_task", None)
            if wrapper_flag is None:
                return False, f"包装对象 {name} 没有 is_background_task 属性"
            # 身份软校验：取不到 _wrapped 就跳过（换实现的核心不该被判失败），
            # 取得到却不是本插件的对象 ⇒ 同名冲突 ⇒ 不可用。
            wrapped = getattr(wrapper, "_wrapped", None)
            if wrapped is not None and wrapped is not raw_map[name]:
                return False, f"包装对象 {name} 包的不是本插件的原始对象（同名冲突）"
            if bool(wrapper_flag) is not True:
                return False, (
                    f"包装对象 {name} 的 is_background_task 为 False 而原始对象为 True"
                    f"（核心未镜像该标志）"
                )
        return True, ""

    def _sync_background_engine(self, *, force_probe: bool = False) -> str:
        """置标志 → （必要时）探测 → 定档 / 降级。幂等，热路径调用。

        探测时机（唯一口径，规格 §3.6）：``initialize()`` 强探一次；``_refresh_config()``
        只在「有目标」且 ``(engine, mode, epoch)`` 签名变化时探；``mode=off`` /
        ``engine=plugin`` 一律不探（D2）。**不引入时间维度**：不设定时重试，
        探测结果只在签名变化时重算（可复现、可测）。

        降级顺序（写死）：core（探测 A）→ plugin（探测 B/C 失败或引擎异常）→
        off（plugin 引擎自身不可用；v1.6.0 无触发路径，保留为"禁止静默前台"的
        唯一合法兜底）。全流程不把异常抛给调用方（工具入口第一行会调它）。
        """
        try:
            engine_cfg = getattr(self, "background_engine", "plugin")
            mode = getattr(self, "background_mode", "off")
            targets = c_scope_targets(engine=engine_cfg, mode=mode)

            # 1) 先按"假设 core 可用"写标志；探测失败时步骤 4 再清。
            epoch = self._bg_epoch()
            self._apply_background_flags(targets)

            # 2) 满足条件才探测。
            if targets:
                sig = (engine_cfg, mode, epoch)
                if force_probe or self._bg_probe_sig != sig:
                    ok, reason = self._probe_background_capability()
                    self._bg_probe_sig = sig
                    self._bg_core_ok = bool(ok)
                    self._bg_probe_reason = "" if ok else str(reason)
                    if ok:
                        logger.info(
                            f"[{self.metadata_name}] 后台引擎=core（原生后台任务）："
                            f"mode={mode} 范围={sorted(targets)}"
                        )
                        if mode == "all":
                            logger.info(
                                f"[{self.metadata_name}] tools.background_mode=all 在 "
                                f"engine=core 下只对 {_BROWSE_WEB_TOOL_NAME} 生效；"
                                f"{_LOCAL_PAGE_TOOL_NAME} 与 {_SEARCH_TOOL_NAME} 按前台同步"
                                f"执行（永久排除，见 README「两个引擎下 browse_web 的差别」）。"
                                f"想让这两个工具也后台化：把 tools.background_engine 改回 plugin。"
                            )
                    else:
                        logger.error(
                            f"[{self.metadata_name}] {_BG_ENGINE_REFUSED}：{reason}。"
                            f"已降级为 plugin 引擎（插件自管后台，mode={mode}）；"
                            f"不会静默按前台执行。修复指引：{_CORE_FIX_HINT}"
                        )
            else:
                # mode=off 或 engine=plugin：机制无关，清掉探测态（D2 不告警）。
                self._bg_core_ok = True
                self._bg_probe_reason = ""
                self._bg_probe_sig = None

            # 3) 定档。
            effective = resolve_effective_engine(
                engine=engine_cfg, mode=mode, probe_ok=bool(self._bg_core_ok)
            )
            if effective == "core":
                self.background_engine_effective = "core"
                return "core"

            # 4) 降级：**先清 flag 再定档**（顺序是正确性，不是风格）。
            if targets:
                self._apply_background_flags(frozenset())
            self.background_engine_effective = "plugin"
            return "plugin"
        except Exception as exc:  # noqa: BLE001 — fail-closed
            logger.error(
                f"[{self.metadata_name}] 后台引擎同步异常（{type(exc).__name__}: {exc}）；"
                f"已 fail-closed 到 plugin 引擎并清空全部后台标志。"
            )
            try:
                self._apply_background_flags(frozenset())
            except Exception:  # noqa: BLE001
                pass
            self.background_engine_effective = "plugin"
            self._bg_core_ok = False
            return "plugin"

    # ----------------------------------------------------------------
    # C 模式执行体与媒体发布（媒体边界 A：只给路径，禁止 event.send）
    # ----------------------------------------------------------------

    def _publish_c_media(self, umo: str, path: str) -> tuple[str, bool]:
        """把媒体发布到核心可发送目录。返回 ``(对外路径, 是否已发布)``。"""
        src = Path(str(path))
        if not src.is_file():
            return str(path), False
        root = _c_media_public_root()
        if root is None:
            return str(path), False
        # 目录名只要 UMO 摘要：不把原始会话标识写进 data/temp 的路径里。
        target_dir = root / self._umo_digest(umo)[:8]
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            # 文件名带源路径 8 位摘要：两次截图同名（browse_xxx.png）不互相覆盖。
            digest = hashlib.md5(str(src).encode("utf-8")).hexdigest()[:8]
            dst = target_dir / f"{src.stem}_{digest}{src.suffix}"
            if dst.exists():
                return str(dst), True
            try:
                os.link(src, dst)  # 零拷贝（同一 data 文件系统）
            except OSError:
                shutil.copy2(src, dst)  # 跨文件系统/权限回退
            return str(dst), True
        except Exception as exc:  # noqa: BLE001 — fail-soft：不静默丢媒体
            logger.warning(
                f"[{self.metadata_name}] C 模式媒体发布失败（{type(exc).__name__}），"
                f"改用原路径"
            )
            return str(path), False

    async def _run_c_background_body(
        self, event: AstrMessageEvent, task_input: str, perception_mode: str
    ) -> str:
        """C 模式执行体：在核心的后台任务里跑真正的浏览，返回**给唤醒轮看的文本**。

        四件事（顺序即正确性）：①``artifact_sink`` 截流媒体（机械保证不
        ``event.send``）②整任务预算自截断 ③不可信前缀 + 截断正文 ④**台账与媒体
        块都在截断之后**拼接。**没有任何**"已受理"文案：核心已经先回过提交回执了。

        预算仍用 ``tools.background_timeout``（默认 240）：核心那层是 3600s
        硬编码，插件不自带预算就等于没有上限。超时不是异常抛出，而是一条
        ``【错误】`` 文本返回给唤醒轮（与失败同口径）。

        **为什么台账必须在截断之后拼接**（与媒体块同位置同理由）：台账是"内层
        模型到底动了哪些手"的唯一旁证，而它排在结论**后面**——一次 2000 字的
        结论 + ``background_max_chars=1500`` 就足以把整块台账吃掉，于是"收尾
        失真"这个缺陷在最需要它的生产配置下（``all × core`` + 800 字符）反而
        看不见了。截断只作用于结论，台账按自己的行数上限单独渲染。

        失败判定走 ``meta["plugin_failure"]``（**结构化**），不再嗅探文本前缀：
        内层模型把页面正文（页面以【错误】开头）原样带回时，前缀嗅探会把一次
        **成功**的浏览判成失败，还会让页面自己写"原因"。
        """
        umo = self._umo_of(event)
        budget = float(self.background_timeout)
        media: list[str] = []
        meta: dict = {}
        try:
            with artifact_sink() as bucket:
                try:
                    text = await asyncio.wait_for(
                        self._browse_web_run(
                            event, task_input, perception_mode, meta=meta
                        ),
                        timeout=budget,
                    )
                except asyncio.TimeoutError:
                    meta["plugin_failure"] = True
                    text = (
                        f"【错误】浏览超过 {budget:.0f} 秒整任务预算被中断"
                        f"（C 模式；{_BROWSE_WEB_TOOL_NAME} 没有中断前的部分结果）。"
                    )
                media = [str(p) for p in bucket]
        except Exception as exc:  # noqa: BLE001 — 后台任务不能把异常抛给核心
            logger.warning(f"[{self.metadata_name}] browse_web（C 模式）执行异常: {exc}")
            meta["plugin_failure"] = True
            text = f"【错误】浏览器子代理执行失败：{exc}"
        published = [self._publish_c_media(umo, p) for p in media if os.path.isfile(p)]
        paths = [dst for dst, _ok in published]
        unpublished = any(not ok for _dst, ok in published)
        body = truncate_result(str(text or ""), int(self.background_max_chars))[0]
        ok_text = not bool(meta.get("plugin_failure"))
        parts: list[str] = []
        if ok_text:
            # 前缀只给"可信性未知的正文"；插件自述的失败是我们自己的可信文本，
            # 且唤醒轮要靠【错误】/【拒绝】前缀分流。幂等：结论若已带前缀（源侧
            # 加过）就不再加第二遍。
            prefixed = agent_core.with_untrusted_prefix(body)
            if prefixed != body:
                parts.append(C_UNTRUSTED_PREFIX)
                body = prefixed[len(C_UNTRUSTED_PREFIX) :].lstrip("\n")
        parts.append(body)
        ledger_block = agent_core.render_ledger(
            meta.get("ledger"),
            min(int(self.agent_ledger_max_lines), _C_MODE_LEDGER_MAX_LINES),
        )
        if ledger_block:
            # 台账在**截断之后**拼接（理由见 docstring）：它有独立行数上限，
            # 不受 ``background_max_chars`` 裁切。
            #
            # R13（round 2）：**失败态也拼**。``_browse_web_run`` 的超时文案写着
            # "（已完成 N 步，见台账）" —— 旧实现只给成功态台账，于是"见台账"
            # 指向一个不存在的块（断链）。失败时那几步恰恰是唯一线索。
            parts.append(ledger_block)
        if paths and ok_text:
            # 媒体只附成功态（与 v1.5.0 同口径）；媒体块同样在截断之后拼接，
            # 否则绝对路径会被砍断（"媒体只给路径"方案的致命细节）。
            parts.append(render_c_media_block(paths, unpublished=unpublished))
        return "\n".join(parts)

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

    async def _run_background_job(
        self,
        job: BackgroundJob,
        runner: Callable[[dict], Awaitable[str]],
    ) -> None:
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
            # 预算超时的原因已由终态文案本身说明（「已用满 N 秒硬预算」），
            # 这里不再重复；reason 留给"工具自己报的超时"（如 total_timeout）。
            reason = ""
            partial = self._background_partial(job.tool, progress)
        except asyncio.CancelledError:
            status = STATUS_CANCELLED
            reason = "插件重载/禁用"
            partial = self._background_partial(job.tool, progress)
        except Exception as exc:  # noqa: BLE001 — 后台任务不能把异常抛给事件循环
            status = STATUS_FAILED
            reason = f"{type(exc).__name__}: {exc}"
            partial = self._background_partial(job.tool, progress)
        if status == STATUS_OK:
            # 返回体 → 终态（H1+H2）：搜索工具的返回体是 JSON，必须**先**翻译成
            # 人读摘要并取真实状态，再交给投递层（截断只作用于渲染后的文本，
            # 绝不会把「…已省略 N 字」拼进 JSON 中间）。
            mapped_status, mapped_text, mapped_reason = self._background_user_text(
                job.tool, text
            )
            if mapped_status is not None:
                status, text, reason = mapped_status, mapped_text, mapped_reason
            elif str(text or "").startswith(("【错误】", "【拒绝】")):
                # 纯文本工具把"可预期失败"以【错误】/【拒绝】前缀当文本返回
                # （例如取不到当前对话模型、页面内容命中禁词）：后台模式下必须
                # 判成失败，否则用户看到的是「完成」里包着一条错误。
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
                f"tool={job.tool} umo={self._umo_digest(job.umo)} "
                f"elapsed_ms={elapsed_ms} chars={len(text)} "
                f"media={len(artifacts)} delivered={bool(delivered)}"
            )
            if note:
                # 终态消息首行另打一条 info，便于按 `【后台任务…】` 对账。
                logger.info(f"{tag} {note}")

    def _background_user_text(self, tool_name: str, raw: str) -> tuple[str | None, str, str]:
        """把工具返回体翻译成「发给人的终态」：``(status, text, reason)``。

        为什么需要它（设计 §2.4）：工具返回体是给**模型**读的（``browser_web_search``
        甚至是 JSON），而终态消息是发给**人**的。直接把 JSON 丢进消息里既读不懂、
        又会在截断时被从中间砍断；更糟的是"JSON 里 ``status:"failed"``"逃过了
        ``【错误】`` 前缀判定，会让一次"搜索全挂"在日志里看起来 100% 成功
        （``grep -c "后台任务终态 status=ok"`` 失真）。

        ``status`` 返回 ``None`` 表示"沿用调用方判定"（纯文本工具就是这种）。

        搜索返回体的状态映射（队长裁决，逐条实现）：
        - ``"ok"`` → 成功；
        - ``"partial"`` → 成功，且正文标注「部分结果」；
        - ``"failed"`` → 失败（原因 = ``code: summary`` + hint）；
        - ``"timeout"`` / ``"total_timeout"``（含 ``code == "total_timeout"``）→ 超时；
        - 无法解析 / 缺 ``status`` → 失败，原因「返回体无法解析」；
        - 其它未知 status（含 ``disabled``）→ 失败（绝不把未知态算成成功）。
        """
        body = str(raw or "")
        if body.startswith(C_UNTRUSTED_PREFIX):
            # v1.7.0 repair（评审 V4）：带「不可信外部内容」前缀 ⇔ 这段正文来自
            # **内层模型/页面**（源侧是结构化加的，页面伪造不出来）。因此：
            # ① 判定成功态，**不许**因为页面正文以【错误】开头就把一次成功浏览
            #    显示成「【后台任务失败】」并附上页面自己写的原因；
            # ② 前缀是给模型看的，人读的终态消息里去掉（规格 §1.2.3：plugin
            #    引擎的后台终态消息不加前缀）；
            # ③ 去掉前缀后若正文本身以【错误】/【拒绝】/【不可用】开头，加一行
            #    说明把它钉成"原文"，否则人读起来仍像插件在报错（旧 test_t4 的
            #    关切仍然成立，只是判定依据从"文本前缀"换成了"结构化来源"）。
            stripped = body[len(C_UNTRUSTED_PREFIX) :].lstrip("\n")
            # R12（round 2 的第二个后果）：payload 现在可能是
            # `【注意】…\n\n【错误】…`（通知行在模型结论之前），所以不能只看
            # 整段开头 —— 只要**任何一行**以"像插件的错误前缀"开头就加消歧说明。
            if any(
                line.startswith(_TRUSTED_LOOKING_PREFIXES)
                for line in stripped.splitlines()
                if line.strip()
            ):
                stripped = f"{_ECHO_NOTE}\n{stripped}"
            return STATUS_OK, stripped, ""
        if tool_name != _SEARCH_TOOL_NAME:
            return None, body, ""
        payload = self._parse_search_payload(raw)
        if payload is None:
            return STATUS_FAILED, "", "返回体无法解析"
        status = str(payload.get("status") or "").strip().lower()
        code = str(payload.get("code") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        hint = str(payload.get("hint") or "").strip()
        detail = f"{code}: {summary}".strip(": ").strip()
        if hint:
            detail = f"{detail}（{hint}）" if detail else hint
        if not status:
            return STATUS_FAILED, "", "返回体无法解析：缺少 status 字段"
        if status in (STATUS_TIMEOUT, "total_timeout") or code == "total_timeout":
            return STATUS_TIMEOUT, "", detail or "搜索超过总预算被中断"
        if status == "failed":
            return STATUS_FAILED, "", detail or "搜索失败（返回体未给出原因）"
        if status in ("ok", "partial"):
            text = self._render_search_human(payload)
            if status == "partial":
                text = "（部分结果：有 provider 失败或结果被预算截断）\n" + text
            return STATUS_OK, text, ""
        return STATUS_FAILED, "", f"未预期的返回体 status={status!r}"

    @staticmethod
    def _parse_search_payload(raw: str) -> dict | None:
        """解析搜索工具返回体；不是 JSON 对象时返回 ``None``。

        ``status`` 字段是否缺失由调用方判（缺失要给出「缺少 status」这种更
        具体的失败原因，不能与"整段不是 JSON"混成一句）。
        """
        try:
            payload = json.loads(str(raw or ""))
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _render_search_human(payload: dict, limit: int = 6) -> str:
        """把搜索返回体渲染成人读摘要（结果条目 / notes / 后端摘要 / 提示）。

        Args:
            payload: ``render_payload`` 产出的字典。
            limit: 最多列出多少条结果（条数由调用方的展示宽度决定）。
        """
        lines: list[str] = []
        query = str(payload.get("query") or "").strip()
        if query:
            lines.append(f"检索词：{query}")
        results = payload.get("results") or []
        lines.append(f"命中 {len(results)} 条结果")
        for index, item in enumerate(list(results)[: max(1, int(limit))], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip() or "（无标题）"
            url = str(item.get("url") or "").strip()
            snippet = " ".join(str(item.get("snippet") or "").split())
            lines.append(f"{index}. {title}")
            if url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet[:120]}")
        if len(results) > int(limit):
            lines.append(f"…（其余 {len(results) - int(limit)} 条已省略）")
        notes = [str(note) for note in (payload.get("notes") or []) if str(note)]
        if notes:
            lines.append("备注：" + "、".join(notes))
        attempts = [a for a in (payload.get("attempts") or []) if isinstance(a, dict)]
        if attempts:
            brief = [
                "/".join(
                    str(part)
                    for part in (
                        attempt.get("provider"),
                        attempt.get("outcome"),
                        attempt.get("code"),
                    )
                    if part
                )
                for attempt in attempts[:6]
            ]
            lines.append("后端：" + "、".join(brief))
            if len(attempts) > 6:
                lines.append(f"…（另有 {len(attempts) - 6} 个后端调用未列出）")
        error_codes = [str(code) for code in (payload.get("error_codes") or []) if code]
        if error_codes:
            lines.append("错误码：" + "、".join(error_codes))
        hint = str(payload.get("hint") or "").strip()
        if hint:
            lines.append(f"提示：{hint}")
        return "\n".join(lines)

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
        body = truncate_result(text, int(job.max_chars))[0]
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

        引擎（v1.6.0，配置 tools.background_engine）：plugin（默认，同上）/
        core（平台原生后台任务：由核心派发，完成后核心唤醒一轮主 LLM 交付；
        返回值会被塞进那一轮的系统提示词，因此带「不可信外部内容」前缀并按
        tools.background_max_chars 截断；媒体只给绝对路径，由唤醒轮调用
        send_message_to_user 转发）。core 只在核心已修复时可用，否则插件自动
        拒绝并降级回 plugin（不会静默前台执行）。
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
            # 搜索双模式收敛（v1.7.0 repair，评审 R4）：search.mode / 池可用性变了
            # 就重建内层工具集并重新对齐外层可见性（幂等；签名不变时是两次 dict
            # 比较）。**放在会话鉴权之后**：对齐会写全局持久化名单
            # （``inactivated_llm_tools``），不该让一次被拒绝的调用产生 DB 写。
            await self._sync_search_mode()
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
            # v1.6.0：C 路径（平台原生后台）优先；两门互斥——effective=core 时
            # ``_background_enabled_for`` 恒 False，不会出现"一条任务两条消息"。
            # 窗口期（规格 §4.5）：核心在调 handler **之前**读 flag，所以刚把
            # engine 改成 core 的那一次调用仍可能按前台派发，此时本执行体在前台
            # 跑完并返回"前缀 + 截断正文"（不发媒体、不发【已受理】）——这是
            # 配置热更新的固有延迟，不是静默降级；下一次调用起由核心后台派发。
            if self._background_c_mode_for(_BROWSE_WEB_TOOL_NAME):
                return await self._run_c_background_body(
                    event, task_input, perception_mode
                )
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
            # 前台路径（唯一给主 LLM 直接返回的地方）：结论 + 台账要套上
            # 「不可信外部内容」前缀（规格 §1.2.3）。前缀由执行体在**源侧**加
            # （见 ``_browse_web_run`` 的 ``meta`` 契约）：这样"这是模型说的话"
            # 是结构化事实，不必靠嗅探文本前缀 —— 页面回声以【错误】开头时也
            # 不会被误判成插件自己的失败（评审 R6/V4）。前缀由执行体在源侧加，
            # 这里**不再** wrap 一次（R15：那层 wrap 曾是死代码，注释还谎称"幂等"）。
            meta: dict = {}
            body = await self._browse_web_run(
                event, task_input, perception_mode, meta=meta
            )
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
        perception_mode: str,
        meta: dict | None = None,
    ) -> str:
        """browse_web 的执行体（provider 解析 + 内层代理 + 台账）；前台与后台共用。

        ``meta`` 是**结构化回传通道**（v1.7.0 repair）：调用方传一个 dict，执行体
        填两项事实，避免"靠文本前缀猜语义"：

        - ``meta["plugin_failure"]``：本次失败是**插件自己**判定的（超时 / 内层
          异常 / provider 不可用），还是模型的结论。页面正文以【错误】开头时
          文本前缀嗅探会把一次成功判成失败，还会让页面自己写"原因"。
        - ``meta["ledger"]``：本次的 ``Ledger`` 对象（调用方按自己的行数上限
          渲染，并在**截断之后**拼接 —— C 模式的台账就是这么保住的）。
          ``meta is None`` 时本方法自己把台账拼进返回体（直接调用者的旧行为）。

        前缀：模型结论在**源侧**就带「不可信外部内容」（``with_untrusted_prefix``
        幂等，调用方再 wrap 一次不会翻倍）；插件自述失败**不带**前缀。这样
        ``startswith("【错误】")`` 在下游只剩"插件自己说的"这一种含义。

        为什么 provider 解析放在这里（而不是入口）：后台模式下拿不到模型应
        表现为一条**异步失败终态消息**，而不是「被告知不可用却什么都没发生」。

        四件结构与顺序相关的事：

        1. ``agent_core.run_scope``：把本次任务的「是否影像原生 / 结果上限 /
           图片参数 / 台账」放进 ContextVar（内层截图工具据此决定走原生还是
           caption 兜底；并发会话之间不会串号）；
        2. ``asyncio.wait_for`` 包住**整个** ``tool_loop_agent``：
           ``agent.task_timeout`` 到点就取消，取消后只拼字符串、不再 await
           任何可能阻塞的东西（规格 §8.4 的"不悬挂"硬要求）；
        3. 超时/异常都归成 ``【错误】`` 文本（``TargetClosedError`` 这类取消
           传播出来的异常必须被吞在这里，不许冒泡给主 LLM），并置
           ``meta["plugin_failure"] = True``；
        4. 台账由插件按**实际工具调用**生成 —— 模型改不了它。
        """
        umo = self._umo_of(event)
        # 并发观测（规格 §8.5）：同会话前台并发**只告警不拦截**（前台入口持锁
        # 会与内层工具锁互死，见 browse_web docstring）。计数器按需建：手工
        # 装配的实例（单测 __new__）没有 __init__ 跑过的字段。
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
        try:
            if getattr(self, "_browser_toolset", None) is None:
                # 兜底：手工装配的实例（单测 / 极端加载顺序）没跑过 initialize()。
                # 生产路径上 initialize() 一定会建好，这里只是让它不会拿 None 去跑。
                self._rebuild_browser_toolset()
            decision = await self._resolve_agent_provider(event)
            if not decision.ok:
                # R11（round 2 回归）：配置错误（内层 provider 未加载/被删/缺
                # tool_use）也是**插件自述的失败**，必须经结构化通道告知调用方。
                # 旧实现直接 return，C 模式拿不到 plugin_failure ⇒ 把这段
                # 【错误】当成模型结论套上「不可信外部内容」前缀，唤醒轮看到的是
                # "外部网页说的错误"，而真相是"你自己配置错了"。
                if meta is not None:
                    meta["plugin_failure"] = True
                    meta["ledger"] = None
                return decision.error
            base_instruction = self._build_subagent_instruction(perception_mode)
            instruction = await self._compose_inner_prompt(
                perception_mode, base=base_instruction, event=event
            )
            budget = float(self.agent_task_timeout)
            run = agent_core.AgentRun(
                provider_id=decision.provider_id,
                native_image=decision.native_image,
                result_max_chars=int(self.agent_result_max_chars),
                image_max_side=int(self.agent_image_max_side),
                image_quality=int(self.agent_image_quality),
                char_budget=int(self.agent_max_steps) * int(self.agent_result_max_chars),
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
                            max_steps=self.agent_max_steps,
                            tool_call_timeout=self.agent_tool_timeout,
                        ),
                        timeout=budget,
                    )
                    text = getattr(resp, "completion_text", "") or ""
                    text = text or "（浏览器子代理未返回内容）"
                except asyncio.TimeoutError:
                    plugin_failure = True
                    # R13：文案里承诺"见台账"就必须真的有台账 —— 0 步时台账块
                    # 是空串（``render_ledger`` 对无条目的 Ledger 返回 ""），
                    # 再写"见台账"就是断链。
                    steps = scoped.ledger.steps
                    hint = (
                        f"（已完成 {steps} 步，见台账）"
                        if steps
                        else "（尚未产生任何内层工具调用）"
                    )
                    text = (
                        f"【错误】浏览任务超过 {budget:.0f} 秒硬上限被中止{hint}"
                    )
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
                images = scoped.ledger.images
                ledger_obj = scoped.ledger
            if meta is not None:
                meta["plugin_failure"] = plugin_failure
                meta["ledger"] = ledger_obj
            # R12（round 2 回归）：**先拼 payload，再整体套一次前缀**。
            # 旧实现是"先给结论套前缀、再把【注意】插到它前面"，两个后果：
            # ① 前台成功返回体的**首行**变成【注意】，把「不可信外部内容」挤出
            #    第一行（规格 §1.2.3 要求前缀首行，唤醒轮/主 LLM 靠它分流）；
            # ② plugin 引擎后台的人读终态以【注意】开头 ⇒
            #    ``_background_user_text`` 的 ``startswith(C_UNTRUSTED_PREFIX)``
            #    不再命中 ⇒ 模型侧前缀残留、回声消歧失效。
            # 一处改动同时修好两条：前缀永远是整段 payload 的第一个字节。
            payload = "\n\n".join(
                part for part in (decision.notice, text) if part and not plugin_failure
            ) or text
            if not plugin_failure:
                payload = agent_core.with_untrusted_prefix(payload)
            parts: list[str] = [payload]
            if meta is None:
                # 直接调用者（含 plugin 引擎后台 runner）：照旧自带台账。
                ledger_block = agent_core.render_ledger(
                    ledger_obj, int(self.agent_ledger_max_lines)
                )
                if ledger_block:
                    parts.append(ledger_block)
            logger.debug(
                f"[{self.metadata_name}] browse_web tool_loop_agent 返回，"
                f"len={len(text)} images={images}"
            )
            return "\n\n".join(part for part in parts if part)
        finally:
            inflight = getattr(self, "_fg_inflight", None)
            if isinstance(inflight, dict):
                left = inflight.get(umo, 1) - 1
                if left > 0:
                    inflight[umo] = left
                else:
                    inflight.pop(umo, None)

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
            # 搜索双模式收敛（v1.7.0 repair，评审 R4）：本工具也是外层入口，
            # 从它进来的调用同样要能把 search.mode 的切换落到实处。鉴权之后才做
            # （理由同 browse_web：对齐会写全局持久化名单）。
            await self._sync_search_mode()

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
            # 结果预算走**共享执行体**（``core.agent.truncate_text``）：三个
            # 外层入口对"一次工具结果能有多长"的口径必须一致（``agent.
            # result_max_chars``），否则模型会看到三套长度契约。
            #
            # 为什么本工具**不**进浏览器子代理（规格 §1.4 三条理由）：① 四道
            # 路径校验必须**同步**拒绝（越权路径不能退化成异步失败）② 6 条生产
            # 人格显式列了它（合并=静默剥夺）③ 它的消费者是主 LLM（"没有视觉
            # 模型时看渲染结果"），语义与"网页交互"不同类。
            return agent_core.truncate_text(
                await self._local_page_render(
                    event, target, full_page, wait_ms, perception_mode, {}
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
                if screenshot_ok:
                    # 后台模式下截图必须**进附件**（任务结束时随终态消息一起发），
                    # 不能依赖前台 event.send —— event 在任务跑完时早已终结。
                    # 前台（无 sink）时 collect_artifact 返回 False 且什么都不做，
                    # 因此 v1.4.0 的"只把路径写进返回文本"行为一字不变。
                    collect_artifact(save_path)

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

    async def _page_compact(self, page, max_chars: int = 0) -> str:
        """紧凑页面面貌：``URL / 标题 / 正文前 N 字``（v1.7.0，N=``agent.result_max_chars``）。

        为什么非"读正文"类原语（点击/滚动/切标签/刷新）要改成紧凑返回：
        它们的职责是"动作是否生效"，附上整页正文既吃 token 又诱导模型把
        同一次点击的正文重复读一遍；正文请用 ``browse_get_text`` 显式取。
        禁词过滤与 ``_page_summary`` 同口径（同一个出口，不新开一条路径）。
        """
        info = await self.extractor.extract_page_info(page)
        # getattr 兜底：手工装配的插件实例（单测 __new__）没有该字段。
        default_limit = int(getattr(self, "agent_result_max_chars", 0) or self.max_chars)
        limit = int(max_chars or default_limit)
        text = await self.extractor.extract_text(page, max_chars=limit)
        text = text or "（页面无文本内容）"
        for field, value in (("标题", info.get("title") or ""), ("正文", text)):
            banned = self._check_banned(value)
            if banned:
                return f"【拒绝】页面{field}包含违禁内容：{banned}"
        return f"URL: {info['url']}\n标题: {info['title']}\n\n正文前 {len(text)} 字: {text}"

    async def _page_url_title(self, page) -> str:
        """只回 ``URL + 标题``（v1.7.0，供"跳转类"原语用，规格 §1.3 #6）。"""
        info = await self.extractor.extract_page_info(page)
        banned = self._check_banned(info.get("title") or "")
        if banned:
            return f"【拒绝】页面标题包含违禁内容：{banned}"
        return f"URL: {info['url']}\n标题: {info['title']}"

    async def _scroll_report(self, page, delta: int) -> str:
        """滚动结果报告：``已滚动 <px> px，进度 <%>，页面高度 <h>px``（规格 §1.3 #7）。

        为什么不回正文：滚动这一步的"是否生效"只需要进度与页高；正文要求
        ``browse_get_text`` 显式取，否则同一页正文会被反复塞进上下文。
        """
        info = await self.extractor.extract_page_info(page)
        banned = self._check_banned(info.get("title") or "")
        if banned:
            return f"【拒绝】页面标题包含违禁内容：{banned}"
        metrics: dict = {}
        evaluate = getattr(page, "evaluate", None)
        if evaluate is not None:
            try:
                metrics = (
                    await evaluate(
                        "() => ({y: window.scrollY, h: document.body ? "
                        "document.body.scrollHeight : 0, "
                        "vh: window.innerHeight || 0})"
                    )
                    or {}
                )
            except Exception as e:  # noqa: BLE001 — 量不到就不报进度
                logger.debug(f"读取滚动位置失败: {e}")
                metrics = {}
        action = "向下" if delta >= 0 else "向上"
        height = int(metrics.get("h") or 0)
        y = int(metrics.get("y") or 0)
        vh = int(metrics.get("vh") or 0)
        if height > 0:
            reachable = max(1, height - vh) if vh else height
            percent = int(min(100, max(0, round(y * 100 / reachable))))
            return (
                f"已{action}滚动 {abs(int(delta))} px，进度 {percent}%，"
                f"页面高度 {height}px（当前 scrollY={y}）。"
                "需要正文请用 browse_get_text。"
            )
        return (
            f"已{action}滚动 {abs(int(delta))} px（页面高度读取不可用）。"
            "需要正文请用 browse_get_text。"
        )

    def _lock_for(self, event: AstrMessageEvent) -> asyncio.Lock:
        """返回该会话的专用锁（串行化同一会话的并发工具调用）。"""
        return self.sessions.get_lock(self._umo_of(event))

    @staticmethod
    async def _locator_count(loc) -> int | None:
        """定位器匹配数（v1.6.1）。

        Playwright 的 ``locator.count()`` 是协程；测试桩/极旧实现可能没有该
        方法，此时返回 None 表示「候选数未知」——调用方按单一匹配处理，
        保持 v1.6.0 及以前的宽松行为（不因为数不出候选而报错）。
        """
        counter = getattr(loc, "count", None)
        if counter is None:
            return None
        try:
            return int(await counter())
        except Exception as e:  # noqa: BLE001 — 计数失败按「未知」处理
            logger.debug(f"定位器 count() 失败，按未知处理: {e}")
            return None

    @staticmethod
    async def _checkbox_state(loc) -> bool | None:
        """读取元素的「选中状态」（v1.6.1）。

        - 真 checkbox/radio：走 ``is_checked()``（Playwright async API，必须
          await）；
        - 按钮式选项（``<button aria-pressed>`` / ``role=radio`` 等）：
          ``is_checked()`` 会抛「Not a checkbox or radio button」，此时依次
          读 aria-checked / aria-pressed / aria-selected；
        - 两者都不可用 → None（调用方据此走「按钮式选项」兜底分支）。
        """
        try:
            return bool(await loc.is_checked())
        except Exception as e:  # noqa: BLE001 — 非 checkbox/radio：改读 ARIA
            logger.debug(f"is_checked 不可用，改读按钮式选项的 ARIA 状态: {e}")
        getter = getattr(loc, "get_attribute", None)
        if getter is None:
            return None
        for attr in ("aria-checked", "aria-pressed", "aria-selected"):
            try:
                value = await getter(attr)
            except Exception:  # noqa: BLE001 — 单个属性读取失败不影响其它
                continue
            if value is None:
                continue
            text = str(value).strip().lower()
            if text in ("true", "false"):
                return text == "true"
        return None

    async def _click_text_candidates(self, page, text: str) -> str:
        """采集页面**真实**可点击文本，按与目标文本的相似度返回 top-N（v1.6.1）。

        仅在 browse_click_text 失败时调用：候选来自页面实际渲染文本（不做
        任何改写），排序规则 = 目标文本是候选子串（最高）> 候选是目标子串
        > difflib 相似度，同级按文本字典序。每条带元素类型与可见性，模型
        据此重试；一条都没有时返回空串（调用方只回原错误）。

        采集失败（无 evaluate / evaluate 抛错 / 结果非预期结构）一律返回
        空串——候选是**附加信息**，绝不能因为它把原错误语义盖掉。
        """
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return ""
        try:
            raw = await evaluate(_CLICK_TEXT_CANDIDATES_JS)
        except Exception as e:  # noqa: BLE001 — 采集失败不影响原错误返回
            logger.debug(f"采集可点击文本候选失败: {e}")
            return ""
        items = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            cand = str(item.get("text") or "").strip()
            if not cand or len(cand) > _CLICK_TEXT_CANDIDATE_MAX_LEN:
                continue
            items.append(
                (
                    cand,
                    str(item.get("type") or "?"),
                    bool(item.get("visible")),
                )
            )
            if len(items) >= _CLICK_TEXT_CANDIDATE_SCAN_LIMIT:
                break
        if not items:
            return ""
        needle = str(text).strip().lower()
        scored = []
        for cand, etype, visible in items:
            low = cand.lower()
            if needle and needle in low:
                score = 2.0
            elif low and low in needle:
                score = 1.5
            else:
                score = difflib.SequenceMatcher(None, needle, low).ratio()
            scored.append((score, cand, etype, visible))
        scored.sort(key=lambda row: (-row[0], row[1]))
        top = scored[:_CLICK_TEXT_CANDIDATE_TOP_N]
        lines = [
            f"相近的可点击文本（top {len(top)}，页面共采集 {len(items)} 个候选）："
        ]
        for i, (_score, cand, etype, visible) in enumerate(top, 1):
            state = "可见" if visible else "不可见"
            lines.append(f"{i}. 「{cand}」（{etype}，{state}）")
        lines.append("请只用上面列出的真实文本重试，禁止臆造选项标签。")
        return "\n".join(lines)

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
                # v1.7.0：紧凑返回（URL/标题/正文前 N 字），正文请用
                # browse_get_text 显式取（规格 §1.3 #3）。
                return await self._page_compact(page)
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

    def _filter_banned_link_lines(self, links: str, note: bool = True) -> str:
        """按 ``security.banned_words`` **逐行**过滤链接列表（R16）。

        与 ``browse_get_interactive`` 完全同口径：命中的那一行被丢弃并计数
        （``note=True`` 时附一行说明），其余行原样保留、**编号不变**——编号是
        ``browse_click_link(index=…)`` 的唯一落点，重排会让模型点错链接。

        Args:
            links: ``extract_links`` 产出的 ``[n] 文本 → url`` 多行文本。
            note: 是否在输出末尾追加"另有 N 个链接命中禁词"的说明行。
                ``browse_click_link`` 解析时传 ``False``（说明行不是链接）。
        """
        body = str(links or "")
        if not body:
            return body
        kept: list[str] = []
        dropped = 0
        for line in body.splitlines():
            # 说明行 / 超限提示行原样保留。
            if not line.startswith("["):
                kept.append(line)
                continue
            if self._check_banned(line):
                dropped += 1
                continue
            kept.append(line)
        if dropped and note:
            kept.append(
                f"（另有 {dropped} 个链接的文本命中禁词，已按 security.banned_words 略去）"
            )
        return "\n".join(kept)

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
                # R16（round 2）：与 browse_get_interactive 同一口径 —— 逐行过禁词、
                # **丢掉命中的那一条并计数**，不整段拒绝（整段拒绝会让页面上出现一个
                # 违禁词就把"看链接→点链接"这条动线废掉）。
                return self._filter_banned_link_lines(links)
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
                # R16：与 browse_get_links 用**同一个**过滤器，否则被丢掉的条目会
                # 让 index 与模型看到的编号错位（模型点 [3]，这里按未过滤列表取第 3 条）。
                links = self._filter_banned_link_lines(links, note=False)
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
                # v1.7.0：只回 URL + 标题（不附正文）——点击的职责是"跳没跳"，
                # 正文由 browse_get_text 显式取（规格 §1.3 #6）。
                return await self._page_url_title(page)
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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                # v1.7.0：只回滚动结果（像素/进度/页高），**不返回正文**——
                # 滚动是"看没看到更多"，正文用 browse_get_text（规格 §1.3 #7）。
                return await self._scroll_report(page, y)
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
                # 导航类原语：失效用**动作前**的 URL（动作后 URL 已变）。
                pre_url = getattr(page, "url", "")
                try:
                    await page.go_back()
                except Exception as e:  # noqa: BLE001
                    return f"【错误】后退失败：{e}"
                self._vision_cache_invalidate(event, pre_url)
                return await self._page_compact(page)
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
                # 导航类原语：失效用**动作前**的 URL（动作后 URL 已变）。
                pre_url = getattr(page, "url", "")
                try:
                    await page.go_forward()
                except Exception as e:  # noqa: BLE001
                    return f"【错误】前进失败：{e}"
                self._vision_cache_invalidate(event, pre_url)
                return await self._page_compact(page)
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
                summary = await self._page_compact(new_page)
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
                # 切标签前记下「离开的那一页」的 URL：只失效它的缓存，
                # 其它标签页的条目一条都不动（不跨标签误删）。
                pre_url = getattr(page, "url", "")
                target = await self.sessions.switch_tab(umo, index)
                if target is None:
                    return f"【错误】标签页编号 {index} 不存在（当前共 {total} 个，编号从 1 开始）。"
                self._vision_cache_invalidate(event, pre_url)
                summary = await self._page_compact(target)
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
                # 关标签前记下当前页 URL：只失效这一页的缓存（不跨标签误删）。
                pre_url = getattr(page, "url", "")
                closed = await self.sessions.close_tab(umo, index)
                if not closed:
                    return f"【错误】标签页编号 {index} 不存在（当前共 {total} 个，编号从 1 开始）。"
                self._vision_cache_invalidate(event, pre_url)
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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
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

    async def browse_screenshot(self, event: AstrMessageEvent):
        """截取当前页面截图；**影像原生**交给内层模型（无参数）。

        三个分支（规格 §2.3 的三行表格）：

        1. 内层模型多模态 ⇒ 返回 ``mcp.types.CallToolResult``（TextContent +
           ImageContent），图片由核心缓存后在**下一轮**以 user 消息塞进内层
           上下文（``tool_loop_agent_runner.py:1310-1325`` / ``:1107-1141``）。
           此时**完全不调用** ``llm_generate``，``vision_provider_id`` 不参与。
        2. 内层模型是纯文本 ⇒ 走 caption 兜底：返回 ``str``
           ``"识图结果：\\n<caption>"``（等于 v1.6.0 的行为，用 T2 修过的
           内容哈希缓存）。
        3. 纯文本 + 未配/无效的识图兜底 ⇒ 返回明确 ``【不可用】`` 文案，
           **不许**静默返回空串让模型瞎猜。

        ``silent_mode=false`` 时仍向会话发图（``_emit_media`` 不变）。
        子 Agent 工具上下文（FunctionTool.call）不能 yield 消息，因此发图
        改用 event.send(image_result(path))。
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
                run = agent_core.current_run()
                if run is not None and run.native_image:
                    # ---- 分支 1：影像原生 ----
                    payload = await self._image_native_result(
                        result,
                        run=run,
                        size_label=self._viewport_label(page),
                        sent_to_user=not self.silent_mode,
                        crop=None,
                    )
                    if payload is not None:
                        return payload
                    # mcp 不可用 / 图片超硬上限 / 张数用尽 ⇒ 走 caption 兜底
                    # （可见降级：文本里说明了原因，不是静默丢图）。
                # ---- 分支 2/3：caption 兜底（T2 的内容哈希缓存路径）----
                # kind="screenshot" 进缓存键：全图与 browse_zoom_crop 的裁剪
                # 结果不再互相命中（v1.6.1）。
                desc = await self._describe_screenshot(
                    result,
                    event,
                    page_url=getattr(page, "url", None),
                    kind="screenshot",
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
                if run is not None and run.native_image:
                    # 影像原生失败且 caption 也没有（未配置识图兜底）：
                    # 明确告知不可用，别让模型猜页面长什么样。
                    return _IMAGE_UNAVAILABLE_TEXT
                prefix = (
                    "截图已识图（静默模式，未发送）"
                    if self.silent_mode
                    else "截图已发送给用户"
                )
                return f"{prefix}。如需识图请配置 vision_provider_id 多模态模型。"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_screenshot 失败: {e}")
            return f"【错误】截图失败：{e}"

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

    async def _image_native_result(
        self,
        path: str,
        *,
        run: agent_core.AgentRun,
        size_label: str,
        sent_to_user: bool,
        crop: tuple[int, int, int, int] | None,
    ):
        """把截图/裁剪包成 ``CallToolResult``（影像原生的**唯一**出站处）。

        三条产出规则（规格 §2.2 / §8.1）：

        - 每工具每次最多 **1** 张 ``ImageContent``，全任务最多
          ``MAX_IMAGES_PER_TASK`` 张；超额 ⇒ 返回纯文本（说明原因），不丢静默；
        - base64 超过 ``LLM_IMAGE_MAX_B64`` 由 ``agent_core`` 再压/丢弃；
        - 安全：``path`` 必须是**本插件截图目录**下的文件（不允许任意路径
          变成模型可读图片 —— 否则就是任意文件读取原语，规格 §9.3-P6）。

        Returns:
            ``CallToolResult`` / 纯文本 ``str``；``None`` 表示"图片没做成，
            请调用方继续走 caption 兜底"。
        """
        if not self._is_own_screenshot(path):
            logger.error(
                f"[{METADATA_NAME}] 影像原生拒绝非截图目录路径"
                f"（{Path(str(path)).name}）"
            )
            return None
        if run.ledger.images >= agent_core.MAX_IMAGES_PER_TASK:
            return (
                f"【预算】本次任务图片数已达上限（{agent_core.MAX_IMAGES_PER_TASK} 张），"
                "本张不再附图片；请改用 browse_get_text / browse_get_interactive 获取信息。"
            )
        image = await agent_core.async_image_content(
            path,
            max_side=run.image_max_side,
            quality=run.image_quality,
        )
        if image is None:
            # 太大/读不到：图片没做成，交给调用方走 caption 兜底。
            return None
        if crop is None:
            first = (
                "截图已发送给用户，同时附在本条结果之后。"
                if sent_to_user
                else f"截图已获取（{size_label}，JPEG q={run.image_quality}）。"
                "图片已附在本条工具结果之后，可直接查看。"
            )
            text = (
                f"{first}\n如需读小字/图标：先据此图定位，再用 "
                "browse_zoom_crop(x,y,w,h) 放大该区域。"
            )
        else:
            x, y, width, height = crop
            text = f"裁剪区域 (x,y,w,h)=({x},{y},{width},{height}) 已放大附在本条结果之后。"
        result = agent_core.call_result_with_image(text, image)
        if result is None:
            return None
        # 张数由台账在 ``record_and_truncate`` 里统一计一次（``Ledger.add`` 看到
        # ImageContent 就 +1）。这里**不能**再加一次：那会让"40 张上限"实际变成
        # 20 张（每张被计两遍）。
        return result

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


    async def browse_click_text(
        self, event: AstrMessageEvent, text: str, index: int = 0
    ) -> str:
        """网页交互场景专用：按文本内容精准点击页面元素（按钮/选项等非链接 JS 元素）。

        Args:
            text(string): 要点击的元素包含的文本内容，如"确认并继续"。
            index(number): 多个元素包含同一文本时的序号，从 0 开始，默认 0（第一个）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                text = str(text).strip()
                if not text:
                    return "【错误】text 不能为空。"
                try:
                    idx = int(index or 0)
                except (TypeError, ValueError):
                    return (
                        f"【错误】index 参数无效：{index!r}，"
                        "应为从 0 开始的整数（0=第一个匹配）。"
                    )
                if idx < 0:
                    return f"【错误】index 不能为负数（收到 {idx}），编号从 0 开始。"
                clicked = False
                # 优先 Playwright 的 get_by_text（语义化文本定位）。
                # v1.7.0 扩参 index：多匹配时用 .nth(idx) 取第 idx 个 ——
                # v1.6.1 的 .first 只能点第一个，页面里同名按钮/选项一多就
                # 点错目标（规格 §1.3 #16）。
                if hasattr(page, "get_by_text"):
                    try:
                        target = page.get_by_text(text, exact=False)
                        target = target.first if idx == 0 else target.nth(idx)
                        await target.click(timeout=10000)
                        clicked = True
                    except Exception as e:  # noqa: BLE001 — 未找到或不可点，降级
                        logger.debug(
                            f"get_by_text 点击失败，降级 locator('text='): {e}"
                        )
                if not clicked:
                    try:
                        fallback = page.locator(f"text={text}")
                        fallback = fallback.first if idx == 0 else fallback.nth(idx)
                        await fallback.click(timeout=10000)
                        clicked = True
                    except Exception as e:  # noqa: BLE001
                        # 失败路径（v1.6.1）：**不伪装成功**——保留【错误】
                        # 前缀与告警日志，另附「相近可点击文本 top-5」帮内层
                        # 模型自我纠正（事故里模型臆造了页面不存在的选项
                        # 标签，只回「找不到」⇒ 只能继续猜）。
                        logger.warning(
                            f"[{METADATA_NAME}] 未找到可点击元素（文本: {text}）: {e}"
                        )
                        message = (
                            f"【错误】页面上找不到包含文本「{text}」的可点击元素"
                            f"（index={idx}）。"
                        )
                        candidates = await self._click_text_candidates(page, text)
                        return f"{message}\n{candidates}" if candidates else message
                logger.info(f"[{METADATA_NAME}] 已按文本点击: {text}（index={idx}）")
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                return await self._page_compact(page)
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
                    self._vision_cache_invalidate(event, getattr(page, "url", ""))
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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
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
                try:
                    await page.select_option(selector, value)
                except Exception as exc:  # noqa: BLE001 — 失败要给出可选值
                    logger.warning(
                        f"[{METADATA_NAME}] browse_select_option 失败: {exc}"
                    )
                    # v1.7.0：失败时列出该 select 的**全部真实 option**
                    # （规格 §1.3 #18）。只回【错误】会让模型继续猜选项名
                    # （事故根因②的同型问题：模型编造页面里没有的标签）。
                    message = f"【错误】选择下拉选项失败：{exc}"
                    options = await self._select_options(page, selector)
                    return f"{message}\n{options}" if options else message
                logger.info(f"[{METADATA_NAME}] 已选择下拉项: {selector} = {value}")
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                return await self._page_compact(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_select_option 失败: {e}")
            return f"【错误】选择下拉选项失败：{e}"

    async def _select_options(self, page, selector: str) -> str:
        """列出某个 select 的全部真实 option（label/value/当前选中）；失败返回空串。"""
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return ""
        try:
            raw = await evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (!el || el.tagName.toLowerCase() !== 'select') return null;
                  return Array.from(el.options).slice(0, 50).map((o) => ({
                    label: String(o.label || o.text || '').trim().slice(0, 40),
                    value: String(o.value ?? '').slice(0, 40),
                    selected: !!o.selected,
                  }));
                }""",
                selector,
            )
        except Exception as e:  # noqa: BLE001 — 附加信息，失败不影响原错误
            logger.debug(f"读取 select 选项失败: {e}")
            return ""
        if not isinstance(raw, list) or not raw:
            return ""
        lines = [f"该下拉框共有 {len(raw)} 个真实选项（label / value / 是否选中）："]
        for idx, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            mark = "（当前选中）" if item.get("selected") else ""
            label = item.get("label") or ""
            value = item.get("value") or ""
            lines.append(f"{idx}. 「{label}」 / value={value}{mark}")
        lines.append("请只用上面列出的 label 或 value 重试，禁止臆造选项。")
        return "\n".join(lines)

    async def _element_at_point(self, page, x: int, y: int) -> str:
        """视口坐标命中的元素标签（``role「文本」``）；失败返回空串。"""
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return ""
        try:
            raw = await evaluate(_POINT_ELEMENT_JS, x, y)
        except Exception as e:  # noqa: BLE001 — 附加信息
            logger.debug(f"读取点击点元素失败: {e}")
            return ""
        return str(raw or "").strip()

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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                # v1.7.0：紧凑返回 + 附「点到了哪个元素」（规格 §1.3 #19）——
                # 坐标点击最容易出错，模型需要一条能自我纠正的旁证。
                hit = await self._element_at_point(page, int(x), int(y))
                body = await self._page_compact(page)
                if hit:
                    return f"已点击坐标 ({int(x)}, {int(y)})，命中元素：{hit}\n{body}"
                return f"已点击坐标 ({int(x)}, {int(y)})（该位置没有可读元素）\n{body}"
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
                        # v1.7.0：与 browse_click_text 同口径给相近候选 top-5
                        # （规格 §1.3 #20）——否则模型只能继续猜文本。
                        message = (
                            f"【错误】页面上找不到包含文本「{text}」的可悬停元素。"
                        )
                        candidates = await self._click_text_candidates(page, text)
                        return f"{message}\n{candidates}" if candidates else message
                logger.info(f"[{METADATA_NAME}] 已悬停: {text}")
                # 悬停常触发悬浮菜单/下拉层（画面变化，URL 不变）⇒ 也失效。
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                return await self._page_compact(page)
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
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                return await self._page_compact(page)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_reload 失败: {e}")
            return f"【错误】刷新页面失败：{e}"

    async def browse_checkbox(
        self,
        event: AstrMessageEvent,
        selector: str,
        checked: bool = True,
        index: int = 0,
    ) -> str:
        """网页交互场景专用：设置复选框/单选框/按钮式选项的选中状态。

        Args:
            selector(string): 复选框的 CSS 选择器（多匹配时用 index 指定第几个）。
            checked(boolean): 目标选中状态（true=勾选，false=取消）。默认 true。
            index(number): 多匹配时的序号，从 0 开始，默认 0（第一个匹配）。

        v1.7.0 三条语义（规格 §1.3 #22）：

        1. ``checked`` 变**可选**（默认 ``true``）⇒ 旧调用（只传 selector）仍成立；
        2. 多匹配**不再**冒泡 Playwright 的 ``strict mode violation``，改用
           ``.nth(index)``（v1.6.1 已做 index，本轮把"取第几个"变成默认行为）；
        3. 目标不是 checkbox/radio 时**降级为点击 + 读回**
           ``aria-checked/aria-pressed/data-state``，禁止直接报
           ``Not a checkbox or radio button`` 就结束 —— 按钮式选项（
           ``<button aria-pressed>``）在真实页面里非常常见。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                try:
                    idx = int(index or 0)
                except (TypeError, ValueError):
                    return (
                        f"【错误】index 参数无效：{index!r}，"
                        "应为从 0 开始的整数（0=第一个匹配）。"
                    )
                if idx < 0:
                    return f"【错误】index 不能为负数（收到 {idx}），编号从 0 开始。"
                want = bool(checked)
                loc = page.locator(selector)
                total = await self._locator_count(loc)
                if total is not None and total == 0:
                    return (
                        f"【错误】页面上找不到匹配「{selector}」的复选框/单选框/"
                        "按钮式选项（候选 0 个）。请确认选择器，或先用 "
                        "browse_get_interactive / browse_get_text 看清页面元素。"
                    )
                if total is not None and idx >= total:
                    return (
                        f"【错误】index={idx} 越界：选择器「{selector}」共匹配 "
                        f"{total} 个候选（编号 0..{total - 1}）。"
                        "请把 index 调到该范围内重试。"
                    )
                # 多匹配取第 idx 个（v1.6.1）：直接 click() 会触发 Playwright
                # 的 strict mode violation（事故里 5 个 radio 匹配直接失败，
                # 模型拿不到任何可用的自我纠正信息）。
                target = loc if (total is None or total <= 1) else loc.nth(idx)
                # Playwright async API：is_checked() 是协程，必须 await；
                # 漏 await 时 bool(coroutine) 恒为 True，会把「目标状态与
                # 当前状态一致」误判为不一致而误点击（切换语义反转）。
                state = await self._checkbox_state(target)
                downgraded = False
                if state is None:
                    # 按钮式选项（<button aria-pressed> / role=radio 等）：
                    # 没有原生选中状态 ⇒ **降级为点击**（v1.7.0）：先读回
                    # aria-checked/aria-pressed/data-state，能读就读，读不到
                    # 就点一次再读回，最后如实报告读到什么。
                    downgraded = True
                    before = await self._button_state(target)
                    if before is not None and before == want:
                        logger.info(
                            f"[{METADATA_NAME}] 按钮式选项状态已符合目标，未点击: "
                            f"{selector}[{idx}] -> {want}"
                        )
                    else:
                        await target.click()
                        logger.info(
                            f"[{METADATA_NAME}] 已按按钮式选项点击（无可读原生状态）: "
                            f"{selector}[{idx}]"
                        )
                    after = await self._button_state(target)
                    self._vision_cache_invalidate(event, getattr(page, "url", ""))
                    body = await self._page_compact(page)
                    readback = (
                        "未知（点击后仍读不到 aria-checked/aria-pressed/data-state）"
                        if after is None
                        else ("true" if after else "false")
                    )
                    note = (
                        "（已降级为点击：目标不是 checkbox/radio，没有原生选中状态）"
                        if downgraded
                        else ""
                    )
                    return (
                        f"已设置「{selector}」[index={idx}] 目标状态={want}{note}；"
                        f"点击前状态={before}，点击后状态={readback}。\n{body}"
                    )
                clicked = False
                if state != want:
                    await target.click()
                    clicked = True
                    logger.info(
                        f"[{METADATA_NAME}] 已设置选中状态: {selector}[{idx}] -> {want}"
                    )
                else:
                    logger.info(
                        f"[{METADATA_NAME}] 选中状态已符合目标，未点击: "
                        f"{selector}[{idx}] -> {want}"
                    )
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                body = await self._page_compact(page)
                tail = "" if clicked else "（选中状态已符合目标，未点击）"
                return (
                    f"已设置「{selector}」[index={idx}] 选中状态={want}"
                    f"（当前={state}）{tail}。\n{body}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_checkbox 失败: {e}")
            return f"【错误】设置复选框状态失败：{e}"

    @staticmethod
    async def _button_state(loc) -> bool | None:
        """读按钮式选项的"按下状态"：aria-checked / aria-pressed / data-state。

        与 ``_checkbox_state`` 的区别：这里**接受** ``data-state``（Radix 系
        组件用 ``data-state="checked|unchecked"``），且不尝试 ``is_checked()``
        （已知它会对非 checkbox 抛错）。读不到返回 ``None``（如实报告未知）。
        """
        getter = getattr(loc, "get_attribute", None)
        if getter is None:
            return None
        for attr in ("aria-checked", "aria-pressed", "data-state"):
            try:
                value = await getter(attr)
            except Exception:  # noqa: BLE001 — 单属性失败继续试下一个
                continue
            if value is None:
                continue
            text = str(value).strip().lower()
            if text in ("true", "false"):
                return text == "true"
            if text in ("checked", "on", "selected"):
                return True
            if text in ("unchecked", "off"):
                return False
        return None

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
                run = agent_core.current_run()
                if run is not None and run.native_image:
                    # ---- 影像原生：裁剪图直接进内层上下文（规格 §2.2）----
                    payload = await self._image_native_result(
                        save_path,
                        run=run,
                        size_label=f"{cw}x{ch}",
                        sent_to_user=False,
                        crop=(cx, cy, cw, ch),
                    )
                    if payload is not None:
                        return payload
                # ---- caption 兜底（T2 的内容哈希缓存路径）----
                # kind + 裁剪参数进缓存键（v1.6.1）：裁剪结果与 browse_screenshot
                # 的全图结果不再互相命中（事故里 crop 的描述被当成全图描述返回）。
                desc = await self._describe_screenshot(
                    save_path,
                    event,
                    page_url=getattr(page, "url", None),
                    kind="zoom_crop",
                    crop=(cx, cy, cw, ch),
                )
                if desc:
                    # 内容安全（v1.3.3）：识图结果与页面正文同口径过禁词
                    # （受 enable_banned_filter 总开关控制）。
                    banned = self._check_banned(desc)
                    if banned:
                        return f"【拒绝】裁剪区域识图结果包含违禁内容：{banned}"
                    return f"裁剪区域识图结果：\n{desc}"
                if run is not None and run.native_image:
                    return (
                        "【不可用】本次裁剪图既无法作为图片交给内层模型"
                        f"（可能超过 {agent_core.LLM_IMAGE_MAX_B64} 字符或图片数已达上限），"
                        "且未配置可用的识图兜底模型。请缩小裁剪区域重试，"
                        "或改用 browse_get_text / browse_get_interactive。"
                    )
                return _IMAGE_UNAVAILABLE_TEXT
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

    async def browse_get_interactive(
        self, event: AstrMessageEvent, max_items: int = 40, role_filter: str = ""
    ) -> str:
        """列出当前页面上**真实存在**的可交互元素（v1.7.0 新增，规格 §1.3 #25）。

        每行 8 列：``#n | role | name(可点文本) | type | checked | disabled |
        selector | cx,cy``。``selector`` 由注入 JS 计算的 CSS 路径（``#id``
        优先，否则 ``tag:nth-of-type`` 链），**可直接喂给
        ``browse_click_selector``** —— "枚举 → 确定性落点"这条链路是关闭事故
        根因②（交互原语太粗、只能猜文本）的关键。

        安全边界：只做 DOM 枚举（``page.evaluate`` 只读），不发网络请求、
        不返回 ``innerHTML`` 全文（只回文本名 + selector），拒绝任意 JS 执行。

        Args:
            max_items(number): 最多返回多少个元素，默认 40（上限 200）。
            role_filter(string): 只列该 role 的元素（如 button/link/checkbox/
                radio/tab），留空 = 全部。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                try:
                    limit = int(max_items) if max_items else 40
                except (TypeError, ValueError):
                    return f"【错误】max_items 参数无效：{max_items!r}，应为数字。"
                limit = max(1, min(200, limit))
                role = str(role_filter or "").strip().lower()
                evaluate = getattr(page, "evaluate", None)
                if evaluate is None:
                    return "【错误】当前页面不支持元素枚举（evaluate 不可用）。"
                try:
                    raw = await evaluate(_INTERACTIVE_JS, limit, role)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[{METADATA_NAME}] browse_get_interactive 枚举失败: {e}"
                    )
                    return f"【错误】枚举可交互元素失败：{e}"
                items = [
                    item
                    for item in (raw or [])
                    if isinstance(item, dict) and item.get("selector")
                ]
                if not items:
                    suffix = f"（role_filter={role}）" if role else ""
                    return (
                        f"当前页面上没有找到可交互元素{suffix}。"
                        "可以先用 browse_get_text 看页面文字，或用 browse_screenshot 看外观。"
                    )
                info = await self.extractor.extract_page_info(page)
                lines = [
                    f"URL: {info['url']}",
                    f"可交互元素共 {len(items)} 个"
                    f"（#{'-'.join(str(i) for i in (0, max(0, len(items) - 1)))}；"
                    "selector 可直接用于 browse_click_selector）：",
                    "#n | role | name | type | checked | disabled | selector | cx,cy",
                ]
                # v1.7.0 repair（评审 R5）：``#n`` **就是** browse_click_selector 的
                # ``index``（0 起算）。此前这里从 1 数、而 index 从 0 起 —— 模型看到
                # "#3" 会传 index=3，实际点到第 4 个（同页三个原语 click_link /
                # click_coords / checkbox 都是 0 起，只有这里不一致）。
                shown = 0
                dropped = 0
                for idx, item in enumerate(items, start=0):
                    name = str(item.get("name") or "")
                    # v1.7.0 repair（评审 V3）：本出口也过禁词，但**逐行丢弃**而不是
                    # 整段拒绝 —— 页面上一个违禁词不该让整个元素清单消失（那等于
                    # 削掉内层能力）。与 ``_filter_banned_results`` 同口径。
                    if name and self._check_banned(name):
                        dropped += 1
                        continue
                    shown += 1
                    state = item.get("state") or "-"
                    disabled = "是" if item.get("disabled") else "否"
                    visible = "" if item.get("visible", True) else "（不可见）"
                    lines.append(
                        f"{idx} | {item.get('role') or '-'} | "
                        f"{name or '-'}{visible} | "
                        f"{item.get('type') or '-'} | {state} | {disabled} | "
                        f"{item.get('selector')} | {item.get('x')},{item.get('y')}"
                    )
                if dropped:
                    lines.append(
                        f"（另有 {dropped} 个元素的文本命中禁词，已按 security.banned_words 略去）"
                    )
                lines.append(
                    "点击请用 browse_click_selector(selector=…, index=…)："
                    "上面的 #n **就是** index（都从 0 起算），可直接照抄；"
                    "selector 里的 nth-of-type 则从 1 开始 —— 两者是不同维度，"
                    "同一 selector 多匹配时才需要改用 index 选第几个。"
                )
                return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_get_interactive 失败: {e}")
            return f"【错误】枚举可交互元素失败：{e}"

    async def browse_click_selector(
        self, event: AstrMessageEvent, selector: str, index: int = 0
    ) -> str:
        """按 CSS 选择器点击元素（v1.7.0 新增，规格 §1.3 #26）。

        走 ``locator(selector).nth(index)`` + ``wait_for(state="visible")`` +
        ``click(timeout=10000)``；返回紧凑摘要 + 该元素点击后的
        ``aria-checked``/``aria-pressed`` 读回值（用于确认"到底选没选中"）。

        为什么单独加这个原语而不是让模型用坐标：``browse_get_interactive``
        给出的 selector 是**确定性落点**，比坐标/文本都稳（坐标会随滚动失效，
        文本会随多匹配选错）。

        Args:
            selector(string): CSS 选择器，通常直接取自 browse_get_interactive。
            index(number): 多匹配时的序号，从 0 开始，默认 0（第一个）。
        """
        try:
            async with self._lock_for(event):
                page, err = await self._get_page(event)
                if page is None:
                    return f"【错误】{err}"
                sel = str(selector or "").strip()
                if not sel:
                    return "【错误】selector 不能为空。"
                try:
                    idx = int(index or 0)
                except (TypeError, ValueError):
                    return (
                        f"【错误】index 参数无效：{index!r}，"
                        "应为从 0 开始的整数（0=第一个匹配）。"
                    )
                if idx < 0:
                    return f"【错误】index 不能为负数（收到 {idx}），编号从 0 开始。"
                loc = page.locator(sel)
                total = await self._locator_count(loc)
                if total is not None and total == 0:
                    return (
                        f"【错误】页面上找不到匹配「{sel}」的元素（候选 0 个）。"
                        "请先用 browse_get_interactive 看清可用的 selector。"
                    )
                if total is not None and idx >= total:
                    return (
                        f"【错误】index={idx} 越界：选择器「{sel}」共匹配 {total} 个"
                        f"（编号 0..{total - 1}）。"
                    )
                target = loc if (total is None or total <= 1) else loc.nth(idx)
                waiter = getattr(target, "wait_for", None)
                if waiter is not None:
                    try:
                        await waiter(state="visible", timeout=10000)
                    except Exception as e:  # noqa: BLE001 — 等不到可见也试一次点击
                        logger.debug(f"wait_for(visible) 未通过，仍尝试点击: {e}")
                try:
                    await target.click(timeout=10000)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[{METADATA_NAME}] browse_click_selector 点击失败: {e}"
                    )
                    return (
                        f"【错误】点击「{sel}」[index={idx}] 失败：{e}。"
                        "可用 browse_get_interactive 重新枚举，或改用 "
                        "browse_click_text / browse_click_coords。"
                    )
                logger.info(
                    f"[{METADATA_NAME}] 已按 selector 点击: {sel}[{idx}]"
                )
                self._vision_cache_invalidate(event, getattr(page, "url", ""))
                after = await self._button_state(target)
                readback = (
                    "读不到（该元素没有 aria-checked/aria-pressed/data-state）"
                    if after is None
                    else ("true" if after else "false")
                )
                body = await self._page_compact(page)
                return (
                    f"已点击「{sel}」[index={idx}]（候选 {total if total is not None else '未知'} 个）；"
                    f"点击后状态读回：{readback}。\n{body}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{METADATA_NAME}] browse_click_selector 失败: {e}")
            return f"【错误】按选择器点击失败：{e}"

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

    @staticmethod
    def _vision_image_hash(image_path: str) -> str:
        """图像内容哈希：sha256(截图文件**字节**) 十六进制前 32 位（v1.6.1）。

        这是识图缓存键的主成分，必须是真实图片字节——时间戳、序号、文件
        路径都不能代替它（事故里同一 URL 连续截图的路径各不相同但画面高度
        相关，用路径/序号做键仍会错配或互相覆盖）。文件不可读（不存在、
        无权限、非本地路径如 data: URI）时返回空串，调用方据此跳过缓存
        （fail-closed：宁可多调一次识图，也不返回可能过期的描述）。
        """
        if not image_path:
            return ""
        try:
            with open(image_path, "rb") as fp:
                digest = hashlib.sha256(fp.read()).hexdigest()
        except (OSError, TypeError, ValueError):
            return ""
        return digest[:_VISION_IMAGE_HASH_LEN]

    def _vision_cache_key(
        self,
        page_url: str | None,
        event: AstrMessageEvent | None,
        image_path: str = "",
        kind: str = "screenshot",
        crop: tuple[int, int, int, int] | None = None,
        prompt: str = "",
        provider_id: str = "",
    ) -> tuple | None:
        """构造识图缓存键（v1.6.1 七元组）；缺任一必需成分时返回 None。

        键 = ``(umo, 规范化URL, 工具种类, 裁剪参数, 图像内容哈希,
        提示词哈希, provider_id)``：

        - ``umo`` + ``规范化URL``：会话与页面隔离（v1.3.0 语义保留），也是
          交互后按 ``(umo, URL)`` 前缀失效的依据（见 _vision_cache_invalidate）；
        - ``工具种类``（screenshot / zoom_crop）+ ``裁剪参数``：全图与局部
          裁剪不再互相串号（事故里 crop 的描述被当成 screenshot 的结果）；
        - ``图像内容哈希``：**画面一变就不命中**，这是事故主因的正面修复；
        - ``提示词哈希`` + ``provider_id``：运营方改 vision_prompt 或换识图
          provider 后，旧描述不再命中（否则会按旧口径复用旧描述）。

        None 表示本次调用不参与缓存（不读不写）：缺页面 URL、缺会话、
        或读不到图像字节（见 _vision_image_hash）。
        """
        url = self._normalize_vision_cache_url(page_url or "")
        if not url:
            return None
        umo = self._umo_of(event) if event is not None else ""
        if not umo:
            return None
        image_hash = self._vision_image_hash(image_path)
        if not image_hash:
            return None
        variant = "" if crop is None else ",".join(str(int(v)) for v in crop)
        prompt_hash = hashlib.sha256(
            (prompt or "").encode("utf-8")
        ).hexdigest()[:_VISION_PROMPT_HASH_LEN]
        return (
            umo,
            url,
            str(kind or "screenshot"),
            variant,
            image_hash,
            prompt_hash,
            str(provider_id or ""),
        )

    def _vision_cache_invalidate(
        self, event: AstrMessageEvent | None, page_url: str | None
    ) -> int:
        """交互后失效：删除该 ``(umo, 规范化URL)`` 下的全部识图缓存条目。

        精确键规则（v1.6.1）：键的前两位就是 ``(umo, 规范化URL)``，因此
        「按前两位前缀删除」只清掉**本会话本页面**的条目 —— 其它会话
        （不同 umo）与其它页面（不同规范化 URL，含其它标签页）的条目一律
        不动，不存在跨标签页/跨 URL 误删。

        内容哈希已能在正确性上覆盖「画面变了」，本失效是纵深防御：交互后
        连字节都完全相同（画面真没变）时强制重新识图，避免任何依赖「字节
        相同 ⇒ 语义相同」的残余假设。

        Args:
            event: 消息事件（取会话标识），可为 None。
            page_url: 交互发生时页面的 URL（**交互前**的 URL，导航类原语
                须在动作之前取值），可为 None。

        Returns:
            int: 实际删除的条目数（0 表示无可删/键不成立）。
        """
        cache = getattr(self, "_vision_cache", None)
        if not isinstance(cache, dict) or not cache:
            return 0
        url = self._normalize_vision_cache_url(page_url or "")
        if not url:
            return 0
        umo = self._umo_of(event) if event is not None else ""
        if not umo:
            return 0
        doomed = [
            k
            for k in cache
            if isinstance(k, tuple) and len(k) >= 2 and k[0] == umo and k[1] == url
        ]
        for k in doomed:
            del cache[k]
        if doomed:
            # v1.7.0 repair（评审 R7）：URL 的 query 里可能有 token/签名，日志只留
            # 参数名（_redact_url 把值打成 ***）。
            logger.debug(
                f"[{METADATA_NAME}] 交互后失效识图缓存 {len(doomed)} 条: "
                f"{_redact_url(str(page_url or ''))}"
            )
        return len(doomed)

    def _vision_cache_put(self, key: tuple | None, text: str) -> None:
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
        kind: str = "screenshot",
        crop: tuple[int, int, int, int] | None = None,
    ) -> str:
        """用多模态模型描述截图（配置了 vision_provider_id 才执行）。

        行为：配置的识图 provider 未加载/失效时自动回退当前会话聊天
        Provider（带警告日志）；未配置或全部不可用时返回空串（不抛异常）。
        模型返回拒识文案（纯文本模型不支持图片，如 "[Unsupported Image]"）
        时记警告日志并返回明确提示文案，不把拒识文本误当视觉描述。
        双通道降级：contexts（ImageURLPart）失败后回退 image_urls。
        识图短时缓存（v1.3.0 起；v1.6.1 换键）：提供 page_url 且
        vision_cache_ttl>0 时，按 _vision_cache_key 的七元组（含**图像内容
        哈希**、工具种类/裁剪参数、提示词哈希、provider）在 TTL 内复用上次
        结果（返回文本带 [缓存] 前缀，另记 debug 日志）。画面一变即不命中，
        全图与裁剪不再串号；terminate 时整体清空。

        Args:
            image_path: 截图本地路径（缓存键的图像字节来源）。
            event: 消息事件（失效回退时确定会话），可为 None。
            page_url: 截图对应页面的 URL（缓存键来源），可为 None 表示
                不参与缓存。
            kind: 产出该图的工具种类（screenshot / zoom_crop），进缓存键。
            crop: zoom_crop 的裁剪参数 (x, y, w, h)，进缓存键；全图传 None。

        Returns:
            str: 识图描述文本；未配置或调用失败返回空串；拒识时返回提示
            文案（均不抛异常）。
        """
        # 先解析 provider 与提示词：两者都进缓存键（改配置 ⇒ 旧描述不命中）。
        provider_id = await self._resolve_vision_provider_id(event)
        if not provider_id:
            # v1.7.0 repair（评审 R6）：内层模型非多模态、又没配兜底 ⇒ 这条
            # 截图必然变成【不可用】，日志要说清"兜底 provider 是空的"。
            run = agent_core.current_run()
            if run is not None and not run.native_image:
                logger.info(
                    f"[{self.metadata_name}] 内层模型非多模态，改用识图兜底 "
                    "provider=（空=未配置，截图将不可用）"
                )
            return ""
        run_now = agent_core.current_run()
        if run_now is not None and not run_now.native_image:
            logger.info(
                f"[{self.metadata_name}] 内层模型非多模态，改用识图兜底 "
                f"provider={provider_id}（caption 路径）"
            )
        # 识图提示词：优先用用户配置的 vision_prompt（实时读取），空则回退
        # 默认提示词。
        prompt = self._live_vision_prompt() or _DEFAULT_VISION_PROMPT
        # 识图短时缓存命中检查（TTL=0 或键成分不全时不启用）。
        ttl = int(getattr(self, "vision_cache_ttl", 0) or 0)
        cache_key = (
            self._vision_cache_key(
                page_url,
                event,
                image_path,
                kind=kind,
                crop=crop,
                prompt=prompt,
                provider_id=provider_id,
            )
            if ttl > 0
            else None
        )
        if cache_key is not None:
            hit = self._vision_cache.get(cache_key)
            if hit is not None:
                ts, cached_text = hit
                if time.time() - ts <= ttl:
                    logger.debug(
                        f"[{self.metadata_name}] 识图缓存命中（TTL={ttl}s，"
                        f"kind={kind}）: {_redact_url(str(page_url or ''))}"
                    )
                    return f"[缓存] {cached_text}"
                del self._vision_cache[cache_key]
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

        internal 档恒 False ⇒ ``_reconcile_search_tool`` 会把外层工具 deactivate
        （模型根本看不到它；规格 §4.2）。内层工具在不在由
        ``_rebuild_browser_toolset`` 用 ``_search_pool_ready()`` 判断。
        """
        return self._search_pool_ready() and (
            getattr(self, "search_mode", "external") == "external"
        )

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

    # v1.6.0：后台引擎按名字定位三个工具时复用同一个归属判定（不新造第二套）。
    _is_own_llm_tool = _is_own_search_tool

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

        机制（spec §6.2，captain 裁定 D4）：LLM 工具装饰器在 import 期就注册
        （平台没有"按配置注册"的口子），因此用 ``activate_llm_tool_async``
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

        # v1.7.0：want 多一条 ``search_mode == "external"``（规格 §4.2-1）。
        # internal 档 = 池搜索已经挪进内层工具，外层同名工具**必须**对模型
        # 不可见；否则就是"同名两份并存"——主 LLM 与内层都会搜、结果重复、
        # token 翻倍，还多一个"该用哪个"的决策面（main.py 顶部
        # ``_SEARCH_TOOL_NAME`` 的注释记过同类教训）。
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
            if self.search_mode == "internal":
                # v1.7.0：internal 档停用外层工具是**正常状态**，原因要写清
                # （否则运营方会以为"搜索坏了"）。池是否可用影响内层工具在不在。
                reason = (
                    f"search.mode=internal（池搜索已挂到内层工具 "
                    f"{_INNER_SEARCH_TOOL_NAME}，外层对模型不可见）"
                )
            elif not self.search_enabled:
                reason = "search.enabled=false"
            else:
                reason = "池内无可用 provider（缺 api_key / 全部禁用 / 端点非法）"
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

        双模式（v1.7.0，配置 search.mode）：
        - ``external``（默认）：就是上面这套 —— 你（主 LLM）直接拿到原始结构化
          结果；
        - ``internal``：池搜索已经挪进浏览器子代理（内层工具
          ``browse_web_search``），本工具被插件强制停用、你根本看不到它；要搜
          就用 ``browse_web`` 委托，拿回的是子代理加工过的结论而不是原始 JSON。
        """
        # 后台模式热更新（v1.5.0）：入口第一行刷新配置，Dashboard 把
        # background_mode 改回 off 时下一次调用即生效（F1）。
        self._refresh_config()
        # 双模式收敛（v1.7.0 repair，评审 R4）：本工具也是外层入口。鉴权先做
        # （对齐会写全局持久化名单，未授权调用不该产生 DB 写），再做收敛。
        allowed, _deny_reason = self._is_session_allowed(event)
        if allowed:
            await self._sync_search_mode()
        # 配置里刚切成 internal 时，本入口可能还会被调用一次（核心在模型请求
        # 装配期缓存了工具集），这里显式拒绝并指向 browse_web —— 不给"同名
        # 两份并存"留任何缝隙。
        if self.search_mode == "internal":
            return self._search_payload(
                status="disabled",
                code="handled_by_agent",
                summary=(
                    "【拒绝】search.mode=internal：池搜索由浏览器子代理执行，"
                    "请改用 browse_web 委托搜索任务。"
                ),
                query=(query or "").strip(),
                hint=(
                    "插件配置 search.mode=internal 时，池搜索是内层工具 "
                    "browse_web_search（子代理调用）；外层同名工具对模型不可见。"
                    "若希望自己直接拿到原始搜索结果，请把 search.mode 改回 external。"
                ),
            )
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
        if self._search_tool_active:
            text += self._build_search_instruction()
        return text
