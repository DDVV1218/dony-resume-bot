"""配置管理 - 从环境变量/.env 读取配置"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

from services.agent_config import AgentConfig

# 加载 .env 文件（如果存在）
load_dotenv()


@dataclass
class Config:
    """应用配置"""

    # --- 飞书配置 ---
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", ""))
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", ""))

    # --- OpenAI 配置 ---
    # 注意：本项目仅支持 Qwen3.6-27B 模型，禁止使用其他模型
    # 注意：禁止开启思考模式（reasoning/thinking）
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "Qwen3.6-27B"))
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "http://localhost:3000/v1")
    )
    openai_context_window: int = field(
        default_factory=lambda: int(os.getenv("OPENAI_CONTEXT_WINDOW", "262144"))
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

    # --- SQLite 简历库 ---
    sqlite_path: str = field(default_factory=lambda: os.getenv("SQLITE_PATH", "/app/sessions/resumes.db"))

    # --- 飞书访问控制 ---
    feishu_dm_policy: str = field(
        default_factory=lambda: os.getenv("FEISHU_DM_POLICY", "open")
    )
    feishu_dm_allowlist: List[str] = field(
        default_factory=lambda: [x for x in os.getenv("FEISHU_DM_ALLOWLIST", "").split(",") if x]
    )
    feishu_group_policy: str = field(
        default_factory=lambda: os.getenv("FEISHU_GROUP_POLICY", "open")
    )
    feishu_group_allowlist: List[str] = field(
        default_factory=lambda: [x for x in os.getenv("FEISHU_GROUP_ALLOWLIST", "").split(",") if x]
    )
    feishu_require_mention: bool = field(
        default_factory=lambda: os.getenv("FEISHU_REQUIRE_MENTION", "true").lower() in ("true", "1", "yes")
    )
    feishu_bot_open_id: Optional[str] = field(
        default_factory=lambda: os.getenv("FEISHU_BOT_OPEN_ID", None)
    )

    # --- 流式回复 ---
    feishu_streaming: bool = field(
        default_factory=lambda: os.getenv("FEISHU_STREAMING", "true").lower() in ("true", "1", "yes")
    )
    feishu_streaming_interval: float = field(
        default_factory=lambda: float(os.getenv("FEISHU_STREAMING_INTERVAL", "0.25"))
    )

    # --- MinerU PDF 解析 ---
    mineru_server_url: str = field(
        default_factory=lambda: os.getenv("MINERU_SERVER_URL", "http://localhost:8003")
    )
    mineru_model_name: str = field(
        default_factory=lambda: os.getenv("MINERU_MODEL_NAME", "")
    )
    mineru_process_dir: str = field(
        default_factory=lambda: os.getenv("MINERU_PROCESS_DIR", "/app/mineru_process")
    )

    # --- Agent 配置 ---
    chat_agent: AgentConfig = field(default_factory=lambda: AgentConfig.from_env(
        name="chat",
        prefix="CHAT",
        getenv=os.getenv,
        default_model=os.getenv("OPENAI_MODEL", "Qwen3.6-27B"),
        default_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:3000/v1"),
        default_api_key=os.getenv("OPENAI_API_KEY", ""),
    ))

    keyword_agent: AgentConfig = field(default_factory=lambda: AgentConfig.from_env(
        name="keyword",
        prefix="KEYWORD",
        getenv=os.getenv,
        default_model=os.getenv("OPENAI_MODEL", "Qwen3.6-27B"),
        default_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:3000/v1"),
        default_api_key=os.getenv("OPENAI_API_KEY", ""),
    ))

    analysis_agent: AgentConfig = field(default_factory=lambda: AgentConfig.from_env(
        name="analysis",
        prefix="ANALYSIS",
        getenv=os.getenv,
        default_model=os.getenv("OPENAI_MODEL", "Qwen3.6-27B"),
        default_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:3000/v1"),
        default_api_key=os.getenv("OPENAI_API_KEY", ""),
    ))

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
