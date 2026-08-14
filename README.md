# astrbot_plugin_browser_llm

Agent 驱动的网页浏览插件 —— 让 LLM 通过 function-calling 自主浏览网页：导航、搜索、点击链接、填表、滚动、截图识图、下载媒体，无需用户手动指令。

## ✨ 功能特性

- **工具化子代理架构**：主 LLM 只看到一个 `browse_web` 入口工具，25 个 `browse_*` 工具在子代理上下文中运行，显著减少主 LLM 的 token 占用
- **完整网页交互**：打开网页、提取正文/链接、按文本/坐标点击、填表、按键、滚动、滑块、下拉框、复选框、标签页管理
- **识图辅助**：接入多模态模型（如 opencode-go/mimo-v2.5）对截图做视觉理解，支持区域裁剪放大识图（`browse_zoom_crop`）
- **媒体嗅探**：从页面嗅探图片/视频资源，下载并发送到群聊（`browse_sniff_media`）
- **本地页面自检**：`browse_local_page` 渲染查看本地 HTML 供子代理/开发者自检页面（无头渲染 + 截图 + 视觉描述，视觉不可用时降级文本提取；路径白名单仅限工作区与插件 data 目录；支持 `perception` 参数按任务控制感知方式——全文本子代理读文档/报错页可传 `perception="text"` 跳过截图识图）
- **静默模式**：浏览截图仅内部识图，不往群聊刷图（`silent_mode`）
- **感知模式精细化**：页面感知方式可精确控制——`browse_web` 参数级前缀 `perception=text|text_image|image` 按任务覆盖、`perception_rules` 按会话（UMO 子串匹配）设定默认、全局 `page_perception` 兜底；全文本子代理（前端/测试等）不再为每次浏览付出截图识图开销
- **识图缓存**：同一 URL（去 fragment/尾斜杠规范化）在 `vision_cache_ttl` 内重复识图直接复用结果（带 `[缓存]` 前缀），省识图耗时与 token
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
| `page_perception` | text_image | 全局页面感知方式：text / text_image / image |
| `perception_rules` | [] | 会话级感知规则：按 UMO 子串匹配（不区分大小写）设定会话默认感知方式。每条规则含 `match`、`perception`（text/text_image/image）、可选 `note`；命中第一条生效。优先级：browse_web 前缀 > 规则 > 全局。例：`[{"match":"tester","perception":"text","note":"测试子代理只需正文"}]` |
| `vision_cache_ttl` | 60 | 识图短时缓存秒数：同 URL（去 fragment/尾斜杠）+ 同会话在 TTL 内重复识图复用结果（返回带 `[缓存]` 前缀）；0 关闭 |
| `vision_provider_id` | - | 多模态识图模型 Provider ID。下拉选项自动同步 AstrBot 已配置的聊天模型 Provider（启动时与每次识图调用时刷新）；留空则截图仅发用户不做识图 |
| `vision_prompt` | - | 自定义识图提示词 |
| `cache_days` | 3 | 媒体缓存保留天数，超期自动清理 |
| `agent_max_steps` | 70 | 子代理单次委托最大工具步数 |
| `agent_tool_timeout` | 1200 | 子代理单次工具超时（秒） |
| `banned_words` | 11 词 | 内容禁词过滤 |
| `block_internal_ip` | true | SSRF 防护 |

## 🎯 感知模式控制（v1.3.0）

感知模式决定浏览器子代理「用文字还是截图」理解页面，三处控制点，优先级从高到低：

1. **工具参数级（按任务覆盖）**：`browse_web` 的 `input` 开头加前缀
   `perception=text|text_image|image`（大小写不敏感，后跟空格或换行再接任务描述），
   仅本次任务生效。例：`perception=text 打开 https://example.com 读取正文`
   （纯文字模式，绝对不触发截图识图，适合读文档/查报错/看代码）；
   `perception=image 看看这个页面长什么样`（截图为主）。
   非法值（如 `perception=foo`）会被忽略并记录告警，回落到规则/全局配置。
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

## 🤝 致谢

- 灵感与部分交互模式参考 [astrbot_plugin_browser](https://github.com/Zhalslar/astrbot_plugin_browser)（Playwright 浏览器操作思路）
- llm_tool / 会话过滤模式参考 AstrBot 生态插件与 [AstrBot 官方文档](https://astrbot.app/)
- 多模态识图链路基于 AstrBot 的多模态 Provider 能力

## 📄 许可证

[MIT](./LICENSE)
