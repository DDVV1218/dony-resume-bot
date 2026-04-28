"""飞书 Bot 事件处理器 - WebSocket 长连接、消息路由、LLM 对话"""

import logging
import threading
from datetime import datetime
from typing import Dict, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config import Config
from feishu.dedup import DedupeGuard, TTLSet
from feishu.messages import send_text, send_error
from feishu.models import InboundMessage, resolve_inbound
from services.handlers import TextHandler, UnsupportedHandler, ResumePDFHandler, ResumeImageHandler
from services.session import SessionStore
from services.commands import handle_command
from prompts import load_prompt

logger = logging.getLogger(__name__)


class MessageHandler:
    """飞书消息处理器

    处理 im.message.receive_v1 事件，支持：
    - 普通文字消息 → LLM 对话
    - /status 命令 → 显示 session 信息
    - 文件/图片消息 → 暂不支持提示
    """

    def __init__(self, config: Config, session_store: SessionStore):
        self.config = config
        self.session_store = session_store
        # 每个 session_key 一个锁，保证同 session 消息串行
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        # 系统提示词
        self._system_prompt = load_prompt("system_prompt")
        # 去重守卫：TTL cache + Inflight 三层保护
        self._dedup_guard = DedupeGuard(ttl_ms=600_000, max_size=5000)
        # 文本+时间窗口去重（第二层保护，独立 TTL cache）
        self._text_dedup_cache = TTLSet(ttl_ms=600_000, max_size=5000)
        # 消息处理器链
        self._handler_chain = self._build_handler_chain()

    def _get_lock(self, session_key: str) -> threading.Lock:
        with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = threading.Lock()
            return self._locks[session_key]

    def _resolve_inbound(self, data: P2ImMessageReceiveV1) -> InboundMessage:
        """从飞书事件解析规范化入站消息"""
        return resolve_inbound(data)

    def _is_mented_bot(self, data: P2ImMessageReceiveV1) -> bool:
        """检查是否在群聊中 @了 Bot

        使用配置的 feishu_require_mention 控制是否强制要求 @mention。
        """
        event = data.event
        if event is None:
            return True

        message = event.message
        if message is None:
            return True

        # 单聊不需要 @
        if message.chat_type == "p2p":
            return True

        # 如果配置允许不 @mention 就响应
        if not self.config.feishu_require_mention:
            return True

        # 群聊中检查是否 @了 Bot
        mentions = message.mentions
        if mentions:
            for mention in mentions:
                if mention.id and mention.id.type == "appId" and mention.id.app_id == self.config.feishu_app_id:
                    return True

        return False

    def _check_dm_access(self, inbound: InboundMessage) -> bool:
        """检查 DM 访问权限

        Returns:
            True 允许通过，False 拒绝访问
        """
        policy = self.config.feishu_dm_policy
        if policy == "open":
            return True

        if policy == "allowlist":
            open_id = inbound.sender_id
            if open_id and open_id in self.config.feishu_dm_allowlist:
                return True
            logger.warning(f"DM access denied: open_id={open_id} not in allowlist")
            return False

        logger.warning(f"Unknown dm_policy: {policy}")
        return False


    def _check_group_access(self, inbound: InboundMessage) -> bool:
        """检查群聊访问权限

        Returns:
            True 允许通过，False 拒绝访问（静默忽略）
        """
        policy = self.config.feishu_group_policy
        if policy == "disabled":
            return False

        if policy == "open":
            return True

        if policy == "allowlist":
            # 群聊中 conversation_id 就是 chat_id
            if inbound.conversation_id in self.config.feishu_group_allowlist:
                return True
            logger.warning(f"Group access denied: chat_id={inbound.conversation_id} not in group_allowlist")
            return False

        logger.warning(f"Unknown group_policy: {policy}")
        return False


    def handle(self, data: P2ImMessageReceiveV1) -> None:
        """处理飞书消息事件

        Args:
            data: 飞书消息事件数据
        """
        logger.info(f"Received message event")

        # 统一解析为 InboundMessage
        inbound = self._resolve_inbound(data)
        logger.info(f"Session key: {inbound.session_key}, Conversation ID: {inbound.conversation_id}")

        if not inbound.session_key or inbound.session_key == "unknown" or not inbound.conversation_id:
            logger.warning(f"Cannot determine session_key or conversation_id")
            return

        # 群聊中检查是否 @了 Bot（受 feishu_require_mention 配置控制）
        if inbound.chat_type == "group":
            if not self._check_group_access(inbound):
                logger.info(f"Group access denied for {inbound.session_key}, skipping")
                return
            if not self._is_mented_bot(data):
                logger.debug(f"Not mentioned in group, ignoring")
                return
        elif inbound.chat_type == "p2p":
            if not self._check_dm_access(inbound):
                logger.info(f"DM access denied for {inbound.session_key}, sending notice")
                send_text(inbound.conversation_id, f"⛔ 抱歉，您没有权限使用此 Bot。\n您的 open_id: {inbound.sender_id}\n若需要聊天，请将以上 open_id 提供给管理员添加配置。", self.config)
                return

        # ===== 去重检查（在 handle 层，防止竞态）=====
        msg_id = inbound.message_id
        if msg_id:
            msg_text = inbound.text or ""

            # 1. In-flight dedup（message_id 级别，含 TTL cache）
            if not self._dedup_guard.claim(msg_id):
                logger.info(f"Duplicate by message_id {msg_id}, skipping")
                return

            # 2. Text+time 窗口去重（第二层保护）
            dedup_key = f"{inbound.session_key}:{msg_text}:{inbound.create_time // 3}"
            if self._text_dedup_cache.check_and_add(dedup_key):
                logger.info(f"Duplicate by text+time, releasing inflight: {msg_id}")
                self._dedup_guard.release(msg_id)
                return
        # ========================================

        # 在后台线程中处理，避免阻塞 WebSocket 主线程（ping/pong）
        thread = threading.Thread(
            target=self._process_in_background,
            args=(inbound, data, msg_id),
            daemon=True,
        )
        thread.start()


    def _is_old_message(self, inbound: InboundMessage) -> bool:
        """检查消息是否是历史消息（创建时间早于 session 的最后更新时间）

        防止飞书 WebSocket 重连后补推旧消息导致重复处理。
        """
        if not inbound.create_time:
            return False

        msg_ts = inbound.create_time / 1000.0  # ms -> seconds

        # 检查 session 的最后更新时间
        try:
            user_dir = self.session_store._user_dir(inbound.session_key)
            active_file = self.session_store._active_file(user_dir)
            if active_file.exists():
                active_id = active_file.read_text(encoding="utf-8").strip()
                session_file = self.session_store._session_file(user_dir, active_id)
                if session_file.exists():
                    import json as _json
                    session_data = _json.loads(session_file.read_text(encoding="utf-8"))
                    updated_at = session_data.get("updated_at", "")
                    if updated_at:
                        # Parse ISO format timestamp
                        from datetime import datetime as _dt
                        session_updated = _dt.fromisoformat(updated_at).timestamp()
                        # 如果消息创建时间早于 session 最后更新，视为旧消息
                        if msg_ts < session_updated - 2:  # 2 秒容差
                            logger.info(f"Old message detected: msg_time={msg_ts} < session_updated={session_updated}, skipping")
                            return True
        except Exception as e:
            logger.debug(f"Failed to check message age: {e}")

        return False


    def _process_in_background(self, inbound: InboundMessage, data: P2ImMessageReceiveV1, msg_id: Optional[str] = None) -> None:
        """在后台线程中处理消息

        Args:
            inbound: 规范化入站消息
            data: 原始事件数据（仅用于 _is_mented_bot，后续逐步迁移）
            msg_id: 消息 ID，用于 inflight dedup commit/release
        """
        try:
            # 检查是否是历史消息（飞书重连后补推的旧消息）
            if self._is_old_message(inbound):
                if msg_id:
                    self._dedup_guard.release(msg_id)
                return

            # 访问控制检查（防御性二次检查）
            if inbound.chat_type == "group" and not self._check_group_access(inbound):
                if msg_id:
                    self._dedup_guard.release(msg_id)
                return
            if inbound.chat_type == "p2p" and not self._check_dm_access(inbound):
                if msg_id:
                    self._dedup_guard.release(msg_id)
                return

            lock = self._get_lock(inbound.session_key)
            with lock:
                self._process_message(inbound)

            # 处理成功后 commit（加入 TTL cache）
            if msg_id:
                self._dedup_guard.commit(msg_id)

        except Exception as e:
            logger.error(f"Error handling message for {inbound.session_key}: {e}")
            # 异常路径：仅释放 inflight，不加入 cache
            if msg_id:
                self._dedup_guard.release(msg_id)
            try:
                send_error(inbound.conversation_id, f"处理消息时出错：{e}", self.config)
            except Exception as reply_err:
                logger.error(f"Failed to send error reply: {reply_err}")



    def _build_handler_chain(self):
        """构建消息处理器链

        按优先级排列，第一个 can_handle() 返回 True 的处理器接管。
        Phase 2 时在此处添加 ResumePDFHandler 和 ResumeImageHandler（已注册，暂 disabled）。
        """
        return [
            TextHandler(self.config, self.session_store, self._system_prompt),
            UnsupportedHandler(self.config, self.session_store, self._system_prompt),
            # Phase 2 启用简历处理器：
            # ResumePDFHandler(self.config, self.session_store, self._system_prompt),
            # ResumeImageHandler(self.config, self.session_store, self._system_prompt),
        ]

    def _process_message(self, inbound: InboundMessage) -> None:
        """处理消息（已加锁，串行执行）"""
        text = inbound.text
        session_key = inbound.session_key
        conversation_id = inbound.conversation_id

        # 文字消息优先处理命令
        if text is not None:
            # 通过 registry 分发命令
            result = handle_command(inbound, self.session_store, self.config)
            if result is not None:
                send_text(conversation_id, result, self.config)
                return

        # 通过处理器链处理
        for handler in self._handler_chain:
            if handler.can_handle(inbound):
                try:
                    reply = handler.handle(inbound)
                    if reply:
                        send_text(conversation_id, reply, self.config)
                        logger.info(f"{handler.__class__.__name__} replied to {conversation_id}")
                    return
                except Exception as e:
                    logger.error(f"{handler.__class__.__name__} failed: {e}")
                    send_error(conversation_id, f"处理失败：{e}", self.config)
                    return

        logger.warning(f"No handler found for {inbound.message_type} message from {session_key}")


def build_event_handler(config: Config, session_store: SessionStore) -> lark.EventDispatcherHandler:
    """构建飞书事件处理器

    Returns:
        lark.EventDispatcherHandler 实例
    """
    handler = MessageHandler(config, session_store)

    def on_message(data: P2ImMessageReceiveV1) -> None:
        handler.handle(data)

    # 使用 builder 模式注册事件处理器（必须在 build() 之前）
    dispatcher = (lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build())
    return dispatcher
