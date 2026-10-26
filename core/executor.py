"""core.executor — 执行层（v1.7.5 技术栈 ③④；规格 §4）。

两块职责：

1. **定位阶梯** `resolve()`：`role+name > label > text > testid > css > mark`（顺序即优先级）。
   多命中时**先过滤**（`display:none` / `visibility:hidden` / `opacity:0` /
   `pointer-events:none` / `aria-hidden=true` / 零尺寸）**再取 index**；
   无 index 且歧义 ⇒ 返回候选表（role/name/visible/box，Top N）**不点**；
   0 命中 ⇒ 给相近 label/name top-5。**禁止盲 `.nth()`**：`.nth(i)` 只允许出现在
   探针里，`i` 来自 `count()` 的遍历，**不是**模型给的 index。
2. **受信任事件** `trusted_*()`：`scroll_into_view_if_needed` → `bounding_box()` 中心 →
   `Input.dispatchMouseEvent(mouseMoved/mousePressed/mouseReleased)`；
   键盘 `Input.dispatchKeyEvent(rawKeyDown/char/keyUp)` + `Input.insertText`。
   CDP 不可用、无 box、坐标越界、发送异常 ⇒ 回退 `locator.click()` / `locator.fill()`
   并标 `trusted=False` + 可读 `reason`。**禁止**只用 `element.click()`（不可信事件、
   `isTrusted=false`、绕过命中测试 —— 「点了没反应」的一半来源）。

层纪律（§1.2 铁律 1/2）：
- **不 launch 浏览器、不自己取 page**：`page` / `locator` / CDP session 全部注入；
- 模块顶层**不 import playwright**（鸭子类型），没有浏览器也能 import；
- **不缓存 page 引用**（会话回收后失效）；CDP session 按 `id(page)` 缓存，可 `drop_cdp()` 清；
- 所有 await 点**不吞 `CancelledError`**（先 `except asyncio.CancelledError: raise`）；
- CDP 的 `x/y` 是**主视口 CSS 像素**（`png_px = css_px * dpr`），像素坐标**只在内部使用**，
  不进模型输出契约。

**规模说明**（对规格 §1.3 守则 450–650 行的偏离，实现后实测 ~1890 行）：16 个动作各自的
失败面（E2/E3/E4/E6 四类文案）、受信任事件的**每条回退理由**、以及定位阶梯的候选/建议
构造都在这里；这些正是 D2–D7 的正面修复点，压缩它们等于把可读原因又变回一句「失败了」。
"""

from __future__ import annotations

import asyncio
import difflib
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .action_model import Action, ActionKind, Target
from .verify import read_control_state

__all__ = [
    "Candidate",
    "ExecutionResult",
    "InputReport",
    "Resolution",
    "apply",
    "drop_cdp",
    "ensure_cdp",
    "hover_at",
    "press_key",
    "resolve",
    "scroll_at",
    "trusted_click",
    "trusted_focus_and_type",
]

#: 一次 resolve 最多探几个元素（防止 300 个 link 的页面把一步拖成 10s）。
MAX_PROBE = 60
#: 候选表最多列几条（规格 §3.4 R-A8 / §4.1）。
MAX_CANDIDATES = 5
#: 悬停后等待菜单出现的毫秒数（规格 §4.2）。
HOVER_SETTLE_MS = 120

#: 探针脚本（**单参 evaluate**，F1；不传第二个实参）。
_PROBE_JS = """
el => {
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const attr = (n) => (el.getAttribute ? (el.getAttribute(n) || '') : '');
  const box = (r && r.width > 0 && r.height > 0)
    ? [r.left, r.top, r.width, r.height] : null;
  const name = attr('aria-label') || attr('title') ||
    ((el.innerText || el.value || '') + '').trim().slice(0, 200);
  return {
    display: cs ? cs.display : '',
    visibility: cs ? cs.visibility : '',
    opacity: cs ? cs.opacity : '',
    pointer_events: cs ? cs.pointerEvents : '',
    aria_hidden: attr('aria-hidden'),
    disabled: !!(el.disabled || attr('aria-disabled') === 'true'),
    role: (attr('role') || (el.tagName || '')).toLowerCase(),
    name: name,
    box: box,
  };
}
"""

#: 视口尺寸读不到时的兜底（与 resources.viewport 默认值一致）。
_DEFAULT_VIEWPORT = (1280, 800)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """一个**已过滤**的候选元素（给模型看的候选表就按这个渲染）。"""

    role: str = ""
    name: str = ""
    visible: bool = True
    enabled: bool = True
    box: tuple[float, float, float, float] | None = None
    locator_index: int = -1
    hidden_reason: str = ""
    element: Any = None  # playwright Locator（鸭子类型；不进候选表文本）
    node: Any = None  # 对应的 A11yNode（有快照时）

    def label(self) -> str:
        name = self.name or "（无名称）"
        return f"[{self.role or '?'}]「{name}」"

    def render(self, ordinal: int = 0) -> str:
        mark = "①②③④⑤⑥⑦⑧⑨⑩"[ordinal] if 0 <= ordinal < 10 else f"{ordinal + 1}."
        bits = [mark + self.label()]
        bits.append("visible" if self.visible else (self.hidden_reason or "hidden"))
        if not self.enabled:
            bits.append("disabled")
        # R9：候选表进**模型可见**的观测块，按 spec §5.4/§13.2-T3 绝不给像素坐标
        # （否则等于教模型编坐标）。box 仍在 to_payload()/日志/测试里可查。
        return " ".join(bits)

    def to_payload(self) -> dict:
        return {
            "role": self.role,
            "name": self.name,
            "visible": self.visible,
            "enabled": self.enabled,
            "box": list(self.box) if self.box is not None else None,
            "locator_index": self.locator_index,
            "hidden_reason": self.hidden_reason,
        }


@dataclass(frozen=True)
class Resolution:
    """定位结果（成功给 locator；失败给候选 + 可读原因，**结构化、不抛裸异常**）。"""

    ok: bool
    locator: Any | None = None
    node: Any | None = None
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""
    code: str = ""  # "" / E2_NOT_FOUND / E2_AMBIGUOUS / E2_INDEX_OUT_OF_RANGE / E2_MARK_*
    strategy: str = ""
    index: int | None = None
    box: tuple[float, float, float, float] | None = None
    suggestions: tuple[str, ...] = ()
    requires_coordinates: bool = False  # True ⇒ 只能按 box 中心发 CDP 点击（mark 反查失败）

    def render(self) -> str:
        """观测块里的一行（`【未找到】` / `【歧义】` / `【定位失败】`）。"""
        if self.ok:
            return self.reason or "已定位"
        head = "【未找到】" if self.code == "E2_NOT_FOUND" else "【歧义】"
        if self.code in ("E2_INDEX_OUT_OF_RANGE", "E2_MARK_EXPIRED", "E2_MARK_NO_BOX"):
            head = "【未找到】"
        text = f"{head}{self.reason}".rstrip()
        if self.candidates:
            text += "；候选：" + " ".join(
                cand.render(i) for i, cand in enumerate(self.candidates)
            )
        if self.suggestions:
            text += "；相近：" + " ".join(self.suggestions)
        if self.code == "E2_AMBIGUOUS":
            text += "。请给 index（0 起算）或用更精确的 name/text"
        return text

    def candidate_lines(self) -> tuple[str, ...]:
        """候选表文本（0 匹配走 `suggestions`，歧义/越界走 `candidates`）。"""
        if self.candidates:
            return tuple(cand.render(i) for i, cand in enumerate(self.candidates))
        return tuple(self.suggestions)

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {"ok": self.ok, "code": self.code, "strategy": self.strategy}
        if self.reason:
            payload["reason"] = self.reason
        if self.index is not None:
            payload["index"] = self.index
        if self.candidates:
            payload["candidates"] = [cand.to_payload() for cand in self.candidates]
        if self.suggestions:
            payload["suggestions"] = list(self.suggestions)
        return payload


