"""core.a11y — 感知层（v1.7.5）：CDP Accessibility 全树 → 裁剪 → 结构视图 + Set-of-Mark 兜底。

本模块是**唯一**的页面感知来源（规格 §2）。输入一个 Playwright ``page``，
输出一个 :class:`PageSnapshot`；下游（executor 的候选、verify 的差分与观测块、
SoM 的框）全部从这一个对象派生 —— 禁止各处再各自 ``page.evaluate`` 一遍。

四条不可谈判的设计事实（规格附录 A，本轮已实测复现）：

1. **A11y 只能走 CDP**：Playwright 1.61.0 **没有** ``page.accessibility``
   （``hasattr(Page, "accessibility")`` 为 ``False``）。取树路径固定为
   ``page.context.new_cdp_session(page)`` → ``Accessibility.enable``（幂等一次）→
   ``Accessibility.getFullAXTree``（**逐帧显式传 frameId**，含主帧）。
2. **AX 树已穿透 open shadow DOM，但不含子帧节点**：所以 shadow 无需额外代码，
   iframe 必须逐帧取（``Page.getFrameTree``）。closed shadow 只能靠 SoM（§4.5 R-A8）。
3. **只给模型「角色 + 名字（+ 状态）」**：绝不输出像素坐标、selector、class。
   坐标只存在于插件内部（:class:`A11yNode.box`），并且**只**用于画 Set-of-Mark 框。
4. **截图是兜底，不是常规通道**：``screenshot_fallback`` 关闭时整条 SoM 路径不启用；
   SoM 图上模型只回**标签编号**（:class:`MarkEpoch`），编号只在本张图有效。

本模块**纯 Python**：顶层不 import playwright / astrbot（铁律 1），playwright 对象一律
以鸭子类型传递。所有异步入口都接受可注入的 CDP 会话工厂 / 页面函数，因此单测用
JSON 夹具 + fake CDP 即可，不需要真浏览器。

降级语义（E1–E3，规格 §2.2 与 §14）：任何取树失败都返回**带 code 的降级结果**
（``timeout`` / ``unavailable`` / ``error`` / ``frame_unavailable``），**绝不**返回
"看起来正常的空树" —— 观测块据此提示模型改用截图或直接给结论。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:  # pragma: no cover — 仅类型检查，运行时永不导入
    from playwright.async_api import Page  # noqa: F401 — 只用于类型标注

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（角色白名单 / 预算 / 降级码）
# ---------------------------------------------------------------------------

#: 参与"给模型看"的角色白名单（AX 树 role 归一到小写；不在表里的 role 只在
#: 文本视图里出现，不进结构视图）。命名与 playwright 的 ``get_by_role`` 取值域对齐。
RENDER_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "combobox",
        "textbox",
        "searchbox",
        "slider",
        "spinbutton",
        "listbox",
        "heading",
        "img",
        "cell",
        "gridcell",
        "columnheader",
        "rowheader",
        "treeitem",
        "dialog",
        "alertdialog",
        "alert",
        "status",
        "progressbar",
        "tooltip",
        "form",
        "search",
        "navigation",
        "main",
        "article",
        "table",
        "tabpanel",
        "tree",
        "radiogroup",
        "list",
        "listitem",
        "tablist",
        "menu",
        "menubar",
    }
)

#: 需要"人"来操作的角色（决定是否进 SoM 候选与可交互统计）。
ACTIONABLE_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "combobox",
        "textbox",
        "searchbox",
        "slider",
        "spinbutton",
        "treeitem",
    }
)

#: 改变页面的动作（⇒ SoM mark 纪元**作废**，规格 §2.4 的生命周期表）。
MUTATING_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "type",
        "select",
        "check",
        "press",
        "scroll",
        "hover",
        "navigate",
        "back",
        "forward",
        "reload",
        "tab",
    }
)

#: 只读动作（**不作废** mark：模型需要对照表看图）。
READONLY_ACTIONS: frozenset[str] = frozenset(
    {"read", "screenshot", "wait", "done", "observe"}
)

#: 默认预算（= v6 ``perception`` 组默认值，规格 §7.1）。
DEFAULT_MAX_NODES = 300
DEFAULT_MAX_NAME_CHARS = 120
DEFAULT_MAX_MARKS = 60

#: v6 的钳制区间（``A11yLimits.clamped`` 使用，与 ``spec/v175-config-keys.json`` 对齐）。
CLAMP_MAX_NODES = (50, 1000)
CLAMP_MAX_NAME_CHARS = (20, 500)
CLAMP_MAX_MARKS = (5, 200)

#: 帧链深度上限（规格 §2.3：超出记 R-A8 候选）。
FRAME_DEPTH_LIMIT = 3

#: 单次 CDP 取树预算（秒）。取树是"读"，与动作超时（``resources.action_timeout_s``）无关。
DEFAULT_AX_TIMEOUT_S = 8.0

#: SoM 标记的有效期（秒）：超过即作废（规格 §2.4）。
SOM_MARK_TTL_S = 60.0

#: SoM overlay 的 DOM id（必须能精确摘除，异常路径也不例外）。
SOM_OVERLAY_ID = "__blm_som__"

#: 敏感字段名（``role=textbox`` 且 name 命中 ⇒ value 打码，规格 §2.2 规则 8）。
SENSITIVE_NAME_RE = re.compile(r"密码|口令|password|passcode|token|验证码", re.IGNORECASE)

#: 降级码（E1–E3）。观测块 / 台账据此区分"页面没有节点"与"感知坏了"。
DEGRADE_OK = "ok"
DEGRADE_TIMEOUT = "timeout"
DEGRADE_UNAVAILABLE = "unavailable"
DEGRADE_ERROR = "error"
DEGRADE_FRAME = "frame_unavailable"
DEGRADE_NO_PAGE = "no_page"

#: ``nodes=0`` 时追加给模型的一行（规格 §2.2；只在结构视图为空时出现）。
NO_NODES_HINT = (
    "⚠ 本页没有可交互节点（可能是 canvas/图片型界面：browse_screenshot 看标注图）"
)

#: 结构视图被预算截断时的提示模板（``more`` = 被截掉的节点数）。
MORE_NODES_HINT = "…（还有 {more} 个节点：browse_observe(scope=\"full\") 看全部）"

#: D9-① 的逐字提示：有请求被 SSRF 兜底策略拦下时，必须告诉模型
#: （否则站点"看起来没数据"，模型会以为页面坏了而反复重试）。
SSRF_NOTICE_TEMPLATE = "⚠ 本页有 {n} 个请求被 SSRF 策略拦截"

#: 角色 -> 折叠时用的中文量词（"共 7 个选项"）。
_ROLE_NOUN: dict[str, str] = {
    "radio": "选项",
    "option": "选项",
    "checkbox": "复选项",
    "button": "按钮",
    "link": "链接",
    "tab": "标签页",
    "menuitem": "菜单项",
    "row": "行",
    "cell": "单元格",
}


# ---------------------------------------------------------------------------
# 数据结构（规格 §2.1 逐字实现 + 本轮任务要求的 focused/checked_raw）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A11yNode:
    """一个**已经裁剪好、可以直接给模型看**的可访问性节点。

    字段取舍原则：只保留"能决定点不点、填不填、选不选"的信息。
    DOM 细节（tagName/class/selector 路径）**一律不给模型** —— 那是 v1.7.0
    ``browse_get_interactive`` 的老路，模型会照着 selector 猜，猜错就是 D2/D4。

    ``box`` 是**插件内部**数据：(x, y, w, h)，主视口 CSS 坐标（事实 F5：
    Playwright ``bounding_box()`` 含 iframe 内元素也返回主视口坐标）。它只用于
    画 Set-of-Mark 框；**任何渲染函数都不得把它写进给模型的文本**。
    """

    ref: int  # 本次快照内的稳定序号（1 起；跨快照不复用）
    role: str  # 小写 ARIA role
    name: str  # accessible name（已按 max_name_chars 截断）
    value: str = ""  # 输入框/下拉框的当前值（密码类打码见 _mask_value）
    checked: bool | None = None  # checkbox/radio/switch/menuitemcheckbox 三态
    disabled: bool = False
    focused: bool = False  # 键盘焦点（v1.7.5 新增：决定 type/press 的落点）
    expanded: bool | None = None  # combobox/menuitem 的展开态
    level: int = 0  # heading 层级 / listitem 缩进；0=不适用
    frame_path: tuple[str, ...] = ()  # 帧链：()=主帧，("iframe#fr",)=一层子帧
    frame_id: str = ""  # CDP frameId（逐帧取树时用）
    node_id: int = 0  # CDP nodeId / backendDOMNodeId（**跨快照不稳定，禁止外传**）
    box: tuple[float, float, float, float] | None = None  # (x, y, w, h) 主视口坐标
    checked_raw: str = ""  # "true"/"false"/"mixed"（原样，供渲染 mixed）
    truncated: bool = False  # name/value 是否被截断过
    depth: int = 0  # AX 树深度（ignored 祖先也会计入；只用于预算排序与调试）

    # -- 派生判定 ---------------------------------------------------------

    @property
    def visible(self) -> bool:
        """是否有**可见面积**（规则 6：``box`` 缺失或小于 2px ⇒ 不可见）。

        D2 的直接根因：``opacity-0`` / ``pointer-events:none`` 的悬浮提示曾经
        被当成"第 N 个可点元素"。
        """
        box = self.box
        if box is None:
            return False
        return box[2] >= 2.0 and box[3] >= 2.0

    @property
    def actionable(self) -> bool:
        """是否需要"人"来操作（决定是否进 SoM 候选与可交互统计）。"""
        return self.role in ACTIONABLE_ROLES and not self.disabled and self.visible

    @property
    def checked_text(self) -> str:
        """``checked`` 的渲染文本：``true`` / ``false`` / ``mixed`` / ``""``（不适用）。"""
        if self.checked_raw:
            return self.checked_raw
        if self.checked is None:
            return ""
        return "true" if self.checked else "false"

    def key(self) -> tuple[Any, ...]:
        """去重键（懒加载多轮合并 / 相邻合并共用）：不含 ref 与 box。"""
        return (
            self.frame_path,
            self.role,
            self.name,
            self.value,
            self.checked_raw,
        )


@dataclass(frozen=True)
class PageSnapshot:
    """一次页面状态的**完整快照**（感知层的唯一产物）。

    所有下游（executor 的候选、verify 的差分、verify 的观测渲染、SoM 的框）
    都从这一个对象派生 —— 禁止各处再各自 ``page.evaluate`` 一遍。
    """

    url: str
    title: str
    viewport: tuple[int, int]  # (width, height) CSS 像素
    dpr: float  # devicePixelRatio（= resources.viewport.device_scale_factor）
    scroll_y: int
    scroll_height: int
    nodes: tuple[A11yNode, ...]
    pruned: int  # 被裁掉的节点数（ignored/隐藏/超预算）
    frames: int  # 参与取树的帧数（主帧算 1）
    text: str = ""  # 正文（extract 后，按 resources.max_text_chars 截断）
    text_truncated: bool = False
    ssrf_blocked: int = 0  # 本页被 SSRF 兜底策略拦截的请求数（D9）
    taken_at: float = 0.0  # time.monotonic()
    truncated: bool = False  # 节点是否因预算被截断
    raw_total: int = 0  # 裁剪前的原始节点总数（含每帧的 ignored）
    degraded: tuple[str, ...] = ()  # 降级说明（空 = 感知正常）

    # -- 查询 -------------------------------------------------------------

    def by_ref(self, ref: int) -> A11yNode | None:
        """按 ref 取节点（``None`` = 该 ref 不属于本次快照）。"""
        for node in self.nodes:
            if node.ref == ref:
                return node
        return None

    def actionable_nodes(self) -> tuple[A11yNode, ...]:
        """全部可交互且可见的节点（SoM 候选源）。"""
        return tuple(node for node in self.nodes if node.actionable)

    def digest(self) -> str:
        """状态摘要（差分用）：url|title|节点三元组哈希|正文哈希|scrollY。"""
        node_part = "|".join(
            f"{n.ref}:{n.role}:{n.name}:{n.checked_text}:{n.value}" for n in self.nodes
        )
        return "|".join(
            (
                redact_url(self.url),
                self.title,
                _sha(node_part),
                _sha(self.text),
                str(self.scroll_y),
            )
        )

    def raw_text_for_log(self) -> str:
        """**日志专用**：只给长度与节点数，绝不给页面正文（规格 §9.4 沿用）。"""
        return (
            f"url={redact_url(self.url)} title_len={len(self.title)} "
            f"nodes={len(self.nodes)} pruned={self.pruned} frames={self.frames} "
            f"text_len={len(self.text)} scroll={self.scroll_y}/{self.scroll_height}"
        )


@dataclass(frozen=True)
class A11yLimits:
    """感知预算（= v6 ``perception`` 组，规格 §7.1；关闭/非法值钳制到区间）。"""

    max_nodes: int = DEFAULT_MAX_NODES
    max_name_chars: int = DEFAULT_MAX_NAME_CHARS
    som_max_marks: int = DEFAULT_MAX_MARKS
    frame_depth: int = FRAME_DEPTH_LIMIT
    ax_timeout_s: float = DEFAULT_AX_TIMEOUT_S

    def clamped(self) -> "A11yLimits":
        """把三个预算钳制到 v6 声明的区间（面板手改 JSON 也不会把插件打挂）。"""
        return replace(
            self,
            max_nodes=_clamp_int(self.max_nodes, *CLAMP_MAX_NODES, DEFAULT_MAX_NODES),
            max_name_chars=_clamp_int(
                self.max_name_chars, *CLAMP_MAX_NAME_CHARS, DEFAULT_MAX_NAME_CHARS
            ),
            som_max_marks=_clamp_int(
                self.som_max_marks, *CLAMP_MAX_MARKS, DEFAULT_MAX_MARKS
            ),
            frame_depth=max(1, min(FRAME_DEPTH_LIMIT, int(self.frame_depth or 1))),
            ax_timeout_s=max(1.0, min(60.0, float(self.ax_timeout_s or 1.0))),
        )

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "A11yLimits":
        """从插件配置读预算（容忍 v5/v6 两种形态与字符串数字）。

        v6 键：``perception.a11y_max_nodes`` / ``a11y_max_name_chars`` /
        ``som_max_marks``；v5 没有这三项（新增键），缺失即用默认值。
        """
        base = cls().clamped()
        if not isinstance(cfg, Mapping):
            return base
        group = cfg.get("perception")
        group = group if isinstance(group, Mapping) else cfg  # 也接受扁平化输入
        return replace(
            base,
            max_nodes=_as_int(group.get("a11y_max_nodes"), base.max_nodes),
            max_name_chars=_as_int(
                group.get("a11y_max_name_chars"), base.max_name_chars
            ),
            som_max_marks=_as_int(group.get("som_max_marks"), base.som_max_marks),
        ).clamped()


# ---------------------------------------------------------------------------
# ref 分配（同一快照内唯一自增、跨快照绝不复用 —— 规格 §2.1）
# ---------------------------------------------------------------------------


class RefAllocator:
    """ref 分配器：单调递增，**跨快照不复用**旧编号。

    为什么不能"每次快照都从 1 开始"：模型会把上一轮观测里的 ``#3`` 当真
    继续用，而页面变了以后 ``#3`` 已经是另一个元素 —— SoM 的 ``R-A9``
    （mark 纪元）与这里的 ref 纪元是同一个道理。任务边界（新任务）才
    :meth:`reset`。
    """

    __slots__ = ("_next",)

    def __init__(self, start: int = 1) -> None:
        self._next = max(1, int(start))

    @property
    def next_ref(self) -> int:
        """下一次分配会得到的编号。"""
        return self._next

    def allocate(self, count: int) -> int:
        """预分配 ``count`` 个连续编号，返回第一个编号。"""
        count = max(0, int(count))
        first = self._next
        self._next += count
        return first

    def reset(self) -> None:
        """回到 1（**只**在新任务边界调用；跨快照调用会制造 ref 复用）。"""
        self._next = 1


# ---------------------------------------------------------------------------
# 纯函数工具：截断 / 打码 / 属性解析 / URL 脱敏
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    """短哈希（差分摘要用；不做安全用途）。"""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def redact_url(url: str, *, keep_query: bool = False) -> str:
    """URL 脱敏：去掉 query 与 fragment（台账 / 观测块共用，规格 §1.6）。

    query 里常见 token、搜索词与个人信息；观测块只给"哪一页"，不给参数。
    """
    raw = str(url or "")
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:  # pragma: no cover — 极端畸形 URL
        return raw.split("#", 1)[0]
    if not parts.scheme and not parts.netloc:
        return raw if keep_query else raw.split("#", 1)[0].split("?", 1)[0]
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.query if keep_query else "",
            "",
        )
    )


def truncate_text(text: Any, limit: int) -> tuple[str, bool]:
    """按字符数截断并返回 ``(文本, 是否截断)``（省略号占 1 个字符位）。"""
    value = "" if text is None else str(text)
    limit = max(1, int(limit))
    if len(value) <= limit:
        return value, False
    return value[: max(0, limit - 1)] + "…", True


def _mask_value(role: str, name: str, value: str) -> str:
    """敏感值打码：``textbox``/``searchbox`` 且 name 命中敏感词 ⇒ ``***``。"""
    if role not in ("textbox", "searchbox"):
        return value
    if value and SENSITIVE_NAME_RE.search(name or ""):
        return "***"
    return value


def ax_property_map(node: Mapping[str, Any]) -> dict[str, Any]:
    """把 AX 节点的 ``properties`` 归一成 dict。

    CDP 形状（事实 F3）：``properties: [{name, value: {type, value}}]``，
    ``checked`` / ``expanded`` / ``level`` / ``focusable`` / ``focused`` 都在里面。
    """
    out: dict[str, Any] = {}
    for item in node.get("properties") or ():
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not name:
            continue
        raw = item.get("value")
        if isinstance(raw, Mapping):
            raw = raw.get("value")
        out[str(name)] = raw
    return out


def tri_state(raw: Any) -> tuple[bool | None, str]:
    """``checked`` 三态归一：返回 ``(bool|None, "true"|"false"|"mixed"|"")``。"""
    if raw is None:
        return None, ""
    if isinstance(raw, bool):
        return raw, "true" if raw else "false"
    text = str(raw).strip().lower()
    if text == "mixed":
        return None, "mixed"
    if text in ("true", "1", "yes"):
        return True, "true"
    if text in ("false", "0", "no"):
        return False, "false"
    return None, ""


def ax_text(value_obj: Any) -> str:
    """取 AX ``role``/``name``/``value`` 的文本（``{type, value}`` 形状）。"""
    if value_obj is None:
        return ""
    if isinstance(value_obj, Mapping):
        inner = value_obj.get("value")
        return "" if inner is None else str(inner)
    return str(value_obj)


# ---------------------------------------------------------------------------
# 帧树（iframe / OOPIF / 不可达帧降级）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameInfo:
    """一个 CDP frame 的身份与帧链（帧链供 executor 逐级 ``frame_locator``）。"""

    frame_id: str
    parent_id: str | None = None
    name: str = ""
    url: str = ""
    path: tuple[str, ...] = ()  # ()=主帧；("iframe#fr",)=一层子帧
    depth: int = 0
    reachable: bool = True
    reason: str = ""

    @property
    def is_main(self) -> bool:
        return self.depth == 0

    def sub_path(self, label: str) -> tuple[str, ...]:
        """构造子帧的帧链。"""
        return self.path + (label,)


@dataclass(frozen=True)
class FrameTreeScan:
    """``flatten_frame_tree`` 的产物。"""

    frames: tuple[FrameInfo, ...]
    clipped: tuple[str, ...] = ()  # 因深度/数量上限未展开的 frameId
    code: str = DEGRADE_OK
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.code == DEGRADE_OK


def frame_label(frame: Mapping[str, Any], *, index: int = 0) -> str:
    """帧链标签：``iframe#name`` 优先；无名帧用 ``iframe[nth=i]``（executor 可直用）。

    事实 F5 + §2.3：帧链定位以 ``frame.name`` 或 ``iframe[src*=…]`` 为准，
    取不到就按帧序号（``iframe >> nth=``）。
    """
    if not isinstance(frame, Mapping):
        return f"iframe[nth={index}]"
    name = str(frame.get("name") or "").strip()
    if name:
        return f"iframe#{name}"
    frame_id = str(frame.get("id") or "").strip()
    if frame_id:
        return f"iframe[nth={index}]"
    return f"iframe[nth={index}]"


def flatten_frame_tree(
    tree: Mapping[str, Any] | None,
    *,
    max_depth: int = FRAME_DEPTH_LIMIT,
    max_frames: int = 32,
) -> FrameTreeScan:
    """把 ``Page.getFrameTree`` 的结果展平成 :class:`FrameInfo` 元组（纯函数）。

    Args:
        tree: ``Page.getFrameTree`` 的返回值，或其中的 ``frameTree`` 子树。
        max_depth: 帧链深度上限（规格 §2.3：默认 3）。
        max_frames: 帧总数上限（防恶意页面用上千个 iframe 打爆预算）。

    Returns:
        :class:`FrameTreeScan`；``tree`` 为空/非法时返回带原因的降级结果
        （``code=unavailable``），**不抛异常**。
    """
    root = tree
    if isinstance(tree, Mapping) and "frameTree" in tree:
        root = tree.get("frameTree")
    if not isinstance(root, Mapping) or not isinstance(root.get("frame"), Mapping):
        return FrameTreeScan(
            frames=(), code=DEGRADE_UNAVAILABLE, reason="Page.getFrameTree 无 frameTree"
        )

    frames: list[FrameInfo] = []
    clipped: list[str] = []

    def walk(
        item: Mapping[str, Any],
        path: tuple[str, ...],
        depth: int,
        parent: str | None = None,
    ) -> None:
        frame = item.get("frame") or {}
        fid = str(frame.get("id") or "")
        if not fid:
            return
        if len(frames) >= max_frames:
            clipped.append(fid)
            return
        frames.append(
            FrameInfo(
                frame_id=fid,
                # parentId 缺失时用遍历父帧兜底（老内核/裁剪构建会省这个字段）
                parent_id=str(frame.get("parentId") or "") or parent,
                name=str(frame.get("name") or ""),
                url=str(frame.get("url") or ""),
                path=path,
                depth=depth,
            )
        )
        children = item.get("childFrames") or ()
        if depth + 1 > max_depth:
            clipped.extend(
                str((c.get("frame") or {}).get("id") or "") for c in children
            )
            return
        for index, child in enumerate(children):
            if not isinstance(child, Mapping):
                continue
            walk(
                child,
                path + (frame_label(child.get("frame") or {}, index=index),),
                depth + 1,
                fid,
            )

    walk(root, (), 0)
    return FrameTreeScan(frames=tuple(frames), clipped=tuple(clipped))


# ---------------------------------------------------------------------------
# CDP 会话与取树（可注入 fake session；超时/异常给明确降级）
# ---------------------------------------------------------------------------


def default_cdp_factory(page: Any) -> Awaitable[Any]:
    """默认 CDP 会话获取方式（规格 §2.2 唯一合法路径）。

    ``page.context.new_cdp_session(page)`` —— **每个 Page 一条**，由
    :class:`CdpSessionPool` 缓存，``page`` 关闭时丢弃。**不使用**已废弃的
    ``page.accessibility``（Playwright 1.61.0 已没有该属性）。
    """
    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "new_cdp_session"):
        raise AttributeError("page.context.new_cdp_session 不可用")
    return context.new_cdp_session(page)


@dataclass(frozen=True)
class CdpCall:
    """一次 CDP 调用的结果（降级信息与数据同结构，避免"空树=正常"的歧义）。"""

    ok: bool
    data: Any = None
    code: str = DEGRADE_OK
    reason: str = ""
    elapsed_s: float = 0.0

    @property
    def degraded(self) -> bool:
        return not self.ok


async def cdp_send(
    cdp: Any,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout_s: float = DEFAULT_AX_TIMEOUT_S,
) -> CdpCall:
    """带超时与降级的 ``cdp.send`` 包装（**永不抛异常**）。

    超时 ⇒ ``code=timeout``；会话坏/协议错 ⇒ ``code=error``（reason 带原文首行）。
    """
    started = time.monotonic()
    try:
        raw = cdp.send(method, dict(params or {}))
        data = await asyncio.wait_for(raw, timeout=float(timeout_s))
    except asyncio.TimeoutError:
        return CdpCall(
            ok=False,
            code=DEGRADE_TIMEOUT,
            reason=f"{method} 超时（>{float(timeout_s):.1f}s）",
            elapsed_s=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 — 任何协议/会话错误都降级
        return CdpCall(
            ok=False,
            code=DEGRADE_ERROR,
            reason=f"{method} 失败: {str(exc).splitlines()[0][:200]}",
            elapsed_s=time.monotonic() - started,
        )
    return CdpCall(ok=True, data=data, elapsed_s=time.monotonic() - started)


class CdpSessionPool:
    """按 ``page`` 缓存 CDP 会话并持有 ``Accessibility.enable`` 的幂等状态。

    生命周期（规格 §2.2）：每个 Page 一条会话；``page`` 关闭 / 浏览器重建后丢弃
    （:meth:`drop` / :meth:`clear`）。缓存用 ``id(page)`` 做键并同时保存弱引用
    以便页面被 GC 后自愈 —— 这里刻意用 ``dict`` + 显式清理，不引入弱引用复杂度。
    """

    def __init__(
        self,
        factory: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._factory = factory or default_cdp_factory
        self._sessions: dict[int, Any] = {}
        self._pages: dict[int, Any] = {}
        self._ax_enabled: set[int] = set()

    # -- 会话 -------------------------------------------------------------

    async def session_for(
        self, page: Any, *, timeout_s: float = DEFAULT_AX_TIMEOUT_S
    ) -> CdpCall:
        """取得（或创建）该 page 的 CDP 会话；失败返回降级结果。"""
        if page is None:
            return CdpCall(ok=False, code=DEGRADE_NO_PAGE, reason="page 为 None")
        key = id(page)
        cached = self._sessions.get(key)
        if cached is not None:
            return CdpCall(ok=True, data=cached)
        try:
            created = self._factory(page)
            session = await asyncio.wait_for(created, timeout=float(timeout_s))
        except asyncio.TimeoutError:
            return CdpCall(
                ok=False,
                code=DEGRADE_TIMEOUT,
                reason=f"new_cdp_session 超时（>{float(timeout_s):.1f}s）",
            )
        except Exception as exc:  # noqa: BLE001
            return CdpCall(
                ok=False,
                code=DEGRADE_UNAVAILABLE,
                reason=f"new_cdp_session 失败: {str(exc).splitlines()[0][:200]}",
            )
        self._sessions[key] = session
        self._pages[key] = page
        return CdpCall(ok=True, data=session)

    async def enable_accessibility(
        self, page: Any, *, timeout_s: float = DEFAULT_AX_TIMEOUT_S
    ) -> CdpCall:
        """``Accessibility.enable``（每会话**一次**，幂等；失败即降级）。"""
        got = await self.session_for(page, timeout_s=timeout_s)
        if not got.ok:
            return got
        key = id(page)
        if key in self._ax_enabled:
            return CdpCall(ok=True, data=None)
        call = await cdp_send(
            got.data, "Accessibility.enable", timeout_s=timeout_s
        )
        if call.ok:
            self._ax_enabled.add(key)
        return call

    def drop(self, page: Any) -> None:
        """丢弃某页的会话（``page.on("close")`` 时调用；幂等）。"""
        key = id(page)
        self._sessions.pop(key, None)
        self._pages.pop(key, None)
        self._ax_enabled.discard(key)

    def clear(self) -> None:
        """丢弃全部会话（浏览器崩溃重建后调用）。"""
        self._sessions.clear()
        self._pages.clear()
        self._ax_enabled.clear()

    def watch_close(self, page: Any) -> bool:
        """尽力注册 ``page.on("close")`` 自动丢弃（无该 API 的环境返回 False）。"""
        on = getattr(page, "on", None)
        if not callable(on):
            return False
        try:
            on("close", lambda *_: self.drop(page))
        except Exception:  # noqa: BLE001 — 事件注册失败不影响静态可见性
            return False
        return True


@dataclass(frozen=True)
class FrameAx:
    """单个帧的取树结果。"""

    frame: FrameInfo
    ok: bool
    nodes: tuple[Mapping[str, Any], ...] = ()
    code: str = DEGRADE_OK
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True)
class AxCollection:
    """整页取树结果：主帧 + 各层子帧（含降级帧）。"""

    ok: bool
    frames: tuple[FrameAx, ...] = ()
    code: str = DEGRADE_OK
    reason: str = ""
    elapsed_s: float = 0.0
    #: 集合级降级说明（**帧树不可用**这类"整体仍可用但信息不全"的事实）。
    extra_notes: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return not self.ok or bool(self.extra_notes) or any(not f.ok for f in self.frames)

    @property
    def notes(self) -> tuple[str, ...]:
        """给人读的降级说明（写进观测块 / 日志）。"""
        items: list[str] = []
        if not self.ok and self.reason:
            items.append(self.reason)
        items.extend(self.extra_notes)
        for frame_ax in self.frames:
            if frame_ax.ok:
                continue
            where = "主帧" if frame_ax.frame.is_main else "/".join(frame_ax.frame.path)
            items.append(f"{where} 取树降级（{frame_ax.code}）：{frame_ax.reason}")
        return tuple(items)

    @property
    def raw_total(self) -> int:
        return sum(frame_ax.count for frame_ax in self.frames)

    def iter_nodes(self) -> Iterable[tuple[FrameInfo, Mapping[str, Any]]]:
        """按帧顺序遍历 (帧, 原始节点)。"""
        for frame_ax in self.frames:
            if not frame_ax.ok:
                continue
            for node in frame_ax.nodes:
                yield frame_ax.frame, node


async def collect_ax(
    page: Any,
    *,
    pool: CdpSessionPool | None = None,
    limits: A11yLimits | None = None,
    frame_depth: int | None = None,
) -> AxCollection:
    """取整页 AX 树：``Page.getFrameTree`` → 每帧 ``Accessibility.getFullAXTree``。

    命令序列（单测按此断言）：先 ``Accessibility.enable`` **一次**，然后
    ``Page.getFrameTree``，再对**每个**帧（含主帧）``Accessibility.getFullAXTree``
    并显式带 ``frameId``。

    降级策略（E1–E3）：
    - 会话拿不到 / enable 失败 ⇒ 整页 ``ok=False`` + code（``unavailable``/``timeout``/``error``）；
    - ``Page.getFrameTree`` 失败 ⇒ **仍有主帧可用**：按"未知 frameId"再取一次主树，
      且 ``notes`` 里写明帧树不可用（子帧内容缺失是显式事实，不是静默空）；
    - 某子帧取树失败（OOPIF 未附加 / 已 detach）⇒ 该帧 ``ok=False`` + code，
      其它帧照常，整页 ``ok=True``（内容尽量给全）。
    """
    started = time.monotonic()
    limits = (limits or A11yLimits()).clamped()
    depth_limit = max(1, int(frame_depth if frame_depth is not None else limits.frame_depth))
    timeout_s = float(limits.ax_timeout_s)
    pool = pool or CdpSessionPool()

    enabled = await pool.enable_accessibility(page, timeout_s=timeout_s)
    if not enabled.ok:
        return AxCollection(
            ok=False,
            code=enabled.code,
            reason=enabled.reason,
            elapsed_s=time.monotonic() - started,
        )
    session = (await pool.session_for(page, timeout_s=timeout_s)).data

    extra_notes: tuple[str, ...] = ()
    tree_call = await cdp_send(session, "Page.getFrameTree", timeout_s=timeout_s)
    if tree_call.ok:
        scan = flatten_frame_tree(tree_call.data, max_depth=depth_limit)
    else:
        scan = FrameTreeScan(
            frames=(FrameInfo(frame_id="", path=(), depth=0),),
            code=tree_call.code,
            reason=f"Page.getFrameTree 失败（{tree_call.reason}）：只取主帧",
        )
        extra_notes = (
            f"帧树不可用（{tree_call.code}）：只取了主帧，"
            "iframe 内容可能缺失，必要时用 browse_screenshot 确认",
        )
        logger.warning("A11y 帧树不可用，降级为只取主帧: %s", tree_call.reason)

    if not scan.frames:
        return AxCollection(
            ok=False,
            code=DEGRADE_UNAVAILABLE,
            reason=scan.reason or "帧树为空",
            elapsed_s=time.monotonic() - started,
        )

    frames: list[FrameAx] = []
    for frame in scan.frames:
        params: dict[str, Any] = {}
        if frame.frame_id:
            params["frameId"] = frame.frame_id  # 主帧也显式传（规格 §2.2）
        call = await cdp_send(
            session, "Accessibility.getFullAXTree", params, timeout_s=timeout_s
        )
        if not call.ok:
            frames.append(
                FrameAx(
                    frame=replace(frame, reachable=False, reason=call.reason),
                    ok=False,
                    code=call.code,
                    reason=call.reason,
                )
            )
            continue
        nodes = (call.data or {}).get("nodes") if isinstance(call.data, Mapping) else None
        frames.append(
            FrameAx(
                frame=frame,
                ok=True,
                nodes=tuple(nodes or ()),
            )
        )

    main_ok = any(frame_ax.ok and frame_ax.frame.is_main for frame_ax in frames)
    collection = AxCollection(
        ok=main_ok,
        frames=tuple(frames),
        code=DEGRADE_OK if main_ok else next(
            (f.code for f in frames if not f.ok), DEGRADE_ERROR
        ),
        reason="" if main_ok else "主帧取树失败",
        elapsed_s=time.monotonic() - started,
        extra_notes=extra_notes,
    )
    if collection.notes:
        logger.debug("A11y 降级: %s", "; ".join(collection.notes))
    return collection


# ---------------------------------------------------------------------------
# 裁剪（纯函数：真实 AX JSON → 节点表）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PruneResult:
    """单帧裁剪结果（纯函数产物，可单测）。"""

    nodes: tuple[A11yNode, ...]
    pruned: int
    raw_total: int
    truncated: bool
    next_ref: int
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def kept(self) -> int:
        return len(self.nodes)


def prune_ax_nodes(
    nodes: Iterable[Mapping[str, Any]],
    *,
    limits: A11yLimits | None = None,
    frame: FrameInfo | None = None,
    boxes: Mapping[int, tuple[float, float, float, float]] | None = None,
    start_ref: int = 1,
) -> PruneResult:
    """把一帧的 CDP AX 节点压成给模型看的节点表（**纯函数**）。

    裁剪规则顺序**即结果**（规格 §2.2 规则 1–9）：

    1. ``ignored=true`` 丢弃（计入 ``pruned``；**其子节点照常遍历** —— AX 的
       ignored 只表示"这个节点本身不可感知"，子节点往往才是内容）；
    2. ``role`` 小写归一，不在 :data:`RENDER_ROLES` 的丢弃（``StaticText`` /
       ``generic`` / ``none`` / ``InlineTextBox`` 不进结构视图）；
    3. 无 name、无 value、无子节点 ⇒ 丢弃（空容器没有信息）；
    4. ``name`` / ``value`` 按 ``max_name_chars`` 截断（``truncated=True``）；
    6. ``box`` 缺失或 <2px ⇒ 保留但 :attr:`A11yNode.actionable` 为 False（D2）；
    7. 预算：按"可交互优先 + 文档顺序"取前 ``max_nodes``，超出标 ``truncated``；
    8. ``textbox`` 且 name 命中敏感词 ⇒ ``value="***"``；
    9. ``checked/expanded/level/disabled/focused`` 从 ``properties`` 取。

    规则 5（相邻同 ``(role, name, checked)`` 合并成一行）是**渲染期**行为，
    放在 :func:`render_node_lines`，因为节点表要保留每个元素自己的 ref 供点击。

    Args:
        nodes: 一帧的 ``Accessibility.getFullAXTree`` 的 ``nodes`` 列表。
        limits: 预算；``None`` 用默认并钳制。
        frame: 该帧的 :class:`FrameInfo`（决定 ``frame_path``/``frame_id``）。
        boxes: ``backendDOMNodeId -> (x, y, w, h)``（主视口 CSS 坐标）。
        start_ref: 第一个 ref 编号（由 :class:`RefAllocator` 提供）。

    Returns:
        :class:`PruneResult`（``raw_total`` = 输入的原始节点数，含 ignored）。
    """
    limits = (limits or A11yLimits()).clamped()
    frame = frame or FrameInfo(frame_id="")
    boxes = boxes or {}

    raw = list(nodes or ())
    stats = {
        "raw": len(raw),
        "ignored": 0,
        "role": 0,
        "empty": 0,
        "budget": 0,
    }
    candidates: list[A11yNode] = []
    depths = _depths_by_node(raw)

    for item in raw:
        if not isinstance(item, Mapping):
            stats["ignored"] += 1
            continue
        if item.get("ignored") is True:
            stats["ignored"] += 1
            continue
        role = ax_text(item.get("role")).strip().lower()
        if not role or role not in RENDER_ROLES:
            stats["role"] += 1
            continue
        name_raw = ax_text(item.get("name"))
        value_raw = ax_text(item.get("value"))
        has_children = bool(item.get("childIds"))
        if not name_raw.strip() and not value_raw.strip() and not has_children:
            stats["empty"] += 1
            continue

        name, name_cut = truncate_text(name_raw, limits.max_name_chars)
        value, value_cut = truncate_text(value_raw, limits.max_name_chars)
        props = ax_property_map(item)
        checked, checked_raw = tri_state(props.get("checked"))
        expanded = None
        if "expanded" in props:
            expanded_flag, _ = tri_state(props.get("expanded"))
            expanded = expanded_flag
        node_id = _as_int(item.get("backendDOMNodeId"), 0) or _as_int(
            item.get("nodeId"), 0
        )
        box = boxes.get(node_id) if node_id else None
        candidates.append(
            A11yNode(
                ref=0,  # 预算裁剪后再统一分配，避免"被丢弃的节点吃掉编号"
                role=role,
                name=name,
                value=_mask_value(role, name, value),
                checked=checked,
                disabled=_truthy(props.get("disabled")),
                focused=_truthy(props.get("focused")),
                expanded=expanded,
                level=_as_int(props.get("level"), 0),
                frame_path=frame.path,
                frame_id=frame.frame_id,
                node_id=node_id,
                box=box,
                checked_raw=checked_raw,
                truncated=name_cut or value_cut,
                depth=depths.get(str(item.get("nodeId") or ""), 0),
            )
        )

    # 规则 7：预算（可交互优先，其余保持文档顺序），然后恢复文档顺序输出。
    truncated = len(candidates) > limits.max_nodes
    if truncated:
        order = sorted(
            range(len(candidates)),
            key=lambda i: (_priority(candidates[i]), i),
        )
        keep = set(order[: limits.max_nodes])
        stats["budget"] = len(candidates) - len(keep)
        kept = [node for i, node in enumerate(candidates) if i in keep]
    else:
        kept = candidates

    first_ref = max(1, int(start_ref))
    refs = tuple(
        replace(node, ref=first_ref + offset) for offset, node in enumerate(kept)
    )
    return PruneResult(
        nodes=refs,
        pruned=len(raw) - len(refs),
        raw_total=len(raw),
        truncated=truncated,
        next_ref=first_ref + len(refs),
        stats=stats,
    )


def _priority(node: A11yNode) -> int:
    """预算裁剪的优先级（0 最好）：可交互 > 有可见面积 > 其它。"""
    if node.actionable:
        return 0
    if node.visible:
        return 1
    return 2


def _depths_by_node(raw: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """按 ``parentId`` 链算 AX 深度（memo 化，环安全）。"""
    parents = {
        str(item.get("nodeId") or ""): str(item.get("parentId") or "")
        for item in raw
        if isinstance(item, Mapping)
    }
    memo: dict[str, int] = {}

    def depth(node_id: str, seen: frozenset[str]) -> int:
        if node_id in memo:
            return memo[node_id]
        if not node_id or node_id in seen:
            return 0
        parent = parents.get(node_id, "")
        value = 0 if not parent else depth(parent, seen | {node_id}) + 1
        memo[node_id] = value
        return value

    for key in parents:
        memo[key] = depth(key, frozenset())
    return memo


# ---------------------------------------------------------------------------
# 观测文本（给模型看的**唯一**结构视图；不含坐标/selector）
# ---------------------------------------------------------------------------


def observation_header(snap: PageSnapshot, *, step: int = 0) -> str:
    """观测块首行（规格 §2.2 逐字格式；verify 负责拼装整块）。"""
    width, height = snap.viewport
    # 规格 §5.4：只有**确实裁掉了节点**时才写「(+N 已裁)」；pruned==0 时该段
    # 一个字节都不出现（否则 `nodes=18(+0 已裁)` 是纯噪声，还会让模型以为漏了东西）。
    pruned_txt = f"(+{snap.pruned} 已裁)" if snap.pruned else ""
    return (
        f"【观测 #{step}】{redact_url(snap.url)} | 标题：{snap.title} | "
        f"{width}x{height}@dpr{_fmt_float(snap.dpr)} | "
        f"y={snap.scroll_y}/{snap.scroll_height} | frames={snap.frames} | "
        f"nodes={len(snap.nodes)}{pruned_txt}"
    )


def render_node_lines(
    nodes: Sequence[A11yNode],
    *,
    show_ref: bool = False,
    max_lines: int = 40,
    collapse_runs: bool = True,
) -> str:
    """把节点表渲染成给模型的行文本（规格 §2.2）。

    - 每行 ``- [role] name`` + 可选后缀 ``value="…"`` / ``(checked=…)`` /
      ``(expanded=…)`` / ``(disabled)`` / ``(h{level})``；
    - **默认不出现 ref 编号**（编号只在 ``browse_observe(view="full")`` 与 SoM
      标签表里出现，避免模型把显示序号当输入 —— §3.4 R-A7 的孪生风险）；
    - **绝不出现**坐标、selector、class、tagName；
    - 相邻同 ``(role, name, checked)`` 合并成 ``×N``；同 role 连续 ≥3 条折叠。
    """
    if not nodes:
        return NO_NODES_HINT

    groups = _merge_identical(nodes)
    lines: list[str] = []
    if collapse_runs:
        for run in _role_runs(groups):
            count = sum(item[1] for item in run)
            role = run[0][0].role
            if len(run) == 1 and count == 1:
                lines.append(_node_line(run[0][0], 1, show_ref))
                continue
            if count >= 3:
                noun = _ROLE_NOUN.get(role, "项")
                lines.append(f"- [{role}] …（共 {count} 个{noun}，已折叠）")
                continue
            lines.extend(_node_line(node, n, show_ref) for node, n in run)
    else:
        lines.extend(_node_line(node, n, show_ref) for node, n in groups)

    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"…（还有 {hidden} 行）"]
    return "\n".join(lines)


def _node_line(node: A11yNode, count: int, show_ref: bool) -> str:
    prefix = f"#{node.ref} " if show_ref else ""
    parts = [f"- {prefix}[{node.role}] {node.name}".rstrip()]
    if node.value:
        parts.append(f' value="{node.value}"')
    if node.checked_raw:
        parts.append(f" (checked={node.checked_text})")
    if node.expanded is not None:
        parts.append(f" (expanded={'true' if node.expanded else 'false'})")
    if node.disabled:
        parts.append(" (disabled)")
    if node.level:
        parts.append(f" (h{node.level})")
    if count > 1:
        parts.append(f" ×{count}")
    return "".join(parts)


def _merge_identical(
    nodes: Sequence[A11yNode],
) -> list[tuple[A11yNode, int]]:
    """相邻同 ``(role, name, checked)`` 合并（规则 5，渲染期）。"""
    merged: list[tuple[A11yNode, int]] = []
    for node in nodes:
        key = (node.role, node.name, node.checked_raw)
        if merged and (merged[-1][0].role, merged[-1][0].name, merged[-1][0].checked_raw) == key:
            prev_node, prev_count = merged[-1]
            merged[-1] = (prev_node, prev_count + 1)
            continue
        merged.append((node, 1))
    return merged


def _role_runs(
    groups: Sequence[tuple[A11yNode, int]],
) -> list[list[tuple[A11yNode, int]]]:
    """按 role 切连续段（供"同 role 连续 ≥3 条折叠"）。"""
    runs: list[list[tuple[A11yNode, int]]] = []
    for item in groups:
        if runs and runs[-1][0][0].role == item[0].role:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def ssrf_notice(snap_or_count: "PageSnapshot | int") -> str:
    """SSRF 拦截提示行（D9-①）；没有拦截返回空串（不制造噪声）。"""
    count = (
        snap_or_count.ssrf_blocked
        if isinstance(snap_or_count, PageSnapshot)
        else int(snap_or_count or 0)
    )
    return SSRF_NOTICE_TEMPLATE.format(n=count) if count > 0 else ""


def observation_notices(snap: PageSnapshot) -> tuple[str, ...]:
    """观测块的**附加行**（页面派生事实；由 ``verify.render_observation`` 拼装）。

    三类（顺序固定）：SSRF 拦截 → 预算截断 → 感知降级。
    只给"该说什么"，不重复首行与节点行 —— 那是
    :func:`observation_header` / :func:`render_node_lines` 的职责。
    """
    lines: list[str] = []
    notice = ssrf_notice(snap)
    if notice:
        lines.append(notice)
    if snap.truncated and snap.pruned:
        lines.append(MORE_NODES_HINT.format(more=snap.pruned))
    if snap.degraded:
        lines.append("⚠ 感知降级：" + "；".join(snap.degraded))
    return tuple(lines)


def render_snapshot(
    snap: PageSnapshot, *, step: int = 0, max_lines: int = 40, show_ref: bool = False
) -> str:
    """结构视图 = 首行 + 节点行 + 附加行（**不含正文**；正文由 read 显式取）。"""
    body = render_node_lines(snap.nodes, show_ref=show_ref, max_lines=max_lines)
    parts = [observation_header(snap, step=step), body]
    parts.extend(observation_notices(snap))
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# 组装快照
# ---------------------------------------------------------------------------


async def snapshot(
    page: Any,
    *,
    limits: A11yLimits | None = None,
    pool: CdpSessionPool | None = None,
    allocator: RefAllocator | None = None,
    boxes: Mapping[int, tuple[float, float, float, float]] | None = None,
    text_fn: Callable[[Any], Awaitable[str]] | None = None,
    max_text_chars: int = 4000,
    ssrf_blocked: int = 0,
    viewport: tuple[int, int] = (1280, 800),
    dpr: float = 1.0,
    frame_depth: int | None = None,
) -> PageSnapshot:
    """取一次完整页面快照（感知层唯一入口，规格 §2.1 ``a11y.snapshot``）。

    所有慢操作（CDP 取树、正文抽取）都带降级：感知坏掉时返回
    ``nodes=()`` **且** ``degraded`` 非空，观测块据此提示模型走 SoM 或直接给结论。

    Args:
        page: Playwright Page（鸭子类型；单测用 fake）。
        limits: 感知预算（``A11yLimits.from_config`` 的产物）。
        pool: CDP 会话池（跨调用复用；integrator 持有一个）。
        allocator: ref 分配器（**跨快照不复用**；integrator 按任务持有一个）。
        boxes: ``backendDOMNodeId -> (x,y,w,h)``；缺省时 ``box=None`` ⇒ 无 SoM 候选
            （真机由 executor 的 locator 层提供，事实 F5 已实测坐标可用）。
        text_fn: 异步正文抽取器（``core/extract`` 的适配器）；``None`` 不取正文。
        max_text_chars: 正文预算（``resources.max_text_chars``）。
        ssrf_blocked: 本页被 SSRF 兜底拦截的请求数（``BrowserCore`` 提供计数）。
        viewport / dpr: 视口与像素比（渲染首行与 SoM 图尺寸用）。
        frame_depth: 帧链深度上限覆盖值。
    """
    started = time.monotonic()
    limits = (limits or A11yLimits()).clamped()
    pool = pool or CdpSessionPool()
    allocator = allocator or RefAllocator()
    boxes = boxes or {}

    collection = await collect_ax(page, pool=pool, limits=limits, frame_depth=frame_depth)

    nodes: list[A11yNode] = []
    pruned = 0
    raw_total = 0
    truncated = False
    frames_count = 0
    for frame_ax in collection.frames:
        frames_count += 1
        if not frame_ax.ok:
            continue
        result = prune_ax_nodes(
            frame_ax.nodes,
            limits=limits,
            frame=frame_ax.frame,
            boxes=boxes,
            start_ref=allocator.next_ref,
        )
        # 单帧预算用在本帧；跨帧总量再由 _apply_total_budget 统一收口
        allocator.allocate(len(result.nodes))
        nodes.extend(result.nodes)
        pruned += result.pruned
        raw_total += result.raw_total
        truncated = truncated or result.truncated

    nodes, extra_pruned, total_cut = _apply_total_budget(nodes, limits.max_nodes)
    pruned += extra_pruned
    truncated = truncated or total_cut

    text = ""
    text_truncated = False
    if text_fn is not None:
        try:
            raw_text = await text_fn(page)
        except Exception as exc:  # noqa: BLE001 — 正文失败不影响结构视图
            logger.debug("正文抽取失败（忽略）: %s", exc)
            raw_text = ""
        text, text_truncated = truncate_text(raw_text or "", max_text_chars)

    scroll_y, scroll_height = await _read_scroll(page, viewport[1])

    return PageSnapshot(
        url=str(getattr(page, "url", "") or ""),
        title=await _read_title(page),
        viewport=(int(viewport[0]), int(viewport[1])),
        dpr=float(dpr or 1.0),
        scroll_y=scroll_y,
        scroll_height=scroll_height,
        nodes=tuple(nodes),
        pruned=pruned,
        frames=frames_count,
        text=text,
        text_truncated=text_truncated,
        ssrf_blocked=int(ssrf_blocked or 0),
        taken_at=started,
        truncated=truncated,
        raw_total=raw_total,
        degraded=collection.notes,
    )


def _apply_total_budget(
    nodes: Sequence[A11yNode], max_nodes: int
) -> tuple[list[A11yNode], int, bool]:
    """跨帧总量预算：可交互优先，其余保持文档顺序。"""
    if len(nodes) <= max_nodes:
        return list(nodes), 0, False
    order = sorted(range(len(nodes)), key=lambda i: (_priority(nodes[i]), i))
    keep = set(order[:max_nodes])
    kept = [node for i, node in enumerate(nodes) if i in keep]
    return kept, len(nodes) - len(kept), True


async def _read_title(page: Any) -> str:
    getter = getattr(page, "title", None)
    if not callable(getter):
        return ""
    try:
        return str(await getter() or "")
    except Exception:  # noqa: BLE001
        return ""


async def _read_scroll(page: Any, viewport_h: int) -> tuple[int, int]:
    """读滚动位置（**单参 evaluate**，F1：只允许 1 个位置实参）。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return 0, int(viewport_h)
    try:
        data = await evaluate(
            "() => ({y: Math.round(window.scrollY || 0),"
            " h: Math.round(Math.max(document.documentElement ?"
            " document.documentElement.scrollHeight : 0,"
            " document.body ? document.body.scrollHeight : 0))})"
        )
    except Exception:  # noqa: BLE001
        return 0, int(viewport_h)
    if not isinstance(data, Mapping):
        return 0, int(viewport_h)
    return _as_int(data.get("y"), 0), _as_int(data.get("h"), int(viewport_h))


