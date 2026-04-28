"""飞书文件下载工具

处理用户发送的文件（PDF、图片等），从飞书服务器下载到本地。
"""

import json
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://open.feishu.cn/open-apis"

# Token 缓存复用（与 streaming_card 共享同一个模式）
TOKEN_CACHE: dict[str, tuple[str, float]] = {}
TOKEN_TTL = 3600


def _get_token(app_id: str, app_secret: str) -> Optional[str]:
    """获取飞书 tenant_access_token（带缓存）"""
    cache_key = f"{app_id}:{app_secret[:8]}"
    now = time.time()
    cached = TOKEN_CACHE.get(cache_key)
    if cached and (now - cached[1]) < TOKEN_TTL:
        return cached[0]

    try:
        resp = httpx.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("tenant_access_token"):
            token = data["tenant_access_token"]
            TOKEN_CACHE[cache_key] = (token, now)
            return token
        logger.error(f"Get token failed: {data.get('msg')}")
    except Exception as e:
        logger.error(f"Get token exception: {e}")
    return None


def download_file(file_key: str, app_id: str, app_secret: str, save_dir: str) -> Optional[str]:
    """下载飞书文件到本地

    Args:
        file_key: 飞书文件 key（从消息 content 中提取）
        app_id: 飞书 App ID
        app_secret: 飞书 App Secret
        save_dir: 保存目录

    Returns:
        保存的文件路径，失败返回 None
    """
    token = _get_token(app_id, app_secret)
    if not token:
        return None

    # 1. 获取文件元信息（文件名、大小等）
    file_name = None
    try:
        resp = httpx.get(
            f"{API_BASE}/im/v1/files/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            # 有些版本的 API 返回 JSON，有些直接返回文件
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                data = resp.json()
                if data.get("code") == 0 and data.get("data"):
                    file_name = data["data"].get("file_name", data["data"].get("name", ""))
    except Exception as e:
        logger.warning(f"Get file meta failed: {e}")

    # 2. 下载文件内容（使用文件 key 直接获取）
    try:
        resp = httpx.get(
            f"{API_BASE}/im/v1/files/{file_key}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(f"Download file failed: status={resp.status_code}")
            return None

        # 尝试解析错误（如果返回 JSON 错误而不是文件二进制）
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                data = resp.json()
                if data.get("code") != 0:
                    logger.error(f"Download file error: code={data.get('code')}, msg={data.get('msg')}")
                    return None
            except Exception:
                pass

        # 确定文件名
        ext = ""
        ct = resp.headers.get("content-type", "")
        if "pdf" in ct:
            ext = ".pdf"
        elif "png" in ct:
            ext = ".png"
        elif "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "gif" in ct:
            ext = ".gif"

        base_name = file_name or f"file_{file_key[:12]}"
        if not base_name.lower().endswith(ext) and ext:
            base_name += ext

        # 确保目录存在
        os.makedirs(save_dir, exist_ok=True)

        save_path = os.path.join(save_dir, base_name)

        # 避免重名
        counter = 1
        while os.path.exists(save_path):
            name_parts = os.path.splitext(base_name)
            save_path = os.path.join(save_dir, f"{name_parts[0]}_{counter}{name_parts[1]}")
            counter += 1

        with open(save_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"File downloaded: {save_path} ({len(resp.content)} bytes)")
        return save_path

    except Exception as e:
        logger.error(f"Download file exception: {e}")
        return None
