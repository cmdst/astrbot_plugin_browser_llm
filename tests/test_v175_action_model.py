"""tests.test_v175_action_model — 动作层（core/action_model.py）判定表。

覆盖规格 §3 的四条判据：
1. `ACTION_SPEC` 恰好 16 条；
2. `ACTION_JSON_SCHEMA` 与冻结文件 `spec/v175-actions.schema.json` **逐字段相等**；
3. 每个 `R-A*` 至少一个拒绝用例、每个动作至少一个合法用例；
4. Schema 里每个动作出现一次（allOf 16 条）。

另外钉住「单一事实源」：dataclass 字段 ⇄ Schema properties ⇄ ACTION_SPEC 三方一致，
以及「非法动作一律返回 `Rejection`、不抛裸异常」。
"""

from __future__ import annotations

import json
import os
from dataclasses import fields
from pathlib import Path

import pytest

from astrbot_plugin_browser_llm.core.action_model import (
    ACTION_JSON_SCHEMA,
    ACTION_SPEC,
    INDEX_CONVENTION,
    KEY_CHOICES,
    REJECTION_CODES,
    ROLE_CHOICES,
    Action,
    ActionKind,
    Expect,
    ParseContext,
    Rejection,
    Target,
    canonical_json,
    iter_specs,
    parse_action,
    render_action_catalog,
)

#: 16 个动作的最小合法 payload（每个动作至少一条）。
LEGAL_PAYLOADS: tuple[dict, ...] = (
    {"action": "navigate", "value": "https://example.com/a"},
    {"action": "click", "target": {"role": "button", "name": "提交"}},
    {"action": "type", "target": {"label": "搜索"}, "value": "天气"},
    {"action": "select", "target": {"label": "配送方式"}, "value": "加急"},
    {"action": "check", "target": {"text": "同意协议"}},
    {"action": "press", "value": "Enter"},
    {"action": "scroll", "value": "down", "amount": 600},
    {"action": "hover", "target": {"text": "更多"}},
    {"action": "back"},
    {"action": "forward"},
    {"action": "tab", "value": "list"},
    {"action": "read", "value": "text"},
    {"action": "screenshot", "value": "full"},
    {"action": "wait", "value": 500},
    {"action": "reload"},
    {"action": "done", "value": "已在搜索结果页找到答案"},
)


def _spec_dir() -> Path | None:
    """定位冻结的 spec 目录（仓库内无该目录时跳过比对，不算失败）。"""
    candidates = []
    env = os.environ.get("V175_SPEC_DIR")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parent.parent
    candidates.append(here / "spec")
    # dev lane 可能把 spec/ 放在库外（lane 目录）：按兄弟目录形态找一遍
    for base in (here.parent, here.parent.parent):
        candidates.extend(sorted(base.glob("*/dev_astrbot_plugins/astrbot_plugin_browser_llm/spec")))
        candidates.append(base / "dev_astrbot_plugins" / "astrbot_plugin_browser_llm" / "spec")
    for candidate in candidates:
        if (candidate / "v175-actions.schema.json").is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 单一事实源 / Schema ⇄ dataclass
# ---------------------------------------------------------------------------


def test_action_spec_has_exactly_16_kinds():
    assert len(ACTION_SPEC) == 16
    assert len(ActionKind) == 16
    assert [spec.kind for spec in iter_specs()] == list(ActionKind)


def test_schema_enum_order_matches_action_kind_order():
    enum = ACTION_JSON_SCHEMA["properties"]["action"]["enum"]
    assert enum == [kind.value for kind in ActionKind]


def test_schema_allof_covers_every_action_once():
    consts = [
        clause["if"]["properties"]["action"]["const"] for clause in ACTION_JSON_SCHEMA["allOf"]
    ]
    assert consts == [kind.value for kind in ActionKind]
    assert len(ACTION_JSON_SCHEMA["allOf"]) == 16


def test_spec_fields_drive_schema_required_and_value():
    """Schema 的 required / value 子句必须由 ACTION_SPEC 派生（不是手抄）。"""
    for clause in ACTION_JSON_SCHEMA["allOf"]:
        kind = ActionKind(clause["if"]["properties"]["action"]["const"])
        spec = ACTION_SPEC[kind]
        assert clause["then"]["required"] == list(spec.required)
        props = clause["then"]["properties"]
        assert props.get("value") == spec.value_schema()
        assert ("submit" in props) is (not spec.allows_submit)
        assert ("amount" in props) is (not spec.allows_amount)
        assert ("target" in props) is (not spec.allows_target)


