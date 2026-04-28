"""文字消息处理器 - LLM 对话

将原 _process_message 中的 LLM 对话逻辑迁移到此处理器。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Generator, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage
from services.llm import prepare_context, chat, chat_stream

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

            # prepare context
            context = prepare_context(session.messages, self.config)

            # 流式回复或一次性回复
            if self.config.feishu_streaming:
                reply = self._handle_streaming(conversation_id, context)
                streaming = True
            else:
                reply = chat(context, self.config)
                streaming = False
                logger.info(f"LLM replied ({len(reply)} chars)")

            # 追加助手回复并保存
            if reply:
                session.messages.append({"role": "assistant", "content": reply})
                session.updated_at = datetime.now().isoformat()
                self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 流式成功（卡片已更新）时返回 None，否则返回文本让调用方发送
            if streaming and reply is None:
                return None  # 卡片已显示
            return reply  # 文本回复或流式回退

        except Exception as e:
            logger.error(f"LLM chat failed for {session_key}: {e}")
            return None  # 让调用方处理错误

    def _handle_streaming(self, conversation_id: str, context: list) -> Optional[str]:
        """流式回复：通过 interactive card 逐 chunk 更新

        Returns:
            None 表示卡片已成功更新（调用方无需再发文字消息）
            字符串表示流式失败、已回退为一次性回复（调用方需发文字消息）
        """
        from feishu.messages import build_card, send_card, update_card

        # 发送初始卡片
        msg_id = send_card(conversation_id, build_card("", is_final=False), self.config)
        if not msg_id:
            logger.warning("send_card failed, falling back to non-streaming")
            return chat(context, self.config)

        full_reply = ""
        last_update = time.time()
        interval = self.config.feishu_streaming_interval
        cards_failed = 0
        streaming_ok = True

        # 流式生成
        stream = chat_stream(context, self.config)
        for chunk in stream:
            full_reply += chunk
            now = time.time()
            if now - last_update >= interval:
                ok = update_card(msg_id, build_card(full_reply, is_final=False), self.config)
                if not ok:
                    cards_failed += 1
                    if cards_failed >= 3:
                        logger.warning("Too many card update failures, falling back to text")
                        streaming_ok = False
                        break
                last_update = now

        if streaming_ok:
            # 最终更新
            update_card(msg_id, build_card(full_reply, is_final=True), self.config)
            logger.info(f"Streaming done ({len(full_reply)} chars)")
            return None  # 卡片已显示

        # 流式失败，退回文字消息
        logger.info(f"Falling back to text reply ({len(full_reply)} chars)")
        return full_reply
