"""工具基类 - BaseTool 抽象 + ToolResult 统一返回值

所有 Agent 工具（搜索简历、处理 PDF、对话等）都继承 BaseTool，
通过统一的 execute() 入口确保异常不会泄露到 Agent 调用层。
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Type

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ToolResult(BaseModel):
    """工具执行结果，统一结构

    LLM 总是看到结构化的返回值，无论是成功还是失败。
    """

    success: bool
    data: Any = None
    error: Optional[str] = None

    def to_llm_message(self, tool_call_id: str) -> dict:
        """转换为 tool role 消息，追加到对话中"""
        content = (
            self.model_dump_json()
            if self.success
            else json.dumps({"success": False, "error": self.error})
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }


class BaseTool(ABC):
    """工具抽象基类

    子类只需实现：
    - `name`: 工具名（用于 LLM 调用）
    - `description`: 描述（帮助 LLM 决策何时调用）
    - `parameters`: Pydantic model（参数 JSON Schema）
    - `_execute(**kwargs) -> ToolResult`: 实际业务逻辑

    基类统一提供：
    - `execute(**kwargs) -> ToolResult`: 带 try/except 的安全入口
    - `to_openai_tool() -> dict`: OpenAI function calling schema
    """

    name: str = ""
    description: str = ""
    parameters: Type[BaseModel] = BaseModel  # type: ignore[assignment]

    @abstractmethod
    def _execute(self, **kwargs: Any) -> ToolResult:
        """子类实现具体业务逻辑

        只需要执行逻辑并返回 ToolResult，不需要处理异常。
        """
        ...

    def execute(self, **kwargs: Any) -> ToolResult:
        """统一安全入口，永远不往外抛异常

        任何异常都被 catch 并封装为 ToolResult(success=False, error=...) 返回。
        """
        try:
            return self._execute(**kwargs)
        except Exception as e:
            logger.error(f"Tool '{self.name}' execution failed: {e}", exc_info=True)
            return ToolResult(
                success=False,
                error=f"{self.name} 执行失败: {type(e).__name__}: {e}",
            )

    def to_openai_tool(self) -> dict:
        """生成 OpenAI function calling 格式的工具定义

        Returns:
            {
                "type": "function",
                "function": {
                    "name": ...,
                    "description": ...,
                    "parameters": { ... JSON Schema ... }
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters.model_json_schema(),
            },
        }
