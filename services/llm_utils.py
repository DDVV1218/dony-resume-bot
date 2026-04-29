"""LLM 工具函数 - 结构化输出解析、重试、降级

为 Agent 系统提供统一的结构化输出能力。
支持 Pydantic + response_format 模式，自动重试和降级。
"""

import json
import logging
from typing import Any, Callable, Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from services.agent_config import AgentConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LogStructuredParseError(Exception):
    """结构化输出解析失败（重试可恢复）"""


class StructuredOutput:
    """结构化输出工具

    统一处理：
    1. 优先尝试 client.beta.chat.completions.parse()（最精确）
    2. 失败后降级到 response_format json_object + Pydantic 校验
    3. 最终 fallback 到用户提供的默认值

    使用方式：
        result = StructuredOutput.parse(
            model_class=KeywordExtraction,
            messages=[{"role": "user", "content": query}],
            config=keyword_config,
        )
    """

    DEFAULT_RETRIES = 2
    DEFAULT_TIMEOUT = 15.0

    @staticmethod
    def parse(
        model_class: Type[T],
        messages: list[dict],
        config: AgentConfig,
        *,
        retries: int = DEFAULT_RETRIES,
        fallback: Optional[T] = None,
        fallback_factory: Optional[Callable[[], T]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: Optional[int] = None,
    ) -> T:
        """调用 LLM 并解析为指定 Pydantic 模型

        Args:
            model_class: 目标 Pydantic 模型类
            messages: LLM 消息列表
            config: Agent 配置
            retries: 解析失败重试次数
            fallback: 全失败时返回的默认值
            fallback_factory: 全失败时生成默认值的工厂函数
            timeout: LLM 调用超时
            max_tokens: 最大生成 token 数（覆盖 config.max_tokens）

        Returns:
            解析后的 Pydantic 对象

        Raises:
            ValueError: 如果既没有 fallback 也没有 fallback_factory
        """
        client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

        actual_max_tokens = max_tokens or config.max_tokens

        # 第一轮用 beta.parse（原生结构化输出）
        for attempt in range(retries + 1):
            try:
                response = client.beta.chat.completions.parse(
                    model=config.model,
                    messages=messages,
                    response_format=model_class,  # type: ignore[arg-type]
                    temperature=config.temperature,
                    max_tokens=actual_max_tokens,
                    timeout=timeout,
                    extra_body=config.extra_body,
                )
                msg = response.choices[0].message
                # OpenAI v2.x: parsed 属性存在且类型正确
                if msg.parsed is not None:
                    result: T = msg.parsed  # type: ignore[assignment]
                    logger.info(
                        f"StructuredOutput.parse success (attempt {attempt + 1}): "
                        f"{model_class.__name__}"
                    )
                    return result
                logger.info(
                    f"StructuredOutput.parse success (attempt {attempt + 1}): "
                    f"{model_class.__name__}"
                )
                return result

            except Exception as e:
                logger.warning(
                    f"StructuredOutput beta.parse failed (attempt {attempt + 1}/{retries + 1}): "
                    f"{e}"
                )
                if attempt < retries:
                    continue

                # 所有重试结束，降级到 response_format json_object
                logger.info(
                    f"Falling back to json_object mode for {model_class.__name__}"
                )
                try:
                    response = client.chat.completions.create(
                        model=config.model,
                        messages=messages,
                        response_format={"type": "json_object"},
                        temperature=config.temperature,
                        max_tokens=actual_max_tokens,
                        timeout=timeout,
                        extra_body=config.extra_body,
                    )
                    raw = response.choices[0].message.content
                    if raw:
                        try:
                            data = json.loads(raw)
                            return model_class.model_validate(data)
                        except (json.JSONDecodeError, ValidationError) as parse_err:
                            logger.warning(
                                f"StructuredOutput json_object fallback parse failed: "
                                f"{parse_err}"
                            )
                except Exception as e2:
                    logger.warning(
                        f"StructuredOutput json_object fallback call failed: {e2}"
                    )

                # 全部失败，使用 fallback
                if fallback is not None:
                    return fallback
                if fallback_factory is not None:
                    return fallback_factory()
                raise ValueError(
                    f"StructuredOutput.parse failed for {model_class.__name__} "
                    f"after {retries + 1} retries and no fallback provided"
                )

        # 不应该走到这里，但 type checker 需要
        raise RuntimeError("Unexpected end of StructuredOutput.parse")
