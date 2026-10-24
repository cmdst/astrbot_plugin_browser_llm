# astrbot_plugin_browser_llm

Agent 驱动的网页浏览插件 —— 让 LLM 通过 function-calling 自主浏览网页：导航、搜索、点击链接、填表、滚动、截图识图、下载媒体，无需用户手动指令。

当前版本：**v1.5.0**（变更见 [CHANGELOG](./CHANGELOG.md)；v1.5.0 新增「后台异步执行」，默认关闭）

## ✨ 功能特性

- **工具化子代理架构**：主 LLM 只看到一个 `browse_web` 入口工具，24 个 `browse_*` 工具在子代理上下文中运行，显著减少主 LLM 的 token 占用
- **完整网页交互**：打开网页、提取正文/链接、按文本/坐标点击、填表、按键、滚动、滑块、下拉框、复选框、标签页管理
- **识图辅助**：接入多模态模型（如 opencode-go/mimo-v2.5）对截图做视觉理解，支持区域裁剪放大识图（`browse_zoom_crop`）
- **媒体嗅探**：从页面嗅探图片/视频资源，下载并发送到群聊（`browse_sniff_media`）
- **本地页面自检**：`browse_local_page` 渲染查看本地 HTML 供子代理/开发者自检页面（无头渲染 + 截图 + 视觉描述，视觉不可用时降级文本提取；路径白名单为工作区（AstrBot 工作区 + 平台工作区，如 `/root/workspace`）+ 插件 data 目录，可用环境变量 `BROWSER_LLM_EXTRA_LOCAL_ROOTS`（按系统路径分隔符分隔，Linux/macOS 为冒号、Windows 为分号）追加额外根目录；支持 `perception` 参数按任务控制感知方式——全文本子代理读文档/报错页可传 `perception="text"` 跳过截图识图）
- **静默模式**：浏览截图仅内部识图，不往群聊刷图（`silent_mode`）
- **感知模式精细化**：页面感知方式可精确控制——`browse_web` 参数级前缀 `perception=text|text_image|image` 按任务覆盖、`perception_rules` 按会话（UMO 子串匹配）设定默认、全局 `page_perception` 兜底；全文本子代理（前端/测试等）不再为每次浏览付出截图识图开销
- **识图缓存**：同一 URL（去 fragment/尾斜杠规范化）在 `vision_cache_ttl` 内重复识图直接复用结果（带 `[缓存]` 前缀），省识图耗时与 token
- **配置热更新**：会话黑白名单、内容禁词、内网拦截开关、截图开关、识图 Provider、感知规则等修改后无需重启，下次工具调用即生效（Dashboard 保存即同步）
- **后台异步执行（v1.5.0，默认关闭）**：`tools.background_mode` 打开后，`browse_web` / `browse_local_page` / `browser_web_search` 三个 LLM 工具**立刻**返回「已受理 + task_id」，真正的任务在后台跑，结果**单独作为一条消息**发到本会话（四态前缀、部分结果规则、并发上限、回滚见下文「后台异步执行」一节）；默认 `off` 与 v1.4.0 行为一致
- **资源清理加固**：浏览器关闭/插件重载带超时保护与总超时兜底，避免 WebUI 重载后旧实例 chromium 残留进程
- **安全防护**：SSRF 内网拦截、内容禁词过滤（`enable_banned_filter` 总开关，敏感词密集站点可关闭）、图形验证码自动停止、下载大小上限

## 🏗️ 架构

```
主 LLM（main agent）
  ├── browse_web（网页浏览入口 llm_tool）
  │     └── 子代理 tool_loop_agent（24 个 browse_* 工具）
  │           ├── Playwright 浏览器（chromium）
  │           ├── SessionManager（会话/标签页隔离）
  │           ├── SafetyFilter（SSRF/禁词）
  │           └── 多模态识图（vision_provider_id）
  └── browse_local_page（本地 HTML 渲染查看入口 llm_tool，供子代理/开发者自检）
        ├── 无头渲染（独立页面，用完即关）
        ├── 路径白名单（工作区 + 插件 data 目录）
        └── 视觉描述 / 降级文本提取

后台执行（v1.5.0，仅当 tools.background_mode ≠ off）
  ├── BackgroundRegistry（同会话单飞 + 全局最多 4 个并发，core/background.py）
  └── 终态投递 context.send_message（四态前缀；媒体只在成功态附）
```

## 📦 安装

将插件目录放入 `AstrBot/data/plugins/`，重启 AstrBot 或在 WebUI 重载插件。

依赖：Playwright + chromium（AstrBot 环境已提供，无需额外安装）。

## ⚙️ 配置项（WebUI 插件配置）

v1.4.0 起配置按**分组**组织（Dashboard 中折叠显示），v1.5.0 起为 **6 个分组 / 44 个键**：
`browser` / `limits` / `security` / `perception` / `search` / `tools`。前四组是既有
25 个键（只是从扁平结构搬进分组，**键名与语义不变**），`search` 是 v1.4.0 新增的
搜索 API 组（16 键），`tools` 是 v1.5.0 新增的后台任务组（3 键，默认关闭）。

### browser / limits / security / perception（既有 25 键）

