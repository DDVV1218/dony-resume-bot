"""消息类型处理器包

预置处理器：
- TextHandler: 处理文字消息的 LLM 对话
- UnsupportedHandler: 兜底处理
- ResumePDFHandler / ResumeImageHandler: 预留给 Phase 2
"""

from services.handlers.base import BaseMessageHandler
from services.handlers.text_handler import TextHandler
from services.handlers.unsupported_handler import UnsupportedHandler
from services.handlers.resume_handler import ResumePDFHandler, ResumeImageHandler

__all__ = [
    "BaseMessageHandler",
    "TextHandler",
    "UnsupportedHandler",
    "ResumePDFHandler",
    "ResumeImageHandler",
]
