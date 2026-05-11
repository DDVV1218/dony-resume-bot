# resume-bot 架构流程图

```mermaid
flowchart TB
    subgraph Feishu["飞书端"]
        User[用户发送消息]
        WS[WebSocket 长连接<br/>lark-oapi]
    end

    subgraph Core["消息处理核心"]
        direction TB
        Bot[bot.py<br/>MessageHandler]
        Inbound[InboundMessage<br/>统一解析模型]
        Dedup["三层去重<br/>· Inflight<br/>· TTL Cache<br/>· Text+Time窗口"]
        Access["访问控制<br/>· DM白名单<br/>· 群白名单/禁言<br/>· @Bot检查"]
        Lock["Session锁<br/>串行处理"]
    end

    subgraph Handlers["消息处理器链"]
        direction TB
        H_Text[TextHandler<br/>文字消息]
        H_PDF[ResumePDFHandler<br/>PDF简历]
        H_Unsup[UnsupportedHandler<br/>暂不支持]
    end

    subgraph Commands["命令系统"]
        C_Status["/status → 显示Session信息"]
        C_New["/new → 创建新Session"]
    end

    subgraph Session["Session管理<br/>session.py"]
        direction TB
        S_CRUD["JSON文件持久化<br/>每个用户/群独立目录"]
        S_AutoCompact["自动Compact<br/>>85% Context Window时<br/>压缩历史为摘要"]
        S_Multi["多Session切换<br/>/new 创建, /status 查看"]
    end

    subgraph Agent["Agent系统"]
        direction TB
        AL[AgentLoop<br/>agent_loop.py]
        Tools["3个Tool"]
        AL -->|LLM自主决策| Tools
    end

    subgraph ToolsDetail["Agent Tools"]
        T_Search[search_resumes<br/>语义搜索]
        T_Query[query_resume_db<br/>按名精确查询]
        T_SendPDF[send_resume_pdf<br/>发送PDF到飞书]
    end

    subgraph SearchPipeline["搜索流水线"]
        direction TB
        S1["Step 1: ChromaDB向量搜索<br/>Qwen3-Embedding-8B<br/>4个段落分别搜<br/>→ 按人聚合 → 前300"]
        S2["Step 2: Reranker精排<br/>Qwen3-Reranker-8B<br/>全文+最佳段落双路打分<br/>→ 前50"]
        S3["Step 3: Review LLM<br/>两轮审查<br/>Round1: 初判pass/fail<br/>Round2: Reflect自我核查"]
        S4["Step 4: Comment LLM<br/>五维度并发评分<br/>(学历×0.4+经历×0.35+技能×0.25)<br/>→ 最终top 10"]
    end

    subgraph PDFPipeline["简历解析入库流水线"]
        direction TB
        P1["下载PDF到 uploads/"]
        P2["PDF分类 pdf_classifier.py<br/>PyMuPDF逐页分析"]
        P2a[">70%文本页<br/>→ 快速提取<br/>毫秒级"]
        P2b["<70%文本页<br/>→ MinerU VLM<br/>秒级(900s超时)"]
        P3["LLM分析 resume_handler.py<br/>结构化提取<br/>→ ResumeAnalysis Pydantic"]
        P4["入库 resume_indexer.py<br/>SQLite UPSERT<br/>(姓名+电话去重)"]
        P5["向量索引 vector_indexer.py<br/>ChromaDB写入<br/>· full(全文)<br/>· education(教育)<br/>· experience(经历)<br/>· skills(技能)"]
        P6["归档<br/>PDF → resume_archive/pdf/<br/>MD → resume_archive/md/"]
    end

    subgraph Database["数据层"]
        DB_SQLite["SQLite<br/>resumes 表<br/>姓名/电话/邮箱/学校/技能/公司<br/>+ FTS5 (已淘汰)"]
        DB_Chroma["ChromaDB<br/>PersistentClient<br/>hnsw:space=cosine<br/>embedding_dim=4096"]
        DB_Session["JSON文件<br/>sessions/用户目录/*.json<br/>active.txt"]
    end

    subgraph LLM_Infra["LLM基础设施"]
        LLM_Main["Qwen3.6-27B<br/>(vLLM localhost:3000)<br/>Chat / Analysis / Review / Comment"]
        LLM_Embed["Qwen3-Embedding-8B<br/>(vLLM localhost:8005)<br/>4096维向量"]
        LLM_Rerank["Qwen3-Reranker-8B<br/>(vLLM localhost:8006)<br/>/v1/score 批处理"]
    end

    %% 主流程
    User --> WS --> Bot
    Bot --> Inbound --> Dedup --> Access --> Lock
    
    Lock -->|文字| H_Text
    Lock -->|PDF文件| H_PDF
    Lock -->|其他| H_Unsup

    H_Text -->|检查命令| Commands
    H_Text -->|非命令| Agent

    Agent --> T_Search
    Agent --> T_Query
    Agent --> T_SendPDF

    T_Search --> SearchPipeline
    T_Query --> DB_SQLite
    T_SendPDF --> DB_Session

    T_Search -->|最终结果| Agent

    H_PDF --> P1 --> P2 --> P2a & P2b --> P3 --> P4 --> P5 --> P6

    %% 数据层连接
    P4 --> DB_SQLite
    P5 --> DB_Chroma
    S1 --> DB_Chroma
    T_Query --> DB_SQLite

    %% Session连接
    H_Text --> Session
    Commands --> Session

    %% LLM连接
    Agent --> LLM_Main
    S3 --> LLM_Main
    S4 --> LLM_Main
    P3 --> LLM_Main
    S1 --> LLM_Embed
    S2 --> LLM_Rerank

    %% 样式
    classDef feishu fill:#3370ff,color:#fff
    classDef core fill:#ff9f43,color:#fff
    classDef handler fill:#00d2d3,color:#fff
    classDef agent fill:#a29bfe,color:#fff
    classDef search fill:#fd79a8,color:#fff
    classDef pdf fill:#e17055,color:#fff
    classDef data fill:#00b894,color:#fff
    classDef llm fill:#6c5ce7,color:#fff
    classDef session fill:#fdcb6e,color:#333

    class User,WS feishu
    class Bot,Inbound,Dedup,Access,Lock core
    class H_Text,H_PDF,H_Unsup handler
    class AL,Tools,T_Search,T_Query,T_SendPDF agent
    class S1,S2,S3,S4 search
    class P1,P2,P2a,P2b,P3,P4,P5,P6 pdf
    class DB_SQLite,DB_Chroma,DB_Session data
    class LLM_Main,LLM_Embed,LLM_Rerank llm
    class S_CRUD,S_AutoCompact,S_Multi session
```