@dataclass(frozen=True)
class InputReport:
    """一次输入事件的落点（受信任 / 回退 + 可读原因 + CDP 方法序列）。"""

    trusted: bool = False
    used: str = "none"  # "cdp" / "locator" / "page" / "none"
    reason: str = ""
    commands: tuple[str, ...] = ()

    def to_payload(self) -> dict:
        return {
            "trusted": self.trusted,
            "used": self.used,
            "reason": self.reason,
            "commands": list(self.commands),
        }


@dataclass(frozen=True)
class ExecutionResult:
    """一步动作的执行结果（**判定留给 `core.verify`**，这里只说「做了什么、成不成」）。"""

    ok: bool
    action: str
    trusted: bool = False
    used: str = "none"
    reason: str = ""
    detail: str = ""
    commands: tuple[str, ...] = ()
    resolution: Resolution | None = None
    box: tuple[float, float, float, float] | None = None
    value: Any = None
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    elapsed_ms: int = 0  # 本步耗时（D3/D7：返回体固定带 elapsed）
    attempt: int = 1  # 本步尝试次数（navigate/reload 超时重试时为 2）

    def render(self) -> str:
        if self.ok:
            bits = [self.detail or "已执行"]
            if self.used:
                bits.append(f"used={self.used}")
            bits.append("trusted=true" if self.trusted else "trusted=false")
            return "；".join(bits)
        return self.reason or "执行失败"

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "trusted": self.trusted,
            "used": self.used,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.detail:
            payload["detail"] = self.detail
        if self.commands:
            payload["commands"] = list(self.commands)
        if self.candidates:
            payload["candidates"] = [cand.to_payload() for cand in self.candidates]
        payload["elapsed_ms"] = self.elapsed_ms
        payload["attempt"] = self.attempt
        return payload


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_box(raw: Any) -> tuple[float, float, float, float] | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        try:
            return (
                float(raw["x"]),
                float(raw["y"]),
                float(raw["width"]),
                float(raw["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (tuple, list)) and len(raw) == 4:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError):
            return None
    return None


def box_center(box: tuple[float, float, float, float] | None) -> tuple[float, float] | None:
    if box is None:
        return None
    x, y, width, height = box
    if width <= 0 or height <= 0:
        return None
    return (x + width / 2.0, y + height / 2.0)


def _viewport(page: Any, snapshot: Any = None) -> tuple[int, int]:
    if snapshot is not None:
        viewport = getattr(snapshot, "viewport", None)
        if viewport:
            try:
                return (int(viewport[0]), int(viewport[1]))
            except (TypeError, ValueError, IndexError):
                pass
    size = getattr(page, "viewport_size", None)
    if isinstance(size, Mapping):
        try:
            return (int(size["width"]), int(size["height"]))
        except (KeyError, TypeError, ValueError):
            pass
    elif isinstance(size, (tuple, list)) and len(size) == 2:
        try:
            return (int(size[0]), int(size[1]))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_VIEWPORT


def _inside_viewport(point: tuple[float, float], viewport: tuple[int, int]) -> bool:
    x, y = point
    width, height = viewport
    return 0 <= x <= width and 0 <= y <= height


def _hidden_reason(info: Mapping[str, Any]) -> str:
    """把 CSS/ARIA 的六种「看起来在、其实点不到」翻译成原因串（D2 的正面修复）。"""
    display = str(info.get("display") or "").strip().lower()
    if display == "none":
        return "display:none"
    visibility = str(info.get("visibility") or "").strip().lower()
    if visibility in ("hidden", "collapse"):
        return "visibility:hidden"
    opacity = str(info.get("opacity") or "").strip()
    if opacity:
        try:
            if float(opacity) == 0.0:
                return "opacity:0"
        except ValueError:
            pass
    pointer_events = str(info.get("pointer_events") or "").strip().lower()
    if pointer_events == "none":
        return "pointer-events:none"
    if str(info.get("aria_hidden") or "").strip().lower() in ("true", "1"):
        return "aria-hidden=true"
    box = _as_box(info.get("box"))
    if box is None or box[2] <= 0 or box[3] <= 0:
        return "zero-size"
    return ""


def _match_name(name: str, needle: str, exact: bool) -> bool:
    if exact:
        return name.strip() == needle.strip()
    return needle.strip().lower() in name.strip().lower()


# ---------------------------------------------------------------------------
# 定位阶梯
# ---------------------------------------------------------------------------


async def _element_info(element: Any) -> Mapping[str, Any]:
    """单元素探针：优先一次 evaluate；退化到 is_visible/bounding_box。"""
    evaluate = getattr(element, "evaluate", None)
    if evaluate is not None:
        try:
            info = await evaluate(_PROBE_JS)
            if isinstance(info, Mapping):
                payload = dict(info)
                payload.setdefault("box", await _bbox(element))
                return payload
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 换成退化路径
            pass
    info = {}
    try:
        info["visible"] = bool(await element.is_visible())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        info["visible"] = False
    info["box"] = await _bbox(element)
    if not info["visible"] and info["box"] is None:
        info["display"] = "none"
    return info


async def _bbox(element: Any) -> tuple[float, float, float, float] | None:
    getter = getattr(element, "bounding_box", None)
    if getter is None:
        return None
    try:
        return _as_box(await getter())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return None


async def _probe(locator: Any, count: int) -> list[Candidate]:
    """把 locator 的匹配元素逐个探成 `Candidate`（**唯一允许 `.nth(i)` 的地方**）。

    `i` 来自 `count()` 的遍历（0..n-1），**不是**模型给的 `action.index` ——
    模型给的 index 作用在「过滤后的候选序列」上（规格 §4.1 的索引口径）。
    """
    out: list[Candidate] = []
    for i in range(min(count, MAX_PROBE)):
        element = locator.nth(i)
        info = await _element_info(element)
        box = _as_box(info.get("box"))
        hidden = _hidden_reason(info)
        out.append(
            Candidate(
                role=str(info.get("role") or ""),
                name=str(info.get("name") or ""),
                visible=not hidden,
                enabled=not bool(info.get("disabled")),
                box=box,
                locator_index=i,
                hidden_reason=hidden,
                element=element,
            )
        )
    return out


def _order_by_ax(
    candidates: Sequence[Candidate], snapshot: Any, target: Target
) -> list[Candidate]:
    """按 AX 顺序重排候选（规格 §4.1：**先按 AX 定候选**，再让 locator 一一对账）。

    快照缺席、或没有任何候选能在 AX 里找到同名节点时，保持 locator 顺序。
    """
    nodes = getattr(snapshot, "nodes", None) or ()
    if not nodes or not candidates:
        return list(candidates)
    needle = target.name or target.text or target.label or ""
    ranks: dict[str, int] = {}
    for rank, node in enumerate(nodes):
        name = str(getattr(node, "name", "") or "")
        if not name:
            continue
        if needle and not _match_name(name, needle, exact=False):
            continue
        ranks.setdefault(name.strip(), rank)
    if not ranks:
        return list(candidates)

    def rank_of(cand: Candidate) -> int:
        return ranks.get(cand.name.strip(), 10**6)

    ordered = sorted(candidates, key=rank_of)
    if all(rank_of(cand) == 10**6 for cand in ordered):
        return list(candidates)
    return ordered


def _suggestions(snapshot: Any, target: Target, *, limit: int = MAX_CANDIDATES) -> tuple[str, ...]:
    """0 命中时给相近 label/name top-5（子串优先，其次 difflib 相似度）。"""
    nodes = getattr(snapshot, "nodes", None) or ()
    if not nodes:
        return ()
    needles = [n for n in (target.name, target.text, target.label, target.testid) if n]
    if not needles:
        return ()
    nodes = list(nodes)
    picked: list[Any] = []
    seen: set[int] = set()
    for needle in needles:
        for node in nodes:
            name = str(getattr(node, "name", "") or "")
            if not name or id(node) in seen:
                continue
            if _match_name(name, needle, exact=False):
                picked.append(node)
                seen.add(id(node))
    if len(picked) < limit:
        names = [str(getattr(node, "name", "") or "") for node in nodes]
        for needle in needles:
            for hit in difflib.get_close_matches(needle, names, n=limit, cutoff=0.3):
                for node in nodes:
                    if id(node) in seen:
                        continue
                    if str(getattr(node, "name", "") or "") == hit:
                        picked.append(node)
                        seen.add(id(node))
                        break
            if len(picked) >= limit:
                break
    out: list[str] = []
    for ordinal, node in enumerate(picked[:limit]):
        role = str(getattr(node, "role", "") or "")
        name = str(getattr(node, "name", "") or "")
        out.append(f"{'①②③④⑤'[ordinal]}[{role}]「{name}」")
    return tuple(out)


def _attempts(target: Target) -> list[tuple[str, bool | None]]:
    """策略链（顺序即优先级）；role/label 各有 exact 二次尝试。"""
    strategy = target.strategy()
    if strategy == "role":
        return [("role", True), ("role", False)]
    if strategy == "label":
        return [("label", True), ("label", False)]
    return [(strategy, None)]


def _make_locator(page: Any, target: Target, strategy: str, exact: bool | None) -> Any | None:
    """把 target 变成 playwright locator（**只调用 Playwright 原生定位 API**）。"""
    if strategy == "role":
        factory = getattr(page, "get_by_role", None)
        if factory is None:
            return None
        kwargs: dict[str, Any] = {"name": target.name}
        if exact is not None:
            kwargs["exact"] = exact
        return factory(target.role, **kwargs)
    if strategy == "label":
        factory = getattr(page, "get_by_label", None)
        if factory is None:
            return None
        kwargs = {}
        if exact is not None:
            kwargs["exact"] = exact
        return factory(target.label, **kwargs)
    if strategy == "text":
        factory = getattr(page, "get_by_text", None)
        if factory is None:
            return None
        return factory(target.text, exact=False)
    if strategy == "testid":
        factory = getattr(page, "get_by_test_id", None)
        if factory is None:
            return None
        return factory(target.testid)
    if strategy == "css":
        factory = getattr(page, "locator", None)
        if factory is None:
            return None
        return factory(target.css)
    return None


async def _ax_lookup(snapshot: Any, role: str, name: str) -> Any | None:
    nodes = getattr(snapshot, "nodes", None) or ()
    for node in nodes:
        if str(getattr(node, "role", "") or "").lower() != role:
            continue
        if str(getattr(node, "name", "") or "") == name:
            return node
    return None


async def resolve(
    page: Any,
    snapshot: Any,
    action: Action,
    *,
    mark_epoch: Mapping[int, Any] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> Resolution:
    """定位阶梯（规格 §4.1）：成功给 locator，失败给候选/建议 + 结构化 `code`。

    `mark` 策略**不用 locator**：`mark_epoch[mark]` → `A11yNode` → 用 `(role,name)` 反查；
    反查不到但有 `box` ⇒ `requires_coordinates=True`（交给 `trusted_click` 按 box 中心点）。
    """
    target = action.target
    if target is None:
        return Resolution(ok=False, code="E2_NOT_FOUND", reason="该动作没有 target")

    try:
        strategy = target.strategy()
    except ValueError as exc:
        return Resolution(ok=False, code="E2_BAD_TARGET", reason=str(exc))

    if strategy == "mark":
        return await _resolve_mark(page, snapshot, target, mark_epoch, max_candidates)

    # 规格 §4.1：role 候选**必须先落在 AX 树上**（模型只点观测里出现过的元素）。
    # 逃生舱：快照缺席、或节点被裁剪（pruned>0）时不做这层强校验，退回 locator 对账。
    if strategy == "role" and snapshot is not None:
        nodes = tuple(getattr(snapshot, "nodes", ()) or ())
        pruned = int(getattr(snapshot, "pruned", 0) or 0)
        if nodes and pruned == 0:
            wanted_role = (target.role or "").lower()
            wanted_name = target.name or ""
            found = any(
                str(getattr(node, "role", "") or "").lower() == wanted_role
                and _match_name(str(getattr(node, "name", "") or ""), wanted_name, exact=False)
                for node in nodes
            )
            if not found:
                return Resolution(
                    ok=False,
                    code="E2_NOT_FOUND",
                    reason=f"AX 树里没有 [{wanted_role}]「{wanted_name}」（观测里没出现过的元素不要点）",
                    strategy="role",
                    suggestions=_suggestions(snapshot, target, limit=max_candidates),
                )

    hidden_seen = 0
    last_count = 0
    for attempt_strategy, exact in _attempts(target):
        locator = _make_locator(page, target, attempt_strategy, exact)
        if locator is None:
            continue
        try:
            count = int(await locator.count())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 定位器本身坏了，换下一条策略
            last_count = 0
            _ = exc
            continue
        if count <= 0:
            last_count = 0
            continue
        last_count = count
        candidates = await _probe(locator, count)
        hidden_seen += sum(1 for cand in candidates if not cand.visible)
        visible = [cand for cand in candidates if cand.visible]
        if not visible:
            continue  # 全部隐藏：换下一条策略（D2：text= 曾命中隐藏 tooltip）
        # 可交互的排前面（隐藏的已被过滤），再按 AX 顺序对账
        visible.sort(key=lambda cand: (not cand.enabled, cand.locator_index))
        ordered = _order_by_ax(visible, snapshot, target)
        if strategy == "role" and snapshot is not None:
            for cand in ordered:
                if cand.node is None:
                    node = await _ax_lookup(snapshot, target.role or "", cand.name)
                    if node is not None:
                        object.__setattr__(cand, "node", node)
        pool = ordered[:max_candidates] if max_candidates else ordered
        index = action.index
        if index is not None:
            if index < 0 or index >= len(ordered):
                extra = (
                    f"；本次只探了前 {MAX_PROBE} 个匹配（共 {count} 个），请用更精确的 target"
                    if count > MAX_PROBE
                    else ""
                )
                return Resolution(
                    ok=False,
                    code="E2_INDEX_OUT_OF_RANGE",
                    reason=f"index={index} 超出匹配数 {len(ordered)}（0 起算）{extra}",
                    candidates=tuple(pool),
                    strategy=attempt_strategy,
                    index=index,
                )
            chosen = ordered[index]
        else:
            if len(ordered) > 1:
                return Resolution(
                    ok=False,
                    code="E2_AMBIGUOUS",
                    reason=(
                        f"匹配 {len(ordered)} 个可见元素，无法确定点哪个"
                        + (f"（另有 {hidden_seen} 个不可见已排除）" if hidden_seen else "")
                    ),
                    candidates=tuple(pool),
                    strategy=attempt_strategy,
                )
            chosen = ordered[0]
        return Resolution(
            ok=True,
            locator=chosen.element,
            node=chosen.node,
            candidates=tuple(pool),
            reason=f"已定位 {chosen.label()}（{attempt_strategy}）",
            strategy=attempt_strategy,
            index=action.index,
            box=chosen.box,
        )

    if last_count > 0:
        return Resolution(
            ok=False,
            code="E2_NOT_FOUND",
            reason=f"匹配到 {last_count} 个元素但全部不可见（隐藏节点不算命中）",
            suggestions=_suggestions(snapshot, target, limit=max_candidates),
        )
    return Resolution(
        ok=False,
        code="E2_NOT_FOUND",
        reason="匹配 0 个元素",
        suggestions=_suggestions(snapshot, target, limit=max_candidates),
    )


async def _resolve_mark(
    page: Any,
    snapshot: Any,
    target: Target,
    mark_epoch: Mapping[int, Any] | None,
    max_candidates: int,
) -> Resolution:
    """mark → A11yNode → `(role,name)` 反查 locator；反查失败退回 box 坐标点击。"""
    mark = target.mark
    epoch = mark_epoch or {}
    node = epoch.get(int(mark)) if mark is not None else None
    if node is None:
        return Resolution(
            ok=False,
            code="E2_MARK_EXPIRED",
            reason=f"mark={mark} 不在本次截图的标签表（请先 browse_screenshot）",
        )
    role = str(getattr(node, "role", "") or "")
    name = str(getattr(node, "name", "") or "")
    box = _as_box(getattr(node, "box", None))
    if role and name:
        locator = _make_locator(page, Target(role=role, name=name), "role", True)
        if locator is not None:
            try:
                count = int(await locator.count())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                count = 0
            if count >= 1:
                candidates = await _probe(locator, count)
                visible = [cand for cand in candidates if cand.visible]
                if visible:
                    chosen = visible[0]
                    return Resolution(
                        ok=True,
                        locator=chosen.element,
                        node=node,
                        strategy="mark",
                        box=chosen.box or box,
                        reason=f"mark={mark} → {chosen.label()}",
                    )
    if box is not None:
        return Resolution(
            ok=True,
            locator=None,
            node=node,
            strategy="mark",
            box=box,
            requires_coordinates=True,
            reason=f"mark={mark} → {role}「{name}」（closed shadow / 无 locator，按 box 中心点）",
        )
    return Resolution(
        ok=False,
        code="E2_MARK_NO_BOX",
        reason=f"mark={mark} 对应节点既无法定位也没有 box，无法点击",
    )


# ---------------------------------------------------------------------------
# CDP session（注入式：不自己 launch、不自己取 page）
# ---------------------------------------------------------------------------

_CDP_CACHE: dict[int, Any] = {}


async def ensure_cdp(page: Any, *, provider: Any = None) -> Any | None:
    """每 Page 一条 CDP session（缓存键 `id(page)`）；失败返回 None 由调用方回退。

    `provider` 是注入的工厂（默认 `page.context.new_cdp_session`），便于单测。
    """
    key = id(page)
    cached = _CDP_CACHE.get(key)
    if cached is not None:
        return cached
    factory = provider
    if factory is None:
        context = getattr(page, "context", None)
        factory = getattr(context, "new_cdp_session", None)
    if factory is None:
        return None
    try:
        session = await factory(page)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — CDP 不可用就走原生路径
        return None
    if session is not None:
        _CDP_CACHE[key] = session
    return session


def drop_cdp(page: Any) -> None:
    """页面关闭/重建时丢缓存（`executor` 不缓存 page 引用，只按 id 记账）。"""
    _CDP_CACHE.pop(id(page), None)


async def _send(cdp: Any, method: str, params: Mapping[str, Any], commands: list[str]) -> bool:
    try:
        await cdp.send(method, dict(params))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 调用方按 False 走回退
        return False
    kind = params.get("type")
    commands.append(f"{method}:{kind}" if isinstance(kind, str) else method)
    return True


# ---------------------------------------------------------------------------
# 受信任事件（CDP Input.*）
# ---------------------------------------------------------------------------


async def _prepare_box(element: Any) -> tuple[float, float, float, float] | None:
    """`bounding_box()`；为 None 时先 `scroll_into_view_if_needed()` 再取（规格 §4.2）。"""
    box = await _bbox(element)
    if box is not None:
        return box
    scroller = getattr(element, "scroll_into_view_if_needed", None)
    if scroller is not None:
        try:
            await scroller()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None
        box = await _bbox(element)
    return box


async def trusted_click(
    page: Any,
    element: Any = None,
    *,
    cdp: Any = None,
    box: tuple[float, float, float, float] | None = None,
    viewport: tuple[int, int] | None = None,
) -> InputReport:
    """点击：CDP 三连（mouseMoved→mousePressed→mouseReleased）；不行就回退 locator.click()。"""
    commands: list[str] = []
    if box is None and element is not None:
        box = await _prepare_box(element)
    center = box_center(box)
    if cdp is None:
        return await _fallback_click(element, "CDP 会话不可用（new_cdp_session 未返回）")
    if center is None:
        return await _fallback_click(element, "元素没有 bounding box（被隐藏或未渲染）")
    view = viewport or _viewport(page)
    if not _inside_viewport(center, view):
        return await _fallback_click(
            element,
            f"坐标越界：中心 ({center[0]:.0f},{center[1]:.0f}) 不在视口 {view[0]}x{view[1]} 内",
        )
    x, y = center
    ok = await _send(
        cdp,
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
        commands,
    )
    ok = ok and await _send(
        cdp,
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        commands,
    )
    ok = ok and await _send(
        cdp,
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
        commands,
    )
    if not ok:
        return await _fallback_click(
            element,
            "CDP 发送 Input.dispatchMouseEvent 失败（页面已关闭或 session 失效）",
            commands=commands,
        )
    return InputReport(
        trusted=True,
        used="cdp",
        reason=f"CDP 受信任点击 @({x:.0f},{y:.0f})",
        commands=tuple(commands),
    )


async def _fallback_click(
    element: Any, reason: str, *, commands: Sequence[str] = ()
) -> InputReport:
    """回退到 Playwright 原生点击（`trusted=False`，原因必须可读）。"""
    if element is None:
        return InputReport(trusted=False, used="none", reason=reason + "；且没有 locator 可回退")
    clicker = getattr(element, "click", None)
    if clicker is None:
        return InputReport(trusted=False, used="none", reason=reason + "；locator 不支持 click()")
    try:
        await clicker()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return InputReport(
            trusted=False,
            used="locator",
            reason=f"{reason}；locator.click() 也失败：{_short_exc(exc)}",
            commands=tuple(commands),
        )
    return InputReport(
        trusted=False,
        used="locator",
        reason=reason + "；已回退 locator.click()",
        commands=tuple(commands),
    )


#: 键名 → (windowsVirtualKeyCode, char 文本)。Enter 必须发 char(text="\r") 才能提交表单。
_KEY_TABLE: dict[str, tuple[int, str | None, str]] = {
    "Enter": (13, "\r", "Enter"),
    "Tab": (9, None, "Tab"),
    "Escape": (27, None, "Escape"),
    "Backspace": (8, None, "Backspace"),
    "Delete": (46, None, "Delete"),
    "ArrowLeft": (37, None, "ArrowLeft"),
    "ArrowUp": (38, None, "ArrowUp"),
    "ArrowRight": (39, None, "ArrowRight"),
    "ArrowDown": (40, None, "ArrowDown"),
    "Home": (36, None, "Home"),
    "End": (35, None, "End"),
    "PageUp": (33, None, "PageUp"),
    "PageDown": (34, None, "PageDown"),
    "Space": (32, " ", "Space"),
}


async def press_key(page: Any, key: str, *, cdp: Any = None, element: Any = None) -> InputReport:
    """按键：CDP `dispatchKeyEvent(rawKeyDown/char/keyUp)`；回退 `locator.press` / `keyboard.press`。"""
    commands: list[str] = []
    vk, text, code = _KEY_TABLE.get(key, (0, None, key))
    if cdp is not None and vk:
        down_params: dict[str, Any] = {
            "type": "rawKeyDown",
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "key": key,
            "code": code,
        }
        ok = await _send(cdp, "Input.dispatchKeyEvent", down_params, commands)
        if ok and text is not None:
            char_params = dict(down_params)
            char_params.update({"type": "char", "text": text, "unmodifiedText": text})
            ok = await _send(cdp, "Input.dispatchKeyEvent", char_params, commands)
        if ok:
            up_params = dict(down_params)
            up_params["type"] = "keyUp"
            ok = await _send(cdp, "Input.dispatchKeyEvent", up_params, commands)
        if ok:
            return InputReport(
                trusted=True, used="cdp", reason=f"CDP 按键 {key}", commands=tuple(commands)
            )
        reason = f"CDP 发送 Input.dispatchKeyEvent({key}) 失败"
    else:
        reason = "CDP 不可用（或键名不在虚拟键码表）"

    presser = getattr(element, "press", None)
    if presser is not None:
        try:
            await presser(key)
            return InputReport(
                trusted=False,
                used="locator",
                reason=reason + "；已回退 locator.press()",
                commands=tuple(commands),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = f"{reason}；locator.press() 也失败：{_short_exc(exc)}"
    keyboard = getattr(page, "keyboard", None)
    presser = getattr(keyboard, "press", None) if keyboard is not None else None
    if presser is None:
        return InputReport(
            trusted=False, used="none", reason=reason + "；页面没有 keyboard", commands=tuple(commands)
        )
    try:
        await presser(key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return InputReport(
            trusted=False,
            used="page",
            reason=f"{reason}；page.keyboard.press() 也失败：{_short_exc(exc)}",
            commands=tuple(commands),
        )
    return InputReport(
        trusted=False,
        used="page",
        reason=reason + "；已回退 page.keyboard.press()",
        commands=tuple(commands),
    )


async def trusted_focus_and_type(
    page: Any,
    text: str,
    *,
    element: Any = None,
    cdp: Any = None,
    box: tuple[float, float, float, float] | None = None,
    clear: bool = True,
    submit: bool = False,
    viewport: tuple[int, int] | None = None,
) -> InputReport:
    """输入：聚焦（受信任点击）→ 清空 → `Input.insertText`；提交则追加 Enter。"""
    commands: list[str] = []
    focus = await trusted_click(page, element, cdp=cdp, box=box, viewport=viewport)
    commands.extend(focus.commands)
    if cdp is None:
        if element is None:
            return InputReport(
                trusted=False,
                used="none",
                reason=focus.reason + "；且没有 locator 可 fill",
                commands=tuple(commands),
            )
        filler = getattr(element, "fill", None)
        if filler is None:
            return InputReport(
                trusted=False, used="none", reason="locator 不支持 fill()", commands=tuple(commands)
            )
        try:
            await filler(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return InputReport(
                trusted=False,
                used="locator",
                reason=f"locator.fill() 失败：{_short_exc(exc)}",
                commands=tuple(commands),
            )
        report = await press_key(page, "Enter", cdp=None, element=element) if submit else None
        return InputReport(
            trusted=False,
            used="locator",
            reason=focus.reason + "；已回退 locator.fill()",
            commands=tuple(commands) + (report.commands if report else ()),
        )
    if clear and element is not None:
        # insertText 是**追加**语义：先清空（回退路径的 locator.fill 本身就是替换，不需要清）
        filler = getattr(element, "fill", None)
        if filler is not None:
            try:
                await filler("")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 清空失败不影响后续 insertText
                pass
    if not await _send(cdp, "Input.insertText", {"text": text}, commands):
        return InputReport(
            trusted=False,
            used="none",
            reason="CDP 发送 Input.insertText 失败",
            commands=tuple(commands),
        )
    if submit:
        report = await press_key(page, "Enter", cdp=cdp)
        commands.extend(report.commands)
    return InputReport(
        trusted=True,
        used="cdp",
        reason=f"CDP insertText {len(text)} 字符" + ("+Enter" if submit else ""),
        commands=tuple(commands),
    )


async def hover_at(
    page: Any,
    element: Any = None,
    *,
    cdp: Any = None,
    box: tuple[float, float, float, float] | None = None,
    viewport: tuple[int, int] | None = None,
) -> InputReport:
    """悬停：CDP mouseMoved；回退 `locator.hover()`；成功后等 120ms 让菜单出现。"""
    commands: list[str] = []
    if box is None and element is not None:
        box = await _prepare_box(element)
    center = box_center(box)
    view = viewport or _viewport(page)
    if cdp is not None and center is not None and _inside_viewport(center, view):
        ok = await _send(
            cdp,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": center[0],
                "y": center[1],
                "button": "none",
                "buttons": 0,
            },
            commands,
        )
        if ok:
            await _settle(page, HOVER_SETTLE_MS)
            return InputReport(
                trusted=True,
                used="cdp",
                reason=f"CDP 悬停 @({center[0]:.0f},{center[1]:.0f})",
                commands=tuple(commands),
            )
        reason = "CDP 发送 mouseMoved 失败"
    elif cdp is None:
        reason = "CDP 会话不可用"
    elif center is None:
        reason = "元素没有 bounding box"
    else:
        reason = f"坐标越界：中心不在视口 {view[0]}x{view[1]} 内"

    hover = getattr(element, "hover", None) if element is not None else None
    if hover is None:
        return InputReport(trusted=False, used="none", reason=reason, commands=tuple(commands))
    try:
        await hover()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return InputReport(
            trusted=False,
            used="locator",
            reason=f"{reason}；locator.hover() 也失败：{_short_exc(exc)}",
            commands=tuple(commands),
        )
    await _settle(page, HOVER_SETTLE_MS)
    return InputReport(
        trusted=False,
        used="locator",
        reason=reason + "；已回退 locator.hover()",
        commands=tuple(commands),
    )


async def scroll_at(
    page: Any,
    delta_y: int,
    *,
    cdp: Any = None,
    viewport: tuple[int, int] | None = None,
) -> InputReport:
    """滚动：CDP `mouseWheel`（能触发 wheel 监听）；回退单参 `evaluate` 的 scrollBy。"""
    commands: list[str] = []
    view = viewport or _viewport(page)
    center = (view[0] / 2.0, view[1] / 2.0)
    if cdp is not None:
        ok = await _send(
            cdp,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": center[0],
                "y": center[1],
                "deltaX": 0,
                "deltaY": int(delta_y),
            },
            commands,
        )
        if ok:
            return InputReport(
                trusted=True,
                used="cdp",
                reason=f"CDP wheel deltaY={int(delta_y)}",
                commands=tuple(commands),
            )
        reason = "CDP 发送 mouseWheel 失败"
    else:
        reason = "CDP 会话不可用"
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        mouse = getattr(page, "mouse", None)
        wheel = getattr(mouse, "wheel", None) if mouse is not None else None
        if wheel is None:
            return InputReport(
                trusted=False, used="none", reason=reason, commands=tuple(commands)
            )
        try:
            await wheel(0, int(delta_y))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return InputReport(
                trusted=False, used="page", reason=f"{reason}；mouse.wheel 失败：{_short_exc(exc)}"
            )
        return InputReport(
            trusted=False, used="page", reason=reason + "；已回退 mouse.wheel()"
        )
    try:
        # 单参 evaluate（F1：Page.evaluate 只收 1 个位置实参）
        await evaluate("(dy) => window.scrollBy(0, dy)", arg=int(delta_y))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return InputReport(
            trusted=False,
            used="page",
            reason=f"{reason}；window.scrollBy 失败：{_short_exc(exc)}",
            commands=tuple(commands),
        )
    return InputReport(
        trusted=False,
        used="page",
        reason=reason + "；已回退 window.scrollBy",
        commands=tuple(commands),
    )


async def _settle(page: Any, ms: int) -> None:
    waiter = getattr(page, "wait_for_timeout", None)
    if waiter is None:
        await asyncio.sleep(ms / 1000.0)
        return
    try:
        await waiter(ms)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        await asyncio.sleep(ms / 1000.0)


def _short_exc(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text[:120]}" if text else type(exc).__name__


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(exc).__name__.lower()


# ---------------------------------------------------------------------------
# 16 动作执行矩阵（规格 §4.3）
# ---------------------------------------------------------------------------


async def apply(
    page: Any,
    action: Action,
    *,
    cdp: Any = None,
    trusted_events: bool = True,
    snapshot: Any = None,
    mark_epoch: Mapping[int, Any] | None = None,
    tabs: Any = None,
    som: Any = None,
    action_timeout_s: float = 20.0,
    goto_timeout_s: float = 20.0,
    read_limit: int = 8000,
) -> ExecutionResult:
    """执行一个动作（**只执行，不判定**；判定交给 `core.verify`）。

    所有 page/locator/CDP 都由外部注入；本函数**不** launch 浏览器、**不**取 page、
    **不**自己加会话锁（锁归 `SessionManager`，重复加锁会死锁）。
    """
    kind = action.kind
    handler = _HANDLERS.get(kind)
    if handler is None:  # pragma: no cover - ActionKind 是闭集
        return ExecutionResult(ok=False, action=kind.value, reason=f"未实现的动作 {kind.value}")
    started = time.monotonic()
    ctx = _Ctx(
        page=page,
        action=action,
        cdp=cdp,
        trusted_events=trusted_events,
        snapshot=snapshot,
        mark_epoch=mark_epoch,
        tabs=tabs,
        som=som,
        action_timeout_s=action_timeout_s,
        goto_timeout_s=goto_timeout_s,
        read_limit=read_limit,
    )
    result = await handler(ctx)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if result.elapsed_ms == 0 and elapsed_ms > 0:
        result = replace(result, elapsed_ms=elapsed_ms)
    return result


@dataclass
class _Ctx:
    page: Any
    action: Action
    cdp: Any = None
    trusted_events: bool = True
    snapshot: Any = None
    mark_epoch: Mapping[int, Any] | None = None
    tabs: Any = None
    som: Any = None
    action_timeout_s: float = 20.0
    goto_timeout_s: float = 20.0
    read_limit: int = 8000


async def _with_timeout(coro: Any, seconds: float, label: str) -> Any:
    """单步超时（E3）：超时返回 `_TIMEOUT` 哨兵（`label` 供调用方拼文案），取消照旧上抛。"""
    try:
        return await asyncio.wait_for(coro, timeout=max(0.0, seconds))
    except asyncio.TimeoutError:
        _TIMEOUT.label = label  # 只在超时路径写，便于排查是哪一步慢
        return _TIMEOUT


class _Timeout:
    """E3 哨兵（与正常返回值区分；None 也可能是合法结果）。"""

    label: str = ""


_TIMEOUT = _Timeout()


async def _resolve_for_ctx(ctx: _Ctx) -> Resolution | Any:
    result = await _with_timeout(
        resolve(
            ctx.page,
            ctx.snapshot,
            ctx.action,
            mark_epoch=ctx.mark_epoch,
        ),
        ctx.action_timeout_s,
        "定位",
    )
    return result


def _need_resolution(ctx: _Ctx, resolution: Any) -> ExecutionResult | None:
    """把定位失败/超时翻译成 ExecutionResult（失败即返回，成功返回 None）。"""
    if isinstance(resolution, _Timeout):
        return ExecutionResult(
            ok=False,
            action=ctx.action.kind.value,
            reason=f"【超时】定位超过 {ctx.action_timeout_s:g}s",
        )
    if not resolution.ok:
        return ExecutionResult(
            ok=False,
            action=ctx.action.kind.value,
            reason=resolution.render(),
            resolution=resolution,
            candidates=resolution.candidates,
        )
    return None


async def _do_click(ctx: _Ctx) -> ExecutionResult:
    resolution = await _resolve_for_ctx(ctx)
    failed = _need_resolution(ctx, resolution)
    if failed is not None:
        return failed
    if ctx.trusted_events:
        report = await trusted_click(
            ctx.page,
            resolution.locator,
            cdp=ctx.cdp,
            box=resolution.box,
            viewport=_viewport(ctx.page, ctx.snapshot),
        )
    else:
        report = await _fallback_click(resolution.locator, "trusted_events=false（配置关闭 CDP 路径）")
    return ExecutionResult(
        ok=report.used != "none",
        action="click",
        trusted=report.trusted,
        used=report.used,
        reason="" if report.used != "none" else report.reason,
        detail=report.reason,
        commands=report.commands,
        resolution=resolution,
        box=resolution.box,
        candidates=resolution.candidates,
    )


async def _do_type(ctx: _Ctx) -> ExecutionResult:
    resolution = await _resolve_for_ctx(ctx)
    failed = _need_resolution(ctx, resolution)
    if failed is not None:
        return failed
    text = "" if ctx.action.value is None else str(ctx.action.value)
    if ctx.trusted_events:
        report = await trusted_focus_and_type(
            ctx.page,
            text,
            element=resolution.locator,
            cdp=ctx.cdp,
            box=resolution.box,
            submit=bool(ctx.action.submit),
            viewport=_viewport(ctx.page, ctx.snapshot),
        )
    else:
        report = await _fill_native(resolution.locator, text, submit=bool(ctx.action.submit))
    return ExecutionResult(
        ok=report.used != "none",
        action="type",
        trusted=report.trusted,
        used=report.used,
        reason="" if report.used != "none" else report.reason,
        detail=report.reason,
        commands=report.commands,
        resolution=resolution,
        box=resolution.box,
        value=len(text),
    )


async def _fill_native(element: Any, text: str, *, submit: bool = False) -> InputReport:
    if element is None:
        return InputReport(trusted=False, used="none", reason="没有 locator 可 fill")
    filler = getattr(element, "fill", None)
    if filler is None:
        return InputReport(trusted=False, used="none", reason="locator 不支持 fill()")
    try:
        await filler(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return InputReport(
            trusted=False, used="locator", reason=f"locator.fill() 失败：{_short_exc(exc)}"
        )
    commands: tuple[str, ...] = ()
    if submit:
        presser = getattr(element, "press", None)
        if presser is not None:
            try:
                await presser("Enter")
                commands = ("locator.press:Enter",)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
    return InputReport(
        trusted=False,
        used="locator",
        reason="trusted_events=false（配置关闭 CDP 路径）；已用 locator.fill()",
        commands=commands,
    )


async def _do_select(ctx: _Ctx) -> ExecutionResult:
    resolution = await _resolve_for_ctx(ctx)
    failed = _need_resolution(ctx, resolution)
    if failed is not None:
        return failed
    locator = resolution.locator
    if locator is None:
        return ExecutionResult(
            ok=False, action="select", reason="【未找到】该 target 没有 locator，无法选择下拉项"
        )
    wanted = "" if ctx.action.value is None else str(ctx.action.value)
    last_error = ""
    for kwargs in ({"label": wanted}, {"value": wanted}):
        try:
            await locator.select_option(**kwargs)
            return ExecutionResult(
                ok=True,
                action="select",
                used="locator",
                detail=f"已选择 {list(kwargs)[0]}={wanted}",
                resolution=resolution,
                value=wanted,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 按 label → value 顺序降级
            last_error = _short_exc(exc)
    options = await _option_labels(locator)
    listed = "、".join(options[:12]) if options else "（读不到 option 列表）"
    return ExecutionResult(
        ok=False,
        action="select",
        reason=f"【未找到】select 里没有「{wanted}」；全部选项：{listed}",
        detail=last_error,
        resolution=resolution,
    )


async def _option_labels(locator: Any) -> list[str]:
    evaluate = getattr(locator, "evaluate", None)
    if evaluate is None:
        return []
    try:
        raw = await evaluate(
            "el => Array.from(el.options || []).map(o => (o.label || o.textContent || '').trim())"
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return []


async def _do_check(ctx: _Ctx) -> ExecutionResult:
    resolution = await _resolve_for_ctx(ctx)
    failed = _need_resolution(ctx, resolution)
    if failed is not None:
        return failed
    locator = resolution.locator
    target_state = True if ctx.action.value is None else bool(ctx.action.value)
    before = await read_control_state(locator) if locator is not None else "unknown"
    if before == ("checked" if target_state else "unchecked"):
        return ExecutionResult(
            ok=True,
            action="check",
            used="locator",
            detail=f"已是目标态（{before}），无需点击",
            resolution=resolution,
            value=before,
        )
    if ctx.trusted_events:
        report = await trusted_click(
            ctx.page,
            locator,
            cdp=ctx.cdp,
            box=resolution.box,
            viewport=_viewport(ctx.page, ctx.snapshot),
        )
    else:
        report = await _fallback_click(locator, "trusted_events=false（配置关闭 CDP 路径）")
    after = await read_control_state(locator) if locator is not None else "unknown"
    want = "checked" if target_state else "unchecked"
    if after == "unknown":
        return ExecutionResult(
            ok=True,
            action="check",
            trusted=report.trusted,
            used=report.used,
            detail="该控件没有可读状态，只能靠「页面变化」判断",
            commands=report.commands,
            resolution=resolution,
            value=after,
        )
    if after != want:
        return ExecutionResult(
            ok=False,
            action="check",
            trusted=report.trusted,
            used=report.used,
            reason=f"【无变化】点击后状态仍为 {after}（期望 {want}）",
            commands=report.commands,
            resolution=resolution,
            value=after,
        )
    return ExecutionResult(
        ok=True,
        action="check",
        trusted=report.trusted,
        used=report.used,
        detail=f"状态 {before} → {after}",
        commands=report.commands,
        resolution=resolution,
        value=after,
    )


async def _do_press(ctx: _Ctx) -> ExecutionResult:
    key = str(ctx.action.value or "")
    element = None
    if ctx.action.target is not None:
        resolution = await _resolve_for_ctx(ctx)
        failed = _need_resolution(ctx, resolution)
        if failed is not None:
            return failed
        element = resolution.locator
    report = await press_key(ctx.page, key, cdp=ctx.cdp if ctx.trusted_events else None, element=element)
    return ExecutionResult(
        ok=report.used != "none",
        action="press",
        trusted=report.trusted,
        used=report.used,
        reason="" if report.used != "none" else report.reason,
        detail=report.reason,
        commands=report.commands,
        value=key,
    )


async def _do_scroll(ctx: _Ctx) -> ExecutionResult:
    direction = str(ctx.action.value or "down")
    view = _viewport(ctx.page, ctx.snapshot)
    step = ctx.action.amount or int(max(1, view[1]) * 0.8)
    if direction in ("up", "page_up"):
        delta = -abs(step)
    elif direction in ("down", "page_down"):
        delta = abs(step)
    else:  # top / bottom：直接定位到两端
        evaluate = getattr(ctx.page, "evaluate", None)
        if evaluate is None:
            return ExecutionResult(ok=False, action="scroll", reason="页面不支持 evaluate")
        try:
            await evaluate(
                "(to) => window.scrollTo(0, to)",
                arg=0 if direction == "top" else 10**7,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(ok=False, action="scroll", reason=_short_exc(exc))
        return ExecutionResult(ok=True, action="scroll", used="page", detail=direction, value=direction)
    report = await scroll_at(
        ctx.page, delta, cdp=ctx.cdp if ctx.trusted_events else None, viewport=view
    )
    return ExecutionResult(
        ok=report.used != "none",
        action="scroll",
        trusted=report.trusted,
        used=report.used,
        reason="" if report.used != "none" else report.reason,
        detail=report.reason,
        commands=report.commands,
        value=direction,
    )


async def _do_hover(ctx: _Ctx) -> ExecutionResult:
    resolution = await _resolve_for_ctx(ctx)
    failed = _need_resolution(ctx, resolution)
    if failed is not None:
        return failed
    if ctx.trusted_events:
        report = await hover_at(
            ctx.page,
            resolution.locator,
            cdp=ctx.cdp,
            box=resolution.box,
            viewport=_viewport(ctx.page, ctx.snapshot),
        )
    else:
        hover = getattr(resolution.locator, "hover", None)
        if hover is None:
            report = InputReport(trusted=False, used="none", reason="locator 不支持 hover()")
        else:
            try:
                await hover()
                report = InputReport(
                    trusted=False,
                    used="locator",
                    reason="trusted_events=false（配置关闭 CDP 路径）",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                report = InputReport(
                    trusted=False, used="locator", reason=f"locator.hover() 失败：{_short_exc(exc)}"
                )
    return ExecutionResult(
        ok=report.used != "none",
        action="hover",
        trusted=report.trusted,
        used=report.used,
        reason="" if report.used != "none" else report.reason,
        detail=report.reason,
        commands=report.commands,
        resolution=resolution,
    )


async def _navigate_impl(ctx: _Ctx, url: str) -> ExecutionResult:
    goto = getattr(ctx.page, "goto", None)
    if goto is None:
        return ExecutionResult(ok=False, action="navigate", reason="页面不支持 goto()")
    timeout_ms = int(max(0.0, ctx.goto_timeout_s) * 1000)
    for attempt in (1, 2):
        try:
            await goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return ExecutionResult(
                ok=True, action="navigate", used="page", detail=url, attempt=attempt
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_timeout(exc):
                return ExecutionResult(
                    ok=False, action="navigate", reason=f"【错误】导航失败：{_short_exc(exc)}"
                )
            # D3/D7：超时不等于失败 —— 先看有没有正文
            text = await _page_text(ctx.page)
            if len(text.strip()) >= 40:
                return ExecutionResult(
                    ok=True,
                    action="navigate",
                    used="page",
                    detail=(
                        f"【超时但有内容】{ctx.goto_timeout_s:g}s 内未 load 完，"
                        f"但正文已可读（{len(text)} 字）"
                    ),
                    attempt=attempt,
                )
            if attempt == 1:
                continue
            return ExecutionResult(
                ok=False,
                action="navigate",
                reason=f"【超时】导航 {ctx.goto_timeout_s:g}s 超时且正文为空（已重试 1 次）",
                attempt=attempt,
            )
    return ExecutionResult(ok=False, action="navigate", reason="【超时】导航失败")  # pragma: no cover


async def _page_text(page: Any) -> str:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return ""
    try:
        raw = await evaluate("() => document.body ? document.body.innerText : ''")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return ""
    return str(raw or "")


async def _do_back(ctx: _Ctx) -> ExecutionResult:
    host = getattr(ctx.page, "go_back", None)
    action = "back"
    if host is None:
        return ExecutionResult(ok=False, action=action, reason="页面不支持 go_back()")
    try:
        result = await host(wait_until="domcontentloaded", timeout=int(ctx.goto_timeout_s * 1000))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action=action, reason=f"【错误】{_short_exc(exc)}")
    if result is None:
        return ExecutionResult(ok=False, action=action, reason="【不可用】没有可后退的历史")
    return ExecutionResult(ok=True, action=action, used="page", value=str(getattr(result, "url", "")))


async def _do_forward(ctx: _Ctx) -> ExecutionResult:
    host = getattr(ctx.page, "go_forward", None)
    action = "forward"
    if host is None:
        return ExecutionResult(ok=False, action=action, reason="页面不支持 go_forward()")
    try:
        result = await host(wait_until="domcontentloaded", timeout=int(ctx.goto_timeout_s * 1000))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action=action, reason=f"【错误】{_short_exc(exc)}")
    if result is None:
        return ExecutionResult(ok=False, action=action, reason="【不可用】没有可前进的历史")
    return ExecutionResult(ok=True, action=action, used="page", value=str(getattr(result, "url", "")))


async def _do_reload(ctx: _Ctx) -> ExecutionResult:
    host = getattr(ctx.page, "reload", None)
    if host is None:
        return ExecutionResult(ok=False, action="reload", reason="页面不支持 reload()")
    timeout_ms = int(max(0.0, ctx.goto_timeout_s) * 1000)
    try:
        await host(wait_until="domcontentloaded", timeout=timeout_ms)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_timeout(exc):
            text = await _page_text(ctx.page)
            if len(text.strip()) >= 40:
                return ExecutionResult(
                    ok=True,
                    action="reload",
                    used="page",
                    detail="【超时但有内容】刷新超时但正文已可读",
                    attempt=2,
                )
            return ExecutionResult(
                ok=False, action="reload", reason="【超时】刷新超时且正文为空", attempt=2
            )
        return ExecutionResult(ok=False, action="reload", reason=f"【错误】{_short_exc(exc)}")
    return ExecutionResult(ok=True, action="reload", used="page", detail="已刷新")


async def _do_tab(ctx: _Ctx) -> ExecutionResult:
    sub = str(ctx.action.value or "list")
    tabs = ctx.tabs
    if tabs is None:
        return ExecutionResult(
            ok=False, action="tab", reason="【不可用】未注入标签页管理器（tab 动作归 SessionManager）"
        )
    index = ctx.action.index
    try:
        if sub == "new":
            creator = getattr(tabs, "new_tab", None) or getattr(tabs, "new", None)
            if creator is None:
                return ExecutionResult(ok=False, action="tab", reason="标签页管理器不支持新建")
            # new 的落地页固定 about:blank（Schema 里没有 url 字段；要打开网址请接着用 navigate）
            created = await creator("about:blank")
            return ExecutionResult(
                ok=True, action="tab", used="page", detail="已新建标签页", value=str(created)
            )
        if sub in ("switch", "close"):
            if index is None:
                return ExecutionResult(
                    ok=False,
                    action="tab",
                    reason=f"【拒绝】tab({sub}) 需要 index（**1 起算**）",
                )
            if index < 1:
                return ExecutionResult(
                    ok=False,
                    action="tab",
                    reason=f"【拒绝】标签页 index 从 1 起算（收到 {index}）",
                )
            method = "switch_tab" if sub == "switch" else "close_tab"
            caller = getattr(tabs, method, None)
            if caller is None:
                return ExecutionResult(ok=False, action="tab", reason=f"标签页管理器不支持 {method}")
            listing = await _tab_list(tabs)
            if listing and index > len(listing):
                return ExecutionResult(
                    ok=False,
                    action="tab",
                    reason=f"【未找到】标签页 index={index} 越界（共 {len(listing)} 个）："
                    + "；".join(listing),
                )
            await caller(index)
            return ExecutionResult(
                ok=True, action="tab", used="page", detail=f"已{'切换' if sub == 'switch' else '关闭'}第 {index} 个标签页"
            )
        listing = await _tab_list(tabs)
        return ExecutionResult(
            ok=True,
            action="tab",
            used="page",
            detail="；".join(listing) if listing else "（没有标签页）",
            value=len(listing),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action="tab", reason=f"【错误】{_short_exc(exc)}")


async def _tab_list(tabs: Any) -> list[str]:
    lister = getattr(tabs, "list_tabs", None) or getattr(tabs, "list", None)
    if lister is None:
        return []
    try:
        raw = await lister() if asyncio.iscoroutinefunction(lister) else lister()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, Mapping):
        return [f"{idx}. {url}" for idx, url in raw.items()]
    if isinstance(raw, (list, tuple)):
        return [f"{i + 1}. {item}" for i, item in enumerate(raw)]
    return []


async def _do_read(ctx: _Ctx) -> ExecutionResult:
    mode = str(ctx.action.value or "text")
    evaluate = getattr(ctx.page, "evaluate", None)
    if evaluate is None:
        return ExecutionResult(ok=False, action="read", reason="页面不支持 evaluate")
    js = (
        "() => document.body ? document.body.innerText : ''"
        if mode == "text"
        else "() => Array.from(document.querySelectorAll('a')).slice(0, 200).map("
        "a => [(a.innerText || '').trim().slice(0, 120), a.href])"
    )
    try:
        raw = await evaluate(js)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action="read", reason=f"【错误】{_short_exc(exc)}")
    if mode == "text":
        text = str(raw or "")
        return ExecutionResult(
            ok=True,
            action="read",
            used="page",
            detail=f"正文 {len(text)} 字（截断到 {ctx.read_limit}）",
            value=text[: ctx.read_limit],
        )
    links = raw if isinstance(raw, (list, tuple)) else []
    return ExecutionResult(
        ok=True,
        action="read",
        used="page",
        detail=f"链接 {len(links)} 条",
        value=list(links)[:200],
    )


async def _do_screenshot(ctx: _Ctx) -> ExecutionResult:
    if ctx.som is None:
        return ExecutionResult(
            ok=False, action="screenshot", reason="【不可用】未注入 SoM 截图实现"
        )
    mode = str(ctx.action.value or "viewport")
    try:
        result = await ctx.som(ctx.page, target=ctx.action.target, mode=mode)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action="screenshot", reason=f"【错误】截图失败：{_short_exc(exc)}")
    return ExecutionResult(ok=True, action="screenshot", used="page", detail=f"SoM({mode})", value=result)


async def _do_wait(ctx: _Ctx) -> ExecutionResult:
    ms = int(ctx.action.value or 0)
    ms = min(ms, int(max(0.0, ctx.action_timeout_s) * 1000))
    waiter = getattr(ctx.page, "wait_for_timeout", None)
    if waiter is None:
        await asyncio.sleep(ms / 1000.0)
        return ExecutionResult(ok=True, action="wait", used="page", detail=f"等待 {ms}ms（asyncio）")
    try:
        await waiter(ms)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(ok=False, action="wait", reason=_short_exc(exc))
    return ExecutionResult(ok=True, action="wait", used="page", detail=f"等待 {ms}ms")


async def _do_done(ctx: _Ctx) -> ExecutionResult:
    value = ctx.action.value
    return ExecutionResult(
        ok=True, action="done", used="none", detail="任务结束", value=value
    )


_HANDLERS: dict[ActionKind, Any] = {
    ActionKind.NAVIGATE: lambda ctx: _navigate_impl(ctx, str(ctx.action.value or "")),
    ActionKind.CLICK: _do_click,
    ActionKind.TYPE: _do_type,
    ActionKind.SELECT: _do_select,
    ActionKind.CHECK: _do_check,
    ActionKind.PRESS: _do_press,
    ActionKind.SCROLL: _do_scroll,
    ActionKind.HOVER: _do_hover,
    ActionKind.BACK: _do_back,
    ActionKind.FORWARD: _do_forward,
    ActionKind.RELOAD: _do_reload,
    ActionKind.TAB: _do_tab,
    ActionKind.READ: _do_read,
    ActionKind.SCREENSHOT: _do_screenshot,
    ActionKind.WAIT: _do_wait,
    ActionKind.DONE: _do_done,
}
