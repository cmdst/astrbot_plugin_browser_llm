"""core.action_model — 结构化动作模型（v1.7.5 动作层；规格 §3）。

单一事实源：`ACTION_SPEC`（16 个动作的必填字段 / 值域 / 文档）。
**两面都由它生成**，禁止第二份手写清单（层纪律铁律 3）：

1. `ACTION_JSON_SCHEMA` —— draft-07 契约，与 `spec/v175-actions.schema.json`
   逐字段相等（`tests/test_actions_schema.py` 拿冻结文件比对；文件不在时跳过）；
2. `render_action_catalog()` —— 给内层模型看的动作目录（工具 description 取它）。

本模块是**纯 Python**：不 import playwright、不 import astrbot，
因此没有浏览器环境也能加载（层纪律 §1.2 铁律 1）。

运行期唯一闸门是 `parse_action()`（规格 §3.2 的结论：Schema 是契约文档 +
测试神谕，插件不新增 jsonschema 运行期依赖）。Schema 表达不了的三件事只在
`parse_action` 里做：mark 纪元（R-A9）、`index ≥ 匹配数`（R-A8，实际在
`resolve` 里判定，因为要数候选）、URL 安全策略（R-A11）。

**v1.8.0 两层修法（A1 P0）**：闸门**不放宽**，容错放在**工具边界**。`coerce_payload()`
（边缘层，唯一名字）只把模型偶发写成 JSON 字符串的载荷**解开一层**，
解不开就原值返回；真正的判定仍只发生在 `parse_action()`。生产 8/8 全拒的根因是模型把
`action` 参数传成了字符串（`R-A1 动作必须是 JSON 对象，收到 str`），不是闸门太严。

与规格的**两处显式偏离**（都在本文件里写死，评审可查）：

1. `Action.index` 默认 `None`（规格 §3.3 写的是 `0`）。理由：v1.6.x 的 D4
   （「第 2 个」在两个世界里指不同元素）与 v1.7.5 的歧义候选表都要求区分
   「模型没指定 index」与「模型明确要第 0 个」。`None` 在 JSON 里等价于
   字段缺省，Schema 不受影响；`index=0` 仍照旧表示第一个。
2. `parse_action` 额外接受一组**旧词别名**（`goto`/`fill`/`uncheck`/`tab_*`），
   归一化后才进入 16 枚举校验。别名是「预归一化层」，不进 Schema、不进
   `ACTION_SPEC`，未知动作仍按 R-A1 拒绝（见 `ACTION_ALIASES`）。

**规模说明**（对规格 §1.3 守则 320–450 行的偏离，实现后实测 ~1340 行）：本模块除了
dataclass + 校验，还内联了**冻结契约的 43 条 description 文案**与 **14 条拒绝码文案**
—— 它们必须与 `spec/v175-actions.schema.json` 逐字节相等（判据 §3-2 的硬门禁），
不能移到别处又不新增第四个模块。删掉这些字符串会直接让「生成物 == 冻结文件」失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "ACTION_ALIASES",
    "ACTION_JSON_SCHEMA",
    "ACTION_SPEC",
    "Action",
    "ActionKind",
    "ActionSpec",
    "Expect",
    "INDEX_CONVENTION",
    "KEY_CHOICES",
    "MAX_ACTION_BYTES",
    "MAX_CANDIDATES",
    "ParseContext",
    "READ_MODES",
    "REJECTION_CODES",
    "ROLE_CHOICES",
    "Rejection",
    "SCREENSHOT_MODES",
    "SCROLL_DIRECTIONS",
    "TAB_OPS",
    "Target",
    "canonical_json",
    "coerce_payload",
    "parse_action",
    "render_action_catalog",
]

# ---------------------------------------------------------------------------
# 上限常量（与 Schema 同源：改这里就同时改了 Schema 与 parse_action）
# ---------------------------------------------------------------------------

MAX_ACTION_BYTES = 2048  # R-A13：防提示注入塞大对象
_FENCE_LINES = ("```", "```json")  # 边缘容错认的围栏首行（小写比较）
MAX_NAME_CHARS = 200  # target.name
MAX_TARGET_TEXT_CHARS = 200  # target.label / text / testid
MAX_CSS_CHARS = 400  # target.css
MAX_URL_CHARS = 2000  # navigate 的 value
MAX_VALUE_CHARS = 4000  # type / done 的 value
MAX_SELECT_CHARS = 200  # select 的 value
MAX_WAIT_MS = 30000  # wait 的 value
MAX_INDEX = 500  # index / expect.count
MAX_MARK = 9999  # target.mark
MAX_AMOUNT = 5000  # amount
MAX_EXPECT_URL_CHARS = 300
MAX_EXPECT_TEXT_CHARS = 200
MAX_EXPECT_TIMEOUT_MS = 30000
MAX_CANDIDATES = 5  # 观测块最多列 5 条候选（规格 §3.4 R-A8）

# ---------------------------------------------------------------------------
# 枚举白名单（全部是 Schema 里直接出现的值域）
# ---------------------------------------------------------------------------

#: `target.role` 的取值域 = Playwright `get_by_role` 的 role 词表（82 个）。
ROLE_CHOICES: tuple[str, ...] = (
    "alert", "alertdialog", "application", "article", "banner", "blockquote", "button",
    "caption", "cell", "checkbox", "code", "columnheader", "combobox", "complementary",
    "contentinfo", "definition", "deletion", "dialog", "directory", "document", "emphasis",
    "feed", "figure", "form", "generic", "grid", "gridcell", "group", "heading", "img",
    "insertion", "link", "list", "listbox", "listitem", "log", "main", "marquee", "math",
    "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio", "meter", "navigation",
    "none", "note", "option", "paragraph", "presentation", "progressbar", "radio", "radiogroup",
    "region", "row", "rowgroup", "rowheader", "scrollbar", "search", "searchbox", "separator",
    "slider", "spinbutton", "status", "strong", "subscript", "superscript", "switch", "tab",
    "table", "tablist", "tabpanel", "term", "textbox", "time", "timer", "toolbar", "tooltip",
    "tree", "treegrid", "treeitem",
)

#: `press` 的键名白名单（13 个；`Space` 是 Playwright 的叫法）。
KEY_CHOICES: tuple[str, ...] = (
    "Enter", "Escape", "Tab", "Backspace", "Delete", "ArrowUp", "ArrowDown",
    "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space",
)

#: `scroll` 的方向白名单。
SCROLL_DIRECTIONS: tuple[str, ...] = ("down", "up", "page_down", "page_up", "top", "bottom")

#: `tab` 的子操作。
TAB_OPS: tuple[str, ...] = ("new", "switch", "close", "list")

#: `read` 的读取模式。
READ_MODES: tuple[str, ...] = ("text", "links")

#: `screenshot` 的取图范围。
SCREENSHOT_MODES: tuple[str, ...] = ("viewport", "full")

#: `expect.state` 的状态词（visible/hidden/attached/detached 走 wait_for，其余读属性）。
EXPECT_STATES: tuple[str, ...] = (
    "visible", "hidden", "attached", "detached",
    "checked", "unchecked", "enabled", "disabled",
)

#: `expect` 的合法键（顺序即 Schema 里的 properties 顺序）。
EXPECT_KEYS: tuple[str, ...] = (
    "url_contains", "title_contains", "text_contains",
    "role", "name", "state", "count", "timeout_ms",
)

#: 索引口径（写死，禁止再引入第三种；规格 §4.1）。
INDEX_CONVENTION: dict[str, str] = {
    "element_index": "0 起算（click/type/select/check/hover/read 的 index）",
    "tab_index": "1 起算（tab 动作 switch/close）",
    "mark_id": "Set-of-Mark 标签 1 起算，由插件生成、模型只读",
    "reason": (
        "元素 0 起算沿用 v1.7.0（click_text/click_selector/checkbox/index），"
        "标签页 1 起算沿用 v1.6.x 的 browse_switch_tab；两处口径写死在此表，"
        "禁止再引入第三种"
    ),
}

#: 14 条拒绝码（Schema 的 `x-rejection-codes`，与 `parse_action` / `resolve` 共用）。
REJECTION_CODES: dict[str, str] = {
    "R-A1": "未知字段 / 未知 action",
    "R-A2": "缺必填字段（按 action 的条件必填）",
    "R-A3": "target 同时给出多种定位策略",
    "R-A4": "role 缺 name（role-only 目标不可判定）",
    "R-A5": "字符串长度越界（target 200 / value 4000 / css 400）",
    "R-A6": "value 类型或枚举不符（press/scroll/read/tab/screenshot/wait 的白名单）",
    "R-A7": "index 越界或负数",
    "R-A8": "index ≥ 匹配数（附候选清单）",
    "R-A9": "mark 过期（不在最近一次 SoM 截图的 mark 纪元内）",
    "R-A10": (
        "css 含 Playwright 引擎前缀或 xpath/js 注入"
        "（xpath= / text= / >> / :has-text( / :text( / javascript:）"
    ),
    "R-A11": "navigate 的 URL 未过安全策略（非 http/https / 内网 / 禁词 / 长度）",
    "R-A12": "未知 expect 键 / expect 空对象",
    "R-A13": "动作超过 2KB 序列化上限（防提示注入塞大对象）",
    "R-A14": "预算耗尽（remaining < 1s）时不再接受新动作",
}

#: css 逃生舱里**禁止**出现的引擎前缀 / 注入形态（R-A10）。
_CSS_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "xpath=", "text=", ">>", ":has-text(", ":text(", "javascript:", "css=", "id=",
)

#: 旧词别名 → (正式 action, 强制 value)。预归一化层，不进 Schema / ACTION_SPEC。
ACTION_ALIASES: dict[str, tuple[str, Any]] = {
    "goto": ("navigate", None),
    "open": ("navigate", None),
    "fill": ("type", None),
    "input": ("type", None),
    "uncheck": ("check", False),
    "tab_new": ("tab", "new"),
    "tab_switch": ("tab", "switch"),
    "tab_close": ("tab", "close"),
    "tab_list": ("tab", "list"),
}


# ---------------------------------------------------------------------------
# 枚举与动作规格（唯一事实源）
# ---------------------------------------------------------------------------


class ActionKind(str, Enum):
    """16 个动作。声明顺序 == Schema 里 `enum` / `allOf` 的顺序（逐字段比对要用）。"""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    CHECK = "check"
    PRESS = "press"
    SCROLL = "scroll"
    HOVER = "hover"
    BACK = "back"
    FORWARD = "forward"
    TAB = "tab"
    READ = "read"
    SCREENSHOT = "screenshot"
    WAIT = "wait"
    RELOAD = "reload"
    DONE = "done"


@dataclass(frozen=True)
class ActionSpec:
    """一个动作的元数据。Schema、工具描述、运行期校验**都从这里派生**。"""

    kind: ActionKind
    required: tuple[str, ...]  # 条件必填字段（Schema 的 allOf.then.required）
    value_type: str | None  # "str" / "int" / "bool" / "enum" / None
    value_choices: tuple[str, ...] = ()  # enum 白名单（value_type == "enum"）
    allows_target: bool = False
    allows_submit: bool = False
    allows_amount: bool = False
    value_min: int | None = None  # minLength（str）/ minimum（int）
    value_max: int | None = None  # maxLength（str）/ maximum（int）
    doc: str = ""  # 唯一一句人读语义；Schema 的 x-action-kinds.semantics 取它

    # ---- Schema 派生 -------------------------------------------------

    def value_schema(self) -> dict | None:
        """`allOf.then.properties.value` 的子 Schema（None ⇒ 该动作无 value）。"""
        if self.value_type is None:
            return None
        if self.value_type == "enum":
            return {"type": "string", "enum": list(self.value_choices)}
        if self.value_type == "str":
            schema: dict[str, Any] = {"type": "string"}
            if self.value_min is not None:
                schema["minLength"] = self.value_min
            if self.value_max is not None:
                schema["maxLength"] = self.value_max
            return schema
        if self.value_type == "int":
            schema = {"type": "integer"}
            if self.value_min is not None:
                schema["minimum"] = self.value_min
            if self.value_max is not None:
                schema["maximum"] = self.value_max
            return schema
        if self.value_type == "bool":
            return {"type": "boolean"}
        raise AssertionError(f"未知 value_type: {self.value_type!r}")

    def then_schema(self) -> dict:
        """`allOf[].then`：条件必填 + 该动作**不接受**的字段（写成 false）。"""
        props: dict[str, Any] = {}
        value_schema = self.value_schema()
        if value_schema is not None:
            props["value"] = value_schema
        if not self.allows_submit:
            props["submit"] = False
        if not self.allows_amount:
            props["amount"] = False
        if not self.allows_target:
            props["target"] = False
        return {"required": list(self.required), "properties": props}

    @property
    def json_value_type(self) -> str | None:
        """Schema 词汇里的值类型（"str"→"string"，"enum"→"string"）。"""
        return {
            "str": "string",
            "int": "integer",
            "bool": "boolean",
            "enum": "string",
            None: None,
        }[self.value_type]

    @property
    def forbidden_fields(self) -> tuple[str, ...]:
        """该动作不接受的顶层字段（`then.properties` 里写成 false 的那几个）。"""
        out: list[str] = []
        if not self.allows_submit:
            out.append("submit")
        if not self.allows_amount:
            out.append("amount")
        if not self.allows_target:
            out.append("target")
        return tuple(out)


_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        kind=ActionKind.NAVIGATE,
        required=("value",),
        value_type="str",
        value_min=1,
        value_max=MAX_URL_CHARS,
        allows_target=True,
        doc="打开 URL（http/https）。等价 v1.7.0 的 browse_open / browse_new_tab(url)",
    ),
    ActionSpec(
        kind=ActionKind.CLICK,
        required=("target",),
        value_type=None,
        allows_target=True,
        doc="点击 target；受信任事件默认走 CDP Input.dispatchMouseEvent",
    ),
    ActionSpec(
        kind=ActionKind.TYPE,
        required=("target", "value"),
        value_type="str",
        value_min=None,
        value_max=MAX_VALUE_CHARS,
        allows_target=True,
        allows_submit=True,
        doc="向 target 输入文本；trusted_events 时走 CDP Input.insertText，否则 locator.fill",
    ),
    ActionSpec(
        kind=ActionKind.SELECT,
        required=("target", "value"),
        value_type="str",
        value_min=1,
        value_max=MAX_SELECT_CHARS,
        allows_target=True,
        doc="选择 target 所指 select 的选项（label/value/index 三选一，按 label→value→序号解析）",
    ),
    ActionSpec(
        kind=ActionKind.CHECK,
        required=("target",),
        value_type="bool",
        allows_target=True,
        doc="勾选/取消 target（checkbox/radio/switch/按钮式选项）；value 缺省 true",
    ),
    ActionSpec(
        kind=ActionKind.PRESS,
        required=("value",),
        value_type="enum",
        value_choices=KEY_CHOICES,
        doc=(
            "按键（白名单）：Enter/Escape/Tab/Backspace/Delete/ArrowUp/Down/Left/Right/"
            "Home/End/PageUp/PageDown/Space"
        ),
    ),
    ActionSpec(
        kind=ActionKind.SCROLL,
        required=("value",),
        value_type="enum",
        value_choices=SCROLL_DIRECTIONS,
        allows_target=True,
        allows_amount=True,
        doc="滚动方向：down/up/page_down/page_up/top/bottom；amount 覆盖像素步长",
    ),
    ActionSpec(
        kind=ActionKind.HOVER,
        required=("target",),
        value_type=None,
        allows_target=True,
        doc="悬停 target（触发悬浮菜单）；走 CDP mouseMoved",
    ),
    ActionSpec(
        kind=ActionKind.BACK,
        required=(),
        value_type=None,
        doc="后退",
    ),
    ActionSpec(
        kind=ActionKind.FORWARD,
        required=(),
        value_type=None,
        doc="前进",
    ),
    ActionSpec(
        kind=ActionKind.TAB,
        required=("value",),
        value_type="enum",
        value_choices=TAB_OPS,
        doc=(
            "标签页操作：new/switch/close/list；new 需 url（可省=about:blank），"
            "switch/close 需 index（1 起）"
        ),
    ),
    ActionSpec(
        kind=ActionKind.READ,
        required=("value",),
        value_type="enum",
        value_choices=READ_MODES,
        doc="读取页面：text（正文）/links（链接表）；不改变页面状态",
    ),
    ActionSpec(
        kind=ActionKind.SCREENSHOT,
        required=(),
        value_type="enum",
        value_choices=SCREENSHOT_MODES,
        allows_target=True,
        doc="SoM 截图兜底：viewport（默认）/full；返回 ImageContent + 标签表",
    ),
    ActionSpec(
        kind=ActionKind.WAIT,
        required=("value",),
        value_type="int",
        value_min=0,
        value_max=MAX_WAIT_MS,
        doc="显式等待毫秒（0..30000）；一般不需要——每次动作都自带等待与验证",
    ),
    ActionSpec(
        kind=ActionKind.RELOAD,
        required=(),
        value_type=None,
        doc="重新加载当前页（navigate 到同一 URL 不等价：会丢 POST 状态）",
    ),
    ActionSpec(
        kind=ActionKind.DONE,
        required=("value",),
        value_type="str",
        value_min=1,
        value_max=MAX_VALUE_CHARS,
        doc="结束任务并给出最终结论（内层代理唯一合法终止动作）",
    ),
)

#: **唯一事实源**：ActionKind → ActionSpec（顺序 == 枚举顺序）。
ACTION_SPEC: dict[ActionKind, ActionSpec] = {spec.kind: spec for spec in _SPECS}

assert len(ACTION_SPEC) == 16, "ACTION_SPEC 必须恰好 16 条"


# ---------------------------------------------------------------------------
# JSON Schema（由 ACTION_SPEC 生成，不手抄）
# ---------------------------------------------------------------------------

SCHEMA_ID = (
    "https://github.com/cmdst/astrbot_plugin_browser_llm/spec/v175-actions.schema.json"
)
SCHEMA_TITLE = "astrbot_plugin_browser_llm v1.7.5 结构化动作（内层代理输出）"
SCHEMA_DESCRIPTION = (
    "内层代理每步**只输出一个**动作对象（不是动作数组）。字段集固定为 "
    "action / target / value / index / expect / submit / amount，"
    "additionalProperties=false ⇒ 多一个字段就是拒绝（R-A1）。本文件是"
    "**契约文档 + 测试神谕**；运行期真正的闸门是 core.action_model.parse_action()，"
    "两者不一致时以 parse_action 为准，并把该用例补进 "
    "tests/test_v175_action_model.py 的判定表。"
)

DESC_TARGET = (
    "定位目标。**恰好一种定位策略**：role(+name) / label / text / testid / css / mark。"
    "多种同时出现=拒绝（R-A3）。"
)
DESC_ROLE = "ARIA role（Playwright get_by_role 的取值域，已收敛到可交互/可读角色）"
DESC_NAME = "可访问名（accessibility name）：role 策略下必填；与观测块里的 name 逐字一致"
DESC_LABEL = "表单控件的 label 文本（get_by_label）"
DESC_TEXT = "可见文本（get_by_text，子串匹配）"
DESC_TESTID = "data-testid / data-test / data-qa 的值（get_by_test_id）"
DESC_CSS = (
    "裸 CSS 选择器（逃生舱）。禁止 Playwright 引擎前缀：xpath= / text= / >> / "
    ":has-text( / :text( / js="
)
DESC_MARK = (
    "Set-of-Mark 标签 ID。**只在最近一次 browse_screenshot(SoM) 之后的下一个动作有效**"
    "（mark 纪元），过期即拒绝（R-A9）"
)
DESC_VALUE = (
    "动作主操作数；按 action 解释（见 x-action-kinds）。"
    "navigate/type/select/press/scroll/read/tab/screenshot/search/done 取字符串或整数，"
    "check 取布尔。"
)
DESC_INDEX = (
    "多个元素匹配 target 时取第 index 个（**0 起算**，与 v1.7.0 的 "
    "click_text/click_selector/checkbox 一致）。超出匹配数=拒绝并附候选（R-A8）。"
    "标签页 index 例外：**1 起算**（tab 动作）"
)
DESC_EXPECT = "动作后置条件（验证层用）。缺省时验证层走默认差分（§5.1）。"
DESC_SUBMIT = "仅 type：输入后追加 Enter（搜索框常用）。受信任事件下等价 CDP dispatchKeyEvent"
DESC_AMOUNT = "仅 scroll：本次滚动像素；缺省按视口高度的 0.8 倍"
DESC_EXPECT_TEXT_CONTAINS = "页面正文（extract 后）包含该子串"
DESC_EXPECT_ROLE = "与 name 合用：断言 A11y 树里出现该 role+name 节点"
DESC_EXPECT_STATE = (
    "状态断言；visible/hidden/attached/detached 走 wait_for(state=)，其余读属性"
)
DESC_EXPECT_COUNT = "目标匹配数（与 role/name 或 target 合用）"
DESC_EXPECT_TIMEOUT = (
    "本动作的额外等待上限；运行期再夹到 min(该值, resources.action_timeout_s*1000)"
)


def _build_target_schema() -> dict:
    properties: dict[str, Any] = {
        "role": {"type": "string", "enum": list(ROLE_CHOICES), "description": DESC_ROLE},
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_CHARS,
            "description": DESC_NAME,
        },
        "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_TARGET_TEXT_CHARS,
            "description": DESC_LABEL,
        },
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_TARGET_TEXT_CHARS,
            "description": DESC_TEXT,
        },
        "testid": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_TARGET_TEXT_CHARS,
            "description": DESC_TESTID,
        },
        "css": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CSS_CHARS,
            "description": DESC_CSS,
        },
        "mark": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MARK,
            "description": DESC_MARK,
        },
    }
    # oneOf 强制「恰好一种定位策略」：required 自己 + not(anyOf 其余)
    strategies: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("role", ("role", "name")),
        ("label", ("label",)),
        ("text", ("text",)),
        ("testid", ("testid",)),
        ("css", ("css",)),
        ("mark", ("mark",)),
    )
    one_of: list[dict] = []
    for key, required in strategies:
        if key == "role":
            # role 分支：role+name 视为同一策略，互斥项是其余五种策略
            others = [other for other, _ in strategies if other != key]
        else:
            # 其余分支：role 与 name 各自都不能出现（role-only / name-only 都不成立）
            others = ["role", "name"] + [
                other for other, _ in strategies if other not in (key, "role")
            ]
        one_of.append(
            {
                "required": list(required),
                "not": {"anyOf": [{"required": [other]} for other in others]},
            }
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "description": DESC_TARGET,
        "properties": properties,
        "oneOf": one_of,
    }


def _build_expect_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "minProperties": 1,
        "description": DESC_EXPECT,
        "properties": {
            "url_contains": {"type": "string", "maxLength": MAX_EXPECT_URL_CHARS},
            "title_contains": {"type": "string", "maxLength": MAX_EXPECT_TEXT_CHARS},
            "text_contains": {
                "type": "string",
                "maxLength": MAX_EXPECT_TEXT_CHARS,
                "description": DESC_EXPECT_TEXT_CONTAINS,
            },
            "role": {"type": "string", "description": DESC_EXPECT_ROLE},
            "name": {"type": "string", "maxLength": MAX_EXPECT_TEXT_CHARS},
            "state": {
                "type": "string",
                "enum": list(EXPECT_STATES),
                "description": DESC_EXPECT_STATE,
            },
            "count": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_INDEX,
                "description": DESC_EXPECT_COUNT,
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_EXPECT_TIMEOUT_MS,
                "description": DESC_EXPECT_TIMEOUT,
            },
        },
    }


def build_action_json_schema() -> dict:
    """由 `ACTION_SPEC` 生成 draft-07 契约（与冻结的 spec 文件逐字段相等）。"""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": SCHEMA_ID,
        "title": SCHEMA_TITLE,
        "description": SCHEMA_DESCRIPTION,
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": [kind.value for kind in ActionKind]},
            "target": _build_target_schema(),
            "value": {
                "description": DESC_VALUE,
                "oneOf": [
                    {"type": "string", "maxLength": MAX_VALUE_CHARS},
                    {"type": "integer"},
                    {"type": "boolean"},
                ],
            },
            "index": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_INDEX,
                "description": DESC_INDEX,
            },
            "expect": _build_expect_schema(),
            "submit": {"type": "boolean", "description": DESC_SUBMIT},
            "amount": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_AMOUNT,
                "description": DESC_AMOUNT,
            },
        },
        "allOf": [
            {
                "if": {
                    "required": ["action"],
                    "properties": {"action": {"const": spec.kind.value}},
                },
                "then": spec.then_schema(),
            }
            for spec in ACTION_SPEC.values()
        ],
        "x-rejection-codes": dict(REJECTION_CODES),
        "x-action-kinds": {
            spec.kind.value: {
                "value_type": spec.json_value_type,
                "semantics": spec.doc,
                "required": list(spec.required),
            }
            for spec in ACTION_SPEC.values()
        },
        "x-index-convention": dict(INDEX_CONVENTION),
    }


#: 生成的 draft-07 契约（唯一来自 ACTION_SPEC）。
ACTION_JSON_SCHEMA: dict = build_action_json_schema()


def render_action_catalog() -> str:
    """动作目录（给内层模型看的纯文本；工具 description 直接取它）。

    格式固定为 `· <kind>  <必填>  <doc>`，避免各处再手写一份动作清单。
    """
    lines: list[str] = []
    for kind, spec in ACTION_SPEC.items():
        required = "、".join(spec.required) if spec.required else "无必填"
        lines.append(f"· {kind.value}  必填：{required}  {spec.doc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


class _Missing:
    """哨兵：区分「字段没给」与「给了 None」。"""


MISSING = _Missing()


def canonical_json(obj: Any) -> str:
    """稳定序列化（sort_keys + 紧凑分隔符；digest 与字节上限共用）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Target:
    """定位目标：**恰好一种**策略（role+name / label / text / testid / css / mark）。

    零种或多种的判定在 `parse_action` 里（拒绝码 R-A3/R-A4）；
    `strategy()` / `digest()` 只服务已合法的对象，非法时抛 `ValueError`。
    """

    role: str | None = None
    name: str | None = None
    label: str | None = None
    text: str | None = None
    testid: str | None = None
    css: str | None = None
    mark: int | None = None

    def strategy(self) -> str:
        """返回唯一策略名：role / label / text / testid / css / mark。"""
        present = [
            key
            for key in ("role", "label", "text", "testid", "css", "mark")
            if getattr(self, key) is not None
        ]
        if len(present) != 1:
            raise ValueError(
                f"target 必须有且只有一种定位方式，实际有 {len(present)} 种：{present}"
            )
        return present[0]

    def digest(self) -> str:
        """规范化摘要（repeat_guard 用）：strategy + 值 + name；**不含 index**。"""
        try:
            strategy = self.strategy()
        except ValueError:
            return "target:invalid:" + "|".join(
                f"{key}={getattr(self, key)!r}"
                for key in ("role", "name", "label", "text", "testid", "css", "mark")
                if getattr(self, key) is not None
            )
        if strategy == "role":
            return f"role:{self.role}:name:{self.name}"
        return f"{strategy}:{getattr(self, strategy)}"

    def to_payload(self) -> dict:
        """回吐为 JSON 口径（只带非 None 字段）。"""
        payload: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None:
                payload[field.name] = value
        return payload

    def is_empty(self) -> bool:
        return self.strategy_or_none() is None

    def strategy_or_none(self) -> str | None:
        try:
            return self.strategy()
        except ValueError:
            return None


