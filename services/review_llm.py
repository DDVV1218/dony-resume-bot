"""Review LLM — 硬性条件审查

对 Reranker 后的候选批量进行硬性条件审查。
两轮机制：
- Round 1: 初判每人 pass/fail
- Round 2 (Reflect): 自我核查，修正错误
"""

import json
import logging
from typing import Any, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from prompts import load_prompt
from services.agent_config import AgentConfig

logger = logging.getLogger(__name__)


class ReviewVerdict(BaseModel):
    """单个人的审查结论"""
    id: int = Field(description="候选人 ID")
    name: str = Field(description="候选人姓名")
    verdict: str = Field(description="pass 或 fail", pattern="^(pass|fail)$")
    reason: str = Field(description="判断原因（10-30字）")


class ReviewRound(BaseModel):
    """一轮审查结果"""
    results: List[ReviewVerdict] = Field(description="审查结果列表")


def _build_input(
    query: str,
    candidates: List[Dict[str, Any]],
) -> str:
    """构建审查输入文本"""
    lines = [f"## 用户需求\n{query}\n", "## 候选人列表\n"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"### {i}. {c.get('name', '未知')} (ID: {c.get('id', '?')})")
        lines.append(f"- 学校/学历: {c.get('school', '未知')} / {c.get('degree', '未知')}")
        lines.append(f"- 技能: {c.get('skills', '未知')}")
        lines.append(f"- 公司: {c.get('company', '未知')}")
        exp_detail = c.get('experience_detail', '')
        if exp_detail:
            lines.append(f"- 经历详情: {exp_detail}")
        skills_detail = c.get('skills_detail', '')
        if skills_detail:
            lines.append(f"- 技能详情: {skills_detail}")
        lines.append("")
    return "\n".join(lines)


def _call_review(
    messages: List[Dict[str, str]],
    config: AgentConfig,
) -> Optional[ReviewRound]:
    """调用 LLM 进行一轮审查"""
    try:
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        response = client.beta.chat.completions.parse(
            model=config.model,
            messages=messages,
            response_format=ReviewRound,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            timeout=30.0,
            extra_body=config.extra_body,
        )
        result: Optional[ReviewRound] = response.choices[0].message.parsed
        return result
    except Exception as e:
        logger.error(f"Review LLM call failed: {e}")
        return None


def batch_review(
    query: str,
    candidates: List[Dict[str, Any]],
    config: AgentConfig,
) -> Optional[List[ReviewVerdict]]:
    """对候选人列表进行硬性条件审查（含 Reflect）

    Args:
        query: 用户搜索需求
        candidates: 候选人 meta 信息列表
        config: Agent 配置

    Returns:
        审查结果列表，每人 pass/fail。失败返回 None（降级）。
    """
    if not candidates:
        return []

    prompt = load_prompt("review_llm")
    input_text = _build_input(query, candidates)

    # === Round 1: 初判 ===
    logger.info(f"Review Round 1: {len(candidates)} candidates")
    r1_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"请审查以下候选人的硬性条件是否符合需求：\n\n{input_text}"},
    ]
    r1_result = _call_review(r1_messages, config)
    if r1_result is None:
        return None

    # === Round 2: Reflect 自我核查 ===
    logger.info("Review Round 2 (Reflect): self-check")
    r2_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"以下是第一轮审查的结果，请自我核查是否有误判。\n\n## 用户需求\n{query}\n\n## 候选人列表\n{input_text}\n\n## 第一轮审查结果\n```json\n{r1_result.model_dump_json(indent=2)}\n```\n\n请仔细检查每一条判断。特别关注：\n1. 是否有符合条件的候选人被错误淘汰了？检查经历详情中是否隐含了匹配的经验。\n2. 是否有不符合条件的候选人被错误通过？\n3. 如果发现错误，请修正。\n4. 如果第一轮判断全部正确，请保持原结果。\n\n注意：宁过勿杀，不确定时默认通过。\n输出最终审查结果。"},
    ]
    r2_result = _call_review(r2_messages, config)
    if r2_result is None:
        # Round 2 失败，使用 Round 1 结果
        logger.warning("Review Round 2 failed, using Round 1 results")
        return r1_result.results

    # 统计修正情况
    r1_verdicts = {v.id: v.verdict for v in r1_result.results}
    r2_verdicts = {v.id: v.verdict for v in r2_result.results}
    changes = sum(1 for vid in r1_verdicts if r1_verdicts.get(vid) != r2_verdicts.get(vid))
    if changes > 0:
        logger.info(f"Review Reflect corrected {changes} verdict(s)")

    return r2_result.results
