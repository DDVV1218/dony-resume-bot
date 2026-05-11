# Resume Bot — 飞书智能简历管理系统

基于飞书 Bot 的智能简历管理平台，支持对话式交互、PDF 简历自动解析入库、语义搜索推荐、AI 多维度评分。

> ⚠️ **免责声明：本项目仅作思路参考**
>
> 本项目落地依赖的 LLM 模型（Qwen3.6-27B、Embedding-8B、Reranker-8B）、
> PDF 解析服务（MinerU）等均为**本地私有化部署**，不依赖任何外部 SaaS 服务。
>
> 实际使用时，您需要根据自身环境调整模型配置、API 地址、访问权限策略等，
> 也可能需要自定义系统提示词以适配您所在组织的架构和业务场景。
>
> **本仓库仅为架构思路和实现方案参考**，非开箱即用的商业产品。

---

## ✨ 核心功能

| 功能 | 描述 |
|------|------|
| 🤖 **对话式交互** | 在飞书中通过自然语言与 Bot 对话，支持文字/富文本/文件/图片消息 |
| 📄 **PDF 简历自动解析** | 上传 PDF 后自动提取结构化信息（姓名、学校、技能、经历等）并入库 |
| 🔍 **语义搜索** | 根据模糊需求搜索简历库（如"找复旦毕业的 CTA 量化实习生"） |
| 🎯 **多层排序流水线** | 向量搜索 → Reranker 精排 → Review LLM 审查 → Comment LLM 评分 |
| 📤 **简历发送** | 从归档中直接发送候选人 PDF 到飞书对话 |
| 📊 **SQL 查询** | 支持通过自然语言查询简历统计数据（"有多少候选人？"） |
| 🧵 **多 Session 管理** | 支持多 Session 切换、自动上下文压缩（Context Compact） |
| 🔐 **访问控制** | DM/群聊白名单、@mention 检查、多级权限策略 |

---

## 🏗️ 系统架构