@dataclass(frozen=True)
class Expect:
    """动作后置条件（验证层输入契约；规格 §3.5）。"""

    url_contains: str | None = None
    title_contains: str | None = None
    text_contains: str | None = None
    role: str | None = None
    name: str | None = None
    state: str | None = None
    count: int | None = None
    timeout_ms: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Expect":
        return cls(
            url_contains=payload.get("url_contains"),
            title_contains=payload.get("title_contains"),
            text_contains=payload.get("text_contains"),
            role=payload.get("role"),
            name=payload.get("name"),
            state=payload.get("state"),
            count=payload.get("count"),
            timeout_ms=payload.get("timeout_ms"),
        )

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None:
                payload[field.name] = value
        return payload

    def is_empty(self) -> bool:
        return not self.to_payload()


@dataclass(frozen=True)
class Action:
    """一个已校验的动作（运行期对象）。

    `index` 默认 `None` == 「模型没指定」（见模块 docstring 的偏离说明 1）。
    `normalized_from` 是运行期元数据（别名归一化前的原名），不进 payload / digest。
    """

    kind: ActionKind
    target: Target | None = None
    value: str | int | bool | None = None
    index: int | None = None
    expect: Expect | None = None
    submit: bool = False
    amount: int | None = None
    normalized_from: str = ""

    def digest(self) -> str:
        """canonical json → sha1[:16]；**不含 expect，也不含 index**。"""
        body = {
            "action": self.kind.value,
            "target": self.target.to_payload() if self.target else None,
            "value": self.value,
            "submit": bool(self.submit),
            "amount": self.amount,
        }
        return hashlib.sha1(canonical_json(body).encode("utf-8")).hexdigest()[:16]

    def to_payload(self) -> dict:
        """回吐为 JSON 口径（缺省字段不出现；index 只有模型给过才出现）。"""
        payload: dict[str, Any] = {"action": self.kind.value}
        if self.target is not None:
            payload["target"] = self.target.to_payload()
        if self.value is not None:
            payload["value"] = self.value
        if self.index is not None:
            payload["index"] = self.index
        if self.expect is not None and not self.expect.is_empty():
            payload["expect"] = self.expect.to_payload()
        if self.submit:
            payload["submit"] = True
        if self.amount is not None:
            payload["amount"] = self.amount
        return payload

    def describe(self) -> str:
        """`【动作】` 行用的短标签（不给 selector 路径、不给像素坐标）。"""
        parts = [self.kind.value]
        if self.target is not None:
            strategy = self.target.strategy_or_none()
            if strategy == "role":
                parts.append(f"target=[{self.target.role}]「{self.target.name}」")
            elif strategy == "css":
                # 规格 §5.4：观测/动作行**不含 selector 路径**（否则模型会照着选择器猜）
                parts.append("target=css（选择器不回显）")
            elif strategy == "mark":
                parts.append(f"target=mark={self.target.mark}")
            elif strategy is None:
                parts.append("target=<非法>")
            else:
                parts.append(f"target={strategy}「{getattr(self.target, strategy)}」")
        if self.value is not None:
            shown = self.value
            if isinstance(shown, str) and len(shown) > 40:
                shown = shown[:40] + "…"
            parts.append(f'value="{shown}"')
        if self.index is not None:
            parts.append(f"index={self.index}")
        if self.submit:
            parts.append("submit=true")
        return " ".join(parts)


