"""飞书消息发送 - 文字、富文本、错误提示"""

import json
import logging
from typing import List, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

import time

from config import Config

logger = logging.getLogger(__name__)


def _build_client(config: Config) -> lark.Client:
    """构建飞书 HTTP 客户端，设置 30 秒超时"""
    client = (lark.Client.builder()
        .app_id(config.feishu_app_id)
        .app_secret(config.feishu_app_secret)
        .timeout(30000)  # 30 秒超时
        .build())
    return client


def _get_client(config: Config) -> lark.Client:
    """获取飞书 HTTP 客户端（每次新建，避免与 WS client 冲突）"""
    return _build_client(config)


def send_text(conversation_id: str, content: str, config: Config) -> None:
    """发送文字消息

    Args:
        conversation_id: 会话 ID（open_id / chat_id）
        content: 消息文本
        config: 配置
    """
    try:
        client = _get_client(config)
        # 根据 conversation_id 前缀判断 receive_id_type
        if conversation_id.startswith("ou_"):
            receive_id_type = "open_id"
        elif conversation_id.startswith("oc_"):
            receive_id_type = "chat_id"
        elif conversation_id.startswith("on_"):
            receive_id_type = "union_id"
        else:
            receive_id_type = "open_id"

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(conversation_id)
                .msg_type("text")
                .content(json.dumps({"text": content}))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"send_text failed: code={response.code}, msg={response.msg}")
        else:
            logger.debug("send_text OK")
    except Exception as e:
        logger.error(f"send_text exception: {e}")
        raise


def send_rich_text(conversation_id: str, title: str, elements: List[str], config: Config) -> None:
    """发送富文本消息（多行文本列表）

    Args:
        conversation_id: 会话 ID
        title: 第一行加粗标题
        elements: 后续文本行列表
        config: 配置
    """
    try:
        client = _get_client(config)
        if conversation_id.startswith("ou_"):
            receive_id_type = "open_id"
        elif conversation_id.startswith("oc_"):
            receive_id_type = "chat_id"
        else:
            receive_id_type = "open_id"

        # 构建富文本内容
        content = {
            "zh_cn": {
                "title": title,
                "content": [
                    [{"tag": "text", "text": line}] for line in elements
                ]
            }
        }

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(conversation_id)
                .msg_type("post")
                .content(json.dumps(content))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"send_rich_text failed: code={response.code}, msg={response.msg}")
        else:
            logger.debug("send_rich_text OK")
    except Exception as e:
        logger.error(f"send_rich_text exception: {e}")
        raise


def send_error(conversation_id: str, error_msg: str, config: Config) -> None:
    """发送错误提示消息

    Args:
        conversation_id: 会话 ID
        error_msg: 错误信息
        config: 配置
    """
    send_text(conversation_id, f"⚠️ 操作失败：{error_msg}", config)


def build_card(text: str, is_final: bool = False) -> dict:
    """构建飞书 interactive card JSON

    Args:
        text: 卡片显示的文字内容（支持 Markdown）
        is_final: 是否为最终回复（控制 header 样式）

    Returns:
        卡片 JSON dict
    """
    header_title = "🤖 AI 回复完成" if is_final else "🤖 AI 回复中..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "green" if is_final else "blue",
        },
        "elements": [
            {"tag": "markdown", "content": text or "正在思考..."},
        ],
    }


def send_card(conversation_id: str, card: dict, config: Config) -> Optional[str]:
    """发送 interactive card 消息

    Args:
        conversation_id: 会话 ID（open_id / chat_id）
        card: 卡片 JSON dict
        config: 配置

    Returns:
        消息 ID（可用于后续 update_card），失败返回 None
    """
    try:
        client = _get_client(config)
        if conversation_id.startswith("ou_"):
            receive_id_type = "open_id"
        elif conversation_id.startswith("oc_"):
            receive_id_type = "chat_id"
        elif conversation_id.startswith("on_"):
            receive_id_type = "union_id"
        else:
            receive_id_type = "open_id"

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(conversation_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()
            ) \
            .build()

        response = client.im.v1.message.create(request)
        if not response.success():
            logger.error(f"send_card failed: code={response.code}, msg={response.msg}")
            return None

        msg_id = None
        try:
            data = response.data
            msg_id = data.message_id
        except Exception:
            logger.warning("Could not extract message_id from send_card response")

        logger.debug(f"send_card OK, msg_id={msg_id}")
        return msg_id

    except Exception as e:
        logger.error(f"send_card exception: {e}")
        return None


def update_card(message_id: str, card: dict, config: Config) -> bool:
    """更新已发送的 interactive card

    Args:
        message_id: 飞书消息 ID（om_xxx）
        card: 新的卡片 JSON dict
        config: 配置

    Returns:
        True 成功，False 失败
    """
    try:
        client = _get_client(config)
        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card))
                .build()
            ) \
            .build()

        response = client.im.v1.message.patch(request)
        if not response.success():
            logger.error(f"update_card failed: code={response.code}, msg={response.msg}")
            return False

        logger.debug("update_card OK")
        return True

    except Exception as e:
        logger.error(f"update_card exception: {e}")
        return False