| 分组.键 | 默认 | 说明 |
|---|---|---|
| `browser.type` | chromium | 浏览器内核（chromium/firefox/webkit，`chrome` 自动映射为 chromium；非法值启动前给出可选值提示） |
| `browser.default_url` | https://www.baidu.com | 新会话首个标签的初始页 |
| `browser.default_search_engine` | 必应搜索 | 浏览器 SERP 模板（供子代理 `browse_search` 用，与搜索 API 无关） |
| `browser.timeout` | 30 | 页面加载超时（秒） |
| `browser.proxy` | - | 浏览器代理（如 http://127.0.0.1:7890），留空直连 |
| `browser.viewport` | 1280×800 | 浏览器视口尺寸 |
| `limits.max_chars` | 4000 | 正文摘要最大字符数（<=0 用默认值） |
| `limits.max_links` | 20 | 链接列表最多返回条数（<=0 用默认值） |
| `limits.max_pages` | 5 | 全部会话的标签页总数上限 |
| `limits.idle_timeout` | 1800 | 会话空闲回收阈值（秒） |
| `limits.cache_days` | 3 | 媒体缓存保留天数，超期自动清理；0 或负值 = 不清理 |
| `limits.agent_max_steps` | 70 | 子代理单次委托最大工具步数 |
| `limits.agent_tool_timeout` | 1200 | 子代理单次工具超时（秒） |
| `security.banned_words` | 11 词 | 内容禁词过滤：URL / 搜索关键词 / 本地页面 / 网页正文与标题 / browse_web 任务描述 / 截图识图结果 / 搜索结果标题与摘要命中均拒绝或丢弃（受 `enable_banned_filter` 控制） |
| `security.enable_banned_filter` | true | 内容审核总开关：关闭后不再因禁词拦截内容，适合论坛等敏感词密集站点；不影响内网拦截/路径白名单等安全边界 |
| `security.block_internal_ip` | true | SSRF 防护（拦截内网/环回/云元数据地址），搜索端点与结果链接同样受它约束 |
| `security.session_whitelist` | [] | 允许使用浏览器的会话白名单（空=全部允许） |
| `security.session_blacklist` | [] | 禁止使用浏览器的会话黑名单（搜索工具同样受限） |
| `perception.enable_screenshot` | true | 是否允许截图 |
| `perception.silent_mode` | true | 静默模式：截图仅内部识图不发群 |
| `perception.page_perception` | text_image | 全局页面感知方式：text / text_image / image |
| `perception.perception_rules` | [] | 会话级感知规则：按 UMO 子串匹配设定会话默认感知方式。每条含 `match`、`perception`、可选 `note`；命中第一条生效 |
| `perception.vision_provider_id` | - | 多模态识图模型 Provider ID（下拉自动同步已配置模型）；留空则截图仅发用户不做识图 |
| `perception.vision_prompt` | - | 自定义识图提示词 |
| `perception.vision_cache_ttl` | 60 | 识图短时缓存秒数（同 URL + 同会话复用）；0 关闭 |

> 类型健壮性（v1.3.1）：数值/布尔配置遇非法值（字符串、None、类型错误）自动回退
> schema 默认值并记录警告，手动编辑 config.json 不会导致插件加载失败；字符串
> `"false"`/`"0"`/`"off"` 按布尔 false 归一，不再被强转 True。

### search（v1.4.0 新增：搜索 API 池 + `browser_web_search` 工具）

| 键 | 默认 | 说明 |
|---|---|---|
| `search.enabled` | false | 搜索工具总开关。开启且池内至少一个 provider 填了 `api_key` 时，主 LLM 才能看到 `browser_web_search`；否则工具对模型**不可见**（自动闸门，日志记 `search tool inactive`） |
| `search.providers` | [] | provider 池（模板 brave / bocha / firecrawl / tavily / exa，可加多份、每份独立 key）。数组顺序即默认优先级 |
| `search.max_parallel` | 3 | 单波并发上限（1-10） |
| `search.max_results` | 8 | 合并去重后返回条数上限（1-50），同时作为单源默认请求量 |
| `search.max_rounds` | 2 | **搜索步数 = 波次上限**（1-5）。一轮 = 从「尚未尝试且可用」的 provider 里按序取至多 `max_parallel` 个并行检索；够 `max_results` 提前结束。一次调用最多尝试 `min(可用池, max_parallel × max_rounds)` 个源 |
| `search.provider_timeout` | 15 | 单源超时（秒，3-60）；provider 模板 `timeout` 可覆盖 |
| `search.total_timeout` | 60 | 单次调用总预算（秒，5-300，含抓正文）；到点保留已有结果并记 `total_timeout` |
| `search.cooldown_seconds` | 60 | 401/403/429 后该源冷却（秒，0=不冷却），冷却期内跳过不发请求 |
| `search.cache_ttl` | 120 | 相同检索词+参数的缓存 TTL（秒，0=关） |
| `search.fetch_pages` | false | 是否抓取结果正文（纯 HTTP，不启浏览器/不执行 JS） |
| `search.fetch_top_n` | 3 | 抓前 N 条（0-5，0=不抓） |
| `search.fetch_timeout` | 12 | 单条抓取超时（秒，3-60） |
| `search.fetch_max_chars` | 2000 | 抓取正文长度上限（200-20000） |
| `search.region` | 空 | ISO-3166-1 alpha-2 国家码（仅 brave 消费，其余源记 `region_unsupported:<p>`） |
| `search.safesearch` | moderate | off / moderate / strict（仅 brave 消费） |
| `search.proxy` | 空 | 搜索请求与正文抓取用的代理，与 `browser.proxy` **相互独立** |

