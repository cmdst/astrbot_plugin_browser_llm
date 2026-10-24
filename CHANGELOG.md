# CHANGELOG

本仓库为 `astrbot_plugin_browser_llm`（LLM浏览器插件）独立 git 仓库，由 AstrBot 主仓库
`data/` 目录（主仓库 .gitignore 忽略）迁移初始化而来。仅跟踪插件源码与测试，运行时
数据（`data/`：截图、运行时配置、临时文件）一律不入库。

## [v1.5.0] - 2026-10-25

### 特性：三个 LLM 工具支持后台异步执行（路线 D：插件内闭环、默认 off、零核心改动）

- **新增 `tools` 配置组（第 6 组，3 键，默认关闭）**：
  - `tools.background_mode`：`off`（默认，三工具全前台同步）/ `browse_only`
    （只后台化 `browse_web`）/ `all`（三工具都后台化，**不推荐**）。非法值
    fail-closed 回 `off` 并记一条 warning；
  - `tools.background_timeout`：整任务硬预算（默认 240 秒，钳 30-900；核心的
    工具调用超时对后台执行体已不生效，这条是唯一约束）；
  - `tools.background_max_chars`：终态消息正文截断（默认 1500 字符，钳 200-8000）。
  - **不需要手工迁移配置**：核心按 schema 自动补齐缺失组/叶，覆盖安装后配置里
    自动长出 `tools` 组（值 = 默认 `off`）。
- **新增 `core/background.py`（306 行，纯 Python 不 `import astrbot`，可独立单测）**：
  `BackgroundRegistry`（umo→job 单飞索引、`active_job` / `active_task_id` /
  `finish` / `running` / `cancel_all` / `clear`）、`BackgroundJob`（**提交时快照**
  预算与截断长度，运行中改配置不变）、四态四前缀 `HEADERS`、`render_accepted_text` /
  `render_rejected_text` / `render_terminal_text`、`truncate_result`、
  `_BG_MAX_CONCURRENT = 4` 与 `_BG_BUSY_TEXT`。
- **三个入口拆成「同步前置校验 + 后台/前台分派」**：`browse_web` 的会话白/黑名单、
  禁词校验与 `perception=` 前缀解析留在前台；`browse_local_page` 的四道路径校验
  抽成 `_resolve_local_page_target()` 并留在前台；`browser_web_search` 的
  `search.enabled` 闸门与检索词校验留在前台。⇒ 越权/禁词/路径非法**永远是同步
  拒绝**，不会退化成一条异步失败消息。
- **终态投递**：`context.send_message(umo, MessageChain([Plain(正文)] + 媒体))`；
  媒体走既有 `artifact_sink()`，**只在成功态附**；投递失败只记日志、不重试，
  且 `finish()` 在 `finally` 里（投递异常不会让该会话被永久单飞拒绝）。
- **插件重载/禁用**：`terminate()` 先取消在跑任务（等至多 5 秒）再关浏览器，
  发「【后台任务已取消】」；进程级重启没有终态消息（已知边界，见下）。

### 修复（review T8 落项：搜索终态、失败口径与投递加固）

- **搜索终态从「原始 JSON」改为人读摘要**：`browser_web_search` 的返回体先经
  `_background_user_text` → `_render_search_human` 渲染成「检索词 / 命中 N 条 /
  前 6 条 title+url+摘要片段 / 其余条数省略 / 备注 / 后端 provider·outcome·code /
  错误码 / 提示」，再交给投递层截断 ⇒ **截断只作用于渲染后的文本**，不会把
  「…（完整结果已省略 N 字）」拼进 JSON 中间。
- **返回体状态映射（可预期失败不再算"完成"）**：`ok` → 成功；`partial` → 成功且
  正文首行标注「（部分结果：有 provider 失败或结果被预算截断）」；`failed` → 失败
  （原因 = `code: summary（hint）`）；`timeout` / `total_timeout`（含
  `code=total_timeout`）→ 超时，并在「原因：」行点明；无法解析 / 缺 `status` /
  未知 `status` → 失败。七类可预期失败因此都不再以"完成"出现：空检索词
  （`empty_query`）、会话被拒（`session_denied`）、禁词（`banned`）、无可用后端
  （`no_provider`）、内部异常（`internal_error`）、wave 全挂（`status=failed`）
  落失败，`total_timeout` 落超时。
- **超时且没有任何半成品时明说「本次没有可用结果。」**（不特判工具名），"工具自己
  报的超时"额外给一行「原因：」（整任务预算超时不重复原因）。
- **`browse_local_page` 截图进附件**：后台模式下截图成功后 `collect_artifact()`，
  随成功终态消息一起发出；前台无 sink 时返回 `False` 且零副作用（v1.4.0 的
  「只把截图路径写进返回文本」一字不变）。
- **投递与生命周期加固**：`terminate()` 的 `finally` 调 `BackgroundRegistry.clear()`
  兜底；终态日志补 `umo=<md5 前 8 位>`（与受理行三元组对齐）；删掉未使用的
  `BackgroundRegistry.get()`；`runner` 标注为 `Callable[[dict], Awaitable[str]]`。
- **补测试**：`all` 模式下六类同步拒绝（会话黑名单 / 含 `perception=` 前缀的禁词 /
  路径不存在 / 扩展名错误 / 路径越权 / 搜索工具黑名单会话）、后台态直发消息为 0 +
  `artifact_sink` 只允许在任务体内（AST 守卫）、`send_message` 抛异常后登记表清空且
  可再次提交（R7）、D2 拒绝耗时 <1s、`clear()` 后登记表为空。

### 行为边界（写清楚，免得运营方当 bug 报）

- **前缀六个字面量、互不嵌套**：拒绝 `【拒绝】`、受理 `【已受理】`、终态
  `【后台任务完成】` / `【后台任务失败】` / `【后台任务超时】` /
  `【后台任务已取消】`。拒绝/受理**同步**返回，四态终态**异步投递**。
  **拒绝态不占 `task_id`**（不登记、不排队、不进后台）。
  四态分开的直接后果：生产判据 `grep -c "【后台任务完成】"` 只数成功，
  失败/超时/取消不会被算进"完成"；日志侧另有 `后台任务终态 status=` 与
  `后台任务受理` 两条结构化关键字可对账。