## 核心数据流

### 1️⃣ 对话流程（文字消息）
```
用户输入 → WebSocket → bot.py(去重/权限/锁) 
  → TextHandler → AgentLoop(LLM自主决策)
    ├─ 直接回答 → 回复用户
    ├─ search_resumes → 向量搜 → Reranker → Review → Comment → 回复
    ├─ query_resume_db → SQLite → 回复
    └─ send_resume_pdf → 归档取PDF → 飞书API发送
  → Session.messages.append() → JSON持久化
```

### 2️⃣ 简历入库流程（PDF上传）
```
PDF文件 → ResumePDFHandler
  → 下载 → PyMuPDF分类
    ├─ 快路(>70%文本): PyMuPDF直接提取(毫秒)
    └─ 慢路(<70%): MinerU VLM HTTP客户端(秒级)
  → LLM分析(StructuredOutput) → ResumeAnalysis
  → SQLite UPSERT(姓名+电话去重)
  → ChromaDB 4段向量(full/edu/exp/skills)
  → 归档PDF+MD
```

### 3️⃣ 搜索流水线
```
用户查询 → search_resumes tool
  ├─ 向量搜索: ChromaDB cosine → 每人4分 → 聚合 → 前300
  ├─ Reranker: 全文+最佳段落双路 → 前50
  ├─ Review LLM: 两轮(初判+Reflect) → pass/fail
  └─ Comment LLM: 并发5维打分 → 排序 → top 10
```
