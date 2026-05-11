"""SQLite 数据库管理

提供连接管理、建表。
"""

import logging
import sqlite3
import threading
from typing import Optional

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


def _recreate_tables(conn) -> None:
    """重建所有表（清空数据），用于测试阶段 schema 变更"""
    logger.info("Recreating all tables (data cleared)...")
    conn.execute("DROP TABLE IF EXISTS resumes")
    conn.execute("""
        CREATE TABLE resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            metadata TEXT,
            chroma_id TEXT,
            pdf_path TEXT,
            markdown_path TEXT,
            created_at TEXT,
            UNIQUE(name, phone)
        )
    """)
    conn.commit()
    logger.info("Tables recreated successfully")


def init_db(config: Optional[Config] = None) -> None:
    """初始化数据库：建表

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

        # 检查是否是旧 schema（含 sex 字段）
        has_sex = False
        cursor = conn.execute("PRAGMA table_info(resumes)")
        for row in cursor.fetchall():
            if row[1] == "sex":
                has_sex = True
                break

        if has_sex:
            # 测试阶段：直接重建表
            _recreate_tables(conn)
        else:
            # 新建表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS resumes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    email TEXT,
                    metadata TEXT,
                    chroma_id TEXT,
                    pdf_path TEXT,
                    markdown_path TEXT,
                    created_at TEXT,
                    UNIQUE(name, phone)
                )
            """)


        conn.commit()
        logger.info(f"Database initialized at {_config.sqlite_path}")
