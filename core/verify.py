"""core.verify — 验证层（v1.7.5 技术栈 ⑤；规格 §5）。

职责（三档，优先级从高到低）：

- **V1 显式期望** `action.expect` 非空 ⇒ 逐项求值，全满足才是 `verified`；
- **V2 默认差分** 比较动作前后六类状态，至少一类变化 ⇒ `changed`；
- **V3 无变化** 六类全不变 ⇒ `no_change`（**「调用成功」不等于「状态改变」**，
  D2 的根因 2：v1.6.x 返回「已点击」被模型读成成功，于是同一 selector 连点 4 次）。

另外三件设施：

- `RepeatGuard`：同 (action, 规范化 target) 连续第 N 次无变化 ⇒ 拒绝（§5.3，D2 根因 3）；
- `read_control_state()`：勾选态三路读回 `aria-checked` → 原生 `checked` → `data-state`
  （§5.2，D5：v1.6.x 报 `Not a checkbox or radio button` 的根因）；
- `render_observation()`：观测块的**唯一**渲染处（铁律 3；`executor` / `a11y` 不许各拼一份）。

层纪律：本模块**不 import playwright、不 import astrbot**（纯 Python，可离线单测）；
page / locator 一律鸭子类型，`except Exception` 之前**必须先放行 `CancelledError`**
（取消必须上抛，D9 的 `Future exception was never retrieved` 就是吞取消的形态）。

**规模说明**（对规格 §1.3 守则 260–380 行的偏离，实现后实测 ~800 行）：三档判定 + 六类差分 +
重复守卫 + 三路状态读回 + 观测块渲染（铁律 3 的唯一渲染处）挤在同一模块里；拆开会新增
规格模块清单之外的第四个文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import a11y
from .action_model import Action, ActionKind, Expect, Rejection

__all__ = [
    "OUTCOME_CHANGED",
    "OUTCOME_FAILED",
    "OUTCOME_NO_CHANGE",
    "OUTCOME_REJECTED",
    "OUTCOME_SKIPPED",
    "OUTCOME_UNCHANGED",
    "OUTCOME_VERIFIED",
    "LEDGER_STATUS",
    "Delta",
    "RepeatGuard",
    "State",
    "Verdict",
    "attach_snapshot",
    "ax_digest",
    "capture",
    "check",
    "check_value",
    "diff",
    "is_unchanged",
    "read_control_state",
    "render_observation",
    "wait_for_expect",
]

# ---------------------------------------------------------------------------
# 结果词（写死；台账 `status` 直接取它，模型侧文案在 Verdict.render()）
# ---------------------------------------------------------------------------

OUTCOME_VERIFIED = "verified"  # V1：期望全部满足
OUTCOME_CHANGED = "changed"  # V2：六类状态至少一类变了
OUTCOME_NO_CHANGE = "no_change"  # V3：动作已发出但页面没动（台账 status=unchanged）
OUTCOME_UNCHANGED = OUTCOME_NO_CHANGE  # 规格 §5.2 的叫法，指向同一个词
OUTCOME_FAILED = "failed"  # 期望未达成 / 值没生效
OUTCOME_REJECTED = "rejected"  # 被 repeat_guard 拦下（未执行）
OUTCOME_SKIPPED = "skipped"  # 只读动作（read/observe）不做差分

#: 台账 `status`（规格 §1.6）：`no_change` → `unchanged`，只读动作记 `verified`。
LEDGER_STATUS: dict[str, str] = {
    OUTCOME_VERIFIED: "verified",
    OUTCOME_CHANGED: "changed",
    OUTCOME_NO_CHANGE: "unchanged",
    OUTCOME_FAILED: "failed",
    OUTCOME_REJECTED: "rejected",
    OUTCOME_SKIPPED: "verified",
}

#: `note()` 认的「无变化」词（任务契约写 no_change，规格 §5.3 写 unchanged）。
_NO_CHANGE_STATUSES = frozenset({OUTCOME_NO_CHANGE, "unchanged"})

#: 滚动变化阈值（px）：小于它视为没动（规格 §5.2）。
SCROLL_EPSILON = 8

#: 默认正文采集上限（字符）：只用于差分与 `text_contains`，不进日志。
DEFAULT_BODY_LIMIT = 20000

#: 单参 evaluate：一次取回「正文 + 焦点 + 滚动位置 + 元素数」（不给第二个实参，F1）。
_STATE_JS = """
() => {
  const body = document.body;
  const text = body ? (body.innerText || '') : '';
  const ae = document.activeElement;
  const nameOf = (el) => {
    if (!el) { return ''; }
    const role = (el.getAttribute && el.getAttribute('role')) || (el.tagName || '').toLowerCase();
    const raw = (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('name'))) ||
      (el.innerText || el.value || '');
    return role + ':' + String(raw).trim().slice(0, 60);
  };
  return {
    text: text.slice(0, __BODY_LIMIT__),
    active: nameOf(ae),
    scroll_y: Math.round(window.scrollY || 0),
    nodes: body ? body.getElementsByTagName('*').length : 0,
  };
}
"""


def _state_js(body_limit: int) -> str:
    return _STATE_JS.replace("__BODY_LIMIT__", str(max(1, int(body_limit))))


def _hash_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 状态快照（动作前后各一次）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    """动作前后的一次轻量状态（四个必采项 + 两个差分辅助项）。

    必采项（任务契约）：`url` / `title` / `body_text_hash` / `active_element`。
    辅助项：`scroll_y`（滚动类动作的唯一判据）、`nodes`（DOM 元素数，AX 快照缺席时的替身）。
    """

    url: str = ""
    title: str = ""
    text: str = ""
    body_text_hash: str = ""
    active_element: str = ""
    scroll_y: int = 0
    nodes: int = -1
    ax_digest: str = ""  # AX 摘要（role|name|checked|disabled 三元组的哈希；有快照时才有）
    taken_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # 便于单测：只给 text 时自动补 hash（frozen dataclass 用 object.__setattr__）
        if not self.body_text_hash and self.text:
            object.__setattr__(self, "body_text_hash", _hash_text(self.text))

    @property
    def text_len(self) -> int:
        return len(self.text or "")

    def digest(self) -> str:
        raw = "|".join(
            (
                self.url,
                self.title,
                self.body_text_hash,
                self.active_element,
                str(self.scroll_y),
                str(self.nodes),
                self.ax_digest,
            )
        )
        return _hash_text(raw)

    def to_payload(self) -> dict:
        """给台账/调试用（**不含正文**；正文只经 read(text) 给模型）。"""
        return {
            "url": self.url,
            "title": self.title,
            "body_text_hash": self.body_text_hash,
            "active_element": self.active_element,
            "scroll_y": self.scroll_y,
            "nodes": self.nodes,
            "text_len": self.text_len,
            "ax_digest": self.ax_digest,
        }


async def capture(page: Any, *, body_limit: int = DEFAULT_BODY_LIMIT) -> State:
    """采集一次状态（1 次 `Page.evaluate` + 1 次 `page.title()`）。

    任何单点失败都降级为空字段（**绝不**因为标题取不到就让整步失败）；
    但 `CancelledError` 必须原样上抛。
    """
    url = str(getattr(page, "url", "") or "")
    title = ""
    try:
        title = str(await page.title() or "")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 标题失败不影响差分
        title = ""
    info: Mapping[str, Any] = {}
    try:
        info = await page.evaluate(_state_js(body_limit)) or {}
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 取不到正文就退回空态
        info = {}
    text = str(info.get("text") or "")[:body_limit]
    return State(
        url=url,
        title=title,
        text=text,
        body_text_hash=_hash_text(text),
        active_element=str(info.get("active") or ""),
        scroll_y=int(info.get("scroll_y") or 0),
        nodes=int(info.get("nodes") or -1),
    )


# ---------------------------------------------------------------------------
# 差分（六类）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    """动作前后差异（六类，全部来自前后两次 `State`，**不额外 evaluate**）。

    六类与验收口径的对应：`url`（URL）/ `title`（标题）/ `nodes`（AX 摘要）/
    `text`（正文摘要）/ `scroll`（scrollY）/ `focus`（状态读回：activeElement）。
    """

    url: bool = False
    title: bool = False
    nodes: bool = False
    text: bool = False
    scroll: bool = False
    focus: bool = False
    detail: str = ""

    @property
    def changed(self) -> bool:
        return any((self.url, self.title, self.nodes, self.text, self.scroll, self.focus))

    def to_payload(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "nodes": self.nodes,
            "text": self.text,
            "scroll": self.scroll,
            "focus": self.focus,
            "detail": self.detail,
        }


def ax_digest(snapshot: Any) -> str:
    """AX 摘要：把 `(role, name, checked, disabled)` 三元组序列哈希（6 类差分里的「AX 摘要」）。"""
    nodes = getattr(snapshot, "nodes", None) or ()
    parts: list[str] = []
    for node in nodes:
        parts.append(
            "|".join(
                (
                    str(getattr(node, "role", "") or ""),
                    str(getattr(node, "name", "") or ""),
                    "" if getattr(node, "checked", None) is None
                    else ("1" if getattr(node, "checked") else "0"),
                    "1" if getattr(node, "disabled", False) else "0",
                )
            )
        )
    return _hash_text("\n".join(parts))


def attach_snapshot(state: State, snapshot: Any) -> State:
    """把 AX 摘要挂到 `State` 上（差分用的「AX 摘要」一栏；快照缺席时保持空）。"""
    if snapshot is None:
        return state
    return State(
        url=state.url or str(getattr(snapshot, "url", "") or ""),
        title=state.title or str(getattr(snapshot, "title", "") or ""),
        text=state.text,
        body_text_hash=state.body_text_hash,
        active_element=state.active_element,
        scroll_y=state.scroll_y,
        nodes=len(getattr(snapshot, "nodes", ()) or ()) or state.nodes,
        ax_digest=ax_digest(snapshot),
        taken_at=state.taken_at,
    )


def diff(before: State, after: State) -> Delta:
    """比较两次状态；`detail` 给模型一句话（不含正文原文）。"""
    parts: list[str] = []
    url_changed = before.url != after.url
    if url_changed:
        parts.append("URL 变了")
    title_changed = before.title != after.title
    if title_changed:
        parts.append("标题变了")
    text_changed = before.body_text_hash != after.body_text_hash
    if text_changed:
        delta_len = after.text_len - before.text_len
        parts.append(f"正文 {delta_len:+d} 字" if delta_len else "正文变了")
    if before.ax_digest and after.ax_digest:
        nodes_changed = before.ax_digest != after.ax_digest
    else:
        nodes_changed = (
            before.nodes >= 0 and after.nodes >= 0 and before.nodes != after.nodes
        )
    if nodes_changed:
        parts.append(f"元素 {after.nodes - before.nodes:+d}")
    scroll_changed = abs(after.scroll_y - before.scroll_y) > SCROLL_EPSILON
    if scroll_changed:
        parts.append(f"滚动到 {after.scroll_y}px")
    focus_changed = before.active_element != after.active_element
    if focus_changed:
        parts.append(f"焦点 {after.active_element or '（空）'}")
    return Delta(
        url=url_changed,
        title=title_changed,
        nodes=nodes_changed,
        text=text_changed,
        scroll=scroll_changed,
        focus=focus_changed,
        detail=" / ".join(parts),
    )


def is_unchanged(delta: Delta) -> bool:
    """六类全 False ⇒ 没变（V3 的判据）。"""
    return not delta.changed


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """一次验证的结论。`outcome` 直接进台账 `status`（规格 §1.6）。"""

    ok: bool
    outcome: str
    reason: str = ""
    detail: str = ""
    candidates: tuple[str, ...] = ()
    delta: Delta | None = None
    rejection: Rejection | None = None
    checks: tuple[tuple[str, bool], ...] = ()

    def render(self) -> str:
        """观测块第三段（`【验证】`/`【无变化】`/`【未达成】`/`【拒绝】`）。"""
        if self.outcome == OUTCOME_NO_CHANGE:
            head = f"【无变化】{self.reason or '动作已发出但页面状态未变'}"
            if self.detail:
                head += f"（{self.detail}）"
            if self.candidates:
                head += "；候选：" + " ".join(self.candidates)
            return head
        if self.rejection is not None:
            return self.rejection.render()
        if self.outcome in (OUTCOME_VERIFIED, OUTCOME_CHANGED):
            head = f"【验证】{self.reason or '页面已变化'}"
            if self.outcome == OUTCOME_CHANGED and self.detail:
                head += f"：{self.detail}"
            return head
        if self.outcome == OUTCOME_SKIPPED:
            return f"【验证】{self.reason or '只读动作，不做差分'}"
        return f"【未达成】{self.reason or '期望未满足'}{f'（{self.detail}）' if self.detail else ''}"

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {"ok": self.ok, "outcome": self.outcome}
        if self.reason:
            payload["reason"] = self.reason
        if self.delta is not None:
            payload["delta"] = self.delta.to_payload()
        return payload


# ---------------------------------------------------------------------------
# expect 求值（V1）
# ---------------------------------------------------------------------------

#: 只读动作（不做页面差分，避免把「读正文」误判成 no_change）。
_READ_ONLY_KINDS = frozenset(
    {ActionKind.READ, ActionKind.SCREENSHOT, ActionKind.WAIT, ActionKind.DONE}
)


def _snapshot_names(snapshot: Any) -> list[tuple[str, str]]:
    """从 AX 快照里取 (role, name) 序列（**保留重复**：`expect.count` 要靠它数个数）。"""
    nodes = getattr(snapshot, "nodes", None) or ()
    out: list[tuple[str, str]] = []
    for node in nodes:
        role = str(getattr(node, "role", "") or "").lower()
        name = str(getattr(node, "name", "") or "")
        if role:
            out.append((role, name))
    return out


def _is_subsequence(haystack: str, needle: str) -> bool:
    return bool(needle) and needle in (haystack or "")


def evaluate_expect(
    expect: Expect, after: State, *, snapshot: Any = None
) -> tuple[bool, tuple[tuple[str, bool], ...], str]:
    """逐项求值 `expect`（返回 全部满足? / 每项结果 / 首个未满足项的可读文案）。"""
    checks: list[tuple[str, bool]] = []
    first_failure = ""

    def note(key: str, ok: bool, text: str) -> None:
        nonlocal first_failure
        checks.append((key, ok))
        if not ok and not first_failure:
            first_failure = text

    if expect.url_contains is not None:
        ok = _is_subsequence(after.url, expect.url_contains)
        note("url_contains", ok, f"url_contains 未满足（当前 {after.url[:120]}）")
    if expect.title_contains is not None:
        ok = _is_subsequence(after.title, expect.title_contains)
        note("title_contains", ok, f"title_contains 未满足（当前 {after.title[:80]}）")
    if expect.text_contains is not None:
        ok = _is_subsequence(after.text, expect.text_contains)
        note("text_contains", ok, f"text_contains 未满足（正文 {after.text_len} 字）")
    if expect.role is not None:
        pairs = _snapshot_names(snapshot)
        wanted = (str(expect.role).lower(), str(expect.name or ""))
        ok = wanted in pairs if pairs else False
        note("role+name", ok, f"AX 树里没有 [{wanted[0]}]「{wanted[1]}」")
    if expect.count is not None:
        pairs = _snapshot_names(snapshot)
        if pairs:
            actual = sum(1 for role, name in pairs if _match_count(expect, role, name))
        else:
            actual = -1
        ok = actual == expect.count
        note("count", ok, f"expect.count={expect.count} 未满足（当前 {actual}）")
    return (all(ok for _, ok in checks), tuple(checks), first_failure)


def _match_count(expect: Expect, role: str, name: str) -> bool:
    if expect.role is not None and role != str(expect.role).lower():
        return False
    if expect.name is not None and str(expect.name) not in name:
        return False
    return True


def ledger_status(verdict: Verdict) -> str:
    """把 `Verdict.outcome` 翻成台账 `status`（规格 §1.6 的五个词）。"""
    return LEDGER_STATUS.get(verdict.outcome, "failed")


# ---------------------------------------------------------------------------
# 主判定（V1 / V2 / V3）+ 重复守卫
# ---------------------------------------------------------------------------


def check(
    action: Action | None,
    before: State | None,
    after: State | None,
    *,
    snapshot: Any = None,
    guard: "RepeatGuard | None" = None,
    candidates: Iterable[str] = (),
) -> Verdict:
    """一次动作后的判定（规格 §5.1 三档）。纯函数：不碰 page，可离线单测。"""
    cands = tuple(candidates)

    if action is None:
        return Verdict(ok=True, outcome=OUTCOME_SKIPPED, reason="没有动作，只做观测")

    # 前置闸门：同一动作连续第 N 次无变化 ⇒ 直接拒绝（D2 根因 3），本步不执行
    if guard is not None:
        blocked = guard.before(action)
        if blocked is not None:
            return Verdict(
                ok=False,
                outcome=OUTCOME_REJECTED,
                reason=blocked.reason,
                rejection=blocked,
            )

    if action.kind in _READ_ONLY_KINDS:
        return Verdict(
            ok=True,
            outcome=OUTCOME_SKIPPED,
            reason=f"{action.kind.value} 不改变页面，不做差分",
        )

    expect = action.expect if action.expect is not None else None
    if expect is not None and not expect.is_empty():
        if after is None:
            return Verdict(ok=False, outcome=OUTCOME_FAILED, reason="缺少动作后状态，无法验证期望")
        ok, checks, failure = evaluate_expect(expect, after, snapshot=snapshot)
        if ok:
            verdict = Verdict(
                ok=True,
                outcome=OUTCOME_VERIFIED,
                reason="期望全部满足",
                checks=checks,
            )
        else:
            verdict = Verdict(
                ok=False,
                outcome=OUTCOME_FAILED,
                reason=failure or "期望未满足",
                checks=checks,
            )
        if guard is not None:
            guard.note(action.digest(), verdict.outcome)
        return verdict

    if before is None or after is None:
        return Verdict(ok=True, outcome=OUTCOME_SKIPPED, reason="缺少前后状态，跳过差分")

    delta = diff(before, after)
    if is_unchanged(delta):
        # V3：动作发出但页面没动 —— 必须显式说出来（D2 的根因 2）
        verdict = Verdict(
            ok=False,
            outcome=OUTCOME_NO_CHANGE,
            reason="动作已发出但页面状态未变（可能点到隐藏/被遮挡节点，或本来就没反应）",
            candidates=cands,
            delta=delta,
        )
    else:
        verdict = Verdict(
            ok=True,
            outcome=OUTCOME_CHANGED,
            reason="页面已变化",
            detail=delta.detail,
            delta=delta,
        )
    if guard is not None:
        guard.note(action.digest(), verdict.outcome)
    return verdict


@dataclass
class RepeatGuard:
    """同一动作连续第 N 次「无变化」⇒ 拒绝（规格 §5.3；D2 的根因 3）。

    - 只在**同一任务内**计数（实例随任务创建/丢弃；不用全局字典，避免跨会话串味）；
    - `no_change` 累加；`changed`/`verified` 归零；
      `failed`（定位失败/动作失败）**不累加也不归零** —— 「点不到」与「点了没反应」
      是两种问题，混在一起会误伤重试；
    - `threshold<=0` ⇒ 关闭（排障用）。
    """

    threshold: int = 3
    _streak: dict[str, int] = field(default_factory=dict)

    def key(self, action: Action) -> str:
        """规范化 key：动作种类 + target 摘要（不含 index）+ 值（不含 expect）。"""
        return action.digest()

    def before(self, action: Action) -> Rejection | None:
        """执行**前**的闸门：已经连续 N-1 次无变化 ⇒ 这次直接拒绝，不再执行。"""
        if self.threshold <= 0:
            return None
        streak = self._streak.get(self.key(action), 0)
        if streak + 1 < self.threshold:
            return None
        return self._reject(streak + 1)

    def note(self, digest: str, status: str) -> Rejection | None:
        """执行**后**记账（规格 §5.3 的签名）；累加到阈值时返回拒绝。"""
        if self.threshold <= 0:
            return None
        if status in _NO_CHANGE_STATUSES:
            streak = self._streak.get(digest, 0) + 1
            self._streak[digest] = streak
            if streak >= self.threshold:
                return self._reject(streak)
            return None
        if status in (OUTCOME_CHANGED, OUTCOME_VERIFIED):
            self._streak.pop(digest, None)
        return None

    def reset(self) -> None:
        self._streak.clear()

    def _reject(self, n: int) -> Rejection:
        return Rejection(
            code="E4",
            reason=f"同一动作已连续 {n} 次无变化",
            hint=(
                "换一种定位方式（label/text/testid/css 或用 mark 点），"
                "或先 browse_observe / browse_screenshot 看清页面再动手"
            ),
        )


# ---------------------------------------------------------------------------
# 定位器相关的等待 / 读回（需要真 page，全部 async）
# ---------------------------------------------------------------------------


async def wait_for_expect(
    page: Any,
    action: Action,
    resolution: Any = None,
    *,
    action_timeout_s: float | None = None,
) -> Verdict:
    """V1 里「状态/数量」这类**必须问 locator** 的期望（规格 §5.1 / §3.5）。

    `visible/hidden/attached/detached` 走 `locator.wait_for(state=…)`；
    `checked/unchecked/enabled/disabled` 走三路读回；`count` 走 `locator.count()`。
    超时值取 `min(expect.timeout_ms, action_timeout_s*1000)`，**不许**用期望绕过超时。
    """
    expect = action.expect
    if expect is None or expect.is_empty():
        return Verdict(ok=True, outcome=OUTCOME_SKIPPED, reason="没有 locator 类期望")

    locator = getattr(resolution, "locator", None)
    timeout_ms = expect.timeout_ms
    if action_timeout_s is not None:
        cap = int(max(0.0, action_timeout_s) * 1000)
        timeout_ms = cap if timeout_ms is None else min(timeout_ms, cap)

    checks: list[tuple[str, bool]] = []
    if expect.state is not None:
        state = expect.state
        if locator is None:
            return Verdict(
                ok=False,
                outcome=OUTCOME_FAILED,
                reason=f"expect.state={state} 需要可定位的元素，但本次没有 locator",
            )
        if state in ("visible", "hidden", "attached", "detached"):
            ok = await _wait_for_state(locator, state, timeout_ms)
            checks.append((f"state={state}", ok))
            if not ok:
                return Verdict(
                    ok=False,
                    outcome=OUTCOME_FAILED,
                    reason=f"expect.state={state} 在 {timeout_ms}ms 内未达成",
                    checks=tuple(checks),
                )
        else:
            current = await read_control_state(locator)
            want = {
                "checked": "checked",
                "unchecked": "unchecked",
                "enabled": "enabled",
                "disabled": "disabled",
            }[state]
            if want in ("checked", "unchecked"):
                ok = current == want
            else:
                enabled = await _is_enabled(locator)
                ok = enabled if want == "enabled" else not enabled
            checks.append((f"state={state}", ok))
            if not ok:
                return Verdict(
                    ok=False,
                    outcome=OUTCOME_FAILED,
                    reason=f"expect.state={state} 未达成（当前 {current or '未知'}）",
                    checks=tuple(checks),
                )
    if expect.count is not None and locator is not None:
        actual = await _safe_count(locator)
        ok = actual == expect.count
        checks.append(("count", ok))
        if not ok:
            return Verdict(
                ok=False,
                outcome=OUTCOME_FAILED,
                reason=f"expect.count={expect.count} 未达成（当前 {actual}）",
                checks=tuple(checks),
            )
    # `page` 目前只作「调用方必须显式传入」的占位（多帧/多页扩展会用到）
    return Verdict(ok=True, outcome=OUTCOME_VERIFIED, reason="期望全部满足", checks=tuple(checks))


async def _wait_for_state(locator: Any, state: str, timeout_ms: int | None) -> bool:
    try:
        if timeout_ms is None:
            await locator.wait_for(state=state)
        else:
            await locator.wait_for(state=state, timeout=timeout_ms)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 超时/元素不存在都算「未达成」
        return False


async def _is_enabled(locator: Any) -> bool:
    try:
        return bool(await locator.is_enabled())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


async def _safe_count(locator: Any) -> int:
    try:
        return int(await locator.count())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return -1


async def _attr(locator: Any, name: str) -> str:
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return ""
    try:
        value = await getter(name)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return ""
    return "" if value is None else str(value).strip()


async def read_control_state(locator: Any) -> str:
    """勾选态三路读回（D5）：`aria-checked` → 原生 `checked` → `data-state`。

    三路都读不到 ⇒ `"unknown"`（观测块提示「只能靠页面变化判断」，**不再**报
    `Not a checkbox or radio button`）。
    """
    for attr in ("aria-checked", "aria-pressed", "aria-selected"):
        raw = (await _attr(locator, attr)).lower()
        if raw in ("true", "false"):
            return "checked" if raw == "true" else "unchecked"
    is_checked = getattr(locator, "is_checked", None)
    if is_checked is not None:
        try:
            return "checked" if bool(await is_checked()) else "unchecked"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 非 checkbox 控件会抛，继续降级
            pass
    raw = (await _attr(locator, "data-state")).lower()
    if raw in ("on", "checked", "true", "open"):
        return "checked"
    if raw in ("off", "unchecked", "false", "closed"):
        return "unchecked"
    return "unknown"


async def check_value(locator: Any, expected: Any, *, timeout_s: float = 1.5) -> Verdict:
    """`type` 之后校验输入是否真的落到 value（**「点了成功」≠「状态变了」**）。

    轮询 `locator.input_value()` 直到 timeout_s；末值仍不符 ⇒ `failed` 并给出实际值。
    """
    want = "" if expected is None else str(expected)
    deadline = time.monotonic() + max(0.0, timeout_s)
    actual = ""
    while True:
        try:
            actual = str(await locator.input_value())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 不是输入控件
            return Verdict(
                ok=False,
                outcome=OUTCOME_FAILED,
                reason="该元素没有 value（不是输入控件）",
            )
        if actual == want:
            return Verdict(ok=True, outcome=OUTCOME_VERIFIED, reason="value 已生效")
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    return Verdict(
        ok=False,
        outcome=OUTCOME_FAILED,
        reason=f"输入未生效：期望 value={want!r}，实际 {actual[:60]!r}",
        detail=f"实际长度 {len(actual)} / 期望长度 {len(want)}",
    )


# ---------------------------------------------------------------------------
# 观测块渲染（唯一来源，铁律 3）
# ---------------------------------------------------------------------------

OBS_PREFIX = "【观测"
ACTION_PREFIX = "【动作】"
VERIFY_PREFIX = "【验证】"
BUDGET_PREFIX = "【预算】"
BUDGET_HINT_AT = 60.0  # 剩余预算 < 60s 才追加【预算】行（规格 §5.4）


#: 观测块默认最多给多少节点行（与 ``a11y.render_snapshot`` 的默认值一致；
#: 需要更多行时由调用方注入 ``node_lines``）。
OBS_MAX_LINES = 40


def _render_node(node: Any) -> "a11y.A11yNode":
    """单个节点的归一（``checked`` ⇄ ``checked_raw`` 双向补齐，见 ``_render_view``）。"""
    checked = getattr(node, "checked", None)
    raw = str(getattr(node, "checked_raw", "") or "")
    if not raw and checked is not None:
        raw = "true" if checked else "false"
    return a11y.A11yNode(
        ref=int(getattr(node, "ref", 0) or 0),
        role=str(getattr(node, "role", "") or ""),
        name=str(getattr(node, "name", "") or ""),
        value=str(getattr(node, "value", "") or ""),
        checked=checked if checked is not None else (raw == "true" if raw else None),
        checked_raw=raw,
        disabled=bool(getattr(node, "disabled", False)),
        expanded=getattr(node, "expanded", None),
        level=int(getattr(node, "level", 0) or 0),
        frame_path=tuple(getattr(node, "frame_path", ()) or ()),
        node_id=int(getattr(node, "node_id", 0) or 0),
        box=getattr(node, "box", None),
    )


def _render_view(snapshot: Any) -> tuple[Any, tuple[Any, ...]]:
    """把"看起来像快照"的对象补齐成 a11y 渲染器要的字段（鸭子类型适配）。

    为什么需要适配：``PageSnapshot`` 是冻结 dataclass，但单测与降级路径会传
    ``SimpleNamespace``（字段更少，``checked`` 而不是 ``checked_raw``）。渲染
    仍然只发生在 ``core/a11y`` 一处 —— 这里只做**数据归一**，不做格式决策。
    """
    nodes = tuple(_render_node(node) for node in (getattr(snapshot, "nodes", ()) or ()))
    view = a11y.PageSnapshot(
        url=str(getattr(snapshot, "url", "") or ""),
        title=str(getattr(snapshot, "title", "") or ""),
        viewport=tuple(getattr(snapshot, "viewport", (0, 0)) or (0, 0)),
        dpr=float(getattr(snapshot, "dpr", 1) or 1),
        scroll_y=int(getattr(snapshot, "scroll_y", 0) or 0),
        scroll_height=int(getattr(snapshot, "scroll_height", 0) or 0),
        nodes=nodes,
        pruned=int(getattr(snapshot, "pruned", 0) or 0),
        frames=int(getattr(snapshot, "frames", 1) or 1),
        text=str(getattr(snapshot, "text", "") or ""),
        ssrf_blocked=int(getattr(snapshot, "ssrf_blocked", 0) or 0),
        truncated=bool(getattr(snapshot, "truncated", False)),
        degraded=tuple(getattr(snapshot, "degraded", ()) or ()),
    )
    return view, nodes


def render_observation(
    *,
    step: int = 1,
    snapshot: Any = None,
    header: str = "",
    node_lines: Sequence[str] = (),
    action: Action | None = None,
    verdict: Verdict | None = None,
    remaining_s: float | None = None,
    note: str = "",
) -> str:
    """组装观测块（**唯一**渲染处）。三段固定顺序：观测 → 动作 → 验证。

    - 首行 / 节点行 / 附加行**全部**由 ``core/a11y`` 渲染（本函数只拼装三段）；
    - 无动作时只有第一段（`browse_observe` 的返回不含后两段）；
    - `【预算】` 只在 `remaining_s < 60` 时出现；
    - **不含** selector 路径、ref 编号、像素坐标、正文全文（正文只经 `read(text)` 给）。
    """
    view, nodes = _render_view(snapshot) if snapshot is not None else (None, ())
    head = header or (a11y.observation_header(view, step=step) if view is not None else "")
    first = f"{OBS_PREFIX} #{step}】{head}".rstrip() if not head.startswith(OBS_PREFIX) else head
    lines = [first]
    if node_lines:
        lines.extend(node_lines)
    elif view is not None:
        lines.extend(
            a11y.render_node_lines(nodes, max_lines=OBS_MAX_LINES).splitlines()
        )
    if view is not None:
        lines.extend(a11y.observation_notices(view))
    if note:
        lines.append(note)
    if action is not None:
        lines.append(f"{ACTION_PREFIX}{action.describe()}")
    if verdict is not None:
        lines.append(verdict.render())
    if remaining_s is not None and remaining_s < BUDGET_HINT_AT:
        lines.append(f"{BUDGET_PREFIX}剩余 {max(0, int(remaining_s))} 秒")
    return "\n".join(lines)
