"""搜索简历工具

搜索流程：
1. 语义搜索（ChromaDB 向量）→ 按人聚合
2. 取 top 50 → Reranker（全文 + 最佳段落）
3. 取 top 25 → Review LLM（Phase 2）
4. 取 top K → Comment LLM（Phase 3）
5. 最终输出候选人和 AI 评论
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 向量分公式权重
MAX_VEC_WEIGHT = 0.45
FULL_VEC_WEIGHT = 0.25
SECTION_VEC_WEIGHT = 0.10  # 每个段落（edu/exp/skills）各 0.10

# 各阶段数量
TOP_K_MERGE = 300   # 按人聚合后，前 300 人进 Reranker
REVIEW_TOP_K = 50   # Reranker 后前 50 人进 Review
TOP_K_FINAL = 10   # 最终返回数上限

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

    语义搜索：向量搜索 → 按人聚合 → Reranker 精排。
    最多返回 10 条结果，如果用户要求超过 10 条请告知用户上限为 10。
    """

    name: str = "search_resumes"
    description: str = (
        "根据用户的模糊需求或推荐要求搜索简历库，返回语义匹配的候选人清单。"
        "适用于：技能搜索（如'找会Python的'）、经验匹配（如'有量化实习经验的'）、"
        "条件组合（如'找复旦毕业的CTA量化实习生'）、推荐候选人（如'推荐几个人选'）。"
        "不适用于按姓名查找具体某位候选人（请使用 query_resume_db 工具）。"
        "最多返回 10 条结果，如用户要求数量超过 10 请告知上限。"
    )
    parameters = SearchResumesParams

    def _execute(self, query: str = "", max_results: int = 10) -> ToolResult:
        """执行语义搜索

        Args:
            query: 用户搜索查询
            max_results: 最终返回条数（1~10）

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

        # 校验 max_results
        if max_results < 1 or max_results > 10:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": f"返回条数必须在 1~10 之间，您要求了 {max_results} 条，请重新指定",
                },
            )

        from config import Config
        config = Config()
        self._ensure_db(config)
        max_results = max_results or TOP_K_FINAL

        # === Step 1: 语义搜索（纯向量） ===
        vector_hits = self._vector_search(query, config)
        if not vector_hits:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": "没有找到匹配的简历",
                },
            )

        # === Step 2: 按人聚合向量分，取 top 50 进 Reranker ===
        aggregated = self._aggregate_by_person(vector_hits)
        if not aggregated:
            return ToolResult(
                success=True,
                data={
                    "results": [],
                    "message": "没有找到匹配的简历",
                },
            )

        aggregated_sorted = sorted(aggregated.values(), key=lambda x: x["vector_score"], reverse=True)
        # 按向量分排序，取前 TOP_K_MERGE 人进入 Reranker
        top_50 = aggregated_sorted[:TOP_K_MERGE]
        logger.info(f"Vector aggregated: {len(aggregated)} candidates, top 50 -> Reranker")

        # === Step 3: Reranker 精排 ===
        reranked = self._rerank(top_50, query, config)

        # === Step 4: Review LLM 硬性条件审查 ===
        passed = self._run_review(query, reranked, config)

        # === Step 5: Comment LLM 对所有通过的人打分 ===
        if passed:
            comments = self._run_comment(query, passed, config)

            # 按 Comment 分排序，取 top K
            scored = []
            for c in passed:
                rid = c.get("resume_id", 0)
                cm = comments.get(rid, {})
                scored.append((c, cm.get("score", 0), cm))

            scored.sort(key=lambda x: x[1], reverse=True)
            final_candidates = [x[0] for x in scored[:min(max_results, TOP_K_FINAL)]]
            all_comments = {x[0].get("resume_id", 0): x[2] for x in scored}

            # 拼入结果
            formatted = self._format_results(final_candidates)
            for res in formatted:
                rid = res.get("id")
                cm = all_comments.get(rid, {})
                if cm:
                    res["ai_comment"] = cm.get("comment", "")
                    res["ai_score"] = cm.get("score", 0)
                    res["ai_education"] = cm.get("education_score", 0)
                    res["ai_experience"] = cm.get("experience_score", 0)
                    res["ai_skill"] = cm.get("skill_score", 0)
        else:
            formatted = []

        return ToolResult(
            success=True,
            data={
                "results": formatted,
                "total_found": len(passed) if passed else 0,
            },
        )

    def _vector_search(self, query: str, config) -> List[Dict[str, Any]]:
        """向量搜索，返回每人所有 chunk 的分数

        Returns:
            [{"resume_id": 1, "chunk_type": "full", "score": 0.85, "text": "..."}, ...]
        """
        try:
            from services.vector_indexer import search_similar
            return search_similar(query, config, top_k=-1)  # -1 = 全量召回
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

    def _run_review(
        self,
        query: str,
        reranked: List[Dict[str, Any]],
        config,
    ) -> List[Dict[str, Any]]:
        """Review LLM 硬性条件审查

        取 Reranker 后的 top 25 人，构建 meta 信息，
        调用 Review LLM 审查，只返回通过的人。
        """
        candidates = reranked[:REVIEW_TOP_K]
        logger.info(f"Reranker 前{REVIEW_TOP_K}: " + ", ".join(
            [f"{c.get('name','?')}({c.get('final_score',0):.3f})" for c in candidates[:10]]
        ))
        if not candidates:
            return []

        # 构建候选人 meta 列表
        meta_list = []
        for c in candidates:
            rid = c.get("resume_id", 0)
            try:
                from services.db import get_connection
                conn = get_connection()
                row = conn.execute(
                    "SELECT name, metadata FROM resumes WHERE id = ?",
                    [rid],
                ).fetchone()
            except Exception:
                row = None

            if not row:
                continue

            meta = {"id": rid, "name": row["name"]}
            if row["metadata"]:
                try:
                    md = json.loads(row["metadata"])
                    # 学校信息
                    schools = [md.get("undergraduate", "")]
                    if md.get("master"):
                        schools.append(md["master"])
                    if md.get("doctor"):
                        schools.append(md["doctor"])
                    meta["school"] = " / ".join(filter(None, schools))
                    meta["degree"] = "博士" if md.get("doctor") else ("硕士" if md.get("master") else "本科")
                    meta["skills"] = md.get("skills", "")
                    # 公司/经历
                    comps = [c for c in [md.get("intership_comps", ""), md.get("work_comps", "")] if c]
                    meta["company"] = "; ".join(comps)
                except (json.JSONDecodeError, TypeError):
                    pass

            # 补充 experience 和 skills chunk 文本（含具体工作细节）
            chunk_texts = c.get("chunk_texts", {})
            exp_text = chunk_texts.get("experience", "")
            skills_text = chunk_texts.get("skills", "")
            # 截取前 300 字作为经历描述
            meta["experience_detail"] = (exp_text or "")[:300].replace("\n", " ")
            meta["skills_detail"] = (skills_text or "")[:200].replace("\n", " ")

            meta_list.append(meta)

        try:
            from services.review_llm import batch_review
            verdicts = batch_review(query, meta_list, config.review_agent)
        except Exception as e:
            logger.warning(f"Review LLM failed (non-fatal): {e}")
            # Review 失败时降级：返回全部候选人
            return candidates

        if verdicts is None:
            logger.warning("Review LLM returned None, falling back to all candidates")
            return candidates

        # 过滤通过的人
        passed_ids = {v.id for v in verdicts if v.verdict == "pass"}
        passed = [c for c in candidates if c.get("resume_id", 0) in passed_ids]

        logger.info(f"Review LLM: {len(verdicts)} reviewed, {len(passed)} passed")
        return passed

    def _run_comment(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        config,
    ) -> Dict[int, Dict[str, Any]]:
        """Comment LLM 综合评价

        对最终输出的候选人进行综合评价打分。
        并发调用，失败时跳过。
        """
        if not candidates:
            return {}

        # 构建候选人 meta 列表
        meta_list = []
        for c in candidates:
            rid = c.get("resume_id", 0)
            try:
                from services.db import get_connection
                conn = get_connection()
                row = conn.execute(
                    "SELECT name, metadata FROM resumes WHERE id = ?",
                    [rid],
                ).fetchone()
            except Exception:
                row = None

            if not row:
                continue

            meta = {"id": rid, "name": row["name"]}
            if row["metadata"]:
                try:
                    md = json.loads(row["metadata"])
                    schools = [md.get("undergraduate", "")]
                    if md.get("master"):
                        schools.append(md["master"])
                    if md.get("doctor"):
                        schools.append(md["doctor"])
                    meta["school"] = " / ".join(filter(None, schools))
                    meta["degree"] = "博士" if md.get("doctor") else ("硕士" if md.get("master") else "本科")
                    meta["skills"] = md.get("skills", "")
                    comps = [c for c in [md.get("intership_comps", ""), md.get("work_comps", "")] if c]
                    meta["company"] = "; ".join(comps)
                except (json.JSONDecodeError, TypeError):
                    pass

            chunk_texts = c.get("chunk_texts", {})
            full_text = chunk_texts.get("full", "")
            meta["full_text"] = (full_text or "").replace("\n", " ")
            meta_list.append(meta)

        try:
            from services.comment_llm import evaluate_batch
            comments = evaluate_batch(query, meta_list, config.comment_agent, max_workers=5)
        except Exception as e:
            logger.warning(f"Comment LLM failed (non-fatal): {e}")
            return {}

        return comments

    def _aggregate_by_person(self, vector_hits: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """按人聚合向量搜索结果

        Returns:
            {resume_id: {
                "vector_score": float,
                "chunk_scores": {"full": 0.8, "education": 0.6, ...},
                "chunk_texts": {"full": "...", ...},
            }}
        """
        from collections import defaultdict

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

        if not person_chunks:
            return {}

        result = {}
        for rid, chunk_scores in person_chunks.items():
            vec_score = _compute_vector_person_score(chunk_scores)
            result[rid] = {
                "resume_id": rid,
                "vector_score": vec_score,
                "chunk_scores": chunk_scores,
                "chunk_texts": person_texts.get(rid, {}),
            }

        return result

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

        # 构建批处理：每人的 4 个 chunk，跳过空文本
        chunk_order = ["full", "education", "experience", "skills"]
        all_texts = []
        text_owners = []  # [(index_in_rerank_tasks, chunk_type)]

        for i, (_, full_text, section_texts, _) in enumerate(rerank_tasks):
            for ctype in chunk_order:
                text = full_text if ctype == "full" else section_texts.get(ctype, "")
                if text.strip():
                    all_texts.append(text)
                    text_owners.append((i, ctype))

        if not all_texts:
            return candidates

        # 一次性批处理 Reranker
        from services.reranker import rerank_batch
        scores = rerank_batch(query, all_texts, config)

        # 按人分配分数，缺失的 chunk 给 0 分
        chunk_scores_per_person = [{} for _ in rerank_tasks]
        for (i, ctype), score in zip(text_owners, scores):
            chunk_scores_per_person[i][ctype] = score

        for i, (cand, _, _, _) in enumerate(rerank_tasks):
            cs = chunk_scores_per_person[i]
            for ctype in chunk_order:
                cs.setdefault(ctype, 0.0)

            # 和向量分相同的加权公式：max(4) × 0.45 + full × 0.25 + edu/exp/skills 各 × 0.10
            max_chunk = max(cs.values())
            rerank_score = (
                max_chunk * 0.45
                + cs["full"] * 0.25
                + cs["education"] * 0.10
                + cs["experience"] * 0.10
                + cs["skills"] * 0.10
            )

            cand["rerank_score"] = rerank_score
            cand["rerank_chunk_scores"] = cs
            cand["full_rerank"] = cs["full"]
            cand["final_score"] = max(0.0, rerank_score)

        # 按最终分排序
        candidates.sort(key=lambda x: x.get("final_score", 0.0), reverse=True)
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
                    "SELECT id, name, phone, email, metadata FROM resumes WHERE id = ?",
                    [rid],
                ).fetchone()
            except Exception:
                row = None

            entry = {
                "id": rid,
                "final_score": round(c.get("final_score", c.get("rerank_score", 0.0)), 3),
                "rerank_score": round(c.get("rerank_score", 0.0), 3),
            }

            if row:
                entry["name"] = row["name"]
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