# ---------------------------------------------------------------------------
# 懒加载滚动（参数化：次数 / 间隔 / 到底判定；滚动后重取树并合并）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LazyScrollPlan:
    """受限自动滚动的参数（规格 §2.3：最多 3 屏、每屏 150ms、累计 2s 上限）。"""

    rounds: int = 3
    interval_ms: int = 150
    budget_ms: int = 2000
    step_px: int = 0  # 0 = 一屏（由调用方按 viewport 高度决定）

    def clamped(self) -> "LazyScrollPlan":
        return LazyScrollPlan(
            rounds=max(0, min(10, int(self.rounds))),
            interval_ms=max(0, min(2000, int(self.interval_ms))),
            budget_ms=max(0, min(30000, int(self.budget_ms))),
            step_px=max(0, min(20000, int(self.step_px))),
        )


@dataclass(frozen=True)
class ScrollState:
    """一次滚动读数（到底判定的输入）。"""

    y: int = 0
    height: int = 0
    viewport_h: int = 800

    @property
    def at_bottom(self) -> bool:
        """到底判定：``y + viewport >= height - 2``（子像素留 2px 容差）。"""
        return self.y + self.viewport_h >= self.height - 2


@dataclass(frozen=True)
class LazyScrollResult:
    """滚动结果（``rounds`` = 实际滚动次数）。"""

    rounds: int
    scrolled_px: int
    at_bottom: bool
    states: tuple[ScrollState, ...] = ()
    reason: str = ""


