"""PDF 分类器 - 快速探测 PDF 类型，决定提取策略

分层策略：
1. PyMuPDF 快速探测文本层质量
2. 高质量文本页 → 直接提取文本（毫秒级）
3. 低质量/图片页 → 返回信号走 MinerU VLM（秒级）

不引入 OCR 层，非文本 PDF 全部走 MinerU。
"""

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TEXT_CHARS_THRESHOLD = 80
WORDS_THRESHOLD = 10
GARBAGE_RATIO_THRESHOLD = 0.30
TEXT_PAGE_RATIO_THRESHOLD = 0.70


@dataclass
class PageProfile:
    """单页 PDF 的分析档案"""
    page_index: int
    text_chars: int = 0
    word_count: int = 0
    garbage_ratio: float = 1.0
    image_count: int = 0
    large_image_ratio: float = 0.0
    block_count: int = 0
    classification: str = "unknown"


@dataclass
class PdfClassification:
    """PDF 整体分类结果"""
    pdf_path: str
    page_count: int
    decision: str  # "use_text_extraction" | "use_mineru_vlm"
    page_type_counts: Dict[str, int] = field(default_factory=dict)
    pages: List[PageProfile] = field(default_factory=list)
    extracted_text: Optional[str] = None


def _garbage_ratio(text: str) -> float:
    """估算抽取文本的乱码比例"""
    if not text:
        return 1.0
    normal = re.findall(
        r"[\u4e00-\u9fffA-Za-z0-9\s,.;:!?()（）\[\]【】/\\\-+_@#%&*=<>|·•、。，\u201c\u201d\u2018\u2019：；？！]",
        text,
    )
    return 1 - len(normal) / max(len(text), 1)


def _get_large_image_ratio(page) -> float:
    """估算图片覆盖面积占页面面积的比例"""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    image_area = 0.0
    for img in page.get_images(full=True):
        rects = page.get_image_rects(img[0])
        for rect in rects:
            image_area += rect.width * rect.height
    return min(image_area / page_area, 1.0)


def _classify_page(page, page_index: int) -> PageProfile:
    """分析单页 PDF"""
    text = page.get_text("text") or ""
    words = page.get_text("words") or []
    blocks = page.get_text("blocks") or []
    images = page.get_images(full=True) or []

    text_chars = len(text.strip())
    word_count = len(words)
    gr = _garbage_ratio(text)
    image_count = len(images)
    large_image_ratio = _get_large_image_ratio(page)
    block_count = len(blocks)

    # 决策
    if text_chars >= TEXT_CHARS_THRESHOLD and word_count >= WORDS_THRESHOLD and gr < GARBAGE_RATIO_THRESHOLD:
        cls = "text_page"
    elif text_chars < 30 and image_count > 0 and large_image_ratio >= 0.60:
        cls = "scanned_page"
    elif image_count > 0 and large_image_ratio >= 0.35:
        cls = "image_complex_page"
    elif text_chars < TEXT_CHARS_THRESHOLD:
        cls = "weak_text_page"
    else:
        cls = "complex_page"

    return PageProfile(
        page_index=page_index,
        text_chars=text_chars,
        word_count=word_count,
        garbage_ratio=round(gr, 3),
        image_count=image_count,
        large_image_ratio=round(large_image_ratio, 3),
        block_count=block_count,
        classification=cls,
    )


def _extract_text_fast(doc) -> str:
    """用 PyMuPDF 快速提取所有页文本，返回 Markdown 格式"""
    parts = []
    for i, page in enumerate(doc):
        text = page.get_text("text") or ""
        text = text.strip()
        if text:
            parts.append(f"## Page {i + 1}\n\n{text}")
        else:
            parts.append(f"## Page {i + 1}\n\n（此页无文本内容）")
    return "\n\n".join(parts)


def classify_and_extract(pdf_path: str) -> PdfClassification:
    """分析 PDF 并选择提取策略

    Args:
        pdf_path: PDF 文件路径

    Returns:
        PdfClassification 对象，包含分类结果和可能的提取文本
    """
    try:
        import fitz
    except ImportError:
        logger.error("PyMuPDF (fitz) not installed, falling back to MinerU")
        return PdfClassification(
            pdf_path=pdf_path,
            page_count=0,
            decision="use_mineru_vlm",
        )

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.error(f"PyMuPDF failed to open {pdf_path}: {e}")
        return PdfClassification(
            pdf_path=pdf_path,
            page_count=0,
            decision="use_mineru_vlm",
        )

    total = len(doc)

    if total == 0:
        doc.close()
        return PdfClassification(
            pdf_path=pdf_path,
            page_count=0,
            decision="use_mineru_vlm",
        )

    # 逐页分析
    profiles: List[PageProfile] = []
    for i in range(total):
        profiles.append(_classify_page(doc[i], i))

    # 统计
    counts: Dict[str, int] = {}
    for p in profiles:
        counts[p.classification] = counts.get(p.classification, 0) + 1

    text_like = counts.get("text_page", 0)
    text_pages_ratio = text_like / total

    # 文档级决策
    if text_pages_ratio >= TEXT_PAGE_RATIO_THRESHOLD:
        # 大部分页是文本 → 走快速提取
        extracted = _extract_text_fast(doc)
        doc.close()
        logger.info(
            f"PDF {pdf_path}: {text_like}/{total} text pages ({text_pages_ratio:.0%}), "
            f"using fast text extraction ({len(extracted)} chars)"
        )
        return PdfClassification(
            pdf_path=pdf_path,
            page_count=total,
            decision="use_text_extraction",
            page_type_counts=counts,
            pages=profiles,
            extracted_text=extracted,
        )
    else:
        # 文本不足 → 走 MinerU
        doc.close()
        logger.info(
            f"PDF {pdf_path}: only {text_like}/{total} text pages ({text_pages_ratio:.0%}), "
            f"falling back to MinerU VLM"
        )
        return PdfClassification(
            pdf_path=pdf_path,
            page_count=total,
            decision="use_mineru_vlm",
            page_type_counts=counts,
            pages=profiles,
        )
