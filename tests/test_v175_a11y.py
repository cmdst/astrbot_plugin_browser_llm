"""v1.7.5 感知层（``core.a11y``）与显示固化（``core.browser`` 增量）单元测试。

覆盖规格 §2（感知层）与 §10.1（显示固化）的全部可判定项，**全部离线**：
用 2026-10-27 真实 CDP 输出（Chromium headless + ``page.set_content``）裁剪出的
JSON 夹具 + fake CDP session / fake page，不需要真浏览器、不联网、不碰生产。

夹具来源（可复跑）：``/tmp/perception_probe/capture_ax.py`` 同款探针
（规格附录 A.1 探针 1 的加强版：主帧按钮 + open shadow 按钮 + iframe 按钮 +
``opacity:0`` 隐藏块 + ``display:none`` 块 + 选项列表），
字段原样保留 ``role/name/value/properties/backendDOMNodeId/ignored/childIds``；
``FrameInfo``/``PageSnapshot`` 的字段名来自真实输出，不是手写想象。

三条口径（测试即契约）：

1. **给模型的只有「角色 + 名字 + 状态」**：结构视图里不得出现像素坐标、selector、
   class、ref（``show_ref=True`` 除外）；
2. **降级必须显式**：取树超时/异常返回带 code 的降级结果，绝不返回"看起来正常的空树"；
3. **ref 与 mark 纪元都不复用**：ref 跨快照单调递增；mark 只在"本张图 + 60s +
   无改写动作"内有效。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 库根可跑（不依赖 conftest 也能 import）
    sys.path.insert(0, str(ROOT))

import core.a11y as a11y  # noqa: E402
import core.browser as browser_mod  # noqa: E402
from core.a11y import (  # noqa: E402
    A11yLimits,
    A11yNode,
    CdpSessionPool,
    FrameInfo,
    LazyScrollPlan,
    Mark,
    MarkEpoch,
    PageSnapshot,
    RefAllocator,
    SoMResult,
)
from core.browser import (  # noqa: E402
    BrowserCore,
    check_cjk_fonts,
    context_options,
    resolve_launch_plan,
    validate_headless_mode,
)

# ---------------------------------------------------------------------------
# 真实夹具（2026-10-27 CDP 实测，按节点裁剪；字段原样）
# ---------------------------------------------------------------------------

_FIXTURES = json.loads(
    r"""{"main":[{"nodeId":"1","childIds":["5"],"role":{"type":"internalRole","value":"RootWebArea"},"name":{"type":"computedString","value":"感知层夹具","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"attribute","attribute":"aria-label","superseded":true},{"type":"relatedElement","value":{"type":"computedString","value":"感知层夹具"},"nativeSource":"title"}]},"properties":[{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"focused","value":{"type":"booleanOrUndefined","value":true}},{"name":"url","value":{"type":"string","value":"about:blank"}}],"backendDOMNodeId":1,"frameId":"CA6FB93D2BDA7E6F1947FC09AEDB40E7","ignored":false},{"nodeId":"5","parentId":"1","childIds":["10"],"role":{"type":"role","value":"none"},"backendDOMNodeId":5,"ignored":true,"ignoredReasons":[{"name":"uninteresting","value":{"type":"boolean","value":true}}]},{"nodeId":"10","parentId":"5","childIds":["11","12","13","14","54","15","16","18","19","21","22","23","24","37","40","44","48"],"role":{"type":"role","value":"none"},"backendDOMNodeId":10,"ignored":true,"ignoredReasons":[{"name":"uninteresting","value":{"type":"boolean","value":true}}]},{"nodeId":"11","parentId":"10","childIds":["50"],"role":{"type":"role","value":"heading"},"name":{"type":"computedString","value":"主文档标题","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"主文档标题"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"level","value":{"type":"integer","value":1}}],"backendDOMNodeId":11,"ignored":false},{"nodeId":"12","parentId":"10","childIds":["51"],"role":{"type":"role","value":"heading"},"name":{"type":"computedString","value":"二级标题","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"二级标题"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"level","value":{"type":"integer","value":2}}],"backendDOMNodeId":12,"ignored":false},{"nodeId":"13","parentId":"10","childIds":["52"],"role":{"type":"role","value":"button"},"name":{"type":"computedString","value":"主按钮","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"relatedElement","nativeSource":"label"},{"type":"contents","value":{"type":"computedString","value":"主按钮"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}}],"backendDOMNodeId":13,"ignored":false},{"nodeId":"14","parentId":"10","childIds":["53"],"role":{"type":"role","value":"link"},"name":{"type":"computedString","value":"帮助中心","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"帮助中心"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}}],"backendDOMNodeId":14,"ignored":false},{"nodeId":"16","parentId":"10","childIds":["17"],"role":{"type":"role","value":"textbox"},"name":{"type":"computedString","value":"姓名","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"relatedElement","value":{"type":"computedString","value":"姓名"},"nativeSource":"labelfor","nativeSourceValue":{"type":"nodeList","relatedNodes":[{"backendDOMNodeId":15,"text":"姓名"}]}},{"type":"attribute","attribute":"title","superseded":true},{"type":"placeholder","attribute":"placeholder","superseded":true},{"type":"placeholder","attribute":"aria-placeholder","superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"editable","value":{"type":"token","value":"plaintext"}},{"name":"settable","value":{"type":"booleanOrUndefined","value":true}},{"name":"multiline","value":{"type":"boolean","value":false}},{"name":"readonly","value":{"type":"boolean","value":false}},{"name":"required","value":{"type":"boolean","value":false}},{"name":"labelledby","value":{"type":"nodeList","relatedNodes":[{"backendDOMNodeId":15,"text":"姓名"}]}}],"backendDOMNodeId":16,"ignored":false},{"nodeId":"19","parentId":"10","childIds":["20"],"role":{"type":"role","value":"textbox"},"name":{"type":"computedString","value":"密码","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"relatedElement","value":{"type":"computedString","value":"密码"},"nativeSource":"labelfor","nativeSourceValue":{"type":"nodeList","relatedNodes":[{"backendDOMNodeId":18,"text":"密码"}]}},{"type":"attribute","attribute":"title","superseded":true},{"type":"placeholder","attribute":"placeholder","superseded":true},{"type":"placeholder","attribute":"aria-placeholder","superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"editable","value":{"type":"token","value":"plaintext"}},{"name":"settable","value":{"type":"booleanOrUndefined","value":true}},{"name":"multiline","value":{"type":"boolean","value":false}},{"name":"readonly","value":{"type":"boolean","value":false}},{"name":"required","value":{"type":"boolean","value":false}},{"name":"labelledby","value":{"type":"nodeList","relatedNodes":[{"backendDOMNodeId":18,"text":"密码"}]}}],"backendDOMNodeId":19,"ignored":false},{"nodeId":"21","parentId":"10","childIds":[],"role":{"type":"role","value":"checkbox"},"name":{"type":"computedString","value":"同意条款","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"同意条款"},"attribute":"aria-label","attributeValue":{"type":"string","value":"同意条款"}},{"type":"relatedElement","superseded":true,"nativeSource":"label"},{"type":"contents","superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"checked","value":{"type":"tristate","value":"false"}}],"backendDOMNodeId":21,"ignored":false},{"nodeId":"22","parentId":"10","childIds":["57"],"role":{"type":"role","value":"radio"},"name":{"type":"computedString","value":"非常同意","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"非常同意"},"attribute":"aria-label","attributeValue":{"type":"string","value":"非常同意"}},{"type":"contents","value":{"type":"computedString","value":"非常同意"},"superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"checked","value":{"type":"tristate","value":"false"}}],"backendDOMNodeId":22,"ignored":false},{"nodeId":"23","parentId":"10","childIds":["58"],"role":{"type":"role","value":"radio"},"name":{"type":"computedString","value":"非常不同意","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"非常不同意"},"attribute":"aria-label","attributeValue":{"type":"string","value":"非常不同意"}},{"type":"contents","value":{"type":"computedString","value":"非常不同意"},"superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"checked","value":{"type":"tristate","value":"true"}}],"backendDOMNodeId":23,"ignored":false},{"nodeId":"24","parentId":"10","childIds":["27"],"role":{"type":"role","value":"combobox"},"name":{"type":"computedString","value":"地区","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"地区"},"attribute":"aria-label","attributeValue":{"type":"string","value":"地区"}},{"type":"relatedElement","superseded":true,"nativeSource":"label"},{"type":"attribute","attribute":"title","superseded":true}]},"value":{"type":"string","value":"北京"},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"hasPopup","value":{"type":"token","value":"menu"}},{"name":"expanded","value":{"type":"booleanOrUndefined","value":false}}],"backendDOMNodeId":24,"ignored":false},{"nodeId":"44","parentId":"10","childIds":["45","46","47"],"role":{"type":"role","value":"list"},"name":{"type":"computedString","value":"选项列表","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"选项列表"},"attribute":"aria-label","attributeValue":{"type":"string","value":"选项列表"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[],"backendDOMNodeId":44,"ignored":false},{"nodeId":"48","parentId":"10","childIds":[],"role":{"type":"internalRole","value":"Iframe"},"name":{"type":"computedString","value":"","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"attribute","attribute":"title"}]},"properties":[],"backendDOMNodeId":48,"ignored":false},{"nodeId":"50","parentId":"11","childIds":["-1000000002"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"主文档标题","sources":[{"type":"contents","value":{"type":"computedString","value":"主文档标题"}}]},"properties":[],"backendDOMNodeId":50,"ignored":false},{"nodeId":"52","parentId":"13","childIds":["-1000000004"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"主按钮","sources":[{"type":"contents","value":{"type":"computedString","value":"主按钮"}}]},"properties":[],"backendDOMNodeId":52,"ignored":false},{"nodeId":"57","parentId":"22","childIds":["-1000000009"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"非常同意","sources":[{"type":"contents","value":{"type":"computedString","value":"非常同意"}}]},"properties":[],"backendDOMNodeId":57,"ignored":false},{"nodeId":"58","parentId":"23","childIds":["-1000000010"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"非常不同意","sources":[{"type":"contents","value":{"type":"computedString","value":"非常不同意"}}]},"properties":[],"backendDOMNodeId":58,"ignored":false},{"nodeId":"59","parentId":"37","childIds":["-1000000011"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"非常同意","sources":[{"type":"contents","value":{"type":"computedString","value":"非常同意"}}]},"properties":[],"backendDOMNodeId":59,"ignored":false},{"nodeId":"41","parentId":"40","childIds":["61"],"role":{"type":"role","value":"button"},"name":{"type":"computedString","value":"影子按钮","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"影子按钮"},"attribute":"aria-label","attributeValue":{"type":"string","value":"影子按钮"}},{"type":"relatedElement","superseded":true,"nativeSource":"label"},{"type":"contents","value":{"type":"computedString","value":"影子按钮"},"superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}}],"backendDOMNodeId":41,"ignored":false},{"nodeId":"42","parentId":"40","childIds":["43"],"role":{"type":"role","value":"textbox"},"name":{"type":"computedString","value":"影子输入","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","value":{"type":"computedString","value":"影子输入"},"attribute":"aria-label","attributeValue":{"type":"string","value":"影子输入"}},{"type":"relatedElement","superseded":true,"nativeSource":"label"},{"type":"attribute","attribute":"title","superseded":true},{"type":"placeholder","attribute":"placeholder","superseded":true},{"type":"placeholder","attribute":"aria-placeholder","superseded":true},{"type":"attribute","attribute":"title","superseded":true}]},"value":{"type":"string","value":"影子值"},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"editable","value":{"type":"token","value":"plaintext"}},{"name":"settable","value":{"type":"booleanOrUndefined","value":true}},{"name":"multiline","value":{"type":"boolean","value":false}},{"name":"readonly","value":{"type":"boolean","value":false}},{"name":"required","value":{"type":"boolean","value":false}}],"backendDOMNodeId":42,"ignored":false},{"nodeId":"45","parentId":"44","childIds":["63","64"],"role":{"type":"role","value":"option"},"name":{"type":"computedString","value":"选项一","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"选项一"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"selected","value":{"type":"booleanOrUndefined","value":false}}],"backendDOMNodeId":45,"ignored":false},{"nodeId":"46","parentId":"44","childIds":["65","66"],"role":{"type":"role","value":"option"},"name":{"type":"computedString","value":"选项二","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"选项二"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"selected","value":{"type":"booleanOrUndefined","value":false}}],"backendDOMNodeId":46,"ignored":false},{"nodeId":"47","parentId":"44","childIds":["67","68"],"role":{"type":"role","value":"option"},"name":{"type":"computedString","value":"选项三","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"contents","value":{"type":"computedString","value":"选项三"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"selected","value":{"type":"booleanOrUndefined","value":false}}],"backendDOMNodeId":47,"ignored":false},{"nodeId":"-1000000002","parentId":"50","childIds":[],"role":{"type":"internalRole","value":"InlineTextBox"},"name":{"type":"computedString","value":"主文档标题"},"properties":[],"ignored":false}],"sub":[{"nodeId":"3","childIds":["69"],"role":{"type":"internalRole","value":"RootWebArea"},"name":{"type":"computedString","value":"","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"attribute","attribute":"aria-label","superseded":true},{"type":"relatedElement","nativeSource":"title"}]},"properties":[{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}},{"name":"url","value":{"type":"string","value":"about:srcdoc"}}],"backendDOMNodeId":3,"frameId":"60F8142D4B4D21BA128A78BD9FCA8203","ignored":false},{"nodeId":"69","parentId":"3","childIds":["71"],"role":{"type":"role","value":"none"},"backendDOMNodeId":69,"ignored":true,"ignoredReasons":[{"name":"uninteresting","value":{"type":"boolean","value":true}}]},{"nodeId":"71","parentId":"69","childIds":["72"],"role":{"type":"role","value":"generic"},"name":{"type":"computedString","value":"","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"}]},"properties":[],"backendDOMNodeId":71,"ignored":false},{"nodeId":"72","parentId":"71","childIds":["73"],"role":{"type":"role","value":"button"},"name":{"type":"computedString","value":"帧内按钮","sources":[{"type":"relatedElement","attribute":"aria-labelledby"},{"type":"attribute","attribute":"aria-label"},{"type":"relatedElement","nativeSource":"label"},{"type":"contents","value":{"type":"computedString","value":"帧内按钮"}},{"type":"attribute","attribute":"title","superseded":true}]},"properties":[{"name":"invalid","value":{"type":"token","value":"false"}},{"name":"focusable","value":{"type":"booleanOrUndefined","value":true}}],"backendDOMNodeId":72,"ignored":false},{"nodeId":"73","parentId":"72","childIds":["-1000000020"],"role":{"type":"internalRole","value":"StaticText"},"name":{"type":"computedString","value":"帧内按钮","sources":[{"type":"contents","value":{"type":"computedString","value":"帧内按钮"}}]},"properties":[],"backendDOMNodeId":73,"ignored":false},{"nodeId":"-1000000020","parentId":"73","childIds":[],"role":{"type":"internalRole","value":"InlineTextBox"},"name":{"type":"computedString","value":"帧内按钮"},"properties":[],"ignored":false}],"frame_tree":{"frame":{"id":"CA6FB93D2BDA7E6F1947FC09AEDB40E7","name":"","url":"about:blank"},"childFrames":[{"frame":{"id":"60F8142D4B4D21BA128A78BD9FCA8203","name":"fr","url":"about:srcdoc","parentId":"CA6FB93D2BDA7E6F1947FC09AEDB40E7"}}]},"shadow":{"nodeId":60,"parentId":10,"backendNodeId":40,"nodeType":1,"nodeName":"DIV","localName":"div","nodeValue":"","childNodeCount":0,"children":[],"attributes":["id","host"],"shadowRoots":[{"nodeId":61,"backendNodeId":87,"nodeType":11,"nodeName":"#document-fragment","localName":"","nodeValue":"","childNodeCount":2,"children":[{"nodeId":62,"parentId":61,"backendNodeId":41,"nodeType":1,"nodeName":"BUTTON","localName":"button","nodeValue":"","childNodeCount":1,"children":[{"nodeId":63,"parentId":62,"backendNodeId":61,"nodeType":3,"nodeName":"#text","localName":"","nodeValue":"影子按钮"}],"attributes":["aria-label","影子按钮"]},{"nodeId":64,"parentId":61,"backendNodeId":42,"nodeType":1,"nodeName":"INPUT","localName":"input","nodeValue":"","childNodeCount":0,"children":[],"attributes":["aria-label","影子输入"],"shadowRoots":[{"nodeId":65,"backendNodeId":88,"nodeType":11,"nodeName":"#document-fragment","localName":"","nodeValue":"","childNodeCount":1,"children":[{"nodeId":66,"parentId":65,"backendNodeId":43,"nodeType":1,"nodeName":"DIV","localName":"div","nodeValue":"","childNodeCount":1,"children":[{"nodeId":67,"parentId":66,"backendNodeId":62,"nodeType":3,"nodeName":"#text","localName":"","nodeValue":"影子值"}],"attributes":[]}],"shadowRootType":"user-agent"}]}],"shadowRootType":"open"}]}}"""
)

AX_MAIN = _FIXTURES["main"]  # 主帧（含 shadow 穿透后的 影子按钮/影子输入）
AX_SUB = _FIXTURES["sub"]  # 子帧（iframe#fr 的 帧内按钮）
FRAME_TREE = _FIXTURES["frame_tree"]  # Page.getFrameTree（主帧 + 一层子帧 name="fr"）
SHADOW_DOC = _FIXTURES["shadow"]  # DOM.getDocument(pierce=True) 的 div#host 子树

MAIN_FRAME_ID = FRAME_TREE["frame"]["id"]
SUB_FRAME_ID = FRAME_TREE["childFrames"][0]["frame"]["id"]

#: 真实 DOM.getBoxModel 结果（backendDOMNodeId -> (x, y, w, h)，主视口 CSS 坐标）
BOXES: dict[int, tuple[float, float, float, float]] = {
    13: (8.0, 144.2, 39.0, 19.0),  # button 主按钮
    14: (58.6, 140.2, 64.0, 24.0),  # link 帮助中心
    16: (162.2, 147.2, 171.0, 15.0),  # textbox 姓名
    19: (376.8, 147.2, 171.0, 15.0),  # textbox 密码
    21: (559.4, 146.2, 13.0, 13.0),  # checkbox 同意条款
    22: (0.0, 166.2, 1280.0, 22.4),  # radio 非常同意
    23: (0.0, 188.6, 1280.0, 22.4),  # radio 非常不同意
    41: (8.0, 259.3, 52.0, 19.0),  # button 影子按钮（shadow 内，坐标仍是主视口）
    42: (72.0, 261.3, 171.0, 15.0),  # textbox 影子输入
}


# ---------------------------------------------------------------------------
# fake CDP / fake page（鸭子类型，不 import playwright）
# ---------------------------------------------------------------------------


class FakeCdp:
    """记录命令序列的假 CDP session（可按方法/参数注入异常与响应）。"""

    def __init__(self, handlers=None, *, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.handlers = dict(handlers or {})
        self.delay = delay

    async def send(self, method, params=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        handler = self.handlers.get(method)
        if handler is None:
            return {}
        if callable(handler):
            return handler(params)
        if isinstance(handler, Exception):
            raise handler
        return handler

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def params_for(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]


class FakeContext:
    def __init__(self, cdp) -> None:
        self.cdp = cdp
        self.kwargs = None
        self.init_scripts: list[str] = []
        self.routes: list[tuple[str, object]] = []

    async def new_cdp_session(self, page):
        return self.cdp

    async def new_page(self):
        return self.page

    async def close(self):
        return None

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class FakePage:
    """最小 page：url/title/evaluate/screenshot + context.new_cdp_session。"""

    def __init__(
        self,
        cdp=None,
        *,
        url: str = "https://example.com/search?q=secret",
        title: str = "示例页面",
        scroll: dict | None = None,
        eval_states: list[dict] | None = None,
    ) -> None:
        self.context = FakeContext(cdp or FakeCdp())
        self.context.page = self
        self.url = url
        self._title = title
        self.scroll = dict(scroll or {"y": 0, "h": 2000})
        self.eval_states = list(eval_states or [])
        self.evaluated: list[tuple[str, tuple]] = []
        self.screenshot_kwargs: list[dict] = []
        self.fail_screenshot = False

    async def title(self):
        return self._title

    async def evaluate(self, script, *args):
        self.evaluated.append((script, args))
        if args:  # 滚动调用：evaluate(js, dy)
            return None
        if self.eval_states:
            return self.eval_states.pop(0)
        return self.scroll

    async def screenshot(self, **kwargs):
        self.screenshot_kwargs.append(kwargs)
        if self.fail_screenshot:
            raise RuntimeError("screenshot broken")
        return b"\x89PNG\r\n\x1a\n" + b"0" * 16


class BarePage:
    """没有 context（拿不到 CDP 会话）的页面。"""

    url = "https://example.com/"
    _title = "无 CDP"

    async def title(self):
        return self._title


def _ax_handlers(main_nodes=None, sub_nodes=None, *, sub_error: Exception | None = None):
    """构造按 frameId 分发的 ``Accessibility.getFullAXTree`` 响应。"""
    main_nodes = AX_MAIN if main_nodes is None else main_nodes
    sub_nodes = AX_SUB if sub_nodes is None else sub_nodes

    def full_ax(params):
        frame_id = params.get("frameId")
        if frame_id in (None, MAIN_FRAME_ID):
            return {"nodes": list(main_nodes)}
        if sub_error is not None:
            raise sub_error
        return {"nodes": list(sub_nodes)}

    return {
        "Page.getFrameTree": FRAME_TREE,
        "Accessibility.getFullAXTree": full_ax,
    }


def _run(coro):
    return asyncio.run(coro)


def _pruned(boxes=None, limits=None, frame=None):
    return a11y.prune_ax_nodes(
        AX_MAIN,
        limits=limits,
        frame=frame or FrameInfo(frame_id=MAIN_FRAME_ID),
        boxes=BOXES if boxes is None else boxes,
    )


def _node(node_id, role, name="", *, value=None, props=None, ignored=False, children=()):
    """手工造一个 AX 节点（形状与真实 CDP 输出一致）。"""
    item = {
        "nodeId": str(node_id),
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "ignored": ignored,
        "childIds": [str(c) for c in children],
        "properties": [
            {"name": key, "value": {"type": "boolean", "value": val}}
            for key, val in (props or {}).items()
        ],
    }
    if value is not None:
        item["value"] = {"type": "string", "value": value}
    return item


# ===========================================================================
# 1. 包纪律：可导入、不拉 playwright / astrbot
# ===========================================================================


def test_module_imports_without_playwright_or_astrbot():
    """``core.a11y`` 必须在**没有** playwright/astrbot 的环境里可导入（铁律 1）。"""
    code = (
        "import sys; sys.path.insert(0, '.'); import core.a11y as m; "
        "print('playwright' in sys.modules, 'astrbot' in sys.modules, len(m.__all__))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    assert out.returncode == 0, out.stderr
    playwright_loaded, astrbot_loaded, exported = out.stdout.split()
    assert playwright_loaded == "False", "感知层不得在模块顶层 import playwright"
    assert astrbot_loaded == "False", "感知层不得 import astrbot"
    assert int(exported) > 0


def test_public_api_is_complete():
    """integrator 依赖的公开符号必须齐（少一个就是接线缺口）。"""
    for name in (
        "RENDER_ROLES",
        "A11yNode",
        "PageSnapshot",
        "A11yLimits",
        "RefAllocator",
        "CdpSessionPool",
        "collect_ax",
        "prune_ax_nodes",
        "snapshot",
        "render_node_lines",
        "observation_header",
        "select_marks",
        "som_screenshot",
        "render_marks_table",
        "MarkEpoch",
        "flatten_frame_tree",
        "pierce_shadow",
        "flatten_pierced_document",
        "lazy_scroll",
        "lazy_observe",
        "merge_nodes",
        "redact_url",
    ):
        assert hasattr(a11y, name), name
        assert name in a11y.__all__, f"{name} 未登记进 __all__"


# ===========================================================================
# 2. 取树：命令序列 / 逐帧 / 超时与异常降级
# ===========================================================================


def test_collect_ax_issues_enable_then_full_tree_per_frame():
    """命令序列：``Accessibility.enable`` 一次 → ``Page.getFrameTree`` → 每帧取树。"""
    cdp = FakeCdp(_ax_handlers())
    page = FakePage(cdp)
    pool = CdpSessionPool()
    collection = _run(a11y.collect_ax(page, pool=pool))

    assert collection.ok is True
    assert len(collection.frames) == 2, "主帧 + iframe#fr 都要取"
    methods = cdp.methods()
    assert methods[0] == "Accessibility.enable", "必须先 enable 再取树"
    assert methods[1] == "Page.getFrameTree"
    assert methods.count("Accessibility.enable") == 1

    frame_ids = [params.get("frameId") for params in cdp.params_for("Accessibility.getFullAXTree")]
    assert frame_ids == [MAIN_FRAME_ID, SUB_FRAME_ID], "主帧也要显式传 frameId"


def test_collect_ax_enable_is_idempotent_across_calls():
    cdp = FakeCdp(_ax_handlers())
    page = FakePage(cdp)
    pool = CdpSessionPool()
    _run(a11y.collect_ax(page, pool=pool))
    _run(a11y.collect_ax(page, pool=pool))
    assert cdp.methods().count("Accessibility.enable") == 1, "enable 每会话只需一次"


def test_cdp_send_timeout_returns_code_not_exception():
    """``cdp_send`` 超时 ⇒ ``code=timeout``（不是抛异常，也不是空树）。"""
    cdp = FakeCdp(delay=0.5)
    call = _run(a11y.cdp_send(cdp, "Accessibility.enable", timeout_s=0.05))
    assert call.ok is False
    assert call.code == a11y.DEGRADE_TIMEOUT
    assert "超时" in call.reason


def test_collect_ax_timeout_degrades_explicitly():
    cdp = FakeCdp({"Accessibility.enable": asyncio.TimeoutError()})
    collection = _run(a11y.collect_ax(FakePage(cdp)))
    assert collection.ok is False
    assert collection.code == a11y.DEGRADE_TIMEOUT
    assert collection.frames == ()
    assert collection.notes, "降级必须有可读说明，不能静默空树"


def test_collect_ax_error_degrades_with_reason():
    cdp = FakeCdp({"Accessibility.enable": RuntimeError("cdp exploded")})
    collection = _run(a11y.collect_ax(FakePage(cdp)))
    assert collection.ok is False
    assert collection.code == a11y.DEGRADE_ERROR
    assert "cdp exploded" in " ".join(collection.notes)


def test_collect_ax_without_cdp_is_unavailable():
    collection = _run(a11y.collect_ax(BarePage()))
    assert collection.ok is False
    assert collection.code == a11y.DEGRADE_UNAVAILABLE
    assert collection.notes and "new_cdp_session" in collection.notes[0]


def test_collect_ax_frame_tree_failure_still_returns_main_frame():
    cdp = FakeCdp(
        {
            "Page.getFrameTree": RuntimeError("frame tree broken"),
            "Accessibility.getFullAXTree": {"nodes": list(AX_MAIN)},
        }
    )
    collection = _run(a11y.collect_ax(FakePage(cdp)))
    assert collection.ok is True, "帧树坏了仍应给出主帧内容"
    assert len(collection.frames) == 1
    assert cdp.params_for("Accessibility.getFullAXTree")[0] == {}, (
        "帧树不可用时主帧取树不带 frameId（与带主帧 id 等价）"
    )
    assert any("帧树" in note for note in collection.notes), (
        "帧树不可用是**显式**降级，必须出现在 notes 里"
    )
    assert collection.degraded is True


def test_collect_ax_unreachable_subframe_degrades_not_fatal():
    """OOPIF/已 detach 的子帧：该帧降级，其它帧照常（内容尽量给全）。"""
    cdp = FakeCdp(_ax_handlers(sub_error=RuntimeError("frame detached")))
    collection = _run(a11y.collect_ax(FakePage(cdp)))
    assert collection.ok is True
    main_frame, sub_frame = collection.frames
    assert main_frame.ok is True
    assert sub_frame.ok is False
    assert sub_frame.frame.reachable is False
    assert "frame detached" in sub_frame.reason
    assert collection.degraded is True
    assert any("iframe#fr" in note for note in collection.notes)


# ===========================================================================
# 3. 裁剪：真实 AX JSON → 节点表（纯函数）
# ===========================================================================


def test_prune_real_ax_tree_maps_every_field():
    result = _pruned()
    by_name = {node.name: node for node in result.nodes}

    button = by_name["主按钮"]
    assert button.role == "button"
    assert [node.role for node in result.nodes[:3]] == ["heading", "heading", "button"]
    assert button.ref == 3, "ref 按裁剪后的文档顺序自增（1 起）"
    assert button.box == BOXES[13]
    assert button.actionable is True

    password = by_name["密码"]
    assert password.role == "textbox"
    assert password.value == "", "空值就是空串（不是 'None'）"

    agree = by_name["非常同意"]
    assert (agree.checked, agree.checked_raw) == (False, "false")
    disagree = by_name["非常不同意"]
    assert (disagree.checked, disagree.checked_raw) == (True, "true")

    agrees = by_name["同意条款"]
    assert agrees.role == "checkbox" and agrees.checked is False

    region = by_name["地区"]
    assert region.role == "combobox"
    assert region.value == "北京"
    assert region.expanded is False

    heading = by_name["主文档标题"]
    assert heading.role == "heading" and heading.level == 1
    assert by_name["二级标题"].level == 2

    shadow_button = by_name["影子按钮"]
    assert shadow_button.role == "button", "open shadow 内容必须出现在主帧树里"
    assert by_name["影子输入"].value == "影子值"


def test_prune_drops_ignored_and_non_render_roles():
    result = _pruned()
    roles = {node.role for node in result.nodes}
    assert "none" not in roles, "ignored=None 节点必须丢弃"
    assert "statictext" not in roles and "inlinetextbox" not in roles
    assert "rootwebarea" not in roles and "iframe" not in roles
    assert result.stats["ignored"] >= 2
    assert result.stats["role"] >= 5
    assert result.pruned == result.raw_total - len(result.nodes)


def test_prune_keeps_children_of_ignored_node():
    """ignored 只丢节点本身，**不影响子节点**（真实 Chromium 的 AX 语义）。"""
    nodes = [
        _node(1, "generic", ignored=True, children=[2]),
        _node(2, "button", "被包裹的按钮"),
    ]
    result = a11y.prune_ax_nodes(nodes)
    assert [node.name for node in result.nodes] == ["被包裹的按钮"]
    assert result.stats["ignored"] == 1


def test_prune_truncates_long_name_and_value():
    nodes = [_node(1, "button", "长" * 300), _node(2, "textbox", "标题", value="值" * 300)]
    result = a11y.prune_ax_nodes(nodes, limits=A11yLimits(max_name_chars=20))
    button, textbox = result.nodes
    assert len(button.name) == 20 and button.name.endswith("…")
    assert len(textbox.value) == 20 and textbox.value.endswith("…")
    assert button.truncated is True and textbox.truncated is True


def test_prune_masks_sensitive_values():
    nodes = [
        _node(1, "textbox", "登录密码", value="hunter2"),
        _node(2, "textbox", "姓名", value="张三"),
        _node(3, "textbox", "验证码", value="123456"),
    ]
    result = a11y.prune_ax_nodes(nodes)
    values = {node.name: node.value for node in result.nodes}
    assert values["登录密码"] == "***"
    assert values["验证码"] == "***"
    assert values["姓名"] == "张三", "非敏感字段不该被打码"


def test_prune_maps_focused_disabled_expanded_from_properties():
    nodes = [
        _node(1, "textbox", "搜索", props={"focused": True, "disabled": False}),
        _node(2, "button", "提交", props={"disabled": True}),
        _node(3, "combobox", "城市", props={"expanded": True}),
        _node(4, "radio", "半选", props={"checked": "mixed"}),
    ]
    result = a11y.prune_ax_nodes(nodes)
    by_name = {node.name: node for node in result.nodes}
    assert by_name["搜索"].focused is True
    assert by_name["搜索"].disabled is False
    assert by_name["提交"].disabled is True
    assert by_name["提交"].actionable is False, "disabled 元素不可点"
    assert by_name["城市"].expanded is True
    assert by_name["半选"].checked is None and by_name["半选"].checked_raw == "mixed"


def test_prune_hidden_box_is_kept_but_not_actionable():
    """D2 回归：``opacity:0`` / 零尺寸元素**不得**被当成可点元素。"""
    nodes = [
        _node(1, "radio", "非常同意"),
        _node(2, "radio", "非常同意"),
    ]
    boxes = {1: (0.0, 0.0, 1.0, 1.0)}  # 只有 1px：等于不可见
    result = a11y.prune_ax_nodes(nodes, boxes=boxes)
    first, second = result.nodes
    assert first.box == (0.0, 0.0, 1.0, 1.0)
    assert first.visible is False and first.actionable is False
    assert second.box is None and second.actionable is False
    assert len(result.nodes) == 2, "不可见元素仍保留在树里（模型要看得到它存在）"


def test_prune_budget_prefers_actionable_and_marks_truncated():
    actionable = [_node(i, "button", f"按钮{i}") for i in range(1, 121)]
    noise = [_node(1000 + i, "listitem", f"条目{i}") for i in range(30)]
    boxes = {i: (0.0, float(i), 40.0, 18.0) for i in range(1, 121)}
    result = a11y.prune_ax_nodes(
        actionable + noise, limits=A11yLimits(max_nodes=50), boxes=boxes
    )
    assert len(result.nodes) == 50
    assert result.truncated is True
    assert result.raw_total == 150
    assert result.pruned == 100
    assert result.stats["budget"] == 100
    assert all(node.actionable for node in result.nodes), "预算优先留给可交互元素"
    assert [node.name for node in result.nodes] == [f"按钮{i}" for i in range(1, 51)]


def test_prune_assigns_refs_from_start_ref():
    result = a11y.prune_ax_nodes(AX_MAIN, boxes=BOXES, start_ref=100)
    assert result.nodes[0].ref == 100
    assert [node.ref for node in result.nodes] == list(
        range(100, 100 + len(result.nodes))
    )
    assert result.next_ref == 100 + len(result.nodes)


# ===========================================================================
# 4. ref 纪元：同一快照唯一 / 跨快照不复用
# ===========================================================================


def test_ref_allocator_allocates_monotonically():
    allocator = RefAllocator()
    assert allocator.allocate(3) == 1
    assert allocator.allocate(2) == 4
    assert allocator.next_ref == 6
    allocator.reset()
    assert allocator.allocate(1) == 1


def test_snapshot_refs_never_reused_across_snapshots():
    """同一分配器的两次快照：编号区间不重叠（模型不会把旧 #1 当新元素）。"""
    cdp = FakeCdp(_ax_handlers())
    page = FakePage(cdp)
    pool = CdpSessionPool()
    allocator = RefAllocator()
    first = _run(a11y.snapshot(page, pool=pool, allocator=allocator, boxes=BOXES))
    second = _run(a11y.snapshot(page, pool=pool, allocator=allocator, boxes=BOXES))

    refs_first = [node.ref for node in first.nodes]
    refs_second = [node.ref for node in second.nodes]
    assert refs_first == sorted(refs_first)
    assert refs_second == sorted(refs_second)
    assert max(refs_first) < min(refs_second), "跨快照必须持续自增，绝不复用"
    assert len(set(refs_first + refs_second)) == len(refs_first) + len(refs_second)
    assert first.by_ref(refs_first[0]) is first.nodes[0]
    assert first.by_ref(refs_second[0]) is None, "别的快照的 ref 不属于本次"


