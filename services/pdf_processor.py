"""PDF 解析处理器 - 基于 MinerU CLI

使用 mineru 官方轻量客户端模式：
  mineru -p input.pdf -o output/ -b vlm-http-client -u <server_url>

MinerU 内部处理：PDF → 图片 → VLM HTTP → blocks → json2md → Markdown
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


def _save_markdown_copy(markdown: str, pdf_path: Path, config: Config) -> None:
    """保存 Markdown 副本到 mineru_process 目录"""
    try:
        process_dir = Path(config.mineru_process_dir)
        process_dir.mkdir(parents=True, exist_ok=True)
        md_filename = pdf_path.stem + ".md"
        md_path = process_dir / md_filename
        counter = 1
        while md_path.exists():
            md_path = process_dir / f"{pdf_path.stem}_{counter}.md"
            counter += 1
        md_path.write_text(markdown, encoding="utf-8")
        logger.info(f"Markdown saved: {md_path}")
    except Exception as e:
        logger.warning(f"Failed to save markdown: {e}")


def process_pdf(pdf_path: str, config: Config) -> Optional[str]:
    """解析 PDF 为 Markdown

    分层策略：
    1. 先用 PyMuPDF 快速探测文本层质量
    2. >70% 页是文本 → 直接 PyMuPDF 提取（毫秒级）
    3. 否则 → MinerU VLM（秒级）

    Args:
        pdf_path: PDF 文件路径
        config: 应用配置

    Returns:
        Markdown 文本，失败返回 None
    """
    path = Path(pdf_path)
    if not path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        return None

    # === 快速路径：PyMuPDF 分类 + 提取 ===
    try:
        from services.pdf_classifier import classify_and_extract

        classification = classify_and_extract(str(path))

        if classification.decision == "use_text_extraction" and classification.extracted_text:
            markdown = classification.extracted_text
            logger.info(
                f"Fast path: PyMuPDF extracted {len(markdown)} chars from "
                f"{classification.page_count} pages "
                f"({classification.page_type_counts.get('text_page', 0)} text pages)"
            )

            # 保存副本到 mineru_process 目录
            _save_markdown_copy(markdown, path, config)
            return markdown

        if classification.decision == "use_mineru_vlm":
            logger.info(
                f"MinerU path: {classification.page_count} pages, "
                f"types={classification.page_type_counts}"
            )
    except ImportError:
        logger.warning("PyMuPDF not available, falling back to MinerU")
    except Exception as e:
        logger.warning(f"PDF classification failed (non-fatal), falling back to MinerU: {e}")

    # === 慢速路径：MinerU VLM ===
    return _process_with_mineru(pdf_path, config)


def _process_with_mineru(pdf_path: str, config: Config) -> Optional[str]:
    """使用 MinerU VLM HTTP 客户端解析 PDF 为 Markdown"""
    path = Path(pdf_path)

    # 创建临时输出目录
    with tempfile.TemporaryDirectory(prefix="mineru_") as tmp_dir:
        try:
            cmd = [
                "mineru",
                "-p", str(path),
                "-o", tmp_dir,
                "-b", "vlm-http-client",
                "-u", config.mineru_server_url,
            ]
            if config.mineru_model_name:
                cmd.extend(["-m", config.mineru_model_name])

            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,  # 15 min timeout
            )

            if result.returncode != 0:
                logger.error(f"MinerU failed (code={result.returncode}): {result.stderr[:500]}")
                return None

            logger.info(f"MinerU stdout: {result.stdout[:200]}")

            # 查找生成的 markdown 文件
            # MinerU 输出在 tmp_dir/<pdf_name>/ 或 tmp_dir/ 下
            md_file = _find_markdown_output(path, tmp_dir)
            if md_file:
                markdown = md_file.read_text(encoding="utf-8")
                logger.info(f"Markdown loaded: {md_file} ({len(markdown)} chars)")
            else:
                logger.error(f"No markdown output found in {tmp_dir}")
                logger.info(f"MinerU output dir: {list(Path(tmp_dir).iterdir())}")
                return None

        except subprocess.TimeoutExpired:
            logger.error("MinerU timed out after 900s")
            return None
        except FileNotFoundError:
            logger.error("mineru CLI not found. Run: pip install mineru")
            return None
        except Exception as e:
            logger.error(f"MinerU exception: {e}")
            return None

    # 保存副本到 mineru_process 目录
    _save_markdown_copy(markdown, path, config)
    return markdown


def _find_markdown_output(pdf_path: Path, output_dir: str) -> Optional[Path]:
    """在 MinerU 输出目录中查找生成的 Markdown 文件"""
    root = Path(output_dir)

    # MinerU 有时会在 pdf_name/ 子目录下输出
    candidates = [
        root / f"{pdf_path.stem}.md",
        root / pdf_path.stem / f"{pdf_path.stem}.md",
        root / "auto" / f"{pdf_path.stem}.md",
    ]

    # 递归查找 .md 文件
    if not any(c.exists() for c in candidates):
        md_files = list(root.rglob("*.md"))
        if md_files:
            return md_files[0]

    for c in candidates:
        if c.exists():
            return c
    return None
