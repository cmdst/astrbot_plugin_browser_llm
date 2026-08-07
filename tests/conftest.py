"""pytest 共享夹具：插件根目录入 sys.path + 最小 astrbot 桩。

沙箱无 astrbot 包；main.py 顶层 import astrbot API，测试需注入最小桩
（仅覆盖本插件用到的符号：Star/Context/AstrBotConfig/filter/
AstrMessageEvent/ProviderRequest/TextPart）。桩只保证 import 与装饰器
可用，不模拟真实框架行为。core/*.py 为纯 Python 不依赖 astrbot。
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return  # 真实包可用时不要覆盖

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    star = types.ModuleType("astrbot.api.star")
    event = types.ModuleType("astrbot.api.event")
    provider = types.ModuleType("astrbot.api.provider")
    core = types.ModuleType("astrbot.core")
    core_agent = types.ModuleType("astrbot.core.agent")
    core_agent_message = types.ModuleType("astrbot.core.agent.message")
    core_agent_tool = types.ModuleType("astrbot.core.agent.tool")
    core_provider = types.ModuleType("astrbot.core.provider")
    core_provider_register = types.ModuleType("astrbot.core.provider.register")
    astrbot.api = api
    api.star = star
    api.event = event
    api.provider = provider
    astrbot.core = core
    core.agent = core_agent
    core_agent.message = core_agent_message
    core_agent.tool = core_agent_tool
    core.provider = core_provider
    core_provider.register = core_provider_register

    class AstrBotConfig(dict):
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class AstrMessageEvent:
        """最小事件桩：llm_tool 测试可能用到的方法。"""

        unified_msg_origin = ""

        def get_group_id(self):
            return ""

        def get_sender_id(self):
            return ""

        def image_result(self, path):
            return f"image:{path}"

        def plain_result(self, text):
            return f"plain:{text}"

        def chain_result(self, chain):
            return f"chain:{chain}"

        async def send(self, msg):
            self._sent = msg
            return None

    class ProviderRequest:
        def __init__(self):
            self.system_prompt = ""
            self.extra_user_content_parts = []
            self.contexts = []

    class TextPart:
        """最小桩：仅支持 text 与 mark_as_temp。"""

        def __init__(self, text=""):
            self.text = text
            self.is_temp = False

        def mark_as_temp(self):
            self.is_temp = True
            return self

    class FunctionTool:
        """最小桩：记录构造参数，call 抛 NotImplementedError（由 handler 走）。"""

        def __init__(self, name="", description="", parameters=None, handler=None, **kw):
            self.name = name
            self.description = description
            self.parameters = parameters or {}
            self.handler = handler
            self.active = True

        async def call(self, context, **kwargs):
            if self.handler:
                event = getattr(getattr(context, "context", None), "event", None)
                return await self.handler(event, **kwargs)
            raise NotImplementedError("FunctionTool.call() stub")

    class ToolSet:
        """最小桩：持有 tools 列表。"""

        def __init__(self, tools=None):
            self.tools = list(tools or [])

        def empty(self):
            return not self.tools

        def add_tool(self, tool):
            self.tools.append(tool)

    class Agent:
        """register_agent 创建的 Agent 桩。"""

        def __init__(self, name="", instructions="", tools=None, run_hooks=None):
            self.name = name
            self.instructions = instructions
            self.tools = list(tools or [])

    class RegisteringAgent:
        """register_agent 装饰器返回值（替换被装饰方法名）。"""

        def __init__(self, agent):
            self._agent = agent

    class _AgentRegistry:
        """register_agent 装饰器桩：构造 Agent + 模拟 HandoffTool 注册。"""

        def __init__(self):
            self.registered = []

        def __call__(self, name, instruction="", tools=None, run_hooks=None):
            def decorator(awaitable):
                agent = Agent(name=name, instructions=instruction, tools=tools or [])
                self.registered.append(agent)
                return RegisteringAgent(agent)

            return decorator

    agent_registry = _AgentRegistry()
    func_list = []  # llm_tools.func_list：register_agent 桩把 HandoffTool 加进来

    class _FuncCall:
        def __init__(self):
            self.func_list = func_list

        def get_func(self, name):
            for f in reversed(self.func_list):
                if getattr(f, "name", None) == name:
                    return f
            return None

    class _Filter:
        """装饰器桩：llm_tool / on_llm_request 原样返回函数并记录元数据。"""

        @staticmethod
        def llm_tool(*args, **kwargs):
            def deco(fn):
                fn._llm_tool = kwargs
                return fn

            return deco

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def deco(fn):
                fn._on_llm_request = True
                return fn

            return deco

    star.Context = Context
    star.Star = Star
    api.AstrBotConfig = AstrBotConfig
    api.agent = agent_registry  # from astrbot.api import agent
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = _Filter()
    provider.ProviderRequest = ProviderRequest
    core_agent_message.TextPart = TextPart
    core_agent_tool.FunctionTool = FunctionTool
    core_agent_tool.ToolSet = ToolSet
    core_provider_register.llm_tools = _FuncCall()

    # 注册到 sys.modules：让 main.py 的 from astrbot.xxx import ... 可用。
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.provider"] = provider
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.agent"] = core_agent
    sys.modules["astrbot.core.agent.message"] = core_agent_message
    sys.modules["astrbot.core.agent.tool"] = core_agent_tool
    sys.modules["astrbot.core.provider"] = core_provider
    sys.modules["astrbot.core.provider.register"] = core_provider_register


_install_astrbot_stub()


def _install_package_alias() -> None:
    """把插件目录伪装成包，让 main.py 的相对导入可用，并统一模块实例。

    main.py 使用 ``from .core.browser import ...`` 相对导入；若测试顶层
    导入 main/core，会产生两份模块实例（monkeypatch 会打偏）。做法：
    以包名 ``astrbot_plugin_browser_llm`` 注册目录，预加载包内模块，
    再同时注册为顶层模块名，保证测试与插件代码引用同一实例。
    """
    import importlib

    pkg = types.ModuleType("astrbot_plugin_browser_llm")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["astrbot_plugin_browser_llm"] = pkg

    core_pkg = importlib.import_module("astrbot_plugin_browser_llm.core")
    sys.modules["core"] = core_pkg  # 顶层别名：测试 from core.xxx 与插件同一实例
    main = importlib.import_module("astrbot_plugin_browser_llm.main")
    sys.modules["main"] = main


_install_package_alias()
