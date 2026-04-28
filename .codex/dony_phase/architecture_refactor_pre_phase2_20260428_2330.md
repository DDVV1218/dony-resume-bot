# Phase 1.5：基于 OpenClaw 学习成果的架构重构

## 背景调研结论
- 输入类型：`detailed_plan`
- 当前现状：Phase 1（飞书对话助手基础）已稳定运行，包含配置层、Session 管理、LLM 服务、飞书 WebSocket 集成、Docker 打包。核心文件 `feishu/bot.py`（370 行）承担了过多职责：事件解析、去重、路由、命令处理、LLM 编排全部耦合在同一文件中。
- 已确认问题 / 计划背景：
  - 学习了 OpenClaw 的 channel 架构后，发现我们当前实现存在以下差距：
    1. **无访问控制**：任何人都可以 DM bot 或把 bot 拉入群聊即用，无安全防线
    2. **去重机制薄弱**：简单 `set` + 手动清理（1000→800），无 TTL 自动过期、无 inflight 检测
    3. **消息处理架构"大泥球"**：事件解析、路由、业务逻辑全部耦合在 `_process_message` 一个方法中
    4. **路由硬编码**：命令路由在 if/else 链中，添加新命令需修改 `bot.py`
    5. **消息类型不可扩展**：当前仅支持 text 类型，Phase 2 需支持 PDF/PNG 时需要大幅改动
  - 以上问题若不解决，Phase 2（简历解析 + 向量检索）将在现有架构上叠加更多耦合，导致维护困难。
- 根因判断 / 约束核对：
  - 我们的项目是垂直应用（简历 Bot），不是通用 Agent 框架，不需要 OpenClaw 的全部灵活性（如多 Agent、多账号、流式回复等）
  - 约束：Docker 容器无运行时网络（tiktoken 预缓存）；仅支持 Qwen3.6-27B；禁 think 模式；使用 `uv` 管理环境
  - 需保留：Phase 1 的所有功能（session 切换、auto-compact、后台线程处理、`/status`、历史消息过滤）
- 建议修改方向：按依赖关系分 5 个 Phase 递进实现。每 Phase 可独立部署验证。先安全（访问控制），再可靠性（去重），再架构重构（规范消息 + 可配置路由 + 扩展框架）。
- Phase 划分依据：
  - 访问控制（Phase 1）和去重（Phase 2）无依赖，可作为独立小改进快速上线
  - 规范化消息处理（Phase 3）是后续路由重构和类型扩展的前置基础
  - 声明式路由（Phase 4）依赖于 Phase 3 的规范化 InboundMessage
  - 消息类型扩展框架（Phase 5）依赖于 Phase 3 的消息类型路由入口

## Phase 1：DM/Group 访问控制层

### 任务背景
当前 Bot 对所有人都开放——任何能获取到 Bot 的飞书用户都可以单聊，任何群都可以拉入 Bot 使用。在生产环境中，这是一个安全隐患。参考 OpenClaw 的 `dmPolicy` / `groupPolicy` 体系，引入可配置的访问控制。

### 预期达到的任务效果
- DM 支持三种策略：`open`（全部允许）、`allowlist`（仅允许列表中的 open_id）、`pairing`（发送配对码，等待 CLI approve）——Phase 1 先实现 `open` 和 `allowlist`，`pairing` 留空
- 群聊支持三种策略：`open`、`allowlist`（仅允许列表中的 chat_id）、`disabled`（不响应任何群消息）
- @mention 要求可配置（当前硬编码为必须 @mention）
- 权限不足时在飞书返回友好提示

### 预计修改的文件范围
- `修改：config.py` — 新增 `feishu_dm_policy`、`feishu_dm_allowlist`、`feishu_group_policy`、`feishu_group_allowlist`、`feishu_require_mention` 字段
- `修改：.env.example` — 添加对应环境变量注释
- `修改：feishu/bot.py` — 新增 `_check_dm_access()`、`_check_group_access()` 方法；修改 `_process_in_background()` 入口处增加访问检查逻辑

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from config import Config; c = Config(); print(c.feishu_dm_policy)"` — 验证新字段默认值正确
- 通过 `.env` 设置 `FEISHU_DM_POLICY=allowlist` 和 `FEISHU_DM_ALLOWLIST=ou_test1,ou_test2`，验证非列表中的用户收到拒绝提示
- 通过 `.env` 设置 `FEISHU_GROUP_POLICY=disabled`，验证所有群消息被忽略
- 通过 `.env` 设置 `FEISHU_REQUIRE_MENTION=false`，验证群聊无需 @mention 即可触发

