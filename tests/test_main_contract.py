"""main.py 静态契约测试（工具化子代理版）：AST 解析 + docstring_parser
校验，不 import astrbot（由 conftest 桩兜底，但本文件只用 ast /
docstring_parser 保证即使在无桩环境也能跑）。

校验点：
- 仅 1 个 @filter.llm_tool(name='browse_web') 入口工具；24 个 browse_*
  方法不再有 @filter.llm_tool 装饰器；
- _BROWSER_TOOLS 配置含 24 项，每项 name/description/parameters/method；
- parameters 为合法 JSON Schema（required 必填 / 可选参数类型）；
- 有参方法 docstring 含 Args 段，参数类型标注 ∈ {string,number}；
- 无参方法（browse_current_page/browse_go_back/browse_go_forward/
  browse_screenshot）docstring 无 Args 段；
- browse_web 入口存在且 docstring 含 Args: input(string)；
- 每个浏览方法函数体内有 try/except（容错）；
- on_llm_request 注入方法存在且含 browse_web 委托边界。
"""

import ast
import json
from pathlib import Path

from docstring_parser import parse as dp_parse

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "main.py").read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

# 允许的参数类型标注（工具 parameters JSON Schema 类型）
_ALLOWED_TYPES = {"string", "number", "object", "array", "boolean"}

# 期望的 24 个浏览工具名
EXPECTED_TOOLS = [
    "browse_search", "browse_open", "browse_current_page", "browse_get_text",
    "browse_get_links", "browse_click_link", "browse_scroll", "browse_go_back",
    "browse_go_forward", "browse_new_tab", "browse_switch_tab",
    "browse_close_tab", "browse_input", "browse_press_key", "browse_screenshot",
    "browse_click_text", "browse_set_slider", "browse_select_option",
    "browse_click_coords", "browse_hover", "browse_reload", "browse_checkbox",
    "browse_zoom_crop", "browse_sniff_media",
]

# 无参工具：docstring 不应含 Args 段
NO_ARG_TOOLS = {
    "browse_current_page", "browse_go_back", "browse_go_forward",
    "browse_screenshot", "browse_reload",
}

# 有参工具的期望参数（参数名 -> 是否必填）
EXPECTED_PARAMS = {
    "browse_search": {"query": True, "engine": False},
    "browse_open": {"url": True},
    "browse_get_text": {"max_chars": False},
    "browse_get_links": {"max_items": False},
    "browse_click_link": {"index": True},
    "browse_scroll": {"direction": False, "pixels": False},
    "browse_new_tab": {"url": True},
    "browse_switch_tab": {"index": True},
    "browse_close_tab": {"index": True},
    "browse_input": {"selector": True, "text": True},
    "browse_press_key": {"key": True},
    "browse_click_text": {"text": True},
    "browse_set_slider": {"value": True},
    "browse_select_option": {"selector": True, "value": True},
    "browse_click_coords": {"x": True, "y": True},
    "browse_hover": {"text": True},
    "browse_reload": {},
    "browse_checkbox": {"selector": True, "checked": True},
    "browse_zoom_crop": {"x": True, "y": True, "width": True, "height": True},
    "browse_sniff_media": {"media_type": False, "max_items": False},
}


def _browse_methods() -> dict[str, ast.AsyncFunctionDef]:
    """收集 browse_* 方法定义（name -> 节点）。"""
    return {
        node.name: node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("browse_")
    }


def _browser_tools_spec() -> list[dict]:
    """从 AST 提取 _BROWSER_TOOLS 配置。

    parameters 由 _params() 函数调用生成，无法 literal_eval；这里只
    提取 name/description/method 与 _params 调用的参数（name ->
    (desc, type) 元组），再本地重建 parameters dict 供契约校验。
    """
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_BROWSER_TOOLS":
                    return _eval_tools_list(node.value)
    raise AssertionError("未找到 _BROWSER_TOOLS 配置")