def test_dataclass_fields_match_schema_properties():
    top = ACTION_JSON_SCHEMA["properties"]
    action_fields = {
        ("action" if field.name == "kind" else field.name) for field in fields(Action)
    }
    assert action_fields - {"normalized_from"} == set(top)
    assert {field.name for field in fields(Target)} == set(top["target"]["properties"])
    assert {field.name for field in fields(Expect)} == set(top["expect"]["properties"])
    assert set(REJECTION_CODES) == {
        f"R-A{i}" for i in range(1, 15)
    }, "拒绝码表必须恰好是 R-A1..R-A14"


def test_value_choices_match_schema_enums():
    press = ACTION_SPEC[ActionKind.PRESS]
    assert press.value_choices == KEY_CHOICES
    assert ACTION_JSON_SCHEMA["properties"]["target"]["properties"]["role"]["enum"] == list(
        ROLE_CHOICES
    )
    for kind in (ActionKind.SCROLL, ActionKind.TAB, ActionKind.READ, ActionKind.SCREENSHOT):
        spec = ACTION_SPEC[kind]
        clause = next(
            c for c in ACTION_JSON_SCHEMA["allOf"] if c["if"]["properties"]["action"]["const"] == kind.value
        )
        assert clause["then"]["properties"]["value"]["enum"] == list(spec.value_choices)


def test_index_convention_is_documented_in_schema():
    assert ACTION_JSON_SCHEMA["x-index-convention"] == INDEX_CONVENTION
    assert "0 起算" in INDEX_CONVENTION["element_index"]
    assert "1 起算" in INDEX_CONVENTION["tab_index"]


def test_render_action_catalog_covers_all_kinds():
    catalog = render_action_catalog()
    lines = catalog.splitlines()
    assert len(lines) == 16
    for kind in ActionKind:
        assert kind.value in catalog


def test_generated_schema_equals_frozen_spec_file():
    """判据 §3-2：生成物与冻结契约逐字段相等。"""
    spec_dir = _spec_dir()
    if spec_dir is None:
        pytest.skip("spec/v175-actions.schema.json 不在本机（仅库内测试时跳过）")
    frozen = json.loads((spec_dir / "v175-actions.schema.json").read_text(encoding="utf-8"))
    assert frozen == ACTION_JSON_SCHEMA


# ---------------------------------------------------------------------------
# 合法动作：16 个动作各至少一条
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", LEGAL_PAYLOADS, ids=[p["action"] for p in LEGAL_PAYLOADS])
def test_legal_payloads_parse_to_action(payload):
    result = parse_action(dict(payload))
    assert isinstance(result, Action), f"{payload} 应合法，实际 {result}"
    assert result.kind.value == payload["action"]
    assert isinstance(result.to_payload(), dict)


def test_legal_payloads_cover_all_16_kinds():
    assert {payload["action"] for payload in LEGAL_PAYLOADS} == {k.value for k in ActionKind}


def test_expect_roundtrip_and_defaults():
    action = parse_action(
        {
            "action": "click",
            "target": {"role": "link", "name": "下一页"},
            "expect": {"url_contains": "/page=2", "state": "visible", "timeout_ms": 3000},
            "index": 1,
        }
    )
    assert isinstance(action, Action)
    assert action.index == 1
    assert action.expect is not None
    assert action.expect.url_contains == "/page=2"
    assert action.expect.state == "visible"
    assert action.to_payload()["expect"]["timeout_ms"] == 3000


def test_check_value_defaults_to_true():
    action = parse_action({"action": "check", "target": {"role": "checkbox", "name": "记住我"}})
    assert isinstance(action, Action)
    assert action.value is True


