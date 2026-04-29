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
