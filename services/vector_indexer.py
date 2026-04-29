"""ChromaDB 向量索引器

管理 resume 向量的存储和检索。
每份简历存 4 个向量：
  - full: 整份简历
  - education: 教育背景
  - experience: 实习/工作经历
  - skills: 技能
"""

import json
import logging
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)

# 段落类型列表
CHUNK_TYPES = ["full", "education", "experience", "skills"]

# ChromaDB collection 名称
COLLECTION_NAME = "resumes"

# Embedding 服务配置
EMBEDDING_MODEL = "Qwen3-Embedding-8B"
EMBEDDING_DIM = 4096


def _get_collection(config: Config):
    """获取 ChromaDB collection（延迟初始化，单例）

    ChromaDB PersistentClient 在进程内持久化到磁盘。
    """
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=config.chroma_db_dir,
        settings=Settings(anonymized_telemetry=False),
    )

    # 获取或创建 collection
    try:
        collection = client.get_collection(COLLECTION_NAME)
        logger.info(f"ChromaDB collection '{COLLECTION_NAME}' loaded ({collection.count()} docs)")
    except Exception:
        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"ChromaDB collection '{COLLECTION_NAME}' created")

    return collection


def _get_embedding_client(config: Config):
    """获取 OpenAI-compatible embedding 客户端"""
    from openai import OpenAI

    client = OpenAI(
        base_url=config.embedding_server_url,
        api_key="not-needed",  # vLLM doesn't need API key
    )
    return client


def _embed_text(text: str, config: Config) -> Optional[List[float]]:
    """生成单个文本的 embedding 向量

    Args:
        text: 输入文本
        config: 配置

    Returns:
        4096 维向量，失败返回 None
    """
    if not text or not text.strip():
        return None

    try:
        client = _get_embedding_client(config)
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Embedding failed: {e}")
        return None


def _embed_batch(texts: List[str], config: Config) -> List[Optional[List[float]]]:
    """批量生成 embedding 向量

    Args:
        texts: 输入文本列表
        config: 配置

    Returns:
        向量列表，失败项为 None
    """
    valid_texts = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not valid_texts:
        return [None] * len(texts)

    results: List[Optional[List[float]]] = [None] * len(texts)

    try:
        client = _get_embedding_client(config)
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[t for _, t in valid_texts],
        )

        for data in response.data:
            idx = valid_texts[data.index][0]
            results[idx] = data.embedding

    except Exception as e:
        logger.error(f"Batch embedding failed: {e}")

    return results


def index_resume_vectors(
    resume_id: int,
    full_text: str,
    sections: Dict[str, str],
    config: Config,
) -> bool:
    """为一份简历生成并存储向量

    Args:
        resume_id: resumes 表的 id
        full_text: 简历全文 Markdown
        sections: 段落文本 {"education": "...", "experience": "...", "skills": "..."}
        config: 配置

    Returns:
        是否成功
    """
    # 准备所有段落文本
    texts = {
        "full": full_text,
        "education": sections.get("education", ""),
        "experience": sections.get("experience", ""),
        "skills": sections.get("skills", ""),
    }

    # 批量生成 embedding
    text_list = [texts[t] for t in CHUNK_TYPES]
    vectors = _embed_batch(text_list, config)

    # 过滤失败的
    ids = []
    embeddings = []
    metadatas = []

    for i, chunk_type in enumerate(CHUNK_TYPES):
        vec = vectors[i]
        if vec is None:
            logger.warning(f"Skip {chunk_type} for resume_id={resume_id}: embedding failed")
            continue

        chunk_id = f"{resume_id}_{chunk_type}"
        ids.append(chunk_id)
        embeddings.append(vec)
        metadatas.append({
            "resume_id": resume_id,
            "chunk_type": chunk_type,
        })

    if not ids:
        logger.error(f"No embeddings generated for resume_id={resume_id}")
        return False

    # upsert 到 ChromaDB
    try:
        collection = _get_collection(config)
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info(f"Indexed {len(ids)} vectors for resume_id={resume_id}: {[m['chunk_type'] for m in metadatas]}")
        return True
    except Exception as e:
        logger.error(f"ChromaDB upsert failed for resume_id={resume_id}: {e}")
        return False


def search_similar(
    query: str,
    config: Config,
    top_k: int = 20,
) -> List[Dict[str, Any]]:
    """向量搜索相似简历

    Args:
        query: 搜索查询（自然语言）
        config: 配置
        top_k: 返回最大结果数

    Returns:
        搜索结果，每项含 resume_id, chunk_type, score
    """
    query_vec = _embed_text(query, config)
    if query_vec is None:
        logger.error("Query embedding failed")
        return []

    try:
        collection = _get_collection(config)
        results = collection.query(
            query_embeddings=[query_vec],
            n_results=top_k,
        )

        # 解析结果
        hits = []
        if results["ids"] and results["distances"]:
            for i, chunk_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i]
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                hits.append({
                    "chunk_id": chunk_id,
                    "resume_id": metadata.get("resume_id"),
                    "chunk_type": metadata.get("chunk_type", "unknown"),
                    "score": 1.0 - distance,  # cosine distance → similarity
                })

        logger.info(f"Vector search: '{query[:30]}...' -> {len(hits)} hits")
        return hits

    except Exception as e:
        logger.error(f"Vector search failed: {e}")
        return []


def get_resume_vectors(resume_id: int, config: Config) -> List[Dict[str, Any]]:
    """获取某份简历的所有向量记录

    Args:
        resume_id: 简历 id
        config: 配置

    Returns:
        向量记录列表
    """
    try:
        collection = _get_collection(config)
        prefix = f"{resume_id}_"
        results = collection.get(
            where={"resume_id": resume_id},
        )

        if not results["ids"]:
            return []

        records = []
        for i, chunk_id in enumerate(results["ids"]):
            chunk_type = chunk_id.split("_", 1)[1] if "_" in chunk_id else "unknown"
            records.append({
                "chunk_id": chunk_id,
                "resume_id": resume_id,
                "chunk_type": chunk_type,
            })
        return records

    except Exception as e:
        logger.error(f"Get vectors for resume_id={resume_id} failed: {e}")
        return []


def delete_resume_vectors(resume_id: int, config: Config) -> bool:
    """删除某份简历的所有向量

    Args:
        resume_id: 简历 id
        config: 配置

    Returns:
        是否成功
    """
    try:
        collection = _get_collection(config)
        collection.delete(where={"resume_id": resume_id})
        logger.info(f"Deleted vectors for resume_id={resume_id}")
        return True
    except Exception as e:
        logger.error(f"Delete vectors for resume_id={resume_id} failed: {e}")
        return False