- **部分结果规则**：`browse_local_page` 与 `browser_web_search` 超时/失败时附
  「（以下是中断前已得部分结果，可能不完整）」+ 截断正文；
  **`browse_web` 超时 = 无结果** —— 子代理只在**终局**返回结果文本，中途没有可
  交付的半成品，超时消息里没有内容属预期；超时且没有任何半成品时，正文会明说
  「本次没有可用结果。」，不会是"漏发"。
- **搜索终态是人读摘要，不是 JSON**：后台模式下 `browser_web_search` 的终态正文
  由返回体渲染而来（见上「修复」一节），可读性按"给人看"设计；完整的结构化
  `results[]` / `fetched.text` 只在**前台**调用里返回给主模型。
- **并发上限两个**：同一会话最多 **1 个**后台任务（单飞，第二个同步拒绝、不排队）；
  全进程最多 **4 个**（`_BG_MAX_CONCURRENT`，**模块常量不是配置键**），第 5 个
  同步拒绝（`【拒绝】后台任务已达并发上限（4），本次未提交。`）。
- **后台结果不进主模型上下文**：终态消息是插件自己发的，不写 LLM 历史 ⇒ 用户
  追问时主模型不知道结果。这是路线 D 的固有代价；要"结果回到主模型"需要路线 C
  （核心补一行镜像 + 重启 AstrBot），**本版未实现**，也没有 `background_mode=core`
  之类的取值。
- **`off` 与 v1.4.0 逐字节一致**：三工具返回体、拒绝文案、`_refresh_config()`
  调用次数都由用例钉住（`test_background_tools.py::test_t1_*`、`test_f1_*`）。
- **回滚**：`tools.background_mode` 改回 `off` 即行为回退 —— 入口第一行重读配置，
  **下一次工具调用生效，无需重载、无需重启**。代码级回滚时配置与代码**成对**回退：
  v1.4.0 不认识 `tools` 组，核心会把这三个键当未知键删掉（`Config key removed`），
  属预期行为。
- **媒体只在成功态附**：超时/失败/取消只发文本 + 原因（半成品媒体会误导用户）；
  成功态的附件包括 `browse_local_page` 的截图（前台只给路径，后台随终态消息发出）
  与子代理产生的媒体。
- **顺带清掉 HEAD 遗留的 7 处 F841**，让 `ruff check` 门禁真的能过（否则它是
  "有存量告警但没人看"的假绿灯）。

### 测试

- 全量：`720 passed`（2026-10-25；v1.4.0 基线 650 条 ⇒ 本版累计**新增 70 条**，
  其中评审 T8 返工新增 26 条）。
- `tests/test_background_tools.py`（1568 行）覆盖交接判据 T1–T7 + 三处实现纠偏
  F1（模式热更新）/ F2（媒体仅成功态）/ F3（终态日志带 `status=`）+ D2（全局 4
  并发）+ 返工项 H1/H2（搜索终态人读化与 `status` 映射）、M1（`all` 模式六类同步
  拒绝）、M2（后台态零直发 + `artifact_sink` AST 守卫）、M3（R7 投递异常后仍可再次
  提交）、L1–L7，另有受理/拒绝/终态文案、六前缀互斥、截断、登记表纯单测。
- `ruff check --no-cache .`：`All checks passed!`。
- 仓库卫生：`git status -s` 空，无 `.pyc` / `.pytest_cache` / `.ruff_cache` 新增。

## [v1.4.0] - 2026-10-21

### 特性：配置分组化（5 组 / 41 键）+ 搜索 API 池 + LLM 搜索工具

- **配置结构改为 5 个分组**：`browser` / `limits` / `security` / `perception`
  （既有 25 键，键名与语义不变，仅从扁平搬进分组）+ 新增 `search` 组（16 键）。
  Dashboard 中按组折叠显示。
- **新增 `browser_web_search` LLM 工具**（`@filter.llm_tool`，与核心内置
  `web_search_<provider>` **并存、不接管**）：并行调用插件自有的搜索 API 池
  （brave / bocha / firecrawl / tavily / exa，可配多份、每份独立 key），
  按 URL 去重合并后返回结构化 JSON（`status/results/attempts/error_codes/notes`），
  **任何失败都不抛异常**。
  - 可开关：`search.enabled` + 自动闸门（池内无可用 provider 也不激活）；
    关闭时工具对模型**不可见**（`active=False`，请求装配期被剔除），
    另在 handler 入口做一次配置兜底。
  - 可并行：`search.max_parallel` 单波并发上限，`asyncio.gather` 真并发；
    单源超时/429/异常只影响自己。
  - 可配搜索步数：`search.max_rounds` = **波次上限**（一次调用最多尝试
    `min(可用池, max_parallel × max_rounds)` 个源，够 `max_results` 提前结束）。
  - 可配超时：单源 `search.provider_timeout`（模板 `timeout` 可覆盖）→
    调用级 `search.total_timeout`（含抓取阶段）→ 单条抓取 `search.fetch_timeout`。
  - 可选抓正文：`search.fetch_pages` / `fetch_top_n` / `fetch_max_chars`
    （纯 HTTP，逐跳 SSRF + 禁词过滤，失败不影响主结果）。
- **新增 provider 实现**：`core/search_providers.py`（5 个后端 + 可注入 HTTP
  传输层）、`core/http.py`（aiohttp 优先 / 标准库兜底）、`core/crawler.py`
  新增 `AiohttpFetcher`（逐跳 SSRF + 限长 + 文本化）。
- **`core/search.py` 扩展**：并行波次 `search_waves()`、去重合并确定性排序、
  TTL 缓存、429/401/403 冷却、结构化失败码；`redact_secrets()` 迁入本模块
  （唯一消费者）。
- **新增迁移器 `tools/migrate_config_v4.py`**（v3 → v4）：同时支持**扁平 v3** 与
  **分组 v3** 两种来源；干跑 / 落盘 / 校验 / 从备份回滚；幂等；保留 BOM 与 0600；
  任何模式都不打印配置值；修掉 v3 迁移器的四个坑（组顺序与 KEY_MAP 脱钩、
  `is_migrated` 对缺组不敏感、`--verify` 只比共有键、只支持 2 层）。
  **升级顺序：先迁配置、再换代码；回滚成对进行**（详见 README）。

