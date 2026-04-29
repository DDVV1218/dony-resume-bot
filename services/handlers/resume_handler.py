from __future__ import annotations

"""简历文件处理器

接收用户上传的 PDF 简历文件：
1. 校验为 PDF
2. 下载到 uploads
3. MinerU 解析为 Markdown
4. Markdown 存到 mineru_process
5. LLM 提取结构化信息（展示 + 入库）
6. 结果加入聊天上下文并回复
"""

import logging
import os
import shutil
from services.time_utils import shanghai_now, shanghai_time_str
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from services.handlers.base import BaseMessageHandler
from services.resume_indexer import index_resume
from services.llm_utils import StructuredOutput

if TYPE_CHECKING:
    from feishu.models import InboundMessage

logger = logging.getLogger(__name__)

# ============================================================
# Pydantic 模型：简历分析展示 + 入库元数据
# ============================================================


class ResumeMeta(BaseModel):
    """简历入库元数据 - 用于数据库索引"""
    name: str = ""
    sex: str = ""
    phone: str = ""
    email: str = ""
    undergraduate: Optional[str] = None
    master: Optional[str] = None
    doctor: Optional[str] = None
    skills: Optional[str] = None
    intership_comps: Optional[str] = None
    work_comps: Optional[str] = None


class ResumeAnalysis(BaseModel):
    """LLM 简历分析输出（结构化）

    包含 display（给用户看的 Markdown 文本）和
    meta（入库元数据）两部分，一次 LLM 调用完成。
    is_resume 标记内容是否为简历，非简历不进入简历库。
    """
    # 是否为简历（false 表示内容不是求职简历）
    is_resume: bool = False

    # 展示文本（LLM 直接输出美观的 Markdown）
    display: str = ""

    # 入库元数据
    name: str = ""
    sex: str = ""
    phone: str = ""
    email: str = ""
    undergraduate: Optional[str] = None
    master: Optional[str] = None
    doctor: Optional[str] = None
    skills: Optional[str] = None
    intership_comps: Optional[str] = None
    work_comps: Optional[str] = None

    def to_meta(self) -> ResumeMeta:
        """转换为入库元数据"""
        return ResumeMeta(
            name=self.name,
            sex=self.sex,
            phone=self.phone,
            email=self.email,
            undergraduate=self.undergraduate,
            master=self.master,
            doctor=self.doctor,
            skills=self.skills,
            intership_comps=self.intership_comps,
            work_comps=self.work_comps,
        )


# ============================================================
# Prompt
# ============================================================

RESUME_ANALYSIS_PROMPT = """你是一个简历分析助手。以下是一份文档的 Markdown 内容，请判断它是否为求职简历，并提取结构化信息。

请严格按以下规则输出 JSON：

is_resume: 布尔值。内容是否是一份求职简历（包含候选人姓名、求职意向、教育经历、工作/实习经历、技能等个人信息）。论文、合同、申请表、公司文件等非简历内容设为 false。

display: 如果 is_resume 为 true，用美观的 Markdown 格式展示候选人信息。示例格式：

### 👤 基本信息
- **姓名**：xxx
- **性别**：男/女
- **电话**：11位手机号
- **邮箱**：xxx

### 🎓 教育经历
- **本科**：xx大学 - xx专业
- **硕士**：xx大学 - xx专业
- **博士**：xx大学 - xx专业

### 💼 实习/工作经历
- xx公司 - xx岗位（时间）
- xx公司 - xx岗位（时间）

### 🛠 技能
Python, PyTorch, 机器学习, ...

如果 is_resume 为 false，display 输出：⚠️ 上传的文件内容不是求职简历，无法进入简历库。请确认上传的是 PDF 格式的简历文件。

使用 emoji 图标让展示更生动。没有的字段不显示。

以下是 JSON 字段说明：

name: 姓名
sex: 性别（男/女）
phone: 手机号（11位数字）
email: 邮箱
undergraduate: 本科学校
master: 硕士学校
doctor: 博士学校
skills: 技能列表（逗号分隔）
intership_comps: 实习公司列表（逗号分隔）
work_comps: 曾就职公司列表（逗号分隔）

如果某个字段缺失，填入空字符串或null。
"""


