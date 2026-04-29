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
- Phase 划分依据：按「基础设施 → Agent 循环 → 现有代码迁移」三层递进。每一层都建立在下一层基础上，且每层可独立验证。

## Phase 1：Agent 基础设施层

### 任务背景
当前没有任何 Agent 抽象。每次 LLM 调用都要重复写 `extra_body`、`temperature`、`response_format` 等参数。工具调用没有统一的错误处理。需要建立可复用的基础设施。

### 预期达到的任务效果
- `AgentConfig` 类可用，支持每个 Agent 独立配置
- `BaseTool` 抽象基类可用，子类只需实现 `_execute()`
- `ToolResult` 统一工具返回值，永远包含 `success` 标志
- `StructuredOutput` 静态方法可用，统一处理 `response_format` + 重试 + 降级
- 所有基础组件有单元测试覆盖

### 预计修改的文件范围
- 新增：`services/agent_config.py`
  - `AgentConfig` dataclass
  - `extra_body` 属性（根据 enable_thinking 生成）
- 新增：`services/tool_base.py`
  - `ToolResult` Pydantic model
  - `BaseTool` ABC（`execute()` 包 try/except，`_execute()` 由子类实现）
  - `to_openai_tool()` 方法生成 tool JSON schema
- 新增：`services/llm_utils.py`
  - `StructuredOutput.parse(model_class, messages, config, retries, fallback)`
  - 内部用 `client.beta.chat.completions.parse()` 或 `response_format`
  - 自动重试 + 降级默认值

### 必须涵盖的测试方向
- `uv run python3 -c "from services.agent_config import AgentConfig; c = AgentConfig(name='test', enable_thinking=False); print(c.extra_body)"` — 验证 extra_body 生成正确
- `uv run python3 -c "from services.tool_base import BaseTool, ToolResult"` — 验证 import 正常
- 写一个 TestTool(BaseTool) 子类，测试 `execute()` 在子类 `_execute()` 抛异常时返回 `ToolResult(success=False, error=...)` 而非崩溃
- `uv run python3 -c "from services.llm_utils import StructuredOutput"` — 验证 import 正常

### 最终验收标准
- AgentConfig(enable_thinking=False).extra_body → `{"chat_template_kwargs": {"enable_thinking": False}}`
- AgentConfig(enable_thinking=True).extra_body → `{}`
- BaseTool 子类的 execute() 永不往外抛异常
- StructuredOutput.parse() 在 LLM 返回合法 JSON 时返回 Pydantic 对象，失败时返回 fallback
- 现有功能完全不受影响（不修改任何 handler）

### Commit Message
```
feat: add AgentConfig, BaseTool, ToolResult, StructuredOutput infrastructure
```

## Phase 2：Agent 循环 + 工具定义

### 任务背景
基础设施已就绪，但缺少工具调用循环。需要实现 AgentLoop 让 LLM 能自主决策调工具，以及定义具体工具（`search_resumes` 等）。Phase 2 不改造现有 handler，新功能用 AgentLoop 独立验证。

### 预期达到的任务效果
- `AgentLoop` 类可用，接收 `AgentConfig` + 工具列表
- 支持原生 `tools` 参数驱动的决策循环
- 工具执行结果正确追加到 messages
- `MAX_TURNS` 安全阀生效
- 定义至少 2 个基础工具（`normal_chat`, `search_resumes`）作为 BaseTool 子类
- 工具可被 LLM 调用并返回正确结果

### 预计修改的文件范围
- 新增：`services/agent_loop.py`
  - `AgentLoop` 类
  - `run(user_input, history) -> str` 主入口
  - 循环逻辑：LLM 决策 → tool_calls 执行 → 结果追加 → 继续
  - MAX_TURNS 安全阀
- 新增：`services/tools/__init__.py` — 工具包
- 新增：`services/tools/normal_chat.py` — 纯聊天工具（占位，用于主 LLM 无搜索需求时）
- 新增：`services/tools/search_resumes.py` — 搜索简历工具
  - 内部调用现有的 `resume_searcher.search_resumes()` 和 `keyword_extractor.extract_keywords()`
  - 返回 `ToolResult(success=True, data=[...])`
- 可能新增：`services/tools/process_resume_pdf.py` — 文件处理工具（框架预留，实际仍由代码驱动）