### 修复（review t6 落项：预算与状态口径）

- **预算下沉到波次内**：单源等待改为 `min(provider_timeout, 剩余总预算)`，波级
  硬停为 `min(最慢源+1, 剩余预算 + 0.5s)`；`provider_timeout`（如 15s）大于调用
  预算（如 5s）时，墙钟由**预算**决定，慢源不会再吃满单源超时把调用拖到外层
  `budget+1` 切断。回归用例：`test_budget_caps_single_source_wait_not_provider_timeout`
  / `test_tool_wall_clock_is_capped_by_budget_not_provider_timeout`（复刻 reviewer
  的 `total=5 / provider=15 / 慢源` 组合）。
- **外层超时不再丢结果**：`search_waves(..., progress={...})` 每波把已得结果写进
  调用方的可变容器；`main.py` 外层 `asyncio.TimeoutError` 分支用最后一次快照
  渲染 `status=partial` + `notes=["total_timeout"]`（此前是 `failed/0 条`）。
  用例：`test_outer_timeout_renders_partial_with_kept_results`。
- **池耗尽 ≠ 预算耗尽**：只有预算真的切断工作才记 `total_timeout`；池内源全部
  试完记 `notes=["pool_exhausted"]`。deadline 检查移到组波之前。
- **跳过码不算失败源**：`missing_key` / `disabled` / `cooldown` /
  `unknown_template` / `bad_config` 在 `resolve_status()` / `summarize()` 里
  不计入 failed ⇒ 池内有未配置/停用的源时，有结果的调用 `status=ok`；全跳过才
  `failed/no_provider`。用例：`test_skipped_sources_do_not_make_partial`、
  `test_cooldown_skip_does_not_make_partial`、`test_all_skipped_pool_reports_no_provider`。
- **文档与规格对齐**：`main.py` 三处 docstring 改为 v1.4 口径（5 组 / 41 叶 /
  3 个对外 LLM 工具 + 24 个子代理工具）；README 把 `--verify --baseline`（旧值
  体检）与 `--verify --against`（键+值严格比对）分开写，runbook 增加"产物键集合
  == schema 41 叶"的内联断言；spec 增「7.3.1 预算下沉」与「附录 C 用例名对照」；
  `audit/20261021-disposal-t4.md` 增「AC ↔ 用例」映射表（AC-01…AC-41）。

### 清理：冗余与死代码（消灭"测试全绿但生产不执行"）

- **移植后删除库内重复副本** `astrbot_plugin_browser_llm_tmp/`（23 文件 / 360 KB，
  与主树 `core/` 逐字节相同）：其 3 处独有修正先移植进 `main.py` ——
  ① 数据目录走 `StarTools.get_data_dir()`（`_resolve_data_dir()`，兜底
  `data/plugin_data/<name>`，不再写插件安装目录）；② 工作区回退改走核心
  `get_astrbot_workspaces_path()`（`_resolve_workspaces_fallback()`，数据目录换层级后
  旧 `_DATA_DIR.parents[2]` 会指错）；③ 截图/媒体目录挂到 `_DATA_DIR` 下。
  "检测原插件仍在运行"的 WARN 块**不移植**（正式版改名后没有检测对象）。
- **删除 4 个未接入模块（850 行，占 core 30.9%）**：`core/tasks.py`（290）、
  `core/async_bridge.py`（84）、`core/vision.py`（100）、`core/pages.py`（92）——
  `main.py` 零引用，只有各自测试在测。能力面（异步投递/唤醒、后台任务状态机、
  浏览器→爬虫自动回退、独立识图缓存）列为 **v5 非目标**：识图缓存已内联、
  会话串行化由 `SessionManager` 负责、抓正文只走 `core/crawler.py`。
- **裁剪回胀点**：
  - `core/delivery.py` 470 → 45 行，只留 artifact 三件套（`artifact_sink` /
    `collect_artifact` / `current_artifacts`，`main.py` 的 `_emit_media` 唯一消费者）；
    投递状态机 / 忙闲判定 / 信封渲染删除（`redact_secrets()` 早已迁到 `core/search.py`）。
  - `core/search.py` 删除 v3 串行入口 `SearchService.search()` + `SearchOutcome`
    （v4 只走 `search_waves()`，串行路径 `main.py` 零调用）、`available_providers()`
    与 `ProviderEntry.has_credentials`（均无调用方）。
  - `core/crawler.py` 删除 `PageContent` / `PageBackend` / `open_page` / `html_links`
    （唯一服务已删的 `pages.py`）；`CrawlerLimits.max_redirects` 此前是**未被读取**的
    配置项，改为真正生效（去掉重复的 `MAX_REDIRECTS` 常量）。
- **清掉孤儿资产**：`data/cmd_config.json`（主配置拷贝，含 `dashboard.password` /
  `jwt_secret` 键，零消费方）、`data/t2i_templates/*.html`（`t2i` 全库零引用）、
  `__pycache__` / `.pytest_cache` / `*.pyc`。
- **无用 import**：生产代码（`main.py` / `core/` / `tools/`）本来就零 F401；
  测试代码清掉 6 处（`pytest` / `json` / `_resolve_local_page_roots` / `KEY_MAP` 等）。
- **保留项都有生产接线点**：`browser` / `config_map` / `extract` / `safety` /
  `session`（wired）、`crawler`（`AiohttpFetcher`）、`http` + `search_providers`
  （搜索链路）、`delivery`（artifact 三件套）。`main.py` / `core/browser.py` 的
  点号读取点由 `tests/test_core_config_readpoints.py` 静态钉住，`R3` 的"4 条读路径"
  是 S1 过渡期的刻意设计（保留，不再新增读法）。

### 兼容性 / 破坏性变更

- **配置文件形态变化（扁平 25 键 → 5 组 41 键）**：直接替换插件目录而不迁移配置
  会因核心 `check_config_integrity()` 删除未知键而**丢失全部设置**。升级必须先跑
  `tools/migrate_config_v4.py --apply`，再替换插件目录；回滚同样成对。
- `search.enabled` 默认 **false** ⇒ 新代码 + 旧配置（已迁移）时行为与 v1.3.x 一致，
  搜索工具默认不出现。
