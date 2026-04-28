from __future__ import annotations

"""不支持消息类型的兜底处理器"""

import logging
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)


class UnsupportedHandler(BaseMessageHandler):
    """兜底处理器

    处理所有其他处理器不处理的消息类型。
    返回"暂不支持"提示。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        # 兜底处理器：处理所有非 text 消息（text 消息由 TextHandler 处理）
        return inbound.message_type != "text"

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return "⚠️ 暂不支持该类型消息，请发送文字消息。"
