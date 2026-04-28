"""飞书流式卡片 - 基于 Card Kit API

参考 OpenClaw 的 FeishuStreamingSession 实现，使用飞书 Card Kit API
实现 50ms 级别的高频流式更新，替代旧的 im.message.patch 方案（~500ms）。
"""

import json
import logging
import threading
import time
import uuid as uuid_mod
from typing import Optional

from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)

from config import Config
from feishu.messages import _get_client

logger = logging.getLogger(__name__)


class FeishuStreamingCard:
    """飞书流式卡片

    使用 Card Kit API 实现高频流式文字更新。

    使用方式：
        card = FeishuStreamingCard(config)
        card.start(conversation_id)

        # 在主线程中不断调用 update()
        card.update("text...")

        # 完成后关闭
        card.close()
    """

    def __init__(self, config: Config):
        self.config = config
        self.card_id: Optional[str] = None
        self.message_id: Optional[str] = None
        self._sequence = 0
        self._current_text = ""
        self._closed = False
        self._queue = threading.Event()
        self._queue_lock = threading.Lock()
        # 限流：最小更新间隔
        self._min_interval = 0.1  # 100ms
        self._last_update_time = 0.0

    def start(self, conversation_id: str) -> bool:
        """启动流式卡片

        Args:
            conversation_id: 会话 ID（open_id / chat_id）

        Returns:
            True 成功，False 失败
        """
        client = _get_client(self.config)

        # 1. 创建卡片实体（带 streaming_mode）
        card_json = {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "streaming_config": {
                    "print_frequency_ms": {"default": 50},
                    "print_step": {"default": 1},
                },
            },
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 AI 回复中..."},
                "template": "blue",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "⏳ 正在思考...", "element_id": "content"},
                ],
            },
        }

        create_req = CreateCardRequest.builder() \
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card_json))
                .build()
            ) \
            .build()

        create_resp = client.cardkit.v1.card.create(create_req)
        if create_resp.code != 0 or not create_resp.data or not create_resp.data.card_id:
            logger.error(f"Create streaming card failed: code={create_resp.code}, msg={create_resp.msg}")
            return False

        self.card_id = create_resp.data.card_id
        logger.debug(f"Streaming card created: card_id={self.card_id}")

        # 2. 发送卡片到会话
        card_content = json.dumps({"type": "card", "data": {"card_id": self.card_id}})

        if conversation_id.startswith("ou_"):
            receive_id_type = "open_id"
        elif conversation_id.startswith("oc_"):
            receive_id_type = "chat_id"
        else:
            receive_id_type = "open_id"

        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        msg_req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(conversation_id)
                .msg_type("interactive")
                .content(card_content)
                .build()
            ) \
            .build()

        msg_resp = client.im.v1.message.create(msg_req)
        if msg_resp.code != 0 or not msg_resp.data or not msg_resp.data.message_id:
            logger.error(f"Send streaming card message failed: code={msg_resp.code}")
            return False

        self.message_id = msg_resp.data.message_id
        logger.info(f"Streaming card sent: card_id={self.card_id}, message_id={self.message_id}")
        return True

    def update(self, text: str) -> None:
        """更新卡片内容（高频调用安全，内部限流）

        Args:
            text: 新的文字内容
        """
        if self._closed or not self.card_id:
            return

        # 限流：跳过太频繁的更新
        now = time.time()
        if now - self._last_update_time < self._min_interval:
            return
        self._last_update_time = now

        self._sequence += 1
        seq = self._sequence
        uuid_str = f"s_{self.card_id}_{seq}"
        client = _get_client(self.config)

        req = ContentCardElementRequest.builder() \
            .card_id(self.card_id) \
            .element_id("content") \
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(text)
                .sequence(seq)
                .uuid(uuid_str)
                .build()
            ) \
            .build()

        try:
            resp = client.cardkit.v1.card.content(req)
            if resp.code != 0:
                logger.warning(f"Content update failed (seq={seq}): code={resp.code}")
        except Exception as e:
            logger.warning(f"Content update exception (seq={seq}): {e}")

        self._current_text = text

    def close(self, final_text: Optional[str] = None) -> None:
        """关闭流式模式

        Args:
            final_text: 最终的完整文本
        """
        if self._closed or not self.card_id:
            return
        self._closed = True

        text = final_text or self._current_text

        # 最终内容更新
        if text and text != self._current_text:
            self._sequence += 1
            client = _get_client(self.config)
            req = ContentCardElementRequest.builder() \
                .card_id(self.card_id) \
                .element_id("content") \
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(text)
                    .sequence(self._sequence)
                    .uuid(f"s_{self.card_id}_{self._sequence}")
                    .build()
                ) \
                .build()
            try:
                client.cardkit.v1.card.content(req)
            except Exception as e:
                logger.warning(f"Final content update failed: {e}")

        # 关闭 streaming 模式
        self._sequence += 1
        client = _get_client(self.config)
        settings = json.dumps({
            "config": {
                "streaming_mode": False,
                "summary": {"content": (text or "")[:50].replace("\n", " ")},
            },
        })

        req = SettingsCardRequest.builder() \
            .card_id(self.card_id) \
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings)
                .sequence(self._sequence)
                .uuid(f"c_{self.card_id}_{self._sequence}")
                .build()
            ) \
            .build()

        try:
            resp = client.cardkit.v1.card.settings(req)
            if resp.code != 0:
                logger.warning(f"Close streaming failed: code={resp.code}")
            else:
                logger.info(f"Streaming closed: card_id={self.card_id}, final_len={len(text)}")
        except Exception as e:
            logger.warning(f"Close streaming exception: {e}")

    def is_active(self) -> bool:
        return not self._closed and self.card_id is not None
