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


class PdfContentDetect(BaseModel):
    """PDF 内容类型检测

    判断一份 PDF 是单人简历、多人简历还是非简历内容。
    """
    # 内容类型: single_resume / multi_resume / not_resume
    content_type: str = "not_resume"
    # 候选人总数（multi_resume 时有效）
    candidate_count: int = 0
    # 候选人姓名列表（multi_resume 时有效）
    candidate_names: list = []


class ResumeAnalysis(BaseModel):
    """LLM 简历分析输出（结构化）

    包含 display（给用户看的 Markdown 文本）、
    meta（入库元数据）和 sections（段落文本，用于向量索引）三部分。
    is_resume 标记内容是否为简历，非简历不进入简历库。
    """
    # 是否为简历（false 表示内容不是求职简历）
    is_resume: bool = False

    # 展示文本（LLM 直接输出美观的 Markdown）
    display: str = ""

    # 段落文本（用于生成向量索引，Embedding 用）
    sections: dict = {
        "education": "",
        "experience": "",
        "skills": "",
    }

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

CONTENT_DETECT_PROMPT = """你是一个文档内容检测助手。以下是一份文档的 Markdown 内容，请判断它包含什么。

规则：
- 如果内容是一份或多份求职简历（包含候选人姓名、教育经历、工作/实习经历、技能等），content_type 为 "single" 或 "multi"
- 如果不包含任何简历（如论文、合同、周报等），content_type 为 "not_resume"
- 如果是简历，判断是单人("single")还是多人("multi")
- 如果是多人简历，列出所有候选人的姓名

请严格按 JSON 格式输出：
content_type: "single" / "multi" / "not_resume"
candidate_count: 整数
candidate_names: 姓名列表，如 ["张三", "李四"]
"""

