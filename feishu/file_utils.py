"""飞书文件下载工具

处理用户发送的文件（PDF、图片等），从飞书服务器下载到本地。

飞书正确的文件内容下载端点：
  GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file
"""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://open.feishu.cn/open-apis"
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


def download_file(file_key: str, message_id: str, file_name_hint: Optional[str],
                  app_id: str, app_secret: str, save_dir: str) -> Optional[str]:
    """下载飞书文件二进制内容到本地

    Args:
        file_key: 飞书文件 key（从消息 content 提取）
        message_id: 飞书消息 ID（om_xxx）
        file_name_hint: 消息中的文件名提示（可选）
        app_id: 飞书 App ID
        app_secret: 飞书 App Secret
        save_dir: 保存目录

    Returns:
        保存的文件路径，失败返回 None
    """
    token = _get_token(app_id, app_secret)
    if not token:
        return None

    try:
        resp = httpx.get(
            f"{API_BASE}/im/v1/messages/{message_id}/resources/{file_key}?type=file",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(f"Download failed: status={resp.status_code}, body={resp.text[:300]}")
            return None

        # 确定文件名
        ct = resp.headers.get("content-type", "")
        ext_map = {
            "pdf": ".pdf", "png": ".png", "jpeg": ".jpg",
            "jpg": ".jpg", "gif": ".gif",
        }
        ext = ""
        for mime, e in ext_map.items():
            if mime in ct:
                ext = e
                break

        base_name = (file_name_hint or f"file_{file_key[:12]}").strip()
        if ext and not base_name.lower().endswith(ext):
            base_name += ext

        # 确保目录存在
        os.makedirs(save_dir, exist_ok=True)

        save_path = os.path.join(save_dir, base_name)
        counter = 1
        while os.path.exists(save_path):
            name_parts = os.path.splitext(base_name)
            save_path = os.path.join(save_dir, f"{name_parts[0]}_{counter}{name_parts[1]}")
            counter += 1

        with open(save_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"File saved: {save_path} ({len(resp.content)} bytes)")
        return save_path

    except Exception as e:
        logger.error(f"Download exception: {e}")
        return None
