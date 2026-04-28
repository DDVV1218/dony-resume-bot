"""简历处理器桩 - 预留给 Phase 2

Phase 2 实现简历解析时，将 can_handle() 改为返回 True 即可启用。
"""

import logging
from typing import Optional

from feishu.models import InboundMessage
from services.handlers.base import BaseMessageHandler

logger = logging.getLogger(__name__)


class ResumePDFHandler(BaseMessageHandler):
    """简历 PDF 处理器（桩）

    处理 PDF 文件的简历上传。
    Phase 2 实现时设置 can_handle() 返回 True。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        # Phase 2 时改为：return inbound.message_type == "file" and is_pdf(inbound)
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        # Phase 2 实现
        return None


class ResumeImageHandler(BaseMessageHandler):
    """简历图片处理器（桩）

    处理 PNG/JPG 等图片格式的简历上传。
    Phase 2 实现时设置 can_handle() 返回 True。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        # Phase 2 时改为：return inbound.message_type in ("image", "sticker")
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        # Phase 2 实现
        return None