RESUME_ANALYSIS_PROMPT = """你是一个简历分析助手。以下是一份文档的 Markdown 内容，请判断它是否为求职简历，并提取结构化信息。

请严格按以下规则输出 JSON：

is_resume: 布尔值。内容是否是一份求职简历（包含候选人姓名、求职意向、教育经历、工作/实习经历、技能等个人信息）。论文、合同、申请表、公司文件等非简历内容设为 false。

display: 如果 is_resume 为 true，用美观的 Markdown 格式展示候选人信息。如果 is_resume 为 false，display 输出：⚠️ 上传的文件内容不是求职简历。

sections: 字典，包含三个段落的纯文本，用于生成向量索引：
  education: 教育背景的纯文本。包含学校、专业、学位、时间等信息。
  experience: 项目经历（含实习、工作、项目、学术成果）的纯文本。包含公司、岗位、项目名称、学术成果、职责描述等信息。
  skills: 技能的纯文本。包含编程语言、工具、专业能力等信息。
  示例：{"education": "复旦大学金融硕士 2024", "experience": "灵均投资量化实习 因子回测; A股多因子选股模型项目; 某学术论文", "skills": "Python SQL 机器学习"}。无内容则空字符串。

字段说明（请提取精确值，不要用模糊描述）：
name: 姓名（全名）
sex: 性别（男/女）
phone: 手机号（完整11位）
email: 邮箱（完整地址）
undergraduate: 本科学校（全称，如"复旦大学"）
master: 硕士学校（全称，没有则null）
doctor: 博士学校（全称，没有则null）
skills: 技能列表（精确提取，逗号分隔，如"Python, SQL, 机器学习"）
intership_comps: 实习公司列表（提取精确公司全称，逗号分隔，如"杭州长花龙雪信息技术有限公司, 上海千象资产管理有限公司"）
work_comps: 曾就职公司列表（提取精确公司全称，逗号分隔，没有则null）
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

            # === 内容类型检测（单人/多人/非简历） ===
            session_key = inbound.session_key
            session = self.session_store.get_or_create(session_key)

            detect = self._detect_content_type(markdown)

            if detect.content_type == "not_resume":
                reply = "⚠️ 上传的文件内容不是求职简历，无法进入简历库。请确认上传的是 PDF 格式的简历文件。"
                logger.info(f"Not a resume, skipping. Reply: {reply[:50]}")
                if card and card.is_active():
                    card.close(reply)
                    return None
                return reply

            if detect.content_type == "single":
                # === 单人简历：正常分析 + 入库 + 展示 ===
                analysis = self._analyze_single(markdown)

                if not analysis.is_resume:
                    reply = f"{analysis.display}"
                    if card and card.is_active():
                        card.close(reply)
                        return None
                    return reply

                reply = self._process_and_index(
                    markdown, analysis, save_path, file_name, file_size, session, inbound, config=self.config
                )
            else:
                # === 多人简历：每人分析 + 入库，但只返回摘要 ===
                logger.info(f"Multi-resume detected: {detect.candidate_count} people: {detect.candidate_names}")

                indexed_names = []
                for name in detect.candidate_names:
                    try:
                        person_analysis = self._analyze_person(markdown, name)
                        if person_analysis and person_analysis.is_resume:
                            person_reply = self._process_and_index(
                                markdown, person_analysis, save_path, file_name, file_size,
                                session, inbound, config=self.config, silent=True,
                            )
                            indexed_names.append(name)
                            logger.info(f"Multi: indexed {name}")
                        else:
                            logger.warning(f"Multi: failed to index {name}")
                    except Exception as e:
                        logger.warning(f"Multi: error indexing {name}: {e}")

                if indexed_names:
                    reply = f"✅ 已入库 {len(indexed_names)} 人：{'、'.join(indexed_names)}"
                else:
                    reply = "⚠️ 未能从文件中提取出有效的简历信息"

            # === 更新飞书卡片 ===
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

    # ============================================================
    # Content Type Detection
    # ============================================================

    def _detect_content_type(self, markdown: str):
        """检测 PDF 内容类型"""
        messages = [
            {"role": "system", "content": CONTENT_DETECT_PROMPT},
            {"role": "user", "content": f"以下是一份文档的 Markdown 内容：\n\n{markdown}"},
        ]
        return StructuredOutput.parse(
            model_class=PdfContentDetect,
            messages=messages,
            config=self.config.analysis_agent,
            fallback_factory=lambda: PdfContentDetect(content_type="not_resume"),
            retries=1,
            timeout=20.0,
            max_tokens=512,
        )

    def _analyze_single(self, markdown: str):
        """分析单人简历"""
        return self._analyze_person(markdown, None)

    def _analyze_person(self, markdown: str, person_name: Optional[str] = None):
        """分析简历中的一个人"""
        if person_name:
            user_msg = f"以下是一份包含多份简历的文档。请只提取 **{person_name}** 的信息，忽略其他人的内容。\n\n文档内容：\n\n{markdown}"
        else:
            user_msg = f"以下是一份简历的 Markdown 内容：\n\n{markdown}"

        messages = [
            {"role": "system", "content": RESUME_ANALYSIS_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        return StructuredOutput.parse(
            model_class=ResumeAnalysis,
            messages=messages,
            config=self.config.analysis_agent,
            fallback_factory=lambda: ResumeAnalysis(
                is_resume=False,
                display="⚠️ 简历分析失败",
            ),
            retries=1,
            timeout=30.0,
            max_tokens=2048,
        )

    def _process_and_index(
        self, markdown: str, analysis, save_path: str, file_name: str, file_size: str,
        session, inbound, config, silent: bool = False,
    ) -> str:
        """入库索引 + 归档，返回展示文本"""
        from services.db import get_connection

        display_text = analysis.display
        meta = analysis.to_meta()
        resume_id = None

        try:
            if meta.name:
                resume_id = index_resume(
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
                    markdown_path=None,
                )

                # === 生成向量索引 ===
                if resume_id is not None:
                    try:
                        from services.vector_indexer import index_resume_vectors
                        index_resume_vectors(
                            resume_id=resume_id,
                            full_text=markdown,
                            sections=analysis.sections,
                            config=config,
                        )
                    except Exception as vec_err:
                        logger.warning(f"Vector indexing failed: {vec_err}")

                # === 归档文件 ===
                try:
                    os.makedirs(config.resume_archive_pdf_dir, exist_ok=True)
                    os.makedirs(config.resume_archive_md_dir, exist_ok=True)

                    pdf_filename = os.path.basename(save_path)
                    archive_pdf = os.path.join(config.resume_archive_pdf_dir, pdf_filename)
                    if os.path.exists(archive_pdf):
                        base, ext = os.path.splitext(pdf_filename)
                        archive_pdf = os.path.join(config.resume_archive_pdf_dir, f"{base}_{meta.phone}{ext}")

                    if os.path.exists(save_path) and resume_id is not None:
                        shutil.move(save_path, archive_pdf)
                        conn = get_connection()
                        conn.execute("UPDATE resumes SET pdf_path = ? WHERE id = ?", (archive_pdf, resume_id))

                    md_source = os.path.join(config.mineru_process_dir, os.path.splitext(pdf_filename)[0] + ".md")
                    if os.path.exists(md_source) and resume_id is not None:
                        md_filename = os.path.splitext(pdf_filename)[0] + ".md"
                        archive_md = os.path.join(config.resume_archive_md_dir, md_filename)
                        if os.path.exists(archive_md):
                            base, ext = os.path.splitext(md_filename)
                            archive_md = os.path.join(config.resume_archive_md_dir, f"{base}_{meta.phone}{ext}")
                        shutil.move(md_source, archive_md)
                        conn = get_connection()
                        conn.execute("UPDATE resumes SET markdown_path = ? WHERE id = ?", (archive_md, resume_id))
                        conn.commit()
                except Exception as arc_err:
                    logger.warning(f"Archive failed: {arc_err}")
        except Exception as idx_err:
            logger.warning(f"Indexing skipped: {idx_err}")

        if not silent:
            # 将简历内容加入聊天上下文
            time_prefix = f"你是图灵私募基金的HR简历助手。当前的时间是{shanghai_time_str()}。"
            system_content = time_prefix + "\n" + self.system_prompt
            session.messages = [m for m in session.messages if m.get("role") != "system"]
            session.messages.insert(0, {"role": "system", "content": system_content})
            session.messages.append({
                "role": "user",
                "content": f"[用户上传了简历文件：{file_name}（{file_size}）]\n\n简历内容：\n{markdown}",
            })
            session.messages.append({"role": "assistant", "content": display_text})
            session.updated_at = shanghai_now().isoformat()
            self.session_store._save_session(self.session_store._user_dir(session_key), session)

            return f"✅ 已收到简历文件「{file_name}」（{file_size}）\n\n{display_text}"
        else:
            return display_text


class ResumeImageHandler(BaseMessageHandler):
    """简历图片处理器（桩）"""
    def can_handle(self, inbound: InboundMessage) -> bool:
        return False
    def handle(self, inbound: InboundMessage) -> Optional[str]:
        return None
