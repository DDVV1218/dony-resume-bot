"""搜索简历工具

当用户查询涉及简历搜索时，LLM 调用此工具执行 FTS5 全文搜索。
工具内部完成关键词提取 + FTS5 搜索的完整流程。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SearchResumesParams(BaseModel):
    """搜索简历参数"""
    query: str = ""
    max_results: int = 5


class SearchResumesTool(BaseTool):
    """搜索简历工具

    根据用户提供的搜索查询，从简历库中检索匹配的简历。
    支持关键词搜索，查询可以是自然语言描述。
    """

    name: str = "search_resumes"
    description: str = (
        "根据用户提供的查询从简历库中搜索匹配的简历。"
        "查询可以是自然语言，如'找复旦毕业的CTA量化实习生'。"
        "当用户明确要求搜索、查找、寻找简历时使用此工具。"
    )
    parameters = SearchResumesParams

    def _execute(self, query: str = "", max_results: int = 5) -> ToolResult:
        """执行简历搜索

        1. 调用 LLM 从查询中提取关键词
        2. 用关键词执行 FTS5 搜索
        3. 返回结构化结果

        Args:
            query: 用户搜索查询
            max_results: 最大返回条数

        Returns:
            ToolResult(success=True, data=[...]) 或
            ToolResult(success=False, error=...) 搜索失败
        """
        search_results: List[Dict[str, Any]] = []
        keywords: List[str] = []

        # Step 1: 提取关键词
        try:
            keywords = self._extract_keywords(query)
            logger.info(f"Search tool extracted keywords: {keywords}")
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"关键词提取失败: {e}",
            )

        if not keywords:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "keywords": [],
                    "message": "未能从查询中提取出有效的搜索关键词",
                },
            )

        # Step 2: FTS5 搜索
        try:
            from services.resume_searcher import search_resumes as fts_search

            search_results = fts_search(keywords=keywords, max_results=max_results)
            logger.info(f"Search tool found {len(search_results)} results")
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"简历搜索失败: {e}",
            )

        return ToolResult(
            success=True,
            data={
                "results": self._format_results(search_results),
                "keywords": keywords,
                "total_found": len(search_results),
            },
        )

    def _extract_keywords(self, query: str) -> List[str]:
        """调用关键词提取器从查询中提取关键词

        Args:
            query: 用户查询

        Returns:
            关键词列表
        """
        try:
            from services.keyword_extractor import extract_keywords

            # 需要 config，从全局导入
            from config import Config

            config = Config()
            return extract_keywords(query, config)
        except ImportError:
            logger.warning("keyword_extractor not available, using raw query")
            return [query]

    def _format_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """格式化搜索结果，移除内部字段，保留 LLM 需要的信息

        Args:
            results: 原始搜索结果

        Returns:
            格式化后的结果列表
        """
        formatted = []
        for r in results:
            entry = {
                "name": r.get("name", ""),
                "sex": r.get("sex", ""),
                "school": r.get("school", ""),
                "major": r.get("major", ""),
                "degree": r.get("degree", ""),
                "phone": r.get("phone", ""),
                "email": r.get("email", ""),
            }
            # 如果有 metadata_dict，合并进去
            if r.get("metadata_dict"):
                entry["metadata"] = r["metadata_dict"]
            formatted.append(entry)
        return formatted
