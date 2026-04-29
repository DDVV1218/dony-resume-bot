"""飞书文件下载工具

处理用户发送的文件（PDF、图片等），从飞书服务器下载到本地。

飞书正确的文件内容下载端点：
  GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file
"""

import json
import logging
import os
import time
from typing import Optional

import httpx

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


def upload_and_send_file(
    file_path: str,
    conversation_id: str,
    app_id: str,
    app_secret: str,
) -> bool:
    """上传本地文件到飞书并发送为文件消息

    Args:
        file_path: 本地文件路径
        conversation_id: 飞书会话 ID（open_id 或 chat_id）
        app_id: 飞书 App ID
        app_secret: 飞书 App Secret

    Returns:
        是否成功
    """
    token = _get_token(app_id, app_secret)
    if not token:
        return False

    file_name = os.path.basename(file_path)

    try:
        # 1. 上传文件到飞书
        with open(file_path, "rb") as f:
            resp = httpx.post(
                f"{API_BASE}/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (file_name, f, "application/pdf")},
                data={"file_type": "stream", "file_name": file_name},
                timeout=30,
            )
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"File upload failed: {data.get('msg')}")
            return False

        file_key = data["data"]["file_key"]
        logger.info(f"File uploaded: {file_name} -> file_key={file_key}")

        # 2. 发送文件消息
        receive_id_type = "open_id" if conversation_id.startswith("ou_") else "chat_id"
        content = {"file_key": file_key}
        resp = httpx.post(
            f"{API_BASE}/im/v1/messages?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": conversation_id,
                "msg_type": "file",
                "content": json.dumps(content, ensure_ascii=False),
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"File message sent: {file_name} to {conversation_id}")
            return True
        else:
            logger.error(f"Send file message failed: {data.get('msg')}")
            return False

    except Exception as e:
        logger.error(f"Upload/send file exception: {e}")
        return False