def test_snapshot_collects_frames_and_marks_degradation():
    cdp = FakeCdp(_ax_handlers(sub_error=RuntimeError("oopif missing")))
    snap = _run(
        a11y.snapshot(
            FakePage(cdp), boxes=BOXES, viewport=(1280, 800), dpr=2.0, ssrf_blocked=3
        )
    )
    assert snap.frames == 2
    assert (snap.viewport, snap.dpr) == ((1280, 800), 2.0)
    assert snap.ssrf_blocked == 3
    assert snap.degraded and "iframe#fr" in " ".join(snap.degraded)
    assert snap.title == "示例页面"
    assert snap.scroll_height == 2000


# ===========================================================================
# 5. 渲染：只有角色 + 名字 + 状态
# ===========================================================================


def test_render_node_lines_has_no_pixels_no_selector_no_ref():
    nodes = _pruned().nodes
    text = a11y.render_node_lines(nodes, max_lines=200)
    assert "- [button] 主按钮" in text
    assert '- [combobox] 地区 value="北京" (expanded=false)' in text
    for banned in ("px", "selector", "class=", "div#", "#blm", "url=", "@dpr"):
        assert banned not in text, f"结构视图不得出现 {banned!r}"
    assert "#1" not in text, "默认不给 ref 编号（防模型把显示序号当输入）"


