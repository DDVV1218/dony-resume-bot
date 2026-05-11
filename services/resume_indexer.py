"""简历入库索引器

将 LLM 分析后的结构化简历数据写入 SQLite。
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional

from services.db import get_connection
from services.time_utils import shanghai_now

logger = logging.getLogger(__name__)


def _normalize_phone(phone: str) -> str:
    """格式化手机号

    中国大陆手机号：去空格、去连字符、去 +86，保留末 11 位数字
    境外号码（括号内非 86）：保持原样不变
    """
    if not phone:
        return phone

    # 检测括号内的国家码
    m = re.search(r"\(\s*(\+?\d+)\s*\)", phone)
    if m:
        country_code = m.group(1).lstrip("+")
        if country_code != "86":
            # 括号内非 86 → 境外号码，保持原样
            return phone

    # 中国大陆号码：去空格、连字符、括号、点
    cleaned = re.sub(r"[\s\-\.\(\)]", "", phone)
    # 去 +86/86 前缀
    if cleaned.startswith("+86"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("86"):
        cleaned = cleaned[2:]
    # 只保留数字
    digits = re.sub(r"\D", "", cleaned)
    # 取最后 11 位
    if len(digits) >= 11:
        digits = digits[-11:]
    return digits


def index_resume(
    name: str,
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
    """将结构化简历数据写入 SQLite

    Args:
        name: 姓名
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

        # 格式化手机号：去空格、去 +86、去连字符，保留 11 位
        phone_clean = _normalize_phone(phone or "")

        # 构建 metadata JSON（metadata 里也存格式化后的电话）
        metadata = {
            "name": name,
            "phone": phone_clean,
            "email": email,
            "undergraduate": undergraduate,
            "master": master,
            "doctor": doctor,
            "skills": skills,
            "intership_comps": intership_comps,
            "work_comps": work_comps,
        }

        conn = get_connection()

        # 1. UPSERT 主表（使用格式化后的电话做 UNIQUE 约束）
        conn.execute(
            """
            INSERT INTO resumes (name, phone, email, metadata, chroma_id, pdf_path, markdown_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, phone) DO UPDATE SET
                email = excluded.email,
                metadata = excluded.metadata,
                chroma_id = excluded.chroma_id,
                pdf_path = excluded.pdf_path,
                markdown_path = excluded.markdown_path,
                created_at = excluded.created_at
            """,
            [name, phone_clean, email, json.dumps(metadata, ensure_ascii=False),
             chroma_id, pdf_path, markdown_path, now],
        )

        # 获取 rowid（UPSERT 后最新的 rowid）
        row = conn.execute(
            "SELECT rowid FROM resumes WHERE name=? AND phone=?",
            [name, phone_clean],
        ).fetchone()
        if row is None:
            logger.error(f"Failed to get rowid after UPSERT: {name}/{phone}")
            return False
        rowid = row[0]

        conn.commit()
        logger.info(f"Indexed resume: id={rowid} ({name}/{phone_clean})")
        return rowid

    except Exception as e:
        logger.error(f"Failed to index resume {name}: {e}")
        return None

