"""文字消息处理器 - LLM 对话

将原 _process_message 中的 LLM 对话逻辑迁移到此处理器。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage
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
        conversation_id = inbound.conversation_id
        card = None

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

            # 立刻发出思考中卡片
            from feishu.streaming_card import FeishuStreamingCard
            card = FeishuStreamingCard(self.config.feishu_app_id, self.config.feishu_app_secret)
            if card.start(conversation_id):
                logger.info("Thinking card shown")
            else:
                card = None
                logger.warning("Failed to show thinking card, proceeding without card")

            # prepare context
            context = prepare_context(session.messages, self.config)

            # 调用 LLM（非流式）
            reply = chat(context, self.config)
            logger.info(f"LLM replied ({len(reply)} chars)")

            # 追加助手回复并保存
            if reply:
                session.messages.append({"role": "assistant", "content": reply})
                session.updated_at = datetime.now().isoformat()
                self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 更新/发送最终回复
            if reply:
                if card and card.is_active():
                    card.close(reply)
                    return None  # 卡片已更新
                # 卡片失效，直接发文字
                from feishu.messages import send_text
                send_text(conversation_id, reply, self.config)
                return None

            return reply

        except Exception as e:
            logger.error(f"LLM chat failed for {session_key}: {e}")
            # 如果有卡片，更新为错误信息
            if card and card.is_active():
                card.close(f"⚠️ 处理失败：{e}")
            return None


