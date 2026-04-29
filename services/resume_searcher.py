"""简历检索器 - FTS5 关键词搜索

提供 build_fts5_query() 构建 MATCH 语句，search_resumes() 执行搜索并 BM25 排序。
RRF 合并占位，待 Embedding 模型部署后接入 ChromaDB 向量搜索。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from services.db import get_connection

logger = logging.getLogger(__name__)


def build_fts5_query(tokens: List[str]) -> Optional[str]:
    """将 LLM 提取的关键词列表转换为 FTS5 MATCH 语句

    Args:
        tokens: 关键词列表，支持格式：
            "复旦" -> 简单词
            "复旦/交大" -> OR 多选
            "社招/NOT" -> 排除

    Returns:
        FTS5 MATCH 字符串，空列表返回 None
    """
    if not tokens:
        return None

    positive = [t for t in tokens if not t.endswith("/NOT")]
    negative = [t[:-4] for t in tokens if t.endswith("/NOT")]

    # 构建正向匹配部分
    processed = []
    for p in positive:
        p = p.strip()
        if not p:
            continue
        if "/" in p:
            # 多选：复旦/交大 -> (复旦 OR 交大)
            options = [o.strip() for o in p.split("/") if o.strip()]
            if options:
                quoted = _quote_token(options[0]) if len(options) == 1 else " OR ".join(
                    _quote_token(o) for o in options
                )
                if len(options) > 1:
                    processed.append(f"({quoted})")
                else:
                    processed.append(quoted)
        else:
            processed.append(_quote_token(p))

    if not processed:
        return None

    match_expr = " AND ".join(processed)

    # 追加排除部分
    if negative:
        neg_expr = " OR ".join(_quote_token(n) for n in negative if n.strip())
        match_expr += f" NOT ({neg_expr})"

    logger.debug(f"FTS5 query built: {match_expr}")
    return match_expr


def _quote_token(token: str) -> str:
    """用双引号包裹 token，防止特殊字符破坏 FTS5 语法"""
    # 如果 token 不含 FTS5 特殊字符，可以不用引号
    special = set('()"*^:')
    if any(c in token for c in special):
        return f'"{token}"'
    return f'"{token}"'


def search_resumes(
    keywords: List[str],
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """执行 FTS5 关键词搜索

    Args:
        keywords: 关键词列表
        max_results: 返回最大条数

    Returns:
        简历列表，每条含 resume 表字段和 BM25 rank 分数
    """
    query = build_fts5_query(keywords)
    if not query:
        logger.info("Empty FTS5 query, skipping search")
        return []

    try:
        conn = get_connection()

        # FTS5 搜索 + 关联 resumes 主表取完整信息
        # FTS5 rank 需直接在 FTS5 表上查询
        rows = conn.execute(
            """
            SELECT r.*, fts.bm25_score
            FROM (
                SELECT rowid, rank AS bm25_score
                FROM resumes_fts
                WHERE resumes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            ) AS fts
            JOIN resumes AS r ON r.rowid = fts.rowid
            ORDER BY fts.bm25_score
            """,
            [query, max_results],
        ).fetchall()

        results = []
        for row in rows:
            record = dict(row)
            # 解析 metadata JSON
            if record.get("metadata"):
                try:
                    record["metadata_dict"] = json.loads(record["metadata"])
                except (json.JSONDecodeError, TypeError):
                    record["metadata_dict"] = {}
            else:
                record["metadata_dict"] = {}
            results.append(record)

        logger.info(f"FTS5 search returned {len(results)} results")
        return results

    except Exception as e:
        logger.error(f"FTS5 search failed: {e}")
        return []


def merge_results(
    fts_results: List[Dict[str, Any]],
    vector_results: List[Dict[str, Any]],
    top_k: int = 10,
) -> List[Dict[str, Any]]:
    """RRF 合并 FTS5 和向量搜索结果

    RRF score = 1/(k + rank_fts) + 1/(k + rank_vector)

    当前仅返回 FTS5 结果（向量搜索待 Embedding 模型部署后接入）。
    """
    if not fts_results:
        return (vector_results or [])[:top_k]
    if not vector_results:
        return fts_results[:top_k]

    # RRF 合并
    # k = 60 (RRF 标准常数)
    rrf_scores: Dict[int, float] = {}

    for rank, r in enumerate(fts_results, start=1):
        rid = r.get("id", 0) or hash((r.get("name", ""), r.get("phone", "")))
        rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (60 + rank)

    for rank, r in enumerate(vector_results, start=1):
        rid = r.get("id", 0) or hash((r.get("name", ""), r.get("phone", "")))
        rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (60 + rank)

    # 按 RRF 分数排序
    key_to_record = {}
    for r in fts_results:
        rid = r.get("id", 0) or hash((r.get("name", ""), r.get("phone", "")))
        key_to_record[rid] = r
    for r in vector_results:
        rid = r.get("id", 0) or hash((r.get("name", ""), r.get("phone", "")))
        key_to_record[rid] = r

    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    merged = [key_to_record[k] for k in sorted_keys[:top_k]]

    logger.info(f"RRF merged: {len(merged)} results (FTS5={len(fts_results)}, vector={len(vector_results)})")
    return merged
