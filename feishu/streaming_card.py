"""飞书流式卡片 - 基于 Card Kit API

参考 OpenClaw 的 FeishuStreamingSession 实现，使用飞书 Card Kit API
实现卡片的创建、更新和关闭。

核心策略：
  1. Token 在 start() 时一次性获取，缓存复用
  2. start() 立刻创建 "🤖 AI 回复中..." 卡片
  3. close() 更新内容并切换为 "🤖 AI 回复完成"
  4. 输出前预处理 Markdown 适配飞书卡片渲染
"""

import json
import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://open.feishu.cn/open-apis"

TOKEN_CACHE: dict[str, tuple[str, float]] = {}
TOKEN_TTL = 3600  # 1 小时


def _clean_markdown(text: str) -> str:
    """预处理 Markdown 文本，适配飞书卡片渲染

    飞书卡片 markdown 的注意事项：
    - 单换行不被渲染为换行，需要空行（双换行）才能分段
    - 代码块前后需要空行
    - 表格前后需要空行
    - 某些特殊 HTML 标签可能不兼容
    """
    if not text:
        return text

    # 1. 确保段落间有空行（单换行 → 双换行）
    # 但保留代码块、列表内的单换行
    lines = text.split("\n")
    result = []
    in_code_block = False
    prev_blank = True  # 第一行前视为空行

    for i, line in enumerate(lines):
        stripped = line.strip()

        # 检测代码块
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            result.append(line)
            prev_blank = False
            continue

        # 代码块内的内容保持原样
        if in_code_block:
            result.append(line)
            prev_blank = False
            continue

        # 空行：保留
        if not stripped:
            result.append(line)
            prev_blank = True
            continue

        # 列表项：不需要额外空行
        if re.match(r"^[-*+]\s", stripped) or re.match(r"^\d+[.)]\s", stripped):
            if not prev_blank and i > 0 and lines[i - 1].strip():
                result.append("")  # 列表前加空行
            result.append(line)
            prev_blank = False
            continue

        # 表格行：前后保证空行
        if stripped.startswith("|") and stripped.endswith("|"):
            if not prev_blank:
                result.append("")
            result.append(line)
            prev_blank = False
            # 表格后一行如果是表格继续，否则加空行
            continue

        # 标题：前后保证空行
        if stripped.startswith("#"):
            if not prev_blank:
                result.append("")
            result.append(line)
            prev_blank = False
            continue

        # 分隔线：前后保证空行
        if re.match(r"^[-*_]{3,}$", stripped):
            if not prev_blank:
                result.append("")
            result.append(line)
            prev_blank = False
            continue

        # 普通段落：如果前一行非空且不是空行，加空行分隔
        if not prev_blank:
            result.append("")
        result.append(line)
        prev_blank = False

    return "\n".join(result)