@dataclass(frozen=True)
class Rejection:
    """结构化拒绝（**永不抛裸异常**，规格 §3.4 / §1.5 E1/E2）。"""

    code: str  # R-A1..R-A14（resolve 侧另有 E2_* 细分，见 resolve.py）
    reason: str  # 人读中文（进观测块）
    hint: str = ""  # 给模型的纠正建议
    candidates: tuple[str, ...] = ()  # 候选清单（R-A8 用；最多 5 条）

    @property
    def header(self) -> str:
        """观测块前缀：与规格 §3.4 的文案模板对齐。"""
        if self.code == "R-A8":
            return "【未找到】"
        if self.code == "R-A14":
            return "【超时】"
        return "【拒绝】"

    def render(self) -> str:
        """一行观测文本：`【拒绝】R-A1 …`（建议另起一行，模型更好读）。"""
        head = f"{self.header}{self.code} {self.reason}".rstrip()
        if self.candidates:
            head += "；候选：" + " ".join(self.candidates)
        if self.hint:
            head += f"\n　建议：{self.hint}"
        return head

    def to_payload(self) -> dict:
        payload: dict[str, Any] = {"code": self.code, "reason": self.reason}
        if self.hint:
            payload["hint"] = self.hint
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        return payload


@dataclass(frozen=True)
class ParseContext:
    """`parse_action` 的运行期上下文（都可由调用方注入，便于单测）。

    - `mark_epoch`：最近一次 SoM 截图的 {mark_id: A11yNode}；None/空 ⇒ 任何 mark 都按 R-A9 拒；
    - `url_policy`：URL 安全策略包装（返回 None 表示通过，返回字符串表示拒绝原因）；
    - `max_bytes`：R-A13 的字节上限；
    - `remaining_s` / `steps`：预算（R-A14）。
    """

    mark_epoch: Mapping[int, Any] | None = None
    url_policy: Callable[[str], str | None] | None = None
    max_bytes: int = MAX_ACTION_BYTES
    remaining_s: float | None = None
    steps: int = 0


