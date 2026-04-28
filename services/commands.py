"""命令处理层 - 本地命令（/status 等）"""

import logging
from typing import List, Optional

from services.session import SessionInfo, SessionStore
from services.llm import estimate_tokens
from config import Config

logger = logging.getLogger(__name__)


def handle_status(session_key: str, store: SessionStore, config: Config) -> str:
    """处理 /status 命令，返回格式化文本

    Args:
        session_key: 当前 session key
        store: SessionStore 实例
        config: 配置

    Returns:
        格式化的状态文本
    """
    try:
        # 获取当前 active session
        session = store.get_or_create(session_key)
        messages = session.messages

        # 计算 token 数
        total_tokens = estimate_tokens(messages, config.openai_model)
        context_ratio = (total_tokens / config.openai_context_window * 100) if config.openai_context_window > 0 else 0

        # 获取所有 sessions
        all_sessions = store.list_sessions(session_key)

        # 构建状态文本
        lines = [
            "📋 Session 信息",
            "━" * 30,
            f"Session ID: #{session.id}",
            f"Session Key: {session_key}",
            f"创建时间: {session.created_at}",
            f"总消息数: {len(messages)}",
            f"上下文: {total_tokens:,} / {config.openai_context_window:,} tokens ({context_ratio:.1f}%)",
            "",
            f"历史 Sessions: {len(all_sessions)} 个",
        ]

        if len(all_sessions) > 1:
            lines.append("（回复 session 编号可切换，如：1）")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"/status failed: {e}")
        return f"⚠️ 获取状态失败：{e}"


def parse_command(message: str):
    """解析命令消息

    Args:
        message: 用户消息内容

    Returns:
        (command_name, command_args) 元组，如果不是命令则返回 (None, None)
    """
    text = message.strip()
    if not text.startswith("/"):
        return None, None

    parts = text[1:].split(None, 1)
    command = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else None
    return command, args
