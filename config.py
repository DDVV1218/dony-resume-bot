"""配置管理 - 从环境变量/.env 读取配置"""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()


@dataclass
class Config:
    """应用配置"""

    # --- 飞书配置 ---
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", ""))
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", ""))

    # --- OpenAI 配置 ---
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o"))
    openai_context_window: int = field(
        default_factory=lambda: int(os.getenv("OPENAI_CONTEXT_WINDOW", "128000"))
    )
    openai_temperature: float = field(
        default_factory=lambda: float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
    )

    # --- Compact 配置 ---
    compact_threshold: float = field(
        default_factory=lambda: float(os.getenv("COMPACT_THRESHOLD", "0.85"))
    )
    compact_recent_ratio: float = field(
        default_factory=lambda: float(os.getenv("COMPACT_RECENT_RATIO", "0.15"))
    )

    # --- 路径配置 ---
    sessions_dir: str = field(default_factory=lambda: os.getenv("SESSIONS_DIR", "/app/sessions"))
    uploads_dir: str = field(default_factory=lambda: os.getenv("UPLOADS_DIR", "/app/uploads"))
    chroma_db_dir: str = field(default_factory=lambda: os.getenv("CHROMA_DB_DIR", "/app/chroma_db"))

    # --- 应用配置 ---
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def compact_trigger_tokens(self) -> int:
        """触发 compact 的 token 阈值"""
        return int(self.openai_context_window * self.compact_threshold)

    @property
    def compact_recent_tokens(self) -> int:
        """compact 时保留的最近 token 数"""
        return int(self.openai_context_window * self.compact_recent_ratio)

    def validate(self) -> List[str]:
        """验证必要配置是否齐全，返回缺失项列表"""
        missing = []
        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        return missing
