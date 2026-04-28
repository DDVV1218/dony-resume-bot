"""文字消息处理器 - LLM 对话

将原 _process_message 中的 LLM 对话逻辑迁移到此处理器。
"""

import logging
from datetime import datetime
from typing import Optional

from feishu.models import InboundMessage
from services.handlers.base import BaseMessageHandler
from services.llm import prepare_context, chat

logger = logging.getLogger(__name__)


class TextHandler(BaseMessageHandler):
    """文字消息处理器

    处理 text 类型的消息，调用 LLM 生成回复。
    包括 system prompt 修复、消息追加、context prepare、LLM 调用。
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        return inbound.message_type == "text" and inbound.text is not None

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        session_key = inbound.session_key
        text = inbound.text

        try:
            session = self.session_store.get_or_create(session_key)
            logger.info(f"Session loaded, {len(session.messages)} messages")

            # 确保 system message 在第一条且唯一
            system_msg = {"role": "system", "content": self.system_prompt}
            session.messages = [m for m in session.messages if m.get("role") != "system"]
            session.messages.insert(0, system_msg)
            self.session_store._save_session(
                self.session_store._user_dir(session_key), session
            )

            # 追加用户消息
            session.messages.append({"role": "user", "content": text})
            session.updated_at = datetime.now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)
            logger.info(f"User message appended, {len(session.messages)} total")

            # prepare context + LLM call
            context = prepare_context(session.messages, self.config)
            reply = chat(context, self.config)
            logger.info(f"LLM replied ({len(reply)} chars)")

            # 追加助手回复并保存
            session.messages.append({"role": "assistant", "content": reply})
            session.updated_at = datetime.now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)

            return reply

        except Exception as e:
            logger.error(f"LLM chat failed for {session_key}: {e}")
            return None  # 让调用方处理错误