provider 模板字段（每份 7 个）：`enabled`（默认 true）、`name`（显示名，空则
`<模板>#<序号>`）、`api_key`（**敏感**，留空则该源被跳过并记 `missing_key`）、
`base_url`（留空用官方端点；必须 http(s)，命中内网记 `bad_config` 且不发请求）、
`weight`（1-100，合并排序用）、`timeout`（0=用全局）、`max_results`（0=用全局）。

**LLM 工具 `browser_web_search`**（与核心内置 `web_search_<provider>` **并存、不接管**）：
- 参数：`query`（必填）、`max_results`、`rounds`、`timeout`、`providers`、
  `fetch_pages`、`fetch_top_n`；`0`/空 表示沿用配置值。
- 并行：`asyncio.gather` 同时发起最多 `max_parallel` 个 provider，**单源超时/429/
  异常不影响其它源**；结果按 URL 去重（去 `utm_*` 等跟踪参数、host 小写、去
  尾斜杠）后按「权重 → 原始 rank → 轮次 → 去重键」确定性排序。
- 预算：单源等待 = `min(provider_timeout, 剩余总预算)` —— 慢源**不会**吃满
  `provider_timeout` 把整次调用拖到外层超时；预算到点时**已有结果保留**并返回
  `status=partial` + `notes=["total_timeout"]`（不会整批丢弃）。「池内源全部试完」
  与「预算耗尽」是两件事：前者记 `notes=["pool_exhausted"]`，后者才记
  `total_timeout`。
- 状态口径：**跳过源不算失败**。`missing_key`（没填 key）/ `disabled` / `cooldown`
  / `unknown_template` / `bad_config` 都是"没发请求就被跳过"，只要有一个源成功且
  没有真实错误，`status` 就是 `ok`（不会因为池里留着一个没配 key 的模板而恒为
  `partial`）；全是跳过源才 `failed` + `code=no_provider`。
- 返回：**一个 JSON 字符串**（不加代码围栏），含 `status`（ok/partial/failed/
  disabled）、`results[]`（rank/title/url/snippet/sources/fetched）、`attempts[]`
  （每源 outcome/code/耗时/条数）、`error_codes`、`notes`；全失败也返回 JSON
  （`status=failed` + `hint`），**从不抛异常**。整体体积上限 24 KB，超出先砍
  `fetched.text` 再砍结果尾部并记 `payload_truncated`。
  以上是**前台**（`tools.background_mode=off` / `browse_only`）的返回体：后台模式下
  工具入口返回的是「【已受理】+ task_id」文本，终态消息则把这份 JSON 翻译成**人读
  摘要**（见下文「后台异步执行 → 搜索工具的终态」）。
- 安全：密钥只从本插件配置读取（不读核心 `provider_settings.websearch_*_key`、
  不读其它插件）；日志/返回体/异常全部脱敏；结果链接做字面 SSRF 校验，抓正文
  前再走 `acheck_url`（含 DNS）并逐跳校验重定向；检索词命中禁词整单拒绝，
  结果标题/摘要命中只丢该条。

### tools（v1.5.0 新增：后台任务）

| 键 | 默认 | 说明 |
|---|---|---|
| `tools.background_mode` | off | 三取值：`off`（三个工具都前台同步，与 v1.4.0 行为一致）/ `browse_only`（只把 `browse_web` 后台化）/ `all`（三个工具都后台化，**不推荐**）。写错的值按 `off` 处理并记一条 warning（fail-closed，不会让拼错的值变成后台） |
| `tools.background_timeout` | 240 | 后台任务的**整任务**硬预算（秒，钳 30-900）。用满则中断并发一条「【后台任务超时】」终态消息（没有半成品时会明说「本次没有可用结果。」）；已在跑的任务用提交时快照，改配置不会把它中途掐断 |
| `tools.background_max_chars` | 1500 | 终态消息正文的截断长度（字符，钳 200-8000），超出部分标注「…（完整结果已省略 N 字）」；媒体附件与消息头部不计入 |

> **v1.5.0 不需要跑迁移器**：核心加载插件配置时会按 schema 自动补齐缺失的组/叶
> （`Config key missing; added default.`），覆盖安装后配置里自动长出 `tools` 组
> （值 = 默认 `off`），运营方不需要手动迁移、也不需要改配置文件。
> `tools/migrate_config_v4.py` 只是保证"产物键集合齐全"的自洽工具，它的口径已跟上
> **6 组 / 44 叶**。

## 🕒 后台异步执行（v1.5.0，默认关闭）

三个 LLM 工具默认都是**前台同步**：主模型要等工具返回才能继续回复，`browse_web`
一次 68 秒量级会把该会话的回复卡住。v1.5.0 增加 **路线 D**（插件内闭环、零核心
改动、不重启服务）的后台执行方式，由 `tools.background_mode` 控制，**默认 `off`**。

