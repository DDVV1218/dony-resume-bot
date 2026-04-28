# Phase 1: 飞书对话助手基础

## 背景调研结论
- 输入类型：`detailed_plan`
- 当前现状：`apps/resume-bot/` 目录已初始化（git + uv），仅有空的 `pyproject.toml` 和 `main.py` 骨架，无任何业务代码。数据目录 `/data/turing-apps/resume-bot/` 已创建（uploads/chroma_db/sessions）。设计文档位于 `docs/superpowers/specs/2026-04-28-resume-bot-design.md`。
- 已确认问题 / 计划背景：需要构建飞书简历库 Bot 的基础对话能力，包括飞书 WebSocket 长连接、Session 管理（多用户隔离、全部历史持久化）、LLM 对话（Chat Completions API）、上下文 compact 机制、`/status` 命令。
- 根因判断 / 约束核对：
  - 飞书 SDK `lark-oapi` 支持 WebSocket 长连接（`lark.ws.Client`），无需公网 IP，仅需出站 HTTPS
  - OpenAI SDK 仅使用 `chat.completions.create()`，不支持内置记忆，需自行管理
  - Token 计数使用 `tiktoken` 本地估算
  - Session 数据以 JSON 文件持久化到 `/data/turing-apps/resume-bot/sessions/`
  - Python 环境使用 `uv` 管理，禁止使用其他 Python 环境
  - 项目最终以 Docker 打包，数据目录通过 Volume 挂载
- 建议修改方向：按依赖关系分 5 个 Phase 递进实现，每 Phase 可独立验证。先底层（配置 → Session → LLM），再上层（飞书交互），最后打包（Docker + 入口）。
- Phase 划分依据：按子系统依赖拆分——配置层是 Session 的前置，Session 是 LLM 的前置，LLM 和 Session 是飞书交互的前置，飞书交互是 Docker 入口的前置。

## Phase 1：项目初始化与配置层

### 任务背景
建立项目的基础设施：依赖管理、配置读取、提示词模板。这是所有后续模块的前置依赖。

### 预期达到的任务效果
项目配置可正常加载（环境变量/`.env`），提示词模板可被读取，依赖声明完整。

### 预计修改的文件范围
- `修改：pyproject.toml` — 添加依赖（`lark-oapi`, `openai`, `tiktoken`, `python-dotenv`）
- `新增：config.py` — 配置类（数据class），从环境变量读取飞书/AppID/Secret、OpenAI/Key/Model/ContextWindow、SessionsDir
- `新增：prompts/system_prompt.md` — 系统提示词模板（通用助手角色）
- `新增：prompts/compact_prompt.md` — 压缩摘要提示词
- `新增：.env.example` — 环境变量模板（不含真实密钥）
- `新增：prompts/__init__.py` — prompts 包初始化，提供 `load_prompt(name)` 函数

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from config import Config; c = Config(); print(c.model)"` — 验证配置加载
- `cd apps/resume-bot && uv run python -c "from prompts import load_prompt; print(load_prompt('system_prompt'))"` — 验证提示词加载
- `cd apps/resume-bot && uv run python -c "from config import Config; c = Config(); assert c.openai_model"` — 验证字段非空

### 最终验收标准
- `config.Config()` 可正常实例化，所有字段有默认值或从环境变量读取
- `prompts.load_prompt('system_prompt')` 和 `prompts.load_prompt('compact_prompt')` 返回非空字符串
- 所有依赖通过 `uv sync` 成功安装

### Commit Message
`feat: add project config, prompts, and dependencies`

## Phase 2：Session 管理层

### 任务背景
实现多用户/多群的 Session 隔离与持久化。Session 数据存储在 `/data/turing-apps/resume-bot/sessions/` 下，支持创建、读取、追加消息、切换、列出等操作。

### 预期达到的任务效果
Session 的完整 CRUD 操作可用，数据以 JSON 文件持久化，支持并发安全（同 Session 加锁）。

