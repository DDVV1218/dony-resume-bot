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
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
    phone: str = ""
    email: str = ""
    undergraduate: Optional[str] = None
    master: Optional[str] = None
    doctor: Optional[str] = None
    skills: Optional[str] = None
    intership_comps: Optional[str] = None
    work_comps: Optional[str] = None


class PageCheck(BaseModel):
    """判断一页/一段是否是新的简历的开始"""
    is_new_resume: bool = False
    person_name: str = ""


class BatchPageCheck(BaseModel):
    """批量判断：每段是否是新的简历的开始"""
    results: list = []
    # results: [{"is_new_resume": true, "person_name": "张三"}, ...]


class PageCheck(BaseModel):
    """判断一页/一段是否是新的简历的开始"""
    is_new_resume: bool = False
    person_name: str = ""
    # 如果是新简历的开始，person_name 填入该人姓名


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

PAGE_CHECK_PROMPT = """你是一个简历页面检测助手。给你一份文档的多个页面，每个页面可能是一份简历的开头或中间部分。

规则：
- 逐页判断每个页面是否是**一份新简历的开始**
- 新简历开始的标志：页面开头包含一个人名（姓名通常在页面顶部，可能是标题位置）
- 不是新简历开始的标志：页面以"教育经历"、"项目经历"、"专业技能"、"工作经历"、"实习经历"等章节标题开头，且开头没有出现新人名
- 对于每个页面，输出：
  is_new_resume: true/false
  person_name: 如果是新简历开始且有人名，填入该人姓名；否则填空字符串
- 必须为所有页面都输出结果，保持顺序
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

字段说明（提取精确值，并统一规范为标准化格式）：

### 规范化规则（必须严格遵守）

**候选人姓名**（name）：
- 只保留中文姓名，禁止中英文混杂
- 如果候选人同时有中文名和英文名（如"吴伟婷Vicky"或"Vicky Wu"），只取中文名"吴伟婷"
- 示例："吴伟婷Vicky"→"吴伟婷"、"赵一全 Jack"→"赵一全"、"陈田森Tom"→"陈田森"
- 如果只有英文名没有中文名，保留英文名

**学校名称**（undergraduate / master / doctor）：
- 必须还原为**官方全称**，不允许使用简称
- 示例：清华→清华大学、复旦→复旦大学、北大→北京大学、浙大→浙江大学、上海交大→上海交通大学、西安交大→西安交通大学、华科→华中科技大学、武大→武汉大学、南大→南京大学、中科大→中国科学技术大学、上财→上海财经大学、央财→中央财经大学
- 海外学校使用官方中文译名（如 MIT→麻省理工学院）

**公司名称**（intership_comps / work_comps）：
- 必须是公司完整注册全称，不允许用简称或模糊描述
- 示例："蚂蚁集团"→"蚂蚁科技集团股份有限公司"、"字节"→"北京字节跳动科技有限公司"
- 如果不确定精确全称，输出你能确认的最完整名称
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

            # === 逐页切分 + 分组 ===
            # 读取每页前 300 字符，LLM 批量判断每页是否是新简历开始
            session_key = inbound.session_key
            session = self.session_store.get_or_create(session_key)

            candidates = self._split_into_candidates(markdown, pdf_source=save_path)

            if not candidates:
                reply = "⚠️ 上传的文件内容不是求职简历，无法进入简历库。请确认上传的是 PDF 格式的简历文件。"
                logger.info(f"No candidates found, skipping. Reply: {reply[:50]}")
                if card and card.is_active():
                    card.close(reply)
                    return None
                return reply

            logger.info(f"Resume candidates: {[c.get('name', '') for c in candidates]}")

            pdf_basename = os.path.splitext(os.path.basename(save_path))[0]
            os.makedirs(self.config.resume_archive_md_dir, exist_ok=True)

            indexed_names = []
            for cand in candidates:
                name = cand.get("name", "")
                person_text = cand.get("text", "")
                if not name or not person_text:
                    continue

                try:
                    # 落盘为独立 markdown 文件
                    safe_name = name.replace("/", "_").replace("\\", "_")
                    md_filename = f"{pdf_basename}_{safe_name}.md"
                    md_path = os.path.join(self.config.resume_archive_md_dir, md_filename)
                    with open(md_path, "w", encoding="utf-8") as f:
                        f.write(person_text)
                    logger.info(f"Person markdown saved: {md_path}")

                    # 3. 分析（传入完整文本）
                    person_analysis = self._analyze_person(person_text, name)

                    if person_analysis and person_analysis.is_resume:
                        # 4. 入库索引
                        self._process_and_index(
                            person_text, person_analysis, save_path, file_name, file_size,
                            session, inbound, config=self.config, silent=True,
                            person_md_path=md_path,
                        )
                        indexed_names.append(name)
                        logger.info(f"Indexed: {name}")
                    else:
                        logger.warning(f"Failed to index {name}: not a valid resume")

                except Exception as e:
                    logger.warning(f"Error processing {name}: {e}")

            # PDF 归档（只做一次）+ 更新 DB 中的路径
            try:
                os.makedirs(self.config.resume_archive_pdf_dir, exist_ok=True)
                archive_pdf = os.path.join(self.config.resume_archive_pdf_dir, os.path.basename(save_path))
                if os.path.exists(save_path):
                    shutil.move(save_path, archive_pdf)
                    logger.info(f"PDF archived: {save_path} -> {archive_pdf}")
                    # 更新所有指向旧路径的记录的 pdf_path
                    from services.db import get_connection
                    conn = get_connection()
                    conn.execute("UPDATE resumes SET pdf_path = ? WHERE pdf_path = ?", (archive_pdf, save_path))
                    conn.commit()
            except Exception as arc_err:
                logger.warning(f"PDF archive failed: {arc_err}")

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

    def _split_into_candidates(self, markdown: str, pdf_source: Optional[str] = None) -> list:
        """按 PDF 页面切分，LLM 判断每页归属，然后分组拼接"""
        # 1. 尝试从 mineru_process 目录加载每页 markdown
        page_texts = []
        if pdf_source:
            try:
                pdf_basename = os.path.splitext(os.path.basename(pdf_source))[0]
                process_dir = Path(self.config.mineru_process_dir)
                # 找匹配的 _pages/ 目录
                pages_dirs = list(process_dir.glob(f"{pdf_basename}*_pages"))
                if pages_dirs:
                    pages_dir = max(pages_dirs, key=lambda d: d.stat().st_mtime)  # 最新的（按修改时间）
                    page_files = sorted(pages_dir.glob("page_*.md"))
                    for pf in page_files:
                        page_texts.append(pf.read_text(encoding="utf-8"))
                    logger.info(f"Loaded {len(page_texts)} pages from {pages_dir}")
            except Exception as e:
                logger.warning(f"Failed to load per-page data: {e}")

        # 2. 如果没有 per-page 数据（如 PyMuPDF 路径），回退到按标题切分
        if not page_texts:
            logger.info("No per-page data, falling back to heading-based splitting")
            blocks = []
            current_block = []
            for line in markdown.split("\n"):
                if line.startswith("# ") and current_block:
                    blocks.append("\n".join(current_block))
                    current_block = [line]
                else:
                    current_block.append(line)
            if current_block:
                blocks.append("\n".join(current_block))
            if not blocks:
                return []
            page_texts = blocks

        # 3. 并发判断：拆分成多组，每组一次 LLM 调用
        total = len(page_texts)
        num_workers = min(4, total)
        chunk_size = (total + num_workers - 1) // num_workers

        def _check_chunk(chunk_pages: list, start_idx: int) -> list:
            """判断一组页面中每页是否是新简历开始"""
            segments = []
            for i, pt in enumerate(chunk_pages):
                # 简单的头 500 字预览（PyMuPDF 已按视觉从上到下提取，姓名一定在开头）
                preview = pt[:500].replace("\n", " ").strip()
                segments.append(f"第{start_idx + i + 1}页: {preview}")
            input_text = "\n\n".join(segments)

            messages = [
                {"role": "system", "content": PAGE_CHECK_PROMPT},
                {"role": "user", "content": f"以下文档有 {len(chunk_pages)} 页，请逐页判断是否是新简历的开始：\n\n{input_text}"},
            ]
            result = StructuredOutput.parse(
                model_class=BatchPageCheck,
                messages=messages,
                config=self.config.analysis_agent,
                fallback_factory=lambda: BatchPageCheck(results=[]),
                retries=1,
                timeout=30.0,
                max_tokens=65536,
            )
            r = result.results
            # 补全或截断
            if len(r) > len(chunk_pages):
                r = r[:len(chunk_pages)]
            while len(r) < len(chunk_pages):
                r.append({"is_new_resume": False, "person_name": ""})
            return r

        # 分组建任务
        chunks = []
        for i in range(0, total, chunk_size):
            chunks.append((page_texts[i:i + chunk_size], i))

        results = [None] * total
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_check_chunk, pages, start): start
                for pages, start in chunks
            }
            for fut in as_completed(futures):
                start = futures[fut]
                chunk_results = fut.result()
                for j, r in enumerate(chunk_results):
                    if start + j < total:
                        results[start + j] = r

        # 4. 根据 is_new_resume 分组
        candidates = []
        current_person = None
        current_text = []

        for pt, check in zip(page_texts, results):
            is_new = check.get("is_new_resume", False)
            pname = check.get("person_name", "")

            if is_new:
                # 如果名字和上一个人相同 → 是页眉同名导致的误判，合并为续页
                if pname and current_person and pname == current_person:
                    current_text.append(pt)
                else:
                    if current_person is not None and current_text:
                        candidates.append({"name": current_person, "text": "\n".join(current_text)})
                    current_person = pname if pname else ""
                    current_text = [pt]
            else:
                current_text.append(pt)

        if current_person is not None and current_text:
            candidates.append({"name": current_person, "text": "\n".join(current_text)})

        # 5. 过滤掉纯英文名的候选人（双语简历的英文版，国内招聘不需要）
        before = len(candidates)
        candidates = [c for c in candidates if re.search(r'[\u4e00-\u9fff]', c.get("name", ""))]
        dropped = before - len(candidates)
        if dropped:
            logger.info(f"Dropped {dropped} English-name candidate(s) (bilingual resume)")

        logger.info(f"Page split: {len(page_texts)} pages -> {len(candidates)} candidates: {[c['name'] for c in candidates]}")
        return candidates

    
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
            max_tokens=65536,
        )

    def _process_and_index(
        self, markdown: str, analysis, save_path: str, file_name: str, file_size: str,
        session, inbound, config, silent: bool = False, person_md_path: Optional[str] = None,
    ) -> str:
        """入库索引 + 归档，返回展示文本"""
        from services.db import get_connection

        display_text = analysis.display
        meta = analysis.to_meta()
        # 清洗：LLM 可能输出字符串 "null" 作为字段值
        if meta.phone == "null":
            meta.phone = ""
        if meta.email == "null":
            meta.email = ""
        resume_id = None

        try:
            if meta.name:
                resume_id = index_resume(
                    name=meta.name,
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
                    markdown_path=person_md_path,
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
                    if not silent:
                        os.makedirs(config.resume_archive_pdf_dir, exist_ok=True)
                    os.makedirs(config.resume_archive_md_dir, exist_ok=True)

                    pdf_filename = os.path.basename(save_path)
                    if not silent:
                        archive_pdf = os.path.join(config.resume_archive_pdf_dir, pdf_filename)
                        if os.path.exists(archive_pdf):
                            base, ext = os.path.splitext(pdf_filename)
                            archive_pdf = os.path.join(config.resume_archive_pdf_dir, f"{base}_{meta.phone}{ext}")

                        if os.path.exists(save_path) and resume_id is not None:
                            shutil.move(save_path, archive_pdf)
                            conn = get_connection()
                            conn.execute("UPDATE resumes SET pdf_path = ? WHERE id = ?", (archive_pdf, resume_id))

                    if person_md_path:
                        md_source = person_md_path
                    else:
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
