"""S2b2a：schema 叶子定位与 vision 下拉在两种 schema 下的同步。"""

from core.config_map import schema_leaf
from main import BrowserLLMPlugin


class Config(dict):
    """可挂 .schema 的最小配置对象（对齐 AstrBotConfig 的用法）。"""


def make_plugin(schema):
    plugin = BrowserLLMPlugin(None, {})
    cfg = Config({})
    cfg.schema = schema
    plugin.config = cfg
    plugin._collect_provider_ids = lambda: ["p1", "p2"]
    return plugin, cfg


def test_schema_leaf_flat():
    leaf = {"type": "string", "options": [""], "default": ""}
    schema = {"vision_provider_id": leaf}
    assert schema_leaf(schema, "perception.vision_provider_id") is leaf


def test_schema_leaf_grouped():
    leaf = {"type": "string", "options": [""], "default": ""}
    schema = {"perception": {"type": "object", "items": {"vision_provider_id": leaf}}}
    assert schema_leaf(schema, "perception.vision_provider_id") is leaf


def test_schema_leaf_missing_and_bad_input():
    assert schema_leaf({}, "perception.vision_provider_id") is None
    assert schema_leaf(None, "perception.vision_provider_id") is None
    assert schema_leaf({"perception": "not-a-dict"}, "perception.vision_provider_id") is None


def test_vision_sync_grouped_schema():
    leaf = {"type": "string", "options": [""], "default": ""}
    plugin, _cfg = make_plugin(
        {"perception": {"type": "object", "items": {"vision_provider_id": leaf}}}
    )
    options = plugin._sync_vision_provider_options()
    assert options == ["", "p1", "p2"]
    assert leaf["options"] == ["", "p1", "p2"]
    assert leaf["default"] == ""


def test_vision_sync_flat_schema():
    leaf = {"type": "string", "options": [""], "default": "gone"}
    plugin, _cfg = make_plugin({"vision_provider_id": leaf})
    options = plugin._sync_vision_provider_options()
    assert options == ["", "p1", "p2"]
    assert leaf["options"] == ["", "p1", "p2"]
    # 旧 default 不在候选中 -> 修正为空串
    assert leaf["default"] == ""


def test_vision_sync_no_schema():
    plugin, _cfg = make_plugin(None)
    assert plugin._sync_vision_provider_options() == ["", "p1", "p2"]