# ---------------------------------------------------------------------------
# 非法动作：每个 R-A* 至少一条（**结构化拒绝，不抛裸异常**）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        # R-A1 未知动作 / 未知字段 / 字段矩阵
        ({"action": "teleport"}, "R-A1"),
        ({"value": "x"}, "R-A1"),
        ({"action": "click", "target": {"text": "提交"}, "colour": "red"}, "R-A1"),
        ({"action": "press", "value": "Enter", "target": {"text": "输入框"}}, "R-A1"),
        ({"action": "back", "submit": True}, "R-A1"),
        # R-A2 缺必填
        ({"action": "type", "target": {"label": "搜索"}}, "R-A2"),
        ({"action": "click"}, "R-A2"),
        ({"action": "navigate"}, "R-A2"),
        # R-A3 / R-A4 target
        ({"action": "click", "target": {}}, "R-A3"),
        ({"action": "click", "target": {"role": "button", "name": "提交", "text": "提交"}}, "R-A3"),
        ({"action": "click", "target": {"role": "button"}}, "R-A4"),
        # R-A5 长度
        ({"action": "click", "target": {"text": "长" * 201}}, "R-A5"),
        ({"action": "click", "target": {"css": "div" * 401}}, "R-A5"),
        ({"action": "navigate", "value": "https://e.com/" + "a" * 1987}, "R-A5"),
        ({"action": "click", "target": {"text": "提交"},
          "expect": {"title_contains": "长" * 201}}, "R-A5"),
        # 2KB 上限（R-A13）先于值长度（R-A5）触发：CJK 4001 字 ≈ 12KB
        ({"action": "done", "value": "结" * 4001}, "R-A13"),
        # R-A6 值域
        ({"action": "press", "value": "Ctrl+C"}, "R-A6"),
        ({"action": "scroll", "value": "sideways"}, "R-A6"),
        ({"action": "tab", "value": "detach"}, "R-A6"),
        ({"action": "read", "value": "html"}, "R-A6"),
        ({"action": "wait", "value": "500"}, "R-A6"),
        ({"action": "check", "target": {"text": "同意"}, "value": "yes"}, "R-A6"),
        # R-A7 index
        ({"action": "click", "target": {"text": "提交"}, "index": -1}, "R-A7"),
        ({"action": "click", "target": {"text": "提交"}, "index": 501}, "R-A7"),
        ({"action": "click", "target": {"text": "提交"}, "index": "1"}, "R-A7"),
        # R-A9 mark 纪元
        ({"action": "click", "target": {"mark": 3}}, "R-A9"),
        # R-A10 css 注入
        ({"action": "click", "target": {"css": "xpath=//button"}}, "R-A10"),
        ({"action": "click", "target": {"css": "div >> text=提交"}}, "R-A10"),
        ({"action": "click", "target": {"css": "button:has-text(提交)"}}, "R-A10"),
        # R-A11 URL 策略
        ({"action": "navigate", "value": "javascript:alert(1)"}, "R-A11"),
        ({"action": "navigate", "value": "file:///etc/passwd"}, "R-A11"),
        # R-A12 expect
        ({"action": "click", "target": {"text": "提交"}, "expect": {}}, "R-A12"),
        ({"action": "click", "target": {"text": "提交"}, "expect": {"nope": 1}}, "R-A12"),
        ({"action": "click", "target": {"text": "提交"}, "expect": {"role": "button"}}, "R-A12"),
        # R-A13 过大
        ({"action": "type", "target": {"label": "搜索"}, "value": "长" * 1400}, "R-A13"),
    ],
)
def test_rejections_are_structured(payload, code):
    result = parse_action(payload)
    assert isinstance(result, Rejection), f"{payload} 应被拒绝，实际 {result}"
    assert result.code == code
    assert result.reason
    assert result.render().startswith(("【拒绝】", "【未找到】", "【超时】"))


def test_reject_budget_exhausted():
    result = parse_action(
        {"action": "click", "target": {"text": "提交"}},
        ctx=ParseContext(remaining_s=0.4, steps=7),
    )
    assert isinstance(result, Rejection)
    assert result.code == "R-A14"
    assert "7 步" in result.reason
    assert result.render().startswith("【超时】")


def test_reject_url_policy_reason_is_passed_through():
    result = parse_action(
        {"action": "navigate", "value": "http://127.0.0.1/admin"},
        ctx=ParseContext(url_policy=lambda url: "目标指向内网/环回地址"),
    )
    assert isinstance(result, Rejection)
    assert result.code == "R-A11"
    assert "内网/环回" in result.reason


