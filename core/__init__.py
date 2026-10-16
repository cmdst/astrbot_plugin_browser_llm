# core 子包：LLM 浏览器插件核心模块（浏览器驱动 / 会话管理 / 内容提取 / 安全过滤）
#
# v1.3.3：配置解析容错函数收敛至此（原先 main.py 的 _as_* 与
# core/browser.py 的 _safe_* 为同构双实现，双命名易漂移）。

import logging

logger = logging.getLogger(__name__)


def as_int(value, default: int) -> int:
    """int 配置解析：非法值回退默认并记 warning，不阻断插件加载。

    None/空串按默认处理（vision_cache_ttl 的「空=0」语义由调用方
    先行 `or 0` 归一，此处只管类型转换容错）。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("配置项数值非法（%r），已回退默认值 %s", value, default)
        return default


def as_float(value, default: float) -> float:
    """float 配置解析：非法值回退默认并记 warning，不阻断插件加载。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("配置项数值非法（%r），已回退默认值 %s", value, default)
        return default


def as_bool(value, default: bool) -> bool:
    """bool 配置解析：字符串 "false"/"0"/"off"/"no"/"" → False（语义归一）。

    JSON 允许 "false" 作为字符串，直接 bool("false") 会得到 True 造成
    语义反转；此处归一化字符串布尔值，其余值按 Python 真值语义。
    None 回退默认。
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "off", "no")
    if value is None:
        return default
    return bool(value)
