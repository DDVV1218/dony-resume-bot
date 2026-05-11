# dony-resume-bot — Agent 开发指引

本文档为 AI 编码助手（如 pi-agent）提供本项目的工作指引。

## 项目概览

飞书智能简历管理系统，支持对话式交互、PDF 简历自动解析入库、语义搜索推荐、AI 多维度评分。

## 技术栈

- **语言**: Python 3.11+（本地环境使用 3.14，但 pyproject.toml 声明 >=3.11）
- **包管理**: `uv`（已配置 `.venv` 虚拟环境）
  - 激活: `cd /home/turingAI/turing-pi/apps/resume-bot && source .venv/bin/activate`
  - 添加依赖: `uv add <package>`
- **Git**: 本项目是一个独立的 git 仓库，每个功能提交请按逻辑分组
- **部署**: Docker + docker-compose（本地部署使用 host 网络模式）

## 项目结构

```
dony-resume-bot/
├── main.py              # 入口
├── config.py            # 配置（多 Agent 独立配置）
├── feishu/              # 飞书集成层
│   ├── bot.py           # 消息事件处理（路由/去重/权限）
│   ├── models.py        # 统一消息模型
│   ├── messages.py      # 消息发送
│   ├── streaming_card.py # 流式回复卡片
│   ├── file_utils.py    # 文件操作
│   └── dedup.py         # 三层消息去重
├── services/            # 业务逻辑层
│   ├── agent_config.py  # Agent 配置模型
│   ├── agent_loop.py    # Agent 工具调用循环
│   ├── tool_base.py     # 工具基类
│   ├── llm.py           # LLM 调用
│   ├── llm_utils.py     # 结构化输出（Pydantic + 重试）
│   ├── session.py       # Session 管理
│   ├── commands.py      # 命令系统
│   ├── registry.py      # 命令注册表
│   ├── db.py            # SQLite 管理
│   ├── resume_indexer.py # 简历入库
│   ├── vector_indexer.py # ChromaDB 向量索引
│   ├── pdf_classifier.py # PDF 分类
│   ├── pdf_processor.py  # PDF 解析
│   ├── reranker.py       # 语义精排
│   ├── comment_llm.py    # AI 评分
│   ├── review_llm.py     # 硬性条件审查
│   ├── handlers/         # 消息处理器链
│   └── tools/            # Agent 工具
├── prompts/             # LLM 提示词模板
├── scripts/             # 运维工具
├── docs/                # 文档
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## 核心架构

### 消息处理流程
```
飞书 WebSocket → bot.py (去重/权限) → 处理器链
  → TextHandler → AgentLoop (LLM 自主决策工具调用)
  → ResumePDFHandler → PDF 解析入库流水线
```

### Agent 工具
- `search_resumes` — 语义搜索（向量 → Reranker → Review LLM → Comment LLM）
- `query_resume_db` — SQL 精确查询（只读 SELECT）
- `send_resume_pdf` — 发送候选人 PDF 到飞书

### PDF 解析流水线
```
PyMuPDF 分类 → >70%文本页: 快速提取 / <70%: MinerU VLM
→ 逐页切分 → LLM 结构化提取 → SQLite 入库 → ChromaDB 向量索引 → 归档
```

## 开发约定

### 编码规范
- 遵循 PEP 8
- 类型注解：所有函数签名必须有类型注解
- 日志：使用 `logging.getLogger(__name__)`，不要使用 `print()`
- 错误处理：使用 try/except 包裹外部调用，避免未捕获异常
- Pydantic：所有结构化数据使用 Pydantic BaseModel

### 配置管理
- 所有可配置项通过 `config.py` 的 `Config` dataclass 管理
- 敏感信息（API Key 等）从环境变量或 `.env` 文件读取
- `.env` 文件已加入 `.gitignore`，不会上传

### 提交规范
- 每次提交按功能分组，保持 commit message 清晰
- 提交信息格式：`type: description`
  - `feat:` 新功能
  - `fix:` 修复
  - `refactor:` 重构
  - `docs:` 文档
  - `chore:` 杂项（配置、依赖等）

### 重要提醒
- 本项目使用 `uv` 管理 Python 环境，**不要使用 pip 直接安装**
- 如需安装系统级依赖（如编译工具），请使用 conda 的 `pi` 环境
- 所有 LLM 调用走私有化部署的 vLLM 服务（本地 localhost），不依赖外部 SaaS
- 修改 `prompts/` 下的提示词会影响 Agent 行为，需谨慎

## 当前状态

- 项目已功能完整上线运行
- master 分支为稳定版本
- 远端仓库: `https://github.com/DDVV1218/dony-resume-bot`
