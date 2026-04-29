"""文字消息处理器 - LLM 对话

将原 _process_message 中的 LLM 对话逻辑迁移到此处理器。
"""

from __future__ import annotations

import logging
from services.time_utils import shanghai_now, shanghai_time_str
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler
from services.resume_searcher import search_resumes, merge_results
from services.keyword_extractor import extract_keywords

if TYPE_CHECKING:
    from feishu.models import InboundMessage
from services.llm import prepare_context, chat

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
        card = None

        try:
            session = self.session_store.get_or_create(session_key)
            logger.info(f"Session loaded, {len(session.messages)} messages")

            # 确保 system message 在第一条且唯一（带动态时间前缀）
            time_prefix = f"你是图灵私募基金的HR简历助手。当前的时间是{shanghai_time_str()}。"
            system_content = time_prefix + "\n" + self.system_prompt
            system_msg = {"role": "system", "content": system_content}
            session.messages = [m for m in session.messages if m.get("role") != "system"]
            session.messages.insert(0, system_msg)
            self.session_store._save_session(
                self.session_store._user_dir(session_key), session
            )

            # 追加用户消息
            session.messages.append({"role": "user", "content": text})
            session.updated_at = shanghai_now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)
            logger.info(f"User message appended, {len(session.messages)} total")

            # 立刻发出思考中卡片
            from feishu.streaming_card import FeishuStreamingCard
            card = FeishuStreamingCard(self.config.feishu_app_id, self.config.feishu_app_secret)
            if card.start(conversation_id):
                logger.info("Thinking card shown")
            else:
                card = None
                logger.warning("Failed to show thinking card, proceeding without card")

            # === 简历检索 ===
            import concurrent.futures
            search_context = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                kw_future = executor.submit(extract_keywords, text, self.config)
                try:
                    keywords = kw_future.result(timeout=8.0)
                    if keywords:
                        fts_results = search_resumes(keywords, max_results=5)
                        vector_results = []  # 占位，待 Embedding 模型部署
                        merged = merge_results(fts_results, vector_results, top_k=5)
                        if merged:
                            summary_lines = []
                            for r in merged:
                                md = r.get("metadata_dict", {})
                                name = md.get("name", r.get("name", "未知"))
                                school = " | ".join(filter(None, [
                                    md.get("undergraduate", ""),
                                    md.get("master", ""),
                                    md.get("doctor", ""),
                                ]))
                                comps = " | ".join(filter(None, [
                                    md.get("intership_comps", ""),
                                    md.get("work_comps", ""),
                                ]))
                                skills = md.get("skills", "")
                                summary_lines.append(
                                    f"  - {name} | 学校: {school or '未知'} | 公司: {comps or '未知'} | 技能: {skills or '未知'}"
                                )
                            search_context = (
                                "[以下是简历库检索结果，请根据用户问题判断是否需要引用这些信息]\n"
                                + "\n".join(summary_lines)
                            )
                            logger.info(f"Search results injected ({len(merged)} candidates)")
                except concurrent.futures.TimeoutError:
                    logger.warning("Keyword extraction timed out, skipping search")
                except Exception as search_err:
                    logger.warning(f"Search failed (non-fatal): {search_err}")

            if search_context:
                # 将检索结果注入会话上下文（插入在 system 和当前用户消息之间）
                session.messages.insert(-1, {"role": "system", "content": search_context})
                self.session_store._save_session(
                    self.session_store._user_dir(session_key), session
                )

            # prepare context
            context = prepare_context(session.messages, self.config)

            # 调用 LLM（非流式）
            reply = chat(context, self.config)
            logger.info(f"LLM replied ({len(reply)} chars)")

            # 追加助手回复并保存
            if reply:
                session.messages.append({"role": "assistant", "content": reply})
                session.updated_at = shanghai_now().isoformat()
                self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 更新/发送最终回复
            if reply:
                if card and card.is_active():
                    card.close(reply)
                    return None  # 卡片已更新
                # 卡片失效，直接发文字
                from feishu.messages import send_text
                send_text(conversation_id, reply, self.config)
                return None

            return reply

        except Exception as e:
            logger.error(f"LLM chat failed for {session_key}: {e}")
            # 如果有卡片，更新为错误信息
            if card and card.is_active():
                card.close(f"⚠️ 处理失败：{e}")
            return None


