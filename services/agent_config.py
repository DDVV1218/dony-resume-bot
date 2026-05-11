"""Agent 配置 - 每个 Agent 独立配置模型、参数、思考模式等"""

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """单个 Agent 的完整配置

    每个 Agent（聊天、关键词提取、简历分析等）使用独立的 LLM 配置，
    包括模型地址、API Key、温度参数等，不互相依赖。

    Attributes:
        name: Agent 标识名称
        model: 模型名称
        base_url: OpenAI API 兼容地址
        api_key: API Key
        temperature: 采样温度 (0.0-2.0)
        max_tokens: 单次最大生成 token 数
        max_loop_turns: AgentLoop 最大工具调用轮数（安全阀）
        system_prompt: 系统提示词
        enable_thinking: 是否启用思考模式（vLLM Qwen 专属）
    """

    name: str
    model: str = field(default="Qwen3.6-27B")
    base_url: str = field(default="http://localhost:3000/v1")
    api_key: str = field(default="")
    temperature: float = field(default=0.7)
    max_tokens: int = field(default=4096)
    max_loop_turns: int = field(default=20)
    system_prompt: str = field(default="")
    enable_thinking: bool = field(default=False)

    @property
    def extra_body(self) -> dict:
        """生成 LLM 调用时的额外参数

        主要用于 vLLM 特有参数透传：
        - enable_thinking=False → 显式关闭 Qwen 思考模式
        """
        body: dict = {}
        if not self.enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    @classmethod
    def from_env(
        cls,
        name: str,
        prefix: str,
        *,
        getenv,
        default_model: str = "Qwen3.6-27B",
        default_base_url: str = "http://localhost:3000/v1",
        default_api_key: str = "",
    ) -> "AgentConfig":
        """从环境变量构建 AgentConfig

        每个 Agent 的环境变量命名规则：
        {PREFIX}_MODEL
        {PREFIX}_BASE_URL
        {PREFIX}_API_KEY
        {PREFIX}_TEMPERATURE
        {PREFIX}_MAX_TOKENS
        {PREFIX}_MAX_LOOP_TURNS
        {PREFIX}_ENABLE_THINKING

        Args:
            name: Agent 名称
            prefix: 环境变量前缀（如 CHAT、KEYWORD）
            getenv: os.getenv 函数
            default_model: 默认模型名
            default_base_url: 默认 API 地址
            default_api_key: 默认 API Key

        Returns:
            AgentConfig 实例
        """
        return cls(
            name=name,
            model=getenv(f"{prefix}_MODEL", default_model),
            base_url=getenv(f"{prefix}_BASE_URL", default_base_url),
            api_key=getenv(f"{prefix}_API_KEY", default_api_key),
            temperature=float(getenv(f"{prefix}_TEMPERATURE", "0.7")),
            max_tokens=int(getenv(f"{prefix}_MAX_TOKENS", "65536")),
            max_loop_turns=int(getenv(f"{prefix}_MAX_LOOP_TURNS", "20")),
            enable_thinking=getenv(f"{prefix}_ENABLE_THINKING", "false").lower()
            in ("true", "1", "yes"),
        )
