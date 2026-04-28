from __future__ import annotations

"""简历处理器桩 - 预留给 Phase 2

Phase 2 实现简历解析时，将 can_handle() 改为返回 True 即可启用。
"""

import logging
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)


class ResumePDFHandler(BaseMessageHandler):
    """简历 PDF 处理器（桩）

    处理 PDF 文件的简历上传。
    Phase 2 实现时设置 can_handle() 返回 True。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return None


class ResumeImageHandler(BaseMessageHandler):
    """简历图片处理器（桩）

    处理 PNG/JPG 等图片格式的简历上传。
    Phase 2 实现时设置 can_handle() 返回 True。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return None
