"""时间工具 - 统一使用东八区上海时间"""

from datetime import datetime, timezone, timedelta

# 东八区时区
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_now() -> datetime:
    """获取当前上海时间"""
    return datetime.now(SHANGHAI_TZ)


def shanghai_time_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """获取格式化的上海时间字符串

    Args:
        fmt: 时间格式，默认 yyyy-mm-dd HH:MM:SS

    Returns:
        格式化后的时间字符串
    """
    return shanghai_now().strftime(fmt)
