"""会话黑白名单精确匹配行为测试（v1.2.0 P2：子串误伤修复）。

验证 _is_session_allowed 的匹配规则：
- 条目与 umo 按分隔符（: / |）切分后的字段精确比对，杜绝子串误伤
  （黑名单 "123" 不得命中群 "1234"）；
- 完整 UMO 条目（含冒号）与 umo 整体精确相等仍命中（兼容既有配置）；
- 群号精确比对兜底；_umo_of 兜底格式（group|sender）同样精确匹配。
"""

from astrbot.api.star import Context
from main import BrowserLLMPlugin


def _make_plugin(**cfg):
    base = {
        "banned_words": [],
        "block_internal_ip": True,
        "session_whitelist": [],
        "session_blacklist": [],
    }
    base.update(cfg)
    return BrowserLLMPlugin(context=Context(), config=base)


class _Event:
    """最小事件桩：umo / group_id 可配置（unified_msg_origin 为空时走兜底）。"""

    def __init__(self, umo="", group_id="", sender_id=""):
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id


# ------------------------------------------------------------
# 黑名单：精确匹配 / 子串误伤回归
# ------------------------------------------------------------

def test_blacklist_exact_field_no_substring_hit():
    """黑名单 '123' 不得误伤群 '1234'（子串误伤核心回归）。"""
    plugin = _make_plugin(session_blacklist=["123"])
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is True and reason == "", "黑名单 123 不应命中群 1234"


def test_blacklist_exact_field_hits_group():
    """黑名单完整群号精确命中 session_id 字段。"""
    plugin = _make_plugin(session_blacklist=["1234"])
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False and "黑名单" in reason


def test_blacklist_field_hits_private_user_id():
    """黑名单命中私聊 session_id 字段（平台/类型字段不干扰）。"""
    plugin = _make_plugin(session_blacklist=["987654"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:FriendMessage:987654", group_id="")
    )
    assert allowed is False


def test_blacklist_platform_field_hits():
    """黑名单平台名（字段级）仍可精确命中。"""
    plugin = _make_plugin(session_blacklist=["aiocqhttp"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False


def test_blacklist_full_umo_exact_still_works():
    """完整 UMO 条目（含冒号）与 umo 整体精确相等仍命中（兼容既有配置）。"""
    plugin = _make_plugin(session_blacklist=["aiocqhttp:GroupMessage:1234"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False


def test_blacklist_full_umo_no_prefix_false_positive():
    """完整 UMO 条目不得误伤前缀相似的会话（精确相等而非子串）。"""
    plugin = _make_plugin(session_blacklist=["aiocqhttp:GroupMessage:1234"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:12345", group_id="12345")
    )
    assert allowed is True


def test_blacklist_group_id_direct_match():
    """群号精确比对兜底（umo 无匹配但群号命中时仍拒绝）。"""
    plugin = _make_plugin(session_blacklist=["5555"])
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="weixin_oc:GroupMessage:9999", group_id="5555")
    )
    assert allowed is False and "黑名单" in reason


# ------------------------------------------------------------
# 白名单：精确匹配 / 未命中拒绝
# ------------------------------------------------------------

def test_whitelist_exact_field_allows():
    """白名单字段精确命中放行（平台名）。"""
    plugin = _make_plugin(session_whitelist=["aiocqhttp"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is True


def test_whitelist_not_matched_rejected():
    """白名单未命中（含前缀相似）拒绝。"""
    plugin = _make_plugin(session_whitelist=["9999"])
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False and "白名单" in reason


def test_whitelist_no_substring_false_positive():
    """白名单 '12' 不得放行群 '1234'（子串误伤回归）。"""
    plugin = _make_plugin(session_whitelist=["12"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False


# ------------------------------------------------------------
# _umo_of 兜底格式（group|sender）：按 | 切分精确匹配
# ------------------------------------------------------------

def test_umo_fallback_pipe_format_exact_hit():
    """兜底格式 group|sender：群号字段精确命中。"""
    plugin = _make_plugin(session_blacklist=["123456"])
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="", group_id="123456")
    )
    assert allowed is False and "黑名单" in reason


def test_umo_fallback_pipe_format_no_substring_hit():
    """兜底格式下黑名单 '123' 也不得误伤群 '123456'。"""
    plugin = _make_plugin(session_blacklist=["123"])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="", group_id="123456")
    )
    assert allowed is True


# ------------------------------------------------------------
# 空白条目与空配置
# ------------------------------------------------------------

def test_blank_entries_ignored():
    """空白条目忽略，不因空白条目误拒绝。"""
    plugin = _make_plugin(session_blacklist=["  ", "1234", ""])
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:5678", group_id="5678")
    )
    assert allowed is True


def test_empty_lists_allow_all():
    """黑白名单均为空时全部放行（默认配置）。"""
    plugin = _make_plugin()
    allowed, _ = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is True


def test_blacklist_outranks_whitelist():
    """同一会话同时命中黑白名单：黑名单优先拒绝。"""
    plugin = _make_plugin(
        session_whitelist=["1234"],
        session_blacklist=["1234"],
    )
    allowed, reason = plugin._is_session_allowed(
        _Event(umo="aiocqhttp:GroupMessage:1234", group_id="1234")
    )
    assert allowed is False and "黑名单" in reason
