"""消息类型处理器基类

定义处理器接口，所有消息类型处理器必须继承 BaseMessageHandler。
添加新消息类型只需：
  1. 创建一个继承 BaseMessageHandler 的类
  2. 实现 can_handle() 和 handle() 方法
  3. 在 bot.py 的 handler 链中注册
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

from config import Config
from services.session import SessionStore

if TYPE_CHECKING:
    from feishu.models import InboundMessage


class BaseMessageHandler(ABC):
    """消息类型处理器基类"""

    def __init__(self, config: Config, session_store: SessionStore, system_prompt: str):
        self.config = config
        self.session_store = session_store
        self.system_prompt = system_prompt

    @abstractmethod
    def can_handle(self, inbound: InboundMessage) -> bool:
        """判断此处理器是否能处理该消息

        Args:
            inbound: 规范化入站消息

        Returns:
            True 可以处理，False 跳过
        """
        ...

    @abstractmethod
    def handle(self, inbound: InboundMessage) -> Optional[str]:
        """处理消息

        Args:
            inbound: 规范化入站消息

        Returns:
            回复文本，None 表示不回复
        """
        ...

    def __str__(self) -> str:
        return self.__class__.__name__