def test_render_node_lines_show_ref_when_explicitly_asked():
    text = a11y.render_node_lines(_pruned().nodes, show_ref=True)
    assert text.splitlines()[0] == "- #1 [heading] 主文档标题 (h1)"
    assert "- #3 [button] 主按钮" in text


def test_render_node_lines_collapses_identical_and_role_runs():
    radios = [
        A11yNode(ref=i, role="radio", name=f"选项{i}", box=(0.0, float(i), 100.0, 20.0))
        for i in range(1, 8)
    ]
    collapsed = a11y.render_node_lines(radios)
    assert "（共 7 个选项，已折叠）" in collapsed

    same = [
        A11yNode(ref=i, role="button", name="下一页", box=(0.0, 0.0, 10.0, 10.0))
        for i in range(1, 3)
    ]
    assert "×2" in a11y.render_node_lines(same)


def test_render_node_lines_empty_uses_hint():
    assert a11y.render_node_lines(()) == a11y.NO_NODES_HINT
    assert "browse_screenshot" in a11y.NO_NODES_HINT


def test_observation_header_redacts_query_and_formats_spec_fields():
    snap = _run(a11y.snapshot(FakePage(FakeCdp(_ax_handlers())), boxes=BOXES))
    header = a11y.observation_header(snap, step=4)
    assert header.startswith("【观测 #4】https://example.com/search |")
    assert "q=secret" not in header, "观测块不得带 query（隐私）"
    assert "@dpr1" in header and "y=0/2000" in header
    assert f"nodes={len(snap.nodes)}(" in header and "frames=2" in header


