# 飞书简历库 Bot

基于飞书 Bot 的智能简历管理系统，支持对话交互、Session 管理、LLM 驱动的智能问答。

## 架构

```
飞书用户 → 飞书 WebSocket → Python Bot → OpenAI LLM → 回复用户
                              ↓
                        ChromaDB (向量数据库)
                              ↓
                        /data/turing-apps/resume-bot/sessions/
```

## 技术栈

- **语言**: Python 3.11+
- **飞书 SDK**: lark-oapi (WebSocket 长连接)
- **LLM**: OpenAI Chat Completions API
- **向量数据库**: ChromaDB
- **Token 估算**: tiktoken
- **部署**: Docker + docker-compose

## 目录结构

```
resume-bot/
├── main.py                  # 入口：启动飞书 WebSocket 连接
├── config.py                # 配置管理（从 .env 读取）
├── prompts/                 # 提示词模板
│   ├── system_prompt.md     # 系统提示词
│   └── compact_prompt.md    # 压缩摘要提示词
├── feishu/
│   ├── bot.py               # 飞书 Bot 事件处理
│   └── messages.py          # 消息发送（文字、富文本）
├── services/
│   ├── llm.py               # OpenAI 调用 + Token 估算 + Compact
│   ├── session.py           # Session 管理（CRUD、持久化）
│   └── commands.py          # 本地命令处理（/status）
├── Dockerfile               # Docker 镜像构建
├── docker-compose.yml       # Docker 编排
├── .env.example             # 环境变量模板
└── pyproject.toml           # 项目元数据 + 依赖
```

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入飞书和 OpenAI 密钥
```

### 2. 本地开发

```bash
# 使用 uv 管理 Python 环境
uv sync
uv run python main.py
```

### 3. Docker 部署

```bash
# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

## 数据持久化

数据目录挂载到宿主机：

| 容器内路径 | 宿主机路径 | 用途 |
|-----------|-----------|------|
| `/app/sessions` | `/data/turing-apps/resume-bot/sessions` | 对话历史 |
| `/app/uploads` | `/data/turing-apps/resume-bot/uploads` | 简历文件 |
| `/app/chroma_db` | `/data/turing-apps/resume-bot/chroma_db` | 向量数据库 |

## 飞书端命令

| 命令 | 功能 |
|------|------|
| `/status` | 显示当前 Session 信息 |
| `/new` | 创建新的对话 Session |
| 回复数字 | 切换 Session（如回复 `1` 切换到 #001） |

## Session 管理

- **单聊**: 每个用户独立的 Session
- **群聊**: 每个群独立的 Session
- **全部历史**: 保存 100% 对话历史到 JSON 文件
- **自动 Compact**: Token 超过 context window 85% 时自动压缩

## 配置项

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `FEISHU_APP_ID` | - | 飞书应用 ID |
| `FEISHU_APP_SECRET` | - | 飞书应用密钥 |
| `OPENAI_API_KEY` | - | OpenAI API 密钥 |
| `OPENAI_MODEL` | `gpt-4o` | LLM 模型 |
| `OPENAI_CONTEXT_WINDOW` | `128000` | 上下文窗口大小 |
| `COMPACT_THRESHOLD` | `0.85` | Compact 触发阈值 |
| `COMPACT_RECENT_RATIO` | `0.15` | Compact 保留最近比例 |
| `SESSIONS_DIR` | `/app/sessions` | Session 存储目录 |

## 注意事项

- 飞书 WebSocket 长连接需要出站 HTTPS 访问公网
- 每个应用最多 50 个 WebSocket 连接
- 事件处理需在 3 秒内完成，否则触发超时重推