- 新增 `search` 组不改变 `browser.default_search_engine`（浏览器 SERP 模板）的语义，
  两条链路（浏览器搜索页 vs HTTP 搜索 API）互相独立、互不自动启用。

### 测试

- 全套 **640 passed / 0 failed**（`PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ -q`）。
  计数口径：692（t3 后基线）− 66（删除项）+ 14（新增）= 640。
  - 删除 66 条：`test_core_tasks.py` 13（模块删）、`test_core_async_bridge_vision.py` 15
    （两模块删）、`test_core_pages_crawler.py` 15 → 其中 7 条 crawler 文本用例迁入
    `test_core_crawler_text.py`（净 −8）、`test_core_search.py` 11（串行入口删）、
    `test_core_delivery.py` 19 → 其中 1 条 artifact 用例迁入 `test_t_m0_media.py`（净 −18）。
  - 新增 14 条：`tests/test_repo_hygiene.py` 13（删项不复活 / 裁剪不回胀 / 接线不回退）+
    迁入的 1 条 artifact 用例。
  - 逐条"原守护内容 + 接替者"见本次交付说明（每条删除项都给了接替用例或"能力删除"结论）。
- 新增：`tests/test_search_config_schema.py`（schema 叶子 ⇄ KEY_MAP ⇄ provider 模板）、
  `tests/test_search_providers.py`（5 后端请求/响应/错误分类/密钥边界）、
  `tests/test_search_service_parallel.py`（真并发/轮数/超时/去重排序/冷却/缓存/
  返回体裁剪）、`tests/test_search_tool_contract.py`（工具契约/开关兜底/抓取/
  不泄密钥/SSRF）、`tests/test_search_tool_toggle.py`（activate 对齐/降级/名称冲突/
  模型不可见性）、`tests/test_search_migration_v4.py`（两种来源/幂等/值零变化/回滚）。
- 更新 9 处既有守卫（config_map / readpoints / qa_complement / main_contract /
  vision_provider）：schema 分组后改为按组内下标读取，并补反向守卫（死组、v4 路径
  不得走扁平兜底）。

## [v1.3.3] - 2026-09-06

### 特性：内容审核总开关（enable_banned_filter）

- 新增配置 `enable_banned_filter`（bool，默认 true）：内容审核总开关。
  关闭后 `_check_banned` 统一放行，所有禁词拦截点（网页正文/标题/搜索
  关键词/URL/browse_web 任务描述/本地页面/截图与裁剪识图结果）不再因
  banned_words 拦截内容——论坛等敏感词密集站点可关闭，避免整页被拒；
  Dashboard 保存配置热生效（经 `_refresh_config` → `_load_config`）。
- **不影响安全边界**：SSRF 内网拦截（`acheck_url`/页面级兜底路由）、
  本地页路径白名单、会话黑白名单均不受此开关影响。
- 激活日志新增开关状态输出（开启/关闭）。

### 修复

- **P1 识图结果禁词旁路**：`browse_screenshot` / `browse_zoom_crop` 的
  识图结果此前直接返回、未过禁词——违禁正文可经截图识图透传给 LLM/
  用户，与 `_conf_schema.json` 承诺（禁词内容禁止返回）不一致。修复：
  识图描述返回前过 `_check_banned`（命中返回【拒绝】文案），对齐
  `browse_local_page` 既有做法；受 `enable_banned_filter` 总开关控制。
- **感知前缀空值边界**：`perception=`（等号后空值）此前既不匹配合法
  前缀也不匹配非法前缀正则 `\S+`，整段被当任务文本且无告警。修复：
  `_INVALID_PERCEPTION_PREFIX_RE` 改为 `^perception\s*=(?:\S+\s*|\s*)`
  分支剔除——非法值非空时连同值一并剔除（既有语义不变），空值时仅
  剔除前缀形态与空白、保留任务文本；均记录 warning 并回落默认链路。

### 优化

- **配置解析双实现收敛**：main.py 的 `_as_int/_as_float/_as_bool` 与
  core/browser.py 的 `_safe_float/_safe_bool` 为同构双实现（双命名漂移
  风险），统一收敛至 `core/__init__.py` 的 `as_int/as_float/as_bool`；
  main.py 三个方法改为委托，BrowserCore 改用共享实现。
- **Windows 兼容**：`BROWSER_LLM_EXTRA_LOCAL_ROOTS` 由硬编码冒号分隔
  改为 `os.pathsep`（Linux/macOS 行为不变，Windows 支持分号与盘符路径）。
- **类型收紧**：`max_chars` / `max_links` 配置解析由 `_as_float` 改为
  `_as_int`（与 schema 声明的 int 一致）。
- **日志风格**：terminate 超时日志由 f-string 内嵌 `%.1fs` 位置参数改为
  纯 f-string 格式化。

### 测试

- 新增 `tests/test_banned_switch.py`（12 例）：开关默认开启且命中返回
  禁词、关闭后 `_check_banned` 恒 None、`_page_summary` 关闭后透传 /
  开启后拒绝、browse_screenshot / browse_zoom_crop 识图含禁词时默认
  拒绝且关闭后放行、非法配置值回退默认、`_refresh_config` 热更新开关、
  关闭开关不影响 SSRF（`acheck_url` 仍拦截内网）；
- `tests/test_perception_rules.py` 新增 3 例：`perception=` 空值剔除 +
  告警 + 任务保留、空值后换行接任务、裸 perception（无 =）不受影响；
- 全量回归：`python -m pytest tests/ -q` → **393 passed**（378 基线
  + 15 新增）。

## [v1.3.2] - 2026-09-06

### 修复（工程缺陷清理）

- **P1 browse_checkbox 漏 await**：`loc.is_checked()` 在 Playwright async API 中
  返回协程，原实现未 await，`bool(coroutine)` 恒为 True——「目标状态与当前状态
  一致」时误点击导致切换语义反转（如目标未勾选且当前已未勾选时被误选上）。
  修复为 `await loc.is_checked()`；新增四种状态组合回归测试（fake locator 的
  is_checked 返回真实协程，与 Playwright async API 行为一致）。
- **子代理指令去重**：工具清单中 `browse_zoom_crop` 重复列出两次，删除一处
  （「操作原则」中的用法提及保留）。