class FeishuStreamingCard:
    """飞书流式卡片"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.card_id: Optional[str] = None
        self.message_id: Optional[str] = None
        self._token: Optional[str] = None
        self._sequence = 0
        self._current_text = ""
        self._last_update_len = 0
        self._closed = False
        self.min_interval = 0.15
        self.min_new_chars = 15
        self._last_update_time = 0.0

    def _ensure_token(self) -> Optional[str]:
        """获取飞书 tenant_access_token（带缓存）"""
        cache_key = f"{self.app_id}:{self.app_secret[:8]}"
        now = time.time()
        cached = TOKEN_CACHE.get(cache_key)
        if cached and (now - cached[1]) < TOKEN_TTL:
            return cached[0]

        try:
            resp = httpx.post(
                f"{API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0 and data.get("tenant_access_token"):
                token = data["tenant_access_token"]
                TOKEN_CACHE[cache_key] = (token, now)
                self._token = token
                return token
            logger.error(f"Get token failed: {data.get('msg')}")
        except Exception as e:
            logger.error(f"Get token exception: {e}")
        return None

    def start(self, conversation_id: str) -> bool:
        """创建并发送思考中卡片"""
        token = self._ensure_token()
        if not token:
            return False

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

        try:
            resp = httpx.post(
                f"{API_BASE}/cardkit/v1/cards",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"type": "card_json", "data": json.dumps(card_json)},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0 or not data.get("data", {}).get("card_id"):
                logger.error(f"Create card failed: code={data.get('code')}, msg={data.get('msg')}")
                return False
            self.card_id = data["data"]["card_id"]
        except Exception as e:
            logger.error(f"Create card exception: {e}")
            return False

        card_content = json.dumps({"type": "card", "data": {"card_id": self.card_id}})
        receive_id_type = "open_id" if conversation_id.startswith("ou_") else "chat_id"

        try:
            resp = httpx.post(
                f"{API_BASE}/im/v1/messages?receive_id_type={receive_id_type}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"receive_id": conversation_id, "msg_type": "interactive", "content": card_content},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0 or not data.get("data", {}).get("message_id"):
                logger.error(f"Send card message failed: code={data.get('code')}, msg={data.get('msg')}")
                return False
            self.message_id = data["data"]["message_id"]
        except Exception as e:
            logger.error(f"Send card exception: {e}")
            return False

        logger.info(f"Thinking card OK: card_id={self.card_id}, msg_id={self.message_id}")
        return True

    def update(self, text: str) -> None:
        """更新卡片内容（仅流式场景使用）"""
        if self._closed or not self.card_id:
            return

        new_chars = len(text) - self._last_update_len
        now = time.time()
        elapsed = now - self._last_update_time

        if new_chars < self.min_new_chars and elapsed < self.min_interval:
            return

        self._sequence += 1
        token = self._token or self._ensure_token()
        if not token:
            return

        try:
            httpx.put(
                f"{API_BASE}/cardkit/v1/cards/{self.card_id}/elements/content/content",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"content": text, "sequence": self._sequence, "uuid": f"s_{self.card_id}_{self._sequence}"},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Update exception (seq={self._sequence}): {e}")

        self._current_text = text
        self._last_update_len = len(text)
        self._last_update_time = now

    def close(self, final_text: Optional[str] = None) -> None:
        """更新为最终回复并关闭流式模式"""
        if self._closed or not self.card_id:
            return
        self._closed = True

        text = final_text or self._current_text
        # 预处理 Markdown
        cleaned_text = _clean_markdown(text) if text else text
        token = self._token or self._ensure_token()
        if not token:
            return

        # 1. 更新内容元素为最终回复
        if cleaned_text and (len(cleaned_text) > self._last_update_len or final_text != self._current_text):
            self._sequence += 1
            try:
                httpx.put(
                    f"{API_BASE}/cardkit/v1/cards/{self.card_id}/elements/content/content",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"content": cleaned_text, "sequence": self._sequence, "uuid": f"s_{self.card_id}_{self._sequence}"},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"Final update exception: {e}")

        # 2. 更新 header + 关闭 streaming：通过 PATCH im/v1/messages 整体替换卡片
        # 这是唯一一次 im.message.patch，不涉及流式更新，没有限流问题
        self._sequence += 1
        final_card_json = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 AI 回复完成"},
                "template": "green",
            },
            "elements": [
                {"tag": "markdown", "content": cleaned_text or ""},
            ],
        }

        if self.message_id:
            try:
                import lark_oapi as lark
                from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
                client = (lark.Client.builder()
                    .app_id(self.app_id)
                    .app_secret(self.app_secret)
                    .timeout(30000)
                    .build())

                request = (PatchMessageRequest.builder()
                    .message_id(self.message_id)
                    .request_body(
                        PatchMessageRequestBody.builder()
                        .content(json.dumps(final_card_json))
                        .build()
                    )
                    .build())

                resp = client.im.v1.message.patch(request)
                if resp.success():
                    logger.info(f"Final card shown: card_id={self.card_id}, final_len={len(cleaned_text or '')}")
                else:
                    logger.warning(f"Patch final card failed: code={resp.code}, msg={resp.msg}")
            except Exception as e:
                logger.warning(f"Patch final card exception: {e}")

        # 3. 关闭 Card Kit streaming 模式（避免残留）
        try:
            settings = json.dumps({"config": {"streaming_mode": False}})
            httpx.patch(
                f"{API_BASE}/cardkit/v1/cards/{self.card_id}/settings",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"settings": settings, "sequence": self._sequence, "uuid": f"c_{self.card_id}_{self._sequence}"},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Close streaming fallback exception: {e}")

    def is_active(self) -> bool:
        return not self._closed and self.card_id is not None
