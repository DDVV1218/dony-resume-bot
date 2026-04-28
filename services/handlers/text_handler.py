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
        """流式回复：后台线程更新卡片，不阻塞 LLM streaming

        Returns:
            None 表示卡片已成功更新
            字符串表示流式失败、已回退为一次性回复
        """
        from feishu.messages import build_card, send_card, update_card
        import threading
        import time

        # 发送初始卡片
        msg_id = send_card(conversation_id, build_card("", is_final=False), self.config)
        if not msg_id:
            logger.warning("send_card failed, falling back to non-streaming")
            return chat(context, self.config)

        # 用可变对象在线程间共享累加文本
        full_reply: list[str] = [""]
        stop_event = threading.Event()
        # 最小更新间隔（太快会触发飞书 API 限流）
        min_interval = self.config.feishu_streaming_interval
        # 记录最后一次成功发送的内容长度，避免重复发送相同内容
        last_sent_len = 0

        def card_updater():
            """后台线程：有足够新内容时立即更新卡片"""
            nonlocal last_sent_len
            last_update = 0.0
            while not stop_event.is_set():
                current = full_reply[0]
                now = time.time()
                elapsed = now - last_update
                if elapsed >= min_interval and len(current) > last_sent_len:
                    update_card(msg_id, build_card(current, is_final=False), self.config)
                    last_sent_len = len(current)
                    last_update = now
                stop_event.wait(0.05)  # 每 50ms 检查一次

        # 启动后台线程
        updater = threading.Thread(target=card_updater, daemon=True)
        updater.start()

        # 主线程：持续累积 chunks
        stream = chat_stream(context, self.config)
        for chunk in stream:
            full_reply[0] += chunk

        # 停止更新线程，执行最终更新
        stop_event.set()
        updater.join(timeout=5)
        final_card_ok = update_card(msg_id, build_card(full_reply[0], is_final=True), self.config)
        logger.info(f"Streaming done ({len(full_reply[0])} chars), final_update_ok={final_card_ok}")

        return None