# ---------------------------------------------------------------------------
# 运行期闸门
# ---------------------------------------------------------------------------


def _reject(code: str, reason: str, *, hint: str = "", candidates: Iterable[str] = ()) -> Rejection:
    return Rejection(
        code=code,
        reason=reason,
        hint=hint,
        candidates=tuple(candidates)[:MAX_CANDIDATES],
    )


def _normalize_alias(raw: dict, normalized_from: list[str]) -> None:
    """旧词别名归一化（`goto`→`navigate` 等）；就地修改 `raw`。"""
    action = raw.get("action")
    if not isinstance(action, str):
        return
    alias = ACTION_ALIASES.get(action.strip().lower())
    if alias is None:
        return
    kind, forced_value = alias
    normalized_from.append(action)
    raw["action"] = kind
    if forced_value is not None and "value" not in raw:
        raw["value"] = forced_value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_target(payload: Mapping[str, Any]) -> tuple[Target | None, Rejection | None]:
    """R-A3 / R-A4 + target 字段自身类型（结构错误一律结构化返回）。"""
    raw_target = payload.get("target")
    if raw_target is None:
        return None, None
    if not isinstance(raw_target, Mapping):
        return None, _reject(
            "R-A3",
            f"target 必须是对象，收到 {type(raw_target).__name__}",
            hint='示例：{"action":"click","target":{"role":"button","name":"提交"}}',
        )
    known = {field.name for field in fields(Target)}
    unknown = [key for key in raw_target if key not in known]
    if unknown:
        return None, _reject(
            "R-A3",
            f"target 里出现未知字段 {sorted(unknown)}；可用：{sorted(known)}",
        )
    target = Target(
        role=raw_target.get("role"),
        name=raw_target.get("name"),
        label=raw_target.get("label"),
        text=raw_target.get("text"),
        testid=raw_target.get("testid"),
        css=raw_target.get("css"),
        mark=raw_target.get("mark"),
    )
    present = [
        key for key in ("role", "label", "text", "testid", "css", "mark")
        if getattr(target, key) is not None
    ]
    if not present:
        return None, _reject(
            "R-A3",
            "target 里没有任何定位方式",
            hint="六选一：{role+name} / {label} / {text} / {testid} / {css} / {mark}",
        )
    if len(present) > 1:
        return None, _reject(
            "R-A3",
            f"target 只能给一种定位方式，收到：{' + '.join(present)}",
            hint="role 必须与 name 成对；不要同时给 label/text/css。",
        )
    if "role" in present and not target.name:
        return None, _reject(
            "R-A4",
            "role 必须与 name 一起用（页面上的「按钮」可能有 30 个）",
            hint='要按文本点用 {"text": "…"}；要按语义点用 {"role": "button", "name": "…"}',
        )
    if not isinstance(target.role, str) and target.role is not None:
        return None, _reject("R-A3", f"target.role 必须是字符串，收到 {type(target.role).__name__}")
    return target, None