# ============================================================
# Handler
# ============================================================


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

            # 3. LLM 结构化提取
            analysis_messages = [
                {"role": "system", "content": RESUME_ANALYSIS_PROMPT},
                {"role": "user", "content": f"以下是一份简历的 Markdown 内容：\n\n{markdown}"},
            ]

            analysis = StructuredOutput.parse(
                model_class=ResumeAnalysis,
                messages=analysis_messages,
                config=self.config.analysis_agent,
                fallback_factory=lambda: ResumeAnalysis(
                    is_resume=False,
                    display="⚠️ 简历分析失败",
                ),
                retries=1,
                timeout=30.0,
                max_tokens=2048,
            )

            logger.info(f"Resume analysis: display={len(analysis.display)} chars")

            # === 判断是否为简历 ===
            display_text = analysis.display
            session_key = inbound.session_key
            session = self.session_store.get_or_create(session_key)

            if not analysis.is_resume:
                # 非简历：不入库、不追加上下文，直接回复提示
                reply = f"{display_text}"
                logger.info(f"Not a resume, skipping indexing. Reply: {reply[:50]}")
                if card and card.is_active():
                    card.close(reply)
                    return None
                return reply

            # === 简历入库索引 ===
            try:
                meta = analysis.to_meta()
                if meta.name:
                    index_resume(
                        name=meta.name,
                        sex=meta.sex or "未知",
                        phone=meta.phone or "",
                        email=meta.email or "",
                        undergraduate=meta.undergraduate,
                        master=meta.master,
                        doctor=meta.doctor,
                        skills=meta.skills,
                        intership_comps=meta.intership_comps,
                        work_comps=meta.work_comps,
                        full_text=markdown,
                        pdf_path=save_path,
                        markdown_path=os.path.join(
                            self.config.mineru_process_dir,
                            os.path.splitext(os.path.basename(save_path))[0] + ".md"
                        ) if os.path.exists(os.path.join(
                            self.config.mineru_process_dir,
                            os.path.splitext(os.path.basename(save_path))[0] + ".md"
                        )) else None,
                    )

                    # === 归档文件到简历库 ===
                    try:
                        # 确保归档目录存在
                        os.makedirs(self.config.resume_archive_pdf_dir, exist_ok=True)
                        os.makedirs(self.config.resume_archive_md_dir, exist_ok=True)

                        # 目标路径
                        pdf_filename = os.path.basename(save_path)
                        archive_pdf = os.path.join(self.config.resume_archive_pdf_dir, pdf_filename)
                        # 避免文件名冲突
                        if os.path.exists(archive_pdf):
                            base, ext = os.path.splitext(pdf_filename)
                            archive_pdf = os.path.join(
                                self.config.resume_archive_pdf_dir,
                                f"{base}_{meta.phone}{ext}"
                            )

                        if os.path.exists(save_path):
                            shutil.move(save_path, archive_pdf)
                            logger.info(f"PDF archived: {save_path} -> {archive_pdf}")

                            # 更新数据库中的 pdf_path
                            from services.db import get_connection
                            conn = get_connection()
                            conn.execute(
                                "UPDATE resumes SET pdf_path = ? WHERE name = ? AND sex = ? AND phone = ?",
                                (archive_pdf, meta.name, meta.sex or "未知", meta.phone)
                            )

                        # 归档 markdown
                        md_source = os.path.join(
                            self.config.mineru_process_dir,
                            os.path.splitext(pdf_filename)[0] + ".md"
                        )
                        if os.path.exists(md_source):
                            md_filename = os.path.splitext(pdf_filename)[0] + ".md"
                            archive_md = os.path.join(self.config.resume_archive_md_dir, md_filename)
                            if os.path.exists(archive_md):
                                base, ext = os.path.splitext(md_filename)
                                archive_md = os.path.join(
                                    self.config.resume_archive_md_dir,
                                    f"{base}_{meta.phone}{ext}"
                                )
                            shutil.move(md_source, archive_md)
                            logger.info(f"MD archived: {md_source} -> {archive_md}")

                            # 更新数据库中的 markdown_path
                            conn.execute(
                                "UPDATE resumes SET markdown_path = ? WHERE name = ? AND sex = ? AND phone = ?",
                                (archive_md, meta.name, meta.sex or "未知", meta.phone)
                            )
                            conn.commit()

                    except Exception as arc_err:
                        logger.warning(f"Resume archive failed (non-fatal): {arc_err}")
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
            session.messages.append({"role": "assistant", "content": display_text})
            session.updated_at = shanghai_now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)

            # 5. 更新卡片或返回文本
            reply = f"✅ 已收到简历文件「{file_name}」（{size_str}）\n\n{display_text}"

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
