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

        # 简历元数据主表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resumes (
                name TEXT NOT NULL,
                sex TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                metadata TEXT,
                chroma_id TEXT,
                pdf_path TEXT,
                markdown_path TEXT,
                created_at TEXT,
                PRIMARY KEY (name, sex, phone)
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
