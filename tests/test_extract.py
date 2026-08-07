"""ContentExtractor 全量测试：正文提取（截断/降级）、链接提取
（编号/去重/过滤/超限）、页面信息。全部使用 FakePage mock，
不依赖 playwright。"""

import asyncio

import pytest

from core.extract import ContentExtractor

TRUNCATE_MARKER = "...（内容过长已截断）..."


class FakePage:
    """mock Page：inner_text / evaluate / title / url，可按需注入失败。"""

    def __init__(
        self,
        body_text="",
        links=None,
        url="https://example.com",
        title="示例页",
        fail_inner=False,
        fail_evaluate=False,
        fail_title=False,
        fail_url=False,
    ):
        self._body = body_text
        self._links = links or []
        self.url = url
        self._title = title
        self.fail_inner = fail_inner
        self.fail_evaluate = fail_evaluate
        self.fail_title = fail_title
        self.fail_url = fail_url

    async def inner_text(self, selector):
        if self.fail_inner:
            raise RuntimeError("inner_text 失败")
        return self._body

    async def evaluate(self, js):
        if self.fail_evaluate:
            raise RuntimeError("evaluate 失败")
        if "querySelectorAll" in js:  # 链接收集 JS
            return self._links
        return self._body  # 降级取正文 JS

    async def title(self):
        if self.fail_title:
            raise RuntimeError("title 失败")
        return self._title


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------
# 正文提取
# ------------------------------------------------------------

def test_extract_text_normal():
    ex = ContentExtractor()
    out = _run(ex.extract_text(FakePage(body_text="  你好\n\n  世界  ")))
    assert out == "你好 世界", "应压缩连续空白"


def test_extract_text_short_no_truncate():
    ex = ContentExtractor()
    text = "短文本" * 100  # 300 字符 < 4000
    out = _run(ex.extract_text(FakePage(body_text=text), max_chars=4000))
    assert out == text and TRUNCATE_MARKER not in out


def test_extract_text_truncate_keeps_head_tail():
    """超长截断：保留开头与结尾，中间含省略标记。"""
    ex = ContentExtractor()
    long_text = "A" * 5000
    out = _run(ex.extract_text(FakePage(body_text=long_text), max_chars=100))
    assert len(out) <= 100
    assert TRUNCATE_MARKER in out
    assert out.startswith("A") and out.endswith("A")
    # 首尾各保留一段（非仅开头）
    assert out.index(TRUNCATE_MARKER) > 0 and out.index(TRUNCATE_MARKER) < len(out) - 1


def test_extract_text_fallback_on_inner_text_failure():
    """inner_text 抛异常 → 降级 document.body.innerText。"""
    ex = ContentExtractor()
    out = _run(ex.extract_text(FakePage(body_text=" 降级文本 ", fail_inner=True)))
    assert out == "降级文本"


def test_extract_text_both_fail_returns_empty():
    """inner_text 与 evaluate 都失败 → 返回空串。"""
    ex = ContentExtractor()
    out = _run(
        ex.extract_text(FakePage(body_text="x", fail_inner=True, fail_evaluate=True))
    )
    assert out == ""


def test_extract_text_empty_body():
    ex = ContentExtractor()
    assert _run(ex.extract_text(FakePage(body_text="  \n "))) == ""


# ------------------------------------------------------------
# 链接提取
# ------------------------------------------------------------

LINKS = [
    {"text": "首页", "href": "https://example.com/", "raw": "/"},
    {"text": "关于", "href": "https://example.com/about", "raw": "/about"},
    {"text": "重复", "href": "https://example.com/", "raw": "/"},  # 同 href 去重
    {"text": "", "href": "", "raw": ""},  # 空 href
    {"text": "锚点", "href": "https://example.com/#top", "raw": "#top"},  # 纯锚点
    {"text": "JS", "href": "javascript:void(0)", "raw": "javascript:void(0)"},  # javascript:
    {"text": "外链", "href": "https://other.com/x", "raw": "https://other.com/x"},
    {"text": "多", "href": "https://example.com/1", "raw": "/1"},
    {"text": "余", "href": "https://example.com/2", "raw": "/2"},
]


def test_extract_links_numbered_format():
    ex = ContentExtractor()
    out = _run(ex.extract_links(FakePage(links=LINKS[:2]), max_links=20))
    lines = out.splitlines()
    assert lines[0] == "[1] 首页 → https://example.com/"
    assert lines[1] == "[2] 关于 → https://example.com/about"


def test_extract_links_dedupe_same_href():
    ex = ContentExtractor()
    out = _run(ex.extract_links(FakePage(links=LINKS), max_links=20))
    # 同 href 保留首个：'首页' 保留、'重复' 被丢弃
    assert "[1] 首页 → https://example.com/" in out
    assert "重复" not in out
    # 'https://example.com/' 精确 href 只出现在第 1 行（其余是 /about /1 /2）
    hrefs = [line.rsplit(" → ", 1)[1] for line in out.splitlines() if " → " in line]
    assert hrefs.count("https://example.com/") == 1, hrefs


def test_extract_links_filters_bad():
    """过滤空 href / 纯锚点 / javascript:。"""
    ex = ContentExtractor()
    out = _run(ex.extract_links(FakePage(links=LINKS), max_links=20))
    assert "锚点" not in out and "JS" not in out
    assert "https://other.com/x" in out


def test_extract_links_max_links_limit():
    """超限：仅保留 max_links 条 + 追加提示行。"""
    ex = ContentExtractor()
    out = _run(ex.extract_links(FakePage(links=LINKS), max_links=3))
    lines = out.splitlines()
    assert len(lines) == 4, "3 条链接 + 1 条提示行"
    assert "共 5 个链接" in lines[-1] and "仅显示前 3 个" in lines[-1]
    assert lines[0].startswith("[1]") and lines[2].startswith("[3]")


def test_extract_links_no_overflow_no_hint():
    ex = ContentExtractor()
    out = _run(ex.extract_links(FakePage(links=LINKS[:2]), max_links=3))
    assert "仅显示前" not in out and len(out.splitlines()) == 2


def test_extract_links_exception_returns_empty():
    ex = ContentExtractor()
    out = _run(
        ex.extract_links(FakePage(links=LINKS, fail_evaluate=True), max_links=20)
    )
    assert out == ""


def test_extract_links_non_list_returns_empty():
    class WeirdPage(FakePage):
        async def evaluate(self, js):
            return {"not": "a list"}

    ex = ContentExtractor()
    assert _run(ex.extract_links(WeirdPage(), max_links=20)) == ""


# ------------------------------------------------------------
# 页面信息
# ------------------------------------------------------------

def test_page_info_normal():
    ex = ContentExtractor()
    info = _run(ex.extract_page_info(FakePage(url="https://example.com", title="标题")))
    assert info == {"url": "https://example.com", "title": "标题"}


def test_page_info_partial_failure():
    """title 失败 → 空串，url 保留。"""
    ex = ContentExtractor()
    info = _run(
        ex.extract_page_info(FakePage(url="https://example.com", fail_title=True))
    )
    assert info == {"url": "https://example.com", "title": ""}


def test_page_info_url_failure():
    class UrlBoomPage(FakePage):
        def __init__(self):
            self._title = "t"
            self.fail_title = False
            self.fail_inner = False
            self.fail_evaluate = False

        @property
        def url(self):
            raise RuntimeError("url boom")

    ex = ContentExtractor()
    info = _run(ex.extract_page_info(UrlBoomPage()))
    assert info == {"url": "", "title": "t"}