def test_observation_notices_match_spec_wording():
    """D9-①：被 SSRF 拦下时必须逐字提示（否则站点"看起来没数据"）。"""
    assert a11y.SSRF_NOTICE_TEMPLATE == "⚠ 本页有 {n} 个请求被 SSRF 策略拦截"
    assert a11y.ssrf_notice(3) == "⚠ 本页有 3 个请求被 SSRF 策略拦截"
    assert a11y.ssrf_notice(0) == ""

    snap = PageSnapshot(
        url="https://example.com/",
        title="被拦",
        viewport=(1280, 800),
        dpr=1.0,
        scroll_y=0,
        scroll_height=800,
        nodes=_pruned().nodes,
        pruned=7,
        frames=1,
        ssrf_blocked=2,
        truncated=True,
        degraded=("iframe#fr 取树降级（error）：frame detached",),
    )
    notices = a11y.observation_notices(snap)
    assert notices[0] == "⚠ 本页有 2 个请求被 SSRF 策略拦截"
    assert notices[1] == a11y.MORE_NODES_HINT.format(more=7)
    assert notices[2].startswith("⚠ 感知降级：") and "iframe#fr" in notices[2]

    block = a11y.render_snapshot(snap, step=2, max_lines=200)
    assert block.splitlines()[0].startswith("【观测 #2】https://example.com/")
    assert "⚠ 本页有 2 个请求被 SSRF 策略拦截" in block
    assert "正文" not in block


