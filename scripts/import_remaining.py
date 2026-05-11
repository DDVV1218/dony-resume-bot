"""找出未入库的 PDF 并批量导入"""
import sys, os, re
sys.path.insert(0, '/app')
import logging
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)

from config import Config
from services.db import configure as db_configure, get_connection
from services.pdf_processor import process_pdf
from services.handlers.resume_handler import ResumePDFHandler
from prompts import load_prompt
from services.session import SessionStore

config = Config()
db_configure(config)
conn = get_connection()

# 已入库的 PDF 文件名
existing = set()
for r in conn.execute("SELECT pdf_path FROM resumes WHERE pdf_path IS NOT NULL").fetchall():
    if r[0]:
        existing.add(os.path.basename(r[0]))

# 找出未入库的
root = '/app/external_data/量化研究员4.17'
todo = []
for dirpath, _, files in os.walk(root):
    for f in sorted(files):
        if f.lower().endswith('.pdf') and f not in existing:
            todo.append(os.path.join(dirpath, f))

print(f'未入库 PDF: {len(todo)}')
if not todo:
    print('全部已入库，无需操作')
    sys.exit(0)

for t in todo:
    print(f'  {os.path.basename(t)[:50]}')
print()

# 批量入库
handler = ResumePDFHandler(config, SessionStore(config.sessions_dir), load_prompt('system_prompt'))
from tqdm import tqdm

archive_pdf = config.resume_archive_pdf_dir
archive_md = config.resume_archive_md_dir
os.makedirs(archive_pdf, exist_ok=True)
os.makedirs(archive_md, exist_ok=True)

total_ok = 0
for pdf_path in tqdm(todo, desc='入库', unit='pdf', ncols=60):
    fname = os.path.basename(pdf_path)
    try:
        markdown = process_pdf(pdf_path, config)
        if not markdown:
            continue
        candidates = handler._split_into_candidates(markdown, pdf_source=pdf_path)
        for cand in candidates:
            name = cand.get('name', '')
            text = cand.get('text', '')
            if not name or not text:
                continue
            analysis = handler._analyze_person(text, name)
            if not analysis or not analysis.is_resume:
                continue
            safe_name = name.replace('/', '_').replace('\\', '_')
            pdf_base = os.path.splitext(fname)[0]
            person_md = os.path.join(archive_md, f'{pdf_base}_{safe_name}.md')
            with open(person_md, 'w', encoding='utf-8') as f:
                f.write(text)

            meta = analysis.to_meta()
            from services.resume_indexer import index_resume
            from services.vector_indexer import index_resume_vectors
            rid = index_resume(
                name=meta.name, phone=meta.phone or '', email=meta.email or '',
                undergraduate=meta.undergraduate, master=meta.master, doctor=meta.doctor,
                skills=meta.skills, intership_comps=meta.intership_comps, work_comps=meta.work_comps,
                full_text=text, pdf_path=pdf_path, markdown_path=person_md,
            )
            if rid:
                try:
                    index_resume_vectors(rid, text, analysis.sections, config)
                except Exception:
                    pass
                total_ok += 1
        tqdm.write(f'  ✅ {fname[:35]}')
    except Exception as e:
        tqdm.write(f'  ❌ {fname[:35]}: {str(e)[:50]}')

print(f'\n✅ 完成: 新增 {total_ok} 份简历')
conn = get_connection()
print(f'数据库总计: {conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]}')
