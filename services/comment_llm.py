"""Comment LLM — 综合评价打分

对每个候选人进行综合评价，从匹配度、学历、经历、成果、技能五个维度打分（1-10 分）。
支持并发调用。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from prompts import load_prompt
from services.agent_config import AgentConfig

logger = logging.getLogger(__name__)


class CommentResult(BaseModel):
    """综合评价结果"""
    education_score: int = Field(description="学历教育背景评分(1-10)", ge=1, le=10)
    experience_score: int = Field(description="实习/工作/项目经历评分(1-10)", ge=1, le=10)
    skill_score: int = Field(description="技能评分(1-10)", ge=1, le=10)
    final_score: int = Field(description="综合分(由代码加权计算，无需LLM填写)", ge=1, le=10)
    comment: str = Field(description="综合评价文字（80-200字）")


def _build_input(
    query: str,
    candidate: Dict[str, Any],
) -> str:
    """构建单个候选人的评价输入"""
    lines = [
        f"## 招聘需求\n{query}\n",
        f"## 候选人信息\n",
        f"- 姓名: {candidate.get('name', '未知')}",
        f"- 学校/学历: {candidate.get('school', '未知')} / {candidate.get('degree', '未知')}",
        f"- 技能: {candidate.get('skills', '未知')}",
        f"- 公司: {candidate.get('company', '未知')}",
    ]
    full = candidate.get("full_text", "")
    if full:
        lines.append(f"\n## 简历全文\n{full}\n")
    return "\n".join(lines)


def evaluate(
    query: str,
    candidate: Dict[str, Any],
    config: AgentConfig,
) -> Optional[CommentResult]:
    """对单个候选人进行综合评价

    Args:
        query: 用户搜索需求
        candidate: 候选人 meta 信息
        config: Agent 配置

    Returns:
        CommentResult 或 None（失败时）
    """
    prompt = load_prompt("comment_llm")
    input_text = _build_input(query, candidate)

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": input_text},
    ]

    try:
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        response = client.beta.chat.completions.parse(
            model=config.model,
            messages=messages,
            response_format=CommentResult,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=30.0,
            extra_body=config.extra_body,
        )
        result: Optional[CommentResult] = response.choices[0].message.parsed
        if result:
            # 代码加权计算最终分：学历×0.40 + 经历×0.35 + 技能×0.25
            weighted = result.education_score * 0.40 + result.experience_score * 0.35 + result.skill_score * 0.25
            result.final_score = max(1, min(10, round(weighted)))
            logger.info(f"Comment for {candidate.get('name', '?')}: "
                        f"edu={result.education_score} exp={result.experience_score} "
                        f"skill={result.skill_score} final={result.final_score}")
        return result
    except Exception as e:
        logger.warning(f"Comment LLM failed for {candidate.get('name', '?')}: {e}")
        return None


def evaluate_batch(
    query: str,
    candidates: List[Dict[str, Any]],
    config: AgentConfig,
    max_workers: int = 4,
) -> Dict[int, Dict[str, Any]]:
    """并发对多个候选人进行综合评价

    Args:
        query: 用户搜索需求
        candidates: 候选人 meta 信息列表
        config: Agent 配置
        max_workers: 最大并发数

    Returns:
        {candidate_id: {"score": int, "comment": str, "name": str}}
    """
    results: Dict[int, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for c in candidates:
            cid = c.get("id", 0)
            name = c.get("name", "?")
            future = executor.submit(evaluate, query, c, config)
            future_map[future] = (cid, name)

        for future in as_completed(future_map):
            cid, name = future_map[future]
            try:
                result = future.result()
                if result:
                    results[cid] = {
                        "name": name,
                        "score": result.final_score,
                        "education_score": result.education_score,
                        "experience_score": result.experience_score,
                        "skill_score": result.skill_score,
                        "comment": result.comment,
                    }
                else:
                    logger.warning(f"Comment failed for {name}, skipping")
            except Exception as e:
                logger.warning(f"Comment exception for {name}: {e}")

    logger.info(f"Comment batch: {len(results)}/{len(candidates)} succeeded")
    return results