### 最终验收标准
- DM 策略生效：被拒绝的用户收到 "⛔ 抱歉，您没有权限使用此 Bot" 提示
- 群聊策略生效：不在 allowlist 中的群聊发消息无响应
- `disabled` 模式下所有群消息静默忽略（不回复任何内容）
- @mention 开关生效：关闭后群聊无需 @ 即可交互
- 所有策略在 Docker 容器重启后通过环境变量生效

### Commit Message
`feat: add DM and group access control with configurable policies`

## Phase 2：In-flight Dedup + TTL 自动过期去重

### 任务背景
当前去重层存在两个问题：1）用简单 `set` + 满 1000 删到 800 的手动清理，在长时间运行后可能导致内存泄漏或误杀；2）没有 inflight 检测——同一个 `message_id` 可能在两个并发后台线程中被同时取到（虽然 `handle()` 层有锁，但极端情况下仍有竞态窗口）。参考 OpenClaw 的 `claim → commit/release` 三层模式 + TTL cache。

### 预期达到的任务效果
- 去重使用 TTL 自动过期（默认 20 分钟），不再需要手动清理
- 引入 inflight guard：`claim(msg_id)` 成功后其他线程无法再 claim 同一条消息；处理完成后 `commit()` 或 `release()`
- 保留现有的 `text+时间窗口` 去重作为第二层保护
- 保证错误路径也会 release（finally）

