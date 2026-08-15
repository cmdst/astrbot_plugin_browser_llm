# CHANGELOG

本仓库为 `astrbot_plugin_browser_llm`（LLM浏览器插件）独立 git 仓库，由 AstrBot 主仓库
`data/` 目录（主仓库 .gitignore 忽略）迁移初始化而来。仅跟踪插件源码与测试，运行时
数据（`data/`：截图、运行时配置、临时文件）一律不入库。

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
