# CHANGELOG

本仓库为 `astrbot_plugin_browser_llm`（LLM浏览器插件）独立 git 仓库，由 AstrBot 主仓库
`data/` 目录（主仓库 .gitignore 忽略）迁移初始化而来。仅跟踪插件源码与测试，运行时
数据（`data/`：截图、运行时配置、临时文件）一律不入库。

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
