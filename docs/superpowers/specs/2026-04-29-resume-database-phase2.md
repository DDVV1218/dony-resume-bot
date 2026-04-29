# Phase 2: 简历库系统设计文档

> 日期：2026-04-29
> 项目：Feishu Resume Bot
> 状态：设计确认，待实现

## 概述

在现有 Phase 1 的 PDF → MinerU → LLM 分析链路上，新增简历索引与检索能力，支持人力专员通过自然语言对话进行简历检索。

## 架构总览

```
[入库] PDF → MinerU → Markdown → LLM 提取结构化信息
                                        ↓
                              ┌─────────┴─────────┐
                              ↓                   ↓
                          ChromaDB             SQLite
                     (向量语义检索)         (FTS5 关键词 + 元数据管理)
                              ↓                   ↓
                              └─────────┬─────────┘
                                        ↓
                                  RRF 合并排序
                                        ↓
                                  LLM 综合回答
                                        ↓
                                 飞书卡片回复
```

## 1. 入库流程（Data Ingestion）

### 1.1 ChromaDB

- **数据库**: ChromaDB PersistentClient（持久化到 `/app/chroma_db`）
- **Collection**: `resumes`
- **每条记录**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | `resume_id`（UUID） |
| `text` | str | 简历全文 Markdown |
| `metadata.name` | str | 姓名 |
| `metadata.sex` | str | 性别 |
| `metadata.phone` | str | 手机号 |
| `metadata.email` | str | 邮箱 |
| `metadata.undergraduate` | str | 本科学校 |
| `metadata.master` | str | 硕士学校 |
| `metadata.doctor` | str | 博士学校 |
| `metadata.skills` | str | 技能，逗号分隔 |
| `metadata.intership_comps` | str | 实习公司，逗号分隔 |
| `metadata.work_comps` | str | 工作公司，逗号分隔 |
| `metadata.resume_id` | str | 关联 ID |

> 注：ChromaDB metadata 不支持嵌套结构，list 字段转为逗号分隔字符串。

### 1.2 SQLite

表结构：

```sql
-- 元数据主表
CREATE TABLE resumes (
    name TEXT NOT NULL,
    sex TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    metadata TEXT,              -- 完整 JSON 元数据
    chroma_id TEXT,             -- ChromaDB 中的 ID
    pdf_path TEXT,              -- PDF 文件绝对路径
    markdown_path TEXT,         -- Markdown 文件绝对路径
    created_at TEXT,
    PRIMARY KEY (name, sex, phone)
);

-- FTS5 全文索引表
CREATE VIRTUAL TABLE resumes_fts USING fts5(
    full_text,                  -- 简历全文 Markdown
    name,                       -- 姓名
    school,                     -- 学校（拼接本硕博）
    skills,                     -- 技能
    company,                    -- 公司（拼接实习+工作）
    tokenize='unicode61'        -- 中文字符按单字切分
);
```

去重策略：UPSERT

```sql
INSERT INTO resumes (...) VALUES (...)
ON CONFLICT(name, sex, phone) DO UPDATE SET
    email = excluded.email,
    metadata = excluded.metadata,
    chroma_id = excluded.chroma_id,
    pdf_path = excluded.pdf_path,
    markdown_path = excluded.markdown_path,
    created_at = excluded.created_at;
```

ChromaDB 同步：更新时删除旧 `chroma_id` 对应的向量，写入新向量。

### 1.3 入库时序

```
收到 PDF → 下载 → MinerU 解析 → Markdown
    ↓
LLM 提取结构化信息（姓名、学校、技能...）
    ↓
Chromadb: 写入向量 + metadata
SQLite: UPSERT 元数据 + FTS5 索引
    ↓
保存 Markdown 副本到 /app/mineru_process/
```

错误处理：
- ChromaDB 失败 → SQLite 正常写入，日志告警
- SQLite 失败 → ChromaDB 正常写入，日志告警
- 两路都失败 → 记录错误，不阻塞用户

## 2. 检索流程（Query Flow）

### 2.1 路由策略

不做显式路由判断。TextHandler 统一处理：
1. 先跑检索
2. 检索结果注入 LLM 上下文
3. LLM 自行决定是否基于检索结果回答

