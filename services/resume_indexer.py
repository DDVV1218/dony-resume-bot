"""简历入库索引器

将 LLM 分析后的结构化简历数据写入 SQLite + FTS5。
"""

import json
import logging
from datetime import datetime
from typing import Optional

import jieba

from services.db import get_connection
from services.time_utils import shanghai_now

logger = logging.getLogger(__name__)


def index_resume(
    name: str,
    sex: str,
    phone: str,
    email: Optional[str] = None,
    undergraduate: Optional[str] = None,
    master: Optional[str] = None,
    doctor: Optional[str] = None,
    skills: Optional[str] = None,
    intership_comps: Optional[str] = None,
    work_comps: Optional[str] = None,
    full_text: Optional[str] = None,
    pdf_path: Optional[str] = None,
    markdown_path: Optional[str] = None,
    chroma_id: Optional[str] = None,
) -> Optional[int]:
    """将结构化简历数据写入 SQLite + FTS5

    Args:
        name: 姓名
        sex: 性别
        phone: 手机号
        email: 邮箱
        undergraduate: 本科学校
        master: 硕士学校
        doctor: 博士学校
        skills: 技能，逗号分隔
        intership_comps: 实习公司，逗号分隔
        work_comps: 工作公司，逗号分隔
        full_text: 简历全文 Markdown
        pdf_path: PDF 文件绝对路径
        markdown_path: Markdown 文件绝对路径
        chroma_id: ChromaDB 中的 ID（留待 Embedding 模型部署后填充）

    Returns:
        新插入/更新记录的 id，失败返回 None
    """
    try:
        now = shanghai_now().isoformat()

        # 构建 metadata JSON
        metadata = {
            "name": name,
            "sex": sex,
            "phone": phone,
            "email": email,
            "undergraduate": undergraduate,
            "master": master,
            "doctor": doctor,
            "skills": skills,
            "intership_comps": intership_comps,
            "work_comps": work_comps,
        }

        conn = get_connection()

        # 1. UPSERT 主表
        conn.execute(
            """
            INSERT INTO resumes (name, sex, phone, email, metadata, chroma_id, pdf_path, markdown_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, sex, phone) DO UPDATE SET
                email = excluded.email,
                metadata = excluded.metadata,
                chroma_id = excluded.chroma_id,
                pdf_path = excluded.pdf_path,
                markdown_path = excluded.markdown_path,
                created_at = excluded.created_at
            """,
            [name, sex, phone, email, json.dumps(metadata, ensure_ascii=False),
             chroma_id, pdf_path, markdown_path, now],
        )

        # 获取 rowid（UPSERT 后最新的 rowid）
        row = conn.execute(
            "SELECT rowid FROM resumes WHERE name=? AND sex=? AND phone=?",
            [name, sex, phone],
        ).fetchone()
        if row is None:
            logger.error(f"Failed to get rowid after UPSERT: {name}/{sex}/{phone}")
            return False
        rowid = row[0]

        # 2. 更新 FTS5（先删旧记录，再插入新的）
        conn.execute("DELETE FROM resumes_fts WHERE rowid=?", [rowid])

        # jieba cut_for_search 分词（产生冗余切分，提高召回率）
        ft_tokens = _tokenize(full_text or "")
        name_tokens = _tokenize(name)
        school_tokens = _tokenize(" ".join(filter(None, [undergraduate, master, doctor])))
        skills_tokens = _tokenize(skills or "")
        comp_tokens = _tokenize(" ".join(filter(None, [intership_comps, work_comps])))

        conn.execute(
            """
            INSERT INTO resumes_fts (rowid, full_text, name, school, skills, company)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [rowid, ft_tokens, name_tokens, school_tokens, skills_tokens, comp_tokens],
        )

        conn.commit()
        logger.info(f"Indexed resume: id={rowid} ({name}/{sex}/{phone})")
        return rowid

    except Exception as e:
        logger.error(f"Failed to index resume {name}: {e}")
        return None


def _tokenize(text: str) -> str:
    """使用 jieba cut_for_search 分词，返回空格分隔的 token 字符串"""
    if not text or not text.strip():
        return ""
    return " ".join(jieba.cut_for_search(text))
