"""Reranker 服务封装

使用 Qwen3-Reranker-8B (vLLM) 对 query + 文档进行相关性打分。
支持批处理：一次请求多个 pair。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from openai import OpenAI

from config import Config

logger = logging.getLogger(__name__)

RERANKER_MODEL = "Qwen3-Reranker-8B"

# 批处理最大大小（vLLM 单次请求的 pair 数限制）
BATCH_SIZE = 100


def rerank_batch(query: str, texts: List[str], config: Config) -> List[float]:
    """批量 rerank：一个 query 对多个文档

    使用 vLLM 的 /v1/score 批处理接口。
    用 httpx 直接请求，避免 OpenAI SDK 的 post() 限制。

    Args:
        query: 查询文本
        texts: 待评分的文档列表
        config: 配置

    Returns:
        分数列表，顺序与 texts 一一对应，0~1 区间
    """
    if not texts:
        return []

    import httpx

    all_scores = []
    url = config.reranker_server_url.rstrip("/") + "/score"

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    url,
                    json={
                        "model": RERANKER_MODEL,
                        "text_1": [query] * len(batch),
                        "text_2": batch,
                    },
                )
                data = response.json()
                scores = [item["score"] for item in data.get("data", [])]
                all_scores.extend(scores)

                if not scores:
                    logger.warning(f"Reranker batch returned empty data: {data}")
                    all_scores.extend([0.0] * len(batch))

        except Exception as e:
            logger.error(f"Reranker batch failed (offset={start}): {e}")
            all_scores.extend([0.0] * len(batch))

    return all_scores


def rerank_single(query: str, text: str, config: Config) -> float:
    """单个文档 rerank"""
    scores = rerank_batch(query, [text], config)
    return scores[0] if scores else 0.0


def compute_person_rerank_score(
    query: str,
    full_text: str,
    section_texts: Dict[str, str],
    section_scores: Dict[str, float],
    config: Config,
) -> Dict[str, Any]:
    """计算一个人的 rerank 综合分

    Args:
        query: 查询
        full_text: 简历全文
        section_texts: 段落文本 {"education": "...", "experience": "...", "skills": "..."}
        section_scores: 向量搜索阶段各段落的 cosine 分 {"education": 0.6, ...}
        config: 配置

    Returns:
        {"rerank_score": float, "full_score": float, "best_section_score": float, "best_section_type": str}
    """
    # 找出向量分最大的段落
    best_section_type = max(section_scores, key=section_scores.get) if section_scores else ""
    best_section_text = section_texts.get(best_section_type, "")

    # 只 rerank 全文 + 最佳段落
    texts_to_rerank = [full_text]
    if best_section_text:
        texts_to_rerank.append(best_section_text)

    scores = rerank_batch(query, texts_to_rerank, config)

    full_score = scores[0] if len(scores) > 0 else 0.0
    best_score = scores[1] if len(scores) > 1 else 0.0

    # 公式：max(full, best) × 0.50 + full × 0.25 + best × 0.25
    max_score = max(full_score, best_score)
    rerank_score = max_score * 0.50 + full_score * 0.25 + best_score * 0.25

    return {
        "rerank_score": rerank_score,
        "full_rerank": full_score,
        "best_section_rerank": best_score,
        "best_section_type": best_section_type,
    }
