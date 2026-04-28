"""飞书简历库 Bot - 入口

启动飞书 WebSocket 长连接，开始接收消息事件。
"""

import logging
import time


class ShanghaiFormatter(logging.Formatter):
    """日志格式化器 - 使用东八区上海时间"""
    def converter(self, seconds):
        return time.gmtime(seconds + 8 * 3600)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        return time.strftime(self.default_time_format, ct)
import sys
import os

import lark_oapi as lark

from config import Config
from feishu.bot import build_event_handler
from services.session import SessionStore

# 配置日志
def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ShanghaiFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)


def main():
    # 加载配置
    config = Config()
    setup_logging(config.log_level)

    logger = logging.getLogger(__name__)

    # 验证必要配置
    missing = config.validate()
    if missing:
        logger.error(f"Missing required config: {', '.join(missing)}")
        logger.error("Please set these environment variables or create a .env file")
        sys.exit(1)

    logger.info(f"Starting Resume Bot...")
    logger.info(f"Model: {config.openai_model}, Context Window: {config.openai_context_window}")
    logger.info(f"Sessions Dir: {config.sessions_dir}")

    # 创建 Session 存储
    session_store = SessionStore(config.sessions_dir)
    logger.info(f"Session store initialized at {config.sessions_dir}")

    # 构建飞书事件处理器
    event_handler = build_event_handler(config, session_store)

    # 创建飞书 WebSocket 客户端
    cli = lark.ws.Client(
        app_id=config.feishu_app_id,
        app_secret=config.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,  # 调试模式
    )

    logger.info("Starting WebSocket connection to Feishu...")

    try:
        # 启动长连接（阻塞，直到进程结束）
        cli.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
