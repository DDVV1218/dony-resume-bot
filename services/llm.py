"""LLM 服务层 - OpenAI Chat Completions、Token 估算、Auto-Compact"""

import logging
from typing import Dict, List, Optional

import tiktoken
from openai import OpenAI

from config import Config
from prompts import load_prompt

logger = logging.getLogger(__name__)

# 全局 OpenAI 客户端（延迟初始化）
_client: Optional[OpenAI] = None


def get_client(config: Config) -> OpenAI:
    """获取或创建 OpenAI 客户端"""
    global _client
    if _client is None:
        kwargs = {"api_key": config.openai_api_key}
        if config.openai_base_url:
            kwargs["base_url"] = config.openai_base_url
        _client = OpenAI(**kwargs)
    return _client


# tiktoken 编码器缓存
_encoders: Dict[str, tiktoken.Encoding] = {}


def _get_encoder(model: str = "gpt-4o") -> tiktoken.Encoding:
    """获取 tiktoken 编码器"""
    if model not in _encoders:
        try:
            _encoders[model] = tiktoken.encoding_for_model(model)
        except KeyError:
            # 如果模型没有对应的编码器，使用 cl100k_base（gpt-4 系列）
            logger.warning(f"tiktoken: model '{model}' not found, using cl100k_base")
            _encoders[model] = tiktoken.get_encoding("cl100k_base")
    return _encoders[model]


def estimate_tokens(messages: List[Dict[str, str]], model: str = "gpt-4o") -> int:
    """估算消息列表的 Token 数

    基于 tiktoken，按 OpenAI 的 token 计数规则估算。
    每条消息大约额外消耗 3-4 tokens（role + metadata）。

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}, ...]
        model: OpenAI 模型名称

    Returns:
        预估的总 Token 数
    """
    enc = _get_encoder(model)
    total = 0
    for msg in messages:
        # 每条消息约 3 tokens 的 overhead（role + 格式）
        total += 3
        # 内容按字符编码估算
        total += len(enc.encode(msg.get("content", "")))
    # 预留一些 overhead
    total += 3
    return total


def chat(messages: List[Dict[str, str]], config: Config) -> str:
    """调用 OpenAI Chat Completions API

    Args:
        messages: 消息列表
        config: 配置

    Returns:
        LLM 回复文本

    Raises:
        Exception: API 调用异常
    """
    client = get_client(config)
    response = client.chat.completions.create(
        model=config.openai_model,
        messages=messages,
        temperature=config.openai_temperature,
    )
    return response.choices[0].message.content


def compact_messages(
    messages: List[Dict[str, str]],
    config: Config,
) -> List[Dict[str, str]]:
    """压缩消息列表，控制 Token 数

    策略：
    1. 保留 system message（第 1 条）
    2. 保留最近 15% context_window 的对话
    3. 中间部分发给 LLM 生成摘要
    4. 返回 [system, summary, ...recent_15%]

    Args:
        messages: 完整消息列表（含 system）
        config: 配置

    Returns:
        压缩后的消息列表
    """
    if len(messages) <= 2:
        return messages

    # 分离 system 和对话消息
    system_msgs = [m for m in messages if m["role"] == "system"]
    chat_msgs = [m for m in messages if m["role"] != "system"]

    if not chat_msgs:
        return messages

    # 计算最近 15% 的 token 数
    recent_tokens = config.compact_recent_tokens

    # 从后往前累积，找到最近 15% 的起始位置
    accumulated = 0
    split_idx = 0
    for i in range(len(chat_msgs) - 1, -1, -1):
        msg_tokens = estimate_tokens([chat_msgs[i]])
        accumulated += msg_tokens
        if accumulated >= recent_tokens:
            split_idx = i
            break

    # 中间部分（需要压缩的）
    middle_msgs = chat_msgs[:split_idx]
    # 最近部分（保留的）
    recent_msgs = chat_msgs[split_idx:]

    if not middle_msgs:
        return messages

    # 构建摘要 prompt
    conversation_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in middle_msgs
    )
    compact_prompt_template = load_prompt("compact_prompt")
    summary_content = compact_prompt_template.format(
        conversation_history=conversation_text
    )

    try:
        summary = chat(
            [{"role": "user", "content": summary_content}],
            config,
        )
    except Exception as e:
        logger.error(f"Compact failed: {e}")
        # 压缩失败，直接返回最近部分 + system
        summary = "[摘要生成失败，保留最近对话历史]"

    # 构建压缩后的消息列表
    return [
        *system_msgs,
        {"role": "assistant", "content": f"[本次对话之前的摘要]\n{summary}"},
        *recent_msgs,
    ]


def prepare_context(
    messages: List[Dict[str, str]],
    config: Config,
) -> List[Dict[str, str]]:
    """准备发送给 LLM 的上下文

    自动判断是否需要 compact：
    - 如果 token 数 < 85% context_window，直接返回
    - 如果 >= 85%，触发 compact

    Args:
        messages: 完整消息列表
        config: 配置

    Returns:
        准备就绪的消息列表（token 数在安全范围内）
    """
    total_tokens = estimate_tokens(messages, config.openai_model)
    trigger = config.compact_trigger_tokens

    logger.info(
        f"Context: {total_tokens} tokens / {config.openai_context_window} "
        f"({total_tokens / config.openai_context_window * 100:.1f}%) "
        f"threshold={trigger}"
    )

    if total_tokens < trigger:
        return messages

    logger.info(f"Triggering compact: {total_tokens} >= {trigger}")
    return compact_messages(messages, config)