def _check_target_lengths(target: Target | None) -> Rejection | None:
    if target is None:
        return None
    limits = {
        "name": MAX_NAME_CHARS,
        "label": MAX_TARGET_TEXT_CHARS,
        "text": MAX_TARGET_TEXT_CHARS,
        "testid": MAX_TARGET_TEXT_CHARS,
        "css": MAX_CSS_CHARS,
    }
    for key, limit in limits.items():
        value = getattr(target, key)
        if value is None:
            continue
        if not isinstance(value, str):
            return _reject("R-A6", f"target.{key} 必须是字符串，收到 {type(value).__name__}")
        if len(value) > limit:
            return _reject("R-A5", f"target.{key} 超过 {limit} 字符（{len(value)}）")
    if target.role is not None and target.role not in ROLE_CHOICES:
        return _reject(
            "R-A6",
            f"target.role「{target.role}」不在 role 白名单（82 个）",
            hint="先用 browse_observe 看页面上的 [role] 名称，照抄。",
        )
    if target.mark is not None:
        if not _is_int(target.mark):
            return _reject("R-A6", f"target.mark 必须是整数，收到 {type(target.mark).__name__}")
        if not 1 <= target.mark <= MAX_MARK:
            return _reject("R-A7", f"target.mark 必须在 1..{MAX_MARK}")
    return None


