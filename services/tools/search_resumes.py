"""搜索简历工具

完整的混合搜索流程：
1. 并行：向量搜索（ChromaDB）+ FTS5 关键词搜索
2. 加权合并：向量 × 0.70 + FTS × 0.30
3. TOP50 → Reranker（全文 + 最佳段落）→ TOP10
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 权重配置
VECTOR_WEIGHT = 0.70
FTS_WEIGHT = 0.30

# 向量分公式权重
MAX_VEC_WEIGHT = 0.45
FULL_VEC_WEIGHT = 0.25
SECTION_VEC_WEIGHT = 0.10  # 每个段落（edu/exp/skills）各 0.10

# 各阶段数量
TOP_K_VECTOR = 30  # 向量返回数
TOP_K_MERGE = 50   # 合并后取前 50 进 reranker
TOP_K_FINAL = 10   # 最终返回数

CHUNK_TYPES = ["full", "education", "experience", "skills"]


class SearchResumesParams(BaseModel):
    """搜索简历参数"""
    query: str = ""
    max_results: int = 10


def _compute_vector_person_score(chunk_scores: Dict[str, float]) -> float:
    """计算一个人的向量综合分

    公式：max(4 chunk) × 0.45 + full × 0.25 + edu×0.10 + exp×0.10 + skills×0.10
    """
    max_score = max(chunk_scores.values()) if chunk_scores else 0.0
    full = chunk_scores.get("full", 0.0)
    edu = chunk_scores.get("education", 0.0)
    exp = chunk_scores.get("experience", 0.0)
    skills = chunk_scores.get("skills", 0.0)

    return (
        max_score * MAX_VEC_WEIGHT
        + full * FULL_VEC_WEIGHT
        + edu * SECTION_VEC_WEIGHT
        + exp * SECTION_VEC_WEIGHT
        + skills * SECTION_VEC_WEIGHT
    )


class SearchResumesTool(BaseTool):
    """搜索简历工具

    混合搜索：向量搜索（70%）+ 关键词搜索（30%），Reranker 精排。
    """

    name: str = "search_resumes"
    description: str = (
        "根据用户提供的查询从简历库中搜索匹配的简历。"
        "查询可以是自然语言，如'找复旦毕业的CTA量化实习生'。"
        "当用户明确要求搜索、查找、寻找简历时使用此工具。"
    )
    parameters = SearchResumesParams

    def _execute(self, query: str = "", max_results: int = 10) -> ToolResult:
        """执行混合搜索

        Args:
            query: 用户搜索查询
            max_results: 最终返回条数

        Returns:
            ToolResult
        """
        if not query:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": "请提供搜索查询",
                },
            )

        from config import Config
        config = Config()
        self._ensure_db(config)
        max_results = min(max_results or TOP_K_FINAL, TOP_K_FINAL)

        # === Step 1: 并行向量搜索 + FTS 搜索 ===
        vector_hits = self._vector_search(query, config)
        fts_hits = self._fts_search(query, config)

        if not vector_hits and not fts_hits:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": "没有找到匹配的简历",
                },
            )

        # === Step 2: 按人聚合 + 加权合并 ===
        merged = self._merge_scores(vector_hits, fts_hits)
        logger.info(f"Merged {len(merged)} candidates")

        if not merged:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": "没有找到匹配的简历",
                },
            )

        # 取 top 50 进 reranker
        merged_sorted = sorted(merged.values(), key=lambda x: x["combined_score"], reverse=True)
        top_50 = merged_sorted[:TOP_K_MERGE]

        # === Step 3: Reranker 精排 ===
        reranked = self._rerank(top_50, query, config)

        # === Step 4: 返回 top K ===
        final = reranked[:max_results]

        return ToolResult(
            success=True,
            data={
                "results": self._format_results(final),
                "total_found": len(merged),
            },
        )

    def _vector_search(self, query: str, config) -> List[Dict[str, Any]]:
        """向量搜索，返回每人所有 chunk 的分数

        Returns:
            [{"resume_id": 1, "chunk_type": "full", "score": 0.85, "text": "..."}, ...]
        """
        try:
            from services.vector_indexer import search_similar
            return search_similar(query, config, top_k=TOP_K_VECTOR)
        except Exception as e:
            logger.warning(f"Vector search failed (non-fatal): {e}")
            return []

    def _ensure_db(self, config) -> None:
        """确保数据库已配置"""
        try:
            from services.db import get_connection
            get_connection()
        except RuntimeError:
            from services.db import configure
            configure(config)

    def _fts_search(self, query: str, config) -> Dict[int, float]:
        """FTS5 搜索，返回每人一个 BM25 分

        Returns:
            {resume_id: normalized_fts_score, ...}
        """
        self._ensure_db(config)

        try:
            from services.keyword_extractor import extract_keywords
            keywords = extract_keywords(query, config)
        except Exception as e:
            logger.warning(f"Keyword extraction failed: {e}")
            keywords = [query]

        if not keywords:
            return {}

        try:
            from services.resume_searcher import search_resumes as fts_search
            results = fts_search(keywords, max_results=TOP_K_VECTOR)
        except Exception as e:
            logger.warning(f"FTS search failed: {e}")
            return {}

        if not results:
            return {}

        # 归一化 BM25（rank 是负值，越负越相关）
        raw_scores = []
        for r in results:
            bm25 = r.get("bm25_score", 0.0)
            raw_scores.append(-bm25)  # 转正

        max_raw = max(raw_scores) if raw_scores else 1.0
        if max_raw <= 0:
            max_raw = 1.0

        fts_map = {}
        for i, r in enumerate(results):
            rid = r.get("id")
            if rid is None:
                continue
            fts_map[rid] = raw_scores[i] / max_raw

        logger.info(f"FTS search: {len(fts_map)} results")
        return fts_map

    def _merge_scores(
        self,
        vector_hits: List[Dict[str, Any]],
        fts_scores: Dict[int, float],
    ) -> Dict[int, Dict[str, Any]]:
        """合并向量和 FTS 分数，按人聚合

        Returns:
            {resume_id: {
                "combined_score": float,
                "chunk_scores": {"full": 0.8, "education": 0.6, ...},
                "chunk_texts": {"full": "...", ...},
                "vector_score": float,
                "fts_score": float,
            }}
        """
        from collections import defaultdict

        # 按人聚合向量结果
        person_chunks: Dict[int, Dict[str, float]] = defaultdict(dict)
        person_texts: Dict[int, Dict[str, str]] = defaultdict(dict)

        for hit in vector_hits:
            rid = hit.get("resume_id")
            if rid is None:
                continue
            ctype = hit.get("chunk_type", "")
            score = hit.get("score", 0.0)
            text = hit.get("text", "")
            person_chunks[rid][ctype] = score
            person_texts[rid][ctype] = text

        if not person_chunks and not fts_scores:
            return {}

        # 合并
        merged = {}
        all_ids = set(person_chunks.keys()) | set(fts_scores.keys())

        for rid in all_ids:
            chunk_scores = person_chunks.get(rid, {})
            chunk_texts = person_texts.get(rid, {})

            # 向量分
            if chunk_scores:
                vec_score = _compute_vector_person_score(chunk_scores)
            else:
                vec_score = 0.0

            # FTS 分
            fts_score = fts_scores.get(rid, 0.0)

            # 综合分
            combined = vec_score * VECTOR_WEIGHT + fts_score * FTS_WEIGHT

            merged[rid] = {
                "resume_id": rid,
                "combined_score": combined,
                "vector_score": vec_score,
                "fts_score": fts_score,
                "chunk_scores": chunk_scores,
                "chunk_texts": chunk_texts,
            }

        return merged

    def _rerank(
        self,
        candidates: List[Dict[str, Any]],
        query: str,
        config,
    ) -> List[Dict[str, Any]]:
        """Reranker 精排

        对每人 rerank 全文 + 最佳段落，按公式重算分数
        """
        try:
            from services.reranker import compute_person_rerank_score
        except ImportError:
            logger.warning("Reranker not available, returning merged results")
            return candidates

        # 收集所有需要 rerank 的文本
        rerank_tasks = []  # [(index, full_text, section_texts, section_scores)]

        for cand in candidates:
            chunk_texts = cand.get("chunk_texts", {})
            chunk_scores = cand.get("chunk_scores", {})

            full_text = chunk_texts.get("full", "")
            section_texts = {
                k: v for k, v in chunk_texts.items() if k in ("education", "experience", "skills")
            }
            section_scores = {
                k: v for k, v in chunk_scores.items() if k in ("education", "experience", "skills")
            }

            if not full_text:
                continue

            rerank_tasks.append((cand, full_text, section_texts, section_scores))

        if not rerank_tasks:
            return candidates

        # 构建批处理：每个人 2 个文本（全文 + 最佳段落）
        all_texts = []
        task_mapping = []  # [(index_in_rerank_tasks, is_full_text)]

        for _, full_text, section_texts, section_scores in rerank_tasks:
            all_texts.append(full_text)
            # 找出最佳段落
            best_type = max(section_scores, key=section_scores.get) if section_scores else ""
            best_text = section_texts.get(best_type, "")
            if best_text:
                all_texts.append(best_text)

        if not all_texts:
            return candidates

        # 一次性批处理
        from services.reranker import rerank_batch
        scores = rerank_batch(query, all_texts, config)

        # 分配分数
        score_idx = 0
        for i, (cand, full_text, section_texts, section_scores) in enumerate(rerank_tasks):
            full_score = scores[score_idx] if score_idx < len(scores) else 0.0
            score_idx += 1

            best_score = 0.0
            best_type = ""
            if section_texts:
                best_type = max(section_scores, key=section_scores.get) if section_scores else ""
                if best_type and score_idx < len(scores):
                    best_score = scores[score_idx]
                    score_idx += 1

            # 公式：max(full, best) × 0.50 + full × 0.25 + best × 0.25
            max_score = max(full_score, best_score)
            rerank_score = max_score * 0.50 + full_score * 0.25 + best_score * 0.25

            cand["rerank_score"] = rerank_score
            cand["full_rerank"] = full_score
            cand["best_section_rerank"] = best_score
            cand["best_section_type"] = best_type

        # 按 rerank 分排序
        candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        return candidates

    def _format_results(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """格式化搜索结果，供 LLM 展示"""
        formatted = []
        for c in candidates:
            rid = c["resume_id"]
            # 从数据库取完整信息
            try:
                from services.db import get_connection
                conn = get_connection()
                row = conn.execute(
                    "SELECT id, name, sex, phone, email, metadata FROM resumes WHERE id = ?",
                    [rid],
                ).fetchone()
            except Exception:
                row = None

            entry = {
                "id": rid,
                "rerank_score": round(c.get("rerank_score", c.get("combined_score", 0.0)), 3),
            }

            if row:
                entry["name"] = row["name"]
                entry["sex"] = row["sex"]
                entry["phone"] = row["phone"]
                entry["email"] = row["email"] or ""
                if row["metadata"]:
                    try:
                        md = json.loads(row["metadata"])
                        entry["metadata"] = md
                    except (json.JSONDecodeError, TypeError):
                        entry["metadata"] = {}
            else:
                entry["name"] = f"简历 #{rid}"

            formatted.append(entry)

        return formatted