```
用户： "找复旦的CTA实习生"
    → 检索命中 3 份简历 → 注入 LLM → LLM 回答候选人信息

用户： "今天天气怎么样"
    → 检索命中 0 份 → 注入空结果 → LLM 正常聊天
```

### 2.2 双通道并行检索

```
用户问题 Q
    ↓
┌───────────────────────────────┐───────────────────────────────┐
│  ChromaDB 向量检索            │  SQLite FTS5 关键词检索        │
│  Q → embedding 模型 → 向量    │  Q 全文 → FTS5 MATCH          │
│  → query top-30               │  → BM25 top-30                │
│  + metadata where（可选过滤）  │                               │
└───────────────────────────────┘───────────────────────────────┘
                ↓                           ↓
           各自的 top-30 list（含 rank 位置）
                ↓                           ↓
                └────────── RRF 合并 ────────┘
                              ↓
                        top-10 简历 ID 列表
                              ↓
                  从 SQLite 取出完整简历内容
                              ↓
                   LLM 综合判断 → 飞书卡片回复
```

### 2.3 RRF 合并公式

```
score(id) = 1/(60 + rank_vector(id)) + 1/(60 + rank_fts(id))

rank_vector: 该 ID 在 ChromaDB 结果中的排名（1-based）
rank_fts:    该 ID 在 FTS5 结果中的排名
```

仅在双通道都返回结果时做 RRF。任一通道失败则降级为单通道。

### 2.4 LLM 解析异步辅助

LLM 对用户问题的结构化解析（提取条件）是异步、非阻塞的：
- 检索先用原问题直接跑
- LLM 解析完成后，其结果仅用于优化 metadata 过滤或注入 LLM 最终回答的参考

### 2.5 降级策略

| 故障场景 | 行为 |
|----------|------|
| ChromaDB 挂了 | 纯 FTS5 检索 |
| SQLite/FTS5 挂了 | 纯 ChromaDB 检索 |
| 两者都挂 | 正常 LLM 聊天，无检索结果 |

## 3. 去重策略

- **主键去重**: SQLite `PRIMARY KEY (name, sex, phone)`
- **UPSERT 更新**: 重复时用新数据覆写旧数据
- **ChromaDB 同步**: 旧向量删除 + 新向量写入
- 同一人更换手机号视为不同记录

## 4. 文件存储

- **PDF**: `/app/uploads/{sender_id}/{filename}`（现有路径）
- **Markdown**: `/app/mineru_process/{filename}.md`（现有路径）
- **ChromaDB 持久化**: `/app/chroma_db/`
- **SQLite 文件**: `/app/sessions/resumes.db`

所有路径在 SQLite 中记录绝对路径，后续可通过 Feishu API 上传文件发送给用户。

## 5. 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 向量数据库 | ChromaDB | 内嵌库，零运维，千份简历够用 |
| 关键字索引 | SQLite FTS5 + unicode61 | 内嵌，无需额外服务 |
| 元数据管理 | SQLite 普通表 | 与 FTS5 同库，减少连接数 |
| Embedding 模型 | 独立本地部署（如 bge-m3） | OpenAI 兼容 API |
| PDF 解析 | MinerU CLI（已有） | 已验证可用 |
| 结构化提取 | Qwen3.6-27B（已有） | 复用现有 LLM |
| 合并排序 | RRF | 简单有效，不依赖调参 |

## 6. 新文件清单

```
services/
├── resume_indexer.py      # 入库：ChromaDB + SQLite 写入
├── resume_searcher.py     # 检索：双通道 + RRF 合并
└── db.py                  # SQLite 连接管理 + 建表

feishu/
└── file_sender.py         # 按路径取文件并通过 Feishu API 发送
```

### 修改文件

```
services/handlers/resume_handler.py  # LLM 分析完成后调用 resume_indexer 入库
services/handlers/text_handler.py    # 检索逻辑挂载点
config.py                            # 新增 Embedding 模型配置
```

## 7. 未纳入 Phase 2（后续考虑）

- LLM Re-rank 层（Phase 3）
- jieba 中文分词（当前 unicode61 够用，需要时再升级）
- Web 搜索界面（是聊天场景）
- 简历标签系统（自动打标签）