### 开关语义

| `tools.background_mode` | `browse_web` | `browse_local_page` | `browser_web_search` |
|---|---|---|---|
| `off`（默认） | 前台同步（与 v1.4.0 **逐字节一致**） | 前台 | 前台 |
| `browse_only` | **后台** | 前台 | 前台 |
| `all` | **后台** | **后台** | **后台（不推荐）** |

`browser_web_search` 常态 1-3 秒，后台化等于多一条消息、且结果不进主模型上下文，
**通常没必要**；`all` 只为"任何工具都不阻塞会话"这种一致性要求存在。

### 前缀表（与 `core/background.py` 逐字一致：`REJECTED_HEADER` / `ACCEPTED_HEADER` / `HEADERS`）

| 前缀 | 类别 | 谁返回 | 同步/异步 | 说明 |
|---|---|---|---|---|
| **`【拒绝】`** | 拒绝 | 工具入口（`_submit_background` 的前置校验，或工具自己的参数/路径校验） | **同步**立即返回 | 后台受理这条路的四类原因：同会话单飞占用 / 全局并发上限 / 会话黑白名单 / 内容禁词。另有工具自己的参数与路径校验（如 `browse_local_page` 的路径白名单、扩展名限制）也仍在**前台**完成，其文案沿用仓库既有 `【错误】` / `【拒绝】` 口径。**拒绝态不占 `task_id`**：不登记、不进后台、不排队，主模型看到的就是最终答复 |
| **`【已受理】`** | 受理 | `_submit_background` | **同步**立即返回 | 文案含 `task_id` / 工具名 / 整任务硬预算，并明说「你现在还没有任何结果」+ 禁止编造 + 禁止重复提交 |
| **`【后台任务完成】`** | 终态·成功 | 后台任务 → `context.send_message` | 异步投递 | **只有这一种算"完成"**；媒体附件只在这种情况下随消息发出 |
| **`【后台任务失败】`** | 终态·失败 | 同上 | 异步投递 | 含失败原因摘要（≤300 字）；工具以 `【错误】` / `【拒绝】` 前缀作为文本返回的可预期失败（如"拿不到当前对话模型"）也归到这里 |
| **`【后台任务超时】`** | 终态·超时 | 同上 | 异步投递 | 用满 `tools.background_timeout` 被中断；有部分结果时附「已得部分结果」，并给调参建议 |
| **`【后台任务已取消】`** | 终态·取消 | 同上（best-effort，2 秒投递上限） | 异步投递 | 插件重载/禁用时取消在跑任务；进程级重启不发（见「代价与边界」） |

读表四条：① 六个前缀**互不嵌套**——失败/超时/取消消息里不会出现"完成"，所以
`grep -c "【后台任务完成】"` 只数成功；② 拒绝与受理**同步**、终态**异步**；
③ 拒绝态**没有** `task_id`，日志里不该按 task_id 去找被拒的请求（单飞拒绝文案里
出现的那个 `task_id` 是"正在跑"的任务，不是本次被拒的请求）；④ 插件日志关键字
（`后台任务受理` / `后台任务终态 status=`）与用户可见前缀是**两套**口径，
可以互相对账。

### 部分结果规则（超时/失败时到底有没有内容）

| 工具 | 超时/失败时有无部分结果 | 说明 |
|---|---|---|
| `browse_web` | **无** | 子代理只在**终局**返回结果文本，中途没有可交付的半成品 |
| `browse_local_page` | 有 | 文本提取在截图/识图之前完成，已提取到的正文会随终态消息发出 |
| `browser_web_search` | 有 | 每波结束的增量快照会被保留，按「title — url」最多渲染 5 条 |

统一形状：先一行 `（以下是中断前已得部分结果，可能不完整）`，再跟截断后的正文；
**超时且没有任何半成品**时，正文会明说「本次没有可用结果。」（不特判工具名，所以
`browse_web` 的超时消息必然是这句）。

> **`browse_web` 超时 = 无结果**（子代理终局才返回）；只有 `browse_local_page` 与
> `browser_web_search` 超时才附「已得部分结果」+ 截断。运营方看到 `browse_web`
> 的超时消息里没有内容属**预期**，不要当 bug 报。

### 搜索工具的终态正文（人读摘要，不是 JSON）

后台模式下 `browser_web_search` 的终态消息**不是**原始 JSON，而是把返回体渲染成的
人读摘要（截断只作用于渲染后的文本，不会把「…（完整结果已省略 N 字）」拼进 JSON
中间）。头部之后依次是：`检索词：<query>` / `命中 N 条结果` / 前 **6** 条（每条
title + url + 摘要片段 ≤120 字）/ 其余条数的省略提示 / `备注：<notes>` /
`后端：<provider / outcome / code>`（最多 6 个）/ `错误码：<error_codes>` /
`提示：<hint>`。

返回体的 `status` 决定进哪种终态 —— **可预期失败不会被算成"完成"**：

