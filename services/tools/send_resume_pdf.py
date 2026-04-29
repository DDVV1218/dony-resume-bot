"""发送简历 PDF 工具

根据用户提供的姓名或电话，从归档中找到 PDF 并通过飞书发送。
"""

import json
import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SendResumePDFParams(BaseModel):
    """发送简历 PDF 参数"""
    name: str = ""
    phone: str = ""


class SendResumePDFTool(BaseTool):
    """发送简历 PDF 工具

    根据姓名查找简历库中的 PDF 文件，通过飞书消息发送给用户。
    支持按姓名或电话查找。
    """

    name: str = "send_resume_pdf"
    description: str = (
        "根据用户需求，将简历的 PDF 文件发送到当前对话中。"
        "用户会说'把x的简历发给我''发送简历''给我看看'等。"
        "需要提供姓名，也可以提供电话。"
    )
    parameters = SendResumePDFParams

    def __init__(self, app_id: str = "", app_secret: str = "", conversation_id: str = ""):
        super().__init__()
        self._app_id = app_id
        self._app_secret = app_secret
        self._conversation_id = conversation_id

    def _execute(self, name: str = "", phone: str = "") -> ToolResult:
        """执行发送简历 PDF

        Args:
            name: 候选人姓名
            phone: 候选人电话（可选）

        Returns:
            ToolResult
        """
        if not name and not phone:
            return ToolResult(
                success=False,
                error="请提供姓名或电话来查找简历",
            )

        # 1. 从数据库查找简历
        try:
            from services.db import get_connection
            conn = get_connection()

            if phone:
                row = conn.execute(
                    "SELECT id, name, pdf_path FROM resumes WHERE phone = ? LIMIT 1",
                    [phone],
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, name, pdf_path FROM resumes WHERE name = ? LIMIT 1",
                    [name],
                ).fetchone()

            if not row:
                return ToolResult(
                    success=True,
                    data={"message": f"未找到姓名为「{name}」的简历，请确认姓名是否正确"},
                )

            found_name = row["name"]
            pdf_path = row["pdf_path"]
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"数据库查询失败: {e}",
            )

        if not pdf_path:
            return ToolResult(
                success=True,
                data={"message": f"「{found_name}」的简历没有 PDF 文件记录"},
            )

        # 2. 检查文件是否存在
        import os as os_mod
        if not os_mod.path.exists(pdf_path):
            return ToolResult(
                success=True,
                data={"message": f"「{found_name}」的 PDF 文件已不存在（路径：{pdf_path}）"},
            )

        # 3. 上传并发送到飞书
        if not self._app_id or not self._app_secret or not self._conversation_id:
            return ToolResult(
                success=False,
                error="发送功能未配置（缺少飞书凭证或会话信息）",
            )

        try:
            from feishu.file_utils import upload_and_send_file
            ok = upload_and_send_file(
                file_path=pdf_path,
                conversation_id=self._conversation_id,
                app_id=self._app_id,
                app_secret=self._app_secret,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"发送 PDF 失败: {e}",
            )

        if ok:
            return ToolResult(
                success=True,
                data={"message": f"已发送「{found_name}」的简历 PDF 到当前对话"},
            )
        else:
            return ToolResult(
                success=True,
                data={"message": f"发送「{found_name}」的简历 PDF 失败，请稍后重试"},
            )
