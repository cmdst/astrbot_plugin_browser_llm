"""SafetyFilter 全量测试：协议白名单 / SSRF 内网拦截 / 禁词检测 /
async acheck_url 与同步 check_url 一致性。纯 Python，无外部依赖。"""

import asyncio

import pytest

from core.safety import SafetyFilter, acheck_hostname_internal

BANNED = ["pornhub", "色情", "成人", "赌博", "暴力", "政治", "反动", "恐怖", "谣言", "诈骗", "病毒"]


@pytest.fixture
def sf() -> SafetyFilter:
    """默认安全过滤器：启用内网拦截 + 全量禁词。"""
    return SafetyFilter(BANNED, block_internal_ip=True)


# ------------------------------------------------------------
# 协议白名单
# ------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:text/html,<b>hi</b>",
    "ftp://example.com/a",
    "mailto:x@y.com",
    "",                       # 空 URL
    "https://",               # 缺主机名
])
def test_reject_bad_schemes(sf, bad_url: str):
    ok, reason = sf.check_url(bad_url)
    assert ok is False, f"应拒绝 {bad_url!r}: {reason}"
    assert reason, "拒绝时应给出原因"


# ------------------------------------------------------------
# SSRF：内网 / 环回 / 链路本地 / 混淆形式
# ------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1/x",          # IPv4 环回
    "http://127.0.0.1:8080/admin",  # 环回 + 端口
    "http://localhost/x",          # 环回域名
    "http://10.0.0.1/",            # 私网 A 段
    "http://10.255.255.255/",
    "http://172.16.0.1/",          # 私网 B 段
    "http://172.31.255.255/",
    "http://192.168.1.1/",         # 私网 C 段
    "http://169.254.169.254/latest/meta-data/",  # 链路本地（云元数据）
    "http://0.0.0.0/",             # 未指定地址
    "http://[::1]/",               # IPv6 环回
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6
    "http://2130706433/",          # 127.0.0.1 十进制混淆
    "http://127.1/",               # 短点分混淆
    "http://0177.0.0.1/",          # 八进制混淆
])
def test_reject_internal_ips(sf, bad_url: str):
    ok, reason = sf.check_url(bad_url)
    assert ok is False, f"应拦截内网地址 {bad_url!r}: {reason}"
    assert "内网" in reason or "保留" in reason, f"原因应说明内网: {reason}"


@pytest.mark.parametrize("good_url", [
    "https://www.baidu.com",
    "http://example.com/path?q=1",
    "https://github.com/",
])
def test_allow_public_urls(sf, good_url: str):
    ok, reason = sf.check_url(good_url)
    assert ok is True, f"应放行公网地址 {good_url!r}: {reason}"


def test_block_internal_ip_disabled():
    """关闭内网拦截后仅做协议检查，内网地址放行。"""
    sf = SafetyFilter(BANNED, block_internal_ip=False)
    assert sf.check_url("http://127.0.0.1/x")[0] is True
    assert sf.check_url("http://192.168.1.1/")[0] is True
    # 协议检查仍然生效
    assert sf.check_url("javascript:alert(1)")[0] is False


# ------------------------------------------------------------
# 禁词检测
# ------------------------------------------------------------

def test_banned_case_insensitive(sf):
    ok, word = sf.check_text("访问 PornHub 网站")
    assert ok is False and word == "pornhub"


def test_banned_substring_hit(sf):
    ok, word = sf.check_text("来赌博吗")
    assert ok is False and word == "赌博"


def test_banned_multiple_words(sf):
    """多词列表：返回命中的第一个禁词。"""
    ok, word = sf.check_text("这里有政治和暴力内容")
    assert ok is False and word in ("政治", "暴力")


def test_clean_text_passes(sf):
    ok, word = sf.check_text("今天天气不错，去公园散步吧")
    assert ok is True and word == ""


def test_empty_text_passes(sf):
    assert sf.check_text("") == (True, "")
    assert sf.check_text(None) == (True, "")


def test_empty_banned_list_always_passes():
    sf = SafetyFilter([])
    assert sf.check_text("赌博色情") == (True, "")


# ------------------------------------------------------------
# async / 同步一致性
# ------------------------------------------------------------

def test_acheck_url_matches_check_url(sf):
    """acheck_url 与 check_url 对同批 URL 结论一致（asyncio.run 驱动）。"""
    urls = [
        "javascript:alert(1)",
        "http://127.0.0.1/x",
        "http://localhost/",
        "http://[::1]/",
        "https://www.baidu.com",
        "http://example.com/",
    ]

    async def _check():
        for url in urls:
            sync_ok, _ = sf.check_url(url)
            async_ok, _ = await sf.acheck_url(url)
            assert sync_ok == async_ok, f"结论不一致: {url}"

    asyncio.run(_check())


# ------------------------------------------------------------
# acheck_hostname_internal（页面级 SSRF 兜底判定）
# ------------------------------------------------------------

def test_acheck_hostname_internal_literals():
    """IP 字面量（含混淆形式）直接判定，无需 DNS。"""
    async def _check():
        assert await acheck_hostname_internal("127.0.0.1") is True
        assert await acheck_hostname_internal("::1") is True
        assert await acheck_hostname_internal("fe80::1") is True
        assert await acheck_hostname_internal("10.0.0.1") is True
        assert await acheck_hostname_internal("169.254.169.254") is True
        assert await acheck_hostname_internal("2130706433") is True, "十进制混淆"
        assert await acheck_hostname_internal("127.1") is True, "短点分混淆"
        assert await acheck_hostname_internal("0177.0.0.1") is True, "八进制混淆"
        assert await acheck_hostname_internal("93.184.216.34") is False

    asyncio.run(_check())


def test_acheck_hostname_internal_dns():
    """域名经 DNS 判定：localhost 为内网，公网域名为 False/None（离线不判定）。"""
    async def _check():
        assert await acheck_hostname_internal("localhost") is True
        verdict = await acheck_hostname_internal("example.com")
        assert verdict in (False, None), f"公网域名不应判为内网: {verdict}"

    asyncio.run(_check())


def test_acheck_hostname_internal_empty():
    async def _check():
        assert await acheck_hostname_internal("") is False
        assert await acheck_hostname_internal(None) is False

    asyncio.run(_check())
