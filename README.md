# astrbot_plugin_browser_llm

Agent 驱动的网页浏览插件 —— 让 LLM 通过 function-calling 自主浏览网页：导航、搜索、点击链接、填表、滚动、截图识图、下载媒体，无需用户手动指令。

## ✨ 功能特性

- **工具化子代理架构**：主 LLM 只看到一个 `browse_web` 入口工具，25 个 `browse_*` 工具在子代理上下文中运行，显著减少主 LLM 的 token 占用
- **完整网页交互**：打开网页、提取正文/链接、按文本/坐标点击、填表、按键、滚动、滑块、下拉框、复选框、标签页管理
- **识图辅助**：接入多模态模型（如 opencode-go/mimo-v2.5）对截图做视觉理解，支持区域裁剪放大识图（`browse_zoom_crop`）
- **媒体嗅探**：从页面嗅探图片/视频资源，下载并发送到群聊（`browse_sniff_media`）
- **本地页面自检**：`browse_local_page` 渲染查看本地 HTML 供子代理/开发者自检页面（无头渲染 + 截图 + 视觉描述，视觉不可用时降级文本提取；路径白名单仅限工作区与插件 data 目录）
- **静默模式**：浏览截图仅内部识图，不往群聊刷图（`silent_mode`）
- **安全防护**：SSRF 内网拦截、内容禁词过滤、图形验证码自动停止、下载大小上限

## 🏗️ 架构

```
主 LLM（main agent）
  ├── browse_web（网页浏览入口 llm_tool）
  │     └── 子代理 tool_loop_agent（25 个 browse_* 工具）
  │           ├── Playwright 浏览器（chromium）
  │           ├── SessionManager（会话/标签页隔离）
  │           ├── SafetyFilter（SSRF/禁词）
  │           └── 多模态识图（vision_provider_id）
  └── browse_local_page（本地 HTML 渲染查看入口 llm_tool，供子代理/开发者自检）
        ├── 无头渲染（独立页面，用完即关）
        ├── 路径白名单（工作区 + 插件 data 目录）
        └── 视觉描述 / 降级文本提取
```

## 📦 安装

将插件目录放入 `AstrBot/data/plugins/`，重启 AstrBot 或在 WebUI 重载插件。

依赖：Playwright + chromium（AstrBot 环境已提供，无需额外安装）。

## ⚙️ 配置项（WebUI 插件配置）

| 配置 | 默认 | 说明 |
|---|---|---|
| `browser_type` | chromium | 浏览器内核 |
| `silent_mode` | true | 静默模式：截图仅内部识图不发群 |
| `page_perception` | text_image | 页面感知方式：text / text_image / image |
| `vision_provider_id` | - | 多模态识图模型 Provider ID。下拉选项自动同步 AstrBot 已配置的聊天模型 Provider（启动时与每次识图调用时刷新）；留空则截图仅发用户不做识图 |
| `vision_prompt` | - | 自定义识图提示词 |
| `cache_days` | 3 | 媒体缓存保留天数，超期自动清理 |
| `agent_max_steps` | 70 | 子代理单次委托最大工具步数 |
| `agent_tool_timeout` | 1200 | 子代理单次工具超时（秒） |
| `banned_words` | 11 词 | 内容禁词过滤 |
| `block_internal_ip` | true | SSRF 防护 |

## 🚀 使用示例

群聊中直接说：

- 「打开 https://example.com 看看是什么」
- 「在这个页面上帮我填表提交」
- 「把这个页面上的图片都下载发我」
- 「打开这个网址，把页面上所有的商品价格读出来」

## 🤝 致谢

- 灵感与部分交互模式参考 [astrbot_plugin_browser](https://github.com/Zhalslar/astrbot_plugin_browser)（Playwright 浏览器操作思路）
- llm_tool / 会话过滤模式参考 AstrBot 生态插件与 [AstrBot 官方文档](https://astrbot.app/)
- 多模态识图链路基于 AstrBot 的多模态 Provider 能力

## 📄 许可证

[MIT](./LICENSE)