| 返回体 `status` / `code` | 终态前缀 | 正文要点 |
|---|---|---|
| `ok` | 【后台任务完成】 | 人读摘要 |
| `partial` | 【后台任务完成】 | 首行标注「（部分结果：有 provider 失败或结果被预算截断）」+ 摘要 |
| `failed` | 【后台任务失败】 | 失败原因 = `code: summary（hint）`，并附「本次没有可用结果。」 |
| `timeout` / `total_timeout`（含 `code=total_timeout`） | 【后台任务超时】 | 「原因：…」点明是搜索自己的总预算被用满；有半成品则附「已得部分结果」 |
| 无法解析 / 缺 `status` / 未知 `status` | 【后台任务失败】 | 「返回体无法解析」/「未预期的返回体 status=…」 |

七类可预期失败按上表分流：空检索词（`empty_query`）、会话被拒（`session_denied`）、
检索词或结果命中禁词（`banned`）、池内无可用后端（`no_provider`）、内部异常
（`internal_error`）、wave 全挂（返回体 `status=failed`）都落 `failed` 分支；搜索自己
的总预算被用满（`total_timeout`）落超时分支 —— **它们都不会计入
`grep -c "【后台任务完成】"`**（旧行为会把它们算成"完成"，这正是返工要修的）。

> 摘要只列前 6 条、每条摘要片段 ≤120 字，`attempts` 最多列 6 个后端调用。需要完整
> 的结构化 `results[]` / `fetched.text` 时，请让搜索走**前台**（`off` 或
> `browse_only` 下搜索本来就是前台，主模型拿到的是完整 JSON）。

### 并发上限：全局 4 个（模块常量，不是配置键）

- **同会话单飞**：同一会话（UMO）同时最多 **1 个**后台任务；第二个提交立即同步拒绝
  （`【拒绝】本会话已有一个后台任务在跑…`），**不排队**。
- **全局上限**：全进程同时最多 **4 个**后台任务 —— `core/background.py` 的
  `_BG_MAX_CONCURRENT = 4`，**模块常量、不是配置键**（面板上改不到它），第 5 个
  同步拒绝（`【拒绝】后台任务已达并发上限（4），本次未提交。请稍后再试；不要重试。`）。
- 为什么不把它做成配置键：每个后台任务 = 一套子代理链 + 一个浏览器 context + 最多
  `limits.max_pages` 个标签，4 个并发已可能撞容量上限；这条闸是防雪崩的硬约束，
  开放成旋钮只会用错。

### 回滚：把 `tools.background_mode` 改回 `off` 即回退（**无需重启、无需重载**）

`off` 是默认值，也是**行为级回滚**：三个工具恢复前台同步，返回体与 v1.4.0 逐字节
一致。每个工具入口第一行都会重读配置，所以面板改回 `off` 后**下一次工具调用**即
生效，**不需要重载插件、不需要重启 AstrBot**；唯一残留是当时在跑的那个任务会照常
跑完并按四态发一条终态消息。

代码级回滚（把插件目录退回 v1.4.0）时，**配置与代码成对回退**：v1.4.0 不认识
`tools` 组，核心会把这三个键当未知键删掉（`Config key removed`）—— 这是**预期
行为**，不是故障。

### 代价与边界（路线 D 的固有取舍，别当 bug 报）

- **后台结果不进主模型上下文**：终态消息由插件自己 `context.send_message` 发出，
  不写 LLM 历史 ⇒ 用户之后追问"刚才看到什么"，主模型**不知道**答案。这是路线 D 的
  固有代价，本版不做补救。要让"结果回到主模型、上下文连续"需要**路线 C**（核心补
  一行镜像 + 重启 AstrBot）——**本版没有实现，也不提供 `background_mode=core` 之类
  的取值**；本版能保证的只是"结果单独一条消息送达用户"。
- **进程级重启没有终态消息**：Dashboard 热重载、插件禁用都会先取消在跑任务并发一条
  「【后台任务已取消】」；但 `astrbot.service` 重启（进程被直接杀掉）没有 terminate
  机会，在跑任务**直接消失、不发任何消息**。属已知边界（跨进程恢复要落盘任务状态，
  不在本版范围）。
- **后台模式下拿不到对话模型 = 异步失败**：`browse_web` 的 provider 解析在执行体里，
  后台模式下失败会变成一条「【后台任务失败】…无法确定当前对话模型…」的终态消息，
  而不是同步错误。
- **后台搜索消息是摘要，不是完整 JSON**：终态正文最多列前 6 条结果（摘要片段 ≤120
  字）、最多 6 个后端调用；需要完整的结构化 `results[]` / `fetched.text` 时请让搜索走
  **前台**（`off` / `browse_only` 下搜索保持前台）——这也是一条"不建议把搜索工具
  后台化"的理由。
- **媒体只在成功态附**：超时/失败/取消只发文本 + 原因，半成品截图/视频不发（否则
  用户会以为任务完成了）。**成功态会带上任务期间产生的附件**：`browse_local_page`
  前台只把截图路径写进返回文本（`截图: <path>`，不直接发图），后台成功后该 png 会作为
  附件随「【后台任务完成】」消息一起发出；`browse_web` 的附件取决于子代理有没有调
  `browse_screenshot`（受 `perception.silent_mode` 管：静默开启时它不产出媒体，自然
  也不会成为附件）/ `browse_sniff_media`。

