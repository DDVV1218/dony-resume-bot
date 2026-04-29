from __future__ import annotations

"""简历文件处理器

接收用户上传的 PDF 简历文件：
1. 校验为 PDF
2. 下载到 uploads
3. MinerU 解析为 Markdown
4. Markdown 存到 mineru_process
5. LLM 提取结构化信息
6. 结果加入聊天上下文并回复
"""

import logging
import os
from services.time_utils import shanghai_now, shanghai_time_str
from typing import TYPE_CHECKING, Optional

from services.handlers.base import BaseMessageHandler
from services.llm import chat, prepare_context
from services.resume_indexer import index_resume

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)
def _extract_field(text: str, label: str) -> Optional[str]:
    """从分析文本中提取字段值，如 '- 姓名：郭星砚' -> '郭星砚'"""
    import re
    m = re.search(rf"[\-\*]\s*{label}[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        val = m.group(1).strip()
        if val not in ("无", "未知", ""):
            return val
    return None


def _extract_list_field(text: str, label: str) -> Optional[str]:
    """从分析文本中提取列表字段，如实习/就业公司名"""
    import re
    m = re.search(rf"[\-\*]\s*{label}[：:]\s*(.+?)(?:\n[\-\*]|\n\s*\n|$)", text, re.DOTALL)
    if m:
        val = m.group(1).strip()
        parts = re.split(r"[、，,]", val)
        companies = []
        for p in parts:
            p = p.strip()
            p = re.split(r"[做从负]", p)[0].strip()
            if p and p not in ("无", "未知"):
                companies.append(p)
        if companies:
            return ",".join(companies)
    return None


def _extract_phone(text: str) -> Optional[str]:
    """从文本中提取手机号"""
    import re
    m = re.search(r"1[3-9]\d{9}", text)
    if m:
        return m.group(0)
    return None

RESUME_ANALYSIS_PROMPT = """你是一个简历分析助手。以下是一份简历的完整内容（Markdown 格式），请提取关键信息并以以下格式输出：

- 姓名：xxx
- 年龄：xx 岁（如未提到则标注"无"）
- 教育经历：
  - 本科：xx大学
  - 硕士：xx大学
  - 博士：xx大学
- 实习经历：列出所有实习公司及岗位
- 就业经历：曾就职于xx公司、xx公司
- 人才特点：总结人才的技能栈和擅长的方向

如果某个字段信息缺失，直接省略该行（年龄缺失则标注"无"）。
"""


class ResumePDFHandler(BaseMessageHandler):
    """简历 PDF 处理器"""

    def can_handle(self, inbound: InboundMessage) -> bool:
        return inbound.message_type == "file"

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        if not inbound.file_key:
            return "⚠️ 无法识别文件内容，请重新发送"

        # 校验文件名：必须是 .pdf
        file_name = (inbound.file_name or "").lower()
        if not file_name.endswith(".pdf") and not file_name.endswith(".PDF"):
            return "⚠️ 暂只支持 PDF 格式的文件"

        conversation_id = inbound.conversation_id
        card = None

        # 显示思考卡片
        try:
            from feishu.streaming_card import FeishuStreamingCard
            card = FeishuStreamingCard(self.config.feishu_app_id, self.config.feishu_app_secret)
            if card.start(conversation_id):
                logger.info("Resume thinking card shown")
            else:
                card = None
        except Exception:
            card = None

        try:
            # 1. 下载 PDF
            save_dir = os.path.join(self.config.uploads_dir, inbound.sender_id or "unknown")
            from feishu.file_utils import download_file
            save_path = download_file(
                file_key=inbound.file_key,
                message_id=inbound.message_id,
                file_name_hint=inbound.file_name,
                app_id=self.config.feishu_app_id,
                app_secret=self.config.feishu_app_secret,
                save_dir=save_dir,
            )

            if not save_path:
                raise RuntimeError("Download failed")

            file_size = os.path.getsize(save_path)
            size_str = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
            logger.info(f"PDF saved: {save_path} ({size_str})")

            # 2. MinerU PDF → Markdown
            from services.pdf_processor import process_pdf
            markdown = process_pdf(save_path, self.config)

            if not markdown:
                raise RuntimeError("MinerU parsing failed")

            logger.info(f"Markdown extracted: {len(markdown)} chars")

            # 3. LLM 提取结构化信息
            session_key = inbound.session_key
            session = self.session_store.get_or_create(session_key)

            # 构建 resume analysis 上下文：先 system prompt + resume + 分析
            analysis_messages = [
                {"role": "system", "content": RESUME_ANALYSIS_PROMPT},
                {"role": "user", "content": f"以下是一份简历的 Markdown 内容：\n\n{markdown}"},
            ]
            analysis_context = prepare_context(analysis_messages, self.config)
            analysis = chat(analysis_context, self.config)

            if not analysis:
                raise RuntimeError("LLM analysis returned empty")

            logger.info(f"Resume analysis: {len(analysis)} chars")

            # === 简历入库索引 ===
            try:
                # 从分析文本中提取结构化字段
                idx_name = _extract_field(analysis, "姓名")
                idx_sex = _extract_field(analysis, "性别") or _extract_field(analysis, "年龄")
                # 从 markdown 中提取手机号（简单匹配）
                idx_phone = _extract_phone(markdown)
                idx_undergrad = _extract_field(analysis, "本科")
                idx_master = _extract_field(analysis, "硕士")
                idx_doctor = _extract_field(analysis, "博士")
                idx_intership = _extract_list_field(analysis, "实习经历")
                idx_work = _extract_list_field(analysis, "就业经历")
                idx_skills = _extract_list_field(analysis, "人才特点")

                # 构造 markdown 保存路径
                from pathlib import Path as PPath
                md_filename = PPath(save_path).stem + ".md"
                md_full_path = os.path.join(self.config.mineru_process_dir, md_filename) if os.path.exists(os.path.join(self.config.mineru_process_dir, md_filename)) else None

                if idx_name:
                    index_resume(
                        name=idx_name,
                        sex=idx_sex or "未知",
                        phone=idx_phone or "",
                        email="",
                        undergraduate=idx_undergrad,
                        master=idx_master,
                        doctor=idx_doctor,
                        skills=idx_skills,
                        intership_comps=idx_intership,
                        work_comps=idx_work,
                        full_text=markdown,
                        pdf_path=save_path,
                        markdown_path=md_full_path,
                    )
            except Exception as idx_err:
                logger.warning(f"Resume indexing skipped (non-fatal): {idx_err}")

            # 4. 将简历内容和分析结果加入聊天上下文
            time_prefix = f"你是图灵私募基金的HR简历助手。当前的时间是{shanghai_time_str()}。"
            system_content = time_prefix + "\n" + self.system_prompt
            session.messages = [m for m in session.messages if m.get("role") != "system"]
            session.messages.insert(0, {"role": "system", "content": system_content})
            session.messages.append({
                "role": "user",
                "content": f"[用户上传了简历文件：{file_name}（{size_str}）]\n\n简历内容：\n{markdown}",
            })
            session.messages.append({"role": "assistant", "content": analysis})
            session.updated_at = shanghai_now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 5. 更新卡片或返回文本
            reply = f"✅ 已收到简历文件「{file_name}」（{size_str}）\n\n{analysis}"

            if card and card.is_active():
                card.close(reply)
                return None
            return reply

        except Exception as e:
            logger.error(f"Resume processing failed: {e}")
            err_msg = f"⚠️ 简历处理失败：{str(e)[:100]}"
            if card and card.is_active():
                card.close(err_msg)
                return None
            return err_msg


class ResumeImageHandler(BaseMessageHandler):
    """简历图片处理器（桩）"""

    def can_handle(self, inbound: InboundMessage) -> bool:
        return False

    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return None