- **一次性任务残留泛化**：子代理指令「提交后等待结果」段的站点专属关键词
  （『生成我的模型』『模型匹配』『八维』『Human Model ID』『指纹』）移除，
  保留通用等待策略（提交后不刷新/不后退，周期性重读页面，间隔 5-8 秒、最多
  60 秒）；`_conf_schema.json` 中 agent_max_steps / agent_tool_timeout 的
  「逐题作答15题」「完整做题」示例同步泛化为「多步表单、连续翻页」。
- **版本单一事实源**：`PLUGIN_VERSION` 改为模块加载时从 `metadata.yaml` 的
  `version` 推导（读取失败回退内置兜底值 `_PLUGIN_VERSION_FALLBACK`），发布
  时只改 metadata.yaml 一处，消除双处手工同步（原注释自认的维护负担）。
- **仓库卫生**：删除插件根目录 `browser.py`（`core/browser.py` 的旧版死副本，
  缺 `_safe_float` 等修复且无任何引用）；清理 `data/cmd_config.json`（宿主
  AstrBot 配置快照）与 `data/t2i_templates/`（宿主 t2i 模板）部署迁移残留及
  `__pycache__` 编译残留（.gitignore 均已覆盖，仅工作区清理）。

### 测试

- 新增 `tests/test_fixes_v132.py`（8 例）：browse_checkbox 四种状态组合
  （状态一致不点击 / 不一致才点击）、指令无一次性任务残留、通用等待策略
  保留、zoom_crop 工具清单去重、PLUGIN_VERSION 与 metadata.yaml 一致；
- 全量回归：`python -m pytest tests/ -q` → **378 passed**（原 370 + 8 新增）。

## [运维] - 2026-08-18 空闲回收参数调整（非源码变更）

- `data/config/astrbot_plugin_browser_llm_config.json` 的 `idle_timeout`：1200 -> 900
  （SRE 2026-08-17 内存优化建议 B1，加快 chromium 空闲会话回收）。改前备份
  （backup.sh）：astrbot_plugin_browser_llm_config_idle_timeout_20260818_064945.json
- 生效链路：插件加载时 `_load_config()`（main.py:723）-> `SessionManager(idle_timeout=...)`
  （main.py:849-852）；热同步 `_refresh_config()`（main.py:825）由 handler 触发。
  直接改配置文件需插件重载或 astrbot 重启后读入内存；运行期 Dashboard 重新保存
  配置亦可触发热同步。源码与 schema 默认值（1800）未动。

## [v1.3.1] - 2026-08-16

### 修复（QA 查缺补漏：1×P1 + 6×P2 + 低风险 P3）

- **P1 内容禁词覆盖网页正文链路**：`banned_words` 过滤此前仅作用于 URL / 搜索
  query / 本地页面，`browse_open` / `browse_current_page` / `browse_click_link` /
  `browse_new_tab` / `browse_scroll` 等经 `_page_summary()` 返回的页面正文与标题
  原样透传（实测含「赌博/色情」正文完整返回）。修复：`_page_summary()` 对标题与
  正文统一过 `_check_banned`，命中返回「【拒绝】页面标题/正文包含违禁内容：词」；
  `browse_get_text()` 返回前同样检查；`browse_web` 任务描述（剔除感知前缀后）入口
  检查（P3-9 纵深防御）。与 `_conf_schema.json` banned_words 描述「网页内容会被
  安全过滤拦截」一致。
- **P2-1 非法数值配置不再阻断加载**：`_load_config` 全部数值/布尔配置改经
  `_as_int` / `_as_float` / `_as_bool` 归一解析——非法字符串 / None / 类型错误
  回退 schema 默认值并记 warning，手动编辑 config.json 不再导致插件 `__init__`
  崩溃；`BrowserCore` 的 timeout / enable_screenshot / block_internal_ip 同步容错。
- **P2-2 字符串布尔归一**：`silent_mode` / `enable_screenshot` /
  `block_internal_ip` 对字符串 `"false"` / `"0"` / `"off"` / `"no"` / `""` 按
  False 解析（原实现 `bool("false")` 强转 True，语义反转）。
- **P2-3 browse_local_page 白名单纳入平台工作区**：`_resolve_local_page_roots()`
  恒加入运行时工作区（`get_astrbot_workspaces_path()`，目录存在时），并追加平台
  工作区候选 `/root/workspace`（存在即加入）与环境变量
  `BROWSER_LLM_EXTRA_LOCAL_ROOTS`（冒号分隔，可配置额外根目录）；去重保序，
  标准 AstrBot 部署行为不变。
- **P2-4 多行链接文本可点击**：`extract_links` 对链接可见文本做空白清洗
  （innerText 保留换行会把编号行拆散）；`browse_click_link` 改为按
  「换行 + 编号行开头」切分条目再解析 target，不再依赖 splitlines 行结构。
- **P2-5 cache_days=0 语义修正**：`cache_days <= 0` 表示「不清理」（与
  vision_cache_ttl=0 关闭缓存的语义一致），不再误删全部媒体/截图缓存；
  README 配置表同步注明。
- **P2-6 无会话 switch/close 不再隐式建会话**：`browse_switch_tab` /
  `browse_close_tab` 在取页前先检查会话白名单与 `tab_count()==0`，无会话直接
  返回「当前会话没有任何标签页」，不触发 `ensure_page` 创建默认页。

### 低风险清理（P3）

- **P3-1** `extract_text(max_chars<=0)` 按默认值 4000 处理，消除 1 字符 +
  截断标记的怪异输出；
- **P3-2** 日志 URL 脱敏：新增 `_redact_url()`（query 参数值 / fragment 打码），
  应用于 browse_open 调试日志、媒体嗅探拦截/失败日志、媒体下载全链路失败日志；
- **P3-3/P3-4** 文档同步：README「25 个工具」→ 24、main.py 注释「15 个」→ 24、
  「22 个配置项」→ 24，README 配置表补齐 default_url / default_search_engine /
  max_chars / max_links / timeout / max_pages / idle_timeout / session_whitelist /
  session_blacklist / enable_screenshot / proxy / viewport 共 12 项；
- **P3-5** 删除插件根目录旧测试副本（`test_browser_core.py` 与 tests/ 完全重复、
  `test_main_contract.py` 为 v1.2.0 旧版），仅保留 `tests/`；