def _eval_tools_list(node: ast.expr) -> list[dict]:
    """递归求值 _BROWSER_TOOLS 列表字面量（处理 _params 调用）。"""
    assert isinstance(node, ast.List), "期望列表字面量"
    out = []
    for item in node.elts:
        assert isinstance(item, ast.Dict), "期望 dict 字面量"
        fields = {}
        for k, v in zip(item.keys, item.values):
            key = k.value if isinstance(k, ast.Constant) else None
            if key is None:
                continue
            if isinstance(v, ast.Constant):
                fields[key] = v.value
            elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "_params":
                fields[key] = _eval_params_call(v)
            elif isinstance(v, ast.List):
                fields[key] = [e.value for e in v.elts if isinstance(e, ast.Constant)]
            else:
                raise AssertionError(f"无法求值的字段 {key}: {type(v).__name__}")
        out.append(fields)
    return out


def _eval_params_call(node: ast.Call) -> dict:
    """求值 _params(required, optional)：返回标准 JSON Schema dict。"""
    args = {a.arg: a.value for a in node.keywords}
    # 位置参数兜底：_params(required_dict, optional_dict)
    pos = list(node.args)

    def _field_dict(n):
        """求值 {name: (desc, type)} 字面量；空/异常返回空 dict。"""
        keys = getattr(n, "keys", None)
        values = getattr(n, "values", None)
        if keys is None or values is None:
            return {}
        result = {}
        for k, v in zip(keys, values):
            if not isinstance(k, ast.Constant):
                continue
            name = k.value
            if isinstance(v, ast.Tuple) and len(v.elts) == 2:
                desc = v.elts[0].value
                ptype = v.elts[1].value
                result[name] = (desc, ptype)
            elif isinstance(v, ast.Constant):
                result[name] = (v.value, "string")
        return result

    required = _field_dict(args.get("required", pos[0] if pos else ast.Dict()))
    optional = _field_dict(args.get("optional", pos[1] if len(pos) > 1 else ast.Dict()))
    properties = {}
    required_list = []
    for name, (desc, ptype) in required.items():
        properties[name] = {"type": ptype, "description": desc}
        required_list.append(name)
    for name, (desc, ptype) in optional.items():
        properties[name] = {"type": ptype, "description": desc}
    return {"type": "object", "properties": properties, "required": required_list}


# ------------------------------------------------------------
# 工具注册：不再全局注册
# ------------------------------------------------------------

def test_no_llm_tool_decorators():
    """15 个 browse_* 方法不应有 @filter.llm_tool 装饰器（仅 browse_web 入口）。"""
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "llm_tool"
                ):
                    # 唯一例外：browse_web 入口工具
                    assert node.name == "browse_web", (
                        f"{node.name} 不应有 @filter.llm_tool 装饰器"
                    )


def test_16_browse_methods_exist():
    methods = _browse_methods()
    for name in EXPECTED_TOOLS:
        assert name in methods, f"缺少浏览方法 {name}"


# ------------------------------------------------------------
# _BROWSER_TOOLS 配置
# ------------------------------------------------------------

def test_24_browse_tools_config():
    spec = _browser_tools_spec()
    assert len(spec) == 24, f"_BROWSER_TOOLS 应为 24 项，实际 {len(spec)}"
    names = [s["name"] for s in spec]
    assert names == EXPECTED_TOOLS, f"工具名顺序不一致: {names}"
    assert len(names) == len(set(names)), "工具名不应重复"


def test_browser_tools_config_fields():
    """每项含 name/description/parameters/method，method 对应实例方法。"""
    spec = _browser_tools_spec()
    methods = _browse_methods()
    for s in spec:
        for field in ("name", "description", "parameters", "method"):
            assert field in s, f"{s.get('name')} 缺字段 {field}"
        assert s["method"] in methods, f"{s['name']} 的 method 不存在"
        assert s["method"] == s["name"], f"{s['name']} 的 method 应等于工具名"


def test_browser_tools_parameters_valid_schema():
    """parameters 为合法 JSON Schema，required 与属性一致。"""
    spec = _browser_tools_spec()
    for s in spec:
        p = s["parameters"]
        assert p.get("type") == "object", f"{s['name']} parameters.type 应为 object"
        props = p.get("properties", {})
        for pname, pmeta in props.items():
            assert pmeta.get("type") in _ALLOWED_TYPES, (
                f"{s['name']}.{pname} 类型非法: {pmeta.get('type')}"
            )
        # required 属性必须存在于 properties 中
        for r in p.get("required", []):
            assert r in props, f"{s['name']} required 含未知参数 {r}"


