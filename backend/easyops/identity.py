"""Local accounts and trusted server-side principals."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import re

from fastapi import HTTPException, Request

from .store import Store

ADMIN_ID = "admin"


def resource_owner(record: dict) -> str:
    return record.get("owner_id") or ADMIN_ID


def actor_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not user.get("id"):
        raise HTTPException(401, "请先登录工作台")
    return user["id"]


def require_admin(request: Request) -> dict:
    actor_id(request)
    if request.state.user.get("role") != "admin":
        raise HTTPException(403, "此操作仅管理员可用")
    return request.state.user


def public_user(user: dict) -> dict:
    return {key: user[key] for key in ("id", "username", "name", "role", "enabled", "created_at", "updated_at", "revision") if key in user}


class IdentityService:
    def __init__(self, store: Store, bootstrap_password: str):
        self.store = store
        def initialize():
            if not store.get("user", ADMIN_ID):
                store.save("user", ADMIN_ID, {"username": "admin", "name": "运维管理员", "role": "admin", "enabled": True,
                                               "password_hash": self.password_hash(bootstrap_password)})
        store.atomic(initialize)

    @staticmethod
    def password_hash(password: str) -> str:
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise HTTPException(422, "口令长度必须为 12 至 1024 个字符")
        salt = secrets.token_bytes(16)
        value = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=128 * 1024 * 1024)
        return "scrypt$32768$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(value).decode()

    @staticmethod
    def password_matches(password, encoded):
        if not isinstance(password, str) or len(password) > 1024:
            return False
        try:
            algorithm, n, r, p, salt, expected = encoded.split("$")
            if algorithm != "scrypt" or (n, r, p) != ("32768", "8", "1"):
                return False
            value = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024)
            return secrets.compare_digest(value, base64.urlsafe_b64decode(expected))
        except (ValueError, TypeError):
            return False

    def authenticate(self, username, password):
        username = username or "admin"
        normalized = username.strip().casefold() if isinstance(username, str) else ""
        user = next((u for u in self.store.list("user") if u["username"] == normalized), None)
        # Use an existing hash for unknown users as well; never authenticate the fallback.
        candidate = user or self.store.get("user", ADMIN_ID)
        valid = self.password_matches(password, candidate.get("password_hash", ""))
        return public_user(user) if user and user.get("enabled") and valid else None

    def session_user(self, session_id: str):
        session = self.store.get("session", session_id) if session_id else None
        if not session or session.get("expires_at", 0) <= time.time():
            return None
        user_id = session.get("user_id") or (ADMIN_ID if session.get("user") == "admin" else None)
        user = self.store.get("user", user_id) if user_id else None
        return public_user(user) if user and user.get("enabled") else None

    def create(self, payload: dict):
        username = payload.get("username", "")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,63}", username):
            raise HTTPException(422, "用户名须为 2 至 64 位字母、数字、下划线、点或连字符")
        username = username.casefold()
        name = payload.get("name", username)
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise HTTPException(422, "显示名称须为 1 至 100 个字符")
        if payload.get("role", "operator") != "operator":
            raise HTTPException(422, "本次仅支持创建普通用户")
        password_hash = self.password_hash(payload.get("password"))
        def create():
            if any(u["username"] == username for u in self.store.list("user")):
                raise HTTPException(409, "用户名已存在")
            user = self.store.save("user", "user_" + secrets.token_hex(16), {"username": username, "name": name.strip(), "role": "operator", "enabled": True, "password_hash": password_hash})
            return public_user(user)
        return self.store.atomic(create)

    def state(self, id: str, enabled: bool):
        if not isinstance(enabled, bool):
            raise HTTPException(422, "enabled 必须为布尔值")
        def update():
            user = self.store.get("user", id)
            if not user:
                raise HTTPException(404, "用户不存在")
            if user["role"] == "admin" and not enabled:
                raise HTTPException(422, "不能停用初始管理员")
            user["enabled"] = enabled
            saved = self.store.save("user", id, user, user["revision"])
            if not enabled:
                for session in self.store.list("session"):
                    if session.get("user_id") == id:
                        self.store.delete("session", session["id"])
            return public_user(saved)
        return self.store.atomic(update)
