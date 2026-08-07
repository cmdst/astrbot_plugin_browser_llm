"""core.extract — 网页内容提取。

负责从 Playwright Page 提取可注入 LLM 的正文文本与编号链接索引，
按 max_chars / max_links 上限截断。所有方法对 playwright 异常做
安全降级（返回空结果或尽力而为的字段），绝不向上抛出未捕获异常
——调用方是 LLM 工具，必须容错。

playwright 包不在此处导入：方法按鸭子类型接收 Page 对象（具备
inner_text / evaluate / title / url 接口），单测时可用假 Page。
"""

import logging
import re

logger = logging.getLogger(__name__)

# 正文截断标记：超长时连接开头与结尾段落，避免丢失尾部关键信息。
_TRUNCATE_MARKER = "...（内容过长已截断）..."

# 降级取正文的 JS：inner_text('body') 失败时的回退路径。
_FALLBACK_INNERTEXT_JS = "document.body ? document.body.innerText : ''"

# 收集链接的 JS：返回 [{text, href, raw}]。
#   text: 链接可见文本（去空白）；href: 浏览器绝对化后的地址；
#   raw:  原始 href 属性值（用于过滤纯锚点 / javascript:）。
_LINKS_JS = """
Array.from(document.querySelectorAll('a[href]')).map(function (a) {
  return {
    text: (a.innerText || a.textContent || '').trim(),
    href: a.href,
    raw: a.getAttribute('href') || ''
  };
})
"""


class ContentExtractor:
    """网页内容提取器：正文文本与编号链接索引。

    纯状态类，不持有 playwright 对象；所有方法接收 Page 并返回
    字符串 / dict，异常时降级为空值。
    """

    def __init__(self) -> None:
        """初始化内容提取器（无状态）。"""

    # ------------------------------------------------------------
    # 正文提取
    # ------------------------------------------------------------

    async def extract_text(self, page, max_chars: int = 4000) -> str:
        """提取页面可见正文：压缩空白并按 max_chars 截断（保留首尾）。

        优先 page.inner_text('body')，失败降级取 document.body.innerText；
        超长时保留开头与结尾各一段，中间以截断标记连接，避免丢失
        页面尾部关键信息。

        Args:
            page: Playwright Page（或具备 inner_text/evaluate 的 mock）。
            max_chars: 返回文本的最大字符数。

        Returns:
            str: 提取后的正文；提取失败返回空字符串。
        """
        raw = ""
        try:
            raw = await page.inner_text("body")
        except Exception as e:  # noqa: BLE001 — playwright 异常需全部兜底
            logger.debug("inner_text('body') 失败，降级取 document.body.innerText: %s", e)
            try:
                raw = await page.evaluate(_FALLBACK_INNERTEXT_JS)
            except Exception as e2:  # noqa: BLE001
                logger.warning("提取页面文本失败（降级路径也失败）: %s", e2)
                return ""
        if not isinstance(raw, str):
            raw = str(raw)
        # 压缩连续空白（含换行/制表符）为单个空格。
        text = re.sub(r"\s+", " ", raw).strip()
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        head_len = max(1, (max_chars - len(_TRUNCATE_MARKER)) // 2)
        tail_len = max_chars - len(_TRUNCATE_MARKER) - head_len
        if tail_len <= 0:
            return text[:head_len] + _TRUNCATE_MARKER
        return text[:head_len] + _TRUNCATE_MARKER + text[-tail_len:]

    # ------------------------------------------------------------
    # 链接提取
    # ------------------------------------------------------------

    async def extract_links(self, page, max_links: int = 20) -> str:
        """提取页面链接并生成编号列表，去重且不超过 max_links 条。

        过滤空 href / 纯锚点（#...）/ javascript: 链接；同 href 保留
        首个；按页面出现顺序编号。超限时末尾追加提示行。

        Args:
            page: Playwright Page（或具备 evaluate 的 mock）。
            max_links: 最多返回的链接条数。

        Returns:
            str: 形如 "[1] 标题 → url" 的多行文本；失败返回空字符串。
        """
        try:
            raw_links = await page.evaluate(_LINKS_JS)
        except Exception as e:  # noqa: BLE001
            logger.warning("提取页面链接失败: %s", e)
            return ""
        if not isinstance(raw_links, list):
            return ""
        # 过滤 + 去重：dict 保序，同 href 保留首个。
        seen: dict[str, str] = {}
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            href = item.get("href") or ""
            raw = item.get("raw") or ""
            text = item.get("text") or ""
            if not href or not raw.strip():
                continue
            if raw.lstrip().startswith("#"):
                continue
            if href.lower().startswith("javascript:"):
                continue
            if href not in seen:
                seen[href] = text
        total = len(seen)
        lines = []
        for idx, (href, text) in enumerate(seen.items(), start=1):
            if idx > max_links:
                break
            lines.append(f"[{idx}] {text} → {href}")
        if total > max_links:
            lines.append(f"...（共 {total} 个链接，仅显示前 {max_links} 个）")
        return "\n".join(lines)

    # ------------------------------------------------------------
    # 页面基本信息
    # ------------------------------------------------------------

    async def extract_page_info(self, page) -> dict:
        """提取页面基本信息 {url, title}，异常时对应字段给空串。

        Args:
            page: Playwright Page（或具备 url 属性 / title 方法的 mock）。

        Returns:
            dict: {"url": str, "title": str}。
        """
        url = ""
        title = ""
        try:
            url = page.url or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 page.url 失败: %s", e)
        try:
            title = await page.title() or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("读取 page.title() 失败: %s", e)
        return {"url": url, "title": title}