def test_browser_tools_params_match_expected():
    """参数名与必填性符合工具契约。"""
    spec = {s["name"]: s["parameters"] for s in _browser_tools_spec()}
    for name, expected in EXPECTED_PARAMS.items():
        p = spec[name]
        props = set(p.get("properties", {}))
        assert props == set(expected), f"{name} 参数不符: {props}"
        required = set(p.get("required", []))
        want_required = {k for k, v in expected.items() if v}
        assert required == want_required, f"{name} required 不符: {required}"


# ------------------------------------------------------------
# docstring 契约（方法 docstring 供 FunctionTool description 参考）
# ------------------------------------------------------------

def test_with_arg_methods_have_args_section():
    with_arg = set(EXPECTED_TOOLS) - NO_ARG_TOOLS
    methods = _browse_methods()
    for name in with_arg:
        doc = ast.get_docstring(methods[name]) or ""
        assert "Args:" in doc, f"{name} 缺 Args 段"
        params = dp_parse(doc).params
        assert params, f"{name} Args 段未解析出参数"
        for p in params:
            assert p.arg_name != "(无参数)", f"{name} 出现 (无参数)"
            assert p.type_name is not None, f"{name} 参数 {p.arg_name} 缺类型"
            assert p.type_name in _ALLOWED_TYPES, (
                f"{name} 参数 {p.arg_name} 类型 {p.type_name} 不在允许集"
            )


def test_no_arg_methods_have_no_args_section():
    for name in NO_ARG_TOOLS:
        doc = ast.get_docstring(_browse_methods()[name]) or ""
        assert "Args:" not in doc, f"{name} 不应有 Args 段"
        assert dp_parse(doc).params == [], f"{name} 不应解析出参数"


# ------------------------------------------------------------
# 容错：每个浏览方法有 try/except
# ------------------------------------------------------------

def _has_try_except(node: ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Try) and any(
            isinstance(h, ast.ExceptHandler) for h in child.handlers
        ):
            return True
    return False


def test_every_browse_method_has_try_except():
    methods = _browse_methods()
    for name in EXPECTED_TOOLS:
        assert _has_try_except(methods[name]), f"{name} 缺少 try/except 容错"


# ------------------------------------------------------------
# 工具化子代理：browse_web 入口 / 工厂 / 装配
# ------------------------------------------------------------

def test_browse_web_entry_registered():
    """@filter.llm_tool(name='browse_web') 入口工具存在。"""
    found = []
    for node in ast.walk(_TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "browse_web":
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "llm_tool"
                ):
                    name = next(
                        (
                            kw.value.value
                            for kw in dec.keywords
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                        ),
                        "",
                    )
                    found.append(name)
    assert found == ["browse_web"], f"browse_web 入口缺失: {found}"


def test_no_agent_decorators():
    """不应再有 @agent 装饰器（改用 llm_tool 入口）。"""
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and (
                    (isinstance(dec.func, ast.Name) and dec.func.id == "agent")
                    or (isinstance(dec.func, ast.Attribute) and dec.func.attr == "agent")
                ):
                    raise AssertionError(f"{node.name} 仍有 @agent 装饰器")


