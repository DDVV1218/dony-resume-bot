"""命令处理层 - 本地命令（/status 等）

通过 registry 装饰器注册，支持声明式路由。
添加新命令：
  1. 定义一个 handler 函数，使用 @registry.command("name") 装饰
  2. 在 bot.py 中通过 registry.resolve() 分发
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from config import Config
from services.llm import estimate_tokens
from services.registry import get_registry
from services.session import SessionStore

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)

# 获取全局 registry 实例
registry = get_registry()


@registry.command("status")
def cmd_status(inbound: InboundMessage, store: SessionStore, config: Config) -> str:
    """处理 /status 命令，返回格式化文本"""
    session_key = inbound.session_key
    try:
        session = store.get_or_create(session_key)
        messages = session.messages

        total_tokens = estimate_tokens(messages, config.openai_model)
        context_ratio = (total_tokens / config.openai_context_window * 100) if config.openai_context_window > 0 else 0
        all_sessions = store.list_sessions(session_key)

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
            lines.append("（使用 /new 创建新 session）")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"/status failed: {e}")
        return f"⚠️ 获取状态失败：{e}"


@registry.command("new")
def cmd_new_session(inbound: InboundMessage, store: SessionStore, config: Config) -> str:
    """处理 /new 命令，创建新 session"""
    try:
        store.create_session(inbound.session_key)
        return "✅ 已创建新的对话 Session。"
    except Exception as e:
        logger.error(f"/new failed: {e}")
        return f"⚠️ 创建 Session 失败：{e}"


def parse_command(message: str) -> tuple:
    """解析命令消息

    Returns:
        (command_name, command_args) 元组，非命令返回 (None, None)
    """
    text = message.strip()
    if not text.startswith("/"):
        return None, None

    parts = text[1:].split(None, 1)
    command = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else None
    return command, args


def handle_command(inbound: InboundMessage, store: SessionStore, config: Config) -> Optional[str]:
    """通过 registry 分发命令

    Returns:
        命令处理结果，非命令返回 None
    """
    text = inbound.text
    if not text:
        return None

    command, args = parse_command(text)
    if not command:
        return None

    handler = registry.resolve(command)
    if handler:
        return handler(inbound, store, config)

    # 未知命令
    known = ", ".join(sorted(registry.commands.keys()))
    return f"⚠️ 未知命令: /{command}\n可用命令: /{known}\n发送 /status 查看帮助"
