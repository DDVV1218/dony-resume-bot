"""普通聊天工具

当 LLM 认为用户消息不需要搜索简历库时使用此工具。
仅作为占位工具，帮助 LLM 理解这是一个不需要搜索的场景。
"""

from pydantic import BaseModel

from services.tool_base import BaseTool, ToolResult


class NormalChatParams(BaseModel):
    """普通聊天参数"""
    response: str


class NormalChatTool(BaseTool):
    """普通聊天工具

    当用户问候、闲聊、或提问不涉及简历搜索时，
    LLM 应在回复前使用此工具表明使用纯聊天模式。
    """

    name: str = "normal_chat"
    description: str = (
        "当用户消息是问候、闲聊、感谢、或与简历库搜索无关的话题时，"
        "使用此工具进行普通聊天。不需要搜索简历库。"
    )
    parameters = NormalChatParams

    def _execute(self, response: str = "") -> ToolResult:
        """直接返回，LLM 会在下一轮用自然语言回复用户"""
        return ToolResult(success=True, data={"mode": "chat", "response": response})
