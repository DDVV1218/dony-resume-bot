"""提示词管理 - 加载 prompts/ 目录下的模板文件"""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """加载提示词模板

    Args:
        name: 模板名称（不含扩展名），如 'system_prompt', 'compact_prompt'

    Returns:
        提示词内容字符串

    Raises:
        FileNotFoundError: 模板文件不存在
    """
    prompt_file = _PROMPTS_DIR / f"{name}.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt template not found: {prompt_file}")
    return prompt_file.read_text(encoding="utf-8")
