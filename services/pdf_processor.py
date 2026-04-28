"""PDF 解析处理器 - 基于 MinerU VLM 服务

使用 turing-agent 相同的 MinerU VLM 管道：
  PDF → PyMuPDF 每页图片 → MinerU VLM HTTP 服务 → blocks → json2md → Markdown
"""

import logging
import os
from pathlib import Path
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


def process_pdf(pdf_path: str, config: Config) -> Optional[str]:
    """使用 MinerU VLM 服务解析 PDF 为 Markdown

    Args:
        pdf_path: PDF 文件路径
        config: 应用配置（含 mineru_server_url 等）

    Returns:
        Markdown 文本，失败返回 None
    """
    path = Path(pdf_path)
    if not path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        return None

    # 1. PDF → 每页 PIL Image（使用 PyMuPDF）
    try:
        import fitz
        from PIL import Image
        import io
    except ImportError:
        logger.error("PyMuPDF (fitz) not installed. Run: pip install pymupdf")
        return None

    doc = fitz.open(pdf_path)
    page_images: list[Image.Image] = []
    try:
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            page_images.append(pil_img)
    finally:
        doc.close()

    if not page_images:
        logger.error(f"No pages found in PDF: {pdf_path}")
        return None

    logger.info(f"PDF loaded: {len(page_images)} pages, {path.name}")

    # 2. 调用 MinerU VLM 服务
    try:
        from mineru_vl_utils import MinerUClient
        from mineru_vl_utils.post_process import json2md
    except ImportError:
        logger.error("mineru-vl-utils not installed. Run: pip install mineru-vl-utils")
        return None

    server_headers = None

    client = MinerUClient(
        backend="http-client",
        server_url=config.mineru_server_url,
        model_name=config.mineru_model_name or None,
        server_headers=server_headers,
        http_timeout=600,
        max_concurrency=4,
        image_analysis=True,
    )

    try:
        page_results = client.batch_two_step_extract(page_images)
    except Exception as e:
        logger.error(f"MinerU VLM extraction failed: {e}")
        return None

    # 3. 每页 blocks → Markdown
    markdown_pages: list[str] = []
    for page_index, extract_result in enumerate(page_results, start=1):
        page_md = json2md(extract_result)
        markdown_pages.append(page_md)

    raw_markdown = "\n\n---\n\n".join(markdown_pages)

    # 4. 保存 .md 文件到 mineru_process 目录
    try:
        process_dir = Path(config.mineru_process_dir)
        process_dir.mkdir(parents=True, exist_ok=True)
        md_filename = path.stem + ".md"
        md_path = process_dir / md_filename

        # 避免重名
        counter = 1
        while md_path.exists():
            md_path = process_dir / f"{path.stem}_{counter}.md"
            counter += 1

        md_path.write_text(raw_markdown, encoding="utf-8")
        logger.info(f"Markdown saved: {md_path} ({len(raw_markdown)} chars)")
    except Exception as e:
        logger.warning(f"Failed to save markdown file: {e}")

    logger.info(f"PDF processed: {len(page_results)} pages, {len(raw_markdown)} chars")
    return raw_markdown