def test_mark_ids_never_mixed_with_ref_numbers():
    """R-A7 孪生风险：SoM 用 mark id，观测块用（或不显示）ref，两者不混用。"""
    nodes = _pruned().nodes
    marks, _ = a11y.select_marks(nodes)
    assert [mark.id for mark in marks] == list(range(1, len(marks) + 1))
    assert any(mark.id != mark.ref for mark in marks), (
        "mark id（1 起，本张图内）与 ref（快照级，跨图不复用）是两套编号"
    )
    assert all(mark.ref >= 1 for mark in marks)

    table = a11y.render_marks_table(marks, width=1280, height=800)
    for mark in marks:
        assert f"\n{mark.id}. [{mark.role}]" in table or table.startswith(
            f"【SoM】labels={len(marks)}"
        )
    assert "#" not in table, "标签表里不得出现 ref 编号写法（#N）"

    model_lines = a11y.render_node_lines(nodes, max_lines=200)
    assert "#" not in model_lines, "默认结构视图不出现任何编号"


def test_snapshot_digest_and_log_never_leak_text():
    page = FakePage(FakeCdp(_ax_handlers()))

    async def text_fn(_page):
        return "正文" * 3000

    snap = _run(
        a11y.snapshot(page, text_fn=text_fn, max_text_chars=100, boxes=BOXES)
    )
    assert snap.text_truncated is True and len(snap.text) <= 100
    log_line = snap.raw_text_for_log()
    assert "正文" not in log_line, "日志只给长度，不给正文"
    assert "q=secret" not in log_line
    assert "text_len=" in log_line
    assert snap.digest() != replace(snap, title="另一个标题").digest()