def test_browse_web_docstring_contract():
    """browse_web docstring 含 Args: input(string)（llm_tool 加载契约）。"""
    node = next(
        (n for n in ast.walk(_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_web"),
        None,
    )
    assert node is not None
    doc = ast.get_docstring(node) or ""
    assert "Args:" in doc, "browse_web 缺 Args 段"
    params = dp_parse(doc).params
    assert len(params) == 1 and params[0].arg_name == "input", (
        f"browse_web 应只有 input 参数: {[(p.arg_name, p.type_name) for p in params]}"
    )
    assert params[0].type_name == "string"


def test_tool_loop_agent_and_toolset_present():
    """browse_web 入口显式调用 tool_loop_agent，且 initialize 构建 ToolSet。"""
    src = _SRC
    assert "tool_loop_agent" in src
    assert "ToolSet(" in src
    assert "def _make_browser_tool" in src
    assert "_attach_agent_tools" not in src, "已删除的装配方法不应残留"


def test_browse_web_uses_plugin_config():
    """browse_web 用 agent_max_steps / agent_tool_timeout 配置。"""
    node = next(
        (n for n in ast.walk(_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_web"),
        None,
    )
    assert node is not None
    body = ast.get_source_segment(_SRC, node) or ""
    assert "agent_max_steps" in body and "agent_tool_timeout" in body


# ------------------------------------------------------------
# 注入钩子：browse_web 委托边界
# ------------------------------------------------------------

def test_on_llm_request_injection_exists():
    """on_llm_request 注入方法存在，指引含 browse_web 与【不要用】边界。"""
    found = []
    for node in ast.walk(_TREE):
        if isinstance(node, ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "on_llm_request"
                ):
                    found.append(node.name)
    assert found, "未找到 on_llm_request 注入方法"
    assert "browse_web" in _SRC
    assert "不要" in _SRC, "指引缺【不要用】边界"


def test_screenshot_filename_has_milli_and_session():
    """截图文件名含会话标识哈希与毫秒时间戳。"""
    src = _SRC
    assert "%f" in src, "文件名缺毫秒时间戳"
    assert "umo_hash" in src, "文件名缺会话标识哈希"
    assert "hashlib.md5" in src, "应使用 hashlib 生成会话哈希"


# ------------------------------------------------------------
# silent_mode / page_perception 配置与动态指令
# ------------------------------------------------------------

def test_new_configs_in_schema():
    """_conf_schema.json 含 silent_mode / page_perception / cache_days。"""
    import json
    schema = json.loads((_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "silent_mode" in schema and schema["silent_mode"]["type"] == "bool"
    assert schema["silent_mode"]["default"] is True
    assert "page_perception" in schema
    assert schema["page_perception"]["type"] == "string"
    assert schema["page_perception"]["default"] == "text_image"
    assert set(schema["page_perception"]["options"]) == {"text", "text_image", "image"}
    assert "cache_days" in schema and schema["cache_days"]["type"] == "int"
    assert schema["cache_days"]["default"] == 3


def test_load_config_reads_new_configs():
    """_load_config 读取 silent_mode / page_perception。"""
    src = _SRC
    assert "self.silent_mode" in src and 'cfg.get("silent_mode", True)' in src
    assert "self.page_perception" in src and 'cfg.get("page_perception", "text_image")' in src


def test_build_browser_instruction_dynamic():
    """_build_subagent_instruction 存在且含三档感知方式映射。"""
    src = _SRC
    assert "def _build_subagent_instruction" in src
    assert "_BROWSER_AGENT_INSTRUCTION" in src, "基础模板常量保留"
    # 三档感知方式文案
    for keyword in ("仅文字", "文字为主，截图辅助", "截图为主"):
        assert keyword in src, f"缺感知方式文案: {keyword}"


def test_browse_web_uses_dynamic_instruction():
    """browse_web 的 tool_loop_agent 用 self._browser_instruction。"""
    node = next(
        (n for n in ast.walk(_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_web"),
        None,
    )
    assert node is not None
    body = ast.get_source_segment(_SRC, node) or ""
    assert "system_prompt=self._browser_instruction" in body, "应用动态指令"


def test_screenshot_silent_mode_logic():
    """browse_screenshot 静默逻辑：silent_mode 时跳过发图。"""
    node = next(
        (n for n in ast.walk(_TREE)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "browse_screenshot"),
        None,
    )
    assert node is not None
    body = ast.get_source_segment(_SRC, node) or ""
    assert "self.silent_mode" in body, "缺静默判断"
    assert "event.send" in body, "非静默仍发图"
    assert "未发送" in body, "静默返回文案"



# ------------------------------------------------------------
# 会话白/黑名单辅助
# ------------------------------------------------------------

def test_session_whitelist_blacklist_attrs():
    """_load_config 读取 session_whitelist / session_blacklist。"""
    src = _SRC
    assert "self.session_whitelist" in src
    assert "self.session_blacklist" in src
    assert "def _is_session_allowed" in src
