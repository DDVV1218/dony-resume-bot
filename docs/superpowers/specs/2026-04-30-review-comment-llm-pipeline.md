# Review & Comment LLM Pipeline

为简历搜索添加两层 LLM 智能处理：**Review LLM** 硬性条件筛选 + **Comment LLM** 综合评价打分。

## 背景

当前简历搜索流程为：向量搜索 → FTS5 → 合并 → Reranker → Agent LLM 格式化输出。存在两个问题：

1. FTS5 精确短语匹配对混合中英文（如"商品期货CTA"）匹配不佳，且回复内容经常出错 
2. 最终输出缺乏对候选人匹配度的智能评价，用户需要自行判断

## 新搜索链路

```
用户输入（自然语言需求 + 推荐人数 K）
        │
        ▼
┌──────────────────────────────────┐
│ ① 语义搜索（Vector Search Only）  │
│   ChromaDB 全量向量查询           │
│   → 按人合并 → top 50 人         │
└──────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────┐
│ ② Reranker 精排                 │
│   Qwen3-Reranker-8B              │
│   → top 25 人                   │
└──────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────┐
│ ③ Review LLM（硬性条件审查）      │
│   1批25人，2轮（初判+Reflect）    │
│   输入：用户需求 + 每人 meta 信息 │
│   输出：通过/淘汰 + 原因          │
└──────────────────────────────────┘
        │
        ▼（只保留通过者）
┌──────────────────────────────────┐
│ ④ 按 Reranker 分排序取前 K 名   │
│   K=用户指定，未指定默认 5，      │
│   上限 10                        │
└──────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────┐
│ ⑤ Comment LLM（综合评价+打分）   │
│   每人单独调用，可并发            │
│   输入：用户需求 + 候选人 meta    │
│   输出：{score: 1-10, comment}   │
└──────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────┐
│ ⑥ Agent LLM 最终回复            │
│   候选人信息卡片 + AI 评论       │
└──────────────────────────────────┘
```

## 对比改动

| 项目 | 改前 | 改后 |
|:----|:----|:----|
| FTS5 搜索 | 有（关键词 BM25） | 移除 |
| 向量搜索 | TOP_K_VECTOR_ALL=500 chunk | 不变 |
| 合并 | 向量+FTS 加权合并 | 仅向量按人聚合 |
| Reranker 进入人数 | top 50 | top 50 |
| Reranker 后取 | top 10（直接输出） | top 25（进 Review） |
| Review LLM | 无 | 新增：1批25人+Reflect |
| Comment LLM | 无 | 新增：每人单独打分 |
| 最终输出 | 候选人卡片 | 候选人卡片 + AI 评论 |

## 组件详细设计

### 1. Review LLM

**文件**: `services/review_llm.py`
**Prompt**: `prompts/review_llm.md`

**功能**: 一次性接收 top 25 候选人的 meta 信息和用户需求，判断每人是否满足硬性条件。

**输入格式**:
```json
{
  "user_query": "推荐复旦毕业懂CTA的量化研究员",
  "candidates": [
    {
      "id": 1,
      "name": "乔伊宁",
      "school": "上海交通大学",
      "degree": "本科",
      "skills": "Python, PyTorch, NLP, SQL",
      "experience_summary": "灵均投资 CTA量化实习; 敦和资产 量化研究实习",
      "company": "灵均投资, 敦和资产",
      "highlights": "GPA 4.0/4.3; 宾夕法尼亚大学交换"
    },
    ...
  ]
}
```

**流程**:
1. **Round 1（初判）**: LLM 对 25 人逐一判断是否符合硬性条件，输出每人 pass/fail 及原因
2. **Round 2（Reflect）**: 将 Round 1 结果 + 原始数据再次输入 LLM，让其自我核查，修正错误，输出最终结论

**输出格式**:
```json
{
  "results": [
    {"id": 1, "name": "乔伊宁", "verdict": "pass", "reason": "上海交通大学符合五校要求; 有CTA量化实习经验; 掌握Python等技能"},
    {"id": 2, "name": "张三", "verdict": "fail", "reason": "学校非目标院校"},
    ...
  ]
}
```

**反思（Reflect）机制**: 将初判结果和原始数据再次发给 LLM，prompt 指示其检查是否有误判（如忽略了符合条件的候选人、错误淘汰了某人）。确保第二轮比第一轮更严谨。

**模型配置**:
- 模型: Qwen3.6-27B-FP8
- Temperature: 0.0
- thinking mode: 关闭 (extra_body={"chat_template_kwargs": {"enable_thinking": False}})
- response_format: {"type": "json_object"}

### 2. Comment LLM

**文件**: `services/comment_llm.py`
**Prompt**: `prompts/comment_llm.md`

**功能**: 对每个通过 Review 的候选人（最终输出的前 K 名），给出 1-10 分和综合评价文字。

