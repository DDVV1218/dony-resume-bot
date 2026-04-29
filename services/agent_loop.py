"""Agent 工具调用循环

使用 OpenAI 原生 tools 参数驱动 LLM 自主决策。
支持工具的注册、调用、结果反馈，以及无限循环安全阀。
"""

import json
import logging
from typing import Dict, List, Optional

from openai import OpenAI

from services.agent_config import AgentConfig
from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class AgentLoop:
    """Agent 主循环

    核心流程：
    1. LLM 收到消息 + tools 定义
    2. LLM 决定：回答（无 tool_calls）→ 结束
    3. LLM 决定：调用工具（有 tool_calls）→ 执行 → 结果追加 → 回到步骤 1
    4. 超出 MAX_TURNS → 中断

    使用方式：
        loop = AgentLoop(config=chat_config, tools=[SearchResumesTool(), NormalChatTool()])
        reply = loop.run(messages=[system_msg, user_msg])
    """

    def __init__(
        self,
        config: AgentConfig,
        tools: List[BaseTool],
    ):
        """初始化 Agent 循环

        Args:
            config: Agent 配置（模型、温度、max_loop_turns 等）
            tools: 可用的工具列表（BaseTool 子类实例）
        """
        self.config = config
        self.tools = {t.name: t for t in tools}

    def run(
        self,
        messages: List[Dict[str, str]],
        *,
        tool_choice: str = "auto",
        verbose: bool = False,
    ) -> str:
        """执行 Agent 循环

        Args:
            messages: 初始消息列表（应包含 system + history + user）
            tool_choice: "auto"(默认) / "none" / "required"
            verbose: 是否输出详细日志

        Returns:
            LLM 最终回复文本

        Raises:
            RuntimeError: 工具不存在等内部错误
        """
        client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
        )

        # 构建 tools 参数
        openai_tools = [t.to_openai_tool() for t in self.tools.values()]

        for turn in range(self.config.max_loop_turns):
            if verbose:
                logger.info(
                    f"AgentLoop turn {turn + 1}/{self.config.max_loop_turns}"
                )

            # 调用 LLM
            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                extra_body=self.config.extra_body,
            )

            msg = response.choices[0].message

            # 情况 1：LLM 决定直接回答（无工具调用）
            if not msg.tool_calls and msg.content:
                if verbose:
                    logger.info("AgentLoop: LLM replied directly, ending loop")
                return msg.content

            # 情况 2：LLM 决定调用工具
            if msg.tool_calls:
                if verbose:
                    logger.info(
                        f"AgentLoop: LLM called {len(msg.tool_calls)} tool(s)"
                    )

                # 保存 assistant 消息（含 tool_calls）
                messages.append(msg.model_dump(
                    exclude={"refusal", "function_call", "audio"},
                    exclude_none=True,
                ))

                # 逐个执行工具
                for tc in msg.tool_calls:
                    tool = self.tools.get(tc.function.name)
                    if tool is None:
                        error_result = ToolResult(
                            success=False,
                            error=f"未知工具: {tc.function.name}",
                        )
                        messages.append(error_result.to_llm_message(tc.id))
                        logger.warning(
                            f"AgentLoop: unknown tool '{tc.function.name}'"
                        )
                        continue

                    # 解析参数
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        error_result = ToolResult(
                            success=False,
                            error=f"工具参数解析失败: {e}",
                        )
                        messages.append(error_result.to_llm_message(tc.id))
                        continue

                    # 执行工具（统一异常处理在 BaseTool.execute 内部）
                    result = tool.execute(**args)
                    messages.append(result.to_llm_message(tc.id))

                    if verbose:
                        logger.info(
                            f"AgentLoop: tool '{tc.function.name}' "
                            f"-> success={result.success}"
                        )

                # 继续下一轮循环（让 LLM 看到工具结果后决策）
                continue

            # 情况 3：LLM 返回了 tool_calls 但 content 不为空，或特殊情况
            # 某些模型可能同时返回 content 和 tool_calls
            if msg.content:
                return msg.content

            # 安全兜底：如果 model 返回了空响应
            logger.warning(
                f"AgentLoop: empty response at turn {turn + 1}, retrying"
            )
            if turn == self.config.max_loop_turns - 1:
                return "抱歉，我暂时无法处理您的请求，请稍后再试。"

        # 超出 MAX_TURNS
        logger.warning(
            f"AgentLoop: exceeded max_loop_turns={self.config.max_loop_turns}"
        )
        return "处理步骤较多，请简单描述您的需求后重试。"