- **P3-7** `browse_press_key` 按键大小写不敏感（`enter` → `Enter`，按大写归一
  后映射回 Playwright 规范按键名）；
- **P3-8** 非法 `browser_type` 启动前校验：给出「可选：chromium/firefox/webkit」
  提示（原为 `None.launch()` 无提示报错），`chrome` 自动映射为 chromium；
- **P3-9** `browse_web` 任务描述入口过禁词（见 P1 条目）。

### 遗留（有意不修，见交付说明）

- P3-6 默认禁词含「政治/暴力」等宽泛词可能误伤正常页面：属默认值设计取舍，
  保留现默认值，用户可按需收敛 `banned_words` 配置；
- P3-10 SSRF DNS rebinding TOCTOU 窗口、P3-11 白名单目录内硬链接风险：均为
  高级对抗/本地写权限前提下的残余风险，文档化提示，不做代码加固。

### 测试

- `tests/test_qa_complement.py` 原缺陷证明用例更新为修复后行为断言
  （非法 int 配置回退 / bool 字符串归一 / 换行链接可点击 / 无会话 switch/close
  报错且不建会话 / extract_text(0) 默认值 / cache_days=0 不清理），并新增
  switch/close 有会话对照用例；
- 新增 `tests/test_fixes_v131.py`（26 例）：P1 正文/标题禁词端到端
  （browse_open / get_text / current_page / new_tab / click_link 拒绝与透传）、
  P2-3 白名单（平台工作区 / env 扩展 / 去重 / 越权拒绝）、P2-4 链接文本清洗、
  P3-2 URL 脱敏、P3-7 按键归一、P3-8 内核校验、P3-9 任务描述禁词；
- 全量回归：`python3 -m pytest tests/ -q` → **370 passed**（原 342 + 28 新增）。

## [v1.3.0] - 2026-08-15

### 特性：感知模式精细化（工具参数级 + 会话规则级 + 识图缓存）

- **工具参数级感知模式**（P0）：`browse_web` 的 `input` 开头支持可选前缀
  `perception=text|text_image|image`（大小写不敏感，后跟空格或换行再接任务描述），
  解析后本次任务覆盖全局配置——动态构造子代理指令时优先用参数值；未带前缀则走
  会话规则/全局链路；前缀非法值（如 `perception=foo`）记录警告并剔除该前缀，
  回落默认链路。解析结果以 debug 日志记录（`感知模式=xxx（显式=xxx）`），可观测。
  典型收益：全文本模型子代理（前端/测试等）浏览网页时不再每次执行截图识图
  （mimo-v2.5，2-8s/次 + token 费）。
- **会话规则级默认**（P0）：新增配置 `perception_rules`（list[dict]：`match`
  与会话 UMO 做子串匹配、不区分大小写；`perception` 为 text/text_image/image；
  可选 `note`），默认空列表。`browse_web` 未显式指定感知模式时，按当前会话 UMO
  遍历规则取第一条命中；未命中回落全局 `page_perception`。优先级：
  显式参数 > perception_rules 命中 > page_perception 全局。`_refresh_config()`
  同步读取（热更新，Dashboard 保存即生效）。非法规则条目（缺字段/非法值）跳过
  并记录警告。
- **识图短时缓存**（P1）：`_describe_screenshot` 增加按 URL 的短时缓存——同一 URL
  （规范化：去 fragment/尾斜杠差异）在 `vision_cache_ttl` 秒内（默认 60，0=关闭）
  重复识图直接返回缓存文本（返回带 `[缓存]` 前缀，另记 debug 日志），省识图耗时
  与 token。缓存键 = (规范化 URL, 会话 UMO)，按会话隔离；选择简单 dict 实现并
  注明并发理由：asyncio 单线程事件循环内 dict 读写原子，同 URL 并发首识可能重复
  调用 LLM 但结果一致、无害，不引入锁。缓存写入时超过 256 条即清理过期项防膨胀；
  `terminate` 资源清理路径整体清空（防重载残留）。`browse_screenshot` /
  `browse_zoom_crop` 以当前页 URL 为键，`browse_local_page` 以文件 URI 为键。
- **schema 与文档**：`_conf_schema.json` 同步新增 `perception_rules`（含 items
  结构：match/perception/note 与 options）与 `vision_cache_ttl`（含 hint）；
  README.md 配置节与新增「感知模式控制」节同步说明三处控制点与优先级。
- **版本**：metadata.yaml 与 main.py PLUGIN_VERSION 同步升至 v1.3.0。

### 测试

- 新增 `tests/test_perception_rules.py`（20 例）：前缀解析（大小写/分隔符/非法值
  告警/无前缀/仅前缀）、规则匹配（UMO 子串、大小写、首条命中、非法规则跳过）、
  优先级（显式 > 规则 > 全局）、browse_web 端到端（system_prompt 断言纯文字模式
  不含截图识图指引、规则默认与显式覆盖、非法前缀回落+告警）；
- 新增 `tests/test_vision_cache.py`（15 例）：TTL 内命中（带 `[缓存]` 前缀且不再
  调 LLM）、TTL 过期重识图、ttl=0 关闭、URL 规范化（fragment/尾斜杠）、会话隔离、
  无 URL/无会话不缓存、拒识不写缓存、超阈值清理过期项、terminate 清空；
- 全量测试保持全绿。

### 补强（同版 v1.3.0）：browse_local_page 感知模式参数

- `browse_local_page` 新增可选参数 `perception`（string，可省略，默认 "" = 跟随
  全局 `page_perception`）：`text` 仅页面文本提取（跳过截图与识图调用、不落盘截图，
  复用既有降级文本提取路径，全文本子代理读文档/报错场景零识图开销）；`text_image`
  默认行为（文本 + 截图识图）；`image` 以截图识图为主、页面文本为辅（辅助文本过
  禁词，命中则不附带）。非法值记录 warning 并回落全局 `page_perception`（与
  browse_web 非法前缀行为一致）；本次解析模式以 debug 日志记录（可观测）。
- 设计说明：子代理无独立 UMO（继承调用方会话），`perception_rules` 按 UMO 子串
  无法区分子代理，故本地页面工具不套用会话规则，感知控制靠工具显式参数（persona
  指令驱动）；识图缓存（vision_cache）在 text_image/image 模式正常生效，text
  模式不触发识图自然不读写缓存。
