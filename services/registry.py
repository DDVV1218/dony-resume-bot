"""命令注册表 - 声明式命令路由

将命令路由从 if/else 链改为可配置的注册表模式。
添加新命令只需：
1. 在 config.py 的 commands dict 中添加一条
2. 写一个 handler 函数并用 @registry.command() 注册

参考 OpenClaw 的 bindings 声明式配置体系。
"""

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CommandRegistry:
    """命令注册表

    管理命令名到 handler 函数的映射。
    支持装饰器注册和显式注册两种方式。
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    def register(self, name: str, handler: Callable) -> Callable:
        """注册一个命令 handler

        Args:
            name: 命令名（不含 / 前缀）
            handler: 处理函数

        Returns:
            handler 本身（支持装饰器使用）
        """
        self._handlers[name] = handler
        logger.debug(f"Registered command: /{name}")
        return handler

    def command(self, name: str) -> Callable:
        """装饰器：注册命令 handler

        Usage:
            @registry.command("status")
            def handle_status(...):
                ...
        """
        def decorator(func: Callable) -> Callable:
            self._handlers[name] = func
            return func
        return decorator

    def resolve(self, name: str) -> Optional[Callable]:
        """查找命令对应的 handler

        Args:
            name: 命令名（不含 / 前缀）

        Returns:
            handler 函数，或 None
        """
        return self._handlers.get(name)

    @property
    def commands(self) -> Dict[str, str]:
        """返回所有已注册的命令名列表（用于帮助信息）"""
        return dict(self._handlers)


# 全局单例
_registry: Optional[CommandRegistry] = None


def get_registry() -> CommandRegistry:
    """获取全局 CommandRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = CommandRegistry()
    return _registry