### 必须涵盖的测试方向
- 孤立测试 AgentLoop 循环逻辑：mock LLM 返回，验证 tool_calls 被正确执行
- `uv run python3 -c "from services.tools.search_resumes import SearchResumesTool"` — 验证 import
- 验证 `SearchResumesTool._execute(keywords=[...])` 返回正确的简历数据
- 验证 `to_openai_tool()` 输出的 JSON Schema 格式正确

### 最终验收标准
- AgentLoop 一次运行中：LLM 返回 tool_calls → 执行工具 → 结果追加 → LLM 继续决策
- AgentLoop 在 LLM 返回普通消息时立即结束
- AgentLoop 超过 MAX_TURNS 返回超时提示
- SearchResumesTool 能正确调用 FTS5 搜索并返回结构化结果
- 现有 handler 不受影响

### Commit Message
```
feat: add AgentLoop with tool-calling loop and search_resumes tool
```

## Phase 3：现有代码迁移改造

### 任务背景
基础设施和 Agent 循环已就绪。现在改造现有代码使用新基础设施：
1. `keyword_extractor.py` 改用 `StructuredOutput.parse()`
2. `resume_handler.py` 分析输出拆为 display + meta
3. `text_handler.py` 文字消息集成 AgentLoop
4. `config.py` 集成 AgentConfig

### 预期达到的任务效果
- `keyword_extractor.py` 内部使用 `StructuredOutput.parse(KeywordExtraction, ...)`，输出是 Pydantic 对象
- `resume_handler.py` 的 LLM 分析输出同时包含 `display`（给用户看的卡片文本）和 `meta`（给机器入库的结构化数据）
- `text_handler.py` 集成 AgentLoop：文字消息走 Agent 决策
- `config.py` 包含 `chat_agent`、`keyword_agent` 等配置字段

### 预计修改的文件范围
- 修改：`services/keyword_extractor.py`
  - 定义 `KeywordExtraction` Pydantic model
  - 内部调用 `StructuredOutput.parse(KeywordExtraction, ...)`
  - 保留原有 `extract_keywords()` 接口签名（不破坏调用方）
- 修改：`services/handlers/resume_handler.py`
  - 定义 `ResumeDisplay`（用户可见的文本）和 `ResumeMeta`（入库字段）两个 Pydantic model
  - LLM 分析输出同时包含 display 和 meta
  - display 给用户看，meta 传给 `index_resume()`
- 修改：`services/handlers/text_handler.py`
  - 文字消息处理改为调用 `AgentLoop.run()`
  - AgentLoop 的 tools 包含 `search_resumes` 和 `normal_chat`
  - 保持系统提示词动态时间前缀
  - 保留思考卡片（thinking card）逻辑
- 修改：`config.py`
  - 新增 `chat_agent: AgentConfig` 字段
  - 新增 `keyword_agent: AgentConfig` 字段
  - 新增 `analysis_agent: AgentConfig` 字段（用于 resume_handler 的 LLM 分析）

### 必须涵盖的测试方向
- `uv run python3 -c "from services.keyword_extractor import extract_keywords; print(extract_keywords('找复旦的CTA', config))"` — 验证返回关键词列表
- `uv run python3 -c "from services.handlers.resume_handler import ResumeDisplay, ResumeMeta; print('Pydantic OK')"` — 验证 Pydantic 模型可用
- E2E 测试：上传 PDF → 分析 → display 展示给用户，meta 入库
- E2E 测试：发文字"帮我找复旦的CTA实习生" → AgentLoop 触发 search_resumes → 返回结果 → 回复
- E2E 测试：发文字"你好" → AgentLoop 无工具调用 → 正常聊天
- E2E 测试：发文字触发多次工具调用 → 不超过 MAX_TURNS

### 最终验收标准
- keyword_extractor 输出始终是合法的 KeywordExtraction 结构
- resume_handler 卡片回复展示 display 字段，入库使用 meta 字段
- text_handler 文字消息走 AgentLoop，LLM 自主决定是否调工具
- config.py 中 chat_agent/keyword_agent/analysis_agent 均可从环境变量读取
- 所有现有功能（PDF 上传、会话管理、卡片回复）不受影响
- Docker 构建成功，容器运行正常

### Commit Message
```
refactor: migrate keyword extraction, resume analysis, and chat handler to agent architecture
```
