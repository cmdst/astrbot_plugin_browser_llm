"""core.safety — 安全过滤（内容禁词 + SSRF 防护）。

负责：
1. check_url：URL 协议白名单（仅 http/https）与内网地址拦截
   （SSRF 防护），阻断浏览器访问内网/环回/链路本地资源；
2. check_text：基于 banned_words 的禁词命中检测（大小写不敏感）。

本模块为纯 Python 实现，不依赖 playwright，可独立单测。
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# DNS 解析超时（秒）：acheck_url 通过 asyncio.wait_for 提供超时保护，
# 避免域名解析长时间阻塞事件循环。
_DNS_RESOLVE_TIMEOUT = 5.0

# 允许的 URL 协议白名单（其余如 javascript:/file:/data: 一律拒绝）。
_ALLOWED_SCHEMES = ("http", "https")

# 内网 / 保留地址网段（SSRF 黑名单）。
_INTERNAL_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),      # IPv4 环回
    ipaddress.ip_network("10.0.0.0/8"),       # 私网 A 段
    ipaddress.ip_network("172.16.0.0/12"),    # 私网 B 段
    ipaddress.ip_network("192.168.0.0/16"),   # 私网 C 段
    ipaddress.ip_network("169.254.0.0/16"),   # 链路本地（APIPA）
    ipaddress.ip_network("0.0.0.0/32"),       # 未指定地址
    ipaddress.ip_network("::1/128"),          # IPv6 环回
    ipaddress.ip_network("fe80::/10"),        # IPv6 链路本地
    ipaddress.ip_network("fc00::/7"),         # IPv6 唯一本地（ULA）
)


def _is_internal_ip(ip) -> bool:
    """判断 IP 是否命中内网/保留网段。

    额外处理 IPv4-mapped IPv6（如 ::ffff:127.0.0.1）：递归检查
    其映射的 IPv4 地址，防止绕过环回拦截。
    """
    if any(ip in net for net in _INTERNAL_NETWORKS):
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return _is_internal_ip(ip.ipv4_mapped)
    return False


def _try_inet_aton(hostname: str):
    """将 inet_aton 兼容的 IPv4 字面量还原为点分十进制。

    覆盖 SSRF 常见混淆形式：十进制整数（2130706433）、八进制
    （0177.0.0.1）、十六进制（0x7f000001）、短点分（127.1）。
    非合法 IPv4 字面量时返回 None。
    """
    try:
        return socket.inet_ntoa(socket.inet_aton(hostname))
    except (OSError, ValueError):
        return None


def _hostname_to_ip(hostname: str):
    """把主机名尝试解释为 IP 字面量（含混淆形式）；非 IP 时返回 None。

    优先使用 ipaddress（覆盖 IPv6 字面量如 [::1] 已剥离括号后的
    '::1' 与标准点分 IPv4），其次用 inet_aton 兜底混淆形式。
    """
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    dotted = _try_inet_aton(hostname)
    if dotted:
        try:
            return ipaddress.ip_address(dotted)
        except ValueError:
            pass
    return None


class SafetyFilter:
    """安全过滤器：内容禁词检测与 URL 内网拦截（SSRF 防护）。"""

    def __init__(self, banned_words: list[str], block_internal_ip: bool = True) -> None:
        """初始化安全过滤器。

        Args:
            banned_words: 禁词列表，check_text 做大小写不敏感的子串匹配。
            block_internal_ip: 为 True 时 check_url 拒绝内网/环回/
                链路本地地址；为 False 时仅做协议白名单检查。
        """
        self.banned_words = [w for w in (banned_words or []) if w]
        # 预小写化禁词，加速 check_text 的匹配。
        self._banned_lower = [w.lower() for w in self.banned_words]
        self.block_internal_ip = bool(block_internal_ip)

    # ------------------------------------------------------------
    # URL 检查
    # ------------------------------------------------------------

    def _precheck(self, url: str):
        """协议白名单 + 主机名提取 + IP 字面量内网检查（纯同步、无 DNS）。

        Returns:
            tuple[bool, str, str | None]: (ok, reason, hostname)。
            需 DNS 解析时 hostname 非 None；主机名本身为 IP 字面量时
            ok 已给出最终结论，hostname 为 None。
        """
        if not url or not isinstance(url, str):
            return False, "URL 为空", None
        url = url.strip()
        try:
            parsed = urlparse(url)
        except ValueError:
            return False, f"URL 解析失败: {url}", None
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            return False, f"仅允许 http/https 协议，收到: {scheme or '（无协议）'}", None
        hostname = parsed.hostname  # urlparse 已剥离 IPv6 字面量的方括号
        if not hostname:
            return False, "URL 缺少主机名", None
        ip = _hostname_to_ip(hostname)
        if ip is not None:
            if self.block_internal_ip and _is_internal_ip(ip):
                return False, f"禁止访问内网/保留地址: {hostname}", None
            return True, "", None
        return True, "", hostname

    def check_url(self, url: str) -> tuple[bool, str]:
        """同步检查 URL 是否允许访问（薄封装）。

        无运行中事件循环时委托 acheck_url（含 DNS 超时保护）；
        处于运行中事件循环（如 AstrBot 宿主）时回退同步 socket
        解析——该路径无超时保护且可能短暂阻塞事件循环，异步场景
        请优先使用 acheck_url。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acheck_url(url))
        return self._check_url_sync(url)

    async def acheck_url(self, url: str) -> tuple[bool, str]:
        """异步检查 URL（推荐）：协议白名单 + SSRF 内网拦截。

        DNS 解析使用事件循环的 getaddrinfo 并带超时保护，不阻塞
        事件循环；域名解析到任一内网地址即拒绝（防 DNS 混合应答）。

        Returns:
            tuple[bool, str]: (是否允许, 拒绝原因或空串)。
        """
        ok, reason, hostname = self._precheck(url)
        if not ok or hostname is None:
            return ok, reason
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(
                loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM),
                timeout=_DNS_RESOLVE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return False, f"域名解析超时: {hostname}"
        except socket.gaierror as e:
            return False, f"域名解析失败: {hostname} ({e})"
        except Exception as e:  # noqa: BLE001
            logger.warning("域名解析异常 host=%s: %s", hostname, e)
            return False, f"域名解析异常: {hostname}"
        return self._judge_addrinfos(hostname, infos)

    def _check_url_sync(self, url: str) -> tuple[bool, str]:
        """同步解析路径：协议/IP 字面量检查 + socket.getaddrinfo。"""
        ok, reason, hostname = self._precheck(url)
        if not ok or hostname is None:
            return ok, reason
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            return False, f"域名解析失败: {hostname} ({e})"
        except Exception as e:  # noqa: BLE001
            logger.warning("域名解析异常 host=%s: %s", hostname, e)
            return False, f"域名解析异常: {hostname}"
        return self._judge_addrinfos(hostname, infos)

    @staticmethod
    def _judge_addrinfos(hostname: str, infos) -> tuple[bool, str]:
        """判断 getaddrinfo 结果：任一内网地址即拒绝，全部外网放行。"""
        if not infos:
            return False, f"域名无解析结果: {hostname}"
        for info in infos:
            addr = info[4][0]
            try:
                ip_obj = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _is_internal_ip(ip_obj):
                return False, f"域名 {hostname} 解析到内网/保留地址: {addr}"
        return True, ""

    # ------------------------------------------------------------
    # 文本检查
    # ------------------------------------------------------------

    def check_text(self, text: str) -> tuple[bool, str]:
        """检查文本是否包含任一禁词（大小写不敏感、子串匹配）。

        Args:
            text: 待检查文本（网页正文 / 搜索结果等）。

        Returns:
            tuple[bool, str]: 未命中返回 (True, '')；命中返回
            (False, 命中的禁词原文)。
        """
        if not text:
            return True, ""
        lowered = text.lower()
        for idx, word_lower in enumerate(self._banned_lower):
            if word_lower in lowered:
                return False, self.banned_words[idx]
        return True, ""
