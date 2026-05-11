#!/usr/bin/env python3
"""批量导入简历 PDF 到数据库

从指定路径（文件或文件夹）批量读取 PDF 简历，按完整流程：
PDF解析 → 分页 → 候选人识别 → LLM分析 → SQLite入库 → ChromaDB向量索引

用法:
    python scripts/batch_import.py /app/external_data/some.pdf
    python scripts/batch_import.py /app/external_data/简历文件夹/
    python scripts/batch_import.py /app/external_data/ --recursive
    python scripts/batch_import.py /app/external_data/ --max-pdfs 50
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

# 将外部的 external_data 也加入搜索路径，方便读取
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from services.db import configure as db_configure, get_connection, init_db
from services.pdf_processor import process_pdf
from services.resume_indexer import index_resume
from services.vector_indexer import index_resume_vectors
from services.handlers.resume_handler import ResumePDFHandler
from services.llm_utils import StructuredOutput
from prompts import load_prompt
from services.session import SessionStore

# ====== 日志设置 ======
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(log_dir, exist_ok=True)
log_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = os.path.join(log_dir, f"batch_import_{log_timestamp}.log")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logger = logging.getLogger("batch_import")
logger.setLevel(logging.INFO)
logger.handlers.clear()

# 控制台输出
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(console)

# 文件日志（完整记录）
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(file_handler)

logger.info(f"日志文件: {log_file}")


def find_pdfs(root: str, recursive: bool = False) -> List[str]:
    """扫描路径下所有 PDF 文件"""
    root = os.path.abspath(root)
    if os.path.isfile(root):
        if root.lower().endswith(".pdf"):
            return [root]
        logger.warning(f"跳过非 PDF 文件: {root}")
        return []

    pdfs = []
    if recursive:
        for dirpath, _, filenames in os.walk(root):
            for f in sorted(filenames):
                if f.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(dirpath, f))
    else:
        for f in sorted(os.listdir(root)):
            if f.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, f))

    return sorted(pdfs)


def process_single_pdf(
    pdf_path: str,
    handler: ResumePDFHandler,
    config: Config,
    archive_pdf_dir: str,
    archive_md_dir: str,
    silent: bool = True,
) -> Tuple[int, List[str]]:
    """处理单个 PDF：解析 → 分页 → 分析 → 入库

    Returns:
        (indexed_count, indexed_names)
    """
    file_name = os.path.basename(pdf_path)
    file_size = os.path.getsize(pdf_path)

    # 1. PDF → Markdown
    markdown = process_pdf(pdf_path, config)
    if not markdown:
        logger.warning(f"  ⚠️ PDF 解析为空: {file_name}")
        return 0, []

    # 2. 分页 → 候选人
    candidates = handler._split_into_candidates(markdown, pdf_source=pdf_path)
    if not candidates:
        logger.warning(f"  ⚠️ 未能识别出候选人: {file_name}")
        return 0, []

    indexed_names = []
    for cand in candidates:
        name = cand.get("name", "")
        person_text = cand.get("text", "")
        if not name or not person_text:
            logger.warning(f"  ⚠️ {file_name} → 跳过无名称/内容的候选人")
            continue

        # 3. LLM 分析
        analysis = handler._analyze_person(person_text, name)
        if not analysis or not analysis.is_resume:
            logger.warning(f"  ⚠️ {file_name} → {name} LLM判定非简历")
            continue

        # 4. 落盘个人 Markdown
        safe_name = name.replace("/", "_").replace("\\", "_")
        pdf_basename = os.path.splitext(file_name)[0]
        person_md_path = os.path.join(archive_md_dir, f"{pdf_basename}_{safe_name}.md")
        os.makedirs(os.path.dirname(person_md_path), exist_ok=True)
        with open(person_md_path, "w", encoding="utf-8") as f:
            f.write(person_text)

        # 5. SQLite 入库
        meta = analysis.to_meta()
        if meta.phone == "null":
            meta.phone = ""
        if meta.email == "null":
            meta.email = ""

        resume_id = None
        if meta.name:
            resume_id = index_resume(
                name=meta.name,
                phone=meta.phone or "",
                email=meta.email or "",
                undergraduate=meta.undergraduate,
                master=meta.master,
                doctor=meta.doctor,
                skills=meta.skills,
                intership_comps=meta.intership_comps,
                work_comps=meta.work_comps,
                full_text=person_text,
                pdf_path=pdf_path,
                markdown_path=person_md_path,
            )

            # 6. ChromaDB 向量索引
            if resume_id is not None:
                try:
                    index_resume_vectors(
                        resume_id=resume_id,
                        full_text=person_text,
                        sections=analysis.sections,
                        config=config,
                    )
                except Exception as e:
                    logger.warning(f"  ⚠️ {file_name} → {name} 向量索引失败: {e}")

            # 7. 归档 PDF（只归档一次，避免重复）
            if not silent:
                archive_dst = os.path.join(archive_pdf_dir, file_name)
                if not os.path.exists(archive_dst):
                    os.makedirs(archive_pdf_dir, exist_ok=True)
                    try:
                        import shutil
                        shutil.copy2(pdf_path, archive_dst)
                        conn = get_connection()
                        conn.execute("UPDATE resumes SET pdf_path = ? WHERE id = ?",
                                     (archive_dst, resume_id))
                        conn.commit()
                    except Exception as e:
                        logger.warning(f"  ⚠️ {file_name} → 归档失败: {e}")

            logger.info(f"✅ {file_name} → 入库: {meta.name} (id={resume_id}, "
                        f"学校={meta.master or meta.undergraduate or '无'}, "
                        f"技能={meta.skills[:40] if meta.skills else '无'}...)")

        indexed_names.append(name)

    return len(indexed_names), indexed_names


def main():
    parser = argparse.ArgumentParser(
        description="批量导入简历 PDF 到数据库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/batch_import.py /app/external_data/some.pdf
  python scripts/batch_import.py /app/external_data/简历文件夹/
  python scripts/batch_import.py /app/external_data/ --recursive
  python scripts/batch_import.py /app/external_data/ --max-pdfs 20 --silent
        """,
    )
    parser.add_argument("path", help="PDF 文件或文件夹路径")
    parser.add_argument("--recursive", "-r", action="store_true", help="递归遍历子目录")
    parser.add_argument("--max-pdfs", "-m", type=int, default=0, help="最多处理的 PDF 数量（0=无限制）")
    parser.add_argument("--silent", "-s", action="store_true", help="安静模式，不归档文件")
    parser.add_argument("--no-vector", action="store_true", help="跳过向量索引（仅入库 SQLite）")
    args = parser.parse_args()

    # 初始化
    config = Config()
    db_configure(config)
    init_db(config)

    # 修正路径（如果用户传的是 host 路径，尝试转容器内路径）
    input_path = args.path
    if input_path.startswith("/data/turing-apps/resume-bot/external_data"):
        input_path = input_path.replace("/data/turing-apps/resume-bot/external_data", "/app/external_data")

    if not os.path.exists(input_path):
        logger.error(f"路径不存在: {input_path}")
        sys.exit(1)

    # 遍历 PDF
    pdfs = find_pdfs(input_path, recursive=args.recursive)
    if not pdfs:
        logger.warning("未找到任何 PDF 文件")
        sys.exit(0)

    if args.max_pdfs > 0 and len(pdfs) > args.max_pdfs:
        logger.info(f"限制处理前 {args.max_pdfs} 个 PDF（共找到 {len(pdfs)} 个）")
        pdfs = pdfs[:args.max_pdfs]

    # 创建设置路径
    archive_pdf_dir = config.resume_archive_pdf_dir
    archive_md_dir = config.resume_archive_md_dir
    os.makedirs(archive_pdf_dir, exist_ok=True)
    os.makedirs(archive_md_dir, exist_ok=True)

    # 创建 Handler（复用其 _split_into_candidates 和 _analyze_person）
    handler = ResumePDFHandler(config, SessionStore(config.sessions_dir), load_prompt("system_prompt"))

    # ====== 批量处理 ======
    logger.info(f"共发现 {len(pdfs)} 个 PDF 文件，开始处理...")
    logger.info(f"输入路径: {input_path}")
    logger.info(f"递归模式: {args.recursive}")
    logger.info(f"限制数量: {args.max_pdfs if args.max_pdfs > 0 else '无限制'}")

    try:
        from tqdm import tqdm
    except ImportError:
        logger.error("请先安装 tqdm: pip install tqdm")
        sys.exit(1)

    total_people = 0
    total_pdfs_ok = 0
    total_pdfs_fail = 0
    start_time = time.time()
    errors = []

    pbar = tqdm(pdfs, desc="入库进度", unit="pdf", ncols=80)
    for pdf_path in pbar:
        file_name = os.path.basename(pdf_path)
        pbar.set_postfix_str(f"{file_name[:30]}")

        t0 = time.time()
        try:
            n, names = process_single_pdf(
                pdf_path, handler, config,
                archive_pdf_dir, archive_md_dir,
                silent=args.silent,
            )
            elapsed = time.time() - t0

            if n > 0:
                total_people += n
                total_pdfs_ok += 1
                tqdm.write(f"  ✅ {file_name[:35]:35s} → {n}人入库 ({elapsed:.1f}s)")
                logger.info(f"✅ {file_name} → {n}人入库 ({elapsed:.1f}s)")
            else:
                total_pdfs_fail += 1
                tqdm.write(f"  ⚠️ {file_name[:35]:35s} → 未识别 ({elapsed:.1f}s)")
                logger.warning(f"⚠️ {file_name} → 未识别出候选人")
        except Exception as e:
            total_pdfs_fail += 1
            elapsed = time.time() - t0
            tqdm.write(f"  ❌ {file_name[:35]:35s} → 失败: {str(e)[:60]} ({elapsed:.1f}s)")
            errors.append((file_name, str(e)))
            logger.error(f"❌ {file_name} → 失败: {e} ({elapsed:.1f}s)")

    # ====== 汇总 ======
    total_time = time.time() - start_time
    print(f"\n{'='*50}")
    print(f"  批量导入完成")
    print(f"{'='*50}")
    print(f"  PDF 总数:   {len(pdfs)}")
    print(f"  成功入库:   {total_pdfs_ok} 个文件")
    print(f"  未识别:     {total_pdfs_fail} 个文件")
    print(f"  入库人数:   {total_people} 人")
    print(f"  总用时:     {total_time:.1f}s ({total_time/len(pdfs):.1f}s/PDF)")
    print(f"{'='*50}")

    # 记录汇总到日志
    logger.info("=" * 40)
    logger.info(f"批量导入完成 - PDF: {len(pdfs)}, 成功: {total_pdfs_ok}, 失败: {total_pdfs_fail}, 入库人数: {total_people}")
    logger.info(f"总用时: {total_time:.1f}s")

    if errors:
        print(f"\n⚠️ 失败详情:")
        for fname, err in errors[:10]:
            print(f"  {fname}: {err[:80]}")
        if len(errors) > 10:
            print(f"  ...共 {len(errors)} 个失败")

        # 全部失败详情写入日志
        logger.warning(f"共 {len(errors)} 个文件处理失败:")
        for fname, err in errors:
            logger.warning(f"  ❌ {fname}: {err}")

    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]
    print(f"\n当前数据库总计: {total} 份简历")


if __name__ == "__main__":
    main()
