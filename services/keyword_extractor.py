"""LLM 关键词提取器

独立 LLM 调用，从用户查询中提取关键词供 FTS5 搜索。
使用独立上下文，不污染主聊天上下文。
"""

import logging
from typing import List, Optional

from config import Config

logger = logging.getLogger(__name__)

KEYWORD_EXTRACT_PROMPT = """你是一个简历检索系统的关键词提取助手。从用户的查询中提取关键词，用于 FTS5 全文搜索。

规则：
- 输出格式：每行一个关键词
- 去掉"找"、"的"、"有"、"吗"、"一下"、"看看"、"帮忙"等无意义词
- 保留：人名、公司名、学校名、技能名、岗位名等实体
- 否定条件（"不要""除外""没有"）在词后加 /NOT
- 多选条件（"或""或者""还是"）用 / 分隔选项

示例：
用户：找复旦或交大毕业的，有CTA经验，懂Python，不要社招的
输出：
复旦/交大
CTA
Python
社招/NOT

用户：帮我看看有没有之前从灵均出来的量化研究员
输出：
灵均投资
量化研究员

用户：最近有投递实习生的简历吗
输出：
实习生

用户：你好
输出：
"""


def extract_keywords(
    user_query: str,
    config: Config,
    timeout: float = 5.0,
) -> List[str]:
    """调用 LLM 从用户查询中提取关键词

    使用独立 LLM 上下文，不污染主聊天上下文。

    Args:
        user_query: 用户原始查询
        config: 应用配置
        timeout: LLM 调用超时（秒）

    Returns:
        关键词列表，失败返回空列表
    """
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )

        messages = [
            {"role": "system", "content": KEYWORD_EXTRACT_PROMPT},
            {"role": "user", "content": user_query},
        ]

        response = client.chat.completions.create(
            model=config.openai_model,
            messages=messages,
            temperature=0.1,
            max_tokens=200,
            timeout=timeout,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        content = response.choices[0].message.content.strip()
        if not content:
            logger.info("LLM returned empty keyword extraction")
            return []

        # 解析每行一个关键词
        keywords = []
        for line in content.split("\n"):
            line = line.strip().strip("-* ")
            if line and not line.startswith("用户"):
                # 验证格式：允许普通词、带/的多选、带/NOT的排除
                if any(c in line for c in ("/", "NOT", " ")):
                    keywords.append(line)
                else:
                    keywords.append(line)

        logger.info(f"Keywords extracted: {keywords}")
        return keywords

    except Exception as e:
        logger.warning(f"Keyword extraction failed (non-fatal): {e}")
        return []