- 测试：`tests/test_local_page.py` 新增 9 例（text 跳过截图/识图、text_image
  默认行为、image 识图为主文本为辅、image 视觉不可用降级、非法值回落全局+告警、
  显式覆盖全局、缺省跟随全局、感知模式 debug 日志、docstring 契约含
  perception(string)）；README 感知模式节补充 browse_local_page 用法。

### 修复（同版 v1.3.0）：识图拒识判定器误判真实视觉描述（P2）

- **误判修复**：`_is_vision_rejection` 由「全文任意位置 search 命中」收紧为
  「响应开头位置约束（前缀窗口 60 字符内且非引号引用）或整句独立（所在子句
  去除拒识短语后仅剩标点/软化词）」——真实视觉描述正文中的叙述性同构短语
  （如"浏览器不支持图像懒加载功能"）不再误判为拒识，不再破坏识图缓存链；
  既有中英文拒识用例召回不变（24/24），收紧 `图片?(?:无法|不能|不支持)`
  需动作词（处理/识别/查看/理解）必填，避免"图片无法加载"类叙述误命中；
- 测试：`tests/test_vision_provider.py` 新增 12 例（8 个叙述/引用反例含
  tester 复现样例、前缀窗口长响应、英文礼貌长前缀、整句独立兜底、引号包裹
  纯拒识短句）；全量测试 306 全绿。

## [v1.2.1] - 2026-08-14

### 修复

- **release 打包**：zip 排除 `dist/*`，修复发布包内含空 dist/ 目录瑕疵；
- **浏览器资源清理加固**：BrowserCore shutdown/close_page 增加单步超时保护（5s）与 warning 日志提级（带实例标识）；terminate 增加总超时兜底（20s）并重构清理顺序（先停 sweeper → browser.shutdown → sessions.shutdown），修复 WebUI 重载场景下旧实例 chromium 残留进程问题；
- 测试：新增 shutdown 清理路径与 terminate 契约用例 13 个（249 passed）。

## [v1.2.0] - 2026-08-14

### 优化：P2 五项（2026-08-14 修复报告建议清单）

- **空闲回收竞态**（P2）：`sweep_idle` 回收前检查会话锁
  （`self._locks[umo].locked()`），会话正在执行工具调用时跳过本轮不回收，
  锁释放后下一轮再回收，避免关闭正在使用的页面；
- **全局锁粒度**（P2）：`ensure_page` / `new_tab` 的 `_global_lock` 不再
  持有到 goto 完成——容量检查与页面挂载在锁内完成，页面创建与 goto（慢
  操作）移出锁外执行，避免单个会话的慢导航串行阻塞全部会话的标签操作；
  锁外创建期间产生的并发重复页/超额页在挂载阶段去重复用或关闭，不泄漏
  页面、不突破 max_pages 上限；
- **会话黑白名单匹配过宽**（P2）：`_is_session_allowed` 由子串匹配改为
  精确匹配——按分隔符（`:` / `|`，覆盖 umo 标准格式与 `_umo_of` 兜底格式）
  切分 umo 后与条目逐字段精确比对，黑名单 `"123"` 不再误伤群 `"1234"`；
  兼容既有配置写法：完整 UMO 条目与 umo 整体精确相等仍命中，群号精确
  比对兜底；
- **配置热更新覆盖不全**（P2）：新增 `_refresh_config()`，在浏览工具入口
  （`browse_web` / `browse_local_page` / 24 个子代理工具 handler 外层）轻量
  重读共享 config dict（Dashboard 保存即 update 的同一对象），并同步
  SessionManager（max_pages / idle_timeout / default_url 由固化改为热更新）、
  SafetyFilter（禁词 / block_internal_ip，新增 `update_config` 方法）、
  BrowserCore（block_internal_ip，作用于新建 context）与子代理指令
  （page_perception 变化下次 browse_web 生效）；黑名单、内网拦截、截图
  开关等配置修改无需重启即生效；
- **锁表清理**（P2）：`_local_page_locks` 改为弱引用字典
  （`weakref.WeakValueDictionary`），browse_local_page 结束、锁无任何强
  引用（无持锁/无等待协程）时条目自动移除，防止字典无限增长；弱引用
  语义天然避免「手动 pop 与并发取锁」的清理竞态，`terminate` 增加显式
  清空兜底。

### 兼容性说明

- 会话黑白名单语义由「子串包含」收紧为「字段精确匹配」：旧配置若依赖
  子串命中（如仅填群号前缀）需改为填写完整字段（群号/用户 ID/平台名或
  完整 UMO）；完整 UMO 条目不受影响。
- `max_pages` / `idle_timeout` / `default_url` 从「重启生效」改为热更新
  （工具入口同步）；既有 context 的 SSRF 兜底路由按创建时的
  `block_internal_ip` 安装，关闭开关不摘除已装拦截（安全方向）。

## [v1.1.1] - 2026-08-14

### 修复：识图模型下拉动态读取 AstrBot 已配置 Provider（Bug #1）

- **动态下拉**：`vision_provider_id` 的 options 不再硬编码，插件启动时与每次识图/
  `browse_web` 调用时从 `Context.provider_manager.provider_insts` 提取已加载聊天
  Provider 并就地更新内存 schema（`plugin_md.config.schema` 与插件 `self.config.schema`
  为同一对象，Dashboard 每次请求实时读取 → 无需重启立即生效）；
- **启动期兜底**：因 AstrBot 启动顺序为「插件加载先于 Provider 初始化」，新增后台
  轮询任务（每 5s，最长 2 分钟），Provider 就绪后自动补齐下拉选项；
- **默认值修正**：`default` 若已不在候选中则自动改为空串，Dashboard「恢复默认」
  不再写回不存在的 Provider；`_conf_schema.json` 不再硬编码
  `opencode-go/mimo-v2.5`（Bug #2），hint 文案同步更新；
- **运行期配置实时生效**：识图 provider 与提示词改为优先读取共享 config dict
  （Dashboard 保存即更新），不再依赖 `__init__` 快照属性；
- **失效自动降级**：配置的识图 Provider 未加载/被删除时，`_describe_screenshot`
  记录警告日志并自动回退到当前会话聊天 Provider（`get_current_chat_provider_id`）；
  留空仍表示显式关闭识图，不触发回退。