def _check_css(target: Target | None) -> Rejection | None:
    if target is None or target.css is None:
        return None
    lowered = target.css.strip().lower()
    for token in _CSS_FORBIDDEN_TOKENS:
        if token in lowered:
            return _reject(
                "R-A10",
                f"css 只接受裸 CSS 选择器（命中禁止形态「{token}」）",
                hint="要按文本点请用 text/role；要按路径点请用 testid。",
            )
    if lowered.startswith("//") or lowered.startswith("javascript:"):
        return _reject(
            "R-A10",
            "css 只接受裸 CSS 选择器（不接受 // 开头的 XPath 或 javascript:）",
            hint="要按文本点请用 text/role。",
        )
    return None


def _check_value(spec: ActionSpec, value: Any) -> Rejection | None:
    """R-A6：value 的类型 / 枚举 / 长度（按 ACTION_SPEC 的 value_schema）。"""
    if spec.value_type is None:
        return None
    if spec.value_type == "bool":
        if not isinstance(value, bool):
            return _reject(
                "R-A6",
                f"{spec.kind.value} 的 value 必须是布尔（收到 {type(value).__name__}）",
                hint='要取消勾选用 {"action":"check","value":false,"target":{…}}',
            )
        return None
    if spec.value_type == "int":
        if not _is_int(value):
            return _reject(
                "R-A6",
                f"{spec.kind.value} 的 value 必须是整数（收到 {type(value).__name__}）",
            )
        if spec.value_min is not None and value < spec.value_min:
            return _reject("R-A6", f"{spec.kind.value} 的 value 不能小于 {spec.value_min}")
        if spec.value_max is not None and value > spec.value_max:
            return _reject("R-A6", f"{spec.kind.value} 的 value 不能大于 {spec.value_max}")
        return None
    # str / enum
    if not isinstance(value, str):
        return _reject(
            "R-A6",
            f"{spec.kind.value} 的 value 必须是字符串（收到 {type(value).__name__}）",
        )
    if spec.value_type == "enum" and value not in spec.value_choices:
        return _reject(
            "R-A6",
            f"value「{value}」不在白名单：{'/'.join(spec.value_choices)}",
            hint=f'示例：{{"action":"{spec.kind.value}","value":"{spec.value_choices[0]}"}}',
        )
    if spec.value_min is not None and len(value) < spec.value_min:
        return _reject("R-A6", f"{spec.kind.value} 的 value 不能为空")
    if spec.value_max is not None and len(value) > spec.value_max:
        return _reject(
            "R-A5",
            f"{spec.kind.value} 的 value 超过 {spec.value_max} 字符（{len(value)}）",
            hint="长文本拆成多次 type。",
        )
    return None


