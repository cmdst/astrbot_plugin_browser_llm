# CHANGELOG

本仓库为 `astrbot_plugin_browser_llm`（LLM浏览器插件）独立 git 仓库，由 AstrBot 主仓库
`data/` 目录（主仓库 .gitignore 忽略）迁移初始化而来。仅跟踪插件源码与测试，运行时
数据（`data/`：截图、运行时配置、临时文件）一律不入库。

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
