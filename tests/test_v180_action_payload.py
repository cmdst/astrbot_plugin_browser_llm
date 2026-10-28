"""A1 边缘容错执行级用例（规格 §4.2 契约 / §4.5 S-1..S-3 / §4.6 MV-01、MV-02）。

判据全部**执行级**：真调 `coerce_payload` 与 `parse_action` 看返回对象与拒绝码，
不做源码字符串断言（规格 §11.7 反例）。
"""

from __future__ import annotations

import json

from core import action_model as am


# --------------------------------------------------------------- 契约 1（EX-01）


def test_mapping_returns_same_object_identity():
    payload = {"action": "back"}
    assert am.coerce_payload(payload) is payload
    assert am.coerce_action_payload(payload) is payload  # 别名是同一函数对象
    assert am.coerce_action_payload is am.coerce_payload


def test_mapping_subclass_is_passed_through_untouched():
    class MyDict(dict):
        pass

    payload = MyDict(action="back")
    assert am.coerce_payload(payload) is payload


# ------------------------------------------------ 契约 3：字符串只解一层（EX-02）


def test_plain_json_string_becomes_mapping():
    coerced = am.coerce_payload('{"action":"back"}')
    assert isinstance(coerced, dict)
    assert coerced["action"] == "back"


def test_json_fence_is_stripped_one_layer():
    assert am.coerce_payload('```json\n{"action":"back"}\n```')["action"] == "back"
    assert am.coerce_payload('```\n{"action":"back"}\n```')["action"] == "back"
    # 围栏内的正文里的 ``` 不许被当成围栏（C-3：只去首尾各一行）
    inner = '```json\n{"action":"type","value":"a```b"}\n```'
    assert am.coerce_payload(inner)["value"] == "a```b"


def test_whitespace_around_payload_is_tolerated():
    assert am.coerce_payload('\n  {"action":"back"}  \n')["action"] == "back"


def test_double_encoded_string_stays_string():
    """EX-08：双重编码**只解一层**（仍交给闸门按 R-A1 拒，不替模型猜意图）。"""
    doubled = json.dumps(json.dumps({"action": "back"}))
    assert isinstance(am.coerce_payload(doubled), str)


# ------------------------------------------------- 契约 4：失败一律原值（EX-05..07, 09）


def test_invalid_json_returns_original_object_identity():
    bad = "{not json}"
    assert am.coerce_payload(bad) is bad


def test_legal_json_that_is_not_a_mapping_returns_original():
    for raw in ("[1,2]", '"x"', "3", "null", "true"):
        assert am.coerce_payload(raw) is raw, raw


def test_empty_and_blank_strings_return_original():
    for raw in ("", "   ", "\n\t "):
        assert am.coerce_payload(raw) is raw, repr(raw)


def test_other_types_return_original_identity():
    marker = object()
    for value in (None, 7, 3.5, [1], (1,), marker, {"a"}):
        assert am.coerce_payload(value) is value, value


# ------------------------------------------- 契约 2：bytes 解码（EX-04）


def test_bytes_are_decoded_then_unwrapped():
    assert am.coerce_payload(b'{"action":"back"}')["action"] == "back"
    assert am.coerce_payload(bytearray(b'{"action":"back"}'))["action"] == "back"


def test_undecodable_bytes_do_not_raise_and_return_original():
    bad = b"\xff\xfe{not json}"
    assert am.coerce_payload(bad) is bad


# ------------------------------------------- 闸门严格性 S-1 / S-2 / S-3（EX-10/11/15）


def test_gate_still_rejects_strings_s1():
    for raw in ('{"action":"back"}', "{}", '```json\n{"action":"back"}\n```'):
        result = am.parse_action(raw)
        assert isinstance(result, am.Rejection), raw
        assert result.code == "R-A1", raw
        assert "收到 str" in result.reason


def test_gate_still_rejects_list_none_and_numbers_s2():
    for payload in ([], None, 7, 3.5, (), b'{"action":"back"}'):
        result = am.parse_action(payload)
        assert isinstance(result, am.Rejection)
        assert result.code == "R-A1"


def test_gate_accepts_coerced_payload_s3():
    action = am.parse_action(am.coerce_payload('{"action":"back"}'))
    assert isinstance(action, am.Action)
    assert action.kind == am.ActionKind.BACK
    # 容错层不制造"猜这种字符串"，闸门仍拒
    quoted = am.parse_action(am.coerce_payload('"x"'))
    assert isinstance(quoted, am.Rejection) and quoted.code == "R-A1"


def test_rejection_code_table_unchanged_exactly_fourteen():
    assert set(am.REJECTION_CODES) == {f"R-A{i}" for i in range(1, 15)}


def test_ra1_hint_mentions_the_edge_contract():
    rejection = am.parse_action('{"action":"back"}')
    assert "不要传 JSON 字符串" in rejection.hint


# ------------------------------------------------------- 变异验证（MV-01 的执行级替身）


def test_mutation_without_coerce_the_same_payload_is_rejected(monkeypatch):
    """MV-01：真把边缘 coerce 去掉（换成恒等函数）⇒ 同一条载荷必须红。

    这条用例的价值在于：它证明"能过闸"的原因**正是** coerce 这一步，
    而不是闸门变宽松了（删掉 coerce 后这里必然是 R-A1）。
    """
    raw = json.dumps({"action": "back"})
    assert isinstance(am.parse_action(am.coerce_payload(raw)), am.Action)

    monkeypatch.setattr(am, "coerce_payload", lambda payload: payload)  # 模拟接线漏了 coerce
    assert isinstance(am.parse_action(am.coerce_payload(raw)), am.Rejection)
    assert am.parse_action(am.coerce_payload(raw)).code == "R-A1"


def test_coerce_never_mutates_input_mapping():
    payload = {"action": "back", "target": {"role": "button", "name": "提交"}}
    before = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    am.coerce_payload(payload)
    assert json.dumps(payload, sort_keys=True, ensure_ascii=False) == before
