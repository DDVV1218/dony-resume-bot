from __future__ import annotations

"""简历文件处理器

接收并处理用户上传的 PDF 简历文件。
- Phase 1.5: 下载并保存文件，回复确认
- Phase 2: 解析简历内容 + 向量化存储
"""

import logging
import os
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)


class ResumePDFHandler(BaseMessageHandler):
    """简历 PDF 处理器

    接收用户发送的 PDF 文件，保存到 uploads 目录。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        # Phase 1.5: 处理所有 file 类型的消息（后续可根据文件后缀区分）
        return inbound.message_type == "file"

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        if not inbound.file_key:
            logger.warning(f"No file_key in file message: {inbound.message_id}")
            return "⚠️ 无法识别文件内容，请重新发送"

        # 下载文件
        save_dir = os.path.join(self.config.uploads_dir, inbound.sender_id or "unknown")
        from feishu.file_utils import download_file
        save_path = download_file(
            inbound.file_key,
            self.config.feishu_app_id,
            self.config.feishu_app_secret,
            save_dir,
        )

        if not save_path:
            logger.error(f"Failed to download file: {inbound.file_key}")
            return "⚠️ 文件下载失败，请稍后重试"

        file_name = inbound.file_name or os.path.basename(save_path)
        file_size = os.path.getsize(save_path)
        size_str = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"

        # Phase 2 时将在此处调用简历解析
        logger.info(f"PDF saved: {save_path} ({size_str})")

        reply = (
            f"✅ 已收到简历文件「{file_name}」（{size_str}）\n\n"
            f"📌 正在分析中，请稍候...\n"
            f"（简历解析功能将在 Phase 2 上线）"
        )
        return reply


class ResumeImageHandler(BaseMessageHandler):
    """简历图片处理器（桩）

    处理 PNG/JPG 等图片格式的简历上传。
    Phase 2 实现时设置 can_handle() 返回 True。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        # Phase 2: 处理 media 或 image 类型的消息
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return None
