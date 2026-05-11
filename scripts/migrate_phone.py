"""迁移现有数据：格式化所有手机号"""
import sys; sys.path.insert(0, "/app")
import json
import re
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

from services.db import configure, get_connection
from config import Config
from services.resume_indexer import _normalize_phone

configure(Config())
conn = get_connection()

# 获取全部记录
rows = conn.execute("SELECT id, name, phone, metadata FROM resumes ORDER BY id").fetchall()
total = len(rows)
updated = 0
conflicts = 0
skipped = 0

for r in rows:
    clean = _normalize_phone(r["phone"])
    if clean == r["phone"]:
        skipped += 1
        continue

    # 先检查是否会冲突
    existing = conn.execute(
        "SELECT id FROM resumes WHERE name = ? AND phone = ? AND id != ?",
        (r["name"], clean, r["id"])
    ).fetchone()
    if existing:
        logger.warning("⚠️ 冲突: id=%d (%s, %s) 与 id=%d phone=%s 冲突，跳过",
                       r["id"], r["name"], r["phone"], existing[0], clean)
        conflicts += 1
        continue

    # 更新 phone 列和 metadata
    md = json.loads(r["metadata"])
    md["phone"] = clean
    conn.execute(
        "UPDATE resumes SET phone = ?, metadata = ? WHERE id = ?",
        (clean, json.dumps(md, ensure_ascii=False), r["id"])
    )
    updated += 1
    logger.info("✅ id=%d %s: %s -> %s", r["id"], r["name"], r["phone"], clean)

conn.commit()

print(f"\n总计: {total}")
print(f"已更新: {updated}")
print(f"无变化: {skipped}")
print(f"冲突跳过: {conflicts}")
