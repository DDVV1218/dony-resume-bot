# Progress Log

## T001

- **Summary**: SQLite + FTS5 infrastructure complete
- **Files**: services/db.py (new), config.py (modified), main.py (modified), requirements.txt (modified)
- **Verification**: FTS5 BM25 MATCH works, jieba cuts correctly, all tables created
- **Result**: pass

## T002

- **Summary**: Resume indexing pipeline complete
- **Files**: services/resume_indexer.py (new), services/handlers/resume_handler.py (modified)
- **Verification**: FTS5 MATCH '复旦 AND 灵均' returns 1 result, UPSERT dedup verified
- **Result**: pass

## T003

- **Summary**: FTS5 search pipeline + LLM keyword extraction complete
- **Files**: services/resume_searcher.py (new), services/keyword_extractor.py (new), services/handlers/text_handler.py (modified)
- **Verification**: build_fts5_query supports OR/NOT/special chars, search_resumes returns BM25 results, text_handler integrates search
- **Result**: pass

---

## 最终状态

- **T001** ✅ SQLite + FTS5 基础设施
- **T002** ✅ 简历入库索引链路
- **T003** ✅ FTS5 检索链路 + LLM 关键词提取
- **全部任务完成**