# ===========================================================================
# 6. iframe / Shadow DOM
# ===========================================================================


def test_flatten_frame_tree_builds_frame_paths():
    scan = a11y.flatten_frame_tree(FRAME_TREE)
    assert scan.ok is True
    main, sub = scan.frames
    assert main.frame_id == MAIN_FRAME_ID and main.path == () and main.depth == 0
    assert sub.frame_id == SUB_FRAME_ID
    assert sub.path == ("iframe#fr",), "帧链用 frame.name 生成"
    assert sub.parent_id == MAIN_FRAME_ID


def test_flatten_frame_tree_respects_depth_limit():
    deep = {
        "frame": {"id": "f0", "name": "", "url": "about:blank"},
        "childFrames": [
            {
                "frame": {"id": "f1", "name": "a", "url": "about:blank"},
                "childFrames": [
                    {"frame": {"id": "f2", "name": "b", "url": "about:blank"}}
                ],
            }
        ],
    }
    scan = a11y.flatten_frame_tree(deep, max_depth=1)
    assert [frame.frame_id for frame in scan.frames] == ["f0", "f1"]
    assert scan.clipped == ("f2",), "超深帧必须显式记账（executor 据此给 R-A8）"


def test_flatten_frame_tree_degrades_on_empty_input():
    scan = a11y.flatten_frame_tree(None)
    assert scan.ok is False and scan.frames == ()
    assert scan.code == a11y.DEGRADE_UNAVAILABLE and scan.reason


def test_frame_label_prefers_name_then_index():
    assert a11y.frame_label({"id": "x", "name": "fr"}) == "iframe#fr"
    assert a11y.frame_label({"id": "x", "name": ""} , index=2) == "iframe[nth=2]"


def test_pierce_shadow_walks_shadow_roots():
    cdp = FakeCdp({"DOM.enable": {}, "DOM.getDocument": {"root": SHADOW_DOC}})
    scan = _run(a11y.pierce_shadow(FakePage(cdp)))
    assert scan.ok is True
    assert cdp.methods() == ["DOM.enable", "DOM.getDocument"]
    assert cdp.params_for("DOM.getDocument")[0]["pierce"] is True
    assert scan.shadow_roots >= 1
    assert any(node.is_shadow_root for node in scan.nodes)
    in_shadow = scan.in_shadow()
    assert any(node.node_name == "BUTTON" for node in in_shadow), "影子树里的按钮要能看到"
    assert all(node.shadow_depth >= 1 for node in in_shadow)


def test_pierce_shadow_degrades_when_dom_enable_fails():
    cdp = FakeCdp({"DOM.enable": RuntimeError("DOM agent closed")})
    scan = _run(a11y.pierce_shadow(FakePage(cdp)))
    assert scan.ok is False and scan.degraded is True
    assert scan.code == a11y.DEGRADE_ERROR
    assert "DOM.enable" in scan.reason


def test_pierce_shadow_falls_back_to_domsnapshot():
    cdp = FakeCdp(
        {
            "DOM.enable": {},
            "DOM.getDocument": RuntimeError("pierce unsupported"),
            "DOMSnapshot.captureSnapshot": {
                "documents": [{"nodes": {"backendNodeId": [1, 2, 3]}}]
            },
        }
    )
    scan = _run(a11y.pierce_shadow(FakePage(cdp)))
    assert scan.ok is True
    assert "DOMSnapshot" in scan.reason
    assert cdp.methods().count("DOMSnapshot.captureSnapshot") == 1


# ===========================================================================
# 7. 懒加载滚动（参数化 + 到底判定 + 多轮合并）
# ===========================================================================


class ScrollPage:
    """只实现 evaluate 的假页面：读状态与滚动分别用不同返回。"""

    def __init__(self, states):
        self.states = list(states)
        self.calls: list[tuple[str, tuple]] = []

    async def evaluate(self, script, *args):
        self.calls.append((script, args))
        if args:
            return None
        return self.states.pop(0) if self.states else {"y": 0, "h": 800}


def test_lazy_scroll_stops_at_bottom():
    page = ScrollPage([{"y": 800, "h": 1700}, {"y": 1600, "h": 1700}])
    plan = LazyScrollPlan(rounds=5, interval_ms=0, budget_ms=5000, step_px=800)

    async def _no_sleep(_seconds):
        return None

    result = _run(a11y.lazy_scroll(page, plan, viewport_h=800, sleep=_no_sleep))
    assert result.rounds == 2, "到底后必须立刻停（不空转剩余轮次）"
    assert result.at_bottom is True
    assert result.reason == "bottom"
    assert result.scrolled_px == 1600


def test_lazy_scroll_plan_clamped_and_zero_rounds():
    plan = LazyScrollPlan(rounds=99, interval_ms=-5, budget_ms=10**9, step_px=-1).clamped()
    assert (plan.rounds, plan.interval_ms, plan.budget_ms, plan.step_px) == (10, 0, 30000, 0)

    page = ScrollPage([{"y": 0, "h": 800}])
    result = _run(a11y.lazy_scroll(page, LazyScrollPlan(rounds=0)))
    assert result.rounds == 0 and result.scrolled_px == 0 and result.reason == "rounds=0"
    assert page.calls == [], "rounds=0 时不得对页面做任何动作"


def test_lazy_scroll_budget_cutoff():
    page = ScrollPage([{"y": 0, "h": 9000} for _ in range(5)])
    plan = LazyScrollPlan(rounds=5, interval_ms=0, budget_ms=0, step_px=800)
    result = _run(a11y.lazy_scroll(page, plan, viewport_h=800))
    assert result.rounds == 0 and result.reason == "budget"


def test_lazy_scroll_without_evaluate_degrades():
    result = _run(a11y.lazy_scroll(BarePage(), LazyScrollPlan(rounds=3)))
    assert result.rounds == 0
    assert "evaluate" in result.reason


def test_merge_nodes_dedupes_and_keeps_first_ref():
    base = (
        A11yNode(ref=1, role="button", name="提交", box=(0.0, 0.0, 10.0, 10.0)),
        A11yNode(ref=2, role="link", name="帮助", box=(0.0, 20.0, 10.0, 10.0)),
    )
    extra = (
        A11yNode(ref=1, role="button", name="提交", box=(0.0, 0.0, 10.0, 10.0)),
        A11yNode(ref=9, role="button", name="下一页", box=(0.0, 40.0, 10.0, 10.0)),
    )
    merged = a11y.merge_nodes(base, extra)
    assert [node.name for node in merged] == ["提交", "帮助", "下一页"]
    assert [node.ref for node in merged] == [1, 2, 3], "新节点必须拿新编号，旧编号不变"


def test_lazy_observe_merges_rounds_and_notes_scrolling():
    rounds = [
        (A11yNode(ref=1, role="button", name="A", box=(0.0, 0.0, 9.0, 9.0)),),
        (A11yNode(ref=1, role="button", name="A", box=(0.0, 0.0, 9.0, 9.0)),
         A11yNode(ref=2, role="button", name="B", box=(0.0, 30.0, 9.0, 9.0))),
    ]
    page = ScrollPage([{"y": 700, "h": 3000}, {"y": 1400, "h": 3000}])
    calls = {"n": 0}

    async def fake_snapshot(_page, **_kwargs):
        idx = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        return PageSnapshot(
            url="https://example.com/",
            title="长页",
            viewport=(1280, 800),
            dpr=1.0,
            scroll_y=0,
            scroll_height=3000,
            nodes=rounds[idx],
            pruned=0,
            frames=1,
        )

    async def _no_sleep(_seconds):
        return None

    snap, scroll = _run(
        a11y.lazy_observe(
            page,
            plan=LazyScrollPlan(rounds=2, interval_ms=0, budget_ms=5000, step_px=700),
            snapshot_fn=fake_snapshot,
            sleep=_no_sleep,
        )
    )
    assert [node.name for node in snap.nodes] == ["A", "B"], "多轮节点必须合并去重"
    assert scroll.rounds == 2
    assert any("已滚动 2 屏" in note for note in snap.degraded)
    assert snap.scroll_height == 3000


# ===========================================================================
# 8. Set-of-Mark：编号一致 / 0 个 / 100+ 个 / 重叠
# ===========================================================================


def _marks_payload(evaluated):
    return [json.loads("{}") or payload for payload in ()] if False else evaluated[0][1][0]


