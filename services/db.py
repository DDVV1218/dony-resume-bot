"""SQLite 数据库管理 - FTS5 简历索引存储

提供连接管理、建表、jieba 分词初始化。
"""

import logging
import sqlite3
import threading
from typing import Optional

import jieba

from config import Config

logger = logging.getLogger(__name__)

# 线程本地存储 + 单例锁
_local = threading.local()
_lock = threading.Lock()
_config: Optional[Config] = None


def configure(config: Config) -> None:
    """设置配置（启动时调用一次）"""
    global _config
    _config = config


def get_connection() -> sqlite3.Connection:
    """获取 SQLite 连接（单例，线程安全）"""
    if not _config:
        raise RuntimeError("db not configured: call configure(config) first")

    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_config.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def _migrate_add_id(conn) -> None:
    """迁移 resumes 表：添加 id 自增主键

    旧表: PRIMARY KEY (name, sex, phone)
    新表: id INTEGER PRIMARY KEY AUTOINCREMENT + UNIQUE(name, sex, phone)
    """
    logger.info("Migrating resumes table: adding id column...")

    # 1. 备份 FTS 数据
    try:
        fts_backup = conn.execute(
            "SELECT rowid, full_text, name, school, skills, company FROM resumes_fts"
        ).fetchall()
    except Exception:
        fts_backup = []

    # 2. 备份 resumes 数据
    old_rows = conn.execute(
        "SELECT rowid, name, sex, phone, email, metadata, chroma_id, pdf_path, markdown_path, created_at "
        "FROM resumes"
    ).fetchall()

    # 3. 创建新表
    conn.execute("DROP TABLE IF EXISTS resumes_new")
    conn.execute("""
        CREATE TABLE resumes_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sex TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            metadata TEXT,
            chroma_id TEXT,
            pdf_path TEXT,
            markdown_path TEXT,
            created_at TEXT,
            UNIQUE(name, sex, phone)
        )
    """)

    # 4. 复制数据（保留旧 rowid 作为 id）
    for row in old_rows:
        conn.execute(
            """INSERT INTO resumes_new (id, name, sex, phone, email, metadata, chroma_id, pdf_path, markdown_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [row["rowid"], row["name"], row["sex"], row["phone"], row["email"],
             row["metadata"], row["chroma_id"], row["pdf_path"], row["markdown_path"], row["created_at"]],
        )

    # 5. 替换旧表
    conn.execute("DROP TABLE IF EXISTS resumes_fts")
    conn.execute("DROP TABLE resumes")
    conn.execute("ALTER TABLE resumes_new RENAME TO resumes")

    # 6. 重建 FTS 表
    conn.execute("""
        CREATE VIRTUAL TABLE resumes_fts USING fts5(
            full_text, name, school, skills, company,
            tokenize='unicode61'
        )
    """)

    # 7. 恢复 FTS 数据
    for row in fts_backup:
        conn.execute(
            "INSERT INTO resumes_fts (rowid, full_text, name, school, skills, company) VALUES (?, ?, ?, ?, ?, ?)",
            [row["rowid"], row["full_text"], row["name"], row["school"], row["skills"], row["company"]],
        )

    conn.commit()
    logger.info(f"Migration complete: {len(old_rows)} records migrated, {len(fts_backup)} FTS entries restored")


def init_db(config: Optional[Config] = None) -> None:
    """初始化数据库：建表 + jieba 加载

    应在应用启动时调用一次。
    """
    if config:
        configure(config)

    if not _config:
        raise RuntimeError("db not configured")

    with _lock:
        conn = get_connection()

        # 检查是否需要迁移（旧表没有 id 列）
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='resumes'"
        ).fetchone()

        if table_exists:
            has_id_column = False
            cursor = conn.execute("PRAGMA table_info(resumes)")
            for row in cursor.fetchall():
                if row[1] == "id":
                    has_id_column = True
                    break

            if not has_id_column:
                _migrate_add_id(conn)

        # 简历元数据主表（如果迁移后表不存在，则新建）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sex TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                metadata TEXT,
                chroma_id TEXT,
                pdf_path TEXT,
                markdown_path TEXT,
                created_at TEXT,
                UNIQUE(name, sex, phone)
            )
        """)

        # FTS5 全文索引表
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS resumes_fts USING fts5(
                full_text,
                name,
                school,
                skills,
                company,
                tokenize='unicode61'
            )
        """)

        conn.commit()
        logger.info(f"Database initialized at {_config.sqlite_path}")

    # 验证 jieba 分词可用
    _test_jieba()


def _test_jieba() -> None:
    """验证 jieba cut_for_search 能正常调用"""
    try:
        tokens = list(jieba.cut_for_search("复旦大学灵均投资"))
        logger.info(f"jieba test OK: {tokens}")
    except Exception as e:
        logger.error(f"jieba initialization failed: {e}")
        raise


def table_exists(name: str) -> bool:
    """检查表是否存在"""
    conn = get_connection()
    result = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        [name],
    ).fetchone()
    return result is not None
