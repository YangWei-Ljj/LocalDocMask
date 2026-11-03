"""格式处理层门面：重新导出注册接口。"""
from .registry import handler_for, supported_extensions, SUPPORTED, _EXT_MAP

__all__ = ["handler_for", "supported_extensions", "SUPPORTED", "_EXT_MAP"]