### 预计修改的文件范围
- `新增：feishu/dedup.py` — 包含 `TTLSet`（OrderedDict+自动过期）、`DedupeGuard`（TTLSet + inflight Set，提供 `claim/commit/release` 方法）
- `修改：feishu/bot.py` — 替换 `_processed_message_ids` set 和 `_text_dedup_keys` set 为 `DedupeGuard` 实例；修改 `handle()` 中的去重逻辑

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from feishu.dedup import DedupeGuard; g = DedupeGuard(ttl_ms=100); assert g.claim('a'); assert not g.claim('a'); g.commit('a'); assert not g.claim('a'); print('claim/commit OK'); g = DedupeGuard(ttl_ms=50); g.claim('b'; import time; time.sleep(0.06); assert g.claim('b'); print('TTL expiry OK')" ` — 验证核心逻辑
- 验证 inflight release 在异常路径中仍被调用：模拟 `_process_message` 抛出异常，确认 inflight set 中该 key 被释放
- 长时间运行测试：发送 5000 条消息后确认 set 大小稳定在 maxSize 附近

### 最终验收标准
- 同一 `message_id` 不会被重复处理（即使并发到达）
- `message_id` 在 TTL 过期后自动从 cache 中移除，允许同一消息（罕见场景）在 20 分钟后被重新处理
- 主动混淆 `_processed_message_ids` 的 bug 不复存在
- 异常路径不会导致 inflight key 泄漏

### Commit Message
`refactor: replace simple dedup set with TTL cache and inflight guard`

## Phase 3：规范化消息处理架构（InboundMessage + 分层处理）

### 任务背景
当前 `feishu/bot.py` 中事件解析、消息提取、session key 生成、会话 ID 提取等功能散落在多个 `_xxx` 方法中，且 `_process_message()` 方法体过大（约 100 行，包含文本提取、命令路由、LLM 调用全部逻辑）。参考 OpenClaw 的 `resolveInboundConversationResolution()` + `canonical` 结构，建立一个规范化的入站消息模型，将"飞书事件解析"与"业务处理"分离。

### 预期达到的任务效果
- 定义 `InboundMessage` dataclass，标准化所有入站消息的字段（session_key、conversation_id、chat_type、sender_id、text、message_type、message_id、create_time、thread_id）
- 抽取 `_resolve_inbound(data) → InboundMessage` 方法，统一完成事件解析
- 将 `_process_message` 拆为三层：
  1. `_resolve_inbound()` — 事件解析层
  2. `_route_message()` — 消息路由层（命令 vs 普通消息 vs 文件）
  3. `_handle_chat()` — LLM 对话业务层（原 LLM 调用逻辑）
- 保证拆分后功能完全不变，所有 Phase 1 的功能回归测试通过

### 预计修改的文件范围
- `新增：feishu/models.py` — `InboundMessage` dataclass 定义
- `修改：feishu/bot.py` — 抽取 `_resolve_inbound`、`_route_message` 方法；重写 `_process_message` 为三层调用；删除不再需要的 `_extract_text_content`、`_get_session_key`、`_get_conversation_id` 等方法（功能内聚到 `_resolve_inbound`）
- `修改：feishu/__init__.py` — 导出 `InboundMessage`

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from feishu.models import InboundMessage; m = InboundMessage(session_key='dm:ou_test', conversation_id='oc_test', chat_type='p2p', message_id='om_test', create_time=1234.0); assert m.session_key == 'dm:ou_test'; print('model OK')"` — 验证 dataclass 定义正确
- 手动构造模拟的 `P2ImMessageReceiveV1` 对象（使用 lark_oapi 的 builder），验证 `_resolve_inbound` 正确提取字段
- 发送 `/status`、`/new`、数字切换、普通消息，验证拆分层后的路由结果与重构前一致

### 最终验收标准
- `_resolve_inbound()` 正确返回所有标准化字段
- `_route_message()` 根据 message_type 将消息分发到正确的 handler
- 三层拆分后，Phase 1 的全部功能（LLM 对话、命令、session 切换）不受影响
- no regression in WS ping/pong 稳定性、去重、旧消息过滤

### Commit Message
`refactor: extract normalized InboundMessage model and layered message processing`

## Phase 4：声明式 Binding 配置 + 命令路由重构

### 任务背景
当前命令路由（`/status`、`/new`、数字切换）硬编码在 `_process_message` 的 if/else 链中，添加新命令需要修改 `bot.py`。参考 OpenClaw 的 `bindings` 声明式配置体系，将路由规则通过配置定义，运行时动态查找 handler。

### 预期达到的任务效果
- 命令路由从代码配置化：`config.py` 中定义 `commands = { "status": {...}, "new": {...} }`
- handler 注册：每个命令对应一个可导入的函数引用，通过 `handle_command()` 分发
- 预留 "消息类型路由" 声明式接口：当 message_type = "file" 时路由到 file_handler，为 Phase 2 的 PDF 上传做准备
- 添加新命令 = 改一行配置 + 写一个函数，不修改 `bot.py`

### 预计修改的文件范围
- `修改：config.py` — 新增 `commands` dict 配置、`message_routes` dict 配置
- `新增：services/registry.py` — 命令注册表 `CommandRegistry` 类，提供 `register()`、`resolve()` 方法；消息路由注册表 `MessageRouteRegistry`
- `修改：services/commands.py` — 使用 decorator 注册 handler（`@registry.command("status")`），导出 registry 实例
- `修改：feishu/bot.py` — 将命令处理的 if/else 链替换为 `registry.resolve(command).handler(session_key, conversation_id, args)` 调用

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from services.registry import CommandRegistry; r = CommandRegistry(); r.register('test', lambda: 'ok'); assert r.resolve('test')() == 'ok'; assert r.resolve('unknown') is None; print('registry OK')"` — 验证注册表
- 验证：`/status` 通过 registry 分发后行为与原 if/else 一致
- 验证：通过 `config.py` 添加新命令后，运行时自动生效

### 最终验收标准
- 所有现有命令（`/status`、`/new`、数字切换）通过 registry 分发，功能无变化
- 添加一个测试命令 `/ping` 且仅改 config + 写 handler 函数，无需修改 `bot.py`
- `message_routes` 配置可正常解析，当 Phase 2 添加 `{"type": "file", "handler": "handle_resume_upload"}` 时映射正确
- 未知命令返回友好提示（保持不变）

### Commit Message
`refactor: replace hardcoded command routing with declarative binding registry`

## Phase 5：消息类型扩展框架

### 任务背景
当前 Bot 仅支持 text 消息，收到文件/图片直接回复"暂不支持"。Phase 2 需要支持 PDF/PNG 上传。参考 OpenClaw 的 channel plugin 体系 + 消息类型归一化，建立一个可扩展的消息类型处理框架，使 Phase 2 可以直接"插入"简历处理逻辑而不改动核心路由。

### 预期达到的任务效果
- 定义 `BaseMessageHandler` 抽象基类（`can_handle()`, `handle()`）
- 实现 `TextHandler`（现有文字对话逻辑迁移）、`UnsupportedHandler`（兜底提示）
- 框架支持按 `message_type` 和 `mime_type` 前缀双重匹配（如 PDF 匹配 `application/pdf`）
- 预留 `ResumePDFHandler` 和 `ResumeImageHandler` 桩（stub），Phase 2 实现

### 预计修改的文件范围
- `新增：services/handlers/__init__.py` — handlers 包
- `新增：services/handlers/base.py` — `BaseMessageHandler` 抽象基类，定义 `message_types()`、`can_handle()`、`handle()` 接口
- `新增：services/handlers/text_handler.py` — 将现有 `_process_message` 中的 LLM 对话代码迁移到 `TextHandler.handle()`
- `新增：services/handlers/unsupported_handler.py` — 非 text 类型消息的兜底处理
- `新增：services/handlers/resume_handler.py` — 预留桩文件：`ResumePDFHandler` 和 `ResumeImageHandler`，`can_handle()` 返回 False（暂不启用），Phase 2 实现
- `修改：feishu/bot.py` — 引入 handler 链：`_route_message()` 遍历 handler 列表找第一个 `can_handle()` 为 True 的，调用其 `handle()`

### 必须涵盖的测试方向
- `cd apps/resume-bot && uv run python -c "from services.handlers.base import BaseMessageHandler; print('abstract base loads')"` — 验证模块可导入
- 模拟 text 消息，确认 `TextHandler.can_handle()` 返回 True，`handle()` 返回 LLM 回复
- 模拟 file/image 消息，确认 `UnsupportedHandler` 被选中，返回"暂不支持"提示
- 验证 handler 链的顺序（text → resume → unsupported）按优先级工作

### 最终验收标准
- 文字消息走 `TextHandler`，行为与当前完全一致
- 文件/图片消息走 `UnsupportedHandler`，提示文字与当前完全一致
- `ResumePDFHandler` 和 `ResumeImageHandler` 已注册但 `can_handle()` 返回 False，不干扰现有流程
- 添加新的 handler 只需新建一个类 + 注册到列表，不修改 `bot.py`、`config.py`
- Phase 2 实现 resume handler 时只需设置 `can_handle()` 返回 True

### Commit Message
`feat: introduce extensible message handler framework for resume parsing preparation`

## 依赖关系图

```
Phase 1 (DM/Group 访问控制)     Phase 2 (In-flight Dedup)
         │                              │
         └──────┬───────────────────────┘
                ▼
         Phase 3 (规范化 InboundMessage)
                │
                ▼
         Phase 4 (声明式 Binding 路由)
                │
                ▼
         Phase 5 (消息类型扩展框架)
                │
                ▼
              Phase 2 简历解析 + 向量检索（下一步）
```

## 风险与注意事项

1. **Phase 3 重构风险最大**：`_process_message` 拆层可能导致回归。建议 Phase 3 单独部署验证 24 小时后再推进 Phase 4。
2. **Docker 兼容性**：每个 Phase 完成后需重新构建 Docker 镜像并验证容器启动正常。
3. **配置兼容性**：Phase 1 和 Phase 4 新增的环境变量需在 `.env.example` 和 Docker 文档中同步更新。
4. **避免过度设计**：Phase 4 和 Phase 5 的灵活性以"够用且可扩展"为标准，不盲目模仿 OpenClaw 的全部复杂度。
