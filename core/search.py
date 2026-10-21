"""搜索服务：provider 池的并行波次检索、合并去重、缓存与脱敏。

设计约束（v4 / S4，见 spec/20261021-search-tool-and-config-v4.md）：

- **纯 Python + asyncio**：不 import ``astrbot``，也不 import 核心私有
  ``web_search_tools``（那是 16 个 ``web_search_<provider>`` 内置工具的实现，
  本插件与之并存、不接管、不读它的 key）。
- **密钥只由调用方传入**：``SearchService(secrets=...)`` 由 ``main.py``
  从**本插件配置** ``search.providers[].api_key`` 组装；本模块不读任何配置、
  不读 ``provider_settings.websearch_*_key``、不读 ``deep_research``。
- **失败不抛异常**：单个 provider 的失败只落 ``attempts``（结构化错误码），
  调用方（LLM 工具）据此返回 JSON，而不是让异常穿透给模型。

唯一检索入口是 ``SearchService.search_waves()`` —— v4 并行波次：每波 ≤
``max_parallel`` 个「尚未试过」的 provider 同时发起，波次上限
``max_rounds``，结果按 provider 权重 / 原始 rank / 轮次 / 去重键确定性
排序后合并。v3 的串行 fallback（``search()`` / ``SearchOutcome``）在
v1.4.0 冗余清理时删除：``main.py`` 零调用，只有测试在测（audit §1）。

「搜索步数」语义（captain 裁定 D5）：``max_rounds`` 是**波次上限**，一次
调用实际最多尝试 ``min(可用池, max_parallel × max_rounds)`` 个 provider；
**不是**「每次对话里 LLM 最多调用几次搜索工具」的会话级计数。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, MutableMapping, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------

#: ``snippet`` 最终长度上限（先脱敏再截断，见 spec §11.2 #13/#16）。
SNIPPET_MAX_CHARS = 300

#: 工具返回体（JSON 字符串）目标字节上限；超限先砍 ``fetched.text``、再砍
#: ``results`` 尾部，并记 ``notes += ["payload_truncated"]``。
PAYLOAD_MAX_BYTES = 24 * 1024

#: 全失败时 ``code`` 的取值优先级（越靠前越"该先处置"）。
ERROR_PRIORITY: tuple[str, ...] = (
    "auth",
    "rate_limited",
    "http_5xx",
    "timeout",
    "network",
    "bad_response",
    "empty",
    "no_provider",
)

#: 不参与去重键的跟踪参数（其余参数按名排序后重建，保证键稳定）。
_TRACKING_PARAMS = frozenset(
    {"gclid", "fbclid", "msclkid", "spm", "scm", "_hsenc", "_hsmi"}
)
_TRACKING_PREFIXES = ("utm_",)

#: 视为「可重试（下次调用还有意义）」的错误码。
_RETRYABLE_CODES = frozenset({"timeout", "network", "http_5xx"})

#: 触发冷却的错误码（配额/鉴权类，短时间重试只会继续失败）。
_COOLDOWN_CODES = frozenset({"auth", "rate_limited"})

#: 波级硬停相对「剩余预算」的宽限（秒）：让单源到点先触发，保住本波已得结果
#: （必须 < ``main.py`` 外层 ``budget + 1`` 的余量）。
_BUDGET_GRACE = 0.5

#: 「跳过码」：这些源**根本没发请求**（池里没填 key / 被停用 / 冷却中 / 模板
#: 未知 / base_url 非法），不代表搜索能力失败。有结果的调用只有遇到**真实
#: 失败源**才降级成 ``partial``，否则应为 ``ok``（spec §6.5 / t6 L-1）。
SKIP_CODES = frozenset(
    {"missing_key", "disabled", "cooldown", "unknown_template", "bad_config"}
)


def _is_skipped(attempt: "SearchAttempt") -> bool:
    """该尝试是「没发请求就被跳过」而非「发出去失败了」。"""
    return attempt.outcome == "skipped" or attempt.code in SKIP_CODES

#: 状态文案前缀（与插件既有【错误】/【拒绝】风格一致）。
_STATUS_PREFIX = {"failed": "【错误】", "disabled": "【拒绝】", "denied": "【拒绝】"}


# ---------------------------------------------------------------
# 脱敏（D8：从 core/delivery.py 迁入，本模块是唯一消费者）
# ---------------------------------------------------------------


def redact_secrets(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """把完整密钥及其首尾片段替换掉（覆盖日志/异常/返回体三种泄漏面）。

    行为与 v3 的 ``core/delivery.py::redact_secrets`` 一致（迁移不改语义）：
    先替换完整密钥，长度 ≥ 8 时再替换前 4/后 4 字符片段（防止日志里只截到
    半截 key 仍可拼回）。
    """
    if not text:
        return text
    out = str(text)
    for secret in secrets:
        if not secret:
            continue
        out = out.replace(str(secret), "[redacted]")
        if len(secret) >= 8:
            out = out.replace(str(secret)[:4], "[redacted]")
            out = out.replace(str(secret)[-4:], "[redacted]")
    return out


# ---------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------


@dataclass(frozen=True)
class SearchResult:
    """单个 provider 返回的一条结果（未合并、未排序）。"""

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    rank: int = 0


class ProviderError(RuntimeError):
    """provider 侧的结构化失败（``code`` 见 spec §6.5 错误码表）。"""

    def __init__(self, code: str, message: str = "", *, http_status: int | None = None):
        super().__init__(message or code)
        self.code = code
        #: 触发该错误码的 HTTP 状态码（无网络阶段失败时为 None）。
        self.http_status = http_status


class SearchProvider(Protocol):
    """一个搜索后端的实现协议（``core/search_providers.py`` 提供 5 个实现）。"""

    name: str

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]: ...


@dataclass
class ProviderEntry:
    """provider 池里的一个**实例**（同一模板可配多份，各带自己的 key）。

    对应 ``search.providers`` 数组的一个条目；``name`` 是显示名
    （``brave#1``），进日志与 ``attempts``，**不含密钥**。
    """

    name: str
    template: str
    provider: SearchProvider | None = None
    enabled: bool = True
    api_key: str = ""
    weight: int = 50
    timeout_s: float = 0.0  # 0 = 用全局 provider_timeout
    limit: int = 0  # 0 = 用全局 max_results
    index: int = 1
    #: 配置层错误码（如 ``bad_config``：base_url 非法/命中内网）；有值时
    #: 组波前直接记该码、不发请求。空串表示配置无问题。
    config_error: str = ""
    #: 结果 URL 的字面校验回调（True=放行）；None 表示不过滤。
    url_guard: Callable[[str], bool] | None = None

    def effective_timeout(self, default: float) -> float:
        return float(self.timeout_s) if self.timeout_s else float(default)

    def effective_limit(self, default: int) -> int:
        return int(self.limit) if self.limit else int(default)


@dataclass
class SearchAttempt:
    """一次 provider 尝试的结构化记录（进返回体 ``attempts[]``）。"""

    provider: str
    template: str
    round: int
    outcome: str
    code: str
    http: int | None = None
    elapsed_ms: int = 0
    count: int = 0
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "template": self.template,
            "round": self.round,
            "outcome": self.outcome,
            "code": self.code,
            "http": self.http,
            "elapsed_ms": self.elapsed_ms,
            "count": self.count,
            "retryable": self.retryable,
        }


@dataclass
class MergedResult:
    """去重合并后的一条结果（进返回体 ``results[]``）。"""

    title: str
    url: str
    snippet: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    fetched: dict[str, Any] | None = None
    rank: int = 0
    key: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rank": self.rank,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "sources": [dict(s) for s in self.sources],
        }
        if self.fetched is not None:
            payload["fetched"] = dict(self.fetched)
        return payload


@dataclass
class WaveResult:
    """``search_waves()`` 的返回值：结果 + 尝试 + 轮数 + 状态。"""

    results: list[MergedResult] = field(default_factory=list)
    attempts: list[SearchAttempt] = field(default_factory=list)
    rounds_used: int = 0
    max_rounds: int = 0
    notes: list[str] = field(default_factory=list)
    cache: str = "miss"
    elapsed_ms: int = 0
    status: str = "failed"
    code: str = "no_provider"
    providers_effective: list[str] = field(default_factory=list)

    @property
    def error_codes(self) -> list[str]:
        out: list[str] = []
        for attempt in self.attempts:
            if attempt.code in ("ok", "cooldown"):
                continue
            tag = f"{attempt.provider}:{attempt.code}"
            if tag not in out:
                out.append(tag)
        return out


# ---------------------------------------------------------------
# 去重 / 合并 / 排序（纯函数，便于单测）
# ---------------------------------------------------------------


def dedupe_key(url: str) -> str:
    """URL 去重键：小写 scheme/host、去默认端口、去 fragment、去跟踪参数。

    规则（spec §7.4）：①scheme/host 小写并去默认端口；②path 去尾部 ``/``
    （根路径保留）；③删除 ``utm_*``/``gclid``/``fbclid``/``msclkid``/``spm``/
    ``scm``/``_hsenc``/``_hsmi``；④其余查询参数按名排序后重建。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    netloc = host
    if parsed.username or parsed.password:
        # 凭据不应进搜索结果；保留占位避免两条不同凭据的 URL 被错误合并。
        netloc = f"[cred]@{host}"
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
        and not any(k.startswith(prefix) for prefix in _TRACKING_PREFIXES)
    ]
    query = "&".join(f"{k}={v}" for k, v in sorted(pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def clean_result(
    item: Any,
    *,
    secrets: list[str] | tuple[str, ...] = (),
    snippet_max: int = SNIPPET_MAX_CHARS,
) -> SearchResult | None:
    """把 provider 返回值净化为 ``SearchResult``：**先脱敏、再截断**。

    - ``title``/``snippet``/``url`` 都过 ``redact_secrets``（顺序不能反：先截断
      会把密钥截成"半截 key"，反而更难识别）；
    - ``url`` 为空则丢弃（无 URL 的结果无法引用）；
    - ``snippet`` 超长按 ``snippet_max`` 截断并以 ``…`` 结尾。
    """
    url = redact_secrets(str(getattr(item, "url", "") or "").strip(), secrets)
    if not url:
        return None
    title = redact_secrets(str(getattr(item, "title", "") or "").strip(), secrets)
    snippet = redact_secrets(str(getattr(item, "snippet", "") or "").strip(), secrets)
    if snippet_max and len(snippet) > snippet_max:
        snippet = snippet[:snippet_max].rstrip() + "…"
    return SearchResult(
        title=title,
        url=url,
        snippet=snippet,
        source=str(getattr(item, "source", "") or ""),
        rank=int(getattr(item, "rank", 0) or 0),
    )


def merge_batches(
    batches: list[tuple[ProviderEntry, int, list[SearchResult]]],
    *,
    max_results: int = 0,
) -> list[MergedResult]:
    """按去重键合并多批结果并确定性排序。

    Args:
        batches: ``[(provider 实例, 轮次, 该 provider 清洗后的结果)]``。
        max_results: 返回条数上限（0/负值表示不裁剪）。

    Returns:
        合并后的 ``MergedResult`` 列表；同键保留 ``weight`` 更高者的记录
        （同 weight 保留轮次更早者），``title``/``snippet`` 取更长（空串不
        参与覆盖），``sources`` 追加 ``{provider, rank, weight, round}``，
        ``rank`` 最后统一重排。

    排序键：``(-max(weight), 该源原始 rank, 轮次, 去重键)`` —— 权重（= 运营方
    在配置里的声明顺序）优先，同源内保持搜索商原始相关度。
    """
    merged: dict[str, MergedResult] = {}
    anchors: dict[str, tuple[int, int, int]] = {}

    for entry, round_no, results in batches:
        weight = int(entry.weight or 0)
        for idx, item in enumerate(results, start=1):
            key = dedupe_key(item.url)
            if not key:
                continue
            rank = int(item.rank or idx)
            source = {
                "provider": entry.name,
                "rank": rank,
                "weight": weight,
                "round": round_no,
            }
            current = merged.get(key)
            if current is None:
                merged[key] = MergedResult(
                    title=item.title,
                    url=item.url,
                    snippet=item.snippet,
                    sources=[source],
                    key=key,
                )
                anchors[key] = (-weight, rank, round_no)
                continue

            prev_weight = max(s["weight"] for s in current.sources)
            prev_round = min(s["round"] for s in current.sources)
            prefer_incoming = weight > prev_weight or (
                weight == prev_weight and round_no < prev_round
            )
            if prefer_incoming:
                current.url = item.url
            if len(item.title or "") > len(current.title or ""):
                current.title = item.title
            if len(item.snippet or "") > len(current.snippet or ""):
                current.snippet = item.snippet
            current.sources.append(source)
            anchors[key] = (
                min(anchors[key][0], -weight),
                min(anchors[key][1], rank),
                min(anchors[key][2], round_no),
            )

    ordered = sorted(merged.values(), key=lambda r: (anchors[r.key], r.key))
    for idx, item in enumerate(ordered, start=1):
        item.rank = idx
        # sources 保持**追加顺序**（波次内按 provider 声明顺序），便于阅读与断言；
        # 排序键只作用于结果本身（见上面的 anchors），不再重排 sources。
    if max_results and max_results > 0:
        ordered = ordered[:max_results]
    return ordered


def _pick_code(attempts: list[SearchAttempt]) -> str:
    """全失败时按优先级挑一个最能指导处置的错误码。

    「跳过码」（``SKIP_CODES``）不算失败：池里只有未配置/停用的源时统一报
    ``no_provider``（对模型与运营方都是最可执行的结论），具体原因仍在
    ``attempts[]`` 里逐条可见。
    """
    codes = {a.code for a in attempts if a.code != "ok" and not _is_skipped(a)}
    for code in ERROR_PRIORITY:
        if code in codes:
            return code
    for attempt in attempts:
        if attempt.code != "ok" and not _is_skipped(attempt):
            return attempt.code
    return "no_provider"


def summarize(
    *,
    status: str,
    code: str,
    results: list[MergedResult] | list[SearchResult],
    attempts: list[SearchAttempt],
    query: str,
) -> str:
    """生成一句话结论（失败以【错误】开头，与插件既有文案风格一致）。

    「跳过码」（``missing_key`` / ``disabled`` / ``cooldown`` /
    ``unknown_template`` / ``bad_config``）不算失败源：池里有空 key 的源不应
    让每次调用都显示"部分失败"（t6 L-1）。
    """
    if status == "ok":
        return f"找到 {len(results)} 条与「{query}」相关的结果"
    if status == "partial":
        failed = [a for a in attempts if a.code != "ok" and not _is_skipped(a)]
        detail = ", ".join(f"{a.provider}:{a.code}" for a in failed[:5])
        return (
            f"部分搜索后端失败（{detail}），已返回 {len(results)} 条结果"
        )
    failed = [a for a in attempts if a.code != "ok" and not _is_skipped(a)]
    if not failed:
        return f"{_STATUS_PREFIX['failed']}没有可用的搜索后端（{code}）"
    detail = ", ".join(f"{a.provider}:{a.code}" for a in failed[:5])
    more = "" if len(failed) <= 5 else f" 等 {len(failed)} 个"
    return (
        f"{_STATUS_PREFIX['failed']}"
        f"{len(failed)} 个搜索后端全部失败（{detail}{more}）"
    )


def resolve_status(results: list[Any], attempts: list[SearchAttempt]) -> tuple[str, str]:
    """由「结果 + 尝试」推导 ``(status, code)``。

    - 有结果且无**真实**失败源 ⇒ ``ok``（跳过码不算失败）；
    - 有结果但有真实失败源 ⇒ ``partial``；
    - 无结果 ⇒ ``failed`` + 优先级最高的错误码（只有跳过码时是 ``no_provider``）。
    """
    failed = [a for a in attempts if a.code != "ok" and not _is_skipped(a)]
    if results:
        return ("partial" if failed else "ok"), "ok"
    return "failed", _pick_code(attempts)


# ---------------------------------------------------------------
# 搜索服务
# ---------------------------------------------------------------


class SearchService:
    """provider 池 + 冷却 + TTL 缓存（``search_waves()`` 是唯一检索入口）。

    v4 起只保留**并行波次**入口：v3 的串行 fallback（``search()`` /
    ``SearchOutcome``）在 ``main.py`` 里零调用，属"测试全绿但生产不执行"
    的假覆盖，v1.4.0 清理时删除（audit §1、spec §11.1）。
    """

    def __init__(
        self,
        *,
        entries: list[ProviderEntry],
        timeout_s: float = 10.0,
        max_results: int = 5,
        rate_limit_cooldown_s: float = 60.0,
        secrets: list[str] | tuple[str, ...] = (),
        clock: Callable[[], float] = time.monotonic,
        cache_ttl_s: float = 0.0,
    ):
        self.entries: list[ProviderEntry] = list(entries)
        self.timeout_s = float(timeout_s)
        self.max_results = int(max_results)
        self.rate_limit_cooldown_s = float(rate_limit_cooldown_s)
        self._secrets = tuple(secrets)
        self._clock = clock
        self.cache_ttl_s = float(cache_ttl_s)
        self._cooldown_until: dict[str, float] = {}
        self._cache: dict[str, tuple[float, WaveResult]] = {}

    # ---------------- 公共读取 ----------------

    def in_cooldown(self, name: str) -> bool:
        return self._clock() < self._cooldown_until.get(name, 0.0)

    def _set_cooldown(self, name: str) -> None:
        if self.rate_limit_cooldown_s <= 0:
            return
        self._cooldown_until[name] = self._clock() + self.rate_limit_cooldown_s

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------------- v4 并行波次 ----------------

    async def search_waves(
        self,
        query: str,
        *,
        max_results: int | None = None,
        max_rounds: int | None = None,
        max_parallel: int | None = None,
        provider_timeout: float | None = None,
        time_budget_s: float | None = None,
        provider_filter: list[str] | None = None,
        use_cache: bool = True,
        progress: MutableMapping[str, Any] | None = None,
    ) -> WaveResult:
        """并行波次检索（spec §7.1）。

        Args:
            query: 检索词（去空白；空 ⇒ ``failed/empty_query``）。
            max_results: 返回条数上限，缺省用实例值。
            max_rounds: **波次上限**（搜索步数），缺省 1。
            max_parallel: 单波并发上限，缺省取 ``max_results``。
            provider_timeout: 单源超时（秒）；模板 ``timeout`` 可覆盖。
                **单源等待会被压缩到「剩余总预算」**（``min(timeout, remaining)``），
                慢源不会吃掉整次调用的预算（t6 M-1）。
            time_budget_s: 本次调用的**阶段预算**（秒）；到点不再开新波，已有
                结果保留并记 ``total_timeout``。
            provider_filter: ``providers=`` 参数解析出的白名单（池外名字记
                ``unknown_provider``）；``None`` 表示不过滤。
            use_cache: 是否允许命中 TTL 缓存。
            progress: 可选**可变容器**：每波结束都写入当时的
                ``results`` / ``attempts`` / ``rounds_used`` / ``status`` /
                ``code`` / ``notes`` / ``providers_effective``。调用方在
                ``asyncio.wait_for`` 外层超时（协程被取消）时用它渲染
                ``partial``，而不是把已得结果整批丢掉（spec §7.3 / t6 M-1）。

        Returns:
            ``WaveResult``：``status`` ∈ {ok, partial, failed}；**永不抛异常**
            （预算耗尽/全部失败都落在返回值里）。
        """
        started = self._clock()
        limit = int(max_results if max_results is not None else self.max_results)
        rounds_cap = max(1, int(max_rounds if max_rounds is not None else 1))
        parallel = max(1, int(max_parallel if max_parallel is not None else max(1, limit)))
        per_source = float(
            provider_timeout if provider_timeout is not None else self.timeout_s
        )

        result = WaveResult(max_rounds=rounds_cap)
        text = (query or "").strip()
        if not text:
            result.status, result.code = "failed", "empty_query"
            result.elapsed_ms = int((self._clock() - started) * 1000)
            return result

        cache_key = json.dumps(
            [text, limit, rounds_cap, parallel, round(per_source, 3), provider_filter or []],
            ensure_ascii=False,
        )
        if use_cache and self.cache_ttl_s > 0:
            hit = self._cache.get(cache_key)
            if hit is not None and self._clock() - hit[0] <= self.cache_ttl_s:
                cached = hit[1]
                return replace(
                    cached,
                    cache="hit",
                    results=[replace(item) for item in cached.results],
                    attempts=[replace(a) for a in cached.attempts],
                    notes=list(cached.notes),
                )

        entries = list(self.entries)
        if provider_filter is not None:
            wanted = [name.strip() for name in provider_filter if name.strip()]
            known = {e.name for e in entries}
            for name in wanted:
                if name not in known:
                    result.attempts.append(
                        SearchAttempt(
                            provider=name,
                            template="",
                            round=0,
                            outcome="skipped",
                            code="unknown_provider",
                        )
                    )
            allowed = {name for name in wanted if name in known}
            entries = [e for e in entries if e.name in allowed]

        # 组波**前**过滤：不占 max_parallel 槽、不计 rounds_used（spec §7.1-2）。
        ready: list[ProviderEntry] = []
        for entry in entries:
            if entry.config_error:
                result.attempts.append(
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=0,
                        outcome="skipped",
                        code=entry.config_error,
                    )
                )
                continue
            if entry.provider is None:
                result.attempts.append(
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=0,
                        outcome="skipped",
                        code="unknown_template",
                    )
                )
                continue
            if not entry.enabled:
                result.attempts.append(
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=0,
                        outcome="skipped",
                        code="disabled",
                    )
                )
                continue
            if not entry.api_key:
                result.attempts.append(
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=0,
                        outcome="skipped",
                        code="missing_key",
                    )
                )
                continue
            if self.in_cooldown(entry.name):
                result.attempts.append(
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=0,
                        outcome="skipped",
                        code="cooldown",
                    )
                )
                continue
            ready.append(entry)

        if not ready:
            # 池内没有"能真正发请求"的源：顶层码统一 no_provider，具体原因
            # （missing_key / disabled / cooldown / unknown_template / bad_config）
            # 都在 attempts 里逐条可见（spec §6.5 对 no_provider 的定义）。
            result.status = "failed"
            result.code = "no_provider"
            result.elapsed_ms = int((self._clock() - started) * 1000)
            return result

        deadline = started + float(time_budget_s) if time_budget_s else None
        attempted: set[str] = set()
        batches: list[tuple[ProviderEntry, int, list[SearchResult]]] = []
        budget_exhausted = False
        pool_exhausted = False

        while result.rounds_used < rounds_cap:
            # 预算检查必须在组波**之前**：先判预算（记 total_timeout），
            # 再判池是否还有没试过的源（池耗尽属正常收尾，不记超时）。
            if deadline is not None and self._clock() >= deadline:
                budget_exhausted = True
                break
            wave = [e for e in ready if e.name not in attempted][:parallel]
            if not wave:
                pool_exhausted = True
                break
            result.rounds_used += 1
            round_no = result.rounds_used
            attempted.update(e.name for e in wave)
            wave_out, wave_timed_out, wave_budget_hit = await self._run_wave(
                wave,
                text,
                round_no=round_no,
                default_timeout=per_source,
                default_limit=limit,
                deadline=deadline,
            )
            for entry, attempt, items in wave_out:
                result.attempts.append(attempt)
                if attempt.code in _COOLDOWN_CODES:
                    self._set_cooldown(entry.name)
                if items:
                    batches.append((entry, round_no, items))
            if wave_timed_out:
                result.notes.append("wave_timeout")
            if wave_budget_hit and "total_timeout" not in result.notes:
                # 本波有源的等待被预算截短 ⇒ 也是"预算到点"，与"没开新波"同码。
                result.notes.append("total_timeout")
                budget_exhausted = True
            # 每波增量交给调用方：外层超时（协程被取消）时仍能渲染 partial。
            self._publish_progress(progress, result, batches, limit)
            if len(merge_batches(batches)) >= limit:
                break

        merged = merge_batches(batches, max_results=limit)
        result.results = merged
        result.status, result.code = resolve_status(merged, result.attempts)
        if pool_exhausted and not budget_exhausted:
            # 池耗尽（没有没试过的源了）属正常收尾，与预算耗尽区分开记录：
            # 只有后者才写 total_timeout。
            result.notes.append("pool_exhausted")
        if budget_exhausted:
            if "total_timeout" not in result.notes:
                result.notes.append("total_timeout")
            if merged:
                result.status, result.code = "partial", "ok"
        result.providers_effective = [
            a.provider for a in result.attempts if a.code == "ok"
        ]
        result.elapsed_ms = int((self._clock() - started) * 1000)
        self._publish_progress(progress, result, batches, limit)
        if self.cache_ttl_s > 0 and result.status in ("ok", "partial"):
            self._cache[cache_key] = (self._clock(), replace(result))
        return result

    def _publish_progress(
        self,
        progress: MutableMapping[str, Any] | None,
        result: WaveResult,
        batches: list[tuple[ProviderEntry, int, list[SearchResult]]],
        limit: int,
    ) -> None:
        """把「当前已得结果」增量写入调用方容器（外层超时兜底用）。

        只做快照，不改 ``result``：调用方拿到的是与本次波次一致的
        ``results`` / ``attempts`` / ``rounds_used`` / ``status`` / ``code``。
        """
        if progress is None:
            return
        merged_now = merge_batches(batches, max_results=limit)
        status, code = resolve_status(merged_now, result.attempts)
        progress["results"] = merged_now
        progress["attempts"] = [replace(a) for a in result.attempts]
        progress["rounds_used"] = result.rounds_used
        progress["max_rounds"] = result.max_rounds
        progress["status"] = status
        progress["code"] = code
        progress["notes"] = list(result.notes)
        progress["providers_effective"] = [
            a.provider for a in result.attempts if a.code == "ok"
        ]

    # -- 波次执行 -------------------------------------------------

    async def _run_wave(
        self,
        wave: list[ProviderEntry],
        query: str,
        *,
        round_no: int,
        default_timeout: float,
        default_limit: int,
        deadline: float | None = None,
    ) -> tuple[
        list[tuple[ProviderEntry, SearchAttempt, list[SearchResult]]], bool, bool
    ]:
        """并行执行一波：单源超时隔离 + 预算压缩 + 外层兜底。

        Returns:
            ``(out, timed_out, budget_hit)``：``timed_out`` 表示波级硬停触发；
            ``budget_hit`` 表示至少一个源的等待被**剩余预算**截短（即预算真的
            切掉了工作，调用方据此记 ``total_timeout``）。

        单源等待 = ``min(模板/全局 timeout, 剩余总预算)``：慢源只会用掉
        **剩余**预算，不会吃满 ``provider_timeout`` 把整次调用拖到外层超时
        （t6 M-1）。外层兜底仍保留，防某源漏 ``await`` 卡整波。
        """

        clamped_by_budget: list[str] = []

        async def run_one(
            entry: ProviderEntry,
        ) -> tuple[SearchAttempt, list[SearchResult]]:
            entry_timeout = entry.effective_timeout(default_timeout)
            remaining = (
                None if deadline is None else max(0.0, deadline - self._clock())
            )
            timeout_s = (
                entry_timeout
                if remaining is None
                else min(entry_timeout, remaining)
            )
            if remaining is not None and timeout_s < entry_timeout:
                # 预算把这一源的等待截短了：记下来给调用方判 total_timeout。
                clamped_by_budget.append(entry.name)
            limit = entry.effective_limit(default_limit)
            began = self._clock()
            attempt = SearchAttempt(
                provider=entry.name,
                template=entry.template,
                round=round_no,
                outcome="error",
                code="bad_response",
            )
            items: list[SearchResult] = []
            if timeout_s <= 0:
                # 预算已耗尽：不发请求，直接记 timeout（保留结构化轨迹）。
                attempt.outcome, attempt.code = "timeout", "timeout"
                attempt.retryable = True
                attempt.elapsed_ms = 0
                return attempt, []
            try:
                assert entry.provider is not None  # 组波前已过滤，此处仅类型收敛
                raw_results = await asyncio.wait_for(
                    entry.provider.search(query, limit=limit, timeout=timeout_s),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                attempt.outcome, attempt.code = "timeout", "timeout"
                attempt.retryable = True
            except ProviderError as exc:
                attempt.outcome = "error"
                attempt.code = exc.code or "bad_response"
                attempt.retryable = attempt.code in _RETRYABLE_CODES
                attempt.http = getattr(exc, "http_status", None)
            except Exception as exc:  # noqa: BLE001 — 单源异常只影响自己
                attempt.outcome = "error"
                attempt.code = _classify_exception(exc)
                attempt.retryable = attempt.code in _RETRYABLE_CODES
            else:
                items = [
                    item
                    for item in (
                        clean_result(raw, secrets=self._secrets)
                        for raw in raw_results or []
                    )
                    if item is not None
                ]
                blocked = 0
                guard = entry.url_guard
                if guard is not None and items:
                    kept: list[SearchResult] = []
                    for item in items:
                        try:
                            if guard(item.url):
                                kept.append(item)
                            else:
                                blocked += 1
                        except Exception:  # noqa: BLE001 — 校验器异常按拦截处理
                            blocked += 1
                    items = kept
                attempt.count = len(items)
                if items:
                    attempt.outcome, attempt.code = "ok", "ok"
                elif blocked:
                    # 结果全部未过 SSRF 字面校验：可观测但不影响其它源。
                    attempt.outcome, attempt.code = "blocked", "ssrf_blocked"
                else:
                    attempt.outcome, attempt.code = "empty", "empty"
            attempt.elapsed_ms = int((self._clock() - began) * 1000)
            return attempt, items

        slowest = max(e.effective_timeout(default_timeout) for e in wave) + 1.0
        if deadline is None:
            budget = slowest
        else:
            # 波级硬停 = min(最慢源+1, 剩余预算 + grace)。grace 让"单源到点"
            # 先于"整波放弃"发生，避免两个计时器同刻触发时丢掉本波已得结果；
            # grace 仍远小于 main.py 的外层 ``budget + 1`` 余量。
            budget = min(slowest, max(0.0, deadline - self._clock()) + _BUDGET_GRACE)
        budget = max(0.001, budget)
        timed_out = False
        gathered: list[Any] = []
        try:
            gathered = await asyncio.wait_for(
                asyncio.gather(*(run_one(e) for e in wave), return_exceptions=True),
                timeout=budget,
            )
            timed_out = False
        except asyncio.TimeoutError:
            # 外层兜底触发：整波放弃，逐源记 timeout（结果丢失但不会挂死）。
            timed_out = True
            gathered = [
                (
                    SearchAttempt(
                        provider=entry.name,
                        template=entry.template,
                        round=round_no,
                        outcome="timeout",
                        code="timeout",
                        retryable=True,
                    ),
                    [],
                )
                for entry in wave
            ]

        out: list[tuple[ProviderEntry, SearchAttempt, list[SearchResult]]] = []
        for entry, item in zip(wave, gathered):
            if isinstance(item, BaseException):
                out.append(
                    (
                        entry,
                        SearchAttempt(
                            provider=entry.name,
                            template=entry.template,
                            round=round_no,
                            outcome="error",
                            code=_classify_exception(item),
                            retryable=True,
                        ),
                        [],
                    )
                )
            elif isinstance(item, tuple) and len(item) == 2:
                attempt, items = item
                out.append((entry, attempt, list(items)))
            else:  # pragma: no cover — gather 只会返回二元组或异常
                out.append(
                    (
                        entry,
                        SearchAttempt(
                            provider=entry.name,
                            template=entry.template,
                            round=round_no,
                            outcome="error",
                            code="bad_response",
                        ),
                        [],
                    )
                )
        return out, timed_out, bool(clamped_by_budget)


def _classify_exception(exc: BaseException) -> str:
    """把任意异常归类成 spec §6.5 的错误码（只认**类型名**，不读异常文本）。"""
    name = type(exc).__name__.lower()
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "timeout"
    if "timeout" in name or "timedout" in name:
        return "timeout"
    if any(
        token in name
        for token in (
            "clienterror",
            "clientconnector",
            "connect",
            "dns",
            "ssl",
            "tls",
            "gaierror",
            "oserror",
        )
    ):
        return "network"
    if any(token in name for token in ("json", "decode", "valueerror", "keyerror", "typeerror")):
        return "bad_response"
    return "network"


# ---------------------------------------------------------------
# 返回体渲染（纯函数；main.py 只负责编排与安全过滤）
# ---------------------------------------------------------------


def render_payload(
    *,
    status: str,
    code: str,
    summary: str,
    query: str,
    rounds_used: int = 0,
    max_rounds: int = 0,
    providers_effective: list[str] | None = None,
    cache: str = "miss",
    elapsed_ms: int = 0,
    results: list[MergedResult] | None = None,
    attempts: list[SearchAttempt] | None = None,
    error_codes: list[str] | None = None,
    notes: list[str] | None = None,
    hint: str = "",
    max_bytes: int = PAYLOAD_MAX_BYTES,
) -> str:
    """渲染工具返回的 JSON 字符串（三态同一 schema，spec §6.4）。

    - ``ok`` = ``status in {ok, partial}``（有结果即 true）；
    - 体积超 ``max_bytes`` 时先砍 ``fetched.text``、再砍 ``results`` 尾部，
      并追加 ``notes += ["payload_truncated"]``；
    - 失败时 ``summary`` 已由调用方带上【错误】/【拒绝】前缀。
    """
    payload: dict[str, Any] = {
        "status": status,
        "ok": status in ("ok", "partial"),
        "code": code,
        "summary": summary,
        "query": query,
        "rounds_used": rounds_used,
        "max_rounds": max_rounds,
        "providers_effective": list(providers_effective or []),
        "cache": cache,
        "elapsed_ms": elapsed_ms,
        "results": [item.as_dict() for item in (results or [])],
        "attempts": [item.as_dict() for item in (attempts or [])],
        "error_codes": list(error_codes or []),
        "notes": list(notes or []),
    }
    if hint:
        payload["hint"] = hint

    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    # 第一刀：砍已抓取的正文（最占体积、且可重新抓）。
    for item in payload["results"]:
        fetched = item.get("fetched")
        if isinstance(fetched, dict) and fetched.get("text"):
            fetched["text"] = ""
            fetched["truncated"] = True
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) <= max_bytes:
        payload["notes"].append("payload_truncated")
        return json.dumps(payload, ensure_ascii=False)

    # 第二刀：从尾部砍结果，直到进预算（至少留 1 条）。
    while len(payload["results"]) > 1:
        payload["results"].pop()
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) <= max_bytes:
            break
    payload["notes"].append("payload_truncated")
    return json.dumps(payload, ensure_ascii=False)