### 修复：安全与资源泄漏（Review 整改）

- **媒体下载重定向 SSRF**（P1）：`_download_media` 关闭 aiohttp 自动跟随重定向，
  改为逐跳手动跟随（最多 5 跳）且每跳重新过 `acheck_url` 安全校验，防止 302
  重定向绕过内网拦截直达内网；
- **页面级 SSRF 兜底**（P1）：BrowserCore 在 context 级安装路由拦截，页面内
  302/JS 重定向与子资源请求解析到内网/保留地址即 abort（host 判定带 300s 缓存）；
  与 `block_internal_ip` 配置联动，关闭内网拦截时不安装；
- **页面泄漏修复**（P1）：`ensure_page` / `new_tab` 中 `new_page` 成功但 `goto`
  失败时不再泄漏页面与 context（失败导航后必须关闭）；`BrowserCore.new_page`
  创建失败时清理半成品 context/page；
- **错误信息**：`browse_web` 获取当前对话 Provider 失败时返回明确错误文案
  （原先异常被外层兜底吞成泛化报错，`if not provider_id` 分支为死代码）；
- **日志治理**：移除全部 `[DIAG]` 遗留调试日志（降级为 debug 级），激活日志版本号
  修正为 v1.1.0。

### 修复：发布收尾（版本同步与数据脱敏，P1/P2）

- **版本号同步**（P1）：`metadata.yaml` version 更新为 `v1.1.1`；激活日志不再
  硬编码版本串，改为读取模块级 `PLUGIN_VERSION` 常量（真实运行时 `Star` 实例
  无 `self.metadata` 属性，无法动态读取，故集中为单一常量，代码内一处同步）；
- **repo 地址补齐**（P3）：`metadata.yaml` 的 `repo` 填入
  `https://github.com/cmdst/astrbot_plugin_browser_llm`；
- **数据脱敏**（P2）：`_LOCAL_PAGE_WORKSPACE_FALLBACK` 移除硬编码真实服务器路径
  （`/root/AstrBot/data/workspaces`），改为由插件安装位置平台无关推导
  （插件位于 `<astrbot_root>/data/plugins/<plugin>/`，其上级两级即
  `<astrbot_root>/data`，同级 `workspaces`）；`_resolve_local_page_roots` 与
  `browse_local_page` docstring 同步去掉真实路径。「任务约定工作区优先 +
  运行时解析兜底」的既有行为不变（标准部署下推导值即实际工作区，行为完全一致）。

### 修复：识图拒识文本误当视觉描述（P2，tester E2E 发现）

- 配置无视觉能力的模型（如 deepseek-v4-flash）时，其返回的固定拒识文案
  （如 `[Unsupported Image]`）原先被 `_describe_screenshot` 原样当作「视觉描述」
  返回，用户无法感知识图已失败；
- 新增 `_is_vision_rejection` 拒识检测：正则匹配常见拒识特征（中英文、大小写
  不敏感：`[Unsupported Image]` / `cannot view` / `unable to process image` /
  `text-only model` / `图片无法` / `不支持多模态` 等），命中即记警告日志
  （含 provider 与文本片段）并返回明确提示文案「识图模型不支持图片，请更换
  多模态模型或清空识图配置」，不静默成功也不抛异常；`contexts` 与 `image_urls`
  双通道均生效。

### 其他

- `max_pages` 的 description/hint 修正为「所有会话标签页总数上限」（与实现一致，
  原文案误写为每会话上限）；
- 新增测试：`tests/test_vision_provider.py`（22 用例，动态 options/实时配置/失效
  回退；发布收尾新增 32 个拒识检测用例：中英文拒识特征、大小写不敏感、正常
  视觉描述与空文本不误判、`_describe_screenshot` 拒识返回提示文案）、
  `tests/test_browser_core.py`（12 用例，SSRF 守卫/资源清理）、session
  泄漏回归 3 例、safety hostname 判定 3 例、媒体重定向 SSRF 2 例；
- 验证：全量 202 个测试通过（含真实 chromium 渲染验证，0 跳过）。

## [v1.1.0] - 2026-08-12

### 新增：`browse_local_page` 本地页面渲染查看工具

- **功能**：以无头浏览器真实渲染本地 HTML 页面（含 CSS/JS 执行），截图后调用视觉模型
  （`mimo-v2.5`）生成中文视觉描述（页面结构 / 主要文字 / 样式渲染异常），供子代理在
  无视觉模型时「查看」渲染后的本地页面。
- **降级机制**：视觉模型不可用时自动降级为页面文本提取，保证工具不空转。
- **会话隔离**：不经过浏览会话管理器（SessionManager），每次渲染使用独立页面、用完即关，
  不影响既有 `browse_*` 浏览会话状态；同一会话的本地页面渲染按 per-umo 串行化。
- **安全边界**：
  - 路径白名单：仅允许 AstrBot 工作区（`data/workspaces/`）与插件 `data/` 目录下的
    `.html/.htm` 文件，其余路径一律拒绝；
  - 防路径穿越：路径解析后校验（resolve + 白名单前缀检查），越权路径直接拒绝；
  - 禁词过滤：沿用既有安全过滤器（黑/白名单默认配置为空即放行）。
- **测试**：新增 `tests/test_local_page.py`（19 个用例，覆盖路径白名单、防穿越、
  禁词过滤、降级路径、参数校验等）；`tests/test_main_contract.py` 工具例外清单同步更新。
- **验证**：全量 129 个测试通过（含真实 chromium 渲染验证）；本次改动为纯增量
  （diff 验证零修改、零删除原逻辑）。

## [v1.0.0] - 初始版本

- 插件首个独立仓库版本，功能基线：Agent 通过 function-calling 自主浏览网页
  （导航、搜索、点击链接、提取正文与截图），返回结构化浏览结果。
- 模块：`core/browser.py`（Playwright 浏览器驱动）、`core/session.py`（会话管理）、
  `core/extract.py`（正文提取）、`core/safety.py`（安全过滤：SSRF/路径/禁词）。
- 测试基线：`tests/` 6 个测试文件（conftest、extract、safety、session、ssrf_click、
  main_contract）。
