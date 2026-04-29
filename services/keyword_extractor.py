"""LLM 关键词提取器

从用户查询中提取关键词供 FTS5 搜索。
使用独立的 Agent 配置，输出为 Pydantic 结构化对象。
"""

import logging
from typing import List, Optional

from pydantic import BaseModel
from config import Config

from services.llm_utils import StructuredOutput

logger = logging.getLogger(__name__)

# 关键词提取的 system prompt
KEYWORD_EXTRACT_PROMPT = """你是一个简历检索系统的关键词提取助手。从用户的查询中提取关键词，用于 FTS5 全文搜索。

规则：
- 排除"找"、"的"、"有"、"吗"、"一下"、"看看"、"帮忙"等无意义词
- 保留：人名、公司名、学校名、技能名、岗位名等实体
- 否定条件（"不要""除外""没有"）在词后加 /NOT
- 多选条件（"或""或者""还是"）用 / 分隔选项
- should_search 表示是否需要搜索简历库。问候闲聊等非搜索场景设为 false。

示例：
用户：找复旦或交大毕业的，有CTA经验，懂Python，不要社招的
输出：keywords=["复旦/交大", "CTA", "Python", "社招/NOT"], should_search=true

用户：你好
输出：keywords=[], should_search=false

用户：帮我看看有没有之前从灵均出来的量化研究员
输出：keywords=["灵均投资", "量化研究员"], should_search=true

用户：今天天气怎么样
输出：keywords=[], should_search=false

用户：最近有投递实习生的简历吗
输出：keywords=["实习生"], should_search=true
"""


class KeywordExtraction(BaseModel):
    """关键词提取结果"""
    keywords: List[str] = []
    should_search: bool = False


def extract_keywords(
    user_query: str,
    config: Config,
    timeout: float = 5.0,
) -> List[str]:
    """调用 LLM 从用户查询中提取关键词

    使用独立的 Agent 配置和上下文，不污染主聊天上下文。
    内部使用 StructuredOutput.parse() 获取结构化输出。

    Args:
        user_query: 用户原始查询
        config: 应用配置
        timeout: LLM 调用超时（秒）

    Returns:
        关键词列表，失败或非搜索场景返回空列表
    """
    try:
        messages = [
            {"role": "system", "content": KEYWORD_EXTRACT_PROMPT},
            {"role": "user", "content": user_query},
        ]

        result = StructuredOutput.parse(
            model_class=KeywordExtraction,
            messages=messages,
            config=config.keyword_agent,
            fallback=KeywordExtraction(keywords=[], should_search=False),
            retries=1,
            timeout=timeout,
            max_tokens=200,
        )

        if result.should_search:
            logger.info(
                f"Keywords extracted: {result.keywords} (should_search=True)"
            )
            return result.keywords
        else:
            logger.info("Keyword extraction: should_search=False")
            return []

    except Exception as e:
        logger.warning(f"Keyword extraction failed (non-fatal): {e}")
        return []