def _check_expect(raw_expect: Any) -> tuple[Expect | None, Rejection | None]:
    """R-A12：expect 结构 / 未知键 / 空对象 / role 缺 name。"""
    if raw_expect is None:
        return None, None
    if not isinstance(raw_expect, Mapping):
        return None, _reject(
            "R-A12", f"expect 必须是对象，收到 {type(raw_expect).__name__}"
        )
    if not raw_expect:
        return None, _reject("R-A12", "expect 不能是空对象（要么不给，要么给至少一个键）")
    unknown = [key for key in raw_expect if key not in EXPECT_KEYS]
    if unknown:
        return None, _reject(
            "R-A12",
            f"expect 未知键 {sorted(unknown)}；可用：{'/'.join(EXPECT_KEYS)}",
        )
    expect = Expect.from_payload(raw_expect)
    if expect.role is not None and expect.name is None:
        return None, _reject(
            "R-A12",
            "expect.role 必须与 expect.name 合用（只给 role 无法判定是哪一个）",
        )
    if expect.state is not None and expect.state not in EXPECT_STATES:
        return None, _reject(
            "R-A12",
            f"expect.state「{expect.state}」不在 {'/'.join(EXPECT_STATES)}",
        )
    if expect.count is not None:
        if not _is_int(expect.count) or not 0 <= expect.count <= MAX_INDEX:
            return None, _reject("R-A12", f"expect.count 必须是 0..{MAX_INDEX} 的整数")
    if expect.timeout_ms is not None:
        if not _is_int(expect.timeout_ms) or not 0 <= expect.timeout_ms <= MAX_EXPECT_TIMEOUT_MS:
            return None, _reject(
                "R-A12", f"expect.timeout_ms 必须是 0..{MAX_EXPECT_TIMEOUT_MS} 的整数"
            )
    for key, limit in (
        ("url_contains", MAX_EXPECT_URL_CHARS),
        ("title_contains", MAX_EXPECT_TEXT_CHARS),
        ("text_contains", MAX_EXPECT_TEXT_CHARS),
        ("name", MAX_EXPECT_TEXT_CHARS),
    ):
        value = getattr(expect, key)
        if value is None:
            continue
        if not isinstance(value, str):
            return None, _reject("R-A12", f"expect.{key} 必须是字符串")
        if len(value) > limit:
            return None, _reject("R-A5", f"expect.{key} 超过 {limit} 字符（{len(value)}）")
    return expect, None


def coerce_payload(payload: Any) -> Any:
    """边缘容错（**不是闸门**）：把模型偶发写成 JSON 字符串的载荷解开一层。

    契约（四条，逐条有测试，见 ``tests/test_v180_action_payload.py``）：① ``Mapping`` ⇒ **原对象
    返回**（``is`` 同一性成立，零拷贝）；② ``bytes`` / ``bytearray`` ⇒ 按 UTF-8 解码
    （``errors="replace"``）再按字符串处理；③ ``str`` ⇒ 去空白 → 去掉**一层** ``` 围栏
    （首行 ``` 或 ```json、末行 ```）→ 空串返回原值 → ``json.loads`` ⇒ 结果是 ``Mapping``
    才返回它，否则返回原值；④ 其它类型与**任何异常** ⇒ 原值返回。只解一层：**不做递归、
    不做第二次 json.loads**（双重编码仍交给闸门按 R-A1 拒）；绝不抛异常、绝不改写入参。

    实现约束（规格 §4.2 C-1..C-6）：``json.loads`` 最多 1 次、解析结果必须是 ``Mapping``
    才采用、围栏只去首尾各一行、空串走原值、``except Exception`` 且**不记日志**（日志在
    调用点记一次）、Mapping 分支零处理。
    """
    if isinstance(payload, Mapping):
        return payload  # C-6：绝大多数情况零成本，同一性成立
    try:
        text: str | None = None
        if isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            text = payload
        if text is None:
            return payload  # 契约 4：其它类型原样
        body = text.strip()
        if not body:
            return payload  # C-4：空串/纯空白交给闸门给准确 R-A1
        lines = body.splitlines()
        if (len(lines) >= 2 and lines[-1].strip() == "```"
                and lines[0].strip().lower() in _FENCE_LINES):
            body = "\n".join(lines[1:-1]).strip()  # C-3：只去首尾各一行
            if not body:
                return payload
        parsed = json.loads(body)  # C-1：全函数最多这一次
        return parsed if isinstance(parsed, Mapping) else payload  # C-2
    except Exception:  # noqa: BLE001 — C-5：容错层不制造噪声、绝不抛
        return payload




