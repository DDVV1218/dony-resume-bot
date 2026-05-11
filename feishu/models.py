"""消息数据模型 - 规范化入站消息"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    """规范化入站消息

    统一所有消息类型的字段，使业务处理层不依赖于飞书 SDK 的具体事件结构。
    """

    session_key: str           # 用于 SessionStore 的 key: "dm:ou_xxx" / "group:oc_xxx"
    conversation_id: str       # 用于飞书 API 回复的会话 ID
    chat_type: str             # "p2p" / "group"
    sender_id: Optional[str]   # 发送者 open_id
    text: Optional[str]        # 用户文本（已去除 @Bot 部分）
    message_type: str          # "text" / "image" / "file" / "audio" / ...
    message_id: str            # 飞书消息唯一 ID
    create_time: int           # 消息创建时间（毫秒时间戳）
    file_key: Optional[str] = field(default=None)   # 文件消息的 file_key
    file_name: Optional[str] = field(default=None)  # 文件消息的文件名
    thread_id: Optional[str] = field(default=None)  # 飞书 topic 线程 ID


def resolve_inbound(data: P2ImMessageReceiveV1) -> "InboundMessage":
    """从飞书事件中解析出规范化的入站消息

    Args:
        data: 飞书 P2ImMessageReceiveV1 事件

    Returns:
        InboundMessage 实例
    """
    event = data.event
    message = event.message if event else None

    # --- chat_type ---
    chat_type = message.chat_type if message else "unknown"

    # --- sender_id ---
    sender_id = None
    if event and event.sender and event.sender.sender_id:
        sender_id = event.sender.sender_id.open_id

    # --- message_id / create_time / message_type ---
    msg_id = message.message_id if message else ""
    create_time = int(message.create_time) if message and message.create_time else 0
    msg_type = message.message_type if message else ""

    # --- conversation_id & session_key ---
    if chat_type == "p2p":
        conversation_id = sender_id or ""
        session_key = f"dm:{sender_id}" if sender_id else "unknown"
    elif chat_type == "group":
        chat_id = message.chat_id if message else ""
        conversation_id = chat_id or ""
        session_key = f"group:{chat_id}" if chat_id else "unknown"
    else:
        conversation_id = ""
        session_key = "unknown"

    # --- text / file_key ---
    text = None
    file_key = None
    file_name = None
    if msg_type == "text" and message and message.content:
        try:
            content = json.loads(message.content)
            text = content.get("text", "").strip()
        except (json.JSONDecodeError, ValueError):
            text = None
    elif msg_type in ("file", "media") and message and message.content:
        try:
            content = json.loads(message.content)
            file_key = content.get("file_key", "") or content.get("file_token", "")
            file_name = content.get("file_name", "") or content.get("name", "")
        except (json.JSONDecodeError, ValueError):
            pass

    # --- 富文本 post（带格式粘贴的文字） ---
    if not text and msg_type == "post" and message and message.content:
        try:
            post_data = json.loads(message.content)
            logger.debug(f"Post content: {message.content[:1000]}")
            # post 格式: { "zh_cn": { "content": [[{"tag":"text","text":"..."},...]] } }
            lang_content = post_data
            for key in ("zh_cn", "en_us"):
                if key in post_data and isinstance(post_data[key], dict):
                    lang_content = post_data[key]
                    break
            paragraphs = lang_content.get("content", []) if isinstance(lang_content, dict) else []
            if not paragraphs:
                # 直接 content 键（无语言包装）
                paragraphs = post_data.get("content", [])
                if isinstance(paragraphs, dict):
                    paragraphs = []
            text_parts = []
            for para in paragraphs:
                if not isinstance(para, list):
                    continue
                for seg in para:
                    if isinstance(seg, dict) and seg.get("tag") == "text":
                        text_parts.append(seg.get("text", ""))
            if text_parts:
                text = "".join(text_parts).strip()
            logger.debug(f"Post extracted text: {text[:200] if text else 'None'}")
        except Exception as e:
            logger.warning(f"Failed to parse post content: {e}")
            text = None

    # --- 去除 @Bot 标记 ---
    if text and message and message.mentions:
        text = re.sub(r'<at\s+id="[^"]*">\s*</at>\s*', '', text).strip()

    # --- thread_id ---
    thread_id = None
    if message:
        thread_id = getattr(message, "thread_id", None)

    return InboundMessage(
        session_key=session_key,
        conversation_id=conversation_id,
        chat_type=chat_type,
        sender_id=sender_id,
        text=text or None,
        message_type=msg_type,
        message_id=msg_id,
        create_time=create_time,
        file_key=file_key or None,
        file_name=file_name or None,
        thread_id=thread_id,
    )