### 预计修改的文件范围
- `新增：services/__init__.py` — services 包初始化
- `新增：services/session.py` — Session 管理核心逻辑，包含：
  - `Session` dataclass（id, session_key, created_at, updated_at, messages）
  - `SessionStore` 类：
    - `get_or_create(session_key)` → 获取或创建 active session
    - `append_message(session_key, role, content)` → 追加消息并保存
    - `get_messages(session_key)` → 获取全部消息（含 system）
    - `list_sessions(session_key)` → 列出该用户/群的所有 session
    - `switch_session(session_key, session_id)` → 切换 active session
    - `create_session(session_key)` → 创建新 session
    - `delete_session(session_key, session_id)` → 删除指定 session
    - 每个 session_key 使用 `threading.Lock` 保证串行

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from services.session import SessionStore; store = SessionStore('./test_sessions'); s = store.get_or_create('dm:test'); store.append_message('dm:test', 'user', 'hello'); msgs = store.get_messages('dm:test'); assert len(msgs) >= 1; print('OK')"` — 验证 CRUD
- `cd apps/resume-bot && uv run python -c "from services.session import SessionStore; import os, shutil; store = SessionStore('./test_sessions2'); store.get_or_create('dm:a'); store.create_session('dm:a'); sessions = store.list_sessions('dm:a'); assert len(sessions) == 2; shutil.rmtree('./test_sessions2'); print('OK')"` — 验证多 session
- 验证 JSON 文件格式正确（包含 id, session_key, created_at, messages 字段）

### 最终验收标准
- `SessionStore.get_or_create()` 首次调用创建 `001.json` 和 `active.txt`，再次调用返回同一 session
- `SessionStore.append_message()` 追加消息后 JSON 文件实时更新
- `SessionStore.list_sessions()` 返回所有 session 列表
- `SessionStore.create_session()` 创建新编号 session 并切换 active
- 并发安全：同 session_key 的多次操作不产生数据竞争

### Commit Message
`feat: implement session management with JSON persistence and thread safety`

## Phase 3：LLM 服务层

### 任务背景
实现 OpenAI Chat Completions 调用、Token 估算、Compact 机制。Session 的全部历史保存在磁盘，但发送给 API 时需控制 Token 不超过 context window 的 85%。

### 预期达到的任务效果
LLM 调用正常返回，Token 超限 85% 时自动触发 compact，压缩后 Token 恢复到安全范围内。

### 预计修改的文件范围
- `新增：services/llm.py` — LLM 服务核心逻辑，包含：
  - `estimate_tokens(messages)` — 使用 tiktoken 估算总 Token 数
  - `chat(messages)` — 调用 OpenAI Chat Completions API，返回 assistant 回复
  - `compact_messages(messages, context_window, compact_prompt)` — 压缩逻辑：
    1. 保留 system message（第 1 条）
    2. 计算最近 15% token 的起始位置
    3. 中间部分发给 LLM 生成摘要
    4. 返回 `[system, summary, ...recent_15%]`
  - `prepare_context(session_messages, context_window, compact_threshold, compact_prompt)` — 完整流程：估算 → 判断是否 compact → 返回最终消息列表

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from services.llm import estimate_tokens; msgs = [{'role':'user','content':'hello world'}]; t = estimate_tokens(msgs); assert t > 0; print(f'tokens: {t}')"` — 验证 Token 估算
- `cd apps/resume-bot && uv run python -c "from services.llm import chat; reply = chat([{'role':'user','content':'say hi'}]); print(reply[:50])"` — 验证 LLM 调用（需 OPENAI_API_KEY）
- `cd apps/resume-bot && uv run python -c "from services.llm import compact_messages; msgs = [{'role':'system','content':'test'}] + [{'role':'user','content':'x'} if i%2==0 else {'role':'assistant','content':'y'*1000} for i in range(200)]; compacted = compact_messages(msgs, 128000, 'summarize'); assert len(compacted) < len(msgs); assert compacted[0]['role'] == 'system'; print(f'{len(msgs)} -> {len(compacted)}')" ` — 验证 Compact 逻辑

### 最终验收标准
- `estimate_tokens()` 返回的 Token 数与 OpenAI 实际返回的 `usage.total_tokens` 误差在 ±10% 以内
- `chat()` 正常调用 OpenAI API 并返回文本
- `compact_messages()` 在消息超过 context_window * 85% 时正确压缩，保留 system 和最近 15%，中间替换为摘要
- `prepare_context()` 自动判断是否需要 compact，正常情况直接返回原消息，超限情况返回压缩后消息

### Commit Message
`feat: implement LLM service with token estimation and auto-compact`

## Phase 4：飞书交互层