def scroll_state(payload: Any, *, viewport_h: int = 800) -> ScrollState:
    """把页面读数归一成 :class:`ScrollState`（纯函数，容忍缺字段/非数字）。"""
    if not isinstance(payload, Mapping):
        return ScrollState(viewport_h=int(viewport_h))
    return ScrollState(
        y=_as_int(payload.get("y"), 0),
        height=_as_int(payload.get("h", payload.get("height")), int(viewport_h)),
        viewport_h=int(viewport_h),
    )


async def lazy_scroll(
    page: Any,
    plan: LazyScrollPlan | None = None,
    *,
    viewport_h: int = 800,
    evaluate: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> LazyScrollResult:
    """受限自动滚动（**每次最多 N 轮 + 墙钟上限 + 到底即停**）。

    只在 ``read(text, amount=full)`` / ``observe(scope="text")`` 这类**读**路径
    使用：观测本身**不**自动滚动（滚动有副作用，会把页面带到别处）。
    """
    plan = (plan or LazyScrollPlan()).clamped()
    evaluate = evaluate or getattr(page, "evaluate", None)
    sleeper = sleep or asyncio.sleep
    step = int(plan.step_px or viewport_h or 800)
    started = time.monotonic()
    scrolled = 0
    states: list[ScrollState] = []
    reason = "plan"

    if plan.rounds <= 0:
        return LazyScrollResult(rounds=0, scrolled_px=0, at_bottom=False, reason="rounds=0")
    if not callable(evaluate):
        return LazyScrollResult(
            rounds=0, scrolled_px=0, at_bottom=False, reason="page.evaluate 不可用"
        )

    rounds = 0
    for _ in range(plan.rounds):
        if (time.monotonic() - started) * 1000 >= plan.budget_ms:
            reason = "budget"
            break
        try:
            await evaluate(
                "(dy) => window.scrollBy(0, dy)", step
            )  # 单参 evaluate（F1）
        except Exception as exc:  # noqa: BLE001
            reason = f"evaluate 失败: {str(exc).splitlines()[0][:120]}"
            break
        rounds += 1
        scrolled += step
        if plan.interval_ms:
            await sleeper(plan.interval_ms / 1000.0)
        state = await _read_scroll_state(evaluate, viewport_h=viewport_h)
        states.append(state)
        if state.at_bottom:
            reason = "bottom"
            break

    at_bottom = bool(states and states[-1].at_bottom)
    return LazyScrollResult(
        rounds=rounds,
        scrolled_px=scrolled,
        at_bottom=at_bottom,
        states=tuple(states),
        reason=reason,
    )


async def _read_scroll_state(
    evaluate: Callable[..., Awaitable[Any]], *, viewport_h: int
) -> ScrollState:
    try:
        payload = await evaluate(
            "() => ({y: Math.round(window.scrollY || 0),"
            " h: Math.round(Math.max(document.documentElement ?"
            " document.documentElement.scrollHeight : 0,"
            " document.body ? document.body.scrollHeight : 0))})"
        )
    except Exception:  # noqa: BLE001
        return ScrollState(viewport_h=viewport_h)
    return scroll_state(payload, viewport_h=viewport_h)


def merge_nodes(
    base: Sequence[A11yNode], extra: Sequence[A11yNode], *, start_ref: int | None = None
) -> tuple[A11yNode, ...]:
    """多轮滚动后的节点合并（按 :meth:`A11yNode.key` 去重，保序）。

    懒加载会在滚动后**重复**产出已经出现过的节点：合并而不是叠加，
    否则预算被同一批元素吃掉两遍。
    """
    seen = {node.key() for node in base}
    merged = list(base)
    next_ref = max(1, int(start_ref if start_ref is not None else (_max_ref(base) + 1)))
    for node in extra:
        if node.key() in seen:
            continue
        seen.add(node.key())
        merged.append(replace(node, ref=next_ref))
        next_ref += 1
    return tuple(merged)


def _max_ref(nodes: Sequence[A11yNode]) -> int:
    return max((node.ref for node in nodes), default=0)


async def lazy_observe(
    page: Any,
    *,
    plan: LazyScrollPlan | None = None,
    limits: A11yLimits | None = None,
    snapshot_fn: Callable[..., Awaitable[PageSnapshot]] | None = None,
    viewport: tuple[int, int] | None = None,
    viewport_h: int = 800,
    evaluate: Callable[..., Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    **snapshot_kwargs: Any,
) -> tuple[PageSnapshot, LazyScrollResult]:
    """先取一次基线快照，再受限滚动并逐轮重取，最后合并节点。

    Returns:
        ``(合并后的 PageSnapshot, 滚动结果)``。``snapshot_fn`` 可注入（单测无需浏览器）。
    """
    limits = (limits or A11yLimits()).clamped()
    taker = snapshot_fn or snapshot
    viewport = viewport or (1280, viewport_h)
    base = await taker(page, limits=limits, viewport=viewport, **snapshot_kwargs)
    scroll = await lazy_scroll(
        page, plan, viewport_h=viewport_h, evaluate=evaluate, sleep=sleep
    )
    merged_nodes = base.nodes
    rounds_used = 0
    for _ in range(scroll.rounds):
        rounds_used += 1
        current = await taker(
            page, limits=limits, viewport=viewport, **snapshot_kwargs
        )
        merged_nodes = merge_nodes(merged_nodes, current.nodes)
    notes = base.degraded
    if rounds_used:
        notes = notes + (f"（已滚动 {rounds_used} 屏后）",)
    return (
        replace(
            base,
            nodes=merged_nodes,
            scroll_y=scroll.states[-1].y if scroll.states else base.scroll_y,
            scroll_height=scroll.states[-1].height
            if scroll.states
            else base.scroll_height,
            degraded=notes,
        ),
        scroll,
    )


# ---------------------------------------------------------------------------
# Shadow DOM 穿透（CDP DOM.getDocument pierce；open shadow 由 AX 已穿透）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowNode:
    """穿透文档里的一个节点（含 shadow 深度与宿主后端 id）。"""

    backend_id: int
    node_name: str
    node_type: int
    shadow_depth: int
    parent_id: int = 0
    is_shadow_root: bool = False


@dataclass(frozen=True)
class ShadowScan:
    """穿透结果（``closed_shadow`` 计数用于解释"AX 有、locator 无"）。"""

    ok: bool
    nodes: tuple[ShadowNode, ...] = ()
    shadow_roots: int = 0
    code: str = DEGRADE_OK
    reason: str = ""

    @property
    def degraded(self) -> bool:
        return not self.ok

    def in_shadow(self) -> tuple[ShadowNode, ...]:
        """落在 shadow 树里的节点（``shadow_depth > 0``）。"""
        return tuple(node for node in self.nodes if node.shadow_depth > 0)


def flatten_pierced_document(
    root: Mapping[str, Any] | None, *, max_nodes: int = 4000
) -> ShadowScan:
    """把 ``DOM.getDocument(pierce=True)`` 的树展平（**纯函数**）。

    为什么需要它：AX 树已经穿透 open shadow（事实 F4），但
    **closed shadow** 的节点在 AX 里能看见、Playwright locator 却匹配不到
    （只能 SoM + 坐标）。穿透文档让插件能解释"为什么 locator 0 匹配"，
    也能在需要时用 ``DOM.getBoxModel`` 自取 closed shadow 的框。
    """
    if not isinstance(root, Mapping):
        return ShadowScan(ok=False, code=DEGRADE_UNAVAILABLE, reason="DOM 文档为空")

    out: list[ShadowNode] = []
    roots = 0

    def walk(node: Mapping[str, Any], shadow_depth: int, parent_id: int) -> None:
        nonlocal roots
        if len(out) >= max_nodes:
            return
        backend_id = _as_int(node.get("backendNodeId"), 0)
        out.append(
            ShadowNode(
                backend_id=backend_id,
                node_name=str(node.get("nodeName") or ""),
                node_type=_as_int(node.get("nodeType"), 0),
                shadow_depth=shadow_depth,
                parent_id=parent_id,
            )
        )
        for index, child in enumerate(node.get("children") or ()):
            if isinstance(child, Mapping):
                walk(child, shadow_depth, backend_id or index)
        for shadow in node.get("shadowRoots") or ():
            if not isinstance(shadow, Mapping):
                continue
            roots += 1
            if len(out) >= max_nodes:
                return
            out.append(
                ShadowNode(
                    backend_id=_as_int(shadow.get("backendNodeId"), 0),
                    node_name=str(shadow.get("nodeName") or "#shadow-root"),
                    node_type=_as_int(shadow.get("nodeType"), 0),
                    shadow_depth=shadow_depth + 1,
                    parent_id=backend_id,
                    is_shadow_root=True,
                )
            )
            for child in shadow.get("children") or ():
                if isinstance(child, Mapping):
                    walk(child, shadow_depth + 1, _as_int(shadow.get("backendNodeId"), 0))

    walk(root, 0, 0)
    return ShadowScan(ok=True, nodes=tuple(out), shadow_roots=roots)


async def pierce_shadow(
    page: Any,
    *,
    pool: CdpSessionPool | None = None,
    limits: A11yLimits | None = None,
    max_nodes: int = 4000,
    depth: int = -1,
) -> ShadowScan:
    """用 CDP ``DOM.getDocument(pierce=True)`` 穿透 shadow（**不用**浅层 querySelectorAll）。

    失败时先试 ``DOMSnapshot.captureSnapshot``（老内核/裁剪构建），仍失败则返回
    带 code 的降级结果。
    """
    limits = (limits or A11yLimits()).clamped()
    timeout_s = float(limits.ax_timeout_s)
    pool = pool or CdpSessionPool()
    got = await pool.session_for(page, timeout_s=timeout_s)
    if not got.ok:
        return ShadowScan(ok=False, code=got.code, reason=got.reason)
    session = got.data
    enabled = await cdp_send(session, "DOM.enable", timeout_s=timeout_s)
    if not enabled.ok:
        return ShadowScan(
            ok=False,
            code=enabled.code,
            reason=f"DOM.enable 失败: {enabled.reason}",
        )
    call = await cdp_send(
        session, "DOM.getDocument", {"depth": int(depth), "pierce": True}, timeout_s=timeout_s
    )
    if call.ok and isinstance(call.data, Mapping):
        scan = flatten_pierced_document(call.data.get("root"), max_nodes=max_nodes)
        if scan.ok:
            return scan
    # 兜底：DOMSnapshot（返回扁平文档串，不保证 shadow 边界，仅用于统计）
    snap_call = await cdp_send(
        session,
        "DOMSnapshot.captureSnapshot",
        {"computedStyles": [], "includeDOMRects": False, "includePaintOrder": False},
        timeout_s=timeout_s,
    )
    if snap_call.ok and isinstance(snap_call.data, Mapping):
        docs = snap_call.data.get("documents") or ()
        total = 0
        for doc in docs:
            if isinstance(doc, Mapping):
                total += len(doc.get("nodes", {}).get("backendNodeId", ()) or ())
        return ShadowScan(
            ok=True,
            nodes=(),
            code=DEGRADE_OK,
            reason=f"DOM.getDocument 不可用，已用 DOMSnapshot 统计（{total} 节点，无 shadow 边界）",
        )
    return ShadowScan(
        ok=False,
        code=call.code if not call.ok else DEGRADE_UNAVAILABLE,
        reason=f"DOM.getDocument 穿透失败: {call.reason or '返回结构异常'}",
    )


# ---------------------------------------------------------------------------
# Set-of-Mark 截图兜底（唯一合法的"看图"通道）
# ---------------------------------------------------------------------------

#: 叠加层 JS（**单参 evaluate**，规格 §2.4 逐字；不许污染 AX 树）。
SOM_DRAW_JS = """
(marks) => {
  const ROOT_ID = '__blm_som__';
  const old = document.getElementById(ROOT_ID);
  if (old) old.remove();
  const root = document.createElement('div');
  root.id = ROOT_ID;
  root.setAttribute('aria-hidden', 'true');
  root.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none';
  for (const m of marks) {
    const box = document.createElement('div');
    box.style.cssText = `position:fixed;left:${m.x}px;top:${m.y}px;width:${m.w}px;height:${m.h}px;` +
      'border:2px solid #ff2d55;border-radius:3px;pointer-events:none';
    const tag = document.createElement('div');
    tag.textContent = String(m.id);
    tag.style.cssText = 'position:absolute;left:0;top:-16px;background:#ff2d55;color:#fff;' +
      'font:bold 12px/16px "Noto Sans CJK SC",sans-serif;padding:0 4px;border-radius:2px';
    box.appendChild(tag);
    root.appendChild(box);
  }
  document.documentElement.appendChild(root);
  return marks.length;
}
"""

#: 摘除叠加层（**零参**；必须在 ``finally`` 里调，否则红框污染后续截图与编号）。
SOM_CLEAR_JS = "() => { const el = document.getElementById('__blm_som__'); if (el) el.remove(); return true; }"


@dataclass(frozen=True)
class Mark:
    """一个 SoM 标签（编号 + 元素身份 + 框）。模型**只回编号**，不回坐标。"""

    id: int
    ref: int
    role: str
    name: str
    box: tuple[float, float, float, float]
    frame_path: tuple[str, ...] = ()

    @property
    def center(self) -> tuple[float, float]:
        """框中心（插件内部用：只能来自插件自己画的框，禁止从截图反推）。"""
        x, y, w, h = self.box
        return (x + w / 2.0, y + h / 2.0)


@dataclass(frozen=True)
class SoMResult:
    """一次 SoM 截图产物：图 + 标签表 + 编号→节点映射。"""

    png: bytes
    marks: tuple[Mark, ...]
    width: int
    height: int
    dpr: float = 1.0
    truncated: bool = False
    overlay_error: str = ""
    targets: tuple[tuple[int, A11yNode], ...] = ()

    @property
    def labels(self) -> int:
        return len(self.marks)

    def resolve_mark(self, mark_id: int) -> Mark | None:
        """按编号取 :class:`Mark`（含 box）；编号不属于本图返回 ``None``。"""
        for mark in self.marks:
            if mark.id == int(mark_id):
                return mark
        return None

    def target(self, mark_id: int) -> A11yNode | None:
        """按编号取原始 :class:`A11yNode`（executor 用它反查 locator）。"""
        wanted = int(mark_id)
        for mid, node in self.targets:
            if mid == wanted:
                return node
        return None

    def table(self) -> str:
        """给模型的标签表（规格 §2.4 逐字格式）。"""
        return render_marks_table(self.marks, width=self.width, height=self.height, dpr=self.dpr)


def select_marks(
    nodes: Sequence[A11yNode],
    *,
    max_marks: int = DEFAULT_MAX_MARKS,
    clip: tuple[float, float, float, float] | None = None,
) -> tuple[tuple[Mark, ...], bool]:
    """挑 SoM 候选：``actionable`` 且有 ``box``，按文档顺序取前 ``max_marks``。

    Args:
        nodes: 快照节点表。
        max_marks: 标签上限（``perception.som_max_marks``，已钳制）。
        clip: 元素放大时的裁剪框 ``(x, y, w, h)``；只保留与本框相交的候选。

    Returns:
        ``(marks, truncated)``；``marks`` 编号从 1 起、**唯一**（重叠框也各自一个号）。
    """
    limit = max(0, int(max_marks))
    picked: list[Mark] = []
    for node in nodes:
        if not node.actionable:
            continue
        box = node.box
        if box is None:
            continue
        if clip is not None and not boxes_intersect(box, clip):
            continue
        if len(picked) >= limit:
            break
        picked.append(
            Mark(
                id=len(picked) + 1,
                ref=node.ref,
                role=node.role,
                name=node.name,
                box=box,
                frame_path=node.frame_path,
            )
        )
    eligible = 0
    for node in nodes:
        if not node.actionable or node.box is None:
            continue
        if clip is not None and not boxes_intersect(node.box, clip):
            continue
        eligible += 1
    return tuple(picked), eligible > len(picked)


def boxes_intersect(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """两个 ``(x, y, w, h)`` 是否相交（含边界；w/h<=0 视为不相交）。"""
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return False
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def mark_payload(marks: Sequence[Mark]) -> list[dict[str, float]]:
    """``SOM_DRAW_JS`` 的入参（**只有编号与框的数字**，无任何页面内容）。"""
    return [
        {"id": mark.id, "x": round(mark.box[0], 1), "y": round(mark.box[1], 1),
         "w": round(mark.box[2], 1), "h": round(mark.box[3], 1)}
        for mark in marks
    ]


def render_marks_table(
    marks: Sequence[Mark], *, width: int, height: int, dpr: float = 1.0
) -> str:
    """标签表（规格 §2.4 逐字）：``【SoM】labels=N image=WxH@dprD`` + 编号行。"""
    lines = [
        f"【SoM】labels={len(marks)} image={int(width)}x{int(height)}@dpr{_fmt_float(dpr)}"
    ]
    lines.extend(f"{mark.id}. [{mark.role}] {mark.name}".rstrip() for mark in marks)
    example = marks[1].id if len(marks) > 1 else (marks[0].id if marks else 1)
    tail = (
        '用 {"action":"click","target":{"mark":'
        f"{example}"
        '}} 点击；编号只在本张图有效。'
    )
    lines.append(tail)
    return "\n".join(lines)


async def som_screenshot(
    page: Any = None,
    *,
    nodes: Sequence[A11yNode] | None = None,
    marks: Sequence[Mark] | None = None,
    max_marks: int = DEFAULT_MAX_MARKS,
    clip: tuple[float, float, float, float] | None = None,
    viewport: tuple[int, int] = (1280, 800),
    dpr: float = 1.0,
    evaluate_fn: Callable[..., Awaitable[Any]] | None = None,
    screenshot_fn: Callable[..., Awaitable[Any]] | None = None,
) -> SoMResult:
    """画编号 → 截图 → **必定**摘 overlay → 返回 ``(png, marks)``。

    三条禁止事项（规格 §2.4，测试会查）：① 不用截图像素反推点击点；
    ② 不用第三方图像库画标签（禁止 Pillow）；③ 不把图写进日志或 ``data/``。
    """
    evaluate = evaluate_fn or getattr(page, "evaluate", None)
    shoot = screenshot_fn or getattr(page, "screenshot", None)

    if marks is None:
        selected, truncated = select_marks(
            list(nodes or ()), max_marks=max_marks, clip=clip
        )
    else:
        selected = tuple(marks)
        truncated = False

    overlay_error = ""
    if selected:
        if not callable(evaluate):
            overlay_error = "page.evaluate 不可用（未画标签）"
        else:
            try:
                await evaluate(SOM_DRAW_JS, mark_payload(selected))
            except Exception as exc:  # noqa: BLE001 — 画框失败仍可给原图
                overlay_error = f"画标签失败: {str(exc).splitlines()[0][:120]}"
    png: bytes = b""
    try:
        if callable(shoot):
            kwargs: dict[str, Any] = {"full_page": False}
            if clip is not None:
                kwargs["clip"] = {
                    "x": float(clip[0]),
                    "y": float(clip[1]),
                    "width": float(clip[2]),
                    "height": float(clip[3]),
                }
            shot = await shoot(**kwargs)
            png = bytes(shot or b"")
        else:
            overlay_error = overlay_error or "page.screenshot 不可用"
    finally:
        # 必须：异常路径留下的红框会污染后续所有截图与 SoM 编号。
        if selected and callable(evaluate):
            try:
                await evaluate(SOM_CLEAR_JS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("摘除 SoM overlay 失败: %s", exc)

    by_ref = {node.ref: node for node in (nodes or ())}
    targets = tuple(
        (mark.id, by_ref[mark.ref]) for mark in selected if mark.ref in by_ref
    )
    return SoMResult(
        png=png,
        marks=selected,
        width=int(viewport[0]),
        height=int(viewport[1]),
        dpr=float(dpr or 1.0),
        truncated=truncated,
        overlay_error=overlay_error,
        targets=targets,
    )


class MarkEpoch:
    """SoM 标记纪元（规格 §2.4 的生命周期表）：编号只在本张图 + 有效期内可用。

    作废事件：改变页面的动作（:data:`MUTATING_ACTIONS`）、60 秒超时、
    新一轮 SoM 截图（覆盖）、新任务。**只读**观测不作废（模型要对照表看图）。
    """

    def __init__(
        self,
        *,
        ttl_s: float = SOM_MARK_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_s)
        self._clock = clock
        self._result: SoMResult | None = None
        self._stamp = 0.0
        self._reason = ""

    # -- 状态 -------------------------------------------------------------

    @property
    def active(self) -> bool:
        """当前是否有可解析的 mark。"""
        return bool(self._result is not None and self._result.marks) and not self.expired()

    @property
    def reason(self) -> str:
        """最近一次作废原因（``""`` = 从未作废 / 仍有效）。"""
        return self._reason

    def expired(self) -> bool:
        """是否已过期（只看时间；不看动作作废）。"""
        if self._result is None:
            return True
        return (self._clock() - self._stamp) > self._ttl or self._reason != ""

    # -- 写入 -------------------------------------------------------------

    def adopt(self, result: SoMResult) -> SoMResult:
        """采纳一张新的 SoM 图（旧编号随之失效）。"""
        self._result = result
        self._stamp = self._clock()
        self._reason = ""
        return result

    def invalidate(self, reason: str = "invalidated") -> None:
        """显式作废（动作改变了页面 / 任务结束）。"""
        self._result = None
        self._stamp = 0.0
        self._reason = reason

    def on_action(self, action: str) -> bool:
        """按动作类别维护纪元；返回**本次是否作废了** mark。"""
        name = str(action or "").strip().lower()
        if name in READONLY_ACTIONS:
            return False
        if name in MUTATING_ACTIONS:
            had = self._result is not None
            self.invalidate(f"动作 {name} 改变了页面，mark 编号失效")
            return had
        return False

    # -- 读取 -------------------------------------------------------------

    def resolve(self, mark_id: int) -> A11yNode | None:
        """编号 → 节点；``None`` = 编号过期/不属于本张图（executor 据此拒绝 R-A9）。"""
        if not self.active or self._result is None:
            return None
        return self._result.target(mark_id)

    def mark(self, mark_id: int) -> Mark | None:
        """编号 → :class:`Mark`（含 box）；``None`` = 同上。"""
        if not self.active or self._result is None:
            return None
        return self._result.resolve_mark(mark_id)

    @property
    def result(self) -> SoMResult | None:
        return self._result


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    parsed = _as_int(value, default)
    return max(low, min(high, parsed))


def _truthy(value: Any) -> bool:
    """AX 属性里的布尔（可能是 ``True`` / ``"true"`` / ``"mixed"``）。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "mixed")


def _fmt_float(value: Any) -> str:
    """``1.0 -> "1"``、``1.5 -> "1.5"``（观测块里 dpr 的写法与截图一致）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "1"
    return str(int(number)) if number == int(number) else f"{number:g}"


__all__ = [
    "ACTIONABLE_ROLES",
    "A11yLimits",
    "A11yNode",
    "AxCollection",
    "CdpCall",
    "CdpSessionPool",
    "DEGRADE_ERROR",
    "DEGRADE_FRAME",
    "DEGRADE_NO_PAGE",
    "DEGRADE_OK",
    "DEGRADE_TIMEOUT",
    "DEGRADE_UNAVAILABLE",
    "DEFAULT_AX_TIMEOUT_S",
    "DEFAULT_MAX_MARKS",
    "DEFAULT_MAX_NAME_CHARS",
    "DEFAULT_MAX_NODES",
    "FRAME_DEPTH_LIMIT",
    "FrameAx",
    "FrameInfo",
    "FrameTreeScan",
    "LazyScrollPlan",
    "LazyScrollResult",
    "MORE_NODES_HINT",
    "Mark",
    "MarkEpoch",
    "MUTATING_ACTIONS",
    "NO_NODES_HINT",
    "PageSnapshot",
    "PruneResult",
    "READONLY_ACTIONS",
    "RENDER_ROLES",
    "RefAllocator",
    "SOM_CLEAR_JS",
    "SOM_DRAW_JS",
    "SOM_MARK_TTL_S",
    "SOM_OVERLAY_ID",
    "ScrollState",
    "ShadowNode",
    "ShadowScan",
    "SoMResult",
    "ax_property_map",
    "ax_text",
    "boxes_intersect",
    "cdp_send",
    "collect_ax",
    "default_cdp_factory",
    "flatten_frame_tree",
    "flatten_pierced_document",
    "frame_label",
    "lazy_observe",
    "lazy_scroll",
    "mark_payload",
    "merge_nodes",
    "observation_header",
    "pierce_shadow",
    "prune_ax_nodes",
    "redact_url",
    "render_marks_table",
    "observation_notices",
    "render_node_lines",
    "render_snapshot",
    "ssrf_notice",
    "scroll_state",
    "select_marks",
    "snapshot",
    "som_screenshot",
    "tri_state",
    "truncate_text",
]
