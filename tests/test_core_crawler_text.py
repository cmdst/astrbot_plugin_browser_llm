"""core/crawler.py：HTML 文本化 / 编码回退 / 内容类型白名单 / 限长。

（v1.4.0 清理：原 ``test_core_pages_crawler.py`` 里 ``core/pages.py`` 的 8 条
用例随模块删除；crawler 的这 7 条保留 —— 它们是 ``AiohttpFetcher.fetch()``
文本链路的底层守卫，``AiohttpFetcher`` 本身由
``tests/test_search_http_transport.py`` 覆盖。``html_links`` 随 ``pages`` 删除，
原 ``test_html_title_and_links`` 只保留 ``html_title`` 一半。）
"""

from core.crawler import (
    apply_size_limit,
    decode_body,
    html_title,
    html_to_text,
    is_allowed_content_type,
)


def test_html_to_text_strips_scripts_and_entities():
    html = (
        "<html><head><title>标题</title>"
        "<script>var SECRET_MARKER=1;</script>"
        "<style>.x{color:red}</style></head>"
        "<body><p>Hello&nbsp;&amp;&nbsp;world</p><div>第二段</div></body></html>"
    )
    text, truncated = html_to_text(html)
    assert "SECRET_MARKER" not in text
    assert "color:red" not in text
    assert "Hello & world" in text
    assert "第二段" in text
    assert truncated is False


def test_html_to_text_truncates():
    text, truncated = html_to_text("<p>" + "a" * 500 + "</p>", max_chars=100)
    assert truncated is True
    assert len(text) == 100


def test_html_title():
    assert html_title("<title>T1</title><a href=\"/a\">a</a>") == "T1"
    assert html_title("<p>no title</p>") == ""


def test_decode_body_falls_back_to_gb18030():
    raw = "中文".encode("gb18030")
    assert decode_body(raw, encoding="utf-8") == "中文"


def test_decode_body_never_raises():
    raw = b"\xff\xfe\x00bad"
    assert isinstance(decode_body(raw), str)


def test_content_type_whitelist():
    assert is_allowed_content_type("text/html; charset=utf-8") is True
    assert is_allowed_content_type("application/xhtml+xml") is True
    assert is_allowed_content_type("application/json") is False
    assert is_allowed_content_type("") is True


def test_size_limit():
    body, truncated = apply_size_limit(b"x" * 100, max_bytes=10)
    assert truncated is True
    assert len(body) == 10