### 生产判据（部署后照抄；日志口径与用户口径分开数）

```bash
LOG=/root/AstrBot/data/logs/astrbot.log
grep -c "后台任务受理" "$LOG"                     # 分母：受理成功次数（被拒绝的不算）
grep -c "后台任务终态 status=" "$LOG"             # 四态合计；与上行相等 = 每个受理都收尾了
grep -c "后台任务终态 status=ok" "$LOG"           # 成功（失败/超时/取消不算）
grep -c "【后台任务完成】" "$LOG"                 # 用户真的看到的"成功"消息
grep -c "【后台任务失败】\|【后台任务超时】\|【后台任务已取消】" "$LOG"
```

"受理"与"终态"条数不相等 = 还有任务在跑（正常）或投递前进程被杀（异常）；
用户侧 `【后台任务…】` 条数与终态日志条数不相等 = 有终态消息没投递出去
（`send_message` 返回 False，按 `task_id` 查日志）。返工后搜索的**可预期失败**
（空检索词 / 会话被拒 / 禁词 / 无可用后端 / 内部异常）都计 `status=failed`，
不会再出现在 `【后台任务完成】`/`status=ok` 里 —— 拿这两个数算成功率才是真的。

## 🔁 配置迁移与成对回滚（v3 → v4）

> v1.5.0 只新增一个 `tools` 组（默认 `off`，3 键），**不需要手工迁移配置**：核心会
> 按 schema 自动补齐缺失的组/叶（见上「tools」一节）。本节讲的是**一次性**
> v3 → v4 的分组化迁移，对"从扁平 v3 直接升到当前版"仍然适用于。

### 为什么必须先迁配置

核心 `AstrBotConfig.check_config_integrity()`（`astrbot/core/config/astrbot_config.py`）
会把**schema 里没有的键判为 stale 并从配置里删掉、随后落盘**。本插件 v3 的配置是
**扁平 25 键**，当前 schema 是 **6 个分组 / 44 叶**（v4 起分组，v1.5.0 加 `tools`
组）；直接把新代码装上去，旧配置的每个顶层键都会命中"未知键"⇒ 设置被静默清空
（日志 `Config key removed: <key>`）。

**不变量（升级与回滚都要满足）**：配置文件里的键集合 ⊆ 代码 `_conf_schema.json` 的
键集合。违反它的两个方向都不行：

| 场景 | 代码 | 磁盘配置 | 后果 |
|---|---|---|---|
| 升级未做 | 当前版（6 组 44 叶） | v3（扁平 25 键） | 核心删掉全部扁平旧键 → 设置清零 |
| 回滚只回代码 | v3（扁平 25 键） | 当前版（6 组 44 叶） | 核心删掉 6 个组 → 设置清零 |

### 升级步骤（先迁配置，再换代码）

```bash
CFG=<data/config>/astrbot_plugin_browser_llm_config.json   # 正式版；_tmp 副本是另一份文件

# 0) 只读预检（不写盘）：报告来源形态（扁平 v3 / 分组 v3）、将写多少键
python3 tools/migrate_config_v4.py --dry-run --config "$CFG"

# 1) 落盘迁移（务必在替换插件目录之前；自动留 .bak-pre-v4-<ts> 备份，保留 BOM 与 0600）
python3 tools/migrate_config_v4.py --apply   --config "$CFG"

# 2) 旧值体检（跨形态）：只查 25 条旧路径的值 + 缺失项，并打印旧值指纹
python3 tools/migrate_config_v4.py --verify --config "$CFG" --baseline "<.bak-pre-v4-*>"

# 2b) 严格比对（同形态）：键集合 + 值都必须一致，用于"产物 vs 已知良好 v4 配置"
python3 tools/migrate_config_v4.py --verify --config "$CFG" --against "<known-good-v4.json>"

# 2c) 键集合必须 == schema 的 44 叶（迁移后自查，防丢组/多组；无需第二份配置）
#     在**插件目录**下执行（脚本要读同目录的 _conf_schema.json）
python3 - "$CFG" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
schema = json.loads((Path("_conf_schema.json")).read_text(encoding="utf-8"))
leaves = {(g, leaf) for g, spec in schema.items() for leaf in spec.get("items", {})}
got = {(g, leaf) for g, group in cfg.items() if isinstance(group, dict) for leaf in group}
assert got == leaves, f"missing={sorted(leaves - got)} extra={sorted(got - leaves)}"
print(f"[OK] keys == schema ({len(leaves)} leaves)")
PY

# 3) 替换插件目录 → WebUI 重载插件（或重启 AstrBot）
```

**两种校验模式别混用**（`--verify` 必须配一个基线）：

| 模式 | 比什么 | 适用场景 |
|---|---|---|
| `--verify --baseline <旧配置>` | **只**比 25 条旧路径的**值** + 缺失清单 + 旧值指纹；键集合差异只作 INFO 打印 | 跨形态体检（扁平 v3 → 分组 v4），证明"迁移没动旧值" |
| `--verify --against <参照配置>` | **键集合 + 逐路径值**都要一致，任一差异即 FAIL（退出码 3） | 同形态严格比对（迁后产物 vs 已知良好 v4 配置、或两份产物互比） |
| 内联 `python3 - <<'PY'`（见 2c） | 产物的键集合 == `_conf_schema.json` 的 44 叶 | 迁移后自查，防"丢整组 / 多出死组" |