def test_select_marks_skips_boxless_and_disabled():
    nodes = (
        A11yNode(ref=1, role="button", name="有框", box=(0.0, 0.0, 30.0, 20.0)),
        A11yNode(ref=2, role="button", name="无框", box=None),
        A11yNode(ref=3, role="button", name="禁用", box=(0.0, 30.0, 30.0, 20.0), disabled=True),
        A11yNode(ref=4, role="heading", name="标题", box=(0.0, 60.0, 30.0, 20.0)),
    )
    marks, truncated = a11y.select_marks(nodes)
    assert [mark.name for mark in marks] == ["有框"]
    assert truncated is False


def test_select_marks_zero_candidates():
    marks, truncated = a11y.select_marks(())
    assert marks == () and truncated is False
    result = _run(a11y.som_screenshot(nodes=(), evaluate_fn=None, screenshot_fn=None))
    assert result.labels == 0 and result.marks == ()
    assert "labels=0" in result.table()


def test_select_marks_caps_at_max_marks_for_100_plus_nodes():
    nodes = tuple(
        A11yNode(
            ref=i,
            role="button",
            name=f"按钮{i}",
            box=(0.0, float(i * 20), 60.0, 18.0),
        )
        for i in range(1, 121)
    )
    marks, truncated = a11y.select_marks(nodes, max_marks=60)
    assert len(marks) == 60
    assert truncated is True
    assert [mark.id for mark in marks] == list(range(1, 61))
    assert marks[0].ref == 1 and marks[-1].ref == 60


def test_select_marks_clip_filters_intersections():
    nodes = tuple(
        A11yNode(ref=i, role="button", name=f"B{i}", box=(float(i * 100), 0.0, 50.0, 20.0))
        for i in range(1, 6)
    )
    marks, _ = a11y.select_marks(nodes, clip=(100.0, 0.0, 150.0, 20.0))
    assert [mark.name for mark in marks] == ["B1", "B2"]
    assert [mark.id for mark in marks] == [1, 2], "编号在裁剪后重新从 1 起"


def test_overlapping_boxes_still_get_distinct_ids():
    box = (10.0, 10.0, 100.0, 40.0)
    nodes = (
        A11yNode(ref=1, role="button", name="上", box=box),
        A11yNode(ref=2, role="button", name="下", box=(10.0, 30.0, 100.0, 40.0)),
    )
    marks, _ = a11y.select_marks(nodes)
    assert [mark.id for mark in marks] == [1, 2]
    assert a11y.boxes_intersect(marks[0].box, marks[1].box) is True
    assert len({mark.id for mark in marks}) == 2


def test_som_screenshot_overlay_ids_match_marks_and_table():
    nodes = _pruned().nodes
    drawn: list[tuple[str, tuple]] = []
    shots: list[dict] = []

    async def evaluate(script, *args):
        drawn.append((script, args))
        return len(args[0]) if args else True

    async def screenshot(**kwargs):
        shots.append(kwargs)
        return b"\x89PNG\r\n\x1a\n" + b"7" * 8

    result = _run(
        a11y.som_screenshot(
            nodes=nodes,
            max_marks=60,
            evaluate_fn=evaluate,
            screenshot_fn=screenshot,
            viewport=(1280, 800),
            dpr=2.0,
        )
    )
    payload = drawn[0][1][0]
    assert [item["id"] for item in payload] == [mark.id for mark in result.marks]
    assert [item["id"] for item in payload] == list(range(1, result.labels + 1))
    assert drawn[0][0] == a11y.SOM_DRAW_JS
    assert shots == [{"full_page": False}]
    assert result.png.startswith(b"\x89PNG")
    assert result.resolve_mark(1).box == result.marks[0].box
    assert result.resolve_mark(999) is None
    assert result.target(1) is not None and result.target(1).name == result.marks[0].name

    table = result.table()
    assert f"labels={result.labels} image=1280x800@dpr2" in table
    assert "1. [button] 主按钮" in table
    assert '{"action":"click","target":{"mark":2}}' in table
    # overlay 必须被摘掉（否则红框污染后续所有截图）
    assert drawn[-1][0] == a11y.SOM_CLEAR_JS


def test_som_screenshot_removes_overlay_even_on_error():
    drawn: list[str] = []

    async def evaluate(script, *args):
        drawn.append(script)
        return len(args[0]) if args else True

    async def explode(**_kwargs):
        raise RuntimeError("capture failed")

    nodes = (A11yNode(ref=1, role="button", name="X", box=(0.0, 0.0, 20.0, 20.0)),)
    with pytest.raises(RuntimeError):
        _run(
            a11y.som_screenshot(
                nodes=nodes, evaluate_fn=evaluate, screenshot_fn=explode
            )
        )
    assert drawn[0] == a11y.SOM_DRAW_JS
    assert drawn[-1] == a11y.SOM_CLEAR_JS, "异常路径也要摘 overlay"


def test_som_screenshot_clip_passes_clip_to_screenshot():
    nodes = (A11yNode(ref=1, role="button", name="X", box=(100.0, 100.0, 20.0, 20.0)),)
    shots: list[dict] = []

    async def evaluate(_script, *args):
        return True

    async def screenshot(**kwargs):
        shots.append(kwargs)
        return b"png"

    _run(
        a11y.som_screenshot(
            nodes=nodes,
            clip=(90.0, 90.0, 60.0, 60.0),
            evaluate_fn=evaluate,
            screenshot_fn=screenshot,
        )
    )
    assert shots[0]["clip"] == {"x": 90.0, "y": 90.0, "width": 60.0, "height": 60.0}


def test_mark_epoch_expires_and_invalidates_on_mutating_action():
    clock = [0.0]
    epoch = MarkEpoch(ttl_s=60.0, clock=lambda: clock[0])
    node = A11yNode(ref=7, role="button", name="提交", box=(1.0, 2.0, 3.0, 4.0))
    mark = Mark(id=1, ref=7, role="button", name="提交", box=node.box)
    result = SoMResult(
        png=b"png",
        marks=(mark,),
        width=1280,
        height=800,
        targets=((1, node),),
    )
    epoch.adopt(result)
    assert epoch.active is True
    assert epoch.resolve(1) is node
    assert epoch.mark(1).box == node.box

    clock[0] = 59.0
    assert epoch.on_action("read") is False, "只读动作不作废 mark"
    assert epoch.active is True

    clock[0] = 61.0
    assert epoch.expired() is True
    assert epoch.resolve(1) is None, "60 秒后编号作废（R-A9）"

    epoch.adopt(result)
    assert epoch.active is True
    assert epoch.on_action("click") is True
    assert epoch.resolve(1) is None
    assert "改变了页面" in epoch.reason


def test_finally_removes_overlay_string_targets_dom_by_id():
    assert "getElementById" in a11y.SOM_CLEAR_JS
    assert a11y.SOM_OVERLAY_ID in a11y.SOM_DRAW_JS
    assert "aria-hidden" in a11y.SOM_DRAW_JS, "叠加层不得污染 AX 树"


# ===========================================================================
# 9. 安全与工具面门禁（不用 Pillow、不写多参 evaluate、不吐坐标）
# ===========================================================================