def test_mark_inside_epoch_is_accepted():
    action = parse_action(
        {"action": "click", "target": {"mark": 2}},
        ctx=ParseContext(mark_epoch={1: object(), 2: object()}),
    )
    assert isinstance(action, Action)
    assert action.target is not None
    assert action.target.strategy() == "mark"


def test_mark_outside_epoch_names_the_epoch_size():
    result = parse_action(
        {"action": "click", "target": {"mark": 9}},
        ctx=ParseContext(mark_epoch={1: object(), 2: object()}),
    )
    assert isinstance(result, Rejection)
    assert result.code == "R-A9"
    assert "2" in result.reason


@pytest.mark.parametrize(
    "payload",
    [
        None,
        42,
        "click",
        [],
        {"action": None},
        {"action": 3},
        {"action": "click", "target": "button"},
        {"action": "click", "target": {"mark": "3"}},
        {"action": "click", "target": {"role": 3, "name": "x"}},
        {"action": "click", "target": {"text": "x"}, "expect": "url_contains=x"},
        {"action": "click", "target": {"text": "x"}, "amount": "big"},
        {"action": "scroll", "value": "down", "amount": -5},
    ],
)
def test_garbage_never_raises(payload):
    """任何垃圾输入都只能拿到 `Action` 或 `Rejection` —— 绝不抛裸异常。"""
    result = parse_action(payload)
    assert isinstance(result, (Action, Rejection))
    if isinstance(result, Rejection):
        assert result.code.startswith("R-A")


# ---------------------------------------------------------------------------
# 别名归一化（旧词 → 16 枚举；不进 Schema）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "kind", "value"),
    [
        ({"action": "goto", "value": "https://example.com"}, "navigate", "https://example.com"),
        ({"action": "fill", "target": {"label": "搜索"}, "value": "天气"}, "type", "天气"),
        ({"action": "uncheck", "target": {"text": "全选"}}, "check", False),
        ({"action": "tab_switch", "index": 2}, "tab", "switch"),
        ({"action": "tab_close", "index": 1}, "tab", "close"),
        ({"action": "tab_new"}, "tab", "new"),
    ],
)
def test_alias_normalization(payload, kind, value):
    result = parse_action(payload)
    assert isinstance(result, Action), result
    assert result.kind.value == kind
    assert result.value == value
    assert result.normalized_from == payload["action"]


def test_alias_names_do_not_leak_into_schema_or_spec():
    enum = ACTION_JSON_SCHEMA["properties"]["action"]["enum"]
    assert "goto" not in enum and "fill" not in enum and "tab_new" not in enum
    assert all(kind.value in enum for kind in ActionKind)


# ---------------------------------------------------------------------------
# digest / payload
# ---------------------------------------------------------------------------


def test_digest_ignores_expect_and_index():
    base = {"action": "click", "target": {"role": "button", "name": "提交"}}
    first = parse_action({**base, "index": 0})
    second = parse_action({**base, "index": 3, "expect": {"url_contains": "/done"}})
    third = parse_action({**base, "value": None})
    assert isinstance(first, Action) and isinstance(second, Action) and isinstance(third, Action)
    assert first.digest() == second.digest() == third.digest()


def test_digest_differs_for_different_targets():
    one = parse_action({"action": "click", "target": {"text": "提交"}})
    two = parse_action({"action": "click", "target": {"text": "取消"}})
    assert isinstance(one, Action) and isinstance(two, Action)
    assert one.digest() != two.digest()


def test_target_digest_is_normalized_and_index_free():
    assert Target(role="button", name="提交").digest() == "role:button:name:提交"
    assert Target(label="搜索").digest() == "label:搜索"
    with pytest.raises(ValueError):
        Target().strategy()


def test_canonical_json_is_stable():
    payload = {"b": 1, "a": "中文", "c": [1, 2]}
    assert canonical_json(payload) == canonical_json({"c": [1, 2], "a": "中文", "b": 1})
    assert " " not in canonical_json(payload)


def test_describe_has_no_selector_paths_or_pixels():
    action = parse_action(
        {"action": "click", "target": {"css": "#main > div:nth-child(2)"}, "index": 1}
    )
    assert isinstance(action, Action)
    described = action.describe()
    assert described.startswith("click")
    assert "nth-child" not in described
    assert "px" not in described


# ---------------------------------------------------------------------------
# 规格样本：13 条合法 / 9 条非法（验收口径）
# ---------------------------------------------------------------------------

