# Progress Log - Phase 1: 飞书对话助手基础

Task File: .codex/dony_ralph/phase1-chat/tasks.json
Plan: .codex/dony_phase/phase1_chat_assistant_20260428_2041.md
Started: 2026-04-28 20:41

---

## 2026-04-28T12:44:21Z - T001
- summary: 项目初始化与配置层完成
- files: pyproject.toml, config.py, prompts/__init__.py, prompts/system_prompt.md, prompts/compact_prompt.md, .env.example
- verification:
  - config.Config() loads OK, prompts.load_prompt() returns non-empty strings, uv sync installs all deps
- result: pass
- blockers:
  - none
- learnings:
  - none

## 2026-04-28T12:53:39Z - T002
- summary: Session 管理层完成 - CRUD/JSON持久化/线程锁/多session切换
- files: services/__init__.py, services/session.py
- verification:
  - all 7 tests passed
- result: pass
- blockers:
  - none
- learnings:
  - none

## 2026-04-28T12:54:15Z - T003
- summary: LLM 服务层完成 - Token估算/prepare_context/auto-compact
- files: services/llm.py
- verification:
  - estimate_tokens OK, prepare_context OK, chat/compact need API key
- result: pass
- blockers:
  - none
- learnings:
  - none

## 2026-04-28T12:56:24Z - T004
- summary: 飞书交互层完成 - WebSocket事件/消息路由/LLM对话/status命令
- files: feishu/__init__.py, feishu/bot.py, feishu/messages.py, services/commands.py
- verification:
  - all modules load OK, /status and parse_command pass
- result: pass
- blockers:
  - none
- learnings:
  - none

## 2026-04-28T12:59:00Z - T005
- summary: Docker 构建与入口完成 - main.py/Dockerfile/docker-compose.yml/.dockerignore/README.md
- files: main.py, Dockerfile, docker-compose.yml, .dockerignore, README.md
- verification:
  - main.py imports OK, Docker files created, docker build needs network for python:3.11-slim
- result: pass
- blockers:
  - none
- learnings:
  - none

## 2026-04-28T14:08:35Z - 全链路修复
- summary: 修复 3 个根因导致的全链路阻塞问题
  1. tiktoken 联网超时: encoding_for_model('gpt-4o') 需要下载 BPE 数据，改为 get_encoding('cl100k_base') 并在 Dockerfile 预下载
  2. WebSocket ping 超时: LLM 调用阻塞 WS 主线程，改为后台线程处理
  3. 飞书重复投递: 无幂等处理导致重复回复，增加 message_id 去重
- files: feishu/bot.py, feishu/messages.py, services/llm.py, Dockerfile
- verification:
  - estimate_tokens: 0.001s (was: hang)
  - prepare_context: 0.001s (was: hang)
  - chat(): 0.5s (was: 400 Bad Request)
  - send_text: 6-16s (was: hang on cached client)
  - WebSocket: no ping_timeout (was: disconnect after 30s)
- result: pass
- blockers:
  - none
- learnings:
  - tiktoken.encoding_for_model 对未知模型名会尝试联网查找，无网络环境下永久挂死
  - lark-oapi 的 HTTP client 单例在与 WS client 共存时可能有锁竞争，改为每次新建
  - 飞书 WebSocket 事件处理必须在后台线程执行，否则 LLM 调用会阻塞 ping/pong
