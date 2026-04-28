"""飞书消息发送 - 文字、富文本、错误提示"""

import json
import logging
from typing import List

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from config import Config

logger = logging.getLogger(__name__)


def _build_client(config: Config) -> lark.ws.Client:
    """
    注意：飞书消息发送不通过 ws.Client，而是通过 HTTP API。
    这里使用 lark-oapi 的 HTTP 客户端。
    """
    client = lark.Client.builder().app_id(config.feishu_app_id).app_secret(config.feishu_app_secret).build()
    return client


def _get_client(config: Config) -> lark.Client:
    """获取飞书 HTTP 客户端（全局单例）"""
    if not hasattr(_get_client, "_instance"):
        _get_client._instance = _build_client(config)
    return _get_client._instance


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
            logger.error(f"send_text failed: code={response.code}, msg={response.msg}, request_id={response.request_id}")
        else:
            logger.debug(f"send_text OK, message_id={response.data.get('message_id', 'N/A')}")
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
            logger.error(f"send_rich_text failed: code={response.code}, msg={response.msg}, request_id={response.request_id}")
        else:
            logger.debug(f"send_rich_text OK")
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