迁移器同时支持**扁平 v3** 与**分组 v3** 两种来源（生产同时存在这两份配置），
并修掉了 v3 迁移器的四个坑：组顺序与 `KEY_MAP` 脱钩会静默丢整组、
`is_migrated()` 对缺组不敏感、`--verify` 只比共有键（v4 起必须显式选
`--baseline` 或 `--against`）、只支持 2 层嵌套。

### 成对回滚（配置与代码必须一起回）

```bash
# 回滚 = 先退配置、再退代码；顺序反了会先被核心删键
python3 tools/migrate_config_v4.py --restore-from "<.bak-pre-v4-*>" --config "$CFG"
# 然后把插件目录退回 v1.3.x 版本 → 重载插件
# v3 迁移器的 --from-backup 必须带同一个 --to（否则会回滚到另一份配置）
```

**只回其一 = 静默丢设置**：只回代码，分组化的 `search` / `tools` 组会被旧代码当未知键
删掉；只回配置，新代码会把扁平 25 键删掉。两条都已用生产配置的只读副本演练过
（迁移 → `--verify --baseline` 旧值零丢失 + `--verify --against` 键值严格一致 → 再回扁平 25 键，产物喂核心
`check_config_integrity` 得到 `has_new=False / unchanged=True`）。

### 部署顺序与上线检查（`_tmp` 副本退役）

1. 先在 WebUI **禁用** `astrbot_plugin_browser_llm_tmp`（它读的是
   `astrbot_plugin_browser_llm_tmp_config.json`，与正式版不是同一份文件），
   再安装/升级正式版 `astrbot_plugin_browser_llm`；
2. 检查核心 `inactivated_llm_tools` 里**没有** `browser_web_search` 残留
   （面板停用过一次会持久化，插件侧只能读到 `active=False`）；
3. `search.enabled=true` 且至少一个 provider 填了 `api_key` 时，主 LLM 的工具
   列表里应出现 `browser_web_search`；关掉后应立刻消失（`active=False`）。

> 库内那份 `astrbot_plugin_browser_llm_tmp/` 副本已在 v1.4.0 删除：它的 3 个必须
> 保留的修正（StarTools 数据目录、workspaces 回退、截图/媒体目录）已移植进
> `main.py`，"检测原插件"的 WARN 块按 D9 不移植（正式版改名后没有检测对象）。

## 📁 仓库结构（v1.4.0 清理后，v1.5.0 增补）

```
main.py                      插件入口：browse_web / browse_local_page / browser_web_search
core/__init__.py             配置解析容错（as_int / as_float / as_bool）
core/background.py           后台任务登记与四态文案（BackgroundRegistry / HEADERS / render_*）
core/browser.py              Playwright 驱动（BrowserCore）
core/config_map.py           配置键单一事实源（KEY_MAP / GROUPS / ConfigView / schema_leaf）
core/crawler.py              搜索正文抓取（AiohttpFetcher：逐跳 SSRF + 限长 + 文本化）
core/delivery.py             后台媒体登记（artifact_sink / collect_artifact / current_artifacts）
core/extract.py              正文/链接提取（ContentExtractor）
core/http.py                 HTTP 传输层（aiohttp 优先 / 标准库兜底，均不跟随重定向）
core/safety.py               SSRF + 内容禁词（SafetyFilter / check_url_literal / acheck_url）
core/search.py               搜索池：并行波次、去重合并、TTL 缓存、冷却、脱敏
core/search_providers.py     brave / bocha / firecrawl / tavily / exa 五个后端
core/session.py              浏览会话与标签页（SessionManager）
tools/migrate_config_v3.py   扁平 25 键 → 分组 v3（4 组）历史迁移器（保留给分组 v3 场景与既有测试）
tools/migrate_config_v4.py   v3 → 当前版迁移器（6 组 / 44 叶：v4 加 search 组、v1.5.0 加 tools 组）
```

每个 `core/` 模块都有**生产调用方**（`main.py` 或 `core/browser.py`），
写入 v1.4.0 清理前的 `tasks / async_bridge / vision / pages` 四个模块
（850 行，`main.py` 零引用、只有测试在测）已删除 —— 它们要做的能力
（异步投递/唤醒、后台任务状态机、浏览器→爬虫自动回退、独立识图缓存）
都是 **v5 非目标**：识图缓存已内联在 `main.py`，会话串行化由 `SessionManager`
负责，抓正文只走 `core/crawler.py`。将来若重新引入，请按届时 spec 重写，
不要从 v1.3.x 的历史文件里恢复。

## 🎯 感知模式控制（v1.3.0）

感知模式决定浏览器子代理「用文字还是截图」理解页面，三处控制点，优先级从高到低：

