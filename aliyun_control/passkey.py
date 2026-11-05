from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet

LOG = logging.getLogger(__name__)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PasskeyStore:
    """Encrypted, single-admin WebAuthn credential store."""

    def __init__(self, path: Optional[Path], key_path: Optional[Path]) -> None:
        self.path = path
        self.key_path = key_path
        self._lock = threading.RLock()
        self._credentials: List[Dict[str, Any]] = []
        self._load()

    @property
    def credentials(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._credentials]

    def find(self, credential_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for item in self._credentials:
                if item.get("credential_id") == credential_id:
                    return dict(item)
        return None

    def add(self, credential: Dict[str, Any]) -> None:
        with self._lock:
            if self.find(str(credential["credential_id"])):
                raise ValueError("This passkey is already registered")
            self._credentials.append(dict(credential))
            self._save()

    def update_sign_count(self, credential_id: str, sign_count: int) -> None:
        with self._lock:
            for item in self._credentials:
                if item.get("credential_id") == credential_id:
                    item["sign_count"] = int(sign_count)
                    item["last_used_at"] = now_iso()
                    self._save()
                    return
            raise KeyError(credential_id)

    def remove(self, credential_id: str) -> bool:
        with self._lock:
            before = len(self._credentials)
            self._credentials = [item for item in self._credentials if item.get("credential_id") != credential_id]
            if len(self._credentials) == before:
                return False
            self._save()
            return True

    def public_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "credential_id": item["credential_id"],
                    "name": item.get("name", "Passkey"),
                    "created_at": item.get("created_at"),
                    "last_used_at": item.get("last_used_at"),
                }
                for item in self._credentials
            ]

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        if not self.key_path or not self.key_path.exists():
            raise ValueError("Passkey credential key is missing")
        try:
            token = self.path.read_bytes()
            payload = json.loads(Fernet(self.key_path.read_bytes().strip()).decrypt(token).decode("utf-8"))
            if not isinstance(payload, list):
                raise ValueError("Passkey store must contain an array")
            self._credentials = [item for item in payload if isinstance(item, dict)]
        except Exception as exc:
            raise ValueError(f"Could not read passkey store: {self.path}") from exc

    def _save(self) -> None:
        if not self.path:
            return
        if not self.key_path or not self.key_path.exists():
            raise ValueError("Passkey credential key is missing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = Fernet(self.key_path.read_bytes().strip()).encrypt(
            json.dumps(self._credentials, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        )
        fd, temporary = tempfile.mkstemp(prefix="passkeys-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
