# 多 Agent 架构重构

## 背景调研结论
- 输入类型：`detailed_plan`
- 当前现状：Phase 2 已实现 FTS5 检索、简历入库、LLM 关键词提取，但架构是**代码驱动**的。Handler 直接决定流程，LLM 只是被调用的工具，不知晓整体上下文。
- 已确认计划背景：
  - OneAPI + vLLM 已验证支持原生 `tools` 参数（function calling）
  - OneAPI + vLLM 已验证支持 `response_format={"type": "json_object"}` 和 `beta.parse()`
  - 当前两处 LLM 结构化输出（关键词提取、简历分析）使用纯文本解析，不够稳健
  - 后续工具会增多（搜索、对比、入库、统计等），需要一个统一管理机制
- 建议修改方向：从代码驱动改为 **Agent 驱动**，主 LLM 自主决策是否调工具
- Phase 划分依据：重构范围涉及多个文件，但功能间耦合高，建议一次性完成基础设施层再逐步改造上层

## Phase 1：多 Agent 基础设施 + 现有代码改造

### 任务背景
将当前代码驱动架构重构为 Agent 驱动架构。核心变化：
1. 新增 `AgentConfig` — 每个 Agent 独立配置（模型、地址、温度、思考模式等）
2. 新增 `BaseTool` + `ToolResult` — 工具基类，统一异常处理
3. 新增 `AgentLoop` — 工具调用循环，原生 `tools` 参数驱动
4. 改造 `keyword_extractor.py` — 改用 Pydantic + `response_format`
5. 改造 `resume_handler.py` 分析输出 — 拆分为 display + meta 两个 Pydantic 结构

### 预期达到的任务效果
- AgentConfig 可单独配置每个 agent 的 model/base_url/api_key/temperature/enable_thinking
- BaseTool 子类只需实现 `_execute()`，异常由基类统一兜底
- AgentLoop 使用 OpenAI 原生 `tools` 参数驱动，LLM 自主决策工具调用
- Tool 执行结果统一用 `ToolResult(success=True/False, data=..., error=...)` 返回给 LLM
- AgentLoop 有安全阀（MAX_TURNS），防止无限循环
- keyword_extractor 改用 `response_format` + Pydantic
- resume_handler 分析输出拆为 display（给用户看）+ meta（给机器入库）

### 预计修改的文件范围

**新增文件：**
- `services/agent_config.py` — AgentConfig dataclass
- `services/tool_base.py` — BaseTool 抽象基类 + ToolResult
- `services/agent_loop.py` — AgentLoop 工具调用循环

**修改文件：**
- `config.py` — 新增各 Agent 的配置字段，引用 AgentConfig
- `services/keyword_extractor.py` — 改用 Pydantic + `response_format`
- `services/handlers/resume_handler.py` — LLM 分析输出拆为 display + meta
- `services/handlers/text_handler.py` — 集成 AgentLoop（文字消息走 Agent 决策）
- `services/llm.py` — chat() 函数接收 extra_body，使用 AgentConfig.extra_body

### 必须涵盖的测试方向

**单元测试：**
- `AgentConfig.extra_body`：`enable_thinking=False` → `{"chat_template_kwargs": {"enable_thinking": False}}`
- `ToolResult.to_llm_message()`：返回格式正确
- `BaseTool.execute()`：成功返回 ToolResult(success=True)；异常返回 ToolResult(success=False, error=...)
- `AgentLoop`：MAX_TURNS 超限后强制返回

**集成测试：**
- `StructuredOutput.parse()`：LLM 正常返回 Pydantic 对象；LLM 返回非 JSON 时重试并降级
- keyword_extractor 新实现：关键词提取正确，支持多选/否定/特殊字符
- function calling 测试：`tools` 参数可正常被 LLM 识别和调用

**E2E 测试：**
- 发文字"帮我找复旦的CTA实习生" → AgentLoop 触发 search_resumes 工具 → 返回结果 → LLM 回答
- 发文字"你好" → AgentLoop 无工具调用 → 直接聊天
- 上传 PDF → TextHandler（代码驱动）→ process_resume_pdf → 入库 → 回复

### 最终验收标准
- AgentConfig 支持的配置项：name, model, base_url, api_key, temperature, max_tokens, max_loop_turns, system_prompt, enable_thinking
- BaseTool.execute() 永不往外抛异常，所有错误封装为 ToolResult
- AgentLoop 在收到 LLM 返回的 tool_calls 时正确执行工具，tool 结果正确追加到 messages，LLM 能基于结果继续决策
- AgentLoop 超过 MAX_TURNS 后断开循环，返回超时提示
- keyword_extractor 输出始终是合法的 `KeywordExtraction` 结构
- resume_handler 分析输出同时包含 `display`（用户可见）和 `meta`（机器入库）两个部分
- 现有所有功能（PDF 上传、聊天、会话管理）不受影响

### Commit Message
```
refactor: multi-agent architecture with BaseTool, AgentLoop and structured output
```