```
┌─ 飞书端 ───────────────────────────────────────────────────┐
│  用户消息 → WebSocket (lark-oapi) → P2ImMessageReceiveV1    │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─ 消息处理核心 ──────────────────────────────────────────────┐
│  bot.py (MessageHandler)                                    │
│  ├─ InboundMessage 统一解析（纯文本/富文本/文件/图片）        │
│  ├─ 三层去重：Inflight + TTL Cache + Text-Time Window        │
│  ├─ 访问控制：DM白名单 / 群白名单 / @mention 检查             │
│  └─ Session 串行锁（同会话消息有序处理）                      │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─ 处理器链 ─────────────────────────────────────────────────┐
│  TextHandler      ← 文字/富文本消息 → AgentLoop 驱动 LLM    │
│  ResumePDFHandler ← PDF 文件    → PDF 解析入库流水线        │
│  UnsupportedHandler ← 其他类型 → "暂不支持"提示              │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─ Agent 系统 ────────────────────────────────────────────────┐
│  AgentLoop (tool-calling loop)                              │
│  ├─ search_resumes    → 语义搜索 + AI 评分                   │
│  ├─ query_resume_db   → SQL 精确查询                        │
│  ├─ send_resume_pdf   → 飞书文件发送                        │
│  └─ (LLM 自主决策调用哪个工具)                              │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─ 搜索流水线 ────────────────────────────────────────────────┐
│  Step 1: ChromaDB 向量搜索 (Qwen3-Embedding-8B) → 前 300 人 │
│  Step 2: Reranker 精排 (Qwen3-Reranker-8B)       → 前 50 人 │
│  Step 3: Review LLM (两轮: 初判 + Reflect 自我核查)         │
│  Step 4: Comment LLM (并发五维度评分) → Top 10              │
└─────────────────────────────────────────────────────────────┘

┌─ PDF 解析流水线 ────────────────────────────────────────────┐
│  下载 → PyMuPDF 分类                                         │
│  ├─ >70% 文本页 → 快速提取（毫秒级）                        │
│  └─ <70%       → MinerU VLM（秒级，支持图片型 PDF）          │
│  → 逐页切分（支持多人简历） → LLM 结构化提取                  │
│  → SQLite 入库 (UPSERT 去重) → ChromaDB 向量索引             │
│  → 归档 (PDF + Markdown)                                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 技术栈

### 核心框架
| 技术 | 用途 |
|------|------|
| **Python 3.11+** | 运行时 |
| **lark-oapi** | 飞书 SDK（WebSocket 长连接 + HTTP API） |
| **OpenAI SDK** | LLM 调用（兼容 vLLM / OpenAI API） |

### 数据存储
| 技术 | 用途 |
|------|------|
| **SQLite** | 简历主表（姓名/电话/邮箱/元数据等） |
| **ChromaDB** | 向量数据库（cosine 相似度搜索，4096 维） |
| **JSON 文件** | Session 对话历史持久化 |
| **文件系统** | PDF/Markdown 归档 |

### AI 基础设施
| 模型 | 用途 | 部署 |
|------|------|------|
| **Qwen3.6-27B** | 对话/分析/审查/评分 | vLLM (localhost:3000) |
| **Qwen3-Embedding-8B** | 简历向量化 | vLLM (localhost:8005) |
| **Qwen3-Reranker-8B** | 语义精排 | vLLM (localhost:8006) |
| **MinerU** | PDF → Markdown 解析 | HTTP Server (localhost:8003) |

### 项目管理
| 技术 | 用途 |
|------|------|
| **uv** | Python 环境与依赖管理 |
| **Docker** | 容器化部署 |
| **tiktoken** | Token 计数与上下文压缩 |

---

## 📁 项目结构

```
resume-bot/
├── main.py                       # 入口：启动飞书 WebSocket 连接
├── config.py                     # 配置管理（多 Agent 独立配置）
├── pyproject.toml                # 项目元数据与依赖声明
│
├── feishu/                       # 飞书集成层
│   ├── bot.py                    # 消息事件处理（路由/去重/权限）
│   ├── models.py                 # InboundMessage 统一消息模型
│   ├── messages.py               # 消息发送（文字/富文本/卡片）
│   ├── streaming_card.py         # 流式回复卡片（Card Kit API）
│   ├── file_utils.py             # 文件下载与飞书发送
│   └── dedup.py                  # 三层消息去重（TTL + Inflight）
│
├── services/                     # 业务逻辑层
│   ├── agent_config.py           # Agent 配置模型
│   ├── agent_loop.py             # Agent 工具调用循环
│   ├── tool_base.py              # BaseTool 抽象基类
│   ├── llm.py                    # LLM 调用、Token 估算、Auto-Compact
│   ├── llm_utils.py              # 结构化输出（Pydantic + 重试 + 降级）
│   ├── session.py                # Session 管理（JSON 持久化）
│   ├── commands.py               # 命令系统（/status, /new）
│   ├── registry.py               # 命令注册表
│   ├── time_utils.py             # 东八区时间工具
│   ├── db.py                     # SQLite 数据库管理
│   ├── resume_indexer.py         # 简历入库（UPSERT + 电话格式化）
│   ├── vector_indexer.py         # ChromaDB 向量索引
│   ├── pdf_classifier.py         # PDF 内容分类
│   ├── pdf_processor.py          # PDF 解析（PyMuPDF / MinerU）
│   ├── reranker.py               # Reranker 服务封装
│   ├── comment_llm.py            # Comment LLM 并发评分
│   ├── review_llm.py             # Review LLM 硬性条件审查
│   ├── handlers/                 # 消息处理器链
│   │   ├── base.py               # 处理器基类
│   │   ├── text_handler.py       # 文字消息处理器
│   │   ├── resume_handler.py     # PDF 简历处理器
│   │   └── unsupported_handler.py
│   └── tools/                    # Agent 工具
│       ├── search_resumes.py     # 语义搜索工具
│       ├── query_resume_db.py    # SQL 查询工具
│       └── send_resume_pdf.py    # PDF 发送工具
│
├── prompts/                      # LLM 提示词模板
│   ├── system_prompt.md          # 系统提示词（工具选择、展示格式）
│   ├── compact_prompt.md         # 上下文压缩提示词
│   ├── review_llm.md             # 硬性条件审查提示词
│   └── comment_llm.md            # 综合评价评分提示词
│
├── scripts/                      # 运维工具脚本
│   ├── batch_import.py           # 批量导入简历
│   ├── check_session.py          # Session 检查
│   ├── import_remaining.py       # 增量导入
│   └── migrate_phone.py          # 手机号格式迁移
│
├── docs/                         # 文档
│   └── architecture-flow.md      # 完整架构流程图（Mermaid）
│
├── Dockerfile                    # Docker 镜像构建
├── docker-compose.yml            # Docker 编排
├── .env.example                  # 环境变量模板
└── .gitignore
```

---

## 🚀 快速开始

### 前置条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- 飞书企业版账号 + 自建应用

### 1. 配置

```bash
cp .env.example .env
# 编辑 .env，填入飞书 App ID/Secret 和 LLM API 地址
```

### 2. 本地开发

```bash
uv sync
uv run python main.py
```

### 3. Docker 部署

```bash
docker compose up -d --build
docker compose logs -f        # 查看日志
docker compose down           # 停止
```

---

## 🔧 配置说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `FEISHU_APP_ID` | - | 飞书应用 ID |
| `FEISHU_APP_SECRET` | - | 飞书应用密钥 |
| `OPENAI_BASE_URL` | `http://localhost:3000/v1` | LLM API 地址 |
| `OPENAI_MODEL` | `Qwen3.6-27B` | LLM 模型名 |
| `OPENAI_CONTEXT_WINDOW` | `262144` | 上下文窗口大小 |
| `CHAT_MODEL` | `Qwen3.6-27B` | 对话 Agent 模型 |
| `ANALYSIS_MODEL` | `Qwen3.6-27B` | 简历分析 Agent 模型 |
| `REVIEW_MODEL` | `Qwen3.6-27B` | 审查 Agent 模型 |
| `COMMENT_MODEL` | `Qwen3.6-27B` | 评分 Agent 模型 |
| `EMBEDDING_SERVER_URL` | `http://localhost:8005/v1` | Embedding 服务地址 |
| `RERANKER_SERVER_URL` | `http://localhost:8006/v1` | Reranker 服务地址 |
| `MINERU_SERVER_URL` | `http://localhost:8003` | MinerU PDF 解析服务 |
| `SESSIONS_DIR` | `/app/sessions` | Session 存储目录 |
| `CHROMA_DB_DIR` | `/app/chroma_db` | 向量数据库目录 |
| `RESUME_ARCHIVE_DIR` | `/app/resume_archive` | 简历归档目录 |
| `BOT_IDENTITY` | `基金公司` | Bot 自我介绍中的组织名称，请根据实际修改 |
| `FEISHU_DM_POLICY` | `open` | DM 访问策略（open/allowlist） |
| `FEISHU_GROUP_POLICY` | `open` | 群聊策略（open/allowlist/disabled） |

