"""飞书 Bot 事件处理器 - WebSocket 长连接、消息路由、LLM 对话"""

import json
import logging
import threading
from datetime import datetime
from typing import Dict, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config import Config
from feishu.dedup import DedupeGuard, TTLSet
from feishu.messages import send_text, send_error
from services.session import SessionStore
from services.llm import prepare_context, chat, estimate_tokens
from services.commands import handle_status, parse_command
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

    def _get_lock(self, session_key: str) -> threading.Lock:
        with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = threading.Lock()
            return self._locks[session_key]

    def _get_session_key(self, data: P2ImMessageReceiveV1) -> str:
        """从事件数据中提取 session_key

        单聊: dm:{open_id}
        群聊: group:{chat_id}
        """
        event = data.event
        if event is None:
            return "unknown"

        message = event.message
        if message is None:
            return "unknown"

        # 判断单聊还是群聊
        chat_type = message.chat_type
        if chat_type == "p2p":
            # 单聊 - 使用发送者的 open_id
            sender = event.sender
            if sender and sender.sender_id:
                open_id = sender.sender_id.open_id
                return f"dm:{open_id}" if open_id else "unknown"
        elif chat_type == "group":
            # 群聊 - 使用 chat_id
            chat_id = message.chat_id
            return f"group:{chat_id}" if chat_id else "unknown"

        return "unknown"

    def _get_conversation_id(self, data: P2ImMessageReceiveV1) -> str:
        """从事件数据中提取 conversation_id（用于回复消息）"""
        event = data.event
        if event is None:
            return ""

        message = event.message
        if message is None:
            return ""

        chat_type = message.chat_type
        if chat_type == "p2p":
            sender = event.sender
            if sender and sender.sender_id:
                return sender.sender_id.open_id or ""
        elif chat_type == "group":
            return message.chat_id or ""

        return ""

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

    def _check_dm_access(self, data: P2ImMessageReceiveV1) -> bool:
        """检查 DM 访问权限

        Returns:
            True 允许通过，False 拒绝访问
        """
        policy = self.config.feishu_dm_policy
        if policy == "open":
            return True

        if policy == "allowlist":
            event = data.event
            if event and event.sender and event.sender.sender_id:
                open_id = event.sender.sender_id.open_id
                if open_id in self.config.feishu_dm_allowlist:
                    return True
                logger.warning(f"DM access denied: open_id={open_id} not in allowlist")
            return False

        logger.warning(f"Unknown dm_policy: {policy}")
        return False

    def _check_group_access(self, data: P2ImMessageReceiveV1) -> bool:
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
            event = data.event
            if event and event.message:
                chat_id = event.message.chat_id
                if chat_id in self.config.feishu_group_allowlist:
                    return True
                logger.warning(f"Group access denied: chat_id={chat_id} not in group_allowlist")
            return False

        logger.warning(f"Unknown group_policy: {policy}")
        return False

    def _extract_text_content(self, data: P2ImMessageReceiveV1) -> Optional[str]:
        """提取消息文本内容"""
        event = data.event
        if event is None:
            return None

        message = event.message
        if message is None:
            return None

        msg_type = message.message_type

        if msg_type == "text":
            # 解析 text 类型的 content（JSON 字符串）
            try:
                content = json.loads(message.content) if message.content else {}
                return content.get("text", "").strip()
            except json.JSONDecodeError:
                return None

        return None

    def _get_user_text_from_mention(self, data: P2ImMessageReceiveV1) -> Optional[str]:
        """从 @Bot 消息中提取用户实际输入（去除 @Bot 部分）"""
        text = self._extract_text_content(data)
        if text is None:
            return None

        # 如果消息包含 @Bot，去掉 @Bot 部分
        event = data.event
        if event and event.message and event.message.mentions:
            # 按 mentions 位置移除 @Bot 引用
            mentions = event.message.mentions
            # 简单处理：去掉所有 <at id=...></at> 及相邻空白
            import re
            text = re.sub(r'<at\s+id="[^"]*">\s*</at>\s*', '', text).strip()

        return text if text else None

    def handle(self, data: P2ImMessageReceiveV1) -> None:
        """处理飞书消息事件

        Args:
            data: 飞书消息事件数据
        """
        logger.info(f"Received message event")
        session_key = self._get_session_key(data)
        conversation_id = self._get_conversation_id(data)
        logger.info(f"Session key: {session_key}, Conversation ID: {conversation_id}")

        if not session_key or session_key == "unknown" or not conversation_id:
            logger.warning(f"Cannot determine session_key or conversation_id")
            return

        # 群聊中检查是否 @了 Bot（受 feishu_require_mention 配置控制）
        event = data.event
        if event and event.message:
            chat_type = event.message.chat_type
            if chat_type == "group":
                # 群聊访问控制
                if not self._check_group_access(data):
                    logger.info(f"Group access denied for {session_key}, skipping")
                    return
                # @mention 检查
                if not self._is_mented_bot(data):
                    logger.debug(f"Not mentioned in group, ignoring")
                    return
            elif chat_type == "p2p":
                # 单聊访问控制
                if not self._check_dm_access(data):
                    logger.info(f"DM access denied for {session_key}, sending notice")
                    send_text(conversation_id, "⛔ 抱歉，您没有权限使用此 Bot。", self.config)
                    return

        # ===== 去重检查（在 handle 层，防止竞态）=====
        if event and event.message:
            msg_id = event.message.message_id
            msg_text = self._extract_text_content(data) or ""
            msg_create_time = event.message.create_time

            # 1. In-flight dedup（message_id 级别，含 TTL cache）
            if not self._dedup_guard.claim(msg_id):
                logger.info(f"Duplicate by message_id {msg_id}, skipping")
                return

            # 2. Text+time 窗口去重（第二层保护）
            now_ts = int(msg_create_time) if msg_create_time else 0
            dedup_key = f"{session_key}:{msg_text}:{now_ts // 3}"
            if self._text_dedup_cache.check_and_add(dedup_key):
                logger.info(f"Duplicate by text+time, releasing inflight: {msg_id}")
                self._dedup_guard.release(msg_id)
                return
        else:
            msg_id = None
        # ========================================

        # 在后台线程中处理，避免阻塞 WebSocket 主线程（ping/pong）
        thread = threading.Thread(
            target=self._process_in_background,
            args=(session_key, conversation_id, data, msg_id),
            daemon=True,
        )
        thread.start()

    def _is_old_message(self, data: P2ImMessageReceiveV1, session_key: str) -> bool:
        """检查消息是否是历史消息（创建时间早于 session 的最后更新时间）

        防止飞书 WebSocket 重连后补推旧消息导致重复处理。
        """
        event = data.event
        if not event or not event.message:
            return False

        msg_create_time = event.message.create_time
        if not msg_create_time:
            return False

        msg_ts = int(msg_create_time) / 1000.0  # ms -> seconds

        # 检查 session 的最后更新时间
        try:
            user_dir = self.session_store._user_dir(session_key)
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

    def _process_in_background(self, session_key: str, conversation_id: str, data: P2ImMessageReceiveV1, msg_id: Optional[str] = None) -> None:
        """在后台线程中处理消息

        Args:
            session_key: session 标识
            conversation_id: 飞书会话 ID
            data: 原始事件数据
            msg_id: 消息 ID，用于 inflight dedup commit/release
        """
        try:
            # 检查是否是历史消息（飞书重连后补推的旧消息）
            if self._is_old_message(data, session_key):
                if msg_id:
                    self._dedup_guard.release(msg_id)
                return

            # 访问控制检查（防御性二次检查，保障即使 handle() 层有遗漏）
            event = data.event
            if event and event.message:
                chat_type = event.message.chat_type
                if chat_type == "group" and not self._check_group_access(data):
                    if msg_id:
                        self._dedup_guard.release(msg_id)
                    return
                if chat_type == "p2p" and not self._check_dm_access(data):
                    if msg_id:
                        self._dedup_guard.release(msg_id)
                    return

            lock = self._get_lock(session_key)
            with lock:
                self._process_message(session_key, conversation_id, data)

            # 处理成功后 commit（加入 TTL cache）
            if msg_id:
                self._dedup_guard.commit(msg_id)

        except Exception as e:
            logger.error(f"Error handling message for {session_key}: {e}")
            # 异常路径：仅释放 inflight，不加入 cache
            if msg_id:
                self._dedup_guard.release(msg_id)
            try:
                send_error(conversation_id, f"处理消息时出错：{e}", self.config)
            except Exception as reply_err:
                logger.error(f"Failed to send error reply: {reply_err}")

    def _process_message(self, session_key: str, conversation_id: str, data: P2ImMessageReceiveV1) -> None:
        """处理消息（已加锁，串行执行）"""
        # 获取用户文本
        text = self._get_user_text_from_mention(data)

        if text is None:
            # 非文字消息（文件/图片等）
            send_text(conversation_id, "⚠️ 暂不支持该类型消息，请发送文字消息。", self.config)
            return

        # 检查是否为命令
        command, args = parse_command(text)
        if command:
            if command == "status":
                status_text = handle_status(session_key, self.session_store, self.config)
                send_text(conversation_id, status_text, self.config)
            elif command == "new":
                self.session_store.create_session(session_key)
                send_text(conversation_id, "✅ 已创建新的对话 Session。", self.config)
            else:
                send_text(conversation_id, f"⚠️ 未知命令: /{command}\n发送 /status 查看帮助", self.config)
            return

        # 检查是否是 session 切换指令（纯数字）
        if text.isdigit() and len(text) <= 3:
            try:
                session_id = text.zfill(3)
                self.session_store.switch_session(session_key, session_id)
                send_text(conversation_id, f"✅ 已切换到 Session #{session_id}", self.config)
            except FileNotFoundError:
                send_text(conversation_id, f"⚠️ Session #{text} 不存在", self.config)
            except Exception as e:
                send_text(conversation_id, f"⚠️ 切换失败：{e}", self.config)
            return

        # --- 普通文字消息：LLM 对话 ---
        try:
            # 获取 session 对象（已持有锁）
            session = self.session_store.get_or_create(session_key)
            logger.info(f"Session loaded, {len(session.messages)} messages")

            # 确保 system message 在第一条位置，且只有一条
            system_msg = {"role": "system", "content": self._system_prompt}
            # 先移除所有旧 system message
            session.messages = [m for m in session.messages if m.get("role") != "system"]
            # 然后在开头插入唯一的 system message
            session.messages.insert(0, system_msg)
            # 保存修复后的 session（避免嵌套锁：直接操作文件）
            self.session_store._save_session(
                self.session_store._user_dir(session_key), session
            )
            logger.info(f"System message fixed, {len(session.messages)} messages")

            # 追加用户消息（直接操作 session 对象，避免嵌套锁）
            session.messages.append({"role": "user", "content": text})
            session.updated_at = datetime.now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)
            logger.info(f"User message appended, {len(session.messages)} total")

            logger.info(f"Calling prepare_context...")
            context = prepare_context(session.messages, self.config)
            logger.info(f"Calling LLM chat...")
            reply = chat(context, self.config)
            logger.info(f"LLM replied ({len(reply)} chars)")

            # 追加助手回复
            session.messages.append({"role": "assistant", "content": reply})
            session.updated_at = datetime.now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 发送回复
            send_text(conversation_id, reply, self.config)
            logger.info(f"Reply sent to {conversation_id}")

        except Exception as e:
            logger.error(f"LLM chat failed for {session_key}: {e}")
            send_error(conversation_id, f"LLM 调用失败：{e}", self.config)


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