def test_perception_module_has_no_pillow_and_no_multiarg_evaluate():
    source = (ROOT / "core" / "a11y.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    evaluate_arity: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = None
            if name == "evaluate":
                evaluate_arity.append(len(node.args))
    assert "PIL" not in imported and "Pillow" not in imported, "禁止第三方图像库（F10）"
    assert all(arity <= 2 for arity in evaluate_arity), (
        f"Page.evaluate 只收 1 个位置实参（js + arg），实测 {evaluate_arity}"
    )
    assert evaluate_arity, "模块里应当至少有 1 处 evaluate（滚动读数）"


def test_perception_never_creates_a_browser():
    """会话必须复用 ``core.session`` 给的 page：本模块不得 launch/new_context。"""
    source = (ROOT / "core" / "a11y.py").read_text(encoding="utf-8")
    for banned in ("async_playwright", ".launch(", "new_context(", "new_browser("):
        assert banned not in source, f"感知层不得自建浏览器（{banned}）"
    assert "new_cdp_session" in source, "CDP 会话从 page.context 取（唯一合法路径）"


def test_snapshot_tags_iframe_nodes_with_frame_path():
    """iframe 内容逐帧取树后必须带帧链（executor 靠它逐级 frame_locator）。"""
    cdp = FakeCdp(_ax_handlers())
    snap = _run(a11y.snapshot(FakePage(cdp), boxes=BOXES))
    in_frame = [node for node in snap.nodes if node.frame_path == ("iframe#fr",)]
    assert [node.name for node in in_frame] == ["帧内按钮"]
    assert in_frame[0].frame_id == SUB_FRAME_ID
    main_frame_nodes = [node for node in snap.nodes if node.frame_path == ()]
    assert main_frame_nodes and all(node.frame_id == MAIN_FRAME_ID for node in main_frame_nodes)


def test_snapshot_contains_no_pixel_coordinates_in_model_text():
    snap = _run(a11y.snapshot(FakePage(FakeCdp(_ax_handlers())), boxes=BOXES))
    model_text = a11y.render_node_lines(snap.nodes, max_lines=200)
    assert "144" not in model_text and "1280" not in model_text, (
        "坐标只留在 A11yNode.box 里供画框，绝不能进模型文本"
    )


# ===========================================================================
# 10. 显示固化（core.browser 增量）：无头语义 / 视口 DPR / locale / 字体
# ===========================================================================


def test_launch_plan_new_headless_uses_chromium_channel():
    plan = resolve_launch_plan("new", engine="chromium")
    assert plan.mode == "new" and plan.headless is True
    assert plan.channel == "chromium", "新版无头是 channel 构建，不是 headless shell"
    assert "--disable-background-networking" in plan.args
    assert "--no-sandbox" in plan.args

    # integrator 直接 ``engine.launch(**plan.launch_kwargs(proxy=...))``
    kwargs = plan.launch_kwargs(proxy="http://127.0.0.1:7890")
    assert kwargs["headless"] is True and kwargs["channel"] == "chromium"
    assert kwargs["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert isinstance(kwargs["args"], list)


def test_launch_plan_shell_drops_channel_and_firefox_has_none():
    shell = resolve_launch_plan("shell", engine="chromium")
    assert shell.headless is True and shell.channel is None
    firefox = resolve_launch_plan("new", engine="firefox")
    assert firefox.channel is None, "firefox/webkit 不接受 channel"


def test_launch_plan_headful_requires_display_then_falls_back():
    with_display = resolve_launch_plan("headful", env={"DISPLAY": ":99"})
    assert with_display.mode == "headful" and with_display.headless is False
    assert with_display.notes == ()

    without = resolve_launch_plan("headful", env={})
    assert without.requested == "headful"
    assert without.mode == "new" and without.headless is True, "无 DISPLAY 必须回落而不是静默失败"
    assert any("DISPLAY" in note and "回落" in note for note in without.notes)


def test_validate_headless_mode_rejects_unknown_with_choices():
    assert validate_headless_mode("NEW") == "new"
    assert validate_headless_mode("headless") == "shell"
    with pytest.raises(ValueError) as excinfo:
        validate_headless_mode("xvfb")
    assert "new/shell/headful" in str(excinfo.value)


def test_context_options_pin_viewport_dpr_and_locale():
    options = context_options(width=1024, height=768, device_scale_factor=2.0)
    assert options["viewport"] == {"width": 1024, "height": 768}
    assert options["screen"] == {"width": 1024, "height": 768}
    assert options["device_scale_factor"] == 2.0
    assert options["locale"] == "zh-CN"
    assert context_options(device_scale_factor=99)["device_scale_factor"] == 3.0


def test_font_helpers_carry_cjk_stack_and_package_hint():
    assert "Noto Sans CJK SC" in browser_mod.font_stack()
    css = browser_mod.font_css()
    assert "Noto Sans CJK SC" in css and "!important" not in css, (
        "不覆盖站点图标字体（PUA 字符会变方框）"
    )
    script = browser_mod.font_init_script()
    assert "Noto Sans CJK SC" in script and "__blm_font_fallback__" in script

    ok, hint = check_cjk_fonts("")
    assert ok is False and "fonts-noto-cjk" in hint
    ok, hint = check_cjk_fonts("Noto Sans CJK SC: /usr/share/fonts/opentype/noto.otf")
    assert ok is True and hint == ""


def test_probe_cjk_fonts_is_injectable_and_cached(monkeypatch):
    calls = {"n": 0}

    def runner():
        calls["n"] += 1
        return "WenQuanYi Micro Hei"

    monkeypatch.setattr(browser_mod, "_CJK_FONT_CACHE", {})
    ok, hint = browser_mod.probe_cjk_fonts(runner)
    assert (ok, hint) == (True, "")
    browser_mod.probe_cjk_fonts(runner)
    assert calls["n"] == 1, "fc-list 每进程只跑一次"

    monkeypatch.setattr(browser_mod, "_CJK_FONT_CACHE", {})
    ok, hint = browser_mod.probe_cjk_fonts(lambda: "")
    assert ok is False and "fonts-noto-cjk" in hint


class _BrowserFakePage:
    def __init__(self):
        self.closed = False
        self.timeout = None

    def set_default_timeout(self, ms):
        self.timeout = ms

    async def close(self):
        self.closed = True

    async def screenshot(self, **kwargs):
        return b"png"


class _BrowserFakeContext:
    def __init__(self):
        self.closed = False
        self.kwargs = None
        self.init_scripts: list[str] = []
        self.routes: list[tuple[str, object]] = []
        self.page = _BrowserFakePage()

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class _BrowserFakeBrowser:
    def __init__(self, context):
        self.context = context

    def is_connected(self):
        return True

    async def new_context(self, **kwargs):
        self.context.kwargs = kwargs
        return self.context

    async def close(self):
        return None


def _core_with_fakes(config=None, *, context=None):
    cfg = {
        "resources": {
            "engine": "chromium",
            "headless": "shell",
            "viewport": {"width": 1024, "height": 768, "device_scale_factor": 2},
            "action_timeout_s": 7,
            "proxy": "",
        },
        "perception": {"screenshot_fallback": False},
        "security": {"block_internal_ip": True},
    }
    if config:
        cfg.update(config)
    ctx = context or _BrowserFakeContext()
    core = BrowserCore({**cfg, "data_dir": str(Path.cwd() / "data")})
    core._browser = _BrowserFakeBrowser(ctx)
    core._playwright = object()
    return core, ctx


def test_browser_core_reads_v6_display_keys():
    core, _ctx = _core_with_fakes()
    assert core.browser_type == "chromium"
    assert core.headless_mode == "shell"
    assert core.viewport == {"width": 1024, "height": 768, "device_scale_factor": 2.0}
    assert core.timeout == 7
    assert core.enable_screenshot is False
    assert core.locale == "zh-CN"

    plan = core.launch_plan()
    assert plan.headless is True and plan.channel is None

    kwargs = core.context_kwargs()
    assert kwargs["viewport"] == {"width": 1024, "height": 768}
    assert kwargs["device_scale_factor"] == 2.0
    assert kwargs["locale"] == "zh-CN"


def test_browser_new_page_applies_display_freeze_and_counts_ssrf():
    core, ctx = _core_with_fakes()
    page = _run(core.new_page())
    assert ctx.kwargs["viewport"] == {"width": 1024, "height": 768}
    assert ctx.kwargs["screen"] == {"width": 1024, "height": 768}
    assert ctx.kwargs["device_scale_factor"] == 2.0
    assert ctx.kwargs["locale"] == "zh-CN"
    assert page.timeout == 7000
    assert any("Noto Sans CJK SC" in script for script in ctx.init_scripts), (
        "字体栈必须随 context 注入（显示固化）"
    )

    handler = ctx.routes[0][1]

    class _Req:
        def __init__(self, url):
            self.request = type("R", (), {"url": url})()
            self.aborted = False

        async def abort(self, reason):
            self.aborted = True

        async def continue_(self):
            return None

    route = _Req("http://127.0.0.1/admin")
    _run(handler(route))
    assert route.aborted is True
    assert core.ssrf_blocked(page) == 1, "D9-①：拦截次数要能被感知层取到"
    _run(handler(_Req("http://127.0.0.1/again")))
    assert core.ssrf_blocked(page) == 2


def test_browser_core_rejects_invalid_headless_mode():
    with pytest.raises(ValueError):
        BrowserCore(
            {
                "resources": {"headless": "xvfb"},
                "data_dir": str(Path.cwd() / "data"),
            }
        )


def test_browser_launch_plan_is_pure_and_side_effect_free():
    """``resolve_launch_plan`` 不改全局状态（可安全用于日志/探针）。"""
    before = dict(os.environ)
    resolve_launch_plan("headful", env={})
    assert dict(os.environ) == before