### 任务背景
实现飞书 WebSocket 长连接、消息事件处理、回复发送、`/status` 命令。这是用户实际感知的界面层。

### 预期达到的任务效果
Bot 可通过飞书接收文字消息，普通消息转发给 LLM 回复，`/status` 命令返回当前 Session 信息。

### 预计修改的文件范围
- `新增：feishu/__init__.py` — feishu 包初始化
- `新增：feishu/bot.py` — 飞书事件处理器，包含：
  - `MessageHandler` 类，注册 `im.message.receive_v1` 事件
  - 消息路由：`/` 开头 → 命令处理；普通文字 → LLM 对话
  - Session key 生成：单聊 `dm:{open_id}`，群聊 `group:{chat_id}`
  - 同 session 消息排队（使用 `services/session.py` 的锁）
- `新增：feishu/messages.py` — 消息发送工具，包含：
  - `send_text(conversation_id, content)` — 发送文字消息
  - `send_rich_text(conversation_id, title, elements)` — 发送富文本消息（用于 /status）
  - `send_error(conversation_id, error_msg)` — 发送错误提示
- `新增：services/commands.py` — 命令处理，包含：
  - `handle_status(session_key, store, context_window)` → 返回 /status 富文本内容
  - `/status` 展示：session_id, session_key, start_time, message_count, context_ratio (token/total / context_window)

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from services.commands import handle_status; print('commands module loads OK')"` — 验证命令模块加载
- 飞书端测试：发送普通文字消息，确认 Bot 回复 LLM 生成的内容
- 飞书端测试：发送 `/status`，确认返回当前 session 信息（session_id, start_time, context_ratio）
- 飞书端测试：发送 `/unknown`，确认返回错误提示
- 飞书端测试：发送文件/图片，确认返回"暂不支持"提示
- 飞书端测试：两个不同用户同时发送消息，确认 session 隔离

### 最终验收标准
- 飞书单聊发送文字消息，Bot 在 3 秒内返回 LLM 回复
- `/status` 返回格式化的 Session 信息卡片（session_id, start_time, context_ratio）
- 不同用户的对话历史完全隔离
- 错误消息（LLM 异常、飞书 API 异常）在飞书端以友好格式提示
- 文件/图片消息回复"暂不支持，请发送文字消息"

### Commit Message
`feat: implement feishu integration with event handling and /status command`

## Phase 5：Docker 构建与入口

### 任务背景
完成项目入口（main.py）、Docker 配置文件、README。使项目可通过 Docker 一键启动，数据持久化到宿主机。

### 预期达到的任务效果
`docker compose up -d` 可正常启动 Bot，飞书端可正常交互，数据持久化到 `/data/turing-apps/resume-bot/`。

### 预计修改的文件范围
- `修改：main.py` — 入口文件，启动飞书 WebSocket 长连接：
  - 加载 config
  - 初始化 SessionStore、LLM Service
  - 创建飞书 EventDispatcherHandler
  - 启动 lark.ws.Client
- `新增：Dockerfile` — 基于 `python:3.11-slim`，安装依赖，复制代码，ENTRYPOINT `python main.py`
- `新增：docker-compose.yml` — 定义服务，挂载 `/data/turing-apps/resume-bot/sessions` 到容器内 `/app/sessions`
- `新增：.dockerignore` — 排除 `.git`, `__pycache__`, `.codex`, `.env`
- `新增：README.md` — 项目说明（架构、配置、部署）

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "import main; print('main module imports OK')"` — 验证入口模块可导入
- `cd apps/resume-bot && docker build -t resume-bot:test .` — 验证 Docker 镜像构建成功
- `cd apps/resume-bot && docker compose up -d` — 验证容器启动，飞书端可交互
- 验证 `/data/turing-apps/resume-bot/sessions/` 目录有数据写入
- `docker compose down` 后再次 `docker compose up -d`，验证历史数据不丢失

### 最终验收标准
- `docker build` 成功构建镜像（无报错，镜像大小合理）
- `docker compose up -d` 容器正常启动，日志中显示 "connected to wss://..."
- 飞书端可正常发送文字消息并收到回复
- 飞书端 `/status` 命令正常返回 Session 信息
- 容器重启后历史对话不丢失
- `/data/turing-apps/resume-bot/sessions/` 目录有 JSON 文件生成

### Commit Message
`feat: add Docker support and application entry point`