1. **工具参数级（按任务覆盖）**：`browse_web` 的 `input` 开头加前缀
   `perception=text|text_image|image`（大小写不敏感，后跟空格或换行再接任务描述），
   仅本次任务生效。例：`perception=text 打开 https://example.com 读取正文`
   （纯文字模式，绝对不触发截图识图，适合读文档/查报错/看代码）；
   `perception=image 看看这个页面长什么样`（截图为主）。
   非法值（如 `perception=foo`、`perception=` 等号后空值）会被忽略并记录告警，回落到规则/全局配置。
2. **会话规则级（按 UMO 默认）**：配置 `perception_rules`，按会话 UMO 子串匹配
   （不区分大小写）设定默认模式，命中第一条生效。例：
   `[{"match": "tester", "perception": "text"}]` 后，UMO 含 `tester` 的会话
   （如测试子代理）默认纯文字浏览。
3. **全局默认**：`page_perception`（text / text_image / image）。

**子代理本地页面工具（v1.3.0 补强）**：`browse_local_page` 同样支持感知模式控制。
子代理无独立 UMO（继承调用方会话），`perception_rules` 按 UMO 子串无法区分子代理，
故本地页面工具仅支持**工具参数级**控制：可选参数 `perception=text|text_image|image`
（大小写不敏感，可省略；缺省或非法值时跟随全局 `page_perception`，再回落
text_image）。全文本子代理（前端/测试/工程等）读取本地 HTML 文档、报错页时传
`perception="text"`，仅做页面文本提取——跳过截图与识图调用，零识图耗时/token
开销、不落盘截图文件；`text_image` 为默认（文本 + 截图识图）；`image` 以截图识图
为主、页面文本为辅（识图缓存正常生效）。例：`browse_local_page(path=..., perception="text")`。

## 🚀 使用示例

群聊中直接说：

- 「打开 https://example.com 看看是什么」
- 「在这个页面上帮我填表提交」
- 「把这个页面上的图片都下载发我」
- 「打开这个网址，把页面上所有的商品价格读出来」

## 🧪 开发与测试

```bash
cd astrbot_plugin_browser_llm
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ -q -p no:cacheprovider   # 720 passed（2026-10-25）
```

测试覆盖（32 个文件 / 720 条）：

- **工具契约**：`browse_web` / `browse_local_page` / `browser_web_search` 的 docstring
  与注册一致性、参数类型、入口装饰器白名单（`test_main_contract.py`、
  `test_search_tool_contract.py`、`test_search_tool_toggle.py`）。
- **搜索链路**：provider 实现与错误码（`test_search_providers.py`）、并行波次/去重合并/
  缓存/冷却/预算（`test_search_service_parallel.py`）、HTTP 传输层与抓正文逐跳 SSRF
  （`test_search_http_transport.py`）、配置 schema 与服务端一致性
  （`test_search_config_schema.py`）、迁移器 v4 四种来源/幂等/回滚
  （`test_search_migration_v4.py`）。
- **配置**：schema ⇄ `KEY_MAP` ⇄ `main.py` 读取点三方一致（`test_core_config_map.py`、
  `test_core_config_readpoints.py`、`test_qa_complement.py`）。
- **后台执行链路**（v1.5.0 新增 `test_background_tools.py`，1568 行）：T1–T7 判据
  （`off` 逐字节一致 / 立即返回 / 四态终态各一条消息 / 同会话单飞 <1s 拒绝 /
  `terminate()` 不悬挂 / schema⇄KEY_MAP⇄读取点三方一致）+ F1 模式热更新、F2 媒体
  仅成功态、F3 终态日志带 `status=`、D2 全局 4 并发；`all` 模式下的六类**同步拒绝**
  （会话黑名单 / 含 `perception=` 前缀的禁词 / 路径不存在 / 扩展名错误 / 路径越权 /
  搜索工具黑名单会话）、搜索终态的人读化与 `status` 映射（H1/H2）、`artifact_sink`
  只允许出现在任务体内的 AST 守卫、投递异常后仍能再次提交（R7）；另有受理/拒绝/终态
  文案、六前缀互斥、截断、登记表纯单测。
- **安全**：SSRF（含 DNS 与重定向）、禁词三处链路、点击/新标签/媒体嗅探行为级拦截。
- **浏览**：感知模式前缀/规则/优先级、识图缓存与 provider 降级链、本地页面工具
  （白名单/路径穿越/降级文本）、会话与标签页、浏览器资源清理。
- **仓库卫生**（v1.4.0 新增 `test_repo_hygiene.py`）：已删模块/副本不得复活、
  `core/delivery.py` 只含 artifact 三件套、`SearchService` 无串行入口、
  数据目录走 `_resolve_data_dir()`、`main.py` 无"重复插件检测" WARN 块。

发布流程：commit → push main → 打 tag（如 `v1.5.0`）→ release.yml 自动
打包发布（zip 已排除 `dist/`、`.github/`、`data/`、`__pycache__`）。

## 🤝 致谢

- 灵感与部分交互模式参考 [astrbot_plugin_browser](https://github.com/Zhalslar/astrbot_plugin_browser)（Playwright 浏览器操作思路）
- llm_tool / 会话过滤模式参考 AstrBot 生态插件与 [AstrBot 官方文档](https://astrbot.app/)
- 多模态识图链路基于 AstrBot 的多模态 Provider 能力

## 📄 许可证

[MIT](./LICENSE)