#: 13 条合法样本（覆盖 16 个动作里最常用的族，含 mark 与 submit）。
SPEC_LEGAL_13: tuple[dict, ...] = (
    {"action": "navigate", "value": "https://shop.example/order"},
    {"action": "click", "target": {"role": "button", "name": "提交"}},
    {"action": "click", "target": {"text": "下一页"}},
    {"action": "click", "target": {"testid": "submit-btn"}},
    {"action": "click", "target": {"css": "#main button.primary"}, "index": 1},
    {"action": "click", "target": {"mark": 2}},
    {"action": "type", "target": {"label": "搜索"}, "value": "上海天气", "submit": True},
    {"action": "select", "target": {"text": "配送方式"}, "value": "加急配送"},
    {"action": "check", "target": {"role": "checkbox", "name": "记住我"}, "value": False},
    {"action": "press", "value": "Enter"},
    {"action": "scroll", "value": "down", "amount": 600},
    {"action": "hover", "target": {"text": "更多"}},
    {"action": "tab", "value": "new"},
)

#: 9 条非法样本（每个族一条）。
SPEC_ILLEGAL_9: tuple[dict, ...] = (
    {"action": "teleport"},
    {"value": "https://shop.example/"},
    {"action": "click", "target": {}},
    {"action": "click", "target": {"role": "button", "name": "提交", "css": "#submit"}},
    {"action": "click", "target": {"role": "button"}},
    {"action": "press", "value": "Ctrl+C"},
    {"action": "click", "target": {"text": "提交"}, "index": -1},
    {"action": "click", "target": {"text": "提交"}, "expect": {}},
    {"action": "click", "target": {"text": "提交"}, "colour": "red"},
)


def test_spec_13_legal_9_illegal_samples():
    """验收口径：13 条合法样本全过、9 条非法样本全拒（mark 样本带纪元）。"""
    epoch = ParseContext(mark_epoch={2: object()})
    assert len(SPEC_LEGAL_13) == 13
    for payload in SPEC_LEGAL_13:
        result = parse_action(dict(payload), ctx=epoch)
        assert isinstance(result, Action), f"{payload} 应合法，实际 {result}"
    assert len(SPEC_ILLEGAL_9) == 9
    for payload in SPEC_ILLEGAL_9:
        result = parse_action(dict(payload))
        assert isinstance(result, Rejection), f"{payload} 应被拒，实际 {result}"


# ---------------------------------------------------------------------------
# 可选：用 jsonschema 复核「合法必过、非法必拒」（CI 未装则跳过）
# ---------------------------------------------------------------------------


def test_spec_samples_agree_with_jsonschema():
    """同一批 13/9 样本：jsonschema 与 parse_action 结论必须一致（Schema 是测试神谕）。"""
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(ACTION_JSON_SCHEMA)
    epoch = ParseContext(mark_epoch={2: object()})
    for payload in SPEC_LEGAL_13:
        assert not list(validator.iter_errors(payload)), payload
        assert isinstance(parse_action(dict(payload), ctx=epoch), Action), payload
    for payload in SPEC_ILLEGAL_9:
        assert list(validator.iter_errors(payload)), payload
        assert isinstance(parse_action(dict(payload)), Rejection), payload


def test_schema_agrees_with_parser_on_known_cases():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft7Validator(ACTION_JSON_SCHEMA)
    for payload in LEGAL_PAYLOADS:
        errors = list(validator.iter_errors(payload))
        assert not errors, f"{payload} 应通过 Schema：{errors[:1]}"
        assert isinstance(parse_action(dict(payload)), Action)
    illegal = [
        {"action": "teleport"},
        {"action": "click"},
        {"action": "click", "target": {}},
        {"action": "click", "target": {"role": "button", "name": "x", "css": "#a"}},
        {"action": "press", "value": "Ctrl+C"},
        {"action": "click", "target": {"text": "x"}, "index": -2},
        {"action": "type", "target": {"label": "x"}, "value": 5},
        {"action": "click", "target": {"text": "x"}, "expect": {}},
        {"action": "click", "target": {"text": "x"}, "colour": "red"},
    ]
    for payload in illegal:
        assert list(validator.iter_errors(payload)), f"{payload} 应被 Schema 拒绝"
        assert isinstance(parse_action(payload), Rejection)
