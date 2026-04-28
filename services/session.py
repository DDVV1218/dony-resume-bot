"""Session 管理层 - JSON 文件持久化、线程安全、多 Session 切换"""

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SessionInfo:
    """Session 简要信息"""
    id: str
    session_key: str
    created_at: str
    updated_at: str
    message_count: int


@dataclass
class Session:
    """完整的 Session 数据"""
    id: str
    session_key: str
    created_at: str
    updated_at: str
    messages: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(**data)


class SessionStore:
    """Session 存储管理

    目录结构:
        sessions_dir/
        ├── dm_ou_xxxxx/
        │   ├── 001.json
        │   ├── 002.json
        │   └── active.txt
        └── group_oc_yyyyy/
            ├── 001.json
            └── active.txt
    """

    def __init__(self, sessions_dir: str):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        # 每个 session_key 一个可重入锁（方法内部会互相调用）
        self._locks: Dict[str, threading.RLock] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, session_key: str) -> threading.RLock:
        """获取指定 session_key 的锁（线程安全）"""
        with self._global_lock:
            if session_key not in self._locks:
                self._locks[session_key] = threading.RLock()
            return self._locks[session_key]

    def _user_dir(self, session_key: str) -> Path:
        """获取用户/群的 Session 目录

        将 session_key 中的冒号替换为下划线，避免文件名问题
        例如: dm:ou_xxx → dm_ou_xxx
        """
        safe_key = session_key.replace(":", "_")
        user_dir = self.sessions_dir / safe_key
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _session_file(self, user_dir: Path, session_id: str) -> Path:
        return user_dir / f"{session_id}.json"

    def _active_file(self, user_dir: Path) -> Path:
        return user_dir / "active.txt"

    def _next_session_id(self, user_dir: Path) -> str:
        """计算下一个 Session ID（三位数字，如 001, 002）"""
        existing = [f.stem for f in user_dir.glob("*.json")]
        if not existing:
            return "001"
        max_id = max(int(sid) for sid in existing)
        return f"{max_id + 1:03d}"

    def _save_session(self, user_dir: Path, session: Session) -> None:
        """保存 Session 到 JSON 文件"""
        session_file = self._session_file(user_dir, session.id)
        session_file.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_active(self, user_dir: Path, session_id: str) -> None:
        """保存 active session ID"""
        self._active_file(user_dir).write_text(session_id, encoding="utf-8")

    def get_or_create(self, session_key: str) -> Session:
        """获取当前 active session，不存在则创建

        Returns:
            Session 对象
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            active_file = self._active_file(user_dir)

            if active_file.exists():
                active_id = active_file.read_text(encoding="utf-8").strip()
                session_file = self._session_file(user_dir, active_id)
                if session_file.exists():
                    return Session.from_dict(
                        json.loads(session_file.read_text(encoding="utf-8"))
                    )

            # 不存在则创建
            return self._create_new_session(session_key, user_dir)

    def _create_new_session(self, session_key: str, user_dir: Path) -> Session:
        """创建新的 Session"""
        now = datetime.now().isoformat()
        session_id = self._next_session_id(user_dir)

        # 写入系统提示词占位（实际由 LLM 层注入）
        session = Session(
            id=session_id,
            session_key=session_key,
            created_at=now,
            updated_at=now,
            messages=[],
        )
        self._save_session(user_dir, session)
        self._save_active(user_dir, session.id)
        return session

    def append_message(self, session_key: str, role: str, content: str) -> Session:
        """追加消息到当前 active session

        Args:
            session_key: Session 标识
            role: 消息角色 (system/user/assistant)
            content: 消息内容

        Returns:
            更新后的 Session 对象
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            session = self.get_or_create(session_key)
            session.messages.append({"role": role, "content": content})
            session.updated_at = datetime.now().isoformat()
            self._save_session(user_dir, session)
            return session

    def get_messages(self, session_key: str, session_id: Optional[str] = None) -> List[Dict[str, str]]:
        """获取指定 Session 的全部消息

        Args:
            session_key: Session 标识
            session_id: 指定 session ID，None 则用 active

        Returns:
            消息列表 [{role, content}, ...]
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            if session_id is None:
                session = self.get_or_create(session_key)
            else:
                session_file = self._session_file(user_dir, session_id)
                if not session_file.exists():
                    raise FileNotFoundError(f"Session not found: {session_key}/{session_id}")
                session = Session.from_dict(
                    json.loads(session_file.read_text(encoding="utf-8"))
                )
            return session.messages

    def list_sessions(self, session_key: str) -> List[SessionInfo]:
        """列出指定用户/群的所有 Session

        Returns:
            SessionInfo 列表
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            active_file = self._active_file(user_dir)
            active_id = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else None

            sessions = []
            for f in sorted(user_dir.glob("*.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append(
                    SessionInfo(
                        id=data["id"],
                        session_key=data["session_key"],
                        created_at=data["created_at"],
                        updated_at=data["updated_at"],
                        message_count=len(data.get("messages", [])),
                    )
                )
            return sessions

    def switch_session(self, session_key: str, session_id: str) -> Session:
        """切换到指定 Session

        Args:
            session_key: Session 标识
            session_id: 目标 Session ID

        Returns:
            切换后的 Session 对象

        Raises:
            FileNotFoundError: Session 不存在
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            session_file = self._session_file(user_dir, session_id)
            if not session_file.exists():
                raise FileNotFoundError(f"Session not found: {session_key}/{session_id}")
            self._save_active(user_dir, session_id)
            return Session.from_dict(
                json.loads(session_file.read_text(encoding="utf-8"))
            )

    def create_session(self, session_key: str) -> Session:
        """创建新的 Session 并切换为 active

        Returns:
            新创建的 Session 对象
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            return self._create_new_session(session_key, user_dir)

    def delete_session(self, session_key: str, session_id: str) -> bool:
        """删除指定 Session

        Args:
            session_key: Session 标识
            session_id: 目标 Session ID

        Returns:
            True 如果删除成功，False 如果不存在
        """
        lock = self._get_lock(session_key)
        with lock:
            user_dir = self._user_dir(session_key)
            session_file = self._session_file(user_dir, session_id)
            if not session_file.exists():
                return False
            session_file.unlink()
            # 如果删除的是 active，切换到最新的
            active_file = self._active_file(user_dir)
            if active_file.exists() and active_file.read_text().strip() == session_id:
                remaining = sorted(user_dir.glob("*.json"), key=lambda f: f.stem, reverse=True)
                if remaining:
                    self._save_active(user_dir, remaining[0].stem)
                else:
                    active_file.unlink()
            return True
