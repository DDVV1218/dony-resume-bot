"""文字消息处理器 - Agent 驱动

文字消息通过 AgentLoop 驱动：LLM 自主决策是否搜索简历库。
搜索、聊天等行为由 LLM 通过原生 tools 参数决定，不再由代码硬编码。
"""

from __future__ import annotations

import logging
from services.time_utils import shanghai_now, shanghai_time_str
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)


class TextHandler(BaseMessageHandler):
    """文字消息处理器

    通过 AgentLoop 驱动 LLM：
    - 自动决定是否调用 search_resumes 工具
    - 自动将检索结果纳入上下文
    - 安全阀限制最大工具调用轮数
    """

    def can_handle(self, inbound: InboundMessage) -> bool:
        return inbound.message_type in ("text", "post") and inbound.text is not None

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

            # 显示思考卡片
            from feishu.streaming_card import FeishuStreamingCard
            card = FeishuStreamingCard(self.config.feishu_app_id, self.config.feishu_app_secret)
            if card.start(conversation_id):
                logger.info("Thinking card shown")
            else:
                card = None
                logger.warning("Failed to show thinking card, proceeding without card")

            # === AgentLoop 驱动 ===
            from services.agent_loop import AgentLoop
            from services.tools.search_resumes import SearchResumesTool
            from services.tools.send_resume_pdf import SendResumePDFTool
            from services.tools.query_resume_db import QueryResumeDBTool

            agent_loop = AgentLoop(
                config=self.config.chat_agent,
                tools=[
                    SearchResumesTool(),
                    SendResumePDFTool(
                        app_id=self.config.feishu_app_id,
                        app_secret=self.config.feishu_app_secret,
                        conversation_id=conversation_id,
                    ),
                    QueryResumeDBTool(),
                ],
            )

            # 工具回调：更新卡片显示当前工具状态
            def _on_tool_start(name: str, args: dict) -> None:
                if card and card.is_active():
                    if name == "search_resumes":
                        query = args.get("query", "")
                        card_text = f"🔍 正在搜索简历库...\n\n查询：{query}"
                    else:
                        card_text = f"⚙️ 正在调用 {name}..."
                    card.update(card_text)
                    logger.info(f"Card updated: tool={name} start")

            def _on_tool_end(name: str, args: dict, result) -> None:
                if name == "search_resumes" and card and card.is_active():
                    data = result.data or {}
                    total = data.get("total_found", 0)
                    card_text = (
                        f"🔍 搜索完成，找到 {total} 份简历\n\n---\n\n"
                        f"⏳ AI 正在根据搜索结果生成回复..."
                    )
                    card.update(card_text)
                    logger.info(f"Card updated: tool={name} done, found={total}")
                elif name == "query_resume_db" and card and card.is_active():
                    card_text = f"📊 正在查询数据库..."
                    card.update(card_text)
                elif name == "send_resume_pdf" and card and card.is_active():
                    if result.success:
                        msg = (result.data or {}).get("message", "")
                        card_text = f"📄 处理完成\n\n{msg}"
                    else:
                        card_text = f"⚠️ 发送失败\n\n{result.error}"
                    card.update(card_text)
                    logger.info(f"Card updated: tool={name} done")

            # 运行 Agent 循环（LLM 自主决策）
            # 直接传入 session.messages，AgentLoop 在同一个列表上追加工具调用历史
            reply, _ = agent_loop.run(
                session.messages,
                verbose=False,
                on_tool_start=_on_tool_start,
                on_tool_end=_on_tool_end,
            )
            logger.info(f"AgentLoop replied ({len(reply)} chars), session={len(session.messages)} msgs")

            # AgentLoop 已就地追加工具调用消息到 session.messages
            # 追加最终助手回复
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
            logger.error(f"AgentLoop failed for {session_key}: {e}")
            if card and card.is_active():
                card.close(f"⚠️ 处理失败：{e}")
            return None