def parse_action(payload: Any, *, ctx: ParseContext | None = None) -> Action | Rejection:
    """**运行期唯一闸门**：把模型的原始 payload 变成 `Action` 或 `Rejection`。

    绝不抛裸异常（`Rejection` 是唯一失败形态）。检查顺序（固定，可测）：

    R-A14 预算 → R-A1 结构/动作名 → R-A13 字节 → R-A3 target 策略 →
    R-A4 role 缺 name → R-A2 条件必填 → 「该动作不接受此字段」→ R-A5 长度 →
    R-A6 值域 → R-A10 css → R-A7 index → R-A12 expect → R-A9 mark 纪元 →
    R-A11 URL 安全策略。
    """
    ctx = ctx or ParseContext()

    # 1) R-A14：预算耗尽直接终态，不再解析
    if ctx.remaining_s is not None and ctx.remaining_s < 1.0:
        return _reject(
            "R-A14",
            f"预算耗尽，不再接受新动作（{ctx.steps} 步已记台账）",
            hint="直接给 done 结论，或让主 LLM 起新的浏览任务。",
        )

    # 2) R-A1：必须是对象。**判据与码义一字未改**（规格 §4.3 唯一授权改动 = 下面 hint 追加一句
    #    提醒）：闸门不认字符串，字符串只能由边缘 ``coerce_payload`` 解开一层再进来；
    #    放宽这里等于让「不是对象」整类失败消失。
    if not isinstance(payload, Mapping):
        return _reject(
            "R-A1", f"动作必须是 JSON 对象，收到 {type(payload).__name__}",
            hint=('示例：{"action":"click","target":{"role":"button","name":"提交"}}；'
                  "请直接传对象，不要传 JSON 字符串、不要加引号或代码围栏"))
    raw: dict[str, Any] = dict(payload)
    normalized_from: list[str] = []
    _normalize_alias(raw, normalized_from)

    # 3) R-A13：字节上限（在别名归一化之后，防止别名撑大对象）
    size = len(canonical_json(raw).encode("utf-8"))
    if size > ctx.max_bytes:
        return _reject(
            "R-A13",
            f"动作对象过大（{size} B > {ctx.max_bytes} B）",
            hint="把长文本拆成多次 type，或只传必要字段。",
        )

    # 4) R-A1：动作名
    action_name = raw.get("action")
    if action_name is None:
        return _reject(
            "R-A1",
            "缺少 action 字段；可用：" + "/".join(kind.value for kind in ActionKind),
        )
    if not isinstance(action_name, str) or action_name not in {k.value for k in ActionKind}:
        return _reject(
            "R-A1",
            f"未知动作 {action_name!r}",
            hint="可用：" + "/".join(kind.value for kind in ActionKind),
        )
    kind = ActionKind(action_name)
    spec = ACTION_SPEC[kind]

    # 5) R-A1：顶层未知字段
    known_top = {"action", "target", "value", "index", "expect", "submit", "amount"}
    unknown_top = [key for key in raw if key not in known_top]
    if unknown_top:
        return _reject(
            "R-A1",
            f"顶层出现未知字段 {sorted(unknown_top)}",
            hint=f"字段集固定为：{'/'.join(sorted(known_top))}",
        )

    # 6) R-A3 / R-A4：target
    target, rejection = _check_target(raw)
    if rejection is not None:
        return rejection

    # 7) R-A2：该动作的必填字段
    for field_name in spec.required:
        if field_name == "target":
            if target is None:
                return _reject(
                    "R-A2",
                    f"{kind.value} 缺少 target",
                    hint="给 target{role+name} 或 {text}/{label}/{testid}/{css}/{mark}。",
                )
        elif raw.get(field_name) is None:
            return _reject("R-A2", f"{kind.value} 缺少 {field_name}")

    # 8) R-A1（字段矩阵）：该动作不接受的字段
    for field_name in spec.forbidden_fields:
        if field_name in raw and raw[field_name] is not None:
            return _reject(
                "R-A1",
                f"{kind.value} 不接受 {field_name} 字段",
                hint=f"字段矩阵见 §3.1：{kind.value} 的必填是 {list(spec.required) or '无'}。",
            )

    # 9) R-A5：target 长度（另含 R-A6 类型）
    rejection = _check_target_lengths(target)
    if rejection is not None:
        return rejection

    # 10) R-A6：value 类型 / 枚举 / 长度
    value = raw.get("value")
    if value is not None:
        rejection = _check_value(spec, value)
        if rejection is not None:
            return rejection
    elif spec.value_type is not None:
        value = None  # 已由必填检查拦下；此处保持 None

    # 11) R-A10：css 逃生舱
    rejection = _check_css(target)
    if rejection is not None:
        return rejection

    # 12) R-A7：index
    index = raw.get("index")
    if index is not None:
        if not _is_int(index):
            return _reject("R-A7", f"index 必须是整数，收到 {type(index).__name__}")
        if not 0 <= index <= MAX_INDEX:
            return _reject("R-A7", f"index 必须在 0..{MAX_INDEX}（收到 {index}）")

    # 13) R-A12：expect
    expect, rejection = _check_expect(raw.get("expect"))
    if rejection is not None:
        return rejection

    # 14) R-A9：mark 纪元
    if target is not None and target.mark is not None:
        epoch = ctx.mark_epoch or {}
        if target.mark not in epoch:
            hint = (
                "请先调 browse_screenshot 拿标签表，再按编号点；"
                "像素坐标在 v1.7.5 已废弃。"
            )
            if not epoch:
                return _reject(
                    "R-A9",
                    f"mark={target.mark} 已过期或从未截图（当前没有有效 mark 纪元）",
                    hint=hint,
                )
            return _reject(
                "R-A9",
                f"mark={target.mark} 不在本次截图的标签表（有效编号 {len(epoch)} 个）",
                hint=hint,
            )

    # 15) R-A11：navigate 的 URL 安全策略
    if kind is ActionKind.NAVIGATE and isinstance(value, str) and ctx.url_policy is not None:
        reason = ctx.url_policy(value)
        if reason:
            return _reject(
                "R-A11",
                f"URL 被安全策略拒绝：{reason}",
                hint="只能打开公网 http/https 地址。",
            )
    if kind is ActionKind.NAVIGATE and isinstance(value, str):
        if len(value) > MAX_URL_CHARS:
            return _reject("R-A5", f"navigate 的 URL 超过 {MAX_URL_CHARS} 字符")
        if not value.lower().startswith(("http://", "https://")):
            return _reject(
                "R-A11",
                "URL 必须以 http:// 或 https:// 开头",
                hint="不要用 javascript:/file:/data: —— 一律拒绝。",
            )

    # 16) 归一化：check 的 value 缺省 true
    if kind is ActionKind.CHECK and value is None:
        value = True

    return Action(
        kind=kind,
        target=target,
        value=value,
        index=index,
        expect=expect,
        submit=bool(raw.get("submit") or False),
        amount=raw.get("amount"),
        normalized_from=normalized_from[0] if normalized_from else "",
    )


def schema_bytes(payload: Action | Mapping[str, Any]) -> int:
    """payload 的 canonical 字节数（R-A13 复算 / 台账用）。"""
    body = payload.to_payload() if isinstance(payload, Action) else payload
    return len(canonical_json(body).encode("utf-8"))


def spec_for(kind: ActionKind | str) -> ActionSpec:
    """按枚举或字符串取 ActionSpec（`kind` 非法时抛 KeyError，仅供已校验路径用）。"""
    if isinstance(kind, str):
        kind = ActionKind(kind)
    return ACTION_SPEC[kind]


def iter_specs() -> Sequence[ActionSpec]:
    """按声明顺序遍历 16 条 ActionSpec（工具描述 / 测试用）。"""
    return tuple(ACTION_SPEC.values())