---

## 💬 使用指南

### 对话示例

```
用户: 帮我找一下会 Python 和机器学习的候选人
Bot:  🔍 正在搜索简历库...
     👤 韩伊琳 | 复旦大学 金融硕士 | ⭐ 0.92
     > Python / SQL / 机器学习 / 因子研究
     > 某券商量化实习，负责因子回测与策略分析
     ---
     👤 乔伊宁 | 上海交通大学 计算机硕士 | ⭐ 0.88
     > Python / PyTorch / NLP / SQL
     > 灵均投资 CTA量化实习

用户: 查一下张三个人信息
Bot:  📊 正在查询数据库...
     姓名: 张三 | 电话: 138****1234
     学校: 北京大学 金融硕士
     技能: Python, SQL, C++, 机器学习
     ...

用户: 把李四的简历发给我
Bot:  📄 已发送「李四」的简历 PDF 到当前对话

用户: /status
Bot:  Session ID: #001
     总消息数: 24
     上下文: 3,456 / 262,144 tokens (1.3%)
```

---

## 🔄 核心工作流

### 对话流程
```
用户文字消息 → WebSocket → 三层去重 → 权限检查
  → TextHandler → AgentLoop (LLM自主决策)
    ├─ 直接回答
    ├─ search_resumes → ChromaDB → Reranker → Review → Comment → 回复
    ├─ query_resume_db → SQLite → 回复
    └─ send_resume_pdf → 归档取PDF → 飞书API发送
```

### 简历入库流程
```
PDF文件 → ResumePDFHandler
  → 下载 → PyMuPDF分类
    ├─ >70%文本页: PyMuPDF直接提取（毫秒级）
    └─ <70%: MinerU VLM（秒级，900s超时）
  → 逐页切分（支持多人简历） → LLM结构化分析
  → SQLite UPSERT (姓名+电话去重)
  → ChromaDB 4段向量索引 (全文/教育/经历/技能)
  → PDF + Markdown 归档
```

### 搜索排序流水线
```
用户查询 → search_resumes tool
  1. ChromaDB 向量搜索 → 每人4个chunk分 → 按人聚合 → 前300
  2. Reranker 精排 (全文+教育+经历+技能 四路) → 前50
  3. Review LLM 两轮审查 (初判 + Reflect 自我核查)
  4. Comment LLM 并发五维度评分 (学历×0.4 + 经历×0.35 + 技能×0.25)
  → 排序 → Top 10
```

---

## 🧪 核心设计亮点

- **分层决策分级处理**：PyMuPDF 毫秒级快路 vs MinerU VLM 秒级慢路，根据 PDF 质量自动路由
- **多人简历支持**：逐页切分 + LLM 并发判断页面归属，从多页 PDF 中分离出每位候选人的独立简历
- **三层去重保护**：Inflight Guard（并发保护）+ TTL Cache（时间窗口）+ Text-Time 窗口，防止消息重复处理
- **Review-Reflect 机制**：两轮 LLM 审查（初判 + 自我核查），大幅降低误判率
- **自动上下文压缩**：Token 超过 85% context window 时自动摘要压缩，支持无限长对话
- **多 Agent 独立配置**：聊天、分析、审查、评分四个 Agent 使用独立的模型/参数配置，互不干扰

---

## 📄 License

MIT