**输入格式**:
```json
{
  "user_query": "推荐复旦毕业懂CTA的量化研究员",
  "candidate": {
    "id": 1,
    "name": "乔伊宁",
    "school": "上海交通大学",
    "degree": "本科",
    "skills": "Python, PyTorch, NLP, SQL",
    "experience_summary": "灵均投资 CTA量化实习; 敦和资产 量化研究实习",
    "company": "灵均投资, 敦和资产",
    "highlights": "GPA 4.0/4.3; 宾夕法尼亚大学交换"
  }
}
```

**输出格式**:
```json
{
  "score": 8,
  "comment": "该候选人毕业于上海交通大学（985），专业为数学与应用数学-经济学双学位，具备扎实的数理基础。实习经历涵盖灵均投资（CTA量化）和敦和资产（量化研究），与商品期货CTA研究高度匹配。技能方面掌握Python、PyTorch、NLP、SQL等，计算机能力突出。建议重点关注。"
}
```

**打分维度**:
1. 人才与招聘需求的**匹配度**
2. **学历**是否优秀（学校层次 985/211/海外、专业相关性）
3. **经历**是否丰富（实习数量、公司含金量、工作年限）
4. **成果**是否突出（项目/实习中的具体贡献、成果量化指标）
5. **技能**是否全面（硬技能 + 软技能覆盖程度）

**调用方式**: 每人单独调用，通过 ThreadPoolExecutor 并行执行（max_workers=4），减少总耗时。

**模型配置**:
- 模型: Qwen3.6-27B-FP8
- Temperature: 0.0
- thinking mode: 关闭
- max_tokens: 4096

### 3. `search_resumes.py` 修改

**移除**:
- FTS5 搜索调用 (`_fts_search` 方法)
- FTS 权重相关常量 (`FTS_WEIGHT`, `TOP_K_FTS_ALL`)
- 合并函数中 FTS 相关逻辑 (`fts_scores`, `fts_score`)

**修改**:
- 向量搜索后直接按人聚合计算向量分
- 取 top 50 人（向量分排序）→ 进入 Reranker
- Reranker 后取 top 25 → 进 Review LLM
- Review 通过后取前 K 名 → 进 Comment LLM
- 将 Comment LLM 结果拼入最终输出

**新增常量**:
- `REVIEW_TOP_K = 25` — Reranker 后进入 Review 的人数
- `COMMENT_DEFAULT_K = 5` — 默认推荐人数
- `COMMENT_MAX_K = 10` — 推荐人数上限

**注意**:
- `query_resume_db` 工具不受影响，保持现有功能
- `send_resume_pdf` 工具不受影响

### 4. Prompts

**`prompts/review_llm.md`**:
- 定义 Review LLM 的角色定位（硬性条件审查员）
- 明确审查重点：只需检查 meta 信息中的关键字段（学校、技能、经历等）
- Round 1：逐人判断通过/淘汰及原因
- Round 2 (Reflect)：指出 Round 1 的潜在错误，修正后输出最终结果
- 输出格式：JSON

**`prompts/comment_llm.md`**:
- 定义 Comment LLM 的角色定位（人才评估专家）
- 明确打分维度：匹配度、学历、经历、成果、技能
- 评分标准：1-10 分
- 输出格式：JSON

**`prompts/system_prompt.md`**:
- 补充 Comment LLM 结果展示格式说明
- 要求展示候选人基础信息 + AI 评论（评分 + 评语）

### 5. 错误处理

- **Review LLM 失败**: 跳过 Review 阶段，直接使用 Reranker 结果输出
- **Comment LLM 失败**: 跳过该候选人的评论，仅输出基础信息
- **Review 通过率为 0**: 告知用户没有符合条件的候选人
- **Review 通过人数 < K**: 按实际通过人数输出

### 6. 性能预估

| 步骤 | 调用次数 | 预估耗时 |
|:---|:-------:|:-------:|
| 语义搜索（已有） | 1 次 | ~2s |
| Reranker（已有） | 1 次 | ~3s |
| Review LLM（25人） | 2 次（初判+Reflect） | ~10s |
| Comment LLM（K=5） | 5 次（并发4） | ~6s |
| Agent LLM 回复（已有） | 1 次 | ~4s |
| **总计** | | **~25s** |

### 7. 工作量预估

| 文件 | 操作 | 预估行数 |
|:---|:---|:-------:|
| `services/tools/search_resumes.py` | 修改 | 核心逻辑重写 |
| `services/review_llm.py` | 新增 | ~100 行 |
| `services/comment_llm.py` | 新增 | ~100 行 |
| `prompts/review_llm.md` | 新增 | ~80 行 |
| `prompts/comment_llm.md` | 新增 | ~50 行 |
| `prompts/system_prompt.md` | 修改 | ~10 行 |
| `services/__init__.py` | 修改 | ~2 行 |

### 8. 测试要点

- Review LLM 的初判和 Reflect 两轮结果对比（Reflect 是否修正了错误）
- Comment LLM 的打分是否合理（人工抽样验证）
- 并发 Comment LLM 的稳定性
- Review 通过率 0 的边缘情况
- 整体搜索耗时在预期范围内
